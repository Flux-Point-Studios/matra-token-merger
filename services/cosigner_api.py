#!/usr/bin/env python3
"""
Co-Signer Microservice (Server B) for Dual-Admin Surrender Pool.

This is a lightweight FastAPI service that runs on a SEPARATE server from the
main surrender API.  It holds the second admin signing key and provides a
single endpoint: sign a transaction hash and return the VK witness.

The main surrender API (Server A) builds the transaction, signs with Key A,
then calls this service to get Key B's signature.  Both signatures are merged
into the transaction before returning it to the user.

Security:
  - Protected by a shared API secret (COSIGNER_API_SECRET)
  - Only signs transaction hashes — never sees or builds full transactions
  - Should run on separate infrastructure from Server A
  - If Server A is compromised, attacker still can't drain the pool
    (they'd need Server B's key too, and the validator requires both)

Usage:
  # Set environment variables
  export COSIGNER_SKEY_PATH=/secure/path/to/admin_2.skey
  export COSIGNER_API_SECRET=<shared-secret-with-server-a>

  # Run
  py -3.12 -m services.cosigner_api
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pycardano import (
    PaymentSigningKey,
    PaymentVerificationKey,
    VerificationKey,
    VerificationKeyWitness,
)

logger = logging.getLogger("cosigner_api")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COSIGNER_SKEY_PATH: str = os.environ.get("COSIGNER_SKEY_PATH", "")
COSIGNER_API_SECRET: str = os.environ.get("COSIGNER_API_SECRET", "")
COSIGNER_PORT: int = int(os.environ.get("COSIGNER_API_PORT", "8421"))

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class CosignerState:
    sk: PaymentSigningKey | None = None
    vk: PaymentVerificationKey | None = None
    pkh_hex: str = ""


state = CosignerState()

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CosignRequest(BaseModel):
    """Request to co-sign a transaction hash."""
    tx_hash_hex: str = Field(
        ..., description="Transaction body hash (hex, 64 chars)",
        min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]+$",
    )


class CosignResponse(BaseModel):
    """Response with the VK witness."""
    vkey_hex: str
    signature_hex: str
    pkh_hex: str


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="cMATRA Co-Signer", version="1.0.0")

# No CORS needed — this is server-to-server only
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # no browser access
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "X-API-Secret"],
)


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    """Reject requests without valid API secret (except health)."""
    if request.url.path == "/health":
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)

    if not COSIGNER_API_SECRET:
        return JSONResponse(status_code=503, content={"detail": "Not configured"})

    provided = request.headers.get("x-api-secret", "")
    if not provided or provided != COSIGNER_API_SECRET:
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    return await call_next(request)


@app.on_event("startup")
def startup():
    if COSIGNER_SKEY_PATH and Path(COSIGNER_SKEY_PATH).exists():
        state.sk = PaymentSigningKey.load(COSIGNER_SKEY_PATH)
        state.vk = PaymentVerificationKey.from_signing_key(state.sk)
        state.pkh_hex = state.vk.hash().payload.hex()
        logger.info("Co-signer key loaded: PKH=%s", state.pkh_hex)
    else:
        logger.error("COSIGNER_SKEY_PATH not set or file missing: %s", COSIGNER_SKEY_PATH)


@app.post("/cosign", response_model=CosignResponse)
def cosign(req: CosignRequest):
    """Sign a transaction hash with the co-signer key.

    Returns the verification key and signature so Server A can construct
    a VerificationKeyWitness and merge it into the transaction.
    """
    if not state.sk:
        raise HTTPException(503, "Co-signer key not loaded")

    try:
        tx_hash_bytes = bytes.fromhex(req.tx_hash_hex)
        signature = state.sk.sign(tx_hash_bytes)
        vk = VerificationKey.from_signing_key(state.sk)

        logger.info("Co-signed tx: %s", req.tx_hash_hex[:16])

        return CosignResponse(
            vkey_hex=vk.payload.hex(),
            signature_hex=signature.hex(),
            pkh_hex=state.pkh_hex,
        )
    except Exception as e:
        logger.error("Co-sign failed: %s", e)
        raise HTTPException(500, "Co-signing failed")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "key_loaded": state.sk is not None,
        "pkh": state.pkh_hex[:16] + "..." if state.pkh_hex else None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "services.cosigner_api:app",
        host="0.0.0.0",
        port=COSIGNER_PORT,
        log_level="info",
    )
