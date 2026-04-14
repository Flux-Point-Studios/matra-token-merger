#!/usr/bin/env python3
"""
Mainnet Canary Test — validate the surrender pipeline on mainnet before public launch.

Three escalating test levels:

  Level 1: evaluate_tx dry-run  (zero risk — no submission)
  Level 2: admin self-surrender (real tx — tiny amount from admin wallet)
  Level 3: full portal flow     (manual — use the web portal yourself)

This script automates Levels 1 and 2 against the running surrender API service.

Usage:
  # Level 1 — dry-run only (default)
  py -3.12 scripts/mainnet_canary.py --address addr1... --asset AGENT --qty 1

  # Level 2 — real submission (adds --submit flag)
  py -3.12 scripts/mainnet_canary.py --address addr1... --asset AGENT --qty 1 --submit

  # Check pool status first
  py -3.12 scripts/mainnet_canary.py --pool-status

Prerequisites:
  - Surrender API service running (py -3.12 -m services.surrender_api)
  - SURRENDER_API_SECRET env var set (same value as the service)
  - For Level 2: a CIP-30 wallet is NOT used — the script signs with the
    admin key directly.  This tests the server pipeline but not the wallet
    co-sign flow.  Use the portal (Level 3) for the full wallet test.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / "env.local", override=True)


API_BASE = os.environ.get("SURRENDER_API_URL", "http://localhost:8420")
API_SECRET = os.environ.get("SURRENDER_API_SECRET", "")

HEADERS = {
    "Content-Type": "application/json",
    "X-API-Secret": API_SECRET,
}


def check_health():
    """Verify the service is running and configured."""
    print("=" * 60)
    print("HEALTH CHECK")
    print("=" * 60)
    try:
        r = requests.get(f"{API_BASE}/health", timeout=5)
        data = r.json()
        print(json.dumps(data, indent=2))
        if not data.get("rate_table_loaded"):
            print("\n  WARNING: Rate table not loaded")
            return False
        if not data.get("script_loaded"):
            print("\n  WARNING: Script not loaded")
            return False
        if not data.get("admin_key_loaded"):
            print("\n  WARNING: Admin key not loaded")
            return False
        if not data.get("configured"):
            print("\n  WARNING: Service not fully configured (missing env vars)")
            return False
        print("\n  All systems GO")
        return True
    except Exception as e:
        print(f"\n  FAILED to reach service at {API_BASE}: {e}")
        return False


def check_pool_status():
    """Query pool status."""
    print("\n" + "=" * 60)
    print("POOL STATUS")
    print("=" * 60)
    r = requests.get(f"{API_BASE}/pool-status", timeout=10)
    if r.status_code != 200:
        print(f"  FAILED: {r.status_code} — {r.text}")
        return False
    data = r.json()
    print(json.dumps(data, indent=2))
    if data.get("utxo_count", 0) == 0:
        print("\n  WARNING: No pool UTxOs found — is the pool funded?")
        return False
    print(f"\n  Pool has {data['utxo_count']} UTxO(s), "
          f"{data['pool_remaining_display']:,.2f} cMATRA remaining")
    return True


def run_evaluate(address: str, asset_key: str, qty: int):
    """Level 1: Build + evaluate_tx (no submission)."""
    print("\n" + "=" * 60)
    print("LEVEL 1: EVALUATE (dry-run, zero risk)")
    print("=" * 60)
    print(f"  Address:  {address[:32]}...")
    print(f"  Asset:    {asset_key}")
    print(f"  Quantity: {qty}")

    payload = {
        "user_address": address,
        "assets": [{"asset_key": asset_key, "quantity_base": qty, "nft_units": None}],
    }
    r = requests.post(
        f"{API_BASE}/evaluate-surrender",
        json=payload,
        headers=HEADERS,
        timeout=60,
    )

    if r.status_code != 200:
        print(f"\n  FAILED: {r.status_code}")
        try:
            print(f"  Detail: {r.json().get('detail', r.text)}")
        except Exception:
            print(f"  Response: {r.text[:500]}")
        return False

    data = r.json()
    print(f"\n  TX Hash:    {data['tx_hash']}")
    print(f"  cMATRA:     {data['total_cmatra_display']:.6f}")
    print(f"  Fee:        {data['fee_lovelace']} lovelace ({data['fee_lovelace'] / 1_000_000:.4f} ADA)")
    print(f"  Evaluation: {data['evaluation']}")
    print(f"\n  Outputs:")
    for i, out in enumerate(data.get("outputs_summary", [])):
        print(f"    [{i}] {out['address']}  {out['lovelace']} lovelace  tokens={out['has_tokens']}")
    print(f"\n  Redemption:")
    for key, info in data.get("redemption_summary", {}).items():
        print(f"    {key}: {info['quantity_base']} → {info['cmatra_display']:.6f} cMATRA")

    print("\n  LEVEL 1 PASSED — Script evaluation succeeded on mainnet ledger")
    return True


def run_build_and_submit(address: str, asset_key: str, qty: int):
    """Level 2: Build + admin submit (real transaction)."""
    print("\n" + "=" * 60)
    print("LEVEL 2: ADMIN CANARY SURRENDER (real mainnet tx)")
    print("=" * 60)
    print(f"  Address:  {address[:32]}...")
    print(f"  Asset:    {asset_key}")
    print(f"  Quantity: {qty}")

    # Build
    print("\n  [1/3] Building surrender tx...")
    payload = {
        "user_address": address,
        "assets": [{"asset_key": asset_key, "quantity_base": qty, "nft_units": None}],
    }
    r = requests.post(
        f"{API_BASE}/build-surrender",
        json=payload,
        headers=HEADERS,
        timeout=60,
    )
    if r.status_code != 200:
        print(f"  BUILD FAILED: {r.status_code}")
        try:
            print(f"  Detail: {r.json().get('detail', r.text)}")
        except Exception:
            print(f"  Response: {r.text[:500]}")
        return False

    build_data = r.json()
    tx_cbor = build_data["tx_cbor_hex"]
    tx_hash = build_data["tx_hash"]
    print(f"  TX Hash:  {tx_hash}")
    print(f"  cMATRA:   {build_data['total_cmatra_display']:.6f}")
    print(f"  Pool UTxO: {build_data['pool_utxo_used']}")
    print(f"  CBOR size: {len(tx_cbor) // 2} bytes")

    # NOTE: For a true Level 2 test, the admin IS both signers.
    # The build endpoint already added the admin VK witness.
    # In a real portal flow, the user's wallet would add their witness via signTx.
    # For this canary (admin self-test), we sign with the admin key again
    # since the admin IS the user.  We load the skey and add a second
    # witness for the "user" side (which is the same key).
    print("\n  [2/3] Adding user-side signature (admin self-test)...")
    from pycardano import (
        PaymentSigningKey, Transaction, VerificationKey, VerificationKeyWitness,
    )

    admin_skey_path = os.environ.get("ADMIN_SKEY_PATH", "")
    if not admin_skey_path or not Path(admin_skey_path).exists():
        print(f"  ERROR: ADMIN_SKEY_PATH not set or file missing: {admin_skey_path}")
        print("  Cannot complete Level 2 — need the admin skey to self-sign.")
        return False

    sk = PaymentSigningKey.load(admin_skey_path)
    tx = Transaction.from_cbor(tx_cbor)
    tx_hash_bytes = tx.transaction_body.hash()
    user_sig = sk.sign(tx_hash_bytes)
    user_vk_witness = VerificationKeyWitness(
        VerificationKey.from_signing_key(sk), user_sig,
    )
    # The admin witness is already in the tx; for self-test the same key
    # is already present, so the tx is already fully signed.
    # Just serialize as-is.
    fully_signed_cbor = tx.to_cbor().hex()

    # Submit
    print("\n  [3/3] Submitting to mainnet...")
    r = requests.post(
        f"{API_BASE}/submit-surrender",
        json={"tx_cbor_hex": fully_signed_cbor},
        headers=HEADERS,
        timeout=60,
    )
    if r.status_code != 200:
        print(f"  SUBMIT FAILED: {r.status_code}")
        try:
            print(f"  Detail: {r.json().get('detail', r.text)}")
        except Exception:
            print(f"  Response: {r.text[:500]}")
        return False

    submit_data = r.json()
    print(f"\n  SUBMITTED: {submit_data['tx_hash']}")
    print(f"  https://cardanoscan.io/transaction/{submit_data['tx_hash']}")
    print("\n  LEVEL 2 PASSED — Canary surrender submitted to mainnet")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Mainnet canary test for cMATRA surrender pipeline",
    )
    parser.add_argument("--address", type=str, help="Bech32 address (admin's mainnet wallet)")
    parser.add_argument("--asset", type=str, default="AGENT",
                        help="Asset key to test (default: AGENT)")
    parser.add_argument("--qty", type=int, default=1,
                        help="Quantity in base units (default: 1)")
    parser.add_argument("--submit", action="store_true",
                        help="Level 2: actually submit the tx (default: evaluate only)")
    parser.add_argument("--pool-status", action="store_true",
                        help="Just check pool status and exit")
    args = parser.parse_args()

    # Always start with health check
    if not check_health():
        print("\nService not ready. Fix configuration and retry.")
        sys.exit(1)

    if args.pool_status:
        check_pool_status()
        return

    if not args.address:
        print("\nERROR: --address is required for evaluate/submit tests")
        sys.exit(1)

    # Pool status
    if not check_pool_status():
        print("\nPool not funded. Deploy cMATRA pool UTxOs first.")
        sys.exit(1)

    # Level 1: evaluate (always run first)
    if not run_evaluate(args.address, args.asset, args.qty):
        print("\nLevel 1 FAILED. Do NOT proceed to Level 2.")
        sys.exit(1)

    # Level 2: submit (only if --submit)
    if args.submit:
        print("\n" + "!" * 60)
        print("  WARNING: This will submit a REAL transaction to MAINNET")
        print(f"  Surrendering {args.qty} {args.asset} for cMATRA")
        print("!" * 60)
        confirm = input("\n  Type 'YES' to proceed: ").strip()
        if confirm != "YES":
            print("  Aborted.")
            return
        if not run_build_and_submit(args.address, args.asset, args.qty):
            print("\nLevel 2 FAILED.")
            sys.exit(1)
    else:
        print("\n" + "-" * 60)
        print("Level 1 complete. To run Level 2 (real tx), add --submit")
        print("-" * 60)

    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
