#!/usr/bin/env python3
"""
Preprod Rehearsal Harness
=========================

Full end-to-end preprod deployment for the FLUX merger pipeline.

Stages:
  1. Generate admin + test wallets (or load existing)
  2. Mint AGENT_TEST (0 decimals) and SHARDS_TEST (6 decimals)
  3. Distribute tokens to test wallets with adversarial patterns
  4. Generate synthetic allocation CSV
  5. Mint FLUX_TEST with timelock native script
  6. Deploy claim validator → derive script address
  7. Build claim UTxOs (send FLUX to script with inline datums)
  8. Build claim index
  9. Claim from test wallets (happy path)
 10. Red-team: adversarial claim attempts

Requires:
  - NETWORK=preprod in env
  - BLOCKFROST_PROJECT_ID_PREPROD set (or BLOCKFROST_PROJECT_ID with a preprod key)
  - tADA in the admin wallet (get from faucet: https://docs.cardano.org/cardano-testnets/tools/faucet/)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cbor2
from cbor2 import CBORTag
from pycardano import (
    Address,
    Asset,
    AssetName,
    BlockFrostChainContext,
    MultiAsset,
    NativeScript,
    PaymentSigningKey,
    PaymentVerificationKey,
    ScriptAll,
    ScriptPubkey,
    InvalidBefore,
    InvalidHereAfter,
    Transaction,
    TransactionBody,
    TransactionBuilder,
    TransactionInput,
    TransactionOutput,
    TransactionWitnessSet,
    UTxO,
    Value,
    Network,
    Redeemer,
    RawPlutusData,
    PlutusV3Script,
    ExecutionUnits,
)
from pycardano.hash import (
    ScriptHash,
    TransactionId,
    VerificationKeyHash,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FLUX_TEST_SUPPLY_BASE = 1_000_000_000_000_000  # 1e15 — same as mainnet
FLUX_TEST_DECIMALS = 6
FLUX_ASSET_NAME_HEX = "464c5558"  # "FLUX"


# ---------------------------------------------------------------------------
# Wallet management
# ---------------------------------------------------------------------------

@dataclass
class Wallet:
    name: str
    skey: PaymentSigningKey
    vkey: PaymentVerificationKey
    address: Address
    pkh_hex: str

    @classmethod
    def generate(cls, name: str, network: Network = Network.TESTNET) -> "Wallet":
        sk = PaymentSigningKey.generate()
        vk = PaymentVerificationKey.from_signing_key(sk)
        pkh = vk.hash()
        addr = Address(payment_part=pkh, network=network)
        return cls(name=name, skey=sk, vkey=vk, address=addr, pkh_hex=pkh.payload.hex())

    @classmethod
    def load(cls, name: str, keys_dir: Path, network: Network = Network.TESTNET) -> "Wallet":
        skey_path = keys_dir / f"{name}.skey"
        sk = PaymentSigningKey.load(str(skey_path))
        vk = PaymentVerificationKey.from_signing_key(sk)
        pkh = vk.hash()
        addr = Address(payment_part=pkh, network=network)
        return cls(name=name, skey=sk, vkey=vk, address=addr, pkh_hex=pkh.payload.hex())

    def save(self, keys_dir: Path) -> None:
        keys_dir.mkdir(parents=True, exist_ok=True)
        skey_path = keys_dir / f"{self.name}.skey"
        self.skey.save(str(skey_path))
        info_path = keys_dir / f"{self.name}.json"
        with open(info_path, "w") as f:
            json.dump({
                "name": self.name,
                "address": str(self.address),
                "pkh_hex": self.pkh_hex,
            }, f, indent=2)


def ensure_wallets(
    keys_dir: Path,
    num_test_wallets: int = 20,
    network: Network = Network.TESTNET,
) -> tuple[Wallet, list[Wallet]]:
    """Generate or load admin wallet + test wallets."""
    keys_dir.mkdir(parents=True, exist_ok=True)

    # Admin wallet
    admin_skey = keys_dir / "admin.skey"
    if admin_skey.exists():
        admin = Wallet.load("admin", keys_dir, network)
        logger.info("Loaded admin wallet: %s", admin.address)
    else:
        admin = Wallet.generate("admin", network)
        admin.save(keys_dir)
        logger.info("Generated admin wallet: %s", admin.address)

    # Test wallets
    test_wallets: list[Wallet] = []
    for i in range(num_test_wallets):
        name = f"test_{i:03d}"
        skey_path = keys_dir / f"{name}.skey"
        if skey_path.exists():
            w = Wallet.load(name, keys_dir, network)
        else:
            w = Wallet.generate(name, network)
            w.save(keys_dir)
        test_wallets.append(w)

    logger.info("Loaded/generated %d test wallets", len(test_wallets))
    return admin, test_wallets


# ---------------------------------------------------------------------------
# Native script token minting
# ---------------------------------------------------------------------------

def mint_test_token(
    context: BlockFrostChainContext,
    admin: Wallet,
    token_name_hex: str,
    total_supply: int,
) -> tuple[ScriptHash, str]:
    """Mint a test token using a simple native script (admin signature required).

    Returns (policy_id_hash, tx_hash_hex).
    """
    # Build native script: RequireSignature(admin_pkh)
    pub_key_hash = admin.vkey.hash()
    native_script = ScriptPubkey(pub_key_hash)

    policy_id = native_script.hash()
    asset_name = AssetName(bytes.fromhex(token_name_hex))

    logger.info(
        "Minting %d of asset %s under policy %s",
        total_supply, token_name_hex, policy_id.payload.hex(),
    )

    builder = TransactionBuilder(context)
    builder.add_input_address(admin.address)

    # Mint
    builder.mint = MultiAsset()
    builder.mint[policy_id] = Asset({asset_name: total_supply})
    builder.native_scripts = [native_script]

    # Send minted tokens to admin
    multi = MultiAsset()
    multi[policy_id] = Asset({asset_name: total_supply})
    min_ada = 2_000_000
    tx_out = TransactionOutput(admin.address, Value(min_ada, multi))
    builder.add_output(tx_out)

    signed_tx = builder.build_and_sign(
        signing_keys=[admin.skey],
        change_address=admin.address,
    )

    tx_hash = signed_tx.id.payload.hex()
    context.submit_tx(signed_tx)
    logger.info("Mint TX submitted: %s", tx_hash)
    return policy_id, tx_hash


# ---------------------------------------------------------------------------
# Token distribution to test wallets
# ---------------------------------------------------------------------------

@dataclass
class Distribution:
    """Describes how to distribute test tokens to wallets."""
    wallet_index: int
    agent_amount: int  # base units (0 decimals)
    shards_amount: int  # base units (6 decimals)
    description: str


def build_adversarial_distributions(num_wallets: int) -> list[Distribution]:
    """Create adversarial distribution patterns for testing.

    Patterns:
      - Tiny dust amounts
      - Huge whale amounts
      - Both tokens
      - Only one token
      - Amounts that cause rounding edge cases
      - Zero-eligible wallets (will get 0 FLUX)
    """
    dists: list[Distribution] = []

    # Wallet 0: Whale — holds most AGENT
    dists.append(Distribution(0, 500_000_000, 0, "agent_whale"))

    # Wallet 1: Whale — holds most SHARDS (1.5M display = 1.5e12 base)
    dists.append(Distribution(1, 0, 1_500_000_000_000, "shards_whale"))

    # Wallet 2: Both tokens, moderate amounts
    dists.append(Distribution(2, 100_000_000, 500_000_000_000, "both_moderate"))

    # Wallet 3: Both tokens, small amounts
    dists.append(Distribution(3, 1_000, 1_000_000, "both_small"))

    # Wallet 4: Dust AGENT only (1 base unit)
    dists.append(Distribution(4, 1, 0, "agent_dust"))

    # Wallet 5: Dust SHARDS only (1 base unit = 0.000001 display)
    dists.append(Distribution(5, 0, 1, "shards_dust"))

    # Wallet 6: Amount that causes rounding stress
    # AGENT 333_333_333 → divides unevenly into FLUX bucket
    dists.append(Distribution(6, 333_333_333, 0, "agent_rounding"))

    # Wallet 7: SHARDS amount that causes rounding stress
    dists.append(Distribution(7, 0, 777_777_777_777, "shards_rounding"))

    # Wallet 8: Both tokens, large AGENT small SHARDS
    dists.append(Distribution(8, 50_000_000, 100, "mixed_asymmetric"))

    # Wallet 9: Both tokens, equal USD value (approximately)
    dists.append(Distribution(9, 10_000_000, 100_000_000_000, "both_balanced"))

    # Wallets 10-14: Graduated small amounts for AGENT
    for i in range(10, min(15, num_wallets)):
        amount = 10 ** (i - 9)  # 10, 100, 1000, 10000, 100000
        dists.append(Distribution(i, amount, 0, f"agent_graduated_{amount}"))

    # Wallets 15-19: Graduated small amounts for SHARDS
    for i in range(15, min(20, num_wallets)):
        amount = 10 ** (i - 14)  # 10, 100, 1000, 10000, 100000 base units
        dists.append(Distribution(i, 0, amount, f"shards_graduated_{amount}"))

    return dists


def distribute_test_tokens(
    blockfrost_project_id: str,
    admin: Wallet,
    test_wallets: list[Wallet],
    agent_policy_id: ScriptHash,
    agent_name_hex: str,
    shards_policy_id: ScriptHash,
    shards_name_hex: str,
    distributions: list[Distribution],
) -> list[str]:
    """Send test tokens from admin to test wallets per distribution spec.

    Returns list of submitted tx hashes.
    """
    agent_name = AssetName(bytes.fromhex(agent_name_hex))
    shards_name = AssetName(bytes.fromhex(shards_name_hex))

    tx_hashes = []

    # Batch distributions into transactions (max 10 outputs per tx for safety)
    batch_size = 10
    for batch_start in range(0, len(distributions), batch_size):
        batch = distributions[batch_start : batch_start + batch_size]

        # Fresh context each batch to avoid stale UTxO cache
        ctx = BlockFrostChainContext(project_id=blockfrost_project_id)

        builder = TransactionBuilder(ctx)
        builder.add_input_address(admin.address)

        for dist in batch:
            wallet = test_wallets[dist.wallet_index]
            multi = MultiAsset()
            has_asset = False

            if dist.agent_amount > 0:
                multi[agent_policy_id] = Asset({agent_name: dist.agent_amount})
                has_asset = True
            if dist.shards_amount > 0:
                if shards_policy_id in multi:
                    multi[shards_policy_id][shards_name] = dist.shards_amount
                else:
                    multi[shards_policy_id] = Asset({shards_name: dist.shards_amount})
                has_asset = True

            if not has_asset:
                continue

            min_ada = 2_000_000  # 2 ADA per UTxO
            tx_out = TransactionOutput(wallet.address, Value(min_ada, multi))
            builder.add_output(tx_out)

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )

        tx_hash = signed_tx.id.payload.hex()
        ctx.submit_tx(signed_tx)
        tx_hashes.append(tx_hash)
        logger.info(
            "Distribution batch tx: %s (%d outputs)",
            tx_hash, len(batch),
        )

        # Wait for on-chain confirmation before next batch
        logger.info("Waiting 30s for chain confirmation...")
        time.sleep(30)

    return tx_hashes


# ---------------------------------------------------------------------------
# Synthetic allocation CSV
# ---------------------------------------------------------------------------

def generate_synthetic_allocation(
    test_wallets: list[Wallet],
    distributions: list[Distribution],
    agent_bucket: int,
    shards_bucket: int,
    agent_supply: int,
    shards_supply: int,
    out_csv: Path,
) -> dict[str, Any]:
    """Generate a synthetic allocation CSV using the same math as Phase 4.

    Returns summary stats.
    """
    # Build per-wallet balances
    agent_balances: dict[str, int] = {}
    shards_balances: dict[str, int] = {}

    for dist in distributions:
        pkh = test_wallets[dist.wallet_index].pkh_hex
        agent_balances[pkh] = agent_balances.get(pkh, 0) + dist.agent_amount
        shards_balances[pkh] = shards_balances.get(pkh, 0) + dist.shards_amount

    # Compute allocations using integer floor division
    allocations: dict[str, dict[str, int]] = {}  # pkh → {agent_flux, shards_flux, total}

    total_agent_alloc = 0
    total_shards_alloc = 0

    for pkh in set(list(agent_balances.keys()) + list(shards_balances.keys())):
        agent_bal = agent_balances.get(pkh, 0)
        shards_bal = shards_balances.get(pkh, 0)

        agent_flux = (agent_bal * agent_bucket) // agent_supply if agent_bal > 0 else 0
        shards_flux = (shards_bal * shards_bucket) // shards_supply if shards_bal > 0 else 0
        total_flux = agent_flux + shards_flux

        allocations[pkh] = {
            "agent_flux": agent_flux,
            "shards_flux": shards_flux,
            "flux_total_units": total_flux,
            "agent_balance": agent_bal,
            "shards_balance": shards_bal,
        }
        total_agent_alloc += agent_flux
        total_shards_alloc += shards_flux

    # Write CSV
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "payment_key_hash_hex", "flux_total_units",
            "agent_flux_units", "shards_flux_units",
            "agent_balance", "shards_balance", "addresses",
        ])
        for pkh, alloc in sorted(allocations.items()):
            if alloc["flux_total_units"] <= 0:
                continue
            # Find wallet address
            addr = ""
            for w in test_wallets:
                if w.pkh_hex == pkh:
                    addr = str(w.address)
                    break
            writer.writerow([
                pkh,
                alloc["flux_total_units"],
                alloc["agent_flux"],
                alloc["shards_flux"],
                alloc["agent_balance"],
                alloc["shards_balance"],
                addr,
            ])

    agent_dust = agent_bucket - total_agent_alloc
    shards_dust = shards_bucket - total_shards_alloc
    total_distributed = total_agent_alloc + total_shards_alloc

    summary = {
        "num_claimants": sum(1 for a in allocations.values() if a["flux_total_units"] > 0),
        "total_distributed": total_distributed,
        "agent_dust": agent_dust,
        "shards_dust": shards_dust,
        "total_dust": agent_dust + shards_dust,
        "distributed_plus_dust": total_distributed + agent_dust + shards_dust,
        "flux_supply": agent_bucket + shards_bucket,
        "invariant_holds": (total_distributed + agent_dust + shards_dust) == (agent_bucket + shards_bucket),
    }

    logger.info("Synthetic allocation: %d claimants, %d FLUX distributed", summary["num_claimants"], total_distributed)
    logger.info("Dust: AGENT %d + SHARDS %d = %d", agent_dust, shards_dust, summary["total_dust"])
    logger.info("Invariant (dist+dust == supply): %s", summary["invariant_holds"])

    return summary


# ---------------------------------------------------------------------------
# FLUX mint with timelock
# ---------------------------------------------------------------------------

def mint_flux_test(
    context: BlockFrostChainContext,
    admin: Wallet,
    timelock_slot: int,
) -> tuple[ScriptHash, str]:
    """Mint FLUX_TEST with a time-locked native script.

    Policy: RequireAll [RequireSignature(admin), InvalidHereAfter(slot)]

    Returns (policy_id_hash, tx_hash_hex).
    """
    pub_key_hash = admin.vkey.hash()
    sig_script = ScriptPubkey(pub_key_hash)
    time_script = InvalidHereAfter(timelock_slot)
    policy_script = ScriptAll([sig_script, time_script])

    policy_id = policy_script.hash()
    asset_name = AssetName(bytes.fromhex(FLUX_ASSET_NAME_HEX))

    logger.info(
        "Minting FLUX_TEST: supply=%d, policy=%s, timelock_slot=%d",
        FLUX_TEST_SUPPLY_BASE, policy_id.payload.hex(), timelock_slot,
    )

    builder = TransactionBuilder(context)
    builder.add_input_address(admin.address)

    # Set validity interval (must be before timelock)
    builder.validity_start = context.last_block_slot
    builder.ttl = timelock_slot - 1

    # Mint
    builder.mint = MultiAsset()
    builder.mint[policy_id] = Asset({asset_name: FLUX_TEST_SUPPLY_BASE})
    builder.native_scripts = [policy_script]

    # Send to admin
    multi = MultiAsset()
    multi[policy_id] = Asset({asset_name: FLUX_TEST_SUPPLY_BASE})
    tx_out = TransactionOutput(admin.address, Value(5_000_000, multi))
    builder.add_output(tx_out)

    signed_tx = builder.build_and_sign(
        signing_keys=[admin.skey],
        change_address=admin.address,
    )

    tx_hash = signed_tx.id.payload.hex()
    context.submit_tx(signed_tx)
    logger.info("FLUX_TEST mint TX submitted: %s", tx_hash)
    return policy_id, tx_hash


# ---------------------------------------------------------------------------
# Claim validator deployment
# ---------------------------------------------------------------------------

def load_claim_validator(blueprint_path: Path) -> tuple[PlutusV3Script, ScriptHash, str]:
    """Load compiled claim validator from Aiken blueprint.

    Returns (script, script_hash, script_address_bech32).
    """
    with open(blueprint_path) as f:
        blueprint = json.load(f)

    validators = blueprint.get("validators", [])
    compiled_code = None
    for v in validators:
        if "spend" in v.get("title", "").lower():
            compiled_code = v.get("compiledCode")
            break

    if compiled_code is None and validators:
        compiled_code = validators[0].get("compiledCode")

    if compiled_code is None:
        raise ValueError(f"No compiled validator in {blueprint_path}")

    script = PlutusV3Script(bytes.fromhex(compiled_code))
    script_hash = ScriptHash(bytes.fromhex(blueprint["validators"][0]["hash"]))

    # Derive testnet script address
    script_address = Address(payment_part=script_hash, network=Network.TESTNET)

    logger.info("Claim validator hash: %s", script_hash.payload.hex())
    logger.info("Script address (preprod): %s", script_address)

    return script, script_hash, str(script_address)


# ---------------------------------------------------------------------------
# Build claim UTxOs (Phase 6)
# ---------------------------------------------------------------------------

def encode_claim_datum(pkh_hex: str) -> bytes:
    """Encode ClaimDatum as CBOR: Constr(0, [pkh_bytes])."""
    pkh_bytes = bytes.fromhex(pkh_hex)
    assert len(pkh_bytes) == 28, f"Expected 28-byte keyhash, got {len(pkh_bytes)}"
    return cbor2.dumps(CBORTag(121, [pkh_bytes]))


def build_claim_utxos(
    blockfrost_project_id: str,
    admin: Wallet,
    allocations_csv: Path,
    script_address: str,
    flux_policy_id: ScriptHash,
    batch_size: int = 20,
) -> list[dict[str, Any]]:
    """Build and submit claim UTxO transactions.

    Returns list of batch results with tx hashes and output details.
    """
    asset_name = AssetName(bytes.fromhex(FLUX_ASSET_NAME_HEX))

    # Load allocations
    rows = []
    with open(allocations_csv, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            flux = int(r["flux_total_units"])
            if flux > 0:
                rows.append({"pkh": r["payment_key_hash_hex"], "flux": flux})

    logger.info("Loaded %d allocations for claim UTxO building", len(rows))

    script_addr = Address.from_primitive(script_address)
    batches_result = []

    for batch_start in range(0, len(rows), batch_size):
        batch = rows[batch_start : batch_start + batch_size]
        batch_num = batch_start // batch_size

        # Fresh context each batch to avoid stale UTxO cache
        ctx = BlockFrostChainContext(project_id=blockfrost_project_id)

        builder = TransactionBuilder(ctx)
        builder.add_input_address(admin.address)

        output_details = []
        for alloc in batch:
            datum_cbor = encode_claim_datum(alloc["pkh"])
            datum = RawPlutusData(cbor2.loads(datum_cbor))

            multi = MultiAsset()
            multi[flux_policy_id] = Asset({asset_name: alloc["flux"]})
            min_ada = 2_000_000
            value = Value(min_ada, multi)

            tx_out = TransactionOutput(script_addr, value, datum=datum)
            builder.add_output(tx_out)

            output_details.append({
                "payment_key_hash_hex": alloc["pkh"],
                "flux_units": alloc["flux"],
            })

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )

        tx_hash = signed_tx.id.payload.hex()
        ctx.submit_tx(signed_tx)

        batch_result = {
            "batch_index": batch_num,
            "tx_hash": tx_hash,
            "num_outputs": len(output_details),
            "claimants": output_details,
        }
        batches_result.append(batch_result)
        logger.info("Claim UTxO batch %d submitted: %s (%d outputs)", batch_num, tx_hash, len(output_details))

        # Wait for on-chain confirmation before next batch
        logger.info("Waiting 30s for chain confirmation...")
        time.sleep(30)

    return batches_result


# ---------------------------------------------------------------------------
# Claim index (Phase 7) — simplified for preprod
# ---------------------------------------------------------------------------

def build_claim_index_from_batches(
    batches: list[dict[str, Any]],
    script_address: str,
    flux_policy_hex: str,
) -> dict[str, list]:
    """Build claim index directly from batch submission results.

    On preprod we know the output indices because we built them.
    Output 0..N-1 correspond to batch outputs (change is last).
    """
    index: dict[str, list] = {}
    for batch in batches:
        tx_hash = batch["tx_hash"]
        for i, claimant in enumerate(batch["claimants"]):
            pkh = claimant["payment_key_hash_hex"]
            flux = claimant["flux_units"]
            if pkh not in index:
                index[pkh] = []
            index[pkh].append([tx_hash, i, flux])

    logger.info("Built claim index: %d keyhashes", len(index))
    return index


# ---------------------------------------------------------------------------
# Claim client (Phase 8)
# ---------------------------------------------------------------------------

def claim_flux(
    context: BlockFrostChainContext,
    wallet: Wallet,
    claim_refs: list[list],
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
) -> str:
    """Build and submit a claim transaction for a single wallet.

    Returns tx hash.
    """
    asset_name = AssetName(bytes.fromhex(FLUX_ASSET_NAME_HEX))
    script_addr = Address.from_primitive(script_address)

    builder = TransactionBuilder(context)

    for ref in claim_refs:
        tx_hash_hex, output_index, flux_qty = ref[0], ref[1], ref[2]

        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(tx_hash_hex)),
            output_index,
        )

        # Reconstruct UTxO
        multi = MultiAsset()
        multi[flux_policy_id] = Asset({asset_name: flux_qty})
        value = Value(2_000_000, multi)

        datum_cbor = encode_claim_datum(wallet.pkh_hex)
        datum = RawPlutusData(cbor2.loads(datum_cbor))

        utxo = UTxO(tx_in, TransactionOutput(script_addr, value, datum=datum))

        # Unit redeemer
        redeemer = Redeemer(RawPlutusData(cbor2.loads(b"\xd8\x79\x80")))

        builder.add_script_input(
            utxo,
            script=script,
            redeemer=redeemer,
        )

    # Required signer — the claimant
    builder.required_signers = [wallet.vkey.hash()]

    # Add collateral (wallet needs some ADA UTxOs)
    builder.add_input_address(wallet.address)

    signed_tx = builder.build_and_sign(
        signing_keys=[wallet.skey],
        change_address=wallet.address,
    )

    tx_hash = signed_tx.id.payload.hex()
    context.submit_tx(signed_tx)
    return tx_hash


# ---------------------------------------------------------------------------
# Red-team tests (Phase 10)
# ---------------------------------------------------------------------------

def red_team_wrong_signer(
    context: BlockFrostChainContext,
    wrong_wallet: Wallet,
    victim_wallet: Wallet,
    claim_refs: list[list],
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
) -> bool:
    """Attempt to claim someone else's UTxO. Should FAIL.

    Returns True if attack was correctly rejected.
    """
    asset_name = AssetName(bytes.fromhex(FLUX_ASSET_NAME_HEX))
    script_addr = Address.from_primitive(script_address)

    try:
        builder = TransactionBuilder(context)

        for ref in claim_refs:
            tx_hash_hex, output_index, flux_qty = ref[0], ref[1], ref[2]
            tx_in = TransactionInput(
                TransactionId(bytes.fromhex(tx_hash_hex)),
                output_index,
            )
            multi = MultiAsset()
            multi[flux_policy_id] = Asset({asset_name: flux_qty})
            value = Value(2_000_000, multi)

            # Datum has the VICTIM's pkh
            datum_cbor = encode_claim_datum(victim_wallet.pkh_hex)
            datum = RawPlutusData(cbor2.loads(datum_cbor))
            utxo = UTxO(tx_in, TransactionOutput(script_addr, value, datum=datum))

            redeemer = Redeemer(RawPlutusData(cbor2.loads(b"\xd8\x79\x80")))
            builder.add_script_input(utxo, script=script, redeemer=redeemer)

        # Sign with WRONG wallet
        builder.required_signers = [wrong_wallet.vkey.hash()]
        builder.add_input_address(wrong_wallet.address)

        signed_tx = builder.build_and_sign(
            signing_keys=[wrong_wallet.skey],
            change_address=wrong_wallet.address,
        )
        context.submit_tx(signed_tx)

        # If we get here, the attack succeeded — BAD
        logger.error("RED TEAM FAIL: wrong-signer claim was accepted!")
        return False

    except Exception as e:
        logger.info("RED TEAM PASS: wrong-signer claim rejected: %s", str(e)[:200])
        return True


def red_team_double_claim(
    context: BlockFrostChainContext,
    wallet: Wallet,
    claim_refs: list[list],
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
) -> bool:
    """Attempt to claim the same UTxO twice. Second should FAIL.

    Returns True if double-claim was correctly rejected.
    """
    try:
        # First claim should succeed
        tx_hash = claim_flux(
            context, wallet, claim_refs,
            script_address, script, flux_policy_id,
        )
        logger.info("First claim succeeded: %s", tx_hash)
        time.sleep(20)  # Wait for confirmation

        # Second claim should fail (UTxO already spent)
        try:
            tx_hash2 = claim_flux(
                context, wallet, claim_refs,
                script_address, script, flux_policy_id,
            )
            logger.error("RED TEAM FAIL: double-claim was accepted! TX: %s", tx_hash2)
            return False
        except Exception as e:
            logger.info("RED TEAM PASS: double-claim rejected: %s", str(e)[:200])
            return True

    except Exception as e:
        logger.error("RED TEAM ERROR: first claim failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_preprod_rehearsal(
    work_dir: Path,
    blockfrost_project_id: str,
    blueprint_path: Path,
    num_test_wallets: int = 20,
    timelock_offset_slots: int = 3600,  # ~1 hour after current slot
    skip_to_stage: int = 1,
) -> None:
    """Run the full preprod rehearsal."""
    keys_dir = work_dir / "keys"
    data_dir = work_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Let blockfrost-python auto-detect network from project_id prefix
    context = BlockFrostChainContext(
        project_id=blockfrost_project_id,
    )

    state_file = work_dir / "rehearsal_state.json"
    state: dict[str, Any] = {}
    if state_file.exists():
        with open(state_file) as f:
            state = json.load(f)

    def save_state():
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)

    # ---------------------------------------------------------------
    # Stage 1: Wallets
    # ---------------------------------------------------------------
    if skip_to_stage <= 1:
        logger.info("=" * 60)
        logger.info("STAGE 1: Wallet setup")
        logger.info("=" * 60)

        admin, test_wallets = ensure_wallets(keys_dir, num_test_wallets)

        state["admin_address"] = str(admin.address)
        state["admin_pkh"] = admin.pkh_hex
        state["test_wallets"] = [
            {"name": w.name, "address": str(w.address), "pkh": w.pkh_hex}
            for w in test_wallets
        ]
        save_state()

        logger.info("")
        logger.info(">>> ADMIN ADDRESS: %s", admin.address)
        logger.info(">>> Fund this address with tADA from the Cardano faucet:")
        logger.info(">>> https://docs.cardano.org/cardano-testnets/tools/faucet/")
        logger.info("")

        # Check balance
        try:
            utxos = context.utxos(admin.address)
            total_ada = sum(u.output.amount.coin if hasattr(u.output.amount, 'coin') else u.output.amount for u in utxos)
            logger.info("Admin balance: %d lovelace (%.2f ADA)", total_ada, total_ada / 1e6)
            if total_ada < 100_000_000:
                logger.warning("Insufficient tADA! Need at least 100 ADA. Fund the admin wallet and re-run.")
                state["stage_1_complete"] = True
                state["needs_funding"] = True
                save_state()
                return
        except Exception as e:
            logger.warning("Could not check balance (wallet may not be funded yet): %s", e)
            state["stage_1_complete"] = True
            state["needs_funding"] = True
            save_state()
            logger.info("Fund the admin wallet and re-run with --skip-to-stage 2")
            return

        state["stage_1_complete"] = True
        state["needs_funding"] = False
        save_state()

    else:
        admin = Wallet.load("admin", keys_dir)
        test_wallets = [Wallet.load(f"test_{i:03d}", keys_dir) for i in range(num_test_wallets)]

    # ---------------------------------------------------------------
    # Stage 2: Mint test tokens
    # ---------------------------------------------------------------
    if skip_to_stage <= 2:
        logger.info("=" * 60)
        logger.info("STAGE 2: Mint AGENT_TEST + SHARDS_TEST")
        logger.info("=" * 60)

        agent_name_hex = "4167656e7454657374"  # "AgentTest"
        shards_name_hex = "53686172647354657374"  # "ShardsTest"

        agent_supply = 1_000_000_000  # 1B, 0 decimals
        shards_supply = 3_000_000_000_000  # 3M display × 10^6, 6 decimals

        # Both tokens use the same native script (admin signature)
        pub_key_hash = admin.vkey.hash()
        native_script = ScriptPubkey(pub_key_hash)
        policy_id = native_script.hash()

        agent_name = AssetName(bytes.fromhex(agent_name_hex))
        shards_name = AssetName(bytes.fromhex(shards_name_hex))

        logger.info(
            "Minting both tokens under policy %s in a single tx",
            policy_id.payload.hex(),
        )

        builder = TransactionBuilder(context)
        builder.add_input_address(admin.address)

        # Mint both in one tx
        builder.mint = MultiAsset({
            policy_id: Asset({
                agent_name: agent_supply,
                shards_name: shards_supply,
            })
        })
        builder.native_scripts = [native_script]

        # Output: send both tokens to admin
        multi = MultiAsset({
            policy_id: Asset({
                agent_name: agent_supply,
                shards_name: shards_supply,
            })
        })
        tx_out = TransactionOutput(admin.address, Value(3_000_000, multi))
        builder.add_output(tx_out)

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )
        mint_tx_hash = signed_tx.id.payload.hex()
        context.submit_tx(signed_tx)
        logger.info("Mint TX submitted: %s", mint_tx_hash)
        logger.info("Waiting for confirmation...")
        time.sleep(30)

        agent_policy_id = policy_id
        shards_policy_id = policy_id

        state["agent_test"] = {
            "policy_id": policy_id.payload.hex(),
            "asset_name_hex": agent_name_hex,
            "supply": agent_supply,
            "decimals": 0,
            "mint_tx": mint_tx_hash,
        }
        state["shards_test"] = {
            "policy_id": policy_id.payload.hex(),
            "asset_name_hex": shards_name_hex,
            "supply": shards_supply,
            "decimals": 6,
            "mint_tx": mint_tx_hash,
        }
        state["stage_2_complete"] = True
        save_state()

    else:
        agent_policy_id = ScriptHash(bytes.fromhex(state["agent_test"]["policy_id"]))
        shards_policy_id = ScriptHash(bytes.fromhex(state["shards_test"]["policy_id"]))
        agent_name_hex = state["agent_test"]["asset_name_hex"]
        shards_name_hex = state["shards_test"]["asset_name_hex"]
        agent_supply = state["agent_test"]["supply"]
        shards_supply = state["shards_test"]["supply"]

    # ---------------------------------------------------------------
    # Stage 3: Distribute tokens to test wallets
    # ---------------------------------------------------------------
    if skip_to_stage <= 3:
        logger.info("=" * 60)
        logger.info("STAGE 3: Distribute tokens to test wallets")
        logger.info("=" * 60)

        distributions = build_adversarial_distributions(num_test_wallets)

        dist_txs = distribute_test_tokens(
            blockfrost_project_id, admin, test_wallets,
            agent_policy_id, agent_name_hex,
            shards_policy_id, shards_name_hex,
            distributions,
        )

        state["distributions"] = [
            {"wallet": d.wallet_index, "agent": d.agent_amount,
             "shards": d.shards_amount, "desc": d.description}
            for d in distributions
        ]
        state["distribution_txs"] = dist_txs
        state["stage_3_complete"] = True
        save_state()

    else:
        distributions = [
            Distribution(d["wallet"], d["agent"], d["shards"], d["desc"])
            for d in state["distributions"]
        ]

    # ---------------------------------------------------------------
    # Stage 4: Generate synthetic allocation CSV
    # ---------------------------------------------------------------
    if skip_to_stage <= 4:
        logger.info("=" * 60)
        logger.info("STAGE 4: Generate synthetic allocation")
        logger.info("=" * 60)

        # Use same 85.5/14.5 split as mainnet
        agent_bucket = 855_271_753_084_314
        shards_bucket = FLUX_TEST_SUPPLY_BASE - agent_bucket

        alloc_csv = data_dir / "test_allocations.csv"
        summary = generate_synthetic_allocation(
            test_wallets, distributions,
            agent_bucket, shards_bucket,
            agent_supply, shards_supply,
            alloc_csv,
        )

        state["allocation"] = {
            "csv_path": str(alloc_csv),
            "agent_bucket": agent_bucket,
            "shards_bucket": shards_bucket,
            **summary,
        }
        state["stage_4_complete"] = True
        save_state()

    # ---------------------------------------------------------------
    # Stage 5: Mint FLUX_TEST
    # ---------------------------------------------------------------
    if skip_to_stage <= 5:
        logger.info("=" * 60)
        logger.info("STAGE 5: Mint FLUX_TEST with timelock")
        logger.info("=" * 60)

        # Fresh context after previous stages
        context = BlockFrostChainContext(project_id=blockfrost_project_id)
        current_slot = context.last_block_slot
        timelock_slot = current_slot + timelock_offset_slots
        logger.info("Current slot: %d, timelock: %d (offset +%d)", current_slot, timelock_slot, timelock_offset_slots)

        flux_policy_id, flux_tx = mint_flux_test(context, admin, timelock_slot)
        logger.info("Waiting for FLUX_TEST mint confirmation...")
        time.sleep(30)

        state["flux_test"] = {
            "policy_id": flux_policy_id.payload.hex(),
            "asset_name_hex": FLUX_ASSET_NAME_HEX,
            "supply": FLUX_TEST_SUPPLY_BASE,
            "timelock_slot": timelock_slot,
            "mint_tx": flux_tx,
        }
        state["stage_5_complete"] = True
        save_state()

    else:
        flux_policy_id = ScriptHash(bytes.fromhex(state["flux_test"]["policy_id"]))

    # ---------------------------------------------------------------
    # Stage 6: Deploy claim validator + build claim UTxOs
    # ---------------------------------------------------------------
    if skip_to_stage <= 6:
        logger.info("=" * 60)
        logger.info("STAGE 6: Deploy claim validator + build claim UTxOs")
        logger.info("=" * 60)

        script, script_hash, script_address = load_claim_validator(blueprint_path)

        alloc_csv = Path(state["allocation"]["csv_path"])
        batches = build_claim_utxos(
            blockfrost_project_id, admin, alloc_csv,
            script_address, flux_policy_id,
            batch_size=20,
        )
        logger.info("Waiting for claim UTxO confirmations...")
        time.sleep(30)

        state["claim_validator"] = {
            "script_hash": script_hash.payload.hex(),
            "script_address": script_address,
        }
        state["claim_batches"] = batches
        state["stage_6_complete"] = True
        save_state()

    else:
        script, script_hash, script_address = load_claim_validator(blueprint_path)

    # ---------------------------------------------------------------
    # Stage 7: Build claim index
    # ---------------------------------------------------------------
    if skip_to_stage <= 7:
        logger.info("=" * 60)
        logger.info("STAGE 7: Build claim index")
        logger.info("=" * 60)

        index = build_claim_index_from_batches(
            state["claim_batches"],
            state["claim_validator"]["script_address"],
            state.get("flux_test", {}).get("policy_id", ""),
        )

        index_path = data_dir / "claim_index.json"
        with open(index_path, "w") as f:
            json.dump(index, f, indent=2)

        state["claim_index_path"] = str(index_path)
        state["claim_index_size"] = len(index)
        state["stage_7_complete"] = True
        save_state()

    else:
        with open(state["claim_index_path"]) as f:
            index = json.load(f)

    # ---------------------------------------------------------------
    # Stage 8: Happy-path claims
    # ---------------------------------------------------------------
    if skip_to_stage <= 8:
        logger.info("=" * 60)
        logger.info("STAGE 8: Happy-path claims")
        logger.info("=" * 60)

        claim_results = []
        # Test with first 5 wallets that have claims
        claimed_count = 0
        for w in test_wallets:
            if w.pkh_hex in index and claimed_count < 5:
                refs = index[w.pkh_hex]
                try:
                    # Fresh context per claim to avoid stale UTxOs
                    claim_ctx = BlockFrostChainContext(project_id=blockfrost_project_id)
                    tx_hash = claim_flux(
                        claim_ctx, w, refs,
                        script_address, script, flux_policy_id,
                    )
                    claim_results.append({
                        "wallet": w.name,
                        "pkh": w.pkh_hex,
                        "tx_hash": tx_hash,
                        "status": "success",
                    })
                    logger.info("Claim OK: %s → %s", w.name, tx_hash)
                    claimed_count += 1
                    time.sleep(20)  # Wait between claims
                except Exception as e:
                    claim_results.append({
                        "wallet": w.name,
                        "pkh": w.pkh_hex,
                        "status": "error",
                        "error": str(e)[:300],
                    })
                    logger.warning("Claim FAILED for %s: %s", w.name, str(e)[:200])

        state["claim_results"] = claim_results
        state["stage_8_complete"] = True
        save_state()

    # ---------------------------------------------------------------
    # Stage 9: Red-team tests
    # ---------------------------------------------------------------
    if skip_to_stage <= 9:
        logger.info("=" * 60)
        logger.info("STAGE 9: Red-team adversarial tests")
        logger.info("=" * 60)

        red_team_results = []

        # Find an unclaimed wallet for testing
        unclaimed_wallets = [
            w for w in test_wallets
            if w.pkh_hex in index and w.name not in [
                r.get("wallet") for r in state.get("claim_results", [])
                if r.get("status") == "success"
            ]
        ]

        if len(unclaimed_wallets) >= 2:
            victim = unclaimed_wallets[0]
            attacker = unclaimed_wallets[1]

            # Fresh context for red-team tests
            rt_ctx = BlockFrostChainContext(project_id=blockfrost_project_id)

            # Test 1: Wrong signer
            logger.info("RED TEAM TEST 1: Wrong signer claim")
            victim_refs = index.get(victim.pkh_hex, [])
            if victim_refs:
                passed = red_team_wrong_signer(
                    rt_ctx, attacker, victim,
                    victim_refs, script_address, script, flux_policy_id,
                )
                red_team_results.append({
                    "test": "wrong_signer",
                    "passed": passed,
                })

            # Test 2: Double claim (claim then try again)
            logger.info("RED TEAM TEST 2: Double claim")
            attacker_refs = index.get(attacker.pkh_hex, [])
            if attacker_refs:
                # Fresh context after test 1 may have changed state
                rt_ctx = BlockFrostChainContext(project_id=blockfrost_project_id)
                passed = red_team_double_claim(
                    rt_ctx, attacker, attacker_refs,
                    script_address, script, flux_policy_id,
                )
                red_team_results.append({
                    "test": "double_claim",
                    "passed": passed,
                })
        else:
            logger.warning("Not enough unclaimed wallets for red-team tests")

        state["red_team_results"] = red_team_results
        state["stage_9_complete"] = True
        save_state()

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PREPROD REHEARSAL COMPLETE")
    logger.info("=" * 60)
    logger.info("State saved to: %s", state_file)
    logger.info("")
    logger.info("Results:")
    for key in ["stage_1_complete", "stage_2_complete", "stage_3_complete",
                 "stage_4_complete", "stage_5_complete", "stage_6_complete",
                 "stage_7_complete", "stage_8_complete", "stage_9_complete"]:
        status = "OK" if state.get(key) else "SKIP"
        logger.info("  %s: %s", key, status)

    if state.get("red_team_results"):
        logger.info("")
        logger.info("Red-team results:")
        for r in state["red_team_results"]:
            status = "PASS" if r["passed"] else "FAIL"
            logger.info("  %s: %s", r["test"], status)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX Merger — Preprod Rehearsal Harness",
    )
    parser.add_argument(
        "--work-dir", type=str, default=None,
        help="Working directory for keys and data (default: audit_pack/preprod)",
    )
    parser.add_argument(
        "--blueprint", type=str, default=None,
        help="Path to Aiken plutus.json blueprint",
    )
    parser.add_argument(
        "--num-wallets", type=int, default=20,
        help="Number of test wallets to generate",
    )
    parser.add_argument(
        "--timelock-offset", type=int, default=7200,
        help="Timelock offset in slots from current (~2 hours default)",
    )
    parser.add_argument(
        "--skip-to-stage", type=int, default=1,
        help="Resume from a specific stage (1-9)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Resolve paths
    project_root = Path(__file__).resolve().parent.parent
    work_dir = Path(args.work_dir) if args.work_dir else project_root / "audit_pack" / "preprod"
    blueprint_path = Path(args.blueprint) if args.blueprint else (
        project_root / "onchain" / "claim_validator" / "plutus.json"
    )

    if not blueprint_path.exists():
        logger.error("Blueprint not found: %s", blueprint_path)
        logger.error("Run 'aiken build' in onchain/claim_validator/ first")
        sys.exit(1)

    # Get Blockfrost preprod project ID
    bf_id = os.environ.get(
        "BLOCKFROST_PROJECT_ID_PREPROD",
        os.environ.get("BLOCKFROST_PROJECT_ID", ""),
    )
    if not bf_id or not bf_id.startswith("preprod"):
        logger.error("Need a preprod Blockfrost project ID.")
        logger.error("Set BLOCKFROST_PROJECT_ID_PREPROD in env.local")
        logger.error("Get one free at: https://blockfrost.io/")
        sys.exit(1)

    run_preprod_rehearsal(
        work_dir=work_dir,
        blockfrost_project_id=bf_id,
        blueprint_path=blueprint_path,
        num_test_wallets=args.num_wallets,
        timelock_offset_slots=args.timelock_offset,
        skip_to_stage=args.skip_to_stage,
    )


if __name__ == "__main__":
    main()
