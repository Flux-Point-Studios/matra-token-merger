#!/usr/bin/env python3
"""
Phase 6 — Build Claim-Vault UTxOs (Distribution Transactions)

Reads the allocations CSV and creates batched transactions that lock FLUX
tokens at the claim validator script address with inline datums encoding
each claimant's payment key hash.

Outputs:
  - CBOR tx files per batch
  - manifest.json with txids + claimant list + quantities
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any

from pycardano import (
    Address,
    MultiAsset,
    PaymentSigningKey,
    PaymentVerificationKey,
    ScriptHash,
    Transaction,
    TransactionBody,
    TransactionBuilder,
    TransactionInput,
    TransactionOutput,
    TransactionWitnessSet,
    UTxO,
    Value,
    Withdrawals,
)

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import encode_claim_datum, estimate_min_ada
from tools.config import FLUX_DECIMALS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allocation loading
# ---------------------------------------------------------------------------


def load_allocations(csv_path: Path) -> list[dict[str, Any]]:
    """Load allocations from CSV into a list of dicts."""
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            flux_units = int(r["flux_total_units"])
            if flux_units <= 0:
                continue
            rows.append({
                "payment_key_hash_hex": r["payment_key_hash_hex"],
                "flux_units": flux_units,
                "addresses": r.get("addresses", ""),
            })
    return rows


# ---------------------------------------------------------------------------
# Batch builder
# ---------------------------------------------------------------------------


def build_claim_outputs(
    allocations: list[dict[str, Any]],
    script_address: str,
    flux_policy_hex: str,
    flux_asset_hex: str,
    coins_per_utxo_byte: int = 4310,
) -> list[dict[str, Any]]:
    """Build TransactionOutput descriptors for each allocation.

    Returns a list of dicts with the output details for manifest tracking.
    """
    outputs = []
    for alloc in allocations:
        pkh = alloc["payment_key_hash_hex"]
        flux_qty = alloc["flux_units"]

        # Inline datum CBOR
        datum_cbor = encode_claim_datum(pkh)

        # Estimate min ADA
        min_ada = estimate_min_ada(num_assets=1, datum_size_bytes=len(datum_cbor))

        outputs.append({
            "payment_key_hash_hex": pkh,
            "flux_units": flux_qty,
            "min_ada_lovelace": min_ada,
            "datum_cbor_hex": datum_cbor.hex(),
            "script_address": script_address,
            "flux_policy_hex": flux_policy_hex,
            "flux_asset_hex": flux_asset_hex,
        })
    return outputs


def batch_outputs(
    outputs: list[dict[str, Any]],
    batch_size: int = 40,
) -> list[list[dict[str, Any]]]:
    """Split outputs into batches of *batch_size*."""
    batches = []
    for i in range(0, len(outputs), batch_size):
        batches.append(outputs[i : i + batch_size])
    return batches


def build_batch_tx_cbor(
    bf: BlockfrostClient,
    batch: list[dict[str, Any]],
    funding_address: str,
    funding_skey_path: str,
    script_address: str,
    flux_policy_hex: str,
    flux_asset_hex: str,
) -> dict[str, Any]:
    """Build, sign, and return a transaction CBOR for one batch.

    Returns dict with tx_cbor_hex, tx_hash, outputs metadata.
    """
    from pycardano import (
        BlockFrostChainContext,
        RawPlutusData,
    )
    from pycardano.hash import ScriptHash as PycScriptHash
    from pycardano import AssetName
    import cbor2

    # Set up chain context
    context = BlockFrostChainContext(
        project_id=bf.project_id,
        base_url=bf.base_url,
    )

    sk = PaymentSigningKey.load(funding_skey_path)
    vk = PaymentVerificationKey.from_signing_key(sk)
    funding_addr = Address.from_primitive(funding_address)

    builder = TransactionBuilder(context)
    builder.add_input_address(funding_addr)

    # Build outputs
    policy_id = PycScriptHash(bytes.fromhex(flux_policy_hex))
    asset_name = AssetName(bytes.fromhex(flux_asset_hex))
    script_addr = Address.from_primitive(script_address)

    output_details = []
    for out in batch:
        datum_bytes = bytes.fromhex(out["datum_cbor_hex"])
        datum = RawPlutusData(cbor2.loads(datum_bytes))

        multi = MultiAsset()
        multi[policy_id] = {asset_name: out["flux_units"]}
        value = Value(out["min_ada_lovelace"], multi)

        tx_out = TransactionOutput(script_addr, value, datum=datum)
        builder.add_output(tx_out)

        output_details.append({
            "payment_key_hash_hex": out["payment_key_hash_hex"],
            "flux_units": out["flux_units"],
            "min_ada_lovelace": out["min_ada_lovelace"],
        })

    # Build and sign
    signed_tx = builder.build_and_sign(
        signing_keys=[sk],
        change_address=funding_addr,
    )

    tx_cbor = signed_tx.to_cbor()
    tx_hash = signed_tx.id.payload.hex()

    return {
        "tx_hash": tx_hash,
        "tx_cbor_hex": tx_cbor.hex() if isinstance(tx_cbor, bytes) else tx_cbor,
        "num_outputs": len(output_details),
        "outputs": output_details,
        "signed_tx": signed_tx,
    }


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def preflight_evaluate_tx(
    bf: BlockfrostClient,
    signed_tx,
    batch_label: str = "",
) -> dict[str, Any]:
    """Run evaluate_tx (Blockfrost /utils/txs/evaluate) as preflight check.

    Returns evaluation result dict. Logs warnings on failure.
    """
    from pycardano import BlockFrostChainContext

    context = BlockFrostChainContext(
        project_id=bf.project_id,
        base_url=bf.base_url,
    )

    try:
        tx_cbor = signed_tx.to_cbor()
        cbor_bytes = tx_cbor if isinstance(tx_cbor, bytes) else bytes.fromhex(tx_cbor)
        result = context.evaluate_tx(cbor_bytes)
        logger.info("Preflight %s: evaluation OK — %s", batch_label, result)
        return {"status": "ok", "result": str(result)}
    except Exception as e:
        logger.warning("Preflight %s: evaluation FAILED — %s", batch_label, e)
        return {"status": "failed", "error": str(e)[:500]}


def build_claim_vault(
    bf: BlockfrostClient,
    allocations_csv: Path,
    funding_address: str,
    funding_skey_path: str,
    script_address: str,
    flux_policy_hex: str,
    flux_asset_hex: str = "464c5558",
    batch_size: int = 40,
    out_dir: Path | None = None,
    submit: bool = False,
    preflight: bool = False,
) -> dict[str, Any]:
    """Build all claim vault transactions and optionally submit them."""
    allocs = load_allocations(allocations_csv)
    logger.info("Loaded %d allocations from %s", len(allocs), allocations_csv)

    outputs = build_claim_outputs(
        allocs, script_address, flux_policy_hex, flux_asset_hex,
    )
    batches = batch_outputs(outputs, batch_size)
    logger.info("Split into %d batches (max %d outputs each)", len(batches), batch_size)

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    manifest_entries: list[dict[str, Any]] = []
    total_flux = 0
    total_ada = 0

    for i, batch in enumerate(batches):
        logger.info("Building batch %d/%d (%d outputs)...", i + 1, len(batches), len(batch))

        result = build_batch_tx_cbor(
            bf, batch, funding_address, funding_skey_path,
            script_address, flux_policy_hex, flux_asset_hex,
        )

        # Preflight evaluation
        if preflight and result.get("signed_tx"):
            eval_result = preflight_evaluate_tx(
                bf, result["signed_tx"], f"batch_{i}",
            )
            if eval_result["status"] == "failed":
                logger.error(
                    "Preflight FAILED for batch %d — aborting. Error: %s",
                    i, eval_result["error"],
                )
                raise RuntimeError(f"Preflight failed for batch {i}: {eval_result['error']}")

        # Save CBOR
        if out_dir:
            cbor_path = out_dir / f"batch_{i:04d}.cbor"
            cbor_bytes = bytes.fromhex(result["tx_cbor_hex"])
            with open(cbor_path, "wb") as f:
                f.write(cbor_bytes)

        # Submit if requested
        if submit:
            cbor_bytes = bytes.fromhex(result["tx_cbor_hex"])
            submitted_hash = bf.submit_tx(cbor_bytes)
            logger.info("Submitted batch %d: %s", i, submitted_hash)

        batch_flux = sum(o["flux_units"] for o in result["outputs"])
        batch_ada = sum(o["min_ada_lovelace"] for o in result["outputs"])
        total_flux += batch_flux
        total_ada += batch_ada

        manifest_entries.append({
            "batch_index": i,
            "tx_hash": result["tx_hash"],
            "num_outputs": result["num_outputs"],
            "total_flux_units": batch_flux,
            "total_ada_lovelace": batch_ada,
            "claimants": [
                {
                    "payment_key_hash_hex": o["payment_key_hash_hex"],
                    "flux_units": o["flux_units"],
                }
                for o in result["outputs"]
            ],
        })

    manifest = {
        "manifest_type": "claim_vault",
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "script_address": script_address,
        "flux_policy_hex": flux_policy_hex,
        "flux_asset_hex": flux_asset_hex,
        "totals": {
            "num_batches": len(manifest_entries),
            "num_claimants": len(allocs),
            "total_flux_units": total_flux,
            "total_ada_lovelace": total_ada,
        },
        "batches": manifest_entries,
    }

    if out_dir:
        manifest_path = out_dir / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        logger.info("Manifest written to %s", manifest_path)

    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX merger — Phase 6: Build Claim Vault UTxOs",
    )
    parser.add_argument("--allocations-csv", type=str, required=True)
    parser.add_argument("--funding-address", type=str, required=True)
    parser.add_argument("--funding-skey", type=str, required=True)
    parser.add_argument("--script-address", type=str, required=True)
    parser.add_argument("--flux-policy", type=str, required=True)
    parser.add_argument("--flux-asset-hex", type=str, default="464c5558")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--out-dir", type=str, required=True)
    parser.add_argument("--submit", action="store_true", default=False)
    parser.add_argument("--preflight", action="store_true", default=False,
                        help="Run evaluate_tx preflight check before submitting")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    bf = BlockfrostClient()
    build_claim_vault(
        bf,
        allocations_csv=Path(args.allocations_csv),
        funding_address=args.funding_address,
        funding_skey_path=args.funding_skey,
        script_address=args.script_address,
        flux_policy_hex=args.flux_policy,
        flux_asset_hex=args.flux_asset_hex,
        batch_size=args.batch_size,
        out_dir=Path(args.out_dir),
        submit=args.submit,
        preflight=args.preflight,
    )


if __name__ == "__main__":
    main()
