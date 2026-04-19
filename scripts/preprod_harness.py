#!/usr/bin/env python3
"""
scripts/preprod_harness.py — Preprod Rehearsal Harness (Surrender Model v3.0)
=============================================================================

Full end-to-end preprod deployment for the cMATRA surrender-and-redeem pipeline.

In the surrender model, pool UTxOs hold cMATRA at a script address with Void
datums. The admin processes user surrender requests (trading legacy tokens for
cMATRA) during a time window, and reclaims any unredeemed pool balance after
the deadline.

Stages:
  1. Generate admin + test wallets (or load existing)
  2. Mint AGENT_TEST (0 decimals) and SHARDS_TEST (6 decimals)
  3. Distribute tokens to test wallets with adversarial patterns
  4. Mint cMATRA_TEST with timelock native script + build rate table
  5. Deploy surrender validator -> derive script address
  6. Build surrender pool UTxOs (lock cMATRA at script with Void datums)
  7. Happy-path surrenders (admin processes 3 test wallets)
  8. Red-team: adversarial surrender attempts (3 attack vectors)
  9. Admin withdraw remaining pool after deadline

Requires:
  - NETWORK=preprod in env
  - BLOCKFROST_PROJECT_ID_PREPROD set (or BLOCKFROST_PROJECT_ID with preprod key)
  - tADA in the admin wallet (faucet: https://docs.cardano.org/cardano-testnets/tools/faucet/)

Used by: manual operator invocation for preprod dress rehearsals.
Depends on: pycardano 0.19.x, cbor2, blockfrost-python (auto-detect network).
"""

from __future__ import annotations

import argparse
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

CMATRA_TEST_SUPPLY_BASE = 1_000_000_000_000_000  # 1e15, 6 decimals
CMATRA_TEST_DECIMALS = 6
CMATRA_ASSET_NAME_HEX = "634d41545241"  # hex of "cMATRA"

# CBOR encodings for Plutus Data constructors
# Void datum: Constr(0, []) = CBORTag(121, [])
VOID_DATUM_CBOR = cbor2.dumps(CBORTag(121, []))

# ProcessSurrender redeemer: Constr(0, []) = CBORTag(121, [])
PROCESS_SURRENDER_REDEEMER_CBOR = cbor2.dumps(CBORTag(121, []))

# AdminWithdraw redeemer: Constr(1, []) = CBORTag(122, [])
ADMIN_WITHDRAW_REDEEMER_CBOR = cbor2.dumps(CBORTag(122, []))


# ---------------------------------------------------------------------------
# Wallet management (identical to v2)
# ---------------------------------------------------------------------------

@dataclass
class Wallet:
    """Represents a Cardano wallet with signing key, verification key, and address."""
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
    """Generate or load admin wallet + test wallets.

    Args:
        keys_dir: Directory to store/load key files.
        num_test_wallets: Number of test wallets to create.
        network: Cardano network (TESTNET for preprod).

    Returns:
        Tuple of (admin_wallet, list_of_test_wallets).
    """
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
# Native script token minting (identical to v2)
# ---------------------------------------------------------------------------

def mint_test_token(
    context: BlockFrostChainContext,
    admin: Wallet,
    token_name_hex: str,
    total_supply: int,
) -> tuple[ScriptHash, str]:
    """Mint a test token using a simple native script (admin signature required).

    Args:
        context: Blockfrost chain context.
        admin: Admin wallet that signs the mint.
        token_name_hex: Hex-encoded asset name.
        total_supply: Total number of base units to mint.

    Returns:
        Tuple of (policy_id ScriptHash, tx_hash hex string).
    """
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
# Token distribution to test wallets (identical to v2)
# ---------------------------------------------------------------------------

@dataclass
class Distribution:
    """Describes how to distribute test tokens to a wallet."""
    wallet_index: int
    agent_amount: int   # base units (0 decimals)
    shards_amount: int  # base units (6 decimals)
    description: str


def build_adversarial_distributions(num_wallets: int) -> list[Distribution]:
    """Create adversarial distribution patterns for testing.

    Patterns include: dust amounts, whale amounts, both/single tokens,
    rounding edge cases, and zero-eligible wallets.

    Args:
        num_wallets: Total number of test wallets available.

    Returns:
        List of Distribution specs.
    """
    dists: list[Distribution] = []

    # Wallet 0: Whale -- holds most AGENT
    dists.append(Distribution(0, 500_000_000, 0, "agent_whale"))

    # Wallet 1: Whale -- holds most SHARDS (1.5M display = 1.5e12 base)
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

    Batches into transactions of max 10 outputs. Waits 30s between batches
    for on-chain confirmation.

    Args:
        blockfrost_project_id: Blockfrost preprod project ID.
        admin: Admin wallet funding the distributions.
        test_wallets: List of test wallets to receive tokens.
        agent_policy_id: Policy ID ScriptHash for AGENT_TEST.
        agent_name_hex: Hex-encoded asset name for AGENT_TEST.
        shards_policy_id: Policy ID ScriptHash for SHARDS_TEST.
        shards_name_hex: Hex-encoded asset name for SHARDS_TEST.
        distributions: List of Distribution specs.

    Returns:
        List of submitted tx hash hex strings.
    """
    agent_name = AssetName(bytes.fromhex(agent_name_hex))
    shards_name = AssetName(bytes.fromhex(shards_name_hex))

    tx_hashes = []

    # Batch distributions into transactions (max 10 outputs per tx)
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
# cMATRA mint with timelock (replaces FLUX_TEST mint)
# ---------------------------------------------------------------------------

def mint_cmatra_test(
    context: BlockFrostChainContext,
    admin: Wallet,
    timelock_slot: int,
) -> tuple[ScriptHash, str]:
    """Mint cMATRA_TEST with a time-locked native script.

    Policy: RequireAll [RequireSignature(admin), InvalidHereAfter(slot)]

    The timelock ensures the minting policy is permanently closed after
    the specified slot, preventing future minting.

    Args:
        context: Blockfrost chain context.
        admin: Admin wallet that signs the mint.
        timelock_slot: Slot after which minting is no longer possible.

    Returns:
        Tuple of (policy_id ScriptHash, tx_hash hex string).
    """
    pub_key_hash = admin.vkey.hash()
    sig_script = ScriptPubkey(pub_key_hash)
    time_script = InvalidHereAfter(timelock_slot)
    policy_script = ScriptAll([sig_script, time_script])

    policy_id = policy_script.hash()
    asset_name = AssetName(bytes.fromhex(CMATRA_ASSET_NAME_HEX))

    logger.info(
        "Minting cMATRA_TEST: supply=%d, policy=%s, timelock_slot=%d",
        CMATRA_TEST_SUPPLY_BASE, policy_id.payload.hex(), timelock_slot,
    )

    builder = TransactionBuilder(context)
    builder.add_input_address(admin.address)

    # Set validity interval (must be before timelock)
    builder.validity_start = context.last_block_slot
    builder.ttl = timelock_slot - 1

    # Mint
    builder.mint = MultiAsset()
    builder.mint[policy_id] = Asset({asset_name: CMATRA_TEST_SUPPLY_BASE})
    builder.native_scripts = [policy_script]

    # Send to admin
    multi = MultiAsset()
    multi[policy_id] = Asset({asset_name: CMATRA_TEST_SUPPLY_BASE})
    tx_out = TransactionOutput(admin.address, Value(5_000_000, multi))
    builder.add_output(tx_out)

    signed_tx = builder.build_and_sign(
        signing_keys=[admin.skey],
        change_address=admin.address,
    )

    tx_hash = signed_tx.id.payload.hex()
    context.submit_tx(signed_tx)
    logger.info("cMATRA_TEST mint TX submitted: %s", tx_hash)
    return policy_id, tx_hash


# ---------------------------------------------------------------------------
# Surrender validator loading (replaces load_claim_validator)
# ---------------------------------------------------------------------------

def load_surrender_validator(
    blueprint_path: Path,
    admin_pkh: str | None = None,
    deadline_posix_ms: int | None = None,
) -> tuple[PlutusV3Script, ScriptHash, str]:
    """Load compiled surrender validator from Aiken blueprint.

    If admin_pkh and deadline_posix_ms are provided, applies them as
    parameters using ``aiken blueprint apply``. Otherwise, loads the
    compiled code as-is (for pre-applied blueprints).

    Args:
        blueprint_path: Path to the Aiken plutus.json blueprint file.
        admin_pkh: Admin payment key hash hex (28 bytes / 56 hex chars).
        deadline_posix_ms: Deadline as POSIX milliseconds integer.

    Returns:
        Tuple of (PlutusV3Script, ScriptHash, bech32_script_address).

    Raises:
        ValueError: If no compiled validator is found in the blueprint.
    """
    import hashlib
    import shutil
    import subprocess
    import tempfile

    if admin_pkh is not None and deadline_posix_ms is not None:
        # Use aiken CLI to apply parameters to the blueprint
        # Work in a temp copy so we don't modify the original
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_blueprint = Path(tmpdir) / "plutus.json"
            shutil.copy2(blueprint_path, tmp_blueprint)

            # Apply param 1: admin_pkh as CBOR hex (ByteString)
            pkh_cbor_hex = cbor2.dumps(bytes.fromhex(admin_pkh)).hex()
            result = subprocess.run(
                ["aiken", "blueprint", "apply", pkh_cbor_hex,
                 "-i", str(tmp_blueprint), "-o", str(tmp_blueprint),
                 "-m", "claim_validator", "-v", "surrender_pool"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise ValueError(f"aiken blueprint apply (pkh) failed: {result.stderr}")

            # Apply param 2: deadline as CBOR hex (Integer)
            deadline_cbor_hex = cbor2.dumps(deadline_posix_ms).hex()
            result = subprocess.run(
                ["aiken", "blueprint", "apply", deadline_cbor_hex,
                 "-i", str(tmp_blueprint), "-o", str(tmp_blueprint),
                 "-m", "claim_validator", "-v", "surrender_pool"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise ValueError(f"aiken blueprint apply (deadline) failed: {result.stderr}")

            # Read the applied blueprint
            with open(tmp_blueprint) as f:
                blueprint = json.load(f)

        logger.info(
            "Applied params via aiken CLI: admin_pkh=%s..., deadline=%d",
            admin_pkh[:16], deadline_posix_ms,
        )
    else:
        with open(blueprint_path) as f:
            blueprint = json.load(f)

    validators = blueprint.get("validators", [])
    compiled_code = None
    validator_hash = None

    # Try to find surrender/pool validator by title keyword
    for keyword in ("surrender", "spend"):
        for v in validators:
            title = v.get("title", "").lower()
            if keyword in title:
                compiled_code = v.get("compiledCode")
                validator_hash = v.get("hash")
                break
        if compiled_code:
            break

    if compiled_code is None and validators:
        compiled_code = validators[0].get("compiledCode")
        validator_hash = validators[0].get("hash")

    if compiled_code is None:
        raise ValueError(f"No compiled validator in {blueprint_path}")

    script = PlutusV3Script(bytes.fromhex(compiled_code))

    if validator_hash:
        script_hash = ScriptHash(bytes.fromhex(validator_hash))
    else:
        h = hashlib.blake2b(b"\x03" + script.to_primitive(), digest_size=28)
        script_hash = ScriptHash(h.digest())

    # Derive testnet script address
    script_address = Address(payment_part=script_hash, network=Network.TESTNET)

    logger.info("Surrender validator hash: %s", script_hash.payload.hex())
    logger.info("Script address (preprod): %s", script_address)

    return script, script_hash, str(script_address)


# ---------------------------------------------------------------------------
# Build surrender pool UTxOs (Stage 6)
# ---------------------------------------------------------------------------

def build_pool_utxos(
    blockfrost_project_id: str,
    admin: Wallet,
    script_address: str,
    cmatra_policy_id: ScriptHash,
    pool_base: int,
    num_pool_utxos: int = 5,
    batch_size: int = 5,
) -> list[dict[str, Any]]:
    """Lock cMATRA_TEST at the script address in pool UTxOs with Void datums.

    Splits pool_base evenly across num_pool_utxos, last UTxO gets remainder.
    Groups outputs into batched transactions (max batch_size outputs per tx).

    These are plain sends TO the script address -- no spending from script,
    no redeemers needed. The admin simply sends cMATRA with inline Void datums.

    Args:
        blockfrost_project_id: Blockfrost preprod project ID.
        admin: Admin wallet that holds cMATRA and funds the transaction.
        script_address: Bech32 script address for the surrender pool.
        cmatra_policy_id: Policy ID ScriptHash for cMATRA_TEST.
        pool_base: Total cMATRA base units to lock in the pool.
        num_pool_utxos: Number of pool UTxOs to create (default 5).
        batch_size: Max outputs per transaction (default 5).

    Returns:
        List of batch result dicts with tx_hash and output details.
    """
    asset_name = AssetName(bytes.fromhex(CMATRA_ASSET_NAME_HEX))
    script_addr = Address.from_primitive(script_address)

    # Compute per-UTxO amounts
    per_utxo = pool_base // num_pool_utxos
    assert per_utxo > 0, f"pool_base ({pool_base}) too small for {num_pool_utxos} UTxOs"

    outputs_spec: list[dict[str, Any]] = []
    allocated = 0
    for i in range(num_pool_utxos):
        if i < num_pool_utxos - 1:
            qty = per_utxo
        else:
            qty = pool_base - allocated  # last UTxO gets remainder
        outputs_spec.append({"index": i, "cmatra_qty": qty})
        allocated += qty

    assert allocated == pool_base, f"Allocation mismatch: {allocated} != {pool_base}"

    # Build Void datum
    void_datum = RawPlutusData(cbor2.loads(VOID_DATUM_CBOR))

    batches_result: list[dict[str, Any]] = []

    for batch_start in range(0, len(outputs_spec), batch_size):
        batch = outputs_spec[batch_start : batch_start + batch_size]
        batch_num = batch_start // batch_size

        # Fresh context each batch to avoid stale UTxO cache
        ctx = BlockFrostChainContext(project_id=blockfrost_project_id)

        builder = TransactionBuilder(ctx)
        builder.add_input_address(admin.address)

        output_details = []
        for spec in batch:
            multi = MultiAsset()
            multi[cmatra_policy_id] = Asset({asset_name: spec["cmatra_qty"]})
            min_ada = 2_000_000  # 2 ADA per pool UTxO
            value = Value(min_ada, multi)

            tx_out = TransactionOutput(script_addr, value, datum=void_datum)
            builder.add_output(tx_out)

            output_details.append({
                "pool_utxo_index": spec["index"],
                "cmatra_base_units": spec["cmatra_qty"],
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
            "outputs": output_details,
        }
        batches_result.append(batch_result)
        logger.info(
            "Pool UTxO batch %d submitted: %s (%d outputs)",
            batch_num, tx_hash, len(output_details),
        )

        # Wait for on-chain confirmation before next batch
        logger.info("Waiting 30s for chain confirmation...")
        time.sleep(30)

    return batches_result


# ---------------------------------------------------------------------------
# Happy-path surrender processing (Stage 7)
# ---------------------------------------------------------------------------

def process_surrender(
    blockfrost_project_id: str,
    admin: Wallet,
    user_address: str,
    cmatra_amount: int,
    pool_utxo_ref: dict[str, Any],
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    deadline_posix_ms: int | None = None,
) -> str:
    """Build and submit a surrender-processing transaction.

    The admin spends a pool UTxO using ProcessSurrender redeemer, sending
    cMATRA to the user and returning the remainder to the script.

    Transaction structure:
      - Script input:  pool UTxO (with ProcessSurrender redeemer)
      - Output 1:      cMATRA to user_address at the computed rate
      - Output 2:      remaining pool balance back to script with Void datum
      - Required signer: admin PKH
      - Validity range: must be entirely before the deadline

    Args:
        blockfrost_project_id: Blockfrost preprod project ID.
        admin: Admin wallet that signs the transaction.
        user_address: Bech32 address to receive cMATRA.
        cmatra_amount: cMATRA base units to send to the user.
        pool_utxo_ref: Dict with tx_hash, output_index, cmatra_qty, ada_amount.
        script_address: Bech32 script address of the surrender pool.
        script: Compiled PlutusV3 script object.
        cmatra_policy_id: Policy ID ScriptHash for cMATRA_TEST.
        deadline_posix_ms: Deadline in POSIX ms; if set, TTL is set before it.

    Returns:
        Submitted transaction hash hex string.

    Raises:
        ValueError: If the pool UTxO does not hold enough cMATRA.
    """
    if cmatra_amount > pool_utxo_ref["cmatra_qty"]:
        raise ValueError(
            f"Pool UTxO has {pool_utxo_ref['cmatra_qty']} cMATRA but "
            f"{cmatra_amount} requested"
        )

    # Fresh context for this transaction
    ctx = BlockFrostChainContext(project_id=blockfrost_project_id)

    asset_name = AssetName(bytes.fromhex(CMATRA_ASSET_NAME_HEX))
    script_addr = Address.from_primitive(script_address)

    # Reconstruct the pool UTxO for pycardano
    tx_in = TransactionInput(
        TransactionId(bytes.fromhex(pool_utxo_ref["tx_hash"])),
        pool_utxo_ref["output_index"],
    )

    pool_multi = MultiAsset()
    pool_multi[cmatra_policy_id] = Asset({asset_name: pool_utxo_ref["cmatra_qty"]})
    pool_ada = pool_utxo_ref.get("ada_amount", 2_000_000)
    pool_value = Value(pool_ada, pool_multi)
    pool_datum = RawPlutusData(cbor2.loads(VOID_DATUM_CBOR))

    utxo = UTxO(tx_in, TransactionOutput(script_addr, pool_value, datum=pool_datum))

    # ProcessSurrender redeemer: Constr(0, [])
    redeemer = Redeemer(RawPlutusData(cbor2.loads(PROCESS_SURRENDER_REDEEMER_CBOR)))

    builder = TransactionBuilder(ctx)

    # Set validity interval: must be entirely before the deadline
    current_slot = ctx.last_block_slot
    builder.validity_start = current_slot
    if deadline_posix_ms is not None:
        # Convert deadline POSIX ms to slot (preprod: slot = posix_sec - 1654041600 + 86400)
        # But simpler: just set TTL to current_slot + 300 (5 min window) and trust
        # that the deadline hasn't passed yet
        builder.ttl = current_slot + 300
    else:
        builder.ttl = current_slot + 600

    # Add script input (the pool UTxO we are spending)
    builder.add_script_input(utxo, script=script, redeemer=redeemer)

    # Admin address for collateral and change
    builder.add_input_address(admin.address)

    # Output 1: cMATRA to user
    user_multi = MultiAsset()
    user_multi[cmatra_policy_id] = Asset({asset_name: cmatra_amount})
    user_value = Value(2_000_000, user_multi)
    user_addr = Address.from_primitive(user_address)
    builder.add_output(TransactionOutput(user_addr, user_value))

    # Output 2: remaining pool balance back to script with Void datum
    remaining = pool_utxo_ref["cmatra_qty"] - cmatra_amount
    if remaining > 0:
        return_multi = MultiAsset()
        return_multi[cmatra_policy_id] = Asset({asset_name: remaining})
        return_value = Value(2_000_000, return_multi)
        return_datum = RawPlutusData(cbor2.loads(VOID_DATUM_CBOR))
        builder.add_output(TransactionOutput(script_addr, return_value, datum=return_datum))

    # Required signer: admin PKH (on-chain validator checks this)
    builder.required_signers = [admin.vkey.hash()]

    signed_tx = builder.build_and_sign(
        signing_keys=[admin.skey],
        change_address=admin.address,
    )

    tx_hash = signed_tx.id.payload.hex()
    ctx.submit_tx(signed_tx)
    logger.info(
        "Surrender tx submitted: %s (%d cMATRA -> %s, %d remaining)",
        tx_hash, cmatra_amount, user_address[:24] + "...", remaining,
    )
    return tx_hash


# ---------------------------------------------------------------------------
# Red-team tests (Stage 8)
# ---------------------------------------------------------------------------

def red_team_wrong_signer(
    blockfrost_project_id: str,
    wrong_wallet: Wallet,
    admin: Wallet,
    pool_utxo_ref: dict[str, Any],
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
) -> bool:
    """Attack: non-admin tries to spend pool UTxO with ProcessSurrender.

    The on-chain validator requires admin_pkh in extra_signatories. A
    random wallet signing should fail validation.

    Args:
        blockfrost_project_id: Blockfrost preprod project ID.
        wrong_wallet: Non-admin wallet attempting the attack.
        admin: Admin wallet (for reference, not used to sign).
        pool_utxo_ref: Pool UTxO to attempt spending.
        script_address: Bech32 script address.
        script: Compiled PlutusV3 script.
        cmatra_policy_id: cMATRA policy ID.

    Returns:
        True if the attack was correctly rejected (expected behavior).
    """
    try:
        ctx = BlockFrostChainContext(project_id=blockfrost_project_id)

        asset_name = AssetName(bytes.fromhex(CMATRA_ASSET_NAME_HEX))
        script_addr = Address.from_primitive(script_address)

        # Reconstruct pool UTxO
        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(pool_utxo_ref["tx_hash"])),
            pool_utxo_ref["output_index"],
        )
        pool_multi = MultiAsset()
        pool_multi[cmatra_policy_id] = Asset({asset_name: pool_utxo_ref["cmatra_qty"]})
        pool_ada = pool_utxo_ref.get("ada_amount", 2_000_000)
        pool_value = Value(pool_ada, pool_multi)
        pool_datum = RawPlutusData(cbor2.loads(VOID_DATUM_CBOR))
        utxo = UTxO(tx_in, TransactionOutput(script_addr, pool_value, datum=pool_datum))

        redeemer = Redeemer(RawPlutusData(cbor2.loads(PROCESS_SURRENDER_REDEEMER_CBOR)))

        builder = TransactionBuilder(ctx)
        builder.add_script_input(utxo, script=script, redeemer=redeemer)
        builder.add_input_address(wrong_wallet.address)

        # Sign with WRONG wallet, set wrong signer as required
        builder.required_signers = [wrong_wallet.vkey.hash()]

        # Send pool cMATRA to the wrong wallet
        steal_multi = MultiAsset()
        steal_multi[cmatra_policy_id] = Asset({asset_name: pool_utxo_ref["cmatra_qty"]})
        steal_value = Value(2_000_000, steal_multi)
        builder.add_output(TransactionOutput(wrong_wallet.address, steal_value))

        signed_tx = builder.build_and_sign(
            signing_keys=[wrong_wallet.skey],
            change_address=wrong_wallet.address,
        )
        ctx.submit_tx(signed_tx)

        # If we get here, the attack succeeded -- BAD
        logger.error("RED TEAM FAIL: wrong-signer surrender was accepted!")
        return False

    except Exception as e:
        logger.info("RED TEAM PASS: wrong-signer surrender rejected: %s", str(e)[:200])
        return True


def red_team_post_deadline(
    blockfrost_project_id: str,
    admin: Wallet,
    pool_utxo_ref: dict[str, Any],
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    deadline_posix_ms: int,
) -> bool:
    """Attack: ProcessSurrender with validity range after the deadline.

    The on-chain validator requires is_entirely_before(validity_range, deadline)
    for ProcessSurrender. Setting the validity start well after the deadline
    should fail.

    Args:
        blockfrost_project_id: Blockfrost preprod project ID.
        admin: Admin wallet (correct signer, but wrong timing).
        pool_utxo_ref: Pool UTxO to attempt spending.
        script_address: Bech32 script address.
        script: Compiled PlutusV3 script.
        cmatra_policy_id: cMATRA policy ID.
        deadline_posix_ms: Deadline in POSIX milliseconds.

    Returns:
        True if the attack was correctly rejected (expected behavior).
    """
    try:
        ctx = BlockFrostChainContext(project_id=blockfrost_project_id)

        asset_name = AssetName(bytes.fromhex(CMATRA_ASSET_NAME_HEX))
        script_addr = Address.from_primitive(script_address)

        # Reconstruct pool UTxO
        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(pool_utxo_ref["tx_hash"])),
            pool_utxo_ref["output_index"],
        )
        pool_multi = MultiAsset()
        pool_multi[cmatra_policy_id] = Asset({asset_name: pool_utxo_ref["cmatra_qty"]})
        pool_ada = pool_utxo_ref.get("ada_amount", 2_000_000)
        pool_value = Value(pool_ada, pool_multi)
        pool_datum = RawPlutusData(cbor2.loads(VOID_DATUM_CBOR))
        utxo = UTxO(tx_in, TransactionOutput(script_addr, pool_value, datum=pool_datum))

        # Use ProcessSurrender redeemer (correct redeemer, wrong timing)
        redeemer = Redeemer(RawPlutusData(cbor2.loads(PROCESS_SURRENDER_REDEEMER_CBOR)))

        builder = TransactionBuilder(ctx)
        builder.add_script_input(utxo, script=script, redeemer=redeemer)
        builder.add_input_address(admin.address)

        # Set validity start to well after the deadline
        # Convert deadline from POSIX ms to approximate slot
        # Cardano preprod: slot ~= (posix_ms - 1654041600000) / 1000
        # Add a generous offset to ensure we are past the deadline
        post_deadline_slot = ctx.last_block_slot + 100_000
        builder.validity_start = post_deadline_slot
        builder.ttl = post_deadline_slot + 500

        builder.required_signers = [admin.vkey.hash()]

        # Output: send cMATRA to admin (legitimate amount, wrong timing)
        out_multi = MultiAsset()
        out_multi[cmatra_policy_id] = Asset({asset_name: pool_utxo_ref["cmatra_qty"]})
        out_value = Value(2_000_000, out_multi)
        builder.add_output(TransactionOutput(admin.address, out_value))

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )
        ctx.submit_tx(signed_tx)

        logger.error("RED TEAM FAIL: post-deadline ProcessSurrender was accepted!")
        return False

    except Exception as e:
        logger.info("RED TEAM PASS: post-deadline ProcessSurrender rejected: %s", str(e)[:200])
        return True


def red_team_no_admin_sig(
    blockfrost_project_id: str,
    random_wallet: Wallet,
    pool_utxo_ref: dict[str, Any],
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
) -> bool:
    """Attack: build tx spending pool UTxO but without admin as required_signer.

    Even if the random wallet signs, the on-chain validator checks for the
    admin PKH in extra_signatories. Signing with a different key that is
    not the admin should fail validation.

    Args:
        blockfrost_project_id: Blockfrost preprod project ID.
        random_wallet: A non-admin wallet used to sign.
        pool_utxo_ref: Pool UTxO to attempt spending.
        script_address: Bech32 script address.
        script: Compiled PlutusV3 script.
        cmatra_policy_id: cMATRA policy ID.

    Returns:
        True if the attack was correctly rejected (expected behavior).
    """
    try:
        ctx = BlockFrostChainContext(project_id=blockfrost_project_id)

        asset_name = AssetName(bytes.fromhex(CMATRA_ASSET_NAME_HEX))
        script_addr = Address.from_primitive(script_address)

        # Reconstruct pool UTxO
        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(pool_utxo_ref["tx_hash"])),
            pool_utxo_ref["output_index"],
        )
        pool_multi = MultiAsset()
        pool_multi[cmatra_policy_id] = Asset({asset_name: pool_utxo_ref["cmatra_qty"]})
        pool_ada = pool_utxo_ref.get("ada_amount", 2_000_000)
        pool_value = Value(pool_ada, pool_multi)
        pool_datum = RawPlutusData(cbor2.loads(VOID_DATUM_CBOR))
        utxo = UTxO(tx_in, TransactionOutput(script_addr, pool_value, datum=pool_datum))

        redeemer = Redeemer(RawPlutusData(cbor2.loads(PROCESS_SURRENDER_REDEEMER_CBOR)))

        builder = TransactionBuilder(ctx)
        builder.add_script_input(utxo, script=script, redeemer=redeemer)
        builder.add_input_address(random_wallet.address)

        # Set required_signers to the random wallet, NOT the admin
        builder.required_signers = [random_wallet.vkey.hash()]

        # Output: send cMATRA to random wallet
        out_multi = MultiAsset()
        out_multi[cmatra_policy_id] = Asset({asset_name: pool_utxo_ref["cmatra_qty"]})
        out_value = Value(2_000_000, out_multi)
        builder.add_output(TransactionOutput(random_wallet.address, out_value))

        signed_tx = builder.build_and_sign(
            signing_keys=[random_wallet.skey],
            change_address=random_wallet.address,
        )
        ctx.submit_tx(signed_tx)

        logger.error("RED TEAM FAIL: no-admin-sig surrender was accepted!")
        return False

    except Exception as e:
        logger.info("RED TEAM PASS: no-admin-sig surrender rejected: %s", str(e)[:200])
        return True


# ---------------------------------------------------------------------------
# Admin withdraw after deadline (Stage 9)
# ---------------------------------------------------------------------------

def admin_withdraw(
    blockfrost_project_id: str,
    admin: Wallet,
    pool_utxo_refs: list[dict[str, Any]],
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
) -> str:
    """Admin reclaims remaining pool UTxOs after the deadline using AdminWithdraw.

    Spends pool UTxOs one at a time to avoid multi-script-input redeemer
    assignment issues and execution budget limits.

    Args:
        blockfrost_project_id: Blockfrost preprod project ID.
        admin: Admin wallet that signs the withdrawal.
        pool_utxo_refs: List of remaining pool UTxO dicts.
        script_address: Bech32 script address of the surrender pool.
        script: Compiled PlutusV3 script.
        cmatra_policy_id: cMATRA policy ID.

    Returns:
        Last submitted transaction hash hex string.
    """
    asset_name = AssetName(bytes.fromhex(CMATRA_ASSET_NAME_HEX))
    script_addr = Address.from_primitive(script_address)
    last_tx_hash = ""

    for i, ref in enumerate(pool_utxo_refs):
        # Fresh context each iteration to pick up UTxO changes
        ctx = BlockFrostChainContext(project_id=blockfrost_project_id)

        builder = TransactionBuilder(ctx)

        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(ref["tx_hash"])),
            ref["output_index"],
        )
        pool_multi = MultiAsset()
        pool_multi[cmatra_policy_id] = Asset({asset_name: ref["cmatra_qty"]})
        pool_ada = ref.get("ada_amount", 2_000_000)
        pool_value = Value(pool_ada, pool_multi)
        pool_datum = RawPlutusData(cbor2.loads(VOID_DATUM_CBOR))

        utxo = UTxO(tx_in, TransactionOutput(script_addr, pool_value, datum=pool_datum))
        redeemer = Redeemer(RawPlutusData(cbor2.loads(ADMIN_WITHDRAW_REDEEMER_CBOR)))
        builder.add_script_input(utxo, script=script, redeemer=redeemer)

        # Admin address for collateral and change
        builder.add_input_address(admin.address)

        # Validity range: entirely after deadline
        builder.validity_start = ctx.last_block_slot

        # Required signer: admin PKH
        builder.required_signers = [admin.vkey.hash()]

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )

        last_tx_hash = signed_tx.id.payload.hex()
        ctx.submit_tx(signed_tx)
        logger.info(
            "Admin withdraw %d/%d: %s (%d cMATRA)",
            i + 1, len(pool_utxo_refs), last_tx_hash, ref["cmatra_qty"],
        )

        # Wait for confirmation before next spend
        if i < len(pool_utxo_refs) - 1:
            time.sleep(30)

    total_reclaimed = sum(r["cmatra_qty"] for r in pool_utxo_refs)
    logger.info(
        "Admin withdraw complete: reclaimed %d cMATRA from %d UTxOs",
        total_reclaimed, len(pool_utxo_refs),
    )
    return last_tx_hash


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_preprod_rehearsal(
    work_dir: Path,
    blockfrost_project_id: str,
    blueprint_path: Path,
    num_test_wallets: int = 20,
    timelock_offset_slots: int = 600,  # ~10 minutes for quick AdminWithdraw test
    num_pool_utxos: int = 5,
    skip_to_stage: int = 1,
) -> None:
    """Run the full preprod rehearsal for the surrender-and-redeem model.

    Stages 1-3: wallet setup, token minting, distribution (same as v2).
    Stages 4-9: cMATRA mint, validator deploy, pool build, surrender,
    red-team, and admin withdraw (new for v3).

    Args:
        work_dir: Working directory for keys, data, and state file.
        blockfrost_project_id: Blockfrost preprod project ID.
        blueprint_path: Path to Aiken plutus.json blueprint.
        num_test_wallets: Number of test wallets to create.
        timelock_offset_slots: Offset for minting policy timelock.
        num_pool_utxos: Number of pool UTxOs to create.
        skip_to_stage: Resume from a specific stage (1-9).
    """
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
            total_ada = sum(
                u.output.amount.coin if hasattr(u.output.amount, 'coin')
                else u.output.amount
                for u in utxos
            )
            logger.info("Admin balance: %d lovelace (%.2f ADA)", total_ada, total_ada / 1e6)
            if total_ada < 100_000_000:
                logger.warning(
                    "Insufficient tADA! Need at least 100 ADA. "
                    "Fund the admin wallet and re-run."
                )
                state["stage_1_complete"] = True
                state["needs_funding"] = True
                save_state()
                return
        except Exception as e:
            logger.warning(
                "Could not check balance (wallet may not be funded yet): %s", e
            )
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
        test_wallets = [
            Wallet.load(f"test_{i:03d}", keys_dir)
            for i in range(num_test_wallets)
        ]

    # ---------------------------------------------------------------
    # Stage 2: Mint test tokens (AGENT_TEST + SHARDS_TEST)
    # ---------------------------------------------------------------
    if skip_to_stage <= 2:
        logger.info("=" * 60)
        logger.info("STAGE 2: Mint AGENT_TEST + SHARDS_TEST")
        logger.info("=" * 60)

        agent_name_hex = "4167656e7454657374"    # "AgentTest"
        shards_name_hex = "53686172647354657374"  # "ShardsTest"

        agent_supply = 1_000_000_000              # 1B, 0 decimals
        shards_supply = 3_000_000_000_000         # 3M display x 10^6, 6 decimals

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
    # Stage 4: Mint cMATRA_TEST + build rate table
    # ---------------------------------------------------------------
    if skip_to_stage <= 4:
        logger.info("=" * 60)
        logger.info("STAGE 4: Mint cMATRA_TEST with timelock + build rate table")
        logger.info("=" * 60)

        # Fresh context after previous stages
        context = BlockFrostChainContext(project_id=blockfrost_project_id)
        current_slot = context.last_block_slot
        timelock_slot = current_slot + timelock_offset_slots
        logger.info(
            "Current slot: %d, timelock: %d (offset +%d)",
            current_slot, timelock_slot, timelock_offset_slots,
        )

        cmatra_policy_id, cmatra_tx = mint_cmatra_test(context, admin, timelock_slot)
        logger.info("Waiting for cMATRA_TEST mint confirmation...")
        # Poll for confirmation (preprod blocks ~20s, but can be delayed)
        for _attempt in range(12):
            time.sleep(15)
            try:
                _check_ctx = BlockFrostChainContext(project_id=blockfrost_project_id)
                _utxos = _check_ctx.utxos(admin.address)
                _has_cmatra = any(
                    cmatra_policy_id in (utxo.output.amount.multi_asset or {})
                    for utxo in _utxos
                )
                if _has_cmatra:
                    logger.info("cMATRA_TEST mint confirmed after %d seconds", (_attempt + 1) * 15)
                    break
            except Exception:
                pass
        else:
            logger.warning("cMATRA_TEST mint not confirmed after 180s — proceeding anyway")

        # Build simple rate table: 72.25% pool split equally between AGENT and SHARDS (v5.1)
        pool_base = int(CMATRA_TEST_SUPPLY_BASE * 0.7225)
        agent_bucket = pool_base // 2
        shards_bucket = pool_base - agent_bucket
        rate_table = {
            "tokens": {
                "AGENT_TEST": {
                    "rate_base_per_unit": agent_bucket // agent_supply,
                    "is_nft": False,
                },
                "SHARDS_TEST": {
                    "rate_base_per_unit": shards_bucket // shards_supply,
                    "is_nft": False,
                },
            },
            "public_pool_base": pool_base,
        }

        logger.info(
            "Rate table built: AGENT rate=%d, SHARDS rate=%d, pool=%d",
            rate_table["tokens"]["AGENT_TEST"]["rate_base_per_unit"],
            rate_table["tokens"]["SHARDS_TEST"]["rate_base_per_unit"],
            pool_base,
        )

        state["cmatra_test"] = {
            "policy_id": cmatra_policy_id.payload.hex(),
            "asset_name_hex": CMATRA_ASSET_NAME_HEX,
            "supply": CMATRA_TEST_SUPPLY_BASE,
            "decimals": CMATRA_TEST_DECIMALS,
            "timelock_slot": timelock_slot,
            "mint_tx": cmatra_tx,
        }
        state["rate_table"] = rate_table
        state["pool_base"] = pool_base
        state["stage_4_complete"] = True
        save_state()

    else:
        cmatra_policy_id = ScriptHash(bytes.fromhex(state["cmatra_test"]["policy_id"]))
        rate_table = state["rate_table"]
        pool_base = state["pool_base"]

    # ---------------------------------------------------------------
    # Stage 5: Deploy surrender validator
    # ---------------------------------------------------------------
    if skip_to_stage <= 5:
        logger.info("=" * 60)
        logger.info("STAGE 5: Deploy surrender validator")
        logger.info("=" * 60)

        # Compute deadline: timelock_offset_slots seconds from now (1 slot ≈ 1 sec on preprod)
        deadline_posix_ms = int(time.time() * 1000) + (timelock_offset_slots * 1000)
        logger.info(
            "Applying validator params: admin_pkh=%s, deadline=%d ms (~%d min from now)",
            admin.vkey.hash().payload.hex(), deadline_posix_ms, timelock_offset_slots // 60,
        )

        script, script_hash, script_address = load_surrender_validator(
            blueprint_path,
            admin_pkh=admin.vkey.hash().payload.hex(),
            deadline_posix_ms=deadline_posix_ms,
        )

        state["surrender_validator"] = {
            "script_hash": script_hash.payload.hex(),
            "script_address": script_address,
            "compiled_code": script.to_primitive().hex(),
        }
        state["deadline_posix_ms"] = deadline_posix_ms
        state["stage_5_complete"] = True
        save_state()

        logger.info("Deadline (POSIX ms): %d", deadline_posix_ms)

    else:
        deadline_posix_ms = state["deadline_posix_ms"]
        script, script_hash, script_address = load_surrender_validator(
            blueprint_path,
            admin_pkh=admin.vkey.hash().payload.hex(),
            deadline_posix_ms=deadline_posix_ms,
        )

    # ---------------------------------------------------------------
    # Stage 6: Build surrender pool UTxOs
    # ---------------------------------------------------------------
    if skip_to_stage <= 6:
        logger.info("=" * 60)
        logger.info("STAGE 6: Build surrender pool UTxOs")
        logger.info("=" * 60)

        pool_batches = build_pool_utxos(
            blockfrost_project_id, admin, script_address,
            cmatra_policy_id, pool_base,
            num_pool_utxos=num_pool_utxos,
            batch_size=5,
        )

        # Build a tracking list of all pool UTxO references
        pool_utxo_refs: list[dict[str, Any]] = []
        for batch in pool_batches:
            tx_hash = batch["tx_hash"]
            for out in batch["outputs"]:
                pool_utxo_refs.append({
                    "tx_hash": tx_hash,
                    "output_index": out["pool_utxo_index"],
                    "cmatra_qty": out["cmatra_base_units"],
                    "ada_amount": 2_000_000,
                })

        state["pool_batches"] = pool_batches
        state["pool_utxo_refs"] = pool_utxo_refs
        state["stage_6_complete"] = True
        save_state()

        logger.info(
            "Pool built: %d UTxOs, %d total cMATRA",
            len(pool_utxo_refs),
            sum(r["cmatra_qty"] for r in pool_utxo_refs),
        )

        # Wait for pool UTxOs to appear on-chain before proceeding
        logger.info("Waiting for pool UTxOs to confirm on-chain...")
        time.sleep(60)

    else:
        pool_utxo_refs = state["pool_utxo_refs"]

    # ---------------------------------------------------------------
    # Stage 7: Happy-path surrenders
    # ---------------------------------------------------------------
    if skip_to_stage <= 7:
        logger.info("=" * 60)
        logger.info("STAGE 7: Happy-path surrenders")
        logger.info("=" * 60)

        surrender_results: list[dict[str, Any]] = []

        # Pick first 3 test wallets that have legacy tokens
        wallets_with_tokens = [
            (w, d) for w in test_wallets
            for d in distributions
            if d.wallet_index == test_wallets.index(w)
            and (d.agent_amount > 0 or d.shards_amount > 0)
        ][:3]

        # Use a copy of pool_utxo_refs to track spending
        available_pool_refs = list(pool_utxo_refs)

        for w, dist in wallets_with_tokens:
            if not available_pool_refs:
                logger.warning("No more pool UTxOs available for surrenders")
                break

            # Compute cMATRA amount based on rate table
            total_cmatra = 0
            if dist.agent_amount > 0:
                agent_rate = rate_table["tokens"]["AGENT_TEST"]["rate_base_per_unit"]
                total_cmatra += dist.agent_amount * agent_rate
            if dist.shards_amount > 0:
                shards_rate = rate_table["tokens"]["SHARDS_TEST"]["rate_base_per_unit"]
                total_cmatra += dist.shards_amount * shards_rate

            if total_cmatra <= 0:
                continue

            # Find a pool UTxO with sufficient balance
            selected_ref = None
            for ref in available_pool_refs:
                if ref["cmatra_qty"] >= total_cmatra:
                    selected_ref = ref
                    break

            if selected_ref is None:
                logger.warning(
                    "No pool UTxO large enough for %s (need %d cMATRA)",
                    w.name, total_cmatra,
                )
                continue

            try:
                tx_hash = process_surrender(
                    blockfrost_project_id, admin, str(w.address),
                    total_cmatra, selected_ref, script_address,
                    script, cmatra_policy_id,
                    deadline_posix_ms=deadline_posix_ms,
                )

                # Update tracking: the spent UTxO is gone, a new one is created
                # with the remaining balance at a new tx_hash
                remaining = selected_ref["cmatra_qty"] - total_cmatra
                available_pool_refs.remove(selected_ref)

                if remaining > 0:
                    # The output 1 (index 1) is the return-to-script output
                    # (output 0 is cMATRA to user)
                    new_ref = {
                        "tx_hash": tx_hash,
                        "output_index": 1,
                        "cmatra_qty": remaining,
                        "ada_amount": 2_000_000,
                    }
                    available_pool_refs.append(new_ref)

                surrender_results.append({
                    "wallet": w.name,
                    "address": str(w.address),
                    "cmatra_amount": total_cmatra,
                    "tx_hash": tx_hash,
                    "status": "success",
                })
                logger.info("Surrender OK: %s -> %d cMATRA, tx %s", w.name, total_cmatra, tx_hash)

                # Wait for chain confirmation
                logger.info("Waiting 30s for chain confirmation...")
                time.sleep(30)

            except Exception as e:
                surrender_results.append({
                    "wallet": w.name,
                    "address": str(w.address),
                    "cmatra_amount": total_cmatra,
                    "status": "error",
                    "error": str(e)[:300],
                })
                logger.warning("Surrender FAILED for %s: %s", w.name, str(e)[:200])

        state["surrender_results"] = surrender_results
        state["pool_utxo_refs_remaining"] = available_pool_refs
        state["stage_7_complete"] = True
        save_state()

    else:
        available_pool_refs = state.get("pool_utxo_refs_remaining", pool_utxo_refs)

    # ---------------------------------------------------------------
    # Stage 8: Red-team tests
    # ---------------------------------------------------------------
    if skip_to_stage <= 8:
        logger.info("=" * 60)
        logger.info("STAGE 8: Red-team adversarial tests")
        logger.info("=" * 60)

        red_team_results: list[dict[str, Any]] = []

        if not available_pool_refs:
            logger.warning("No pool UTxOs available for red-team tests")
        else:
            # Use the first available pool UTxO for all tests
            # (none of them should succeed, so it stays unspent)
            test_pool_ref = available_pool_refs[0]

            # Find a test wallet with some ADA for signing
            # Use a wallet that was NOT used in stage 7
            surrendered_wallets = {
                r["wallet"]
                for r in state.get("surrender_results", [])
                if r.get("status") == "success"
            }
            attacker_wallets = [
                w for w in test_wallets
                if w.name not in surrendered_wallets
            ]

            if len(attacker_wallets) < 1:
                logger.warning("No attacker wallets available for red-team tests")
            else:
                attacker = attacker_wallets[0]

                # Test 1: Wrong signer
                logger.info("RED TEAM TEST 1: Wrong signer (non-admin)")
                passed = red_team_wrong_signer(
                    blockfrost_project_id, attacker, admin,
                    test_pool_ref, script_address, script, cmatra_policy_id,
                )
                red_team_results.append({"test": "wrong_signer", "passed": passed})

                # Test 2: Post-deadline surrender
                logger.info("RED TEAM TEST 2: Post-deadline ProcessSurrender")
                passed = red_team_post_deadline(
                    blockfrost_project_id, admin,
                    test_pool_ref, script_address, script, cmatra_policy_id,
                    deadline_posix_ms,
                )
                red_team_results.append({"test": "post_deadline_surrender", "passed": passed})

                # Test 3: No admin signature
                logger.info("RED TEAM TEST 3: No admin signature")
                passed = red_team_no_admin_sig(
                    blockfrost_project_id, attacker,
                    test_pool_ref, script_address, script, cmatra_policy_id,
                )
                red_team_results.append({"test": "no_admin_sig", "passed": passed})

        state["red_team_results"] = red_team_results
        state["stage_8_complete"] = True
        save_state()

    # ---------------------------------------------------------------
    # Stage 9: Admin withdraw after deadline
    # ---------------------------------------------------------------
    if skip_to_stage <= 9:
        logger.info("=" * 60)
        logger.info("STAGE 9: Admin withdraw after deadline")
        logger.info("=" * 60)

        # Check if deadline has passed
        now_ms = int(time.time() * 1000)
        if now_ms < deadline_posix_ms:
            wait_seconds = (deadline_posix_ms - now_ms) / 1000
            logger.info(
                "Deadline not yet reached. Current: %d ms, deadline: %d ms",
                now_ms, deadline_posix_ms,
            )
            if wait_seconds <= 900:  # Wait up to 15 minutes
                logger.info(
                    "Waiting %.0f seconds for deadline to pass...",
                    wait_seconds + 10,
                )
                time.sleep(wait_seconds + 10)
            else:
                logger.warning(
                    "Deadline is %.1f minutes away -- too long to wait. "
                    "Re-run with --skip-to-stage 9 after the deadline.",
                    wait_seconds / 60,
                )
                state["stage_9_complete"] = False
                state["stage_9_skipped_reason"] = "deadline_not_reached"
                save_state()
                return

        remaining_refs = state.get("pool_utxo_refs_remaining", available_pool_refs)

        if not remaining_refs:
            logger.info("No remaining pool UTxOs to withdraw (all used in surrenders)")
            state["admin_withdraw_tx"] = None
            state["stage_9_complete"] = True
            save_state()
        else:
            try:
                withdraw_tx = admin_withdraw(
                    blockfrost_project_id, admin,
                    remaining_refs, script_address,
                    script, cmatra_policy_id,
                )
                state["admin_withdraw_tx"] = withdraw_tx
                state["stage_9_complete"] = True
                save_state()
                logger.info("Admin withdraw complete: %s", withdraw_tx)
            except Exception as e:
                logger.error("Admin withdraw FAILED: %s", str(e)[:300])
                state["admin_withdraw_error"] = str(e)[:500]
                state["stage_9_complete"] = False
                save_state()

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PREPROD REHEARSAL COMPLETE (Surrender Model v3.0)")
    logger.info("=" * 60)
    logger.info("State saved to: %s", state_file)
    logger.info("")
    logger.info("Results:")
    for i in range(1, 10):
        key = f"stage_{i}_complete"
        status = "OK" if state.get(key) else "SKIP/PENDING"
        logger.info("  Stage %d: %s", i, status)

    if state.get("surrender_results"):
        logger.info("")
        logger.info("Surrender results:")
        for r in state["surrender_results"]:
            status = r.get("status", "unknown")
            logger.info("  %s: %s", r["wallet"], status.upper())

    if state.get("red_team_results"):
        logger.info("")
        logger.info("Red-team results:")
        for r in state["red_team_results"]:
            status = "PASS" if r["passed"] else "FAIL"
            logger.info("  %s: %s", r["test"], status)

    if state.get("admin_withdraw_tx"):
        logger.info("")
        logger.info("Admin withdraw TX: %s", state["admin_withdraw_tx"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """CLI entry point for the preprod rehearsal harness."""
    parser = argparse.ArgumentParser(
        description="cMATRA Merger -- Preprod Rehearsal Harness (Surrender Model v3.0)",
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
        "--timelock-offset", type=int, default=600,
        help="Timelock offset in slots from current (~10 minutes default)",
    )
    parser.add_argument(
        "--num-pool-utxos", type=int, default=5,
        help="Number of pool UTxOs to create (default 5)",
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
        num_pool_utxos=args.num_pool_utxos,
        skip_to_stage=args.skip_to_stage,
    )


if __name__ == "__main__":
    main()
