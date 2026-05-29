#!/usr/bin/env python3
"""
scripts/preprod_setup.py — Stand up preprod surrender infrastructure for the
pool-tip chaining + Path B redeemer rehearsal.

Provisions everything scripts/preprod_chain_rehearsal.py needs against the
CURRENT dual-admin main code:
  * fresh admin_1 (Server A) + admin_2 (Server B / cosigner) keys
  * a fresh cMATRA_TEST mint (new timelock slot)
  * a single legacy-NFT policy (native script) and >=NFT_COUNT distinct qty=1
    NFTs minted into one test wallet (forces >=4 chunks at batch=4, and >=9
    chunks at >=36 NFTs for the depth>8 run)
  * the dual-admin surrender validator parameterized with (admin_pkh_1,
    admin_pkh_2, deadline) -> parameterized blueprint + script address
  * one funded pool UTxO (cMATRA + Void datum) at that script address
  * a preprod rate table with a FLUX_PASS entry
  * env.local consumed by both the surrender-api and the cosigner

Funding: a one-time tADA grant from an existing funded preprod wallet
(--funder-skey) so we never depend on faucet rate-limits. All keys are fresh
and preprod-only; no production key is exposed to the surrender-api.

Idempotent: writes audit_pack/preprod_chain_rehearsal/setup_state.json and
skips completed stages on re-run.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import cbor2
from cbor2 import CBORTag
from pycardano import (
    Address,
    Asset,
    AssetName,
    BlockFrostChainContext,
    InvalidHereAfter,
    MultiAsset,
    Network,
    PaymentSigningKey,
    PaymentVerificationKey,
    RawPlutusData,
    ScriptAll,
    ScriptHash,
    ScriptPubkey,
    TransactionBuilder,
    TransactionOutput,
    Value,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("preprod_setup")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOID_DATUM_CBOR = cbor2.dumps(CBORTag(121, []))
CMATRA_ASSET_NAME_HEX = "634d41545241"          # "cMATRA"
CMATRA_SUPPLY_BASE = 1_000_000_000_000_000       # 1e15, 6 decimals
NFT_NAME_PREFIX = "464c5558504153535f"            # "FLUXPASS_"


def bf_ctx(pid: str) -> BlockFrostChainContext:
    return BlockFrostChainContext(project_id=pid)


def wait_for_tx(pid: str, tx_hash: str, timeout: int = 240) -> bool:
    import httpx
    deadline = time.time() + timeout
    url = f"https://cardano-preprod.blockfrost.io/api/v0/txs/{tx_hash}"
    while time.time() < deadline:
        r = httpx.get(url, headers={"project_id": pid}, timeout=30.0)
        if r.status_code == 200:
            return True
        time.sleep(10)
    return False


def gen_or_load_key(path: Path) -> tuple[PaymentSigningKey, PaymentVerificationKey, str, Address]:
    if path.exists():
        sk = PaymentSigningKey.load(str(path))
    else:
        sk = PaymentSigningKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        sk.save(str(path))
    vk = PaymentVerificationKey.from_signing_key(sk)
    pkh = vk.hash().payload.hex()
    addr = Address(payment_part=vk.hash(), network=Network.TESTNET)
    return sk, vk, pkh, addr


def fund_addresses(pid: str, funder_skey: Path, targets: list[tuple[Address, int]]) -> str:
    sk = PaymentSigningKey.load(str(funder_skey))
    vk = PaymentVerificationKey.from_signing_key(sk)
    funder_addr = Address(payment_part=vk.hash(), network=Network.TESTNET)
    ctx = bf_ctx(pid)
    b = TransactionBuilder(ctx)
    b.add_input_address(funder_addr)
    for addr, lovelace in targets:
        b.add_output(TransactionOutput(addr, Value(lovelace)))
    tx = b.build_and_sign([sk], change_address=funder_addr)
    h = tx.id.payload.hex()
    ctx.submit_tx(tx)
    logger.info("Funding tx submitted: %s", h)
    return h


def mint_cmatra(pid: str, admin_sk, admin_vk, admin_addr, timelock_slot: int) -> tuple[str, str]:
    sig = ScriptPubkey(admin_vk.hash())
    policy = ScriptAll([sig, InvalidHereAfter(timelock_slot)])
    policy_id = policy.hash()
    name = AssetName(bytes.fromhex(CMATRA_ASSET_NAME_HEX))
    ctx = bf_ctx(pid)
    b = TransactionBuilder(ctx)
    b.add_input_address(admin_addr)
    b.validity_start = ctx.last_block_slot
    b.ttl = timelock_slot - 1
    b.mint = MultiAsset({policy_id: Asset({name: CMATRA_SUPPLY_BASE})})
    b.native_scripts = [policy]
    multi = MultiAsset({policy_id: Asset({name: CMATRA_SUPPLY_BASE})})
    b.add_output(TransactionOutput(admin_addr, Value(5_000_000, multi)))
    tx = b.build_and_sign([admin_sk], change_address=admin_addr)
    h = tx.id.payload.hex()
    ctx.submit_tx(tx)
    logger.info("cMATRA_TEST mint tx: %s policy=%s", h, policy_id.payload.hex())
    return policy_id.payload.hex(), h


def mint_nfts(pid: str, admin_sk, admin_vk, admin_addr, target_addr: Address,
              count: int) -> tuple[str, list[str], str]:
    """Mint `count` distinct qty=1 NFTs under one native-script policy, all sent
    to target_addr in a single UTxO. Returns (policy_hex, [unit_hex...], tx)."""
    sig = ScriptPubkey(admin_vk.hash())
    policy = ScriptAll([sig])  # admin-signature-only native policy
    policy_id = policy.hash()
    ctx = bf_ctx(pid)
    b = TransactionBuilder(ctx)
    b.add_input_address(admin_addr)
    asset = Asset()
    units: list[str] = []
    for i in range(count):
        name_hex = NFT_NAME_PREFIX + f"{i:04x}"
        an = AssetName(bytes.fromhex(name_hex))
        asset[an] = 1
        units.append(policy_id.payload.hex() + name_hex)
    b.mint = MultiAsset({policy_id: asset})
    b.native_scripts = [policy]
    # Send all NFTs to target wallet in one UTxO (plenty of min-ada headroom).
    out_multi = MultiAsset({policy_id: asset})
    b.add_output(TransactionOutput(target_addr, Value(20_000_000, out_multi)))
    tx = b.build_and_sign([admin_sk], change_address=admin_addr)
    h = tx.id.payload.hex()
    ctx.submit_tx(tx)
    logger.info("Minted %d NFTs under policy %s, tx %s", count, policy_id.payload.hex(), h)
    return policy_id.payload.hex(), units, h


def apply_validator_params(blueprint_src: Path, admin_pkh_1: str, admin_pkh_2: str,
                           deadline_ms: int, out_path: Path) -> tuple[str, str]:
    """Apply (admin_pkh_1, admin_pkh_2, deadline) to the dual-admin validator via
    aiken blueprint apply. Writes the applied blueprint to out_path. Returns
    (script_hash_hex, bech32_script_address)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "plutus.json"
        shutil.copy2(blueprint_src, tmp)
        for cbor_hex in (
            cbor2.dumps(bytes.fromhex(admin_pkh_1)).hex(),
            cbor2.dumps(bytes.fromhex(admin_pkh_2)).hex(),
            cbor2.dumps(deadline_ms).hex(),
        ):
            r = subprocess.run(
                ["aiken", "blueprint", "apply", cbor_hex, "-i", str(tmp), "-o", str(tmp),
                 "-m", "claim_validator", "-v", "surrender_pool"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(f"aiken apply failed: {r.stderr}")
        blueprint = json.loads(tmp.read_text())
    # Pick the spend validator
    val = None
    for v in blueprint.get("validators", []):
        if "spend" in v.get("title", "").lower():
            val = v
            break
    val = val or blueprint["validators"][0]
    script_hash = val["hash"]
    out_path.write_text(json.dumps(blueprint, indent=2))
    addr = Address(payment_part=ScriptHash(bytes.fromhex(script_hash)), network=Network.TESTNET)
    logger.info("Applied dual-admin params; script_hash=%s addr=%s", script_hash, addr)
    return script_hash, str(addr)


def build_pool_utxo(pid: str, admin_sk, admin_addr, script_address: str,
                    cmatra_policy_hex: str, pool_base: int) -> str:
    name = AssetName(bytes.fromhex(CMATRA_ASSET_NAME_HEX))
    policy_id = ScriptHash(bytes.fromhex(cmatra_policy_hex))
    script_addr = Address.from_primitive(script_address)
    void_datum = RawPlutusData(cbor2.loads(VOID_DATUM_CBOR))
    ctx = bf_ctx(pid)
    b = TransactionBuilder(ctx)
    b.add_input_address(admin_addr)
    multi = MultiAsset({policy_id: Asset({name: pool_base})})
    b.add_output(TransactionOutput(script_addr, Value(5_000_000, multi), datum=void_datum))
    tx = b.build_and_sign([admin_sk], change_address=admin_addr)
    h = tx.id.payload.hex()
    ctx.submit_tx(tx)
    logger.info("Pool UTxO funded: tx %s (%d cMATRA + Void datum -> %s)",
                h, pool_base, script_address)
    return h


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work-dir", default=str(PROJECT_ROOT / "audit_pack/preprod_chain_rehearsal"))
    p.add_argument("--funder-skey", required=True, help="Funded preprod payment .skey")
    p.add_argument("--nft-count", type=int, default=40, help="NFTs to mint into the test wallet")
    p.add_argument("--pool-base", type=int, default=200_000_000_000,
                   help="cMATRA base units to lock in the pool UTxO")
    p.add_argument("--flux-pass-rate", type=int, default=1_000_000,
                   help="cMATRA base units per FLUX_PASS NFT (rate table)")
    p.add_argument("--deadline-hours", type=int, default=24)
    args = p.parse_args()

    pid = os.environ["BLOCKFROST_PROJECT_ID_PREPROD"]
    assert pid.startswith("preprod"), "need preprod Blockfrost key"

    work = Path(args.work_dir)
    keys = work / "keys"
    work.mkdir(parents=True, exist_ok=True)
    state_file = work / "setup_state.json"
    state: dict[str, Any] = json.loads(state_file.read_text()) if state_file.exists() else {}

    def save():
        state_file.write_text(json.dumps(state, indent=2))

    # Stage 0: keys
    a1_sk, a1_vk, a1_pkh, a1_addr = gen_or_load_key(keys / "admin_1.skey")
    a2_sk, a2_vk, a2_pkh, a2_addr = gen_or_load_key(keys / "admin_2.skey")
    w_sk, w_vk, w_pkh, w_addr = gen_or_load_key(keys / "wallet_00.skey")
    state.update(admin_1_pkh=a1_pkh, admin_1_addr=str(a1_addr),
                 admin_2_pkh=a2_pkh, admin_2_addr=str(a2_addr),
                 wallet_00_pkh=w_pkh, wallet_00_addr=str(w_addr))
    save()
    logger.info("admin_1=%s admin_2=%s wallet=%s", a1_addr, a2_addr, w_addr)

    # Stage 1: fund admin_1 (mint+pool+fees) and wallet_00 (chunk fees+collateral)
    if not state.get("funded"):
        h = fund_addresses(pid, Path(args.funder_skey),
                           [(a1_addr, 600_000_000), (w_addr, 400_000_000)])
        if not wait_for_tx(pid, h):
            raise SystemExit(f"funding tx {h} did not confirm")
        state["funding_tx"] = h
        state["funded"] = True
        save()

    # Stage 2: mint cMATRA_TEST
    if not state.get("cmatra_policy"):
        ctx = bf_ctx(pid)
        timelock = ctx.last_block_slot + args.deadline_hours * 3600 + 7200
        pol, h = mint_cmatra(pid, a1_sk, a1_vk, a1_addr, timelock)
        if not wait_for_tx(pid, h):
            raise SystemExit("cMATRA mint did not confirm")
        state.update(cmatra_policy=pol, cmatra_mint_tx=h, cmatra_timelock_slot=timelock)
        save()

    # Stage 3: mint NFTs into wallet_00
    if not state.get("nft_policy"):
        pol, units, h = mint_nfts(pid, a1_sk, a1_vk, a1_addr, w_addr, args.nft_count)
        if not wait_for_tx(pid, h):
            raise SystemExit("NFT mint did not confirm")
        state.update(nft_policy=pol, nft_units=units, nft_mint_tx=h)
        save()

    # Stage 4: apply dual-admin validator params -> blueprint + script address
    if not state.get("script_address"):
        deadline_ms = int(time.time() * 1000) + args.deadline_hours * 3600 * 1000
        applied = work / "plutus.applied.json"
        sh, addr = apply_validator_params(
            PROJECT_ROOT / "onchain/claim_validator/plutus.json",
            a1_pkh, a2_pkh, deadline_ms, applied,
        )
        state.update(script_hash=sh, script_address=addr,
                     applied_blueprint=str(applied), deadline_posix_ms=deadline_ms)
        save()

    # Stage 5: fund the pool UTxO
    if not state.get("pool_tx"):
        h = build_pool_utxo(pid, a1_sk, a1_addr, state["script_address"],
                            state["cmatra_policy"], args.pool_base)
        if not wait_for_tx(pid, h):
            raise SystemExit("pool UTxO tx did not confirm")
        state["pool_tx"] = h
        state["pool_base"] = args.pool_base
        save()

    # Stage 6: rate table (FLUX_PASS NFT entry, modest rate)
    rate_table = {
        "tokens": {"FLUX_PASS": {"rate_base_per_unit": args.flux_pass_rate, "is_nft": True}},
        "public_pool_base": args.pool_base,
    }
    rate_path = work / "rate_table_preprod.json"
    rate_path.write_text(json.dumps(rate_table, indent=2))
    state["rate_table_path"] = str(rate_path)
    save()

    # Stage 7: env.local for surrender-api + cosigner
    import secrets
    api_secret = state.get("api_secret") or secrets.token_urlsafe(32)
    cosigner_secret = state.get("cosigner_secret") or secrets.token_urlsafe(32)
    state.update(api_secret=api_secret, cosigner_secret=cosigner_secret)
    save()

    quarantine_preprod = "addr_test1wq5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqxrttf6"
    env_lines = [
        "NETWORK=preprod",
        f"BLOCKFROST_PROJECT_ID_PREPROD={pid}",
        f"BLOCKFROST_PROJECT_ID={pid}",
        f"FLUX_PASS_POLICY={state['nft_policy']}",
        f"SURRENDER_SCRIPT_ADDRESS={state['script_address']}",
        f"CMATRA_POLICY_HEX={state['cmatra_policy']}",
        f"CMATRA_ASSET_HEX={CMATRA_ASSET_NAME_HEX}",
        f"QUARANTINE_ADDRESS={quarantine_preprod}",
        f"ADMIN_SKEY_PATH={keys / 'admin_1.skey'}",
        f"RATE_TABLE_PATH={rate_path}",
        f"BLUEPRINT_PATH={state['applied_blueprint']}",
        f"SURRENDER_API_SECRET={api_secret}",
        "SURRENDER_API_PORT=8420",
        f"COSIGNER_URL=http://127.0.0.1:8421",
        f"COSIGNER_API_SECRET={cosigner_secret}",
        f"COSIGNER_PKH={state['admin_2_pkh']}",
        f"COSIGNER_SKEY_PATH={keys / 'admin_2.skey'}",
        "COSIGNER_API_PORT=8421",
        "POOL_TIP_DEPTH_CAP=8",
        "POOL_TIP_EVICTION_WINDOW_S=240",
        "POOL_TIP_WATCHDOG_INTERVAL_S=30",
    ]
    env_path = PROJECT_ROOT / "env.local"
    env_path.write_text("\n".join(env_lines) + "\n")
    state["env_local"] = str(env_path)
    save()

    logger.info("=== SETUP COMPLETE ===")
    logger.info("script_address=%s", state["script_address"])
    logger.info("cmatra_policy=%s", state["cmatra_policy"])
    logger.info("nft_policy=%s (%d NFTs in %s)", state["nft_policy"],
                args.nft_count, w_addr)
    logger.info("pool_tx=%s (%d cMATRA)", state["pool_tx"], args.pool_base)
    logger.info("env.local written to %s", env_path)
    print(json.dumps({k: state[k] for k in (
        "script_address", "cmatra_policy", "nft_policy", "pool_tx",
        "funding_tx", "cmatra_mint_tx", "nft_mint_tx", "deadline_posix_ms",
    )}, indent=2))


if __name__ == "__main__":
    main()
