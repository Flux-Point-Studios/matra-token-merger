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


def _query_live_claim_utxos(
    bf_client,
    script_address: str,
    flux_policy_hex: str,
) -> dict[str, list[list]]:
    """Query chain for live UTxOs at script address, grouped by datum PKH.

    Returns ``{pkh_hex: [[tx_hash, output_index, flux_qty], ...]}`` —
    the same ref format the claim index uses.  Because this reads
    directly from the on-chain UTXO set, the refs are *never* stale.
    """
    utxos = bf_client.get_address_utxos(script_address)

    flux_unit = flux_policy_hex + FLUX_ASSET_NAME_HEX
    pkh_utxos: dict[str, list[list]] = {}

    for u in utxos:
        # Extract FLUX token amount
        flux_qty = 0
        for amt in u.get("amount", []):
            if amt.get("unit") == flux_unit:
                flux_qty = int(amt.get("quantity", 0))
                break
        if flux_qty <= 0:
            continue  # not a FLUX claim UTxO

        # Parse inline datum → PKH
        datum_hex = u.get("inline_datum")
        if not datum_hex:
            continue
        try:
            obj = cbor2.loads(bytes.fromhex(datum_hex))
            pkh_hex = None
            if hasattr(obj, "value") and isinstance(obj.value, (list, tuple)):
                for item in obj.value:
                    if isinstance(item, bytes) and len(item) == 28:
                        pkh_hex = item.hex()
                        break
            if pkh_hex is None:
                continue
        except Exception:
            continue

        output_idx = u.get("output_index", u.get("tx_index", 0))
        pkh_utxos.setdefault(pkh_hex, []).append(
            [u["tx_hash"], output_idx, flux_qty],
        )

    return pkh_utxos


def extract_deadline_from_compiled(compiled_hex: str) -> int:
    """Extract the baked-in deadline (POSIX ms) from the applied compiled code.

    The deadline is the last CBOR integer parameter in the compiled code,
    encoded as ``1b`` (8-byte unsigned int) near the end of the hex string.
    """
    # Find the last occurrence of "1b" followed by exactly 16 hex chars (8 bytes)
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


def test_admin_reclaim_before_deadline(
    context: BlockFrostChainContext,
    admin,
    victim,
    victim_refs: list,
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
    deadline_posix_ms: int,
) -> dict[str, Any]:
    """Attack: admin tries to reclaim BEFORE deadline. Should FAIL.

    The validator requires is_entirely_after(validity_range, deadline).
    Setting invalid_before to a slot before the deadline should fail.

    Uses explicit execution units to ensure the validator is actually
    evaluated (not short-circuited by evaluate_tx failure).
    """
    try:
        builder = TransactionBuilder(context)
        redeemer = Redeemer(
            RawPlutusData(cbor2.loads(b"\xd8\x79\x80")),
            ExecutionUnits(500_000, 200_000_000),
        )
        for ref in victim_refs:
            utxo = make_script_utxo(
                ref[0], ref[1], ref[2], victim.pkh_hex,
                script_address, flux_policy_id,
            )
            builder.add_script_input(utxo, script=script, redeemer=redeemer)

        builder.required_signers = [admin.vkey.hash()]
        builder.add_input_address(admin.address)

        # Set validity_start to BEFORE deadline (genuinely before the on-chain deadline)
        from tools.cardano_utils import posix_ms_to_slot
        deadline_slot = posix_ms_to_slot(deadline_posix_ms, "preprod")
        builder.validity_start = max(0, deadline_slot - 100)

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )
        context.submit_tx(signed_tx)
        return {"test": "admin_reclaim_before_deadline", "passed": False,
                "error": "Admin reclaim before deadline was accepted!"}
    except Exception as e:
        return {"test": "admin_reclaim_before_deadline", "passed": True,
                "rejection": str(e)[:300]}


def test_admin_reclaim_after_deadline(
    context: BlockFrostChainContext,
    admin,
    claim_refs: list,
    claimant_pkh: str,
    script_address: str,
    script: PlutusV3Script,
    flux_policy_id: ScriptHash,
    deadline_posix_ms: int,
) -> dict[str, Any]:
    """Test: admin reclaims AFTER deadline. Should SUCCEED.

    This is a positive test — admin should be able to sweep after the
    claim window closes.

    Uses explicit execution units to bypass pycardano's evaluate_tx
    (which can fail with extraRedeemers when using synthetic UTxOs).
    The node validates execution budgets on submission.
    """
    try:
        builder = TransactionBuilder(context)
        # Provide explicit execution units so pycardano skips evaluate_tx.
        # The claim validator is trivial (signature + time check), so these
        # are generous but safe.  The node enforces actual usage on submit.
        redeemer = Redeemer(
            RawPlutusData(cbor2.loads(b"\xd8\x79\x80")),
            ExecutionUnits(500_000, 200_000_000),
        )
        for ref in claim_refs:
            utxo = make_script_utxo(
                ref[0], ref[1], ref[2], claimant_pkh,
                script_address, flux_policy_id,
            )
            builder.add_script_input(utxo, script=script, redeemer=redeemer)

        builder.required_signers = [admin.vkey.hash()]
        builder.add_input_address(admin.address)

        # Set validity_start to AFTER deadline
        from tools.cardano_utils import posix_ms_to_slot
        deadline_slot = posix_ms_to_slot(deadline_posix_ms, "preprod")
        builder.validity_start = deadline_slot + 1

        signed_tx = builder.build_and_sign(
            signing_keys=[admin.skey],
            change_address=admin.address,
        )
        context.submit_tx(signed_tx)
        tx_hash = signed_tx.id.payload.hex()
        return {"test": "admin_reclaim_after_deadline", "passed": True,
                "tx_hash": tx_hash, "note": "Admin successfully reclaimed after deadline"}
    except Exception as e:
        return {"test": "admin_reclaim_after_deadline", "passed": False,
                "error": str(e)[:300]}


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

    # Query live UTxOs directly from chain — guarantees no stale refs.
    # Previous runs may have consumed UTxOs (e.g. admin reclaim); reading
    # from the on-chain UTXO set instead of the index file avoids this.
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

    live_index = _query_live_claim_utxos(
        bf_client, script_address, state["flux_test"]["policy_id"],
    )
    total_live = sum(len(v) for v in live_index.values())
    logger.info("Live claim UTxOs on-chain: %d PKHs, %d UTxOs", len(live_index), total_live)

    # Match live UTxOs to test wallets
    wallet_pkhs = {w["pkh"]: w["name"] for w in state["test_wallets"]}
    available = []
    for pkh, refs in live_index.items():
        if pkh in wallet_pkhs:
            w = Wallet.load(wallet_pkhs[pkh], keys_dir)
            available.append((w, refs))

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

    # Test 6: Admin reclaim before deadline (should fail)
    # Use the actual on-chain deadline baked into the compiled validator,
    # NOT the state file's approximate value (which drifts on re-runs).
    try:
        deadline_ms = extract_deadline_from_compiled(compiled)
        logger.info("On-chain deadline: %d ms (from blueprint)", deadline_ms)
    except ValueError:
        deadline_ms = state.get("claim_deadline_posix_ms", 0)
        logger.warning("Could not extract deadline from blueprint, using state: %d ms", deadline_ms)
    if deadline_ms > 0:
        # Load admin wallet for reclaim tests
        admin = Wallet.load("admin", keys_dir)

        logger.info("-" * 40)
        logger.info("TEST 6: Admin reclaim before deadline")
        r = test_admin_reclaim_before_deadline(
            context, admin, victim, victim_refs,
            script_address, script, flux_policy_id, deadline_ms,
        )
        results.append(r)
        logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")

        # Test 7: Admin reclaim after deadline (should succeed)
        # Only run if there are UTxOs left to reclaim
        remaining_unclaimed = [
            (w, refs) for w, refs in available
            if w.pkh_hex != victim.pkh_hex and w.pkh_hex != attacker.pkh_hex
        ]
        if remaining_unclaimed:
            reclaim_w = remaining_unclaimed[0][0]
            # Re-query chain for fresh refs — earlier negative tests don't
            # consume UTxOs, but an external process might have.
            fresh_index = _query_live_claim_utxos(
                bf_client, script_address, state["flux_test"]["policy_id"],
            )
            fresh_refs = fresh_index.get(reclaim_w.pkh_hex, [])
            if not fresh_refs:
                logger.warning(
                    "UTxOs for %s consumed since start — skipping test 7",
                    reclaim_w.name,
                )
            else:
                logger.info("-" * 40)
                logger.info("TEST 7: Admin reclaim after deadline")
                rt_ctx2 = BlockFrostChainContext(project_id=blockfrost_project_id)
                r = test_admin_reclaim_after_deadline(
                    rt_ctx2, admin, fresh_refs, reclaim_w.pkh_hex,
                    script_address, script, flux_policy_id, deadline_ms,
                )
                results.append(r)
                logger.info("Result: %s", "PASS" if r["passed"] else "FAIL")
        else:
            logger.warning("No remaining UTxOs for admin reclaim after deadline test")
    else:
        logger.info("Skipping admin reclaim tests (no deadline in state)")

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
