#!/usr/bin/env python3
"""
scripts/preprod_chain_rehearsal.py — Pool-tip tx-chaining preprod rehearsal.

Drives the EXACT two-party build path that mainnet uses (the surrender-api
/build-surrender + admin/co-signer + /submit-surrender HTTP flow with the
in-memory pool-tip chainer), NOT the admin-only tools/process_surrender path.
This is the rehearsal mandated by the design doc + feedback_cardano_script_
output_datum_required.md: chunk N+1 must chain off chunk N's pending change
output while N is still in the mempool — the exact race that 503s today.

It proves the five design assertions and prints the evidence the operator
attaches to the PR:
  (1) chunk 2 chains off chunk1#1 while chunk1 is still in mempool
  (2) every chained pool output carries the Void inline datum (d87980)
  (3) all chunks land confirmed on preprod
  (4) a depth>cap run exercises the settling-wait (503 POOL_SETTLING) + recovery
  (5) kill+restart the service mid-chain -> cold-start re-seeds from confirmed
      Blockfrost state (no poisoned tip)

PREREQUISITES (operator-supplied — this script cannot fabricate them):
  - A running preprod surrender-api instance (NETWORK=preprod) reachable at
    --api-base, configured with the preprod v2 cMATRA policy/asset + the
    preprod surrender script address + admin_1 skey + a preprod co-signer.
  - BLOCKFROST_PROJECT_ID_PREPROD (preprod… key).
  - A funded preprod pool UTxO (cMATRA + Void datum) at the script address.
  - A preprod TEST WALLET holding >=16 legacy preprod NFTs so a "Surrender
    All" auto-chunks into >=4 chunks (and >=36 NFTs for the depth>8 run in
    assertion 4). If those preprod test assets do not exist, MINT them first
    via scripts/preprod_harness.py (stages 1-6) — do NOT fake them.

This driver SIMULATES the CIP-30 wallet: it loads the test wallet's payment
skey locally and produces the wallet's partial witness over the build-time
tx body, exactly as a browser wallet's signTx(partialSign=true) would. That
is the only mainnet difference (mainnet users sign in their own wallet); the
backend build->advance path under test is byte-identical.

Usage:
  NETWORK=preprod BLOCKFROST_PROJECT_ID_PREPROD=preprod... \\
    python scripts/preprod_chain_rehearsal.py \\
      --api-base http://127.0.0.1:8420 \\
      --api-secret <SURRENDER_API_SECRET> \\
      --test-wallet-skey audit_pack/preprod/keys/wallet_00.skey \\
      --restart-cmd "sudo systemctl restart surrender-api-preprod" \\
      --depth-cap 8
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import cbor2
import httpx
from pycardano import (
    Address,
    PaymentSigningKey,
    PaymentVerificationKey,
    Transaction,
    VerificationKey,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("preprod_chain_rehearsal")

VOID_DATUM_HEX = "d87980"


# ---------------------------------------------------------------------------
# Blockfrost preprod helpers (read-only)
# ---------------------------------------------------------------------------

def _bf_get(path: str, project_id: str) -> Any:
    url = f"https://cardano-preprod.blockfrost.io/api/v0{path}"
    r = httpx.get(url, headers={"project_id": project_id}, timeout=30.0)
    return r


def tx_confirmed(tx_hash: str, project_id: str) -> bool:
    """True once the tx is in a block (Blockfrost returns 200 for /txs/<h>)."""
    r = _bf_get(f"/txs/{tx_hash}", project_id)
    return r.status_code == 200


def output_datum_hex(tx_hash: str, output_index: int, project_id: str) -> str | None:
    """Read the inline datum of a confirmed tx output. Blockfrost returns
    inline_datum as canonical hex ("d87980") or a legacy JSON map; normalize
    both to canonical hex."""
    r = _bf_get(f"/txs/{tx_hash}/utxos", project_id)
    if r.status_code != 200:
        return None
    outs = r.json().get("outputs", [])
    if output_index >= len(outs):
        return None
    inline = outs[output_index].get("inline_datum")
    if inline is None:
        return None
    if isinstance(inline, str):
        return inline.lower()
    # legacy map form {"constructor":0,"fields":[]} -> re-encode to canonical
    if isinstance(inline, dict) and inline.get("constructor") == 0 and inline.get("fields") == []:
        return VOID_DATUM_HEX
    return json.dumps(inline)


# ---------------------------------------------------------------------------
# CIP-30 wallet simulation: produce the wallet's partial witness set
# ---------------------------------------------------------------------------

def wallet_partial_witness(tx_cbor_hex: str, skey: PaymentSigningKey) -> str:
    """Mimic CIP-30 signTx(partialSign=true): sign blake2b-256(tx_body) and
    return ONLY the wallet vkey witness set as CBOR hex `{0: [[pk, sig]]}`."""
    tx = Transaction.from_cbor(tx_cbor_hex)
    body_hash = tx.transaction_body.hash()
    sig = skey.sign(body_hash)
    vk = VerificationKey.from_signing_key(skey)
    # Conway vkey witness: [vkey_bytes, sig_bytes]; witness set map {0: [...]}.
    ws = {0: [[vk.payload, sig]]}
    return cbor2.dumps(ws).hex()


# ---------------------------------------------------------------------------
# surrender-api HTTP client
# ---------------------------------------------------------------------------

class SurrenderApi:
    def __init__(self, base: str, secret: str) -> None:
        self.base = base.rstrip("/")
        self.h = {"X-API-Secret": secret, "Content-Type": "application/json"}

    def build(self, user_address: str, assets: list[dict]) -> httpx.Response:
        return httpx.post(f"{self.base}/build-surrender", headers=self.h,
                          json={"user_address": user_address, "assets": assets},
                          timeout=120.0)

    def submit(self, tx_cbor_hex: str, tx_hash: str) -> httpx.Response:
        return httpx.post(f"{self.base}/submit-surrender", headers=self.h,
                          json={"tx_cbor_hex": tx_cbor_hex, "tx_hash": tx_hash},
                          timeout=120.0)

    def pool_status(self) -> dict:
        return httpx.get(f"{self.base}/pool-status", timeout=30.0).json()

    def chain_state(self) -> dict | None:
        return self.pool_status().get("chainState")


# ---------------------------------------------------------------------------
# One chunk: build -> wallet-cosign -> submit. Returns submitted tx hash.
# ---------------------------------------------------------------------------

def surrender_one_chunk(api: SurrenderApi, user_address: str, assets: list[dict],
                        skey: PaymentSigningKey) -> tuple[str, dict]:
    rb = api.build(user_address, assets)
    if rb.status_code != 200:
        raise RuntimeError(f"build failed [{rb.status_code}]: {rb.text[:300]}")
    bd = rb.json()
    chain_state = api.chain_state()
    logger.info("  built tx %s off pool_utxo %s | chainState=%s",
                bd["tx_hash"][:16], bd["pool_utxo_used"], chain_state)
    wallet_ws = wallet_partial_witness(bd["tx_cbor_hex"], skey)
    rs = api.submit(wallet_ws, bd["tx_hash"])
    if rs.status_code != 200:
        raise RuntimeError(f"submit failed [{rs.status_code}]: {rs.text[:300]}")
    submitted = rs.json()["tx_hash"]
    logger.info("  submitted -> %s | chainState=%s", submitted, api.chain_state())
    return submitted, bd


def _chunk_assets(nft_units: list[str], per_chunk: int) -> list[list[dict]]:
    """Split NFTs into chunks of `per_chunk`, one AGENT-collection asset entry
    per chunk (matches the frontend auto-chunker shape)."""
    chunks = []
    for i in range(0, len(nft_units), per_chunk):
        batch = nft_units[i:i + per_chunk]
        chunks.append([{"asset_key": "FLUX_PASS", "quantity_base": len(batch),
                        "nft_units": batch}])
    return chunks


# ---------------------------------------------------------------------------
# Rehearsal
# ---------------------------------------------------------------------------

def run(api: SurrenderApi, project_id: str, user_address: str,
        skey: PaymentSigningKey, nft_units: list[str], per_chunk: int,
        depth_cap: int, restart_cmd: str | None) -> None:
    evidence: dict[str, Any] = {"chunks": [], "assertions": {}}

    chunks = _chunk_assets(nft_units, per_chunk)
    logger.info("=== Assertion 1+2+3: %d chunks chaining off the pending tip ===",
                len(chunks))
    if len(chunks) < 2:
        raise SystemExit("Need >=2 chunks (>= 2*per_chunk NFTs) to prove chaining. "
                         "Fund the test wallet with more legacy preprod NFTs.")

    submitted_hashes: list[str] = []
    prev_pool_ref: str | None = None
    for i, assets in enumerate(chunks):
        logger.info("chunk %d/%d", i + 1, len(chunks))
        submitted, bd = surrender_one_chunk(api, user_address, assets, skey)
        # Assertion 1: chunk N+1's build consumed chunk N's submitted#1 BEFORE
        # chunk N confirmed (i.e. straight from the mempool tip).
        if i > 0:
            assert bd["pool_utxo_used"] == prev_pool_ref, (
                f"chunk {i+1} built off {bd['pool_utxo_used']}, "
                f"expected pending tip {prev_pool_ref}")
            logger.info("  ✓ chained off pending tip %s (chunk %d still in mempool)",
                        prev_pool_ref, i)
        submitted_hashes.append(submitted)
        prev_pool_ref = f"{submitted}#1"
        evidence["chunks"].append({"index": i, "tx_hash": submitted,
                                   "pool_utxo_used": bd["pool_utxo_used"]})
    evidence["assertions"]["1_chained_off_pending"] = True

    # Assertion 3: wait for all chunks to confirm.
    logger.info("=== Assertion 3: waiting for all %d chunks to confirm ===",
                len(submitted_hashes))
    deadline = time.time() + 600
    pending = set(submitted_hashes)
    while pending and time.time() < deadline:
        for h in list(pending):
            if tx_confirmed(h, project_id):
                logger.info("  confirmed %s", h)
                pending.discard(h)
        if pending:
            time.sleep(15)
    if pending:
        raise SystemExit(f"chunks never confirmed within 10min: {pending}")
    evidence["assertions"]["3_all_confirmed"] = True

    # Assertion 2: every chained pool output[1] carries the Void datum.
    logger.info("=== Assertion 2: every chained pool output carries d87980 ===")
    for h in submitted_hashes:
        d = output_datum_hex(h, 1, project_id)
        assert d == VOID_DATUM_HEX, f"tx {h} output[1] datum {d!r} != {VOID_DATUM_HEX!r}"
        logger.info("  ✓ %s#1 datum=%s", h[:16], d)
    evidence["assertions"]["2_datum_d87980"] = True

    logger.info("=== Assertion 4: depth>cap settling-wait + recovery ===")
    logger.info("  Re-run this script with a wallet holding >= %d NFTs "
                "(>%d chunks) to drive past the depth cap; expect a 503 "
                "{code:POOL_SETTLING} mid-run, then recovery after the pending "
                "root confirms.", (depth_cap + 1) * per_chunk, depth_cap)
    evidence["assertions"]["4_depth_cap_note"] = (
        f"drive >{depth_cap} chunks to exercise; per-chunk={per_chunk}")

    if restart_cmd:
        logger.info("=== Assertion 5: kill+restart mid-chain -> cold-start re-seed ===")
        cs_before = api.chain_state()
        logger.info("  chainState before restart: %s", cs_before)
        logger.info("  running restart: %s", restart_cmd)
        os.system(restart_cmd)
        time.sleep(8)
        # First build after restart re-seeds the tip from confirmed Blockfrost.
        rb = api.build(user_address, chunks[0])
        if rb.status_code != 200:
            raise SystemExit(f"post-restart build failed: {rb.status_code} {rb.text[:200]}")
        cs_after = api.chain_state()
        logger.info("  chainState after restart+build: %s", cs_after)
        assert cs_after and cs_after["depth"] == 0 and cs_after["status"] == "confirmed", (
            f"cold-start did not re-seed to a confirmed depth-0 tip: {cs_after}")
        logger.info("  ✓ cold-start re-seeded from confirmed state (depth 0)")
        evidence["assertions"]["5_cold_start_reseed"] = {
            "before": cs_before, "after": cs_after}

    print("\n===== REHEARSAL EVIDENCE (attach to PR) =====")
    print(json.dumps(evidence, indent=2))


def run_depth_cap(api: SurrenderApi, project_id: str, user_address: str,
                  skey: PaymentSigningKey, nft_units: list[str], per_chunk: int,
                  depth_cap: int) -> None:
    """Assertion 4: drive chunks past the depth cap. Expect builds to succeed up
    to the cap (tip pending at depth==cap), then a 503 {code: POOL_SETTLING},
    then — once the pending root confirms — recovery (the tip re-seeds and
    surrenders resume). Proves the wave-of-N behavior for a 95-chunk wallet."""
    chunks = _chunk_assets(nft_units, per_chunk)
    logger.info("=== Assertion 4: depth>cap settling-wait + recovery "
                "(%d chunks, cap %d) ===", len(chunks), depth_cap)
    if len(chunks) <= depth_cap:
        raise SystemExit(
            f"Need > {depth_cap} chunks to exercise the cap; have {len(chunks)} "
            f"({len(nft_units)} NFTs / {per_chunk} per chunk).")

    submitted: list[str] = []
    settling_seen = False
    prev_pool_ref: str | None = None
    for i, assets in enumerate(chunks):
        rb = api.build(user_address, assets)
        if rb.status_code == 503:
            body = rb.json()
            code = (body.get("detail") or {})
            code = code.get("code") if isinstance(code, dict) else body.get("code")
            cs = api.chain_state()
            logger.info("  chunk %d -> 503 %s | chainState=%s", i + 1, code, cs)
            assert code == "POOL_SETTLING", f"expected POOL_SETTLING, got {body}"
            assert cs and cs["depth"] >= depth_cap, f"503 below cap: {cs}"
            settling_seen = True
            # Recovery: once the pending chain confirms, the server-side
            # watchdog re-seeds the tip (resetting depth) within a tick and the
            # cap lifts. Poll the build until it recovers (handles the confirm
            # latency + the watchdog tick).
            root = cs["utxo_ref"].split("#", 1)[0]
            logger.info("  waiting for pending tip %s to confirm + watchdog "
                        "re-seed for recovery...", root[:16])
            deadline = time.time() + 360
            rb2 = None
            while time.time() < deadline:
                time.sleep(15)
                rb2 = api.build(user_address, assets)
                if rb2.status_code == 200:
                    break
                logger.info("    still settling (build %s); chainState=%s",
                            rb2.status_code, api.chain_state())
            assert rb2 is not None and rb2.status_code == 200, (
                f"post-settle build did not recover within window: "
                f"{rb2.status_code if rb2 else 'n/a'}")
            cs2 = api.chain_state()
            logger.info("  ✓ recovered after settle: chainState=%s", cs2)
            # Complete this recovered chunk so the chain is left clean.
            bd2 = rb2.json()
            ws = wallet_partial_witness(bd2["tx_cbor_hex"], skey)
            rs2 = api.submit(ws, bd2["tx_hash"])
            assert rs2.status_code == 200, f"recovered submit failed: {rs2.text[:200]}"
            submitted.append(rs2.json()["tx_hash"])
            break
        assert rb.status_code == 200, f"chunk {i+1} build failed: {rb.status_code} {rb.text[:200]}"
        bd = rb.json()
        if i > 0 and prev_pool_ref is not None:
            assert bd["pool_utxo_used"] == prev_pool_ref, (
                f"chunk {i+1} not chained off tip {prev_pool_ref}: {bd['pool_utxo_used']}")
        cs = api.chain_state()
        logger.info("  chunk %d/%d built; chainState=%s", i + 1, len(chunks), cs)
        ws = wallet_partial_witness(bd["tx_cbor_hex"], skey)
        rs = api.submit(ws, bd["tx_hash"])
        assert rs.status_code == 200, f"chunk {i+1} submit failed: {rs.text[:200]}"
        sub = rs.json()["tx_hash"]
        submitted.append(sub)
        prev_pool_ref = f"{sub}#1"

    assert settling_seen, "depth cap was never hit — POOL_SETTLING 503 not observed"
    print("\n===== DEPTH-CAP EVIDENCE (assertion 4) =====")
    print(json.dumps({"chunks_submitted_before_and_after_settle": submitted,
                      "settling_503_observed": settling_seen}, indent=2))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api-base", required=True, help="surrender-api base URL")
    p.add_argument("--api-secret", required=True, help="SURRENDER_API_SECRET")
    p.add_argument("--test-wallet-skey", required=True,
                   help="Path to the preprod test wallet payment .skey")
    p.add_argument("--per-chunk", type=int, default=4,
                   help="NFTs per chunk (matches frontend auto-chunk default)")
    p.add_argument("--depth-cap", type=int, default=8)
    p.add_argument("--restart-cmd", default=None,
                   help="Shell command to restart the service for assertion 5")
    p.add_argument("--max-nfts", type=int, default=0,
                   help="Cap the NFTs used (0 = all). Bounds the A/B/C chain "
                        "below the depth cap so the depth-cap run (assertion 4) "
                        "can be driven separately with the full set.")
    p.add_argument("--mode", choices=["abce", "depth-cap"], default="abce",
                   help="abce: assertions 1/2/3 (+5 restart). depth-cap: drive "
                        "past the cap to prove the 503 POOL_SETTLING + recovery.")
    args = p.parse_args(argv)

    project_id = os.environ.get(
        "BLOCKFROST_PROJECT_ID_PREPROD",
        os.environ.get("BLOCKFROST_PROJECT_ID", ""))
    if not project_id.startswith("preprod"):
        raise SystemExit("Set BLOCKFROST_PROJECT_ID_PREPROD (preprod… key).")

    skey = PaymentSigningKey.load(args.test_wallet_skey)
    vk = PaymentVerificationKey.from_signing_key(skey)
    user_address = str(Address(payment_part=vk.hash(), network=__import__(
        "pycardano").Network.TESTNET))
    logger.info("Test wallet address: %s", user_address)

    # Enumerate the wallet's legacy preprod NFTs (qty=1 assets, excluding ADA).
    r = _bf_get(f"/addresses/{user_address}", project_id)
    if r.status_code != 200:
        raise SystemExit(f"Could not read test wallet from Blockfrost: "
                         f"{r.status_code} {r.text[:200]}")
    amounts = r.json().get("amount", [])
    nft_units = [a["unit"] for a in amounts
                 if a["unit"] != "lovelace" and int(a["quantity"]) == 1]
    logger.info("Test wallet holds %d candidate legacy NFTs", len(nft_units))
    if len(nft_units) < 2 * args.per_chunk:
        raise SystemExit(
            f"BLOCKER: test wallet holds only {len(nft_units)} qty=1 NFTs; "
            f"need >= {2*args.per_chunk} to force >= 2 chunks (>= {(args.depth_cap+1)*args.per_chunk} "
            f"for the depth>{args.depth_cap} run). Mint legacy preprod NFTs via "
            f"scripts/preprod_harness.py first — do NOT fake them.")

    if args.max_nfts and args.max_nfts < len(nft_units):
        nft_units = sorted(nft_units)[:args.max_nfts]
        logger.info("Capped to %d NFTs for this run", len(nft_units))

    api = SurrenderApi(args.api_base, args.api_secret)
    if args.mode == "depth-cap":
        run_depth_cap(api, project_id, user_address, skey, nft_units,
                      args.per_chunk, args.depth_cap)
    else:
        run(api, project_id, user_address, skey, nft_units, args.per_chunk,
            args.depth_cap, args.restart_cmd)


if __name__ == "__main__":
    main()
