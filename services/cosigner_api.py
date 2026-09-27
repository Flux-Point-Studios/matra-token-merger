#!/usr/bin/env python3
"""
Co-signer service (Server B) for the dual-admin surrender pool.

Runs on a separate host from the surrender API and holds the second admin
key. It signs a transaction only after it has read the whole transaction and
services.cosign_policy has approved it as a surrender, and only while the
payouts it signed over the last 24 hours stay under a cap persisted on disk.
The check does not depend on anything the surrender API asserts: input values
come from producing bodies hashed against the inputs' transaction ids.

POST /cosign takes the full transaction, the bodies of the transactions that
produced its inputs (each is checked against the input's transaction id), and
the PlutusV3 language views that close its script_data_hash. Requests carry a
shared secret in X-API-Secret, compared in constant time.

Environment (all required unless noted):
  COSIGNER_SKEY_PATH          admin_2 signing key file
  COSIGNER_API_SECRET         shared with the surrender API, >= 32 characters
  COSIGNER_PRIMARY_ADMIN_PKH  admin_1 key hash (the other required signer)
  COSIGNER_LEDGER_PATH        SQLite file recording every signed payout
  MAX_CMATRA_PER_DAY          base units signed per rolling 24 hours
  plus the policy variables read by services.cosign_policy.load_config
  (NETWORK, SURRENDER_SCRIPT_ADDRESS, QUARANTINE_ADDRESS, CMATRA_POLICY_HEX,
  CMATRA_ASSET_HEX, SURRENDER_DEADLINE_POSIX_MS, MAX_CMATRA_PER_TX, and
  optionally RATE_TABLE_PATH / REDEEMABLE_NFTS_PATH).

Usage:
  uvicorn services.cosigner_api:app --host <lan-ip> --port 8421
"""

from __future__ import annotations

import hmac
import logging
import os
import sqlite3
import time
from contextlib import closing
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints
from pycardano import PaymentSigningKey, PaymentVerificationKey

from services.cosign_policy import (
    Approval,
    CosignConfig,
    CosignRejected,
    evaluate_surrender,
    load_config,
    slot_at,
)

logger = logging.getLogger("cosigner_api")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

DAY_S = 86_400
MIN_SECRET_LENGTH = 32


class CosignerState:
    sk: PaymentSigningKey | None = None
    vkey_hex: str = ""
    pkh_hex: str = ""
    secret: bytes = b""
    cfg: CosignConfig | None = None
    ledger_path: str = ""
    max_per_day: int = 0


state = CosignerState()

HexString = Annotated[str, StringConstraints(max_length=65_536, pattern=r"^[0-9a-fA-F]+$")]


class CosignRequest(BaseModel):
    tx_cbor_hex: HexString = Field(..., min_length=8)
    parent_bodies_hex: list[HexString] = Field(..., min_length=1, max_length=256)
    language_views_hex: str = Field(
        ..., min_length=2, max_length=16_384, pattern=r"^[0-9a-fA-F]+$",
    )


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
    ledger_path = _need("COSIGNER_LEDGER_PATH")
    max_per_day = int(_need("MAX_CMATRA_PER_DAY"))
    cfg = load_config(os.environ, [primary, vk.hash().payload])
    with closing(sqlite3.connect(ledger_path)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cosigned ("
            " tx_hash BLOB PRIMARY KEY, payout INTEGER NOT NULL, signed_at REAL NOT NULL)"
        )

    state.sk = sk
    state.vkey_hex = vk.payload.hex()
    state.pkh_hex = vk.hash().payload.hex()
    state.secret = secret.encode()
    state.cfg = cfg
    state.ledger_path = ledger_path
    state.max_per_day = max_per_day
    logger.info(
        "Co-signer ready: PKH=%s network=%s per-tx cap=%d per-day cap=%d",
        state.pkh_hex, cfg.network, cfg.max_payout_per_tx, max_per_day,
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


def _record_within_daily_cap(approval: Approval, now: float) -> None:
    """Record the payout, refusing it if the rolling 24-hour total would pass
    the cap. A transaction already recorded is not counted again."""
    with closing(sqlite3.connect(state.ledger_path, timeout=10, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            known = conn.execute(
                "SELECT 1 FROM cosigned WHERE tx_hash = ?", (approval.tx_hash,)
            ).fetchone()
            if known is None:
                (signed,) = conn.execute(
                    "SELECT COALESCE(SUM(payout), 0) FROM cosigned WHERE signed_at > ?",
                    (now - DAY_S,),
                ).fetchone()
                if signed + approval.payout > state.max_per_day:
                    raise CosignRejected(
                        "daily_cap",
                        f"{signed} signed in 24h + {approval.payout} exceeds {state.max_per_day}",
                    )
                conn.execute(
                    "INSERT INTO cosigned (tx_hash, payout, signed_at) VALUES (?, ?, ?)",
                    (approval.tx_hash, approval.payout, now),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


@app.post("/cosign", response_model=CosignResponse)
def cosign(req: CosignRequest) -> CosignResponse:
    """Verify the full transaction, record it against the daily cap, then sign
    its body hash."""
    now = time.time()
    try:
        approval = evaluate_surrender(
            bytes.fromhex(req.tx_cbor_hex),
            bytes.fromhex(req.language_views_hex),
            [bytes.fromhex(body) for body in req.parent_bodies_hex],
            slot_at(now, state.cfg.network),
            state.cfg,
        )
        _record_within_daily_cap(approval, now)
    except CosignRejected as exc:
        logger.warning("Refused to co-sign: %s", exc)
        raise HTTPException(422, {"code": exc.code, "detail": exc.detail})

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
