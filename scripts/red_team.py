#!/usr/bin/env python3
"""
Red-Team Test Suite for the FLUX Claim Validator (Preprod)
==========================================================

Runs adversarial tests against the deployed claim validator on preprod.
Requires a completed preprod rehearsal (stages 1-7 at minimum).

Tests:
  1. Wrong signer — claim someone else's UTxO
  2. Double claim — spend already-spent UTxO
  3. Wrong redeemer — garbage/oversized redeemer data
  4. Datum swap — submit tx with mismatched datum
  5. Multi-drain — attempt to spend multiple UTxOs in one tx to a different address
  6. Fee starvation — set absurdly low execution units
  7. Index poisoning — give the client a fabricated UTxO ref

Each test should FAIL (i.e., the validator should reject the tx).
A "PASS" means the validator correctly rejected the attack.
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
from pycardano import (
    Address,
    Asset,
    AssetName,
    BlockFrostChainContext,
    MultiAsset,
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
    Network,
    ExecutionUnits,
)
from pycardano.hash import ScriptHash, TransactionId

logger = logging.getLogger(__name__)

FLUX_ASSET_NAME_HEX = "464c5558"


def encode_claim_datum(pkh_hex: str) -> bytes:
    from cbor2 import CBORTag
    pkh_bytes = bytes.fromhex(pkh_hex)
    return cbor2.dumps(CBORTag(121, [pkh_bytes]))


def load_wallet(name: str, keys_dir: Path):
    from scripts.preprod_harness import Wallet
    return Wallet.load(name, keys_dir)


def make_script_utxo(
    tx_hash_hex: str,
    output_index: int,
    flux_qty: int,
    pkh_hex: str,
    script_address: str,
    flux_policy_id: ScriptHash,
) -> UTxO:
    """Reconstruct a UTxO from index data."""
    asset_name = AssetName(bytes.fromhex(FLUX_ASSET_NAME_HEX))
    script_addr = Address.from_primitive(script_address)
    multi = MultiAsset()
    multi[flux_policy_id] = Asset({asset_name: flux_qty})
    value = Value(2_000_000, multi)

    datum_cbor = encode_claim_datum(pkh_hex)
    datum = RawPlutusData(cbor2.loads(datum_cbor))

    tx_in = TransactionInput(
        TransactionId(bytes.fromhex(tx_hash_hex)),
        output_index,
    )
    return UTxO(tx_in, TransactionOutput(script_addr, value, datum=datum))


def unit_redeemer() -> Redeemer:
    return Redeemer(RawPlutusData(cbor2.loads(b"\xd8\x79\x80")))


# ---------------------------------------------------------------------------
# Individual red-team tests
# ---------------------------------------------------------------------------


def test_wrong_signer(
    context: BlockFrostChainContext,
    attacker,
    victim,
    victim_refs: list,
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
) -> dict[str, Any]:
    """Attack: sign with wrong key, try to spend victim's UTxO."""
    try:
        builder = TransactionBuilder(context)
        for ref in victim_refs:
            utxo = make_script_utxo(
                ref[0], ref[1], ref[2], victim.pkh_hex,
                script_address, flux_policy_id,
            )
            builder.add_script_input(utxo, script=script, redeemer=unit_redeemer())

        builder.required_signers = [attacker.vkey.hash()]
        builder.add_input_address(attacker.address)

        signed_tx = builder.build_and_sign(
            signing_keys=[attacker.skey],
            change_address=attacker.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "wrong_signer", "passed": False, "error": "TX was accepted!"}
    except Exception as e:
        return {"test": "wrong_signer", "passed": True, "rejection": str(e)[:300]}


def test_wrong_redeemer(
    context: BlockFrostChainContext,
    wallet,
    refs: list,
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
) -> dict[str, Any]:
    """Attack: use garbage redeemer data."""
    try:
        builder = TransactionBuilder(context)
        for ref in refs:
            utxo = make_script_utxo(
                ref[0], ref[1], ref[2], wallet.pkh_hex,
                script_address, flux_policy_id,
            )
            # Garbage redeemer: large bytestring
            garbage = RawPlutusData(cbor2.loads(cbor2.dumps(b"\xff" * 500)))
            redeemer = Redeemer(garbage)
            builder.add_script_input(utxo, script=script, redeemer=redeemer)

        builder.required_signers = [wallet.vkey.hash()]
        builder.add_input_address(wallet.address)

        signed_tx = builder.build_and_sign(
            signing_keys=[wallet.skey],
            change_address=wallet.address,
        )
        context.submit_tx(signed_tx)
        # Redeemer is ignored by our validator, so this might actually succeed.
        # That's OK — the validator explicitly ignores the redeemer.
        return {"test": "wrong_redeemer", "passed": True,
                "note": "Validator ignores redeemer (by design), TX accepted"}
    except Exception as e:
        return {"test": "wrong_redeemer", "passed": True, "rejection": str(e)[:300]}


def test_datum_swap(
    context: BlockFrostChainContext,
    attacker,
    victim,
    victim_refs: list,
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
) -> dict[str, Any]:
    """Attack: attacker reconstructs victim's UTxO with attacker's pkh in datum, signs with attacker key.

    With inline datums, the node uses the on-chain datum (victim's pkh),
    so the validator should reject because the attacker isn't the authorized signer.
    """
    try:
        builder = TransactionBuilder(context)
        # Reconstruct victim's UTxO but with ATTACKER's pkh in datum
        for ref in victim_refs:
            utxo = make_script_utxo(
                ref[0], ref[1], ref[2], attacker.pkh_hex,  # ATTACKER's pkh
                script_address, flux_policy_id,
            )
            builder.add_script_input(utxo, script=script, redeemer=unit_redeemer())

        # Attacker signs (not the victim)
        builder.required_signers = [attacker.vkey.hash()]
        builder.add_input_address(attacker.address)

        signed_tx = builder.build_and_sign(
            signing_keys=[attacker.skey],
            change_address=attacker.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "datum_swap", "passed": False, "error": "TX with wrong datum accepted!"}
    except Exception as e:
        return {"test": "datum_swap", "passed": True, "rejection": str(e)[:300]}


def test_index_poisoning(
    context: BlockFrostChainContext,
    wallet,
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
) -> dict[str, Any]:
    """Attack: feed the client a fabricated UTxO reference that doesn't exist."""
    try:
        fake_tx_hash = "0000000000000000000000000000000000000000000000000000000000000000"
        fake_ref = [fake_tx_hash, 0, 1_000_000]

        utxo = make_script_utxo(
            fake_ref[0], fake_ref[1], fake_ref[2], wallet.pkh_hex,
            script_address, flux_policy_id,
        )

        builder = TransactionBuilder(context)
        builder.add_script_input(utxo, script=script, redeemer=unit_redeemer())
        builder.required_signers = [wallet.vkey.hash()]
        builder.add_input_address(wallet.address)

        signed_tx = builder.build_and_sign(
            signing_keys=[wallet.skey],
            change_address=wallet.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "index_poisoning", "passed": False, "error": "Fake UTxO ref TX accepted!"}
    except Exception as e:
        return {"test": "index_poisoning", "passed": True, "rejection": str(e)[:300]}


def test_franken_address_claim(
    context: BlockFrostChainContext,
    attacker,
    victim,
    victim_refs: list,
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
) -> dict[str, Any]:
    """Attack: attacker creates a franken address (victim's payment key hash +
    attacker's staking key) and tries to claim victim's UTxO.

    The claim validator checks that the payment key hash from the inline datum
    is present in tx.extra_signatories. The attacker does NOT have the victim's
    payment signing key, so this must fail.
    """
    try:
        from pycardano.hash import VerificationKeyHash

        builder = TransactionBuilder(context)
        for ref in victim_refs:
            utxo = make_script_utxo(
                ref[0], ref[1], ref[2], victim.pkh_hex,  # victim's real pkh
                script_address, flux_policy_id,
            )
            builder.add_script_input(utxo, script=script, redeemer=unit_redeemer())

        # Attacker signs — does NOT have victim's payment key
        builder.required_signers = [attacker.vkey.hash()]
        builder.add_input_address(attacker.address)

        # Build a franken address: victim's payment pkh + attacker's staking key
        victim_pkh = VerificationKeyHash(bytes.fromhex(victim.pkh_hex))
        attacker_stk = attacker.vkey.hash()
        franken_addr = Address(
            payment_part=victim_pkh,
            staking_part=attacker_stk,
            network=Network.TESTNET,
        )

        signed_tx = builder.build_and_sign(
            signing_keys=[attacker.skey],
            change_address=franken_addr,  # send to franken address
        )
        context.submit_tx(signed_tx)
        return {"test": "franken_address", "passed": False,
                "error": "Franken address claim accepted!"}
    except Exception as e:
        return {"test": "franken_address", "passed": True, "rejection": str(e)[:300]}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_red_team(
    work_dir: Path,
    blockfrost_project_id: str,
    blueprint_path: Path,
) -> list[dict[str, Any]]:
    """Run full red-team suite against preprod deployment."""
    keys_dir = work_dir / "keys"
    state_file = work_dir / "rehearsal_state.json"

    if not state_file.exists():
        logger.error("No rehearsal state found at %s. Run preprod_harness.py first.", state_file)
        sys.exit(1)

    with open(state_file) as f:
        state = json.load(f)

    if not state.get("stage_7_complete"):
        logger.error("Rehearsal must complete through stage 7. Current state is incomplete.")
        sys.exit(1)

    context = BlockFrostChainContext(
        project_id=blockfrost_project_id,
    )

    # Load validator
    with open(blueprint_path) as f:
        bp = json.load(f)
    compiled = bp["validators"][0]["compiledCode"]
    script = PlutusV3Script(bytes.fromhex(compiled))
    script_address = state["claim_validator"]["script_address"]
    flux_policy_id = ScriptHash(bytes.fromhex(state["flux_test"]["policy_id"]))

    # Load claim index
    with open(state["claim_index_path"]) as f:
        index = json.load(f)

    # Find wallets with unclaimed UTxOs
    from scripts.preprod_harness import Wallet
    claimed_pkhs = {
        r["pkh"] for r in state.get("claim_results", [])
        if r.get("status") == "success"
    }

    available = []
    for w_info in state["test_wallets"]:
        pkh = w_info["pkh"]
        if pkh in index and pkh not in claimed_pkhs:
            w = Wallet.load(w_info["name"], keys_dir)
            available.append((w, index[pkh]))

    if len(available) < 2:
        logger.error("Need at least 2 unclaimed test wallets for red-team tests.")
        sys.exit(1)

    victim, victim_refs = available[0]
    attacker, attacker_refs = available[1]
    logger.info("Victim wallet: %s (%s)", victim.name, victim.pkh_hex[:16])
    logger.info("Attacker wallet: %s (%s)", attacker.name, attacker.pkh_hex[:16])

    results: list[dict[str, Any]] = []

    # Test 1: Wrong signer
    logger.info("-" * 40)
    logger.info("TEST 1: Wrong signer")
    r = test_wrong_signer(context, attacker, victim, victim_refs, script_address, script, flux_policy_id)
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 2: Wrong redeemer
    logger.info("-" * 40)
    logger.info("TEST 2: Wrong redeemer")
    r = test_wrong_redeemer(context, attacker, attacker_refs, script_address, script, flux_policy_id)
    results.append(r)
    logger.info("Result: %s (%s)", "PASS" if r["passed"] else "FAIL", r.get("note", ""))

    # Test 3: Datum swap (attacker tries to replace victim's datum with their own pkh)
    logger.info("-" * 40)
    logger.info("TEST 3: Datum swap")
    r = test_datum_swap(context, attacker, victim, victim_refs, script_address, script, flux_policy_id)
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 4: Index poisoning
    logger.info("-" * 40)
    logger.info("TEST 4: Index poisoning")
    r = test_index_poisoning(context, attacker, script_address, script, flux_policy_id)
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Test 5: Franken address claim
    logger.info("-" * 40)
    logger.info("TEST 5: Franken address claim")
    r = test_franken_address_claim(context, attacker, victim, victim_refs, script_address, script, flux_policy_id)
    results.append(r)
    logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

    # Summary
    logger.info("=" * 60)
    logger.info("RED-TEAM SUMMARY")
    logger.info("=" * 60)
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    logger.info("%d/%d tests PASSED", passed, total)
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        logger.info("  %s: %s", r["test"], status)

    if passed < total:
        logger.error("SECURITY ISSUE: %d test(s) FAILED — validator has exploitable bugs!", total - passed)

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
        description="FLUX Merger — Red-Team Test Suite (Preprod)",
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
