"""Tests for the surrender_api tx-too-large error mapping.

Covers _tx_too_large_http_exception: pattern-matches the pycardano
InvalidTransactionException message and returns a 413 with a structured
detail body the frontend can parse, OR returns None so the caller
re-raises and a 500 surfaces for non-size invalidity.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from pycardano.exception import InvalidTransactionException

# The surrender_api module needs NETWORK + a project root on sys.path; load
# the same way the service does so the import chain (tools/, env defaults,
# etc.) resolves.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("NETWORK", "mainnet")

from services.surrender_api import _tx_too_large_http_exception  # noqa: E402


def test_size_exceeds_limit_returns_413():
    exc = InvalidTransactionException(
        "Transaction size (25287) exceeds the max limit (16384). "
        "Please try reducing the number of inputs or outputs."
    )

    http_exc = _tx_too_large_http_exception(
        exc, user_address="addr1q9jzvg6qqefx4c8eqse" + "x" * 80,
        assets_count=7,
    )

    assert http_exc is not None
    assert http_exc.status_code == 413
    detail = http_exc.detail
    assert detail["code"] == "TX_TOO_LARGE"
    assert detail["tx_size_bytes"] == 25287
    assert detail["max_tx_size_bytes"] == 16384
    assert detail["suggested_batch_size"] == 4
    assert "fewer assets" in detail["message"]


def test_size_at_boundary_returns_413():
    # 16385 over 16384 still trips the regex
    exc = InvalidTransactionException(
        "Transaction size (16385) exceeds the max limit (16384)."
    )
    http_exc = _tx_too_large_http_exception(
        exc, user_address="addr1qfoo", assets_count=1,
    )
    assert http_exc is not None
    assert http_exc.status_code == 413
    assert http_exc.detail["tx_size_bytes"] == 16385


def test_non_size_invalidity_returns_none():
    # Other InvalidTransactionException causes (insufficient ADA, datum
    # mismatch, missing collateral) must not be mapped to 413 — the caller
    # re-raises them so they surface as a 500 we can investigate.
    cases = [
        "UTxO Balance insufficient: input value 2000000 < output value 5000000",
        "Datum mismatch — provided datum does not match expected hash",
        "Missing collateral input for Plutus script",
        "Some unrelated invalidity",
    ]
    for msg in cases:
        exc = InvalidTransactionException(msg)
        http_exc = _tx_too_large_http_exception(
            exc, user_address="addr1qfoo", assets_count=3,
        )
        assert http_exc is None, f"Should not 413-map: {msg!r}"
