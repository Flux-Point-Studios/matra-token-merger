"""
Thin wrappers around the chain API (Blockfrost-compatible: Iagon Insight by
default) and the TapTools and Koios HTTP APIs.

All functions return raw JSON-decoded dicts/lists.  Retry logic handles
429 / 5xx responses transparently.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests
from pycardano import BlockFrostChainContext

from tools.config import (
    KOIOS_API_KEY,
    KOIOS_BASE_URL,
    NETWORK,
    TAP_TOOLS_API_KEY,
    TAPTOOLS_BASE_URL,
    chain_api_base_url,
    chain_api_key,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_BACKOFF_BASE = 1.5  # seconds


class BlockfrostUnavailable(RuntimeError):
    """Every retry got a transient error (429 or 5xx), the API asked for a
    longer wait than the retries allow, or a list endpoint answered with
    something other than a list."""


class EvaluationFailed(RuntimeError):
    """The chain API did not give execution units for every redeemer."""


def _retry_after(resp: requests.Response) -> float | None:
    """The wait in seconds a response's Retry-After names, or None."""
    try:
        wait = float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return None
    return wait if wait >= 0 else None


def _request_with_retry(
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    data: str | None = None,
    max_retries: int = _MAX_RETRIES,
) -> Any:
    """Issue an HTTP request, retrying transient errors after the wait the
    API names in Retry-After, or else after an exponential back-off. The
    waits together stay within the back-off schedule's own total: an API
    asking for longer (a spent daily credit allowance asks until midnight
    UTC) is given up on at once."""
    budget = sum(_BACKOFF_BASE ** attempt for attempt in range(max_retries))
    waited = 0.0
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                timeout=60,
            )
        except requests.exceptions.ConnectionError as exc:
            if attempt == max_retries:
                raise
            wait = _BACKOFF_BASE ** attempt
            logger.warning("Connection error %s – retry in %.1fs", exc, wait)
            time.sleep(wait)
            waited += wait
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code not in (429, 500, 502, 503, 504):
            resp.raise_for_status()
            continue
        if attempt == max_retries:
            break
        named = _retry_after(resp)
        wait = _BACKOFF_BASE ** attempt if named is None else named
        if waited + wait > budget:
            raise BlockfrostUnavailable(
                f"HTTP {resp.status_code} from {url} asks for a {wait:g}s wait,"
                f" past the {budget:g}s retry budget"
            )
        logger.warning(
            "HTTP %s from %s – retry %d/%d in %.1fs",
            resp.status_code,
            url,
            attempt + 1,
            max_retries,
            wait,
        )
        time.sleep(wait)
        waited += wait
    raise BlockfrostUnavailable(f"Exhausted retries for {url}")


# ===================================================================
# Chain API client (Blockfrost-compatible)
# ===================================================================

class BlockfrostClient:
    """Minimal client for a Blockfrost-compatible REST API, with pagination
    support. Without arguments it uses the configured chain API and key."""

    def __init__(
        self,
        project_id: str | None = None,
        base_url: str | None = None,
    ):
        self.base_url = (base_url or chain_api_base_url(NETWORK)).rstrip("/")
        self.project_id = project_id or chain_api_key()
        self._headers = {"project_id": self.project_id}

    # -- low-level -------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        return _request_with_retry("GET", url, self._headers, params=params)

    def _get_all_pages(
        self,
        path: str,
        page_size: int = 100,
        extra_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Auto-paginate a Blockfrost list endpoint. A page that is not a
        list raises :class:`BlockfrostUnavailable`, so no other answer can
        end a listing. An empty or short page ends it: the provider is
        trusted to end a listing only where it ends."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, Any] = {
                "count": page_size,
                "page": page,
                **(extra_params or {}),
            }
            batch = self._get(path, params)
            if not isinstance(batch, list):
                raise BlockfrostUnavailable(f"{path} page {page}: {type(batch).__name__}, not a list")
            results.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return results

    # -- blocks ----------------------------------------------------------

    def get_latest_block(self) -> dict[str, Any]:
        return self._get("/blocks/latest")

    def get_block(self, hash_or_number: str | int) -> dict[str, Any]:
        return self._get(f"/blocks/{hash_or_number}")

    # -- assets ----------------------------------------------------------

    def get_asset_info(self, unit: str) -> dict[str, Any]:
        return self._get(f"/assets/{unit}")

    def get_asset_addresses(self, unit: str) -> list[dict[str, Any]]:
        """Return all addresses holding *unit* (auto-paged)."""
        return self._get_all_pages(f"/assets/{unit}/addresses")

    # -- transactions ----------------------------------------------------

    def get_tx_utxos(self, tx_hash: str) -> dict[str, Any]:
        return self._get(f"/txs/{tx_hash}/utxos")

    def get_tx(self, tx_hash: str) -> dict[str, Any]:
        return self._get(f"/txs/{tx_hash}")

    def tx_in_block(self, tx_hash: str) -> bool:
        """True once the transaction is in a block, False while the API does
        not know it (404). Any other failure raises."""
        try:
            self.get_tx(tx_hash)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return False
            raise
        return True

    def get_tx_cbor(self, tx_hash: str) -> bytes:
        """The transaction exactly as it sits on chain (body, witnesses, …)."""
        return bytes.fromhex(self._get(f"/txs/{tx_hash}/cbor")["cbor"])

    def submit_tx(self, cbor_bytes: bytes) -> str:
        """Submit a signed transaction. Returns tx hash.

        On rejection, raise with the Blockfrost/ledger response body included —
        the bare status code hides the actual ledger error (e.g. the precise
        UTxO/script failure), which is exactly what an operator needs to debug a
        failed submit."""
        url = f"{self.base_url}/tx/submit"
        resp = requests.post(
            url,
            headers={
                "project_id": self.project_id,
                "Content-Type": "application/cbor",
            },
            data=cbor_bytes,
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"tx submit failed [{resp.status_code}]: {resp.text[:1000]}"
            )
        return resp.json()

    def evaluate_tx(self, tx_cbor: bytes) -> dict[str, dict[str, int]]:
        """Run a transaction's scripts against the ledger without submitting
        it: execution units per redeemer, as {"spend:0": {"memory": ...,
        "steps": ...}}. Insight evaluates one transaction at a time per
        account and answers a concurrent one 429 or 503, which is retried.
        Raises :class:`EvaluationFailed` unless every redeemer got units."""
        answer = _request_with_retry(
            "POST",
            f"{self.base_url}/utils/txs/evaluate",
            {**self._headers, "Content-Type": "application/cbor"},
            data=tx_cbor.hex(),
        )
        result = answer.get("result") if isinstance(answer, dict) else None
        units = result.get("EvaluationResult") if isinstance(result, dict) else None
        if not isinstance(units, dict) or not all(
            isinstance(unit, dict) and type(unit.get("memory")) is int and type(unit.get("steps")) is int
            for unit in units.values()
        ):
            raise EvaluationFailed(json.dumps(answer)[:1000])
        return units

    # -- addresses -------------------------------------------------------

    def get_address_utxos(
        self,
        address: str,
        asset: str | None = None,
    ) -> list[dict[str, Any]]:
        path = f"/addresses/{address}/utxos"
        if asset:
            path += f"/{asset}"
        return self._get_all_pages(path)

    def get_address_transactions(self, address: str) -> list[dict[str, Any]]:
        """Every transaction touching *address*, oldest first (auto-paged)."""
        return self._get_all_pages(
            f"/addresses/{address}/transactions", extra_params={"order": "asc"},
        )

    # -- policy assets ---------------------------------------------------

    def get_policy_assets(self, policy_id: str) -> list[dict[str, Any]]:
        """Return all assets minted under *policy_id* (auto-paged)."""
        return self._get_all_pages(f"/assets/policy/{policy_id}")

    def get_asset_history(self, unit: str) -> list[dict[str, Any]]:
        """Every mint and burn of *unit*, oldest first (auto-paged)."""
        return self._get_all_pages(f"/assets/{unit}/history", extra_params={"order": "asc"})

    # -- protocol params -------------------------------------------------

    def get_protocol_parameters(self) -> dict[str, Any]:
        return self._get("/epochs/latest/parameters")

    def get_epoch_parameters(self, epoch: int) -> dict[str, Any]:
        return self._get(f"/epochs/{epoch}/parameters")


def chain_context(client: BlockfrostClient) -> BlockFrostChainContext:
    """pycardano's chain context on ``client``'s API and key. blockfrost-python
    appends the API version itself, so it gets the base URL without /v0."""
    return BlockFrostChainContext(
        project_id=client.project_id,
        base_url=client.base_url.removesuffix("/v0"),
    )


# ===================================================================
# TapTools client
# ===================================================================

class TapToolsClient:
    """Minimal TapTools REST client."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = TAPTOOLS_BASE_URL,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or TAP_TOOLS_API_KEY
        self._headers = {"x-api-key": self.api_key}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        return _request_with_retry("GET", url, self._headers, params=params)

    def _post(self, path: str, json_body: Any) -> Any:
        url = f"{self.base_url}{path}"
        return _request_with_retry(
            "POST", url, {**self._headers, "Content-Type": "application/json"},
            json_body=json_body,
        )

    # -- token prices / OHLCV -------------------------------------------

    def get_token_ohlcv(
        self,
        unit: str,
        interval: str = "1h",
        num_intervals: int = 168,
    ) -> list[dict[str, Any]]:
        """Get OHLCV candles for a token.

        *interval*: 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w
        """
        return self._get(
            "/token/ohlcv",
            params={
                "unit": unit,
                "interval": interval,
                "numIntervals": num_intervals,
            },
        )

    def get_token_pools(self, unit: str) -> list[dict[str, Any]]:
        """Return DEX pools for a token."""
        return self._get("/token/pools", params={"unit": unit})

    def get_token_pool_ohlcv(
        self,
        pool_id: str,
        interval: str = "1h",
        num_intervals: int = 168,
    ) -> list[dict[str, Any]]:
        """Get OHLCV candles for a specific pool via onchainID."""
        return self._get(
            "/token/ohlcv",
            params={
                "onchainID": pool_id,
                "interval": interval,
                "numIntervals": num_intervals,
            },
        )

    def get_token_price(self, unit: str) -> dict[str, Any]:
        """Get current token price."""
        return self._get("/token/prices", params={"unit": unit})

    def get_ada_price(self) -> float:
        """Get current ADA/USD price via /token/quote."""
        data = self._get("/token/quote", params={"quote": "USD"})
        return float(data.get("price", 0))

    # -- NFT collection endpoints ----------------------------------------

    def get_nft_collection_ohlcv(
        self,
        policy: str,
        interval: str = "1d",
        num_intervals: int = 7,
    ) -> list[dict[str, Any]]:
        """Get floor-price OHLCV candles for an NFT collection."""
        return self._get(
            "/nft/collection/ohlcv",
            params={
                "policy": policy,
                "interval": interval,
                "numIntervals": num_intervals,
            },
        )

    def get_nft_collection_stats(
        self,
        policy: str,
    ) -> dict[str, Any]:
        """Get collection stats (floor, supply, owners, volume)."""
        return self._get(
            "/nft/collection/stats",
            params={"policy": policy},
        )

    def get_nft_collection_holders_top(
        self,
        policy: str,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Get top holders of an NFT collection (paginated)."""
        return self._get(
            "/nft/collection/holders/top",
            params={
                "policy": policy,
                "page": page,
                "perPage": per_page,
            },
        )


# ===================================================================
# Koios client
# ===================================================================

class KoiosClient:
    """Minimal Koios REST client for cross-checking holder data."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = KOIOS_BASE_URL,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or KOIOS_API_KEY
        self._headers: dict[str, str] = {}
        if self.api_key:
            self._headers["authorization"] = f"Bearer {self.api_key}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        return _request_with_retry("GET", url, self._headers, params=params)

    def _post(self, path: str, json_body: Any) -> Any:
        url = f"{self.base_url}{path}"
        headers = {**self._headers, "Content-Type": "application/json"}
        return _request_with_retry("POST", url, headers, json_body=json_body)

    def get_asset_addresses(
        self,
        policy_id: str,
        asset_name_hex: str,
    ) -> list[dict[str, Any]]:
        """Return all addresses holding a specific asset (paginated).

        Koios uses offset-based pagination with a default limit of 1000.
        """
        all_results: list[dict[str, Any]] = []
        offset = 0
        limit = 1000
        while True:
            data = self._get(
                "/asset_addresses",
                params={
                    "_asset_policy": policy_id,
                    "_asset_name": asset_name_hex,
                    "offset": offset,
                    "limit": limit,
                },
            )
            if not data:
                break
            all_results.extend(data)
            if len(data) < limit:
                break
            offset += limit
        return all_results

    def get_asset_info(
        self,
        policy_id: str,
        asset_name_hex: str,
    ) -> list[dict[str, Any]]:
        """Return asset info (supply, metadata) from Koios."""
        return self._post(
            "/asset_info",
            json_body={
                "_asset_list": [[policy_id, asset_name_hex]],
            },
        )
