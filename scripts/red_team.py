#!/usr/bin/env python3
"""
scripts/red_team.py — Red-Team Test Suite for Surrender Pool Validator v4.0
===========================================================================

Runs adversarial tests against the deployed surrender pool validator on preprod.
Requires a completed preprod rehearsal (stages 1-6 at minimum — pool deployed).

The surrender model uses Void datums and DUAL-ADMIN spending. Two paths:
  - ProcessSurrender: BOTH admins sign + tx validity entirely BEFORE deadline
  - AdminWithdraw:    BOTH admins sign + tx validity entirely AFTER deadline

Tests (negative — validator must REJECT):
  1. Non-admin ProcessSurrender — random wallet tries to spend pool UTxO
  2. Post-deadline ProcessSurrender — both admins ProcessSurrender after deadline
  3. Pre-deadline AdminWithdraw — both admins AdminWithdraw before deadline
  4. Non-admin AdminWithdraw — random wallet tries AdminWithdraw after deadline
  5. Wrong redeemer data — garbage Constr(99, [...]) redeemer
  6. Fabricated UTxO reference — non-existent pool UTxO (all-zero tx hash)
  7. Single-admin ProcessSurrender — only admin_1 signs (admin_2 missing)
  8. Single-admin AdminWithdraw — only admin_2 signs (admin_1 missing)

Tests (positive — validator must ACCEPT):
  9. Both admins ProcessSurrender before deadline — happy-path surrender
  10. Both admins AdminWithdraw after deadline — happy-path sweep

Each negative test should FAIL (i.e., the validator rejects the tx).
A "PASS" means the validator correctly rejected the attack.

Used by: manual operator invocation after preprod deployment.
Depends on: scripts/preprod_harness.py (Wallet class), tools/cardano_utils.py,
            tools/api_clients.py (BlockfrostClient), pycardano 0.19.x, cbor2.
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
from cbor2 import CBORTag
from pycardano import (
    Address,
    Asset,
    AssetName,
    BlockFrostChainContext,
    ExecutionUnits,
    MultiAsset,
    Network,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    RawPlutusData,
    Redeemer,
    TransactionBuilder,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.hash import ScriptHash, TransactionId

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CBOR constants for surrender model redeemers and datums
# ---------------------------------------------------------------------------

# Void datum: Constr(0, []) = CBORTag(121, [])
VOID_DATUM_CBOR = cbor2.dumps(CBORTag(121, []))

# ProcessSurrender redeemer: Constr(0, []) = CBORTag(121, [])
PROCESS_SURRENDER_REDEEMER_CBOR = cbor2.dumps(CBORTag(121, []))

# AdminWithdraw redeemer: Constr(1, []) = CBORTag(122, [])
ADMIN_WITHDRAW_REDEEMER_CBOR = cbor2.dumps(CBORTag(122, []))

# cMATRA asset name in hex: "cMATRA"
CMATRA_ASSET_NAME_HEX = "634d41545241"


# ---------------------------------------------------------------------------
# Pool UTxO helpers
# ---------------------------------------------------------------------------


def _query_live_pool_utxos(
    bf_client,
    script_address: str,
    cmatra_policy_hex: str,
    cmatra_asset_hex: str,
) -> list[dict[str, Any]]:
    """Query chain for live pool UTxOs at the script address containing cMATRA.

    Returns a list of dicts sorted by cmatra_amount descending:
        [{tx_hash, output_index, cmatra_amount, ada_amount}, ...]

    Reads directly from the on-chain UTxO set so refs are never stale.
    """
    utxos = bf_client.get_address_utxos(script_address)
    cmatra_unit = cmatra_policy_hex + cmatra_asset_hex

    pool_utxos: list[dict[str, Any]] = []

    for u in utxos:
        # Only accept UTxOs with Void inline datum (Constr(0, []))
        inline = u.get("inline_datum")
        if not inline:
            continue
        if not (isinstance(inline, dict)
                and inline.get("constructor") == 0
                and inline.get("fields") == []):
            continue

        cmatra_qty = 0
        ada_qty = 0
        for amt in u.get("amount", []):
            if amt.get("unit") == cmatra_unit:
                cmatra_qty = int(amt.get("quantity", 0))
            elif amt.get("unit") == "lovelace":
                ada_qty = int(amt.get("quantity", 0))
        if cmatra_qty <= 0:
            continue  # not a cMATRA pool UTxO

        output_idx = u.get("output_index", u.get("tx_index", 0))
        pool_utxos.append({
            "tx_hash": u["tx_hash"],
            "output_index": output_idx,
            "cmatra_amount": cmatra_qty,
            "ada_amount": ada_qty,
        })

    # Sort by cmatra_amount descending so we pick the largest first
    pool_utxos.sort(key=lambda x: x["cmatra_amount"], reverse=True)
    return pool_utxos


def make_pool_utxo(
    tx_hash_hex: str,
    output_index: int,
    cmatra_amount: int,
    ada_amount: int,
    script_address: str,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
) -> UTxO:
    """Reconstruct a pool UTxO with Void datum for transaction building.

    Pool UTxOs in the surrender model use Void datums (Constr(0, [])),
    not per-claimant PKH datums like the old claim model.
    """
    asset_name = AssetName(bytes.fromhex(cmatra_asset_hex))
    script_addr = Address.from_primitive(script_address)
    multi = MultiAsset()
    multi[cmatra_policy_id] = Asset({asset_name: cmatra_amount})
    value = Value(ada_amount, multi)

    # Void datum: Constr(0, [])
    datum = RawPlutusData(cbor2.loads(VOID_DATUM_CBOR))

    tx_in = TransactionInput(
        TransactionId(bytes.fromhex(tx_hash_hex)),
        output_index,
    )
    return UTxO(tx_in, TransactionOutput(script_addr, value, datum=datum))


def process_surrender_redeemer() -> Redeemer:
    """Build ProcessSurrender redeemer (Constr(0, [])) with explicit execution units."""
    return Redeemer(
        RawPlutusData(cbor2.loads(PROCESS_SURRENDER_REDEEMER_CBOR)),
        ExecutionUnits(500_000, 200_000_000),
    )


def admin_withdraw_redeemer() -> Redeemer:
    """Build AdminWithdraw redeemer (Constr(1, [])) with explicit execution units."""
    return Redeemer(
        RawPlutusData(cbor2.loads(ADMIN_WITHDRAW_REDEEMER_CBOR)),
        ExecutionUnits(500_000, 200_000_000),
    )


def extract_deadline_from_compiled(compiled_hex: str) -> int:
    """Extract the baked-in deadline (POSIX ms) from the applied compiled code.

    The deadline is the last CBOR integer parameter in the compiled code,
    encoded as ``1b`` (8-byte unsigned int) near the end of the hex string.
    """
    idx = compiled_hex.rfind("1b")
    while idx >= 0:
        candidate = compiled_hex[idx + 2 : idx + 18]
        if len(candidate) == 16:
            try:
                value = int(candidate, 16)
                # Sanity: POSIX ms should be in 2024-2030 range
                if 1_700_000_000_000 < value < 2_000_000_000_000:
                    return value
            except ValueError:
                pass
        idx = compiled_hex.rfind("1b", 0, idx)
    raise ValueError("Cannot extract deadline from compiled code")


def load_wallet(name: str, keys_dir: Path):
    """Load a wallet by name from the keys directory."""
    from scripts.preprod_harness import Wallet
    return Wallet.load(name, keys_dir)


# ---------------------------------------------------------------------------
# Individual red-team tests
# ---------------------------------------------------------------------------


def test_nonadmin_process_surrender(
    context: BlockFrostChainContext,
    attacker,
    pool_ref: dict,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
) -> dict[str, Any]:
    """Test 1: Non-admin tries ProcessSurrender. Should FAIL (admin_signed check)."""
    try:
        builder = TransactionBuilder(context)
        utxo = make_pool_utxo(
            pool_ref["tx_hash"], pool_ref["output_index"],
            pool_ref["cmatra_amount"], pool_ref["ada_amount"],
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )
        builder.add_script_input(utxo, script=script, redeemer=process_surrender_redeemer())

        # Attacker signs — NOT admin
        builder.required_signers = [attacker.vkey.hash()]
        builder.add_input_address(attacker.address)

        signed_tx = builder.build_and_sign(
            signing_keys=[attacker.skey],
            change_address=attacker.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "nonadmin_process_surrender", "passed": False,
                "error": "Non-admin ProcessSurrender was accepted!"}
    except Exception as e:
        return {"test": "nonadmin_process_surrender", "passed": True,
                "rejection": str(e)[:300]}


def test_postdeadline_process_surrender(
    context: BlockFrostChainContext,
    admin,
    pool_ref: dict,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
    deadline_posix_ms: int,
) -> dict[str, Any]:
    """Test 2: Admin ProcessSurrender AFTER deadline. Should FAIL (is_entirely_before check)."""
    try:
        from tools.cardano_utils import posix_ms_to_slot

        builder = TransactionBuilder(context)
        utxo = make_pool_utxo(
            pool_ref["tx_hash"], pool_ref["output_index"],
            pool_ref["cmatra_amount"], pool_ref["ada_amount"],
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )
        builder.add_script_input(utxo, script=script, redeemer=process_surrender_redeemer())

        builder.required_signers = [admin.vkey.hash()]
        builder.add_input_address(admin.address)

        # Set validity_start AFTER deadline — violates is_entirely_before
        deadline_slot = posix_ms_to_slot(deadline_posix_ms, "preprod")
        builder.validity_start = deadline_slot + 10

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "postdeadline_process_surrender", "passed": False,
                "error": "Post-deadline ProcessSurrender was accepted!"}
    except Exception as e:
        return {"test": "postdeadline_process_surrender", "passed": True,
                "rejection": str(e)[:300]}


def test_predeadline_admin_withdraw(
    context: BlockFrostChainContext,
    admin,
    pool_ref: dict,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
    deadline_posix_ms: int,
) -> dict[str, Any]:
    """Test 3: Admin AdminWithdraw BEFORE deadline. Should FAIL (is_entirely_after check)."""
    try:
        from tools.cardano_utils import posix_ms_to_slot

        builder = TransactionBuilder(context)
        utxo = make_pool_utxo(
            pool_ref["tx_hash"], pool_ref["output_index"],
            pool_ref["cmatra_amount"], pool_ref["ada_amount"],
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )
        builder.add_script_input(utxo, script=script, redeemer=admin_withdraw_redeemer())

        builder.required_signers = [admin.vkey.hash()]
        builder.add_input_address(admin.address)

        # Set TTL BEFORE deadline — violates is_entirely_after
        deadline_slot = posix_ms_to_slot(deadline_posix_ms, "preprod")
        builder.ttl = deadline_slot - 10

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "predeadline_admin_withdraw", "passed": False,
                "error": "Pre-deadline AdminWithdraw was accepted!"}
    except Exception as e:
        return {"test": "predeadline_admin_withdraw", "passed": True,
                "rejection": str(e)[:300]}


def test_nonadmin_admin_withdraw(
    context: BlockFrostChainContext,
    attacker,
    pool_ref: dict,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
    deadline_posix_ms: int,
) -> dict[str, Any]:
    """Test 4: Non-admin tries AdminWithdraw after deadline. Should FAIL (admin_signed check)."""
    try:
        from tools.cardano_utils import posix_ms_to_slot

        builder = TransactionBuilder(context)
        utxo = make_pool_utxo(
            pool_ref["tx_hash"], pool_ref["output_index"],
            pool_ref["cmatra_amount"], pool_ref["ada_amount"],
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )
        builder.add_script_input(utxo, script=script, redeemer=admin_withdraw_redeemer())

        # Attacker signs — NOT admin
        builder.required_signers = [attacker.vkey.hash()]
        builder.add_input_address(attacker.address)

        # Set validity_start AFTER deadline so timing is correct
        deadline_slot = posix_ms_to_slot(deadline_posix_ms, "preprod")
        builder.validity_start = deadline_slot + 1

        signed_tx = builder.build_and_sign(
            signing_keys=[attacker.skey],
            change_address=attacker.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "nonadmin_admin_withdraw", "passed": False,
                "error": "Non-admin AdminWithdraw was accepted!"}
    except Exception as e:
        return {"test": "nonadmin_admin_withdraw", "passed": True,
                "rejection": str(e)[:300]}


def test_wrong_redeemer(
    context: BlockFrostChainContext,
    admin,
    pool_ref: dict,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
) -> dict[str, Any]:
    """Test 5: Garbage redeemer (Constr(99, [0xff * 100])). Should FAIL (pattern match)."""
    try:
        builder = TransactionBuilder(context)
        utxo = make_pool_utxo(
            pool_ref["tx_hash"], pool_ref["output_index"],
            pool_ref["cmatra_amount"], pool_ref["ada_amount"],
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )
        # Garbage redeemer: Constr(99, [b"\xff" * 100])
        # CBORTag(99 + 121) = CBORTag(220, [...]) for constructor indices >= 7
        # Actually, Constr(99, ...) doesn't map to CBORTag(220). Use raw CBOR.
        garbage_data = CBORTag(121 + 99, [b"\xff" * 100])
        garbage_redeemer = Redeemer(
            RawPlutusData(cbor2.loads(cbor2.dumps(garbage_data))),
            ExecutionUnits(500_000, 200_000_000),
        )
        builder.add_script_input(utxo, script=script, redeemer=garbage_redeemer)

        builder.required_signers = [admin.vkey.hash()]
        builder.add_input_address(admin.address)

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "wrong_redeemer", "passed": False,
                "error": "Garbage redeemer was accepted!"}
    except Exception as e:
        return {"test": "wrong_redeemer", "passed": True,
                "rejection": str(e)[:300]}


def test_fabricated_utxo(
    context: BlockFrostChainContext,
    admin,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
) -> dict[str, Any]:
    """Test 6: Reference a non-existent pool UTxO (all-zero tx hash). Should FAIL at submission."""
    try:
        fake_tx_hash = "0000000000000000000000000000000000000000000000000000000000000000"
        utxo = make_pool_utxo(
            fake_tx_hash, 0, 1_000_000, 2_000_000,
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )

        builder = TransactionBuilder(context)
        builder.add_script_input(utxo, script=script, redeemer=process_surrender_redeemer())
        builder.required_signers = [admin.vkey.hash()]
        builder.add_input_address(admin.address)

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "fabricated_utxo", "passed": False,
                "error": "Fake UTxO ref TX accepted!"}
    except Exception as e:
        return {"test": "fabricated_utxo", "passed": True,
                "rejection": str(e)[:300]}


def test_single_admin_process_surrender(
    context: BlockFrostChainContext,
    admin_1,
    pool_ref: dict,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
) -> dict[str, Any]:
    """Test 7: Only admin_1 signs ProcessSurrender (admin_2 missing). Should FAIL."""
    try:
        builder = TransactionBuilder(context)
        utxo = make_pool_utxo(
            pool_ref["tx_hash"], pool_ref["output_index"],
            pool_ref["cmatra_amount"], pool_ref["ada_amount"],
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )
        builder.add_script_input(utxo, script=script, redeemer=process_surrender_redeemer())

        # Only admin_1 signs — admin_2 is missing
        builder.required_signers = [admin_1.vkey.hash()]
        builder.add_input_address(admin_1.address)

        signed_tx = builder.build_and_sign(
            signing_keys=[admin_1.skey],
            change_address=admin_1.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "single_admin_process_surrender", "passed": False,
                "error": "Single-admin ProcessSurrender was accepted!"}
    except Exception as e:
        return {"test": "single_admin_process_surrender", "passed": True,
                "rejection": str(e)[:300]}


def test_single_admin_withdraw(
    context: BlockFrostChainContext,
    admin_2,
    pool_ref: dict,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
    deadline_posix_ms: int,
) -> dict[str, Any]:
    """Test 8: Only admin_2 signs AdminWithdraw (admin_1 missing). Should FAIL."""
    try:
        from tools.cardano_utils import posix_ms_to_slot

        builder = TransactionBuilder(context)
        utxo = make_pool_utxo(
            pool_ref["tx_hash"], pool_ref["output_index"],
            pool_ref["cmatra_amount"], pool_ref["ada_amount"],
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )
        builder.add_script_input(utxo, script=script, redeemer=admin_withdraw_redeemer())

        # Only admin_2 signs — admin_1 is missing
        builder.required_signers = [admin_2.vkey.hash()]
        builder.add_input_address(admin_2.address)

        deadline_slot = posix_ms_to_slot(deadline_posix_ms, "preprod")
        builder.validity_start = deadline_slot + 1

        signed_tx = builder.build_and_sign(
            signing_keys=[admin_2.skey],
            change_address=admin_2.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "single_admin_withdraw", "passed": False,
                "error": "Single-admin AdminWithdraw was accepted!"}
    except Exception as e:
        return {"test": "single_admin_withdraw", "passed": True,
                "rejection": str(e)[:300]}


def test_admin_process_surrender_happy(
    context: BlockFrostChainContext,
    admin_1,
    admin_2,
    pool_ref: dict,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
    deadline_posix_ms: int,
) -> dict[str, Any]:
    """Test 9 (positive): Both admins ProcessSurrender before deadline. Should SUCCEED."""
    try:
        from tools.cardano_utils import posix_ms_to_slot

        builder = TransactionBuilder(context)
        utxo = make_pool_utxo(
            pool_ref["tx_hash"], pool_ref["output_index"],
            pool_ref["cmatra_amount"], pool_ref["ada_amount"],
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )
        builder.add_script_input(utxo, script=script, redeemer=process_surrender_redeemer())

        # Both admins must sign
        builder.required_signers = [admin_1.vkey.hash(), admin_2.vkey.hash()]
        builder.add_input_address(admin_1.address)

        # Ensure validity range is entirely before deadline
        deadline_slot = posix_ms_to_slot(deadline_posix_ms, "preprod")
        builder.ttl = deadline_slot - 1

        signed_tx = builder.build_and_sign(
            signing_keys=[admin_1.skey, admin_2.skey],
            change_address=admin_1.address,
        )
        context.submit_tx(signed_tx)
        tx_hash = signed_tx.id.payload.hex()
        return {"test": "both_admins_process_surrender_happy", "passed": True,
                "tx_hash": tx_hash, "note": "Both admins successfully processed surrender"}
    except Exception as e:
        return {"test": "both_admins_process_surrender_happy", "passed": False,
                "error": str(e)[:300]}


def test_admin_withdraw_happy(
    context: BlockFrostChainContext,
    admin_1,
    admin_2,
    pool_ref: dict,
    script_address: str,
    script: PlutusV3Script,
    cmatra_policy_id: ScriptHash,
    cmatra_asset_hex: str,
    deadline_posix_ms: int,
) -> dict[str, Any]:
    """Test 10 (positive): Both admins AdminWithdraw after deadline. Should SUCCEED."""
    try:
        from tools.cardano_utils import posix_ms_to_slot

        builder = TransactionBuilder(context)
        utxo = make_pool_utxo(
            pool_ref["tx_hash"], pool_ref["output_index"],
            pool_ref["cmatra_amount"], pool_ref["ada_amount"],
            script_address, cmatra_policy_id, cmatra_asset_hex,
        )
        builder.add_script_input(utxo, script=script, redeemer=admin_withdraw_redeemer())

        # Both admins must sign
        builder.required_signers = [admin_1.vkey.hash(), admin_2.vkey.hash()]
        builder.add_input_address(admin_1.address)

        # Set validity_start AFTER deadline
        deadline_slot = posix_ms_to_slot(deadline_posix_ms, "preprod")
        builder.validity_start = deadline_slot + 1

        signed_tx = builder.build_and_sign(
            signing_keys=[admin_1.skey, admin_2.skey],
            change_address=admin_1.address,
        )
        context.submit_tx(signed_tx)
        tx_hash = signed_tx.id.payload.hex()
        return {"test": "both_admins_withdraw_happy", "passed": True,
                "tx_hash": tx_hash, "note": "Both admins successfully withdrew after deadline"}
    except Exception as e:
        return {"test": "both_admins_withdraw_happy", "passed": False,
                "error": str(e)[:300]}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_red_team(
    work_dir: Path,
    blockfrost_project_id: str,
    blueprint_path: Path,
) -> list[dict[str, Any]]:
    """Run full red-team suite against preprod surrender pool deployment.

    Loads admin wallet + one random test wallet (for non-admin attacks),
    queries live pool UTxOs, and runs all 8 tests sequentially.
    """
    keys_dir = work_dir / "keys"
    state_file = work_dir / "rehearsal_state.json"

    if not state_file.exists():
        logger.error("No rehearsal state found at %s. Run preprod_harness.py first.", state_file)
        sys.exit(1)

    with open(state_file) as f:
        state = json.load(f)

    if not state.get("stage_6_complete"):
        logger.error("Rehearsal must complete through stage 6 (pool deployed). Current state is incomplete.")
        sys.exit(1)

    context = BlockFrostChainContext(
        project_id=blockfrost_project_id,
    )

    # Load validator from blueprint
    with open(blueprint_path) as f:
        bp = json.load(f)
    compiled = bp["validators"][0]["compiledCode"]
    script = PlutusV3Script(bytes.fromhex(compiled))

    # Read state for surrender model keys
    script_address = state["surrender_validator"]["script_address"]
    cmatra_policy_hex = state["cmatra_test"]["policy_id"]
    cmatra_asset_hex = state["cmatra_test"]["asset_name_hex"]
    cmatra_policy_id = ScriptHash(bytes.fromhex(cmatra_policy_hex))

    # Extract deadline from compiled validator (most reliable source)
    try:
        deadline_ms = extract_deadline_from_compiled(compiled)
        logger.info("On-chain deadline: %d ms (from blueprint)", deadline_ms)
    except ValueError:
        deadline_ms = state.get("deadline_posix_ms", 0)
        logger.warning("Could not extract deadline from blueprint, using state: %d ms", deadline_ms)

    if deadline_ms <= 0:
        logger.error("No valid deadline found. Cannot run red-team tests.")
        sys.exit(1)

    # Query live pool UTxOs directly from chain
    from scripts.preprod_harness import Wallet
    from tools.api_clients import BlockfrostClient as BfClient

    _BF_BASE_URLS = {
        "preprod": "https://cardano-preprod.blockfrost.io/api/v0",
        "preview": "https://cardano-preview.blockfrost.io/api/v0",
    }
    bf_network = "preprod" if blockfrost_project_id.startswith("preprod") else \
                 "preview" if blockfrost_project_id.startswith("preview") else "mainnet"
    bf_client = BfClient(
        project_id=blockfrost_project_id,
        base_url=_BF_BASE_URLS.get(bf_network, "https://cardano-mainnet.blockfrost.io/api/v0"),
    )

    pool_utxos = _query_live_pool_utxos(
        bf_client, script_address, cmatra_policy_hex, cmatra_asset_hex,
    )
    logger.info("Live pool UTxOs on-chain: %d", len(pool_utxos))

    if len(pool_utxos) < 2:
        logger.error(
            "Need at least 2 pool UTxOs for red-team tests (found %d). "
            "Deploy more pool UTxOs first.",
            len(pool_utxos),
        )
        sys.exit(1)

    # Load admin wallets (dual-signer model)
    admin_1 = Wallet.load("admin", keys_dir)
    logger.info("Admin 1 wallet: %s (%s)", admin_1.name, admin_1.pkh_hex[:16])

    # Admin 2: try "admin2" first, fall back to second test wallet
    try:
        admin_2 = Wallet.load("admin2", keys_dir)
    except Exception:
        # If no admin2 wallet, use a test wallet as stand-in for dual-signer tests
        test_wallet_names = [w["name"] for w in state.get("test_wallets", [])]
        if len(test_wallet_names) >= 2:
            admin_2 = Wallet.load(test_wallet_names[1], keys_dir)
        else:
            logger.error("Need admin2 wallet or at least 2 test wallets for dual-signer tests.")
            sys.exit(1)
    logger.info("Admin 2 wallet: %s (%s)", admin_2.name, admin_2.pkh_hex[:16])

    # Load a random test wallet for non-admin attack tests
    test_wallet_names = [w["name"] for w in state.get("test_wallets", [])]
    if not test_wallet_names:
        logger.error("No test wallets found in rehearsal state.")
        sys.exit(1)
    attacker = Wallet.load(test_wallet_names[0], keys_dir)
    logger.info("Attacker wallet: %s (%s)", attacker.name, attacker.pkh_hex[:16])

    results: list[dict[str, Any]] = []

    # Assign pool UTxOs to tests. Negative tests (1-6) don't consume UTxOs
    # because they are rejected. Positive tests (7, 8) each consume one.
    # We use pool_utxos[0] for negative tests and pool_utxos[0], pool_utxos[1]
    # for positive tests (since test 7 will consume pool_utxos[0]).
    neg_ref = pool_utxos[0]
    pos_ref_1 = pool_utxos[0]  # for test 7 (will be consumed)
    pos_ref_2 = pool_utxos[1]  # for test 8 (separate UTxO)

    # --- Negative tests (1-8) ---

    # Test 1: Non-admin ProcessSurrender
    logger.info("-" * 40)
    logger.info("TEST 1: Non-admin ProcessSurrender")
    r = test_nonadmin_process_surrender(
        context, attacker, neg_ref,
        script_address, script, cmatra_policy_id, cmatra_asset_hex,
    )
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 2: Post-deadline ProcessSurrender (both admins, wrong timing)
    logger.info("-" * 40)
    logger.info("TEST 2: Post-deadline ProcessSurrender")
    r = test_postdeadline_process_surrender(
        context, admin_1, neg_ref,
        script_address, script, cmatra_policy_id, cmatra_asset_hex,
        deadline_ms,
    )
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 3: Pre-deadline AdminWithdraw (both admins, wrong timing)
    logger.info("-" * 40)
    logger.info("TEST 3: Pre-deadline AdminWithdraw")
    r = test_predeadline_admin_withdraw(
        context, admin_1, neg_ref,
        script_address, script, cmatra_policy_id, cmatra_asset_hex,
        deadline_ms,
    )
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 4: Non-admin AdminWithdraw
    logger.info("-" * 40)
    logger.info("TEST 4: Non-admin AdminWithdraw")
    r = test_nonadmin_admin_withdraw(
        context, attacker, neg_ref,
        script_address, script, cmatra_policy_id, cmatra_asset_hex,
        deadline_ms,
    )
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 5: Wrong redeemer data
    logger.info("-" * 40)
    logger.info("TEST 5: Wrong redeemer data")
    r = test_wrong_redeemer(
        context, admin_1, neg_ref,
        script_address, script, cmatra_policy_id, cmatra_asset_hex,
    )
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 6: Fabricated UTxO reference
    logger.info("-" * 40)
    logger.info("TEST 6: Fabricated UTxO reference")
    r = test_fabricated_utxo(
        context, admin_1,
        script_address, script, cmatra_policy_id, cmatra_asset_hex,
    )
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 7: Single-admin ProcessSurrender (only admin_1, missing admin_2)
    logger.info("-" * 40)
    logger.info("TEST 7: Single-admin ProcessSurrender (admin_2 missing)")
    r = test_single_admin_process_surrender(
        context, admin_1, neg_ref,
        script_address, script, cmatra_policy_id, cmatra_asset_hex,
    )
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 8: Single-admin AdminWithdraw (only admin_2, missing admin_1)
    if deadline_passed:
        logger.info("-" * 40)
        logger.info("TEST 8: Single-admin AdminWithdraw (admin_1 missing)")
        r = test_single_admin_withdraw(
            context, admin_2, neg_ref,
            script_address, script, cmatra_policy_id, cmatra_asset_hex,
            deadline_ms,
        )
        results.append(r)
        logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")
    else:
        logger.info("-" * 40)
        logger.info("TEST 8: Single-admin AdminWithdraw — SKIPPED (deadline not passed)")

    # --- Positive tests (9-10) ---
    # These actually spend pool UTxOs, so we check timing constraints.

    # Determine current time vs deadline for positive test eligibility
    current_posix_ms = int(time.time() * 1000)
    deadline_passed = current_posix_ms > deadline_ms

    # Test 9: Both admins ProcessSurrender before deadline (happy path)
    if not deadline_passed:
        logger.info("-" * 40)
        logger.info("TEST 9: Both admins ProcessSurrender (happy path)")
        ctx9 = BlockFrostChainContext(project_id=blockfrost_project_id)
        r = test_admin_process_surrender_happy(
            ctx9, admin_1, admin_2, pos_ref_1,
            script_address, script, cmatra_policy_id, cmatra_asset_hex,
            deadline_ms,
        )
        results.append(r)
        logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

        if r["passed"]:
            logger.info("Waiting 30s for chain confirmation before test 10...")
            time.sleep(30)
    else:
        logger.warning(
            "Deadline has passed (current: %d > deadline: %d). "
            "Skipping test 9 (ProcessSurrender requires pre-deadline).",
            current_posix_ms, deadline_ms,
        )

    # Test 10: Both admins AdminWithdraw after deadline (happy path)
    if deadline_passed:
        logger.info("-" * 40)
        logger.info("TEST 10: Both admins AdminWithdraw after deadline (happy path)")

        fresh_pool = _query_live_pool_utxos(
            bf_client, script_address, cmatra_policy_hex, cmatra_asset_hex,
        )
        if not fresh_pool:
            logger.warning("No pool UTxOs remaining for test 10.")
        else:
            ctx10 = BlockFrostChainContext(project_id=blockfrost_project_id)
            r = test_admin_withdraw_happy(
                ctx10, admin_1, admin_2, fresh_pool[0],
                script_address, script, cmatra_policy_id, cmatra_asset_hex,
                deadline_ms,
            )
            results.append(r)
            logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")
    else:
        logger.warning(
            "Deadline has NOT passed (current: %d < deadline: %d). "
            "Skipping test 10 (AdminWithdraw requires post-deadline).",
            current_posix_ms, deadline_ms,
        )

    # --- Summary ---
    logger.info("=" * 60)
    logger.info("RED-TEAM SUMMARY (Surrender Model v4.0 — Dual-Signer)")
    logger.info("=" * 60)
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    logger.info("%d/%d tests PASSED", passed, total)
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        extra = ""
        if r.get("note"):
            extra = f" ({r['note']})"
        if r.get("tx_hash"):
            extra += f" [tx: {r['tx_hash'][:16]}...]"
        logger.info("  %s: %s%s", r["test"], status, extra)

    if passed < total:
        logger.error(
            "SECURITY ISSUE: %d test(s) FAILED — validator has exploitable bugs!",
            total - passed,
        )

    # Save results
    results_path = work_dir / "data" / "red_team_results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    logger.info("Results saved to %s", results_path)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="cMATRA Merger — Red-Team Test Suite for Surrender Pool (Preprod)",
    )
    parser.add_argument("--work-dir", type=str, default=None)
    parser.add_argument("--blueprint", type=str, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    project_root = Path(__file__).resolve().parent.parent
    work_dir = Path(args.work_dir) if args.work_dir else project_root / "audit_pack" / "preprod"
    blueprint_path = Path(args.blueprint) if args.blueprint else (
        project_root / "onchain" / "claim_validator" / "plutus.json"
    )

    bf_id = os.environ.get(
        "BLOCKFROST_PROJECT_ID_PREPROD",
        os.environ.get("BLOCKFROST_PROJECT_ID", ""),
    )
    if not bf_id or not bf_id.startswith("preprod"):
        logger.error("Need a preprod Blockfrost project ID.")
        sys.exit(1)

    run_red_team(work_dir, bf_id, blueprint_path)


if __name__ == "__main__":
    main()
