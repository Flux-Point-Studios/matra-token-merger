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

import hashlib
import json
import logging
import os
import sys
import time
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
from pycardano.hash import ScriptHash as PycScriptHash, TransactionId

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import estimate_min_ada
from tools.config import FLUX_DECIMALS, ALL_MERGE_ASSETS
from tools.process_surrender import (
    compute_redemption,
    find_pool_utxos,
    load_rate_table,
    load_script_from_blueprint,
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

# CORS origins (flux1 site)
CORS_ORIGINS: list[str] = json.loads(
    os.environ.get(
        "CORS_ORIGINS",
        '["http://localhost:3000","https://fluxpointstudios.com","https://www.fluxpointstudios.com"]',
    )
)

API_PORT: int = int(os.environ.get("SURRENDER_API_PORT", "8420"))

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
    TX_HASH_TTL: float = 600.0  # 10 minutes

    # Pool UTxO reservation: prevents concurrent builds from targeting the
    # same pool UTxO.  Maps "txhash#idx" -> expiry timestamp.
    reserved_pool_utxos: dict[str, float] = {}
    POOL_RESERVE_TTL: float = 120.0  # 2 minutes (enough for user to sign)


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
    tx_cbor_hex: str = Field(
        ..., description="Fully-signed transaction CBOR hex",
        min_length=64, max_length=32_768, pattern=r"^[0-9a-fA-F]+$",
    )


class SubmitResponse(BaseModel):
    tx_hash: str


class PoolStatusResponse(BaseModel):
    pool_remaining_display: float
    pool_remaining_base: int
    utxo_count: int
    window_open: bool


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
def startup():
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


@app.post("/build-surrender", response_model=BuildSurrenderResponse)
def build_surrender(req: BuildSurrenderRequest):
    """Build a partially-signed surrender transaction.

    The server builds the full transaction and signs with the admin key.
    The returned CBOR hex needs the user's wallet signature added before
    submission.
    """
    # Validate user address format
    if not req.user_address.startswith("addr1"):
        raise HTTPException(400, "Invalid Cardano mainnet address")

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

    # Find pool UTxO with sufficient balance (skip reserved ones)
    pool_utxos = find_pool_utxos(
        state.bf, SCRIPT_ADDRESS, CMATRA_POLICY_HEX, CMATRA_ASSET_HEX,
    )
    if not pool_utxos:
        raise HTTPException(503, "No pool UTxOs available")

    _prune_expired_reservations()
    selected_pool = None
    for pu in pool_utxos:
        utxo_key = f"{pu['tx_hash']}#{pu['output_index']}"
        if utxo_key in state.reserved_pool_utxos:
            continue  # skip — another build is in-flight for this UTxO
        if pu["cmatra_amount"] >= total_cmatra:
            selected_pool = pu
            break

    if selected_pool is None:
        raise HTTPException(
            503,
            "No available pool UTxO with sufficient balance. "
            "Another surrender may be in progress — please retry shortly.",
        )

    # Reserve this pool UTxO so concurrent builds don't target it
    pool_utxo_key = f"{selected_pool['tx_hash']}#{selected_pool['output_index']}"
    state.reserved_pool_utxos[pool_utxo_key] = time.time()

    # Build the transaction
    try:
        tx_cbor_hex, tx_hash = _build_cosigned_surrender_tx(
            user_address=req.user_address,
            total_cmatra=total_cmatra,
            legacy_assets=all_legacy_assets,
            pool_utxo=selected_pool,
        )
    except Exception as e:
        # Release reservation on build failure
        state.reserved_pool_utxos.pop(pool_utxo_key, None)
        logger.exception("Failed to build surrender tx for %s", req.user_address[:24])
        raise HTTPException(500, "Transaction build failed. Please try again.")

    # Mandatory preflight: evaluate_tx against live ledger before returning
    # to user.  Catches script errors before any collateral is at risk.
    try:
        preflight_ctx = BlockFrostChainContext(
            project_id=state.bf.project_id,
            base_url=state.bf.base_url,
        )
        preflight_ctx.evaluate_tx(bytes.fromhex(tx_cbor_hex))
        logger.info("Preflight OK for tx %s", tx_hash[:16])
    except Exception as e:
        state.reserved_pool_utxos.pop(pool_utxo_key, None)
        logger.error("Preflight FAILED for tx %s: %s", tx_hash[:16], e)
        raise HTTPException(422, "Transaction preflight failed. Please retry.")

    # Register this tx hash so submit-surrender will accept it
    _prune_expired_tx_hashes()
    state.pending_tx_hashes[tx_hash] = time.time()

    return BuildSurrenderResponse(
        tx_cbor_hex=tx_cbor_hex,
        tx_hash=tx_hash,
        redemption_summary=redemption_summary,
        total_cmatra_display=total_cmatra / (10 ** FLUX_DECIMALS),
        pool_utxo_used=f"{selected_pool['tx_hash']}#{selected_pool['output_index']}",
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


def _build_cosigned_surrender_tx(
    user_address: str,
    total_cmatra: int,
    legacy_assets: list[dict[str, Any]],
    pool_utxo: dict[str, Any],
) -> tuple[str, str]:
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

    Returns (tx_cbor_hex, tx_hash_hex).
    """
    context = BlockFrostChainContext(
        project_id=state.bf.project_id,
        base_url=state.bf.base_url,
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

    builder = TransactionBuilder(context)

    # Script input: pool UTxO
    builder.add_script_input(utxo, script=script, redeemer=redeemer)

    # User's address for UTxO selection (legacy assets + fee contribution)
    user_addr = Address.from_primitive(user_address)
    builder.add_input_address(user_addr)

    # Admin's address for collateral + fee contribution
    builder.add_input_address(state.admin_addr)

    # Use dedicated collateral UTxO if configured (limits max collateral loss)
    if COLLATERAL_UTXO and "#" in COLLATERAL_UTXO:
        col_hash, col_idx = COLLATERAL_UTXO.rsplit("#", 1)
        col_input = TransactionInput(
            TransactionId(bytes.fromhex(col_hash)), int(col_idx),
        )
        col_utxo = UTxO(
            col_input,
            TransactionOutput(state.admin_addr, Value(5_000_000)),  # 5 ADA
        )
        builder.collateral = [col_utxo]

    # Output 1: cMATRA to user
    user_multi = MultiAsset()
    user_multi[cmatra_policy] = Asset({cmatra_asset: total_cmatra})
    user_min_ada = estimate_min_ada(num_assets=1, datum_size_bytes=0)
    builder.add_output(TransactionOutput(user_addr, Value(user_min_ada, user_multi)))

    # Output 2: remaining pool back to script
    remaining_cmatra = pool_utxo["cmatra_amount"] - total_cmatra
    if remaining_cmatra > 0:
        return_multi = MultiAsset()
        return_multi[cmatra_policy] = Asset({cmatra_asset: remaining_cmatra})
        return_min_ada = estimate_min_ada(num_assets=1, datum_size_bytes=8)
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

        quarantine_min_ada = estimate_min_ada(
            num_assets=len(legacy_assets), datum_size_bytes=0,
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

    # Build the transaction — change goes to user
    tx_body = builder.build(change_address=user_addr)

    # Create admin 1 witness (Server A — local key)
    tx_hash = tx_body.hash()
    admin_signature = state.admin_sk.sign(tx_hash)
    admin_vk_witness = VerificationKeyWitness(
        VerificationKey.from_signing_key(state.admin_sk),
        admin_signature,
    )

    # Build witness set with admin 1 signature + script + redeemer
    witness_set = builder.build_witness_set()
    if witness_set.vkey_witnesses is None:
        witness_set.vkey_witnesses = []
    witness_set.vkey_witnesses.append(admin_vk_witness)

    # Get admin 2 witness from co-signer service (Server B)
    if COSIGNER_URL:
        cosigner_witness = _get_cosigner_witness(tx_hash.payload.hex())
        witness_set.vkey_witnesses.append(cosigner_witness)

    # Assemble the partially-signed transaction
    tx = Transaction(tx_body, witness_set)
    tx_cbor_hex = tx.to_cbor().hex()
    tx_hash_hex = tx_hash.payload.hex()

    logger.info(
        "Built co-signed surrender tx: %d cMATRA → %s (tx: %s)",
        total_cmatra,
        user_address[:24] + "...",
        tx_hash_hex[:16] + "...",
    )

    return tx_cbor_hex, tx_hash_hex


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def _prune_expired_tx_hashes() -> None:
    """Remove expired pending tx hashes."""
    now = time.time()
    expired = [h for h, t in state.pending_tx_hashes.items()
               if now - t > state.TX_HASH_TTL]
    for h in expired:
        del state.pending_tx_hashes[h]


def _prune_expired_reservations() -> None:
    """Remove expired pool UTxO reservations."""
    now = time.time()
    expired = [k for k, t in state.reserved_pool_utxos.items()
               if now - t > state.POOL_RESERVE_TTL]
    for k in expired:
        del state.reserved_pool_utxos[k]


@app.post("/submit-surrender", response_model=SubmitResponse)
def submit_surrender(req: SubmitRequest):
    """Submit a fully-signed surrender transaction.

    Only accepts transactions that were previously built by this service
    (tx hash must be in the pending registry).
    """
    if not state.bf:
        raise HTTPException(503, "Blockfrost client not available")

    # Validate hex format and size (max 16 KB for a Cardano tx)
    if len(req.tx_cbor_hex) > 32_768:
        raise HTTPException(400, "Transaction CBOR too large")

    try:
        tx_bytes = bytes.fromhex(req.tx_cbor_hex)
    except ValueError:
        raise HTTPException(400, "Invalid hex encoding")

    # Compute the tx hash from the submitted CBOR and check the registry
    tx = Transaction.from_cbor(req.tx_cbor_hex)
    tx_hash_hex = tx.transaction_body.hash().payload.hex()

    _prune_expired_tx_hashes()
    if tx_hash_hex not in state.pending_tx_hashes:
        logger.warning("Rejected submit for unknown tx hash: %s", tx_hash_hex[:16])
        raise HTTPException(403, "Transaction was not built by this service")

    try:
        submitted_hash = state.bf.submit_tx(tx_bytes)
        # Remove from pending + release UTxO reservation after success
        state.pending_tx_hashes.pop(tx_hash_hex, None)
        _prune_expired_reservations()
        logger.info("Submitted surrender tx: %s", submitted_hash)
        return SubmitResponse(tx_hash=submitted_hash)
    except Exception as e:
        logger.error("Submit failed for tx %s: %s", tx_hash_hex[:16], e)
        raise HTTPException(400, "Transaction submission failed")


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

    # Build the tx (same as build-surrender)
    try:
        tx_cbor_hex, tx_hash_hex = _build_cosigned_surrender_tx(
            user_address=req.user_address,
            total_cmatra=total_cmatra,
            legacy_assets=all_legacy_assets,
            pool_utxo=selected_pool,
        )
    except Exception as e:
        logger.exception("Evaluate: build failed for %s", req.user_address[:24])
        raise HTTPException(500, "Transaction build failed during evaluation")

    # Evaluate against live ledger (does NOT submit)
    try:
        context = BlockFrostChainContext(
            project_id=state.bf.project_id,
            base_url=state.bf.base_url,
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

    return PoolStatusResponse(
        pool_remaining_display=total_base / (10 ** FLUX_DECIMALS),
        pool_remaining_base=total_base,
        utxo_count=len(pool_utxos),
        window_open=len(pool_utxos) > 0,  # Simple heuristic: pool funded = window open
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
