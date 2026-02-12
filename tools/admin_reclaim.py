#!/usr/bin/env python3
"""
Admin Reclaim — sweep unclaimed UTxOs after the claim deadline.

Modes:
  --check-only: report unclaimed UTxOs at the script address
  --submit:     build reclaim tx(es) and submit

The claim validator allows admin reclaim when:
  1. Admin PKH is in extra_signatories
  2. tx.validity_range is entirely after the deadline
     (i.e., invalid_before > deadline)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
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
)
from pycardano.hash import ScriptHash, TransactionId, VerificationKeyHash

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import (
    decode_claim_datum,
    encode_claim_datum,
    posix_ms_to_slot,
)
from tools.config import ADMIN_PKH, CLAIM_DEADLINE_POSIX_MS, NETWORK

logger = logging.getLogger(__name__)

FLUX_ASSET_NAME_HEX = "464c5558"


def discover_unclaimed_utxos(
    bf: BlockfrostClient,
    script_address: str,
    flux_policy_hex: str,
) -> list[dict[str, Any]]:
    """Query script address for all current UTxOs (unclaimed)."""
    utxos = bf.get_address_utxos(script_address)
    results: list[dict[str, Any]] = []

    for u in utxos:
        # Extract FLUX quantity
        flux_qty = 0
        ada_qty = 0
        for amt in u.get("amount", []):
            if amt["unit"] == "lovelace":
                ada_qty = int(amt["quantity"])
            elif amt["unit"].startswith(flux_policy_hex):
                flux_qty = int(amt["quantity"])

        # Decode datum to get claimant PKH
        datum_pkh = None
        inline_datum = u.get("inline_datum")
        if inline_datum:
            try:
                datum_pkh = decode_claim_datum(inline_datum)
            except Exception:
                pass

        results.append({
            "tx_hash": u["tx_hash"],
            "output_index": u["output_index"],
            "ada_lovelace": ada_qty,
            "flux_units": flux_qty,
            "datum_pkh": datum_pkh,
        })

    return results


def build_reclaim_tx(
    context: BlockFrostChainContext,
    admin_skey: PaymentSigningKey,
    admin_address: Address,
    script: PlutusV3Script,
    script_address: str,
    unclaimed: list[dict[str, Any]],
    flux_policy_hex: str,
    deadline_posix_ms: int,
) -> bytes:
    """Build a reclaim transaction sweeping unclaimed UTxOs to admin.

    Sets invalid_before to a slot past the deadline so the validator's
    is_entirely_after check passes.
    """
    asset_name = AssetName(bytes.fromhex(FLUX_ASSET_NAME_HEX))
    script_addr = Address.from_primitive(script_address)
    flux_policy_id = ScriptHash(bytes.fromhex(flux_policy_hex))

    builder = TransactionBuilder(context)

    for u in unclaimed:
        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(u["tx_hash"])),
            u["output_index"],
        )

        # Reconstruct UTxO value
        multi = MultiAsset()
        if u["flux_units"] > 0:
            multi[flux_policy_id] = Asset({asset_name: u["flux_units"]})
        value = Value(u["ada_lovelace"], multi)

        # Reconstruct datum (even though validator doesn't strictly need it
        # for the admin path, pycardano needs to match the on-chain UTxO)
        if u["datum_pkh"]:
            datum_cbor = encode_claim_datum(u["datum_pkh"])
            datum = RawPlutusData(cbor2.loads(datum_cbor))
        else:
            datum = RawPlutusData(cbor2.loads(b"\xd8\x79\x80"))

        utxo = UTxO(tx_in, TransactionOutput(script_addr, value, datum=datum))
        redeemer = Redeemer(RawPlutusData(cbor2.loads(b"\xd8\x79\x80")))

        builder.add_script_input(utxo, script=script, redeemer=redeemer)

    # Admin must be in extra_signatories
    admin_vk = PaymentVerificationKey.from_signing_key(admin_skey)
    builder.required_signers = [admin_vk.hash()]

    # Set validity_start to a slot AFTER the deadline
    # This ensures is_entirely_after(validity_range, deadline) passes
    deadline_slot = posix_ms_to_slot(deadline_posix_ms, NETWORK)
    builder.validity_start = deadline_slot + 1

    # Add admin address for collateral + change
    builder.add_input_address(admin_address)

    signed_tx = builder.build_and_sign(
        signing_keys=[admin_skey],
        change_address=admin_address,
    )

    return signed_tx.to_cbor()


def run_admin_reclaim(
    bf: BlockfrostClient,
    admin_skey_path: str,
    script_address: str,
    script_cbor_hex: str,
    flux_policy_hex: str,
    deadline_posix_ms: int,
    check_only: bool = False,
    submit: bool = False,
    batch_size: int = 20,
) -> dict[str, Any]:
    """Run the admin reclaim workflow."""
    # Discover unclaimed
    unclaimed = discover_unclaimed_utxos(bf, script_address, flux_policy_hex)

    total_flux = sum(u["flux_units"] for u in unclaimed)
    total_ada = sum(u["ada_lovelace"] for u in unclaimed)

    deadline_utc = datetime.fromtimestamp(
        deadline_posix_ms / 1000, tz=timezone.utc
    ).isoformat()

    logger.info("Script address: %s", script_address)
    logger.info("Deadline: %s (POSIX ms: %d)", deadline_utc, deadline_posix_ms)
    logger.info("Unclaimed UTxOs: %d", len(unclaimed))
    logger.info("Total FLUX: %d base units", total_flux)
    logger.info("Total ADA locked: %.2f", total_ada / 1_000_000)

    report: dict[str, Any] = {
        "script_address": script_address,
        "deadline_posix_ms": deadline_posix_ms,
        "deadline_utc": deadline_utc,
        "unclaimed_count": len(unclaimed),
        "total_flux_units": total_flux,
        "total_ada_lovelace": total_ada,
    }

    if check_only:
        report["mode"] = "check_only"
        report["unclaimed"] = unclaimed
        return report

    if not unclaimed:
        logger.info("No unclaimed UTxOs to reclaim.")
        report["mode"] = "no_action"
        return report

    # Load admin key
    admin_sk = PaymentSigningKey.load(admin_skey_path)
    admin_vk = PaymentVerificationKey.from_signing_key(admin_sk)
    admin_addr = Address(
        payment_part=admin_vk.hash(),
        network=Address.from_primitive(script_address).network,
    )
    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))

    # Batch the reclaim
    num_batches = math.ceil(len(unclaimed) / batch_size)
    tx_hashes: list[str] = []

    for i in range(num_batches):
        batch = unclaimed[i * batch_size : (i + 1) * batch_size]
        logger.info("Building reclaim batch %d/%d (%d UTxOs)...", i + 1, num_batches, len(batch))

        context = BlockFrostChainContext(
            project_id=bf.project_id,
            base_url=bf.base_url,
        )

        tx_cbor = build_reclaim_tx(
            context, admin_sk, admin_addr, script, script_address,
            batch, flux_policy_hex, deadline_posix_ms,
        )

        if submit:
            cbor_bytes = tx_cbor if isinstance(tx_cbor, bytes) else bytes.fromhex(tx_cbor)
            tx_hash = bf.submit_tx(cbor_bytes)
            logger.info("Reclaim batch %d submitted: %s", i, tx_hash)
            tx_hashes.append(tx_hash)
        else:
            logger.info("Reclaim batch %d built (not submitted).", i)

    report["mode"] = "submit" if submit else "dry_run"
    report["num_batches"] = num_batches
    report["tx_hashes"] = tx_hashes
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX merger — Admin Reclaim (post-deadline sweep)",
    )
    parser.add_argument("--admin-skey", type=str, required=True,
                        help="Path to admin payment signing key")
    parser.add_argument("--script-address", type=str, required=True)
    parser.add_argument("--blueprint", type=str, required=True,
                        help="Path to Aiken plutus.json blueprint")
    parser.add_argument("--flux-policy", type=str, required=True)
    parser.add_argument("--deadline-posix-ms", type=int, default=None,
                        help="Override deadline (default: from CLAIM_DEADLINE_POSIX_MS env)")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--check-only", action="store_true", default=False,
                        help="Report unclaimed UTxOs without building txs")
    parser.add_argument("--submit", action="store_true", default=False,
                        help="Build and submit reclaim txs")
    parser.add_argument("--out-json", type=str, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    deadline = args.deadline_posix_ms or CLAIM_DEADLINE_POSIX_MS
    if deadline == 0:
        logger.error("No deadline set. Use --deadline-posix-ms or set CLAIM_DEADLINE_POSIX_MS.")
        sys.exit(1)

    # Load script from blueprint
    from tools.claim_flux_indexed import load_script_from_blueprint
    script_cbor_hex = load_script_from_blueprint(args.blueprint)

    bf = BlockfrostClient()

    report = run_admin_reclaim(
        bf,
        admin_skey_path=args.admin_skey,
        script_address=args.script_address,
        script_cbor_hex=script_cbor_hex,
        flux_policy_hex=args.flux_policy,
        deadline_posix_ms=deadline,
        check_only=args.check_only,
        submit=args.submit,
        batch_size=args.batch_size,
    )

    print(json.dumps(report, indent=2))

    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Report written to %s", out_path)


if __name__ == "__main__":
    main()
