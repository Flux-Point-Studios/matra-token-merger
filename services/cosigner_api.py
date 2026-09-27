#!/usr/bin/env python3
"""
Co-signer service (Server B) for the dual-admin surrender pool.

Runs on a separate host from the surrender API and holds the second admin
key. It signs a transaction only after it has read the whole transaction,
services.cosign_policy has approved it as a surrender that its claimant has
signed, services.chain_check has confirmed it against this host's own view
of the chain, and services.redemption_ledger has recorded it within the
24-hour cap and each surrendered unit's pinned limit. The check does not
depend on anything the surrender API asserts: input values come from
producing bodies hashed against the inputs' transaction ids.

POST /cosign takes the full transaction carrying the claimant's witness, the
bodies of the transactions that produced its inputs (each is checked against
the input's transaction id), and the PlutusV3 language views that close its
script_data_hash. Requests carry a shared secret in X-API-Secret, compared in
constant time.

Environment (all required unless noted):
  COSIGNER_SKEY_PATH          admin_2 signing key file
  COSIGNER_API_SECRET         shared with the surrender API, >= 32 characters
  COSIGNER_PRIMARY_ADMIN_PKH  admin_1 key hash (the other required signer)
  COSIGNER_LEDGER_PATH        SQLite file recording every signed surrender, created
                              once with python -m services.redemption_ledger init
  MAX_CMATRA_PER_DAY          base units signed per rolling 24 hours
  BLOCKFROST_PROJECT_ID       this host's Blockfrost project on NETWORK
  plus the policy variables read by services.cosign_policy.load_config
  (NETWORK, SURRENDER_SCRIPT_ADDRESS, QUARANTINE_ADDRESS, CMATRA_POLICY_HEX,
  CMATRA_ASSET_HEX, SURRENDER_DEADLINE_POSIX_MS, MAX_CMATRA_PER_TX, and
  optionally RATE_TABLE_PATH / REDEMPTION_PIN_PATH).

Usage:
  uvicorn services.cosigner_api:app --host <lan-ip> --port 8421
"""

from __future__ import annotations

import hmac
import logging
import os
import sqlite3
import time
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints
from pycardano import PaymentSigningKey, PaymentVerificationKey

from services.chain_check import ChainUnavailable, confirm_on_chain
from services.cosign_policy import (
    CosignConfig,
    CosignRejected,
    evaluate_surrender,
    load_config,
    require_claimant_signature,
    slot_at,
)
from services.redemption_ledger import RedemptionLedger
from tools.api_clients import BlockfrostClient
from tools.config import BLOCKFROST_BASE_URLS

logger = logging.getLogger("cosigner_api")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

MIN_SECRET_LENGTH = 32


class CosignerState:
    sk: PaymentSigningKey | None = None
    vkey_hex: str = ""
    pkh_hex: str = ""
    secret: bytes = b""
    cfg: CosignConfig | None = None
    ledger: RedemptionLedger | None = None
    chain: BlockfrostClient | None = None


state = CosignerState()

# Whole bytes only: bytes.fromhex refuses an odd digit count.
_HEX_BYTES = r"^(?:[0-9a-fA-F]{2})+$"
HexString = Annotated[str, StringConstraints(max_length=65_536, pattern=_HEX_BYTES)]


class CosignRequest(BaseModel):
    tx_cbor_hex: HexString = Field(..., min_length=8)
    parent_bodies_hex: list[HexString] = Field(..., min_length=1, max_length=256)
    language_views_hex: str = Field(..., min_length=2, max_length=16_384, pattern=_HEX_BYTES)


class CosignResponse(BaseModel):
    vkey_hex: str
    signature_hex: str
    pkh_hex: str
    tx_hash: str
    payout: int


app = FastAPI(title="cMATRA Co-Signer", version="2.0.0")


def _need(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} must be set")
    return value


@app.on_event("startup")
def startup() -> None:
    """Load the key, the policy and the ledger; refuse to start without any of them."""
    secret = _need("COSIGNER_API_SECRET")
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"COSIGNER_API_SECRET must be at least {MIN_SECRET_LENGTH} characters")
    sk = PaymentSigningKey.load(_need("COSIGNER_SKEY_PATH"))
    vk = PaymentVerificationKey.from_signing_key(sk)
    primary = bytes.fromhex(_need("COSIGNER_PRIMARY_ADMIN_PKH"))
    ledger = RedemptionLedger(_need("COSIGNER_LEDGER_PATH"), int(_need("MAX_CMATRA_PER_DAY")))
    cfg = load_config(os.environ, [primary, vk.hash().payload])
    chain = BlockfrostClient(_need("BLOCKFROST_PROJECT_ID"), BLOCKFROST_BASE_URLS[cfg.network])

    state.sk = sk
    state.vkey_hex = vk.payload.hex()
    state.pkh_hex = vk.hash().payload.hex()
    state.secret = secret.encode()
    state.cfg = cfg
    state.ledger = ledger
    state.chain = chain
    logger.info(
        "Co-signer ready: PKH=%s network=%s per-tx cap=%d per-day cap=%d pinned units=%d",
        state.pkh_hex, cfg.network, cfg.max_payout_per_tx, ledger.max_per_day,
        len(cfg.redemption_limits),
    )


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    """Every route but /health needs the shared secret."""
    if request.url.path == "/health":
        return await call_next(request)
    provided = request.headers.get("x-api-secret", "").encode()
    if not state.secret or not hmac.compare_digest(provided, state.secret):
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)


@app.post("/cosign", response_model=CosignResponse)
def cosign(req: CosignRequest) -> CosignResponse:
    """Verify the full transaction and its claimant's signature, confirm it
    against the chain, record it within the cap and the pinned unit limits,
    then sign its body hash."""
    now = time.time()
    tx = bytes.fromhex(req.tx_cbor_hex)
    try:
        approval = evaluate_surrender(
            tx,
            bytes.fromhex(req.language_views_hex),
            [bytes.fromhex(body) for body in req.parent_bodies_hex],
            slot_at(now, state.cfg.network),
            state.cfg,
        )
        require_claimant_signature(tx, approval)
        confirm_on_chain(approval, state.cfg, state.chain)
        state.ledger.record(approval, now, state.cfg.redemption_limits)
    except CosignRejected as exc:
        logger.warning("Refused to co-sign: %s", exc)
        raise HTTPException(422, {"code": exc.code, "detail": exc.detail})
    except ChainUnavailable as exc:
        logger.error("Refused to co-sign: chain view unavailable: %s", exc)
        raise HTTPException(
            503, {"code": "chain_unavailable", "detail": "the chain view cannot confirm the transaction"},
        )
    except sqlite3.Error as exc:
        logger.error("Refused to co-sign: ledger unavailable: %s", exc)
        raise HTTPException(
            503, {"code": "ledger_unavailable", "detail": "the signing ledger cannot be written"},
        )

    signature = state.sk.sign(approval.tx_hash)
    logger.info(
        "Co-signed %s: payout %d to %s", approval.tx_hash.hex(), approval.payout,
        approval.claimant.hex(),
    )
    return CosignResponse(
        vkey_hex=state.vkey_hex,
        signature_hex=signature.hex(),
        pkh_hex=state.pkh_hex,
        tx_hash=approval.tx_hash.hex(),
        payout=approval.payout,
    )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "key_loaded": state.sk is not None,
        "pkh": state.pkh_hex[:16] + "..." if state.pkh_hex else None,
    }
