#!/usr/bin/env python3
"""
FastAPI microservice for the cMATRA merger portal.

Wraps tools/process_surrender.py to provide HTTP endpoints for:
  - Building partially-signed surrender transactions (admin co-signs)
  - Submitting fully-signed transactions
  - Pool status queries

Two-party co-signing flow:
  1. Frontend sends user's address + assets to surrender
  2. Server builds tx: pool UTxO (script) + user UTxOs → cMATRA to user + legacy to quarantine
  3. Server signs with admin key (partial: only admin witness)
  4. Returns partially-signed CBOR hex to frontend
  5. Frontend wallet adds user's signature via CIP-30 signTx
  6. Frontend sends fully-signed CBOR back for submission
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import cbor2
import httpx
from cbor2 import CBORTag
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Ensure project root is on path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / "env.local", override=True)
os.environ.setdefault("NETWORK", "mainnet")

from pycardano import (
    Address,
    Asset,
    AssetName,
    BlockFrostChainContext,
    ExecutionUnits,
    MultiAsset,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    RawPlutusData,
    Redeemer,
    Transaction,
    TransactionBody,
    TransactionBuilder,
    TransactionInput,
    TransactionOutput,
    TransactionWitnessSet,
    UTxO,
    Value,
    VerificationKey,
    VerificationKeyWitness,
)
from pycardano.exception import InvalidTransactionException
from pycardano.hash import ScriptHash as PycScriptHash, TransactionId
from pycardano.plutus import RedeemerKey, RedeemerMap, RedeemerTag

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import estimate_min_ada
from tools.config import FLUX_DECIMALS, ALL_MERGE_ASSETS
from tools.process_surrender import (
    compute_redemption,
    find_pool_utxos,
    load_rate_table,
    load_script_from_blueprint,
)
from services.pool_tip import (
    PoolSettlingError,
    PoolTipError,
    PoolTipManager,
    extract_pool_output,
)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

logger = logging.getLogger("surrender_api")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

ADMIN_SKEY_PATH: str = os.environ.get("ADMIN_SKEY_PATH", "")
RATE_TABLE_PATH: str = os.environ.get(
    "RATE_TABLE_PATH",
    str(_PROJECT_ROOT / "audit_pack/2026-03-11/rate_table_cmatra.json"),
)
BLUEPRINT_PATH: str = os.environ.get(
    "BLUEPRINT_PATH",
    str(_PROJECT_ROOT / "onchain/claim_validator/plutus.json"),
)
SCRIPT_ADDRESS: str = os.environ.get("SURRENDER_SCRIPT_ADDRESS", "")
CMATRA_POLICY_HEX: str = os.environ.get("CMATRA_POLICY_HEX", "")
CMATRA_ASSET_HEX: str = os.environ.get("CMATRA_ASSET_HEX", "")
QUARANTINE_ADDRESS: str = os.environ.get("QUARANTINE_ADDRESS", "")

# Allowed user addresses for /build-surrender. Comma-separated bech32. Empty
# string = no whitelist (window open to everyone). During the pre-launch
# closed-beta window we restrict to internal test wallets; before flipping
# the window public, unset this env (or set to empty).
ALLOWED_USER_ADDRESSES: frozenset[str] = frozenset(
    a.strip()
    for a in os.environ.get("ALLOWED_USER_ADDRESSES", "").split(",")
    if a.strip()
)

# CORS origins (flux1 site)
CORS_ORIGINS: list[str] = json.loads(
    os.environ.get(
        "CORS_ORIGINS",
        '["http://localhost:3000","https://fluxpointstudios.com","https://www.fluxpointstudios.com"]',
    )
)

API_PORT: int = int(os.environ.get("SURRENDER_API_PORT", "8420"))

# Redeemer-canonicalization strategy (0.18.0 re-validation, task #400).
#   "hybrid" — seed redeemer ex_units (so builder.build() SKIPS its internal
#             evaluate_tx, which on the chained path indexes the SPEND redeemer
#             to insertion order and 503s with extraRedeemers) THEN run the
#             Path-B CBOR surgery (canonical bare-list inputs + redeemer
#             re-index). The ONLY strategy that both builds the chained chunk
#             AND submits on pycardano 0.18.0. THIS IS THE MAINNET PATH.
#   "pathB" — _canonicalize_body_and_index_redeemer WITHOUT the seed: correct
#             body serialization, but builder.build()'s internal evaluate fires
#             on the chained path and rejects (extraRedeemers) before the
#             surgery runs. Confirmed-tip chunk works; chained chunk fails.
#   "pathA" — _canonicalize_via_builder: skips the internal evaluate via the
#             seed, but pycardano 0.18.0's _build_tx_body() re-emits inputs as a
#             hash-ordered tag-258 SET, so the wallet signature fails with
#             InvalidWitnessesUTXOW. Broken on 0.18.0 (written for 0.19.x).
CANON_STRATEGY: str = os.environ.get("CANON_STRATEGY", "hybrid").strip().lower()

# Dedicated collateral UTxO — bounds max collateral loss on phase-2 failure.
# Format: "txhash#index" (e.g. "abc123...#0"). If unset, pycardano auto-selects.
COLLATERAL_UTXO: str = os.environ.get("COLLATERAL_UTXO", "")

# Co-signer service (Server B) — required for dual-admin validator.
# The co-signer holds the second admin key on separate infrastructure.
COSIGNER_URL: str = os.environ.get("COSIGNER_URL", "")
COSIGNER_SECRET: str = os.environ.get("COSIGNER_API_SECRET", "")

# Shared secret — the Next.js proxy must send this in X-API-Secret header.
# Reject all requests without it.  Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
API_SECRET: str = os.environ.get("SURRENDER_API_SECRET", "")

# --- Pool-tip tx-chaining tunables (see services/pool_tip.py) ---
# Depth cap below the anchor-worker's 10 — surrender txs are much larger, so a
# shorter chain bounds the worst-case unwind. A wallet that needs more chunks
# than this proceeds in waves: at the cap, new builds 503 POOL_SETTLING until
# the pending root confirms, then the tip re-seeds.
POOL_TIP_DEPTH_CAP: int = int(os.environ.get("POOL_TIP_DEPTH_CAP", "8"))
# Signing-round-trip timeout: the single-flight lock is reclaimed if a build
# holds it longer than this (frontend never returned a signature). Tip
# unchanged on reclaim.
POOL_TIP_SIGNING_TIMEOUT_S: float = float(
    os.environ.get("POOL_TIP_SIGNING_TIMEOUT_S", "90")
)
# Eviction window: a pending tip whose root tx hasn't confirmed on Blockfrost
# within this many seconds is rolled back to confirmed state. MUST be well
# below pycardano's auto-TTL (~2.7h) so the rollback happens before the tx
# can no longer be resubmitted/expires.
POOL_TIP_EVICTION_WINDOW_S: float = float(
    os.environ.get("POOL_TIP_EVICTION_WINDOW_S", "240")
)
# Background watchdog tick.
POOL_TIP_WATCHDOG_INTERVAL_S: float = float(
    os.environ.get("POOL_TIP_WATCHDOG_INTERVAL_S", "30")
)

# ---------------------------------------------------------------------------
# CBOR constants
# ---------------------------------------------------------------------------

_PROCESS_SURRENDER_REDEEMER_CBOR = cbor2.dumps(CBORTag(121, []))
_VOID_DATUM_CBOR = cbor2.dumps(CBORTag(121, []))

# ---------------------------------------------------------------------------
# App state (loaded once on startup)
# ---------------------------------------------------------------------------


class AppState:
    """Mutable singleton holding loaded config — populated on startup."""
    rate_table: dict[str, Any] | None = None
    script_cbor_hex: str | None = None
    admin_sk: PaymentSigningKey | None = None
    admin_vk: PaymentVerificationKey | None = None
    admin_pkh: Any = None
    admin_addr: Address | None = None
    bf: BlockfrostClient | None = None

    # Tracks tx hashes built by this service.  submit-surrender only allows
    # submitting txs whose hash is in this set.  Entries expire after 10 min.
    pending_tx_hashes: dict[str, float] = {}  # tx_hash_hex -> created_at
    # Mirror map keyed by the same tx_hash, holding the admin-signed CBOR
    # bytes verbatim. At submit time we merge the wallet's partial witness
    # set into THIS exact byte sequence (preserving the body bytes the
    # wallet signed), then forward to Blockfrost.
    pending_tx_cbor: dict[str, bytes] = {}
    TX_HASH_TTL: float = 600.0  # 10 minutes

    # Pool-tip chainer. Replaces the old per-build Blockfrost re-query +
    # reserved_pool_utxos reservation set — the tip IS the reservation, held by
    # a single-flight lock from build through submit-accept.
    tip_mgr: PoolTipManager | None = None
    # Per-build context stashed at /build-surrender, consumed at
    # /submit-surrender so the tip advance can run the datum guard against the
    # exact pool output the tx carries. Keyed by build tx_hash.
    #   {build_token, delivered_cmatra, built_pool_output}
    build_ctx: dict[str, dict[str, Any]] = {}
    # Background watchdog handle (eviction + stuck-build sweep).
    watchdog_task: Any = None
    # Collateral UTxOs ("txhash#idx") handed out to in-flight chained surrenders.
    # Blockfrost lags the mempool, so while the pool tip is pending each chained
    # chunk would otherwise re-pick the SAME confirmed ADA-only collateral the
    # prior chunk already spent (double-spend -> ledger rejects). We exclude
    # reserved refs here and clear the set whenever the tip re-seeds to a
    # confirmed depth-0 state (the prior chain has settled). Mirrors the pool-tip
    # reservation discipline, applied to collateral.
    reserved_collateral: set[str] = set()
    # The surrendering user's pending change UTxOs, carried forward across a
    # chained surrender. Keyed by bech32 user address -> list of UTxO dicts
    # ({tx_hash, output_index, lovelace, multi_asset}) reconstructed from the
    # previous chunk's outputs that pay back to the user (their NFT/ADA change).
    # Blockfrost lags the mempool, so without this the next chunk re-selects the
    # already-spent NFT UTxO and the ledger rejects it (BadInputsUTxO). This is
    # the user-input analogue of the pool tip. Set after each chained build,
    # cleared per user when the tip re-seeds to confirmed. On the real frontend
    # the CIP-30 wallet supplies these; the backend tracks them so a single
    # wallet's multi-chunk surrender chains without per-chunk confirmation waits.
    user_pending: dict[str, list[dict[str, Any]]] = {}


state = AppState()

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AssetToSurrender(BaseModel):
    """One legacy asset the user is surrendering."""
    asset_key: str = Field(..., description="Merger asset key, e.g. 'AGENT', 'FLUX_PASS'")
    quantity_base: int = Field(..., gt=0, description="Base units (fungible) or NFT count")
    nft_units: list[str] | None = Field(
        None, description="Full unit hex IDs for NFTs being surrendered"
    )


class BuildSurrenderRequest(BaseModel):
    """Request to build a surrender transaction."""
    user_address: str = Field(..., description="User's bech32 Cardano address", min_length=40, max_length=120)
    assets: list[AssetToSurrender] = Field(..., min_length=1, max_length=7)


class BuildSurrenderResponse(BaseModel):
    tx_cbor_hex: str
    tx_hash: str
    redemption_summary: dict[str, dict[str, Any]]
    total_cmatra_display: float
    pool_utxo_used: str


class SubmitRequest(BaseModel):
    # Wallet-side partial witness set CBOR hex (what CIP-30 signTx with
    # partialSign=true returns). Holds the user's vkey witness only. The
    # server merges it into the admin-signed tx body stashed at build time.
    tx_cbor_hex: str = Field(
        ..., description="Wallet partial witness set CBOR hex",
        min_length=8, max_length=32_768, pattern=r"^[0-9a-fA-F]+$",
    )
    # tx_hash returned by /build-surrender. Required so the server can
    # locate the matching admin-signed tx body to merge into.
    tx_hash: str = Field(
        ..., description="tx_hash from the build-surrender response",
        min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]+$",
    )


class SubmitResponse(BaseModel):
    tx_hash: str


class PoolStatusResponse(BaseModel):
    pool_remaining_display: float
    pool_remaining_base: int
    utxo_count: int
    window_open: bool
    # In-memory pool-tip chain state (mirrors anchor-worker /status.chainState).
    # {utxo_ref, balance, status, depth} — None before the first build seeds it.
    # Lets the operator watch chaining live during the canary + 95-chunk drain.
    chainState: dict[str, Any] | None = None
    depthCap: int | None = None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="cMATRA Surrender API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Secret"],
)


@app.middleware("http")
async def verify_api_secret(request: Request, call_next):
    """Reject requests to mutating endpoints without valid API secret."""
    # Health and pool-status are read-only — allow without secret
    if request.url.path in ("/health", "/pool-status", "/docs", "/openapi.json"):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)

    if not API_SECRET:
        logger.error("SURRENDER_API_SECRET not set — refusing all mutating requests")
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"detail": "Service not configured"},
        )

    provided = request.headers.get("x-api-secret", "")
    if not provided or provided != API_SECRET:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=403,
            content={"detail": "Forbidden"},
        )

    return await call_next(request)


@app.on_event("startup")
async def startup():
    """Load config, rate table, script, and admin key on startup."""
    state.bf = BlockfrostClient()

    # Rate table
    if not Path(RATE_TABLE_PATH).exists():
        logger.error("Rate table not found: %s", RATE_TABLE_PATH)
        sys.exit(1)
    state.rate_table = load_rate_table(Path(RATE_TABLE_PATH))
    logger.info(
        "Loaded rate table: %d token(s)", len(state.rate_table.get("tokens", {}))
    )

    # Script
    if Path(BLUEPRINT_PATH).exists():
        state.script_cbor_hex = load_script_from_blueprint(BLUEPRINT_PATH)
        logger.info("Loaded script from blueprint (%d bytes)", len(state.script_cbor_hex) // 2)
    else:
        logger.warning("Blueprint not found: %s — build-surrender will fail", BLUEPRINT_PATH)

    # Validate script address has no staking credential (frankenaddress protection)
    if SCRIPT_ADDRESS:
        sa = Address.from_primitive(SCRIPT_ADDRESS)
        if sa.staking_part is not None:
            logger.warning(
                "SURRENDER_SCRIPT_ADDRESS has a staking credential — "
                "this may be a frankenaddress. Use an enterprise-type "
                "script address (no staking key) to prevent staking reward theft."
            )

    # Validate quarantine address has no staking credential
    if QUARANTINE_ADDRESS:
        qa = Address.from_primitive(QUARANTINE_ADDRESS)
        if qa.staking_part is not None:
            logger.warning(
                "QUARANTINE_ADDRESS has a staking credential — "
                "whoever controls that staking key earns rewards on burned ADA. "
                "Use an enterprise-type address to prevent this."
            )

    # Admin signing key
    if ADMIN_SKEY_PATH and Path(ADMIN_SKEY_PATH).exists():
        state.admin_sk = PaymentSigningKey.load(ADMIN_SKEY_PATH)
        state.admin_vk = PaymentVerificationKey.from_signing_key(state.admin_sk)
        state.admin_pkh = state.admin_vk.hash()
        state.admin_addr = Address(
            payment_part=state.admin_pkh,
            network=Address.from_primitive(SCRIPT_ADDRESS).network
            if SCRIPT_ADDRESS
            else 1,  # mainnet
        )
        logger.info("Admin key loaded: PKH=%s", state.admin_pkh.payload.hex())
    else:
        logger.warning(
            "Admin skey not configured (%s) — build-surrender will fail",
            ADMIN_SKEY_PATH,
        )

    # Pool-tip chainer. Seeds lazily on the first build (so startup never
    # fails when the chain is briefly unreachable), then chains in memory.
    if SCRIPT_ADDRESS and CMATRA_POLICY_HEX and CMATRA_ASSET_HEX:
        state.tip_mgr = PoolTipManager(
            seed_fn=_seed_pool_utxos,
            script_address=SCRIPT_ADDRESS,
            depth_cap=POOL_TIP_DEPTH_CAP,
            signing_timeout_s=POOL_TIP_SIGNING_TIMEOUT_S,
            eviction_window_s=POOL_TIP_EVICTION_WINDOW_S,
        )
        state.watchdog_task = asyncio.create_task(_pool_tip_watchdog())
        logger.info(
            "Pool-tip chainer armed (depth_cap=%d, eviction=%.0fs, "
            "signing_timeout=%.0fs)",
            POOL_TIP_DEPTH_CAP, POOL_TIP_EVICTION_WINDOW_S,
            POOL_TIP_SIGNING_TIMEOUT_S,
        )
    else:
        logger.warning(
            "Pool-tip chainer NOT armed — script/policy/asset not configured"
        )


@app.on_event("shutdown")
async def shutdown():
    """Cancel the background watchdog cleanly."""
    if state.watchdog_task is not None:
        state.watchdog_task.cancel()
        try:
            await state.watchdog_task
        except asyncio.CancelledError:
            pass


def _seed_pool_utxos() -> list[dict[str, Any]]:
    """Confirmed pool-UTxO list for the tip manager (largest-first, all
    carrying the Void datum — find_pool_utxos already enforces both).

    A re-seed means the prior chain has settled (or rolled back), so the
    in-flight collateral reservations and the user's carried-forward pending
    change no longer apply — clear them. Spent refs won't reappear from
    Blockfrost anyway; the next build re-selects from the confirmed chain."""
    state.reserved_collateral.clear()
    state.user_pending.clear()
    return find_pool_utxos(
        state.bf, SCRIPT_ADDRESS, CMATRA_POLICY_HEX, CMATRA_ASSET_HEX,
    )


def _tx_is_confirmed(tx_hash: str) -> bool:
    """True once Blockfrost has the tx in a block. Used by the eviction
    watchdog. ``get_tx_utxos`` 404s (raises) until the tx confirms."""
    try:
        state.bf.get_tx_utxos(tx_hash)
        return True
    except Exception:
        return False


async def _pool_tip_watchdog() -> None:
    """Independent background loop (guard 3 + signing-timeout sweep). Rolls a
    stalled pending tip back to Blockfrost-confirmed state and reclaims a
    build lock held past the signing timeout. The tip's correctness is checked
    against L1 here — never self-asserted."""
    mgr = state.tip_mgr
    assert mgr is not None
    while True:
        try:
            await asyncio.sleep(POOL_TIP_WATCHDOG_INTERVAL_S)
            await mgr.sweep_stuck_build()
            # Clear the depth cap when a maxed-out pending chain has confirmed
            # (resets depth -> POOL_SETTLING lifts; the next wave proceeds).
            await mgr.promote_if_confirmed(_tx_is_confirmed)
            await mgr.evict_if_stale(_tx_is_confirmed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pool-tip watchdog tick failed")


# ---------------------------------------------------------------------------
# Build surrender — two-party co-signing
# ---------------------------------------------------------------------------


def _build_asset_lookup() -> dict[str, Any]:
    """Build name→asset info lookup from config."""
    return {a.name: a for a in ALL_MERGE_ASSETS}


def _resolve_legacy_assets(
    asset_key: str,
    quantity_base: int,
    nft_units: list[str] | None,
    asset_lookup: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve legacy asset details for the quarantine output."""
    info = asset_lookup.get(asset_key)
    if info is None:
        raise HTTPException(400, f"Unknown asset key: {asset_key}")

    if hasattr(info, "asset_name_hex"):
        # Fungible token
        return [{
            "policy_hex": info.policy_id,
            "asset_hex": info.asset_name_hex,
            "quantity": quantity_base,
        }]
    else:
        # NFT collection — each NFT is a separate asset
        if not nft_units:
            raise HTTPException(400, f"NFT surrender requires nft_units list for {asset_key}")
        result = []
        for unit_hex in nft_units:
            policy_hex = unit_hex[:56]
            asset_hex = unit_hex[56:]
            result.append({
                "policy_hex": policy_hex,
                "asset_hex": asset_hex,
                "quantity": 1,
            })
        return result


def _build_tx_blocking(
    user_address: str,
    total_cmatra: int,
    legacy_assets: list[dict[str, Any]],
    pool_utxo: dict[str, Any],
    pool_tip_pending: bool = False,
    user_inputs: list[dict[str, Any]] | None = None,
) -> tuple[str, str, dict[str, Any] | None, list[dict[str, Any]]]:
    """Build + preflight a surrender tx (blocking pycardano + Blockfrost work).
    Runs inside ``asyncio.to_thread`` so it never blocks the event loop or the
    single-flight lock holder. Raises on build/preflight failure; the async
    caller translates to HTTP and releases the lock.

    ``pool_tip_pending`` is True when this surrender chains off a still-
    unconfirmed pool-change output (depth > 0). Blockfrost's evaluate_tx only
    resolves CONFIRMED UTxOs, so it cannot evaluate a tx whose pool input is in
    the mempool — it spuriously reports the (correctly indexed) SPEND redeemer
    as ``extraRedeemers``. On that path the mandatory ledger preflight is
    skipped: the generous seed execution units (set in the builder) cover the
    trivial validator, and the script was already proven valid by the confirmed
    root of the same chain. The submit itself is the authoritative check."""
    tx_cbor_hex, tx_hash, built_pool_output, user_change = _build_cosigned_surrender_tx(
        user_address=user_address,
        total_cmatra=total_cmatra,
        legacy_assets=legacy_assets,
        pool_utxo=pool_utxo,
        user_inputs=user_inputs,
    )
    if pool_tip_pending:
        logger.info(
            "Preflight SKIPPED for tx %s (chained off unconfirmed tip; "
            "Blockfrost evaluate cannot resolve mempool inputs)", tx_hash[:16]
        )
        return tx_cbor_hex, tx_hash, built_pool_output, user_change
    # Mandatory preflight (confirmed-tip path): evaluate_tx against the live
    # ledger before returning to the user. Catches script errors before any
    # collateral is at risk.
    preflight_ctx = BlockFrostChainContext(
        project_id=state.bf.project_id,
        base_url=state.bf.base_url.rstrip("/").removesuffix("/v0"),
    )
    preflight_ctx.evaluate_tx_cbor(bytes.fromhex(tx_cbor_hex))
    logger.info("Preflight OK for tx %s", tx_hash[:16])
    return tx_cbor_hex, tx_hash, built_pool_output, user_change


@app.post("/build-surrender", response_model=BuildSurrenderResponse)
async def build_surrender(req: BuildSurrenderRequest):
    """Build a partially-signed surrender transaction against the pool TIP.

    The server reads the in-memory pool tip (chained off the prior surrender's
    pending change output — no Blockfrost re-query, so chunk N+1 sees chunk N
    immediately), builds the full transaction, signs with the admin key, and
    returns the CBOR for the wallet to co-sign. The single-flight lock is held
    from here through /submit-surrender so two surrenders never target the same
    tip; the tip advances ONLY after submit returns mempool-accept.
    """
    # Validate user address format
    if not req.user_address.startswith("addr1") and not req.user_address.startswith("addr_test1"):
        raise HTTPException(400, "Invalid Cardano address")

    # Reject script-payment addresses — cMATRA must go to a key-controlled
    # wallet, not another script.  Prevents sending funds to an address the
    # user can't spend from (e.g. a frankenaddress with a script payment part).
    try:
        user_addr_check = Address.from_primitive(req.user_address)
        if isinstance(user_addr_check.payment_part, PycScriptHash):
            raise HTTPException(400, "User address must be a wallet address, not a script address")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Invalid Cardano address encoding")

    # Closed-beta whitelist gate. If ALLOWED_USER_ADDRESSES is non-empty, only
    # those exact bech32 addresses may build a surrender tx. Empty = open
    # window (production behavior). Enforced before any chain query, key load,
    # or tip acquisition, so a non-allowed caller can never observe state or
    # consume the single-flight lock.
    if ALLOWED_USER_ADDRESSES and req.user_address not in ALLOWED_USER_ADDRESSES:
        raise HTTPException(403, "Surrender window not yet open")

    # Validate state
    if not state.rate_table:
        raise HTTPException(503, "Rate table not loaded")
    if not state.script_cbor_hex:
        raise HTTPException(503, "Surrender script not loaded")
    if not state.admin_sk:
        raise HTTPException(503, "Admin key not configured")
    if not SCRIPT_ADDRESS:
        raise HTTPException(503, "Script address not configured")
    if not CMATRA_POLICY_HEX or not CMATRA_ASSET_HEX:
        raise HTTPException(503, "cMATRA policy/asset not configured")
    if not QUARANTINE_ADDRESS:
        raise HTTPException(503, "Quarantine address not configured")
    if state.tip_mgr is None:
        raise HTTPException(503, "Pool-tip chainer not initialized")

    asset_lookup = _build_asset_lookup()

    # Compute total cMATRA owed
    total_cmatra = 0
    redemption_summary: dict[str, dict[str, Any]] = {}
    all_legacy_assets: list[dict[str, Any]] = []

    for item in req.assets:
        try:
            cmatra_amount = compute_redemption(
                state.rate_table, item.asset_key, item.quantity_base,
            )
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))

        total_cmatra += cmatra_amount
        redemption_summary[item.asset_key] = {
            "quantity_base": item.quantity_base,
            "cmatra_base": cmatra_amount,
            "cmatra_display": cmatra_amount / (10 ** FLUX_DECIMALS),
        }

        legacy = _resolve_legacy_assets(
            item.asset_key, item.quantity_base, item.nft_units, asset_lookup,
        )
        all_legacy_assets.extend(legacy)

    # Acquire the single-flight lock and read the pool tip. Cold-start seeds
    # from Blockfrost (largest confirmed UTxO); thereafter the tip is the prior
    # surrender's pending change output. Held until /submit-surrender accepts
    # (advance) or a failure/timeout releases it.
    build_token = uuid.uuid4().hex
    try:
        tip = await state.tip_mgr.acquire_for_build(build_token)
    except PoolSettlingError:
        # Depth cap reached — lock already released by the manager. The wallet
        # should retry after the pending root confirms.
        raise HTTPException(
            503,
            detail={
                "code": "POOL_SETTLING",
                "message": "Pool settling, retry in ~20s",
            },
        )
    except PoolTipError as e:
        # Could not seed the tip from chain (no confirmed pool UTxO).
        logger.error("Tip acquisition failed: %s", e)
        raise HTTPException(503, "No pool UTxOs available")

    # From here the lock is HELD — every exit path must advance or release it.
    pool_utxo = tip.as_pool_utxo_dict()
    if pool_utxo["cmatra_amount"] < total_cmatra:
        state.tip_mgr.release_build(build_token)
        raise HTTPException(
            503,
            "Pool tip has insufficient cMATRA for this surrender. "
            "It may be settling — please retry shortly.",
        )

    # Build + preflight off the event loop (blocking pycardano + Blockfrost).
    # A pending tip (depth > 0) chains off an unconfirmed mempool output, which
    # Blockfrost evaluate cannot resolve — the build path skips the hard
    # preflight in that case (see _build_tx_blocking). On that path we also feed
    # the user's pending change UTxOs forward as explicit inputs (Blockfrost
    # can't yet see them), the user-input analogue of the pool tip.
    user_inputs = (
        state.user_pending.get(req.user_address)
        if tip.status == "pending" else None
    )
    try:
        tx_cbor_hex, tx_hash, built_pool_output, user_change = await asyncio.to_thread(
            _build_tx_blocking,
            req.user_address, total_cmatra, all_legacy_assets, pool_utxo,
            tip.status == "pending", user_inputs,
        )
    except InvalidTransactionException as e:
        state.tip_mgr.release_build(build_token)
        size_exc = _tx_too_large_http_exception(
            e, user_address=req.user_address,
            assets_count=len(all_legacy_assets),
        )
        if size_exc is not None:
            raise size_exc
        logger.exception("Failed to build surrender tx for %s", req.user_address[:24])
        raise HTTPException(500, "Transaction build failed. Please try again.")
    except Exception:
        state.tip_mgr.release_build(build_token)
        logger.exception("Failed to build/preflight surrender tx for %s", req.user_address[:24])
        raise HTTPException(422, "Transaction preflight failed. Please retry.")

    # Register the tx hash so submit-surrender will accept it, stash the exact
    # admin-signed CBOR (so the wallet's witness merge preserves the body), and
    # stash the tip-advance context (delivered amount + the pool output the
    # datum guard will check at submit time).
    _prune_expired_tx_hashes()
    state.pending_tx_hashes[tx_hash] = time.time()
    state.pending_tx_cbor[tx_hash] = bytes.fromhex(tx_cbor_hex)
    state.build_ctx[tx_hash] = {
        "build_token": build_token,
        "delivered_cmatra": total_cmatra,
        "built_pool_output": built_pool_output,
        "user_address": req.user_address,
        "user_change": user_change,
    }

    return BuildSurrenderResponse(
        tx_cbor_hex=tx_cbor_hex,
        tx_hash=tx_hash,
        redemption_summary=redemption_summary,
        total_cmatra_display=total_cmatra / (10 ** FLUX_DECIMALS),
        pool_utxo_used=tip.utxo_ref,
    )


def _fetch_cosigner_pkh() -> None:
    """Fetch and cache the co-signer's PKH from their health endpoint."""
    try:
        resp = httpx.get(f"{COSIGNER_URL}/health", timeout=5.0)
        data = resp.json()
        pkh_hex = data.get("pkh", "").replace("...", "")
        if pkh_hex and len(pkh_hex) >= 56:
            # Health returns truncated PKH — use the /cosign endpoint to get full PKH
            # For now, fetch it by doing a dummy cosign or reading from env
            pass
        logger.info("Co-signer service reachable at %s", COSIGNER_URL)
    except Exception as e:
        logger.warning("Co-signer service unreachable: %s", e)

    # Read co-signer PKH from env (most reliable — set during deployment)
    cosigner_pkh_hex = os.environ.get("COSIGNER_PKH", "")
    if cosigner_pkh_hex:
        from pycardano.hash import VerificationKeyHash
        state.cosigner_pkh = VerificationKeyHash(bytes.fromhex(cosigner_pkh_hex))
        logger.info("Co-signer PKH: %s", cosigner_pkh_hex[:16])
    else:
        state.cosigner_pkh = None
        logger.warning("COSIGNER_PKH not set — dual-admin mode disabled")


def _get_cosigner_witness(tx_hash_hex: str) -> VerificationKeyWitness:
    """Call the co-signer service to sign a transaction hash.

    Returns a VerificationKeyWitness ready to merge into the witness set.
    """
    try:
        resp = httpx.post(
            f"{COSIGNER_URL}/cosign",
            json={"tx_hash_hex": tx_hash_hex},
            headers={"X-API-Secret": COSIGNER_SECRET},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()

        vk = VerificationKey.from_primitive(bytes.fromhex(data["vkey_hex"]))
        sig = bytes.fromhex(data["signature_hex"])
        return VerificationKeyWitness(vk, sig)

    except httpx.HTTPStatusError as e:
        logger.error("Co-signer returned %d: %s", e.response.status_code, e.response.text[:200])
        raise HTTPException(503, "Co-signer service error")
    except Exception as e:
        logger.error("Co-signer call failed: %s", e)
        raise HTTPException(503, "Co-signer service unavailable")


_TX_SIZE_RE = re.compile(
    r"Transaction size \((\d+)\) exceeds the max limit \((\d+)\)"
)


def _tx_too_large_http_exception(
    exc: InvalidTransactionException,
    *,
    user_address: str,
    assets_count: int,
) -> HTTPException | None:
    """If `exc` is the protocol-size case, return an actionable 413 the
    frontend can parse for auto-chunking. Other invalidity reasons
    (insufficient ADA, datum-mismatch, missing collateral, ...) are NOT
    matched — the caller should re-raise so they surface as a 500 and we
    can investigate.
    """
    msg = str(exc)
    match = _TX_SIZE_RE.search(msg)
    if not match:
        return None
    tx_size_bytes = int(match.group(1))
    max_tx_size_bytes = int(match.group(2))
    logger.warning(
        "Surrender tx too large: user_address=%s assets_count=%d "
        "tx_size_bytes=%d max_tx_size_bytes=%d",
        user_address[:24] + "...",
        assets_count,
        tx_size_bytes,
        max_tx_size_bytes,
    )
    return HTTPException(
        status_code=413,
        detail={
            "code": "TX_TOO_LARGE",
            "message": (
                "Surrender request too large for one transaction. "
                "Please surrender fewer assets at a time and try again — "
                "the portal will guide you through batching."
            ),
            "tx_size_bytes": tx_size_bytes,
            "max_tx_size_bytes": max_tx_size_bytes,
            "suggested_batch_size": 4,
        },
    )


def _canonicalize_body_and_index_redeemer(
    tx_body: TransactionBody,
    witness_set: TransactionWitnessSet,
    script_input: TransactionInput,
) -> TransactionBody:
    """Rewrite the built tx body so its ``inputs`` are in the ledger's canonical
    order, emitted as a bare definite-length CBOR array, and point the
    ProcessSurrender SPEND redeemer at the script input's position in that
    order. Returns the rebuilt (canonical) ``TransactionBody``; mutates
    ``witness_set`` in place for the redeemer index.

    cbor2's C-encoder re-orders a tag-258 ("set") value by element hash at
    ``dumps()`` time, so the on-wire input order pycardano produces is
    hash-seed-dependent and does NOT match the canonical sort the ledger
    evaluates against. The builder assigns the redeemer index against its own
    pre-encode order, so on the chained surrender path — where the script
    input's tx_id changes every tx — the redeemer lands on the wrong input
    roughly a third of the time and the node rejects the tx with
    ``extraRedeemers=['spend:<n>']``.

    The fix sorts the inputs ourselves and re-emits them as a bare array (not a
    tag-258 set): a plain list is left untouched by cbor2's encoder, so the
    bytes that go on the wire stay in the order we computed, and the redeemer
    index matches the position the ledger derives. A bare inputs array is
    Conway-legal (the ledger canonicalizes inputs regardless of wire framing)
    and ``TransactionBody.from_cbor`` round-trips it.

    MUST run before any signature is computed: the admin sign, the co-signer
    sign, and the wallet's CIP-30 witness all cover ``tx_body.hash()`` over
    THIS canonical body. Outputs (including the pool-change Void datum on
    output[1]), fee, and every other body field are preserved verbatim — only
    the input ordering and the redeemer index change.
    """
    redeemers = witness_set.redeemer
    if redeemers is None:
        raise ValueError("Surrender tx has no redeemers — script spend is missing")

    body = cbor2.loads(tx_body.to_cbor())
    raw_inputs = body[0]
    if isinstance(raw_inputs, CBORTag):  # set-tagged (#6.258)
        raw_inputs = raw_inputs.value
    # Conway's canonical input order: ascending by (tx_id bytes, output index).
    canonical = sorted(
        ((bytes(e[0]), e[1]) for e in raw_inputs), key=lambda p: (p[0], p[1])
    )
    body[0] = [list(p) for p in canonical]
    canonical_body = TransactionBody.from_cbor(cbor2.dumps(body))

    target_index = canonical.index(
        (bytes(script_input.transaction_id), script_input.index)
    )

    if isinstance(redeemers, RedeemerMap):
        spend_keys = [k for k in redeemers if k.tag == RedeemerTag.SPEND]
        if len(spend_keys) != 1:
            raise ValueError(
                f"Expected exactly 1 SPEND redeemer, found {len(spend_keys)}"
            )
        old_key = spend_keys[0]
        if old_key.index != target_index:
            value = redeemers.data.pop(old_key)
            redeemers.data[RedeemerKey(RedeemerTag.SPEND, target_index)] = value
    else:  # legacy list[Redeemer] (use_redeemer_map=False)
        spend = [r for r in redeemers if r.tag == RedeemerTag.SPEND]
        if len(spend) != 1:
            raise ValueError(
                f"Expected exactly 1 SPEND redeemer, found {len(spend)}"
            )
        spend[0].index = target_index

    return canonical_body


def _canonicalize_via_builder(
    builder: TransactionBuilder,
    context: BlockFrostChainContext,
    script: PlutusV3Script,
) -> TransactionBody:
    """Re-emit the built tx with inputs in the ledger's canonical order, the
    SPEND redeemer indexed to the script input's position in that order, real
    execution units, and a matching ``script_data_hash`` — all via pycardano's
    own builder machinery so every committed field is mutually consistent.

    Why this is needed (the bug this rehearsal exposed on a real chain):
    pycardano 0.19.x emits inputs in INSERTION order on the wire and indexes the
    SPEND redeemer to that order, but the ledger sorts inputs canonically
    (ascending by tx_id, index) and re-derives the redeemer position from the
    sorted set. On the pool-tip chained path the script (pool) input's tx_id
    changes every surrender, so insertion order diverges from canonical roughly
    a third of the time and the node rejects the tx with
    ``extraRedeemers=['spend:<n>']`` — at submit AND inside the builder's own
    execution-unit estimate. The earlier CBOR-surgery fix rewrote only the final
    body and ran after that estimate, so it could not save the chained path.

    The fix: after ``builder.build()`` (which skipped its broken internal
    evaluate because the redeemer carried seed ex_units), sort ``builder.inputs``
    canonically, let the builder re-index the redeemer (``_set_redeemer_index``)
    and rebuild the body (``_build_tx_body``) — now inputs and redeemer index
    agree with the ledger. Evaluate that canonical body to get real units, set
    them on the builder's redeemer, then rebuild once more and stamp the
    builder-computed ``script_data_hash`` (which uses pycardano's exact cost-model
    encoding, so it matches the ledger's PPViewHash). MUST run before any
    signature; the body hash covers input order, redeemer index, and
    script_data_hash.
    """
    # 1) Canonical input order (ascending by tx_id bytes, then index).
    builder.inputs.sort(key=lambda u: (bytes(u.input.transaction_id), u.input.index))
    builder._set_redeemer_index()

    # 2) Measure real execution units against the canonical body. Build with the
    #    builder's fake witnesses (vkey witnesses do not affect Plutus cost).
    canonical_body = builder._build_tx_body()
    fake_ws = builder._build_fake_witness_set()
    try:
        result = context.evaluate_tx_cbor(
            Transaction(canonical_body, fake_ws).to_cbor()
        )
        for key, units in (result.items() if hasattr(result, "items")
                           else vars(result).items()):
            tagname, _, idx = str(key).partition(":")
            if tagname.lower() != "spend":
                continue
            try:
                target = int(idx)
            except ValueError:
                continue
            mem = getattr(units, "mem", None)
            steps = getattr(units, "steps", None)
            if mem is None and isinstance(units, dict):
                mem, steps = units.get("mem"), units.get("steps")
            for r in builder._redeemer_list:
                if r.tag == RedeemerTag.SPEND and r.index == target and mem is not None:
                    r.ex_units = ExecutionUnits(
                        int(mem * (1 + builder.execution_memory_buffer)),
                        int(steps * (1 + builder.execution_step_buffer)),
                    )
        logger.info("Measured canonical execution units from ledger evaluate")
    except Exception as e:
        logger.warning("Canonical ex-unit evaluate failed (%s); keeping seed units", e)

    # 3) Rebuild the body with final units and stamp the builder-computed
    #    script_data_hash so the body commits to the patched redeemers exactly
    #    as the ledger recomputes it.
    canonical_body = builder._build_tx_body()
    canonical_body.script_data_hash = builder.script_data_hash
    return canonical_body



def _select_ada_only_collateral(
    context: BlockFrostChainContext, user_addr: Address,
    min_lovelace: int = 5_000_000,
) -> "UTxO | None":
    """Return the smallest ADA-only UTxO at ``user_addr`` with at least
    ``min_lovelace`` (a valid Plutus collateral — token-bearing UTxOs are
    rejected by the ledger with CollateralContainsNonADA), excluding any
    collateral ref already reserved for an in-flight chained surrender.

    The reservation is what makes collateral selection correct on the chained
    path: Blockfrost still lists a confirmed ADA-only UTxO as available after a
    pending chunk has spent it, so without the exclusion every chained chunk
    would re-pick — and double-spend — the same collateral. None if the wallet
    has no eligible pure-ADA UTxO (a token-only wallet must supply collateral via
    its CIP-30 wallet on the real frontend)."""
    try:
        utxos = context.utxos(user_addr)
    except Exception as e:
        logger.warning("Collateral scan failed for %s: %s", str(user_addr)[:24], e)
        return None
    candidates = [
        u for u in utxos
        if (u.output.amount.multi_asset is None
            or len(u.output.amount.multi_asset) == 0)
        and u.output.amount.coin >= min_lovelace
        and f"{u.input.transaction_id}#{u.input.index}" not in state.reserved_collateral
    ]
    if not candidates:
        return None
    # Smallest qualifying UTxO — keeps large ADA free for fees/outputs and
    # bounds the collateral at risk.
    return min(candidates, key=lambda u: u.output.amount.coin)


def _confirmed_ada_only_utxos(
    context: BlockFrostChainContext, user_addr: Address,
    exclude_reserved: bool = False, min_lovelace: int = 2_000_000,
) -> list["UTxO"]:
    """Confirmed pure-ADA UTxOs at ``user_addr`` (>= ``min_lovelace``), optionally
    excluding refs reserved as in-flight collateral. Offered as
    ``potential_inputs`` on the chained path so pycardano has fee/change ADA
    beyond the forced pending change without re-selecting the already-spent
    NFT UTxO from Blockfrost's lagged view."""
    try:
        utxos = context.utxos(user_addr)
    except Exception as e:
        logger.warning("ADA-only scan failed for %s: %s", str(user_addr)[:24], e)
        return []
    out = []
    for u in utxos:
        if u.output.amount.multi_asset and len(u.output.amount.multi_asset) > 0:
            continue
        if u.output.amount.coin < min_lovelace:
            continue
        if exclude_reserved and (
            f"{u.input.transaction_id}#{u.input.index}" in state.reserved_collateral
        ):
            continue
        out.append(u)
    return out


def _build_cosigned_surrender_tx(
    user_address: str,
    total_cmatra: int,
    legacy_assets: list[dict[str, Any]],
    pool_utxo: dict[str, Any],
    user_inputs: list[dict[str, Any]] | None = None,
) -> tuple[str, str, dict[str, Any] | None, list[dict[str, Any]]]:
    """Build a surrender tx and partially sign with admin key(s).

    Transaction structure:
      Inputs:
        - Pool UTxO (script input, ProcessSurrender redeemer)
        - User's UTxOs (selected automatically by builder from user_address)
      Outputs:
        1. cMATRA to user_address
        2. Remaining pool balance back to script_address (void datum)
        3. Legacy assets to quarantine_address
      Required signer: admin PKH

    The builder auto-selects user UTxOs for the legacy assets + fees.
    Admin signs the script spend. User must add their signature via wallet.

    ``user_inputs`` (chained path): the user's pending change UTxOs from the
    prior chunk, added as EXPLICIT inputs instead of Blockfrost address
    selection. Blockfrost lags the mempool, so on a chained surrender it still
    lists the prior chunk's now-spent NFT UTxO as available; selecting it yields
    BadInputsUTxO. Feeding the known pending change forward (the user-input
    analogue of the pool tip) lets a single wallet chain chunks without waiting
    for each to confirm. None on the first (confirmed-tip) chunk -> normal
    address selection.

    Returns (tx_cbor_hex, tx_hash_hex, built_pool_output, user_change_utxos).
    The third element is output[1] read back from the assembled tx body —
    {address, datum_hex, cmatra_amount, ada_amount} — or None if no pool-change
    output exists (full drain). The fourth is the list of this tx's outputs that
    pay back to the user ({tx_hash, output_index, lovelace, multi_asset}) — the
    pending change to feed the next chained chunk.
    """
    # NOTE: state.bf is our custom BlockfrostClient (tools/api_clients.py)
    # which stores base_url WITH '/v0' (e.g. ".../api/v0") because it builds
    # request URLs as `base_url + path`. pycardano's BlockFrostChainContext,
    # however, hands the URL to blockfrost-python's BlockFrostApi which
    # AUTO-APPENDS '/v0'. So passing state.bf.base_url verbatim gives
    # '/api/v0/v0/...' = 400 Invalid path. Strip the suffix.
    context = BlockFrostChainContext(
        project_id=state.bf.project_id,
        base_url=state.bf.base_url.rstrip("/").removesuffix("/v0"),
    )

    script = PlutusV3Script(bytes.fromhex(state.script_cbor_hex))
    cmatra_policy = PycScriptHash(bytes.fromhex(CMATRA_POLICY_HEX))
    cmatra_asset = AssetName(bytes.fromhex(CMATRA_ASSET_HEX))

    # Reconstruct pool UTxO
    pool_multi = MultiAsset()
    pool_multi[cmatra_policy] = Asset({cmatra_asset: pool_utxo["cmatra_amount"]})
    pool_value = Value(pool_utxo["ada_amount"], pool_multi)
    pool_datum = RawPlutusData(cbor2.loads(_VOID_DATUM_CBOR))

    script_addr = Address.from_primitive(SCRIPT_ADDRESS)
    tx_in = TransactionInput(
        TransactionId(bytes.fromhex(pool_utxo["tx_hash"])),
        pool_utxo["output_index"],
    )
    utxo = UTxO(
        tx_in,
        TransactionOutput(script_addr, pool_value, datum=pool_datum),
    )

    redeemer = Redeemer(RawPlutusData(cbor2.loads(_PROCESS_SURRENDER_REDEEMER_CBOR)))
    if CANON_STRATEGY in ("patha", "hybrid"):
        # Seed the redeemer with generous execution units so pycardano's builder
        # SKIPS its internal evaluate_tx (it sets _should_estimate_execution_units
        # False when a redeemer already carries ex_units). That internal evaluate
        # indexes the SPEND redeemer to pycardano's INSERTION order, but the ledger
        # (and Blockfrost's evaluate) sort inputs canonically — on the chained
        # surrender path the script input's tx_id sorts AFTER a wallet input, so the
        # internal evaluate rejects the tx with extraRedeemers BEFORE our canonical
        # fix can run. Seeding lets build() succeed; the canonical fix then runs.
        # The seed (2M mem / 900M steps) is a safe upper bound for the trivial
        # `expect Some(_)` + dual-sig validator (measured units are ~60k mem /
        # 19.5M steps; protocol max is 16.5M mem / 10B steps), so it is always a
        # valid over-estimate. hybrid keeps this seed on the final tx (it only
        # inflates the fee buffer); pathA re-measures and patches exact units.
        redeemer.ex_units = ExecutionUnits(2_000_000, 900_000_000)

    builder = TransactionBuilder(context)

    # Script input: pool UTxO
    builder.add_script_input(utxo, script=script, redeemer=redeemer)

    # User's inputs (legacy assets being surrendered + fee contribution).
    # IMPORTANT: never add admin_addr as an input source — admin is only a
    # script-spend co-signer; the pool UTxO is added explicitly above.
    user_addr = Address.from_primitive(user_address)

    # Collateral selection FIRST (so it can be excluded from the regular
    # fee/change candidates below). Plutus collateral MUST be ADA-only — if
    # pycardano auto-selects it from the tx inputs it can pick a token-bearing
    # UTxO and the ledger rejects the tx with CollateralContainsNonADA. On the
    # chained surrender path this is the common case: the user's only large UTxO
    # is the accumulated pool-surrender change bundling the remaining NFTs with
    # the ADA, so we set an explicit ADA-only collateral.
    if COLLATERAL_UTXO and "#" in COLLATERAL_UTXO:
        # Operator-pinned dedicated collateral (bounds max collateral loss).
        # NOTE: the builder field is `collaterals` (a NonEmptyOrderedSet) — a
        # singular `builder.collateral = [...]` is silently ignored, leaving
        # pycardano to auto-select a (possibly token-bearing) input.
        col_hash, col_idx = COLLATERAL_UTXO.rsplit("#", 1)
        col_input = TransactionInput(
            TransactionId(bytes.fromhex(col_hash)), int(col_idx),
        )
        col_utxo = UTxO(
            col_input,
            TransactionOutput(state.admin_addr, Value(5_000_000)),  # 5 ADA
        )
        builder.collaterals.append(col_utxo)
    else:
        # Pick a small ADA-only UTxO from the user's address for collateral,
        # excluding any reserved for an in-flight chained chunk, then reserve it
        # so the next chained chunk does not re-pick (and double-spend) it.
        ada_only = _select_ada_only_collateral(context, user_addr)
        if ada_only is not None:
            builder.collaterals.append(ada_only)
            col_ref = f"{ada_only.input.transaction_id}#{ada_only.input.index}"
            state.reserved_collateral.add(col_ref)
            logger.info("Collateral set: %s (%d lovelace, ADA-only); reserved=%d",
                        col_ref[:18], ada_only.output.amount.coin,
                        len(state.reserved_collateral))
        else:
            logger.warning("No unreserved ADA-only collateral UTxO at user "
                           "address — pycardano will auto-select (may pick a "
                           "token UTxO; a chained run can exhaust pure-ADA UTxOs)")

    # User's inputs (legacy assets being surrendered + fee contribution).
    # IMPORTANT: never add admin_addr as an input source — admin is only a
    # script-spend co-signer; the pool UTxO is added explicitly above.
    if user_inputs:
        # Chained path: spend the user's known pending change UTxOs explicitly
        # (Blockfrost can't yet see them as spent/created), so the NFTs being
        # surrendered — which live in the prior chunk's mempool change — are
        # actually present. pycardano still appends change back to user_addr.
        for ui in user_inputs:
            ui_multi = MultiAsset()
            for pol_hex, assets in ui.get("multi_asset", {}).items():
                pol = PycScriptHash(bytes.fromhex(pol_hex))
                ui_multi[pol] = Asset({
                    AssetName(bytes.fromhex(an_hex)): qty
                    for an_hex, qty in assets.items()
                })
            ui_val = Value(ui["lovelace"], ui_multi) if ui_multi else Value(ui["lovelace"])
            builder.add_input(UTxO(
                TransactionInput(TransactionId(bytes.fromhex(ui["tx_hash"])),
                                 ui["output_index"]),
                TransactionOutput(user_addr, ui_val),
            ))
        # Give pycardano confirmed ADA-only UTxOs to draw extra fee/change from
        # if the pending change alone can't balance the tx. add_input_address
        # would re-introduce the already-spent NFT UTxO from Blockfrost's lagged
        # view, so we offer pure-ADA candidates only (never the reserved
        # collateral, selected above). Without this the selector can deplete and
        # the chained build fails with InputUTxODepleted.
        for c in _confirmed_ada_only_utxos(context, user_addr, exclude_reserved=True):
            builder.potential_inputs.append(c)
    else:
        builder.add_input_address(user_addr)

    # Output 1: cMATRA to user
    user_multi = MultiAsset()
    user_multi[cmatra_policy] = Asset({cmatra_asset: total_cmatra})
    # estimate_min_ada() under-counts vs Conway mainnet protocol params.
    # Use a 1.5 ADA floor (covers any 1-asset, no-datum output up to ~340 bytes).
    user_min_ada = max(estimate_min_ada(num_assets=1, datum_size_bytes=0), 1_500_000)
    builder.add_output(TransactionOutput(user_addr, Value(user_min_ada, user_multi)))

    # Output 2: remaining pool back to script
    remaining_cmatra = pool_utxo["cmatra_amount"] - total_cmatra
    if remaining_cmatra > 0:
        return_multi = MultiAsset()
        return_multi[cmatra_policy] = Asset({cmatra_asset: remaining_cmatra})
        # 1.5 ADA floor for 1-asset + Void inline datum (D87980 = 3 bytes).
        return_min_ada = max(estimate_min_ada(num_assets=1, datum_size_bytes=8), 1_500_000)
        return_datum = RawPlutusData(cbor2.loads(_VOID_DATUM_CBOR))
        builder.add_output(
            TransactionOutput(script_addr, Value(return_min_ada, return_multi), datum=return_datum)
        )

    # Output 3: legacy assets to quarantine
    if legacy_assets:
        quarantine_multi = MultiAsset()
        for la in legacy_assets:
            la_policy = PycScriptHash(bytes.fromhex(la["policy_hex"]))
            la_asset = AssetName(bytes.fromhex(la["asset_hex"]))
            if la_policy not in quarantine_multi:
                quarantine_multi[la_policy] = Asset({})
            quarantine_multi[la_policy][la_asset] = la["quantity"]

        # 1.5 ADA per asset (safe floor); scales linearly with len(legacy_assets).
        # The legacy bundle can be 1-7 assets across multiple policies, so this
        # caps at ~10.5 ADA worst-case for a full 7-asset surrender.
        quarantine_min_ada = max(
            estimate_min_ada(num_assets=len(legacy_assets), datum_size_bytes=0),
            1_500_000 * max(1, len(legacy_assets)),
        )
        quarantine_addr = Address.from_primitive(QUARANTINE_ADDRESS)
        builder.add_output(
            TransactionOutput(quarantine_addr, Value(quarantine_min_ada, quarantine_multi))
        )

    # Required signers: both admin PKHs (dual-signer validator)
    builder.required_signers = [state.admin_pkh]
    # Add co-signer PKH if configured (dual-admin mode)
    cosigner_pkh_bytes = None
    if COSIGNER_URL:
        # Fetch co-signer's PKH from their health endpoint at startup
        # (cached in state.cosigner_pkh after first call)
        if not hasattr(state, "cosigner_pkh") or state.cosigner_pkh is None:
            _fetch_cosigner_pkh()
        if state.cosigner_pkh:
            builder.required_signers.append(state.cosigner_pkh)

    # Build the transaction — change goes to user.
    tx_body = builder.build(change_address=user_addr)

    # Canonicalize inputs to the ledger's sort order and re-index the SPEND
    # redeemer. Strategy selected by CANON_STRATEGY (0.18.0 re-validation):
    if CANON_STRATEGY == "patha":
        # Path A: re-emit through pycardano's builder machinery. On 0.18.0
        # _build_tx_body() serializes inputs as a hash-ordered tag-258 SET, so
        # the wallet signature fails InvalidWitnessesUTXOW — broken on 0.18.0.
        tx_body = _canonicalize_via_builder(builder, context, script)
        witness_set = builder.build_witness_set()
    else:
        # hybrid (mainnet) / pathB: rewrite the built body's inputs to the
        # ledger's canonical sort order, emitted as a bare definite-length array
        # (immune to cbor2's tag-258 re-ordering, so the wallet/admin signatures
        # cover the bytes the node hashes), and point the SPEND redeemer at the
        # script input's post-sort position. With CANON_STRATEGY=hybrid the seed
        # ex_units above made build() skip its internal evaluate, so this also
        # works on the chained (pending-tip) path; pathB without the seed fails
        # the chained build (extraRedeemers) before reaching here.
        witness_set = builder.build_witness_set()
        tx_body = _canonicalize_body_and_index_redeemer(tx_body, witness_set, tx_in)

    # Create admin 1 witness (Server A — local key) over the canonical body
    tx_hash = tx_body.hash()
    admin_signature = state.admin_sk.sign(tx_hash)
    admin_vk_witness = VerificationKeyWitness(
        VerificationKey.from_signing_key(state.admin_sk),
        admin_signature,
    )

    if witness_set.vkey_witnesses is None:
        witness_set.vkey_witnesses = []
    witness_set.vkey_witnesses.append(admin_vk_witness)

    # Get admin 2 witness from co-signer service (Server B).
    # NOTE: tx_body.hash() returns raw bytes in this pycardano version,
    # NOT a TransactionId — so don't call .payload on it.
    tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash.payload.hex()
    if COSIGNER_URL:
        cosigner_witness = _get_cosigner_witness(tx_hash_hex)
        witness_set.vkey_witnesses.append(cosigner_witness)

    # Assemble the partially-signed transaction
    tx = Transaction(tx_body, witness_set)
    tx_cbor_hex = tx.to_cbor().hex()

    # Read back output[1] (pool-change->script) for the tip-advance datum
    # guard. Reading the assembled body — not trusting the build inputs — is
    # what makes a future refactor that reorders outputs FAIL the guard rather
    # than silently poison the chained tip.
    built_pool_output = extract_pool_output(tx)

    # Collect this tx's outputs that pay back to the user — their pending change
    # (NFT/ADA leftovers) to feed the next chained chunk's user inputs. Reading
    # the canonical body (post input-sort) keeps the output indices correct.
    user_change_utxos: list[dict[str, Any]] = []
    for idx, out in enumerate(tx_body.outputs):
        if str(out.address) != user_address:
            continue
        amt = out.amount
        lovelace = amt if isinstance(amt, int) else amt.coin
        ma: dict[str, dict[str, int]] = {}
        mi = None if isinstance(amt, int) else getattr(amt, "multi_asset", None)
        if mi:
            for pol, assets in (mi.data.items() if hasattr(mi, "data") else mi.items()):
                pol_hex = pol.payload.hex() if hasattr(pol, "payload") else bytes(pol).hex()
                ma[pol_hex] = {}
                for an, qty in (assets.data.items() if hasattr(assets, "data") else assets.items()):
                    an_hex = an.payload.hex() if hasattr(an, "payload") else bytes(an).hex()
                    ma[pol_hex][an_hex] = int(qty)
        user_change_utxos.append({
            "tx_hash": tx_hash_hex, "output_index": idx,
            "lovelace": int(lovelace), "multi_asset": ma,
        })

    logger.info(
        "Built co-signed surrender tx: %d cMATRA → %s (tx: %s, pool_out=%s, "
        "user_change_outs=%d)",
        total_cmatra,
        user_address[:24] + "...",
        tx_hash_hex[:16] + "...",
        (built_pool_output or {}).get("datum_hex"),
        len(user_change_utxos),
    )

    return tx_cbor_hex, tx_hash_hex, built_pool_output, user_change_utxos


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def _prune_expired_tx_hashes() -> None:
    """Remove expired pending tx hashes (and their stashed CBOR)."""
    now = time.time()
    expired = [h for h, t in state.pending_tx_hashes.items()
               if now - t > state.TX_HASH_TTL]
    for h in expired:
        state.pending_tx_hashes.pop(h, None)
        state.pending_tx_cbor.pop(h, None)


def _merge_wallet_witnesses(
    original_tx_cbor: bytes, wallet_witness_cbor: bytes,
) -> bytes:
    """Merge the wallet's partial witness set (CIP-30 signTx partialSign
    output, typically `{0: [[pk, sig]]}`) into the admin-signed tx CBOR
    produced by /build-surrender, and return the assembled tx bytes
    ready for submission.

    The outer tx is a CBOR array of 4: `[tx_body, witness_set, is_valid,
    aux]`. We walk the original CBOR stream to capture the exact byte
    range of each element, decode-merge-re-encode ONLY the witness_set,
    and reassemble. tx_body bytes are preserved verbatim so the wallet's
    Ed25519 signature over blake2b-256(tx_body) remains valid.

    Vkey witnesses are deduplicated by public key (some wallets re-emit
    the admin keys they observed in the original witness set; we keep
    one instance each).
    """
    import io
    if not original_tx_cbor or original_tx_cbor[0] != 0x84:
        raise ValueError("Original tx CBOR header is not array(4)")

    # Walk the original tx CBOR, capturing byte ranges for each element.
    stream = io.BytesIO(original_tx_cbor)
    stream.read(1)  # consume 0x84
    body_start = stream.tell()
    cbor2.CBORDecoder(stream).decode()
    body_end = stream.tell()
    ws_start = body_end
    cbor2.CBORDecoder(stream).decode()
    ws_end = stream.tell()
    tail_bytes = original_tx_cbor[ws_end:]  # is_valid + aux + (optional set tag)
    body_bytes = original_tx_cbor[body_start:body_end]
    original_ws = cbor2.loads(original_tx_cbor[ws_start:ws_end])
    if not isinstance(original_ws, dict):
        raise ValueError("Original witness_set is not a CBOR map")

    wallet_ws = cbor2.loads(wallet_witness_cbor)
    if not isinstance(wallet_ws, dict):
        raise ValueError("Wallet partial witness set is not a CBOR map")

    # Extract vkey witnesses from both. Conway encodes the vkey list as
    # either a plain list `[[pk, sig], ...]` or a tagged set
    # `cbor2.CBORTag(258, [...])`. Treat them uniformly.
    def _unwrap_vkey_list(value):
        if isinstance(value, CBORTag):
            return list(value.value), value.tag
        return list(value) if value else [], None

    orig_vkeys, orig_tag = _unwrap_vkey_list(original_ws.get(0))
    wallet_vkeys, wallet_tag = _unwrap_vkey_list(wallet_ws.get(0))
    set_tag = orig_tag or wallet_tag  # preserve set tag if either side used it

    seen: set[bytes] = set()
    merged_vkeys: list = []
    for vw in (*orig_vkeys, *wallet_vkeys):
        if not (isinstance(vw, list) and len(vw) == 2):
            continue
        pk = bytes(vw[0])
        if pk in seen:
            continue
        seen.add(pk)
        merged_vkeys.append(vw)

    original_ws[0] = CBORTag(set_tag, merged_vkeys) if set_tag else merged_vkeys
    merged_ws_bytes = cbor2.dumps(original_ws)

    return b"\x84" + body_bytes + merged_ws_bytes + tail_bytes


@app.post("/submit-surrender", response_model=SubmitResponse)
async def submit_surrender(req: SubmitRequest):
    """Submit a surrender transaction and advance the pool tip.

    CIP-30 `signTx(cbor, partialSign=true)` returns only the wallet's
    partial witness set (typically `{0: [[pk, sig]]}`), not the full
    signed transaction. This endpoint takes that partial witness set
    plus the build-time `tx_hash`, looks up the admin-signed tx CBOR
    stashed at build time, merges the wallet's vkey witnesses into the
    existing witness set, and forwards the assembled tx to Blockfrost.

    The tx_body bytes are preserved byte-for-byte (we only rewrite the
    witness_set element of the outer CBOR array) so the wallet's
    signature over the body remains valid.

    The pool tip advances ONLY here, after Blockfrost returns a mempool-accept
    hash — never at build time. The datum guard runs against the pool output[1]
    captured at build time before the tip is allowed to chain forward.
    """
    if not state.bf:
        raise HTTPException(503, "Blockfrost client not available")

    try:
        wallet_ws_bytes = bytes.fromhex(req.tx_cbor_hex)
    except ValueError:
        raise HTTPException(400, "Invalid hex encoding")

    tx_hash_hex = req.tx_hash.lower()
    _prune_expired_tx_hashes()
    original_cbor = state.pending_tx_cbor.get(tx_hash_hex)
    if original_cbor is None or tx_hash_hex not in state.pending_tx_hashes:
        logger.warning("Rejected submit for unknown tx hash: %s", tx_hash_hex[:16])
        raise HTTPException(403, "Transaction was not built by this service")

    ctx = state.build_ctx.get(tx_hash_hex)

    try:
        merged_tx_bytes = _merge_wallet_witnesses(original_cbor, wallet_ws_bytes)
    except Exception as e:
        logger.warning("Witness merge failed for tx %s: %s", tx_hash_hex[:16], e)
        raise HTTPException(400, "Malformed witness set CBOR")

    try:
        submitted_hash = await asyncio.to_thread(state.bf.submit_tx, merged_tx_bytes)
    except Exception as e:
        # Submit rejected — tip unchanged, release the single-flight lock so
        # the next surrender can proceed off the still-confirmed tip.
        logger.error("Submit failed for tx %s: %s", tx_hash_hex[:16], e)
        if ctx and state.tip_mgr is not None:
            state.tip_mgr.release_build(ctx["build_token"])
        state.pending_tx_hashes.pop(tx_hash_hex, None)
        state.pending_tx_cbor.pop(tx_hash_hex, None)
        state.build_ctx.pop(tx_hash_hex, None)
        raise HTTPException(400, "Transaction submission failed")

    # Mempool-accept. Advance the tip, running the datum + balance guards
    # against the pool output[1] captured at build time. This is the single
    # point where the tip moves forward.
    state.pending_tx_hashes.pop(tx_hash_hex, None)
    state.pending_tx_cbor.pop(tx_hash_hex, None)
    state.build_ctx.pop(tx_hash_hex, None)
    logger.info("Submitted surrender tx: %s", submitted_hash)

    if ctx and state.tip_mgr is not None:
        try:
            state.tip_mgr.advance_on_submit(
                build_token=ctx["build_token"],
                submitted_tx_hash=submitted_hash,
                delivered_cmatra=ctx["delivered_cmatra"],
                built_pool_output=ctx["built_pool_output"],
            )
            # Promote the user's change outputs to the pending set so the next
            # chained chunk spends them explicitly (Blockfrost can't see them
            # yet). Only after mempool-accept — mirrors the pool-tip advance.
            ua = ctx.get("user_address")
            if ua and ctx.get("user_change") is not None:
                state.user_pending[ua] = ctx["user_change"]
        except PoolTipError as e:
            # The tx LANDED (the user's money moved), but the pool output we
            # built failed a guard (e.g. missing datum). Do NOT chain off it.
            # Flush the tip back to Blockfrost-confirmed state — the watchdog
            # would catch this anyway, but flush eagerly so the next build
            # re-seeds rather than chaining off an unverified output. The user
            # still gets their success.
            # advance_on_submit already released the lock in its finally; just
            # re-seed the tip from confirmed chain state.
            logger.error(
                "Tip advance guard rejected post-submit for %s: %s — flushing "
                "tip to confirmed (tx already landed)", submitted_hash[:16], e,
            )
            try:
                state.tip_mgr.seed_from_chain()
            except Exception:
                logger.exception("Tip flush after guard rejection failed")

    return SubmitResponse(tx_hash=submitted_hash)


# ---------------------------------------------------------------------------
# Dry-run evaluate (mainnet test without submission)
# ---------------------------------------------------------------------------


class EvaluateResponse(BaseModel):
    tx_hash: str
    evaluation: str
    total_cmatra_display: float
    redemption_summary: dict[str, dict[str, Any]]
    fee_lovelace: int
    outputs_summary: list[dict[str, str]]


@app.post("/evaluate-surrender", response_model=EvaluateResponse)
def evaluate_surrender(req: BuildSurrenderRequest):
    """Build a surrender tx and evaluate it against the live ledger — NO submission.

    Identical to build-surrender but instead of returning CBOR for signing,
    it runs Blockfrost evaluate_tx to verify the Plutus script executes
    correctly with real mainnet UTxOs.  Returns execution units and fee.

    Use this to validate the full pipeline before opening the window.
    """
    if not req.user_address.startswith("addr1"):
        raise HTTPException(400, "Invalid Cardano mainnet address")
    if ALLOWED_USER_ADDRESSES and req.user_address not in ALLOWED_USER_ADDRESSES:
        raise HTTPException(403, "Surrender window not yet open")
    if not state.rate_table:
        raise HTTPException(503, "Rate table not loaded")
    if not state.script_cbor_hex:
        raise HTTPException(503, "Surrender script not loaded")
    if not state.admin_sk:
        raise HTTPException(503, "Admin key not configured")
    if not SCRIPT_ADDRESS or not CMATRA_POLICY_HEX or not CMATRA_ASSET_HEX or not QUARANTINE_ADDRESS:
        raise HTTPException(503, "Service not fully configured")

    asset_lookup = _build_asset_lookup()

    total_cmatra = 0
    redemption_summary: dict[str, dict[str, Any]] = {}
    all_legacy_assets: list[dict[str, Any]] = []

    for item in req.assets:
        try:
            cmatra_amount = compute_redemption(
                state.rate_table, item.asset_key, item.quantity_base,
            )
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
        total_cmatra += cmatra_amount
        redemption_summary[item.asset_key] = {
            "quantity_base": item.quantity_base,
            "cmatra_base": cmatra_amount,
            "cmatra_display": cmatra_amount / (10 ** FLUX_DECIMALS),
        }
        legacy = _resolve_legacy_assets(
            item.asset_key, item.quantity_base, item.nft_units, asset_lookup,
        )
        all_legacy_assets.extend(legacy)

    pool_utxos = find_pool_utxos(
        state.bf, SCRIPT_ADDRESS, CMATRA_POLICY_HEX, CMATRA_ASSET_HEX,
    )
    if not pool_utxos:
        raise HTTPException(503, "No pool UTxOs available")
    selected_pool = None
    for pu in pool_utxos:
        if pu["cmatra_amount"] >= total_cmatra:
            selected_pool = pu
            break
    if selected_pool is None:
        raise HTTPException(503, "No pool UTxO with sufficient balance")

    # Build the tx (same as build-surrender). evaluate-surrender is a
    # diagnostic that never submits, so it reads the pool directly (not the
    # chained tip) and discards the built pool output.
    try:
        tx_cbor_hex, tx_hash_hex, _, _ = _build_cosigned_surrender_tx(
            user_address=req.user_address,
            total_cmatra=total_cmatra,
            legacy_assets=all_legacy_assets,
            pool_utxo=selected_pool,
        )
    except InvalidTransactionException as e:
        size_exc = _tx_too_large_http_exception(
            e, user_address=req.user_address,
            assets_count=len(all_legacy_assets),
        )
        if size_exc is not None:
            raise size_exc
        logger.exception("Evaluate: build failed for %s", req.user_address[:24])
        raise HTTPException(500, "Transaction build failed during evaluation")
    except Exception:
        logger.exception("Evaluate: build failed for %s", req.user_address[:24])
        raise HTTPException(500, "Transaction build failed during evaluation")

    # Evaluate against live ledger (does NOT submit)
    try:
        context = BlockFrostChainContext(
            project_id=state.bf.project_id,
            base_url=state.bf.base_url.rstrip("/").removesuffix("/v0"),
        )
        tx_bytes = bytes.fromhex(tx_cbor_hex)
        eval_result = context.evaluate_tx(tx_bytes)
        eval_str = str(eval_result)
        logger.info("evaluate_tx OK for %s: %s", tx_hash_hex[:16], eval_str)
    except Exception as e:
        logger.error("evaluate_tx FAILED for %s: %s", tx_hash_hex[:16], e)
        raise HTTPException(
            422,
            f"Script evaluation failed: {e}",
        )

    # Parse fee from the built tx
    tx = Transaction.from_cbor(tx_cbor_hex)
    fee = tx.transaction_body.fee or 0

    # Summarize outputs
    outputs_summary = []
    for out in tx.transaction_body.outputs:
        addr_str = str(out.address) if out.address else "unknown"
        ada = out.amount if isinstance(out.amount, int) else (out.amount.coin if out.amount else 0)
        outputs_summary.append({
            "address": addr_str[:24] + "...",
            "lovelace": str(ada),
            "has_tokens": str(bool(
                not isinstance(out.amount, int) and out.amount.multi_asset
            )),
        })

    return EvaluateResponse(
        tx_hash=tx_hash_hex,
        evaluation=eval_str,
        total_cmatra_display=total_cmatra / (10 ** FLUX_DECIMALS),
        redemption_summary=redemption_summary,
        fee_lovelace=fee,
        outputs_summary=outputs_summary,
    )


# ---------------------------------------------------------------------------
# Pool status
# ---------------------------------------------------------------------------


@app.get("/pool-status", response_model=PoolStatusResponse)
def pool_status():
    """Query current surrender pool status."""
    if not state.bf:
        raise HTTPException(503, "Blockfrost client not available")
    if not SCRIPT_ADDRESS or not CMATRA_POLICY_HEX or not CMATRA_ASSET_HEX:
        raise HTTPException(503, "Script/policy not configured")

    pool_utxos = find_pool_utxos(
        state.bf, SCRIPT_ADDRESS, CMATRA_POLICY_HEX, CMATRA_ASSET_HEX,
    )

    total_base = sum(u["cmatra_amount"] for u in pool_utxos)

    chain_state = state.tip_mgr.chain_state() if state.tip_mgr else None
    depth_cap = state.tip_mgr.depth_cap if state.tip_mgr else None

    return PoolStatusResponse(
        pool_remaining_display=total_base / (10 ** FLUX_DECIMALS),
        pool_remaining_base=total_base,
        utxo_count=len(pool_utxos),
        window_open=len(pool_utxos) > 0,  # Simple heuristic: pool funded = window open
        chainState=chain_state,
        depthCap=depth_cap,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {
        "status": "ok",
        "rate_table_loaded": state.rate_table is not None,
        "script_loaded": state.script_cbor_hex is not None,
        "admin_key_loaded": state.admin_sk is not None,
        "configured": bool(SCRIPT_ADDRESS and CMATRA_POLICY_HEX and QUARANTINE_ADDRESS),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "services.surrender_api:app",
        host="0.0.0.0",
        port=API_PORT,
        reload=True,
        log_level="info",
    )
