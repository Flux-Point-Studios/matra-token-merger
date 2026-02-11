"""
Thin wrappers around Blockfrost and TapTools HTTP APIs.

All functions return raw JSON-decoded dicts/lists.  Retry logic with
exponential back-off handles 429 / 5xx responses transparently.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from tools.config import (
    BLOCKFROST_BASE_URL,
    BLOCKFROST_PROJECT_ID,
    TAP_TOOLS_API_KEY,
    TAPTOOLS_BASE_URL,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_BACKOFF_BASE = 1.5  # seconds


def _request_with_retry(
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    max_retries: int = _MAX_RETRIES,
) -> Any:
    """Issue an HTTP request with exponential back-off on transient errors."""
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = _BACKOFF_BASE ** attempt
                logger.warning(
                    "HTTP %s from %s – retry %d/%d in %.1fs",
                    resp.status_code,
                    url,
                    attempt + 1,
                    max_retries,
                    wait,
                )
                time.sleep(wait)
                continue
            # Non-retryable error
            resp.raise_for_status()
        except requests.exceptions.ConnectionError as exc:
            if attempt < max_retries:
                wait = _BACKOFF_BASE ** attempt
                logger.warning("Connection error %s – retry in %.1fs", exc, wait)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Exhausted retries for {url}")


# ===================================================================
# Blockfrost client
# ===================================================================

class BlockfrostClient:
    """Minimal Blockfrost REST client with pagination support."""

    def __init__(
        self,
        project_id: str | None = None,
        base_url: str = BLOCKFROST_BASE_URL,
    ):
        self.base_url = base_url.rstrip("/")
        self.project_id = project_id or BLOCKFROST_PROJECT_ID
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
        """Auto-paginate a Blockfrost list endpoint."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, Any] = {
                "count": page_size,
                "page": page,
                **(extra_params or {}),
            }
            batch = self._get(path, params)
            if not batch:
                break
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

    def submit_tx(self, cbor_bytes: bytes) -> str:
        """Submit a signed transaction. Returns tx hash."""
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
        resp.raise_for_status()
        return resp.json()

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

    # -- protocol params -------------------------------------------------

    def get_protocol_parameters(self) -> dict[str, Any]:
        return self._get("/epochs/latest/parameters")


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
        return self._post(
            "/token/ohlcv",
            {
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
        """Get OHLCV candles for a specific pool."""
        return self._post(
            "/token/ohlcv",
            {
                "pairID": pool_id,
                "interval": interval,
                "numIntervals": num_intervals,
            },
        )

    def get_token_price(self, unit: str) -> dict[str, Any]:
        """Get current token price."""
        return self._post("/token/price", {"unit": unit})

    def get_ada_price(self) -> float:
        """Get current ADA/USD price."""
        data = self._get("/token/price/ada")
        return float(data.get("price", 0))
