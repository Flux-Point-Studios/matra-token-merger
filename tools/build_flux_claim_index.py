#!/usr/bin/env python3
"""
Phase 7 — Build Claim Index (No-Scan Claims)

.. deprecated::
    This module implements the old per-claimant claim-index model.
    The surrender-and-redeem model (v3.0) does not use per-claimant indices.

Reads the claim vault manifest and queries Blockfrost for each transaction's
UTxOs. Matches script outputs by inline datum to claimant key hashes and
builds a lookup index: keyhash → [tx_hash, output_index, flux_units].

Outputs:
  - claim_index_min.json  (compact: keyhash → refs)
  - claim_index_full.json (verbose: includes warnings/unmatched)
  - claim_index.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import decode_claim_datum

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------


def build_index_from_manifest(
    bf: BlockfrostClient,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Build the claim index from a vault manifest.

    For each batch tx, fetches UTxO details from Blockfrost and matches
    script outputs to claimant key hashes via their inline datums.
    """
    script_address = manifest["script_address"]

    # Build expected mapping: tx_hash → list of expected claimants
    expected: dict[str, list[dict[str, Any]]] = {}
    for batch in manifest["batches"]:
        tx_hash = batch["tx_hash"]
        expected[tx_hash] = batch["claimants"]

    # keyhash → list of (tx_hash, output_index, flux_units)
    index: dict[str, list[list[Any]]] = {}

    # Tracking
    missing_keyhashes: list[str] = []
    mismatches: list[dict[str, Any]] = []
    unmatched_outputs: list[dict[str, Any]] = []
    matched_count = 0

    for tx_hash, claimants in expected.items():
        logger.info("Indexing tx %s (%d claimants)...", tx_hash[:16], len(claimants))

        try:
            tx_utxos = bf.get_tx_utxos(tx_hash)
        except Exception as e:
            logger.error("Failed to fetch UTxOs for %s: %s", tx_hash, e)
            for c in claimants:
                missing_keyhashes.append(c["payment_key_hash_hex"])
            continue

        # Build a set of expected keyhashes for this tx
        expected_pkhs = {c["payment_key_hash_hex"]: c["flux_units"] for c in claimants}
        found_pkhs: set[str] = set()

        outputs = tx_utxos.get("outputs", [])
        for out in outputs:
            # Only look at outputs to the script address
            if out.get("address") != script_address:
                continue

            # Extract inline datum
            inline_datum_hex = out.get("inline_datum")
            if inline_datum_hex is None:
                # Try data_hash fallback (shouldn't happen with inline datums)
                unmatched_outputs.append({
                    "tx_hash": tx_hash,
                    "output_index": out.get("output_index"),
                    "reason": "no_inline_datum",
                })
                continue

            try:
                pkh = decode_claim_datum(inline_datum_hex)
            except Exception as e:
                unmatched_outputs.append({
                    "tx_hash": tx_hash,
                    "output_index": out.get("output_index"),
                    "reason": f"datum_decode_error: {e}",
                })
                continue

            # Match to expected
            output_index = out.get("output_index")

            # Extract FLUX quantity from output amounts
            flux_policy = manifest["flux_policy_hex"]
            flux_asset = manifest["flux_asset_hex"]
            flux_unit = flux_policy + "." + flux_asset
            flux_qty = 0
            for amount in out.get("amount", []):
                if amount.get("unit") == flux_unit or amount.get("unit") == flux_policy + flux_asset:
                    flux_qty = int(amount["quantity"])
                    break

            if pkh in expected_pkhs:
                expected_qty = expected_pkhs[pkh]
                if flux_qty != expected_qty:
                    mismatches.append({
                        "tx_hash": tx_hash,
                        "output_index": output_index,
                        "pkh": pkh,
                        "expected_flux": expected_qty,
                        "actual_flux": flux_qty,
                    })

                if pkh not in index:
                    index[pkh] = []
                index[pkh].append([tx_hash, output_index, flux_qty])
                found_pkhs.add(pkh)
                matched_count += 1
            else:
                unmatched_outputs.append({
                    "tx_hash": tx_hash,
                    "output_index": output_index,
                    "pkh": pkh,
                    "reason": "pkh_not_in_expected",
                })

        # Check for missing expected
        for pkh in expected_pkhs:
            if pkh not in found_pkhs:
                missing_keyhashes.append(pkh)

    # Build minimal index
    index_min = {pkh: refs for pkh, refs in sorted(index.items())}

    # Build full index with diagnostics
    index_full = {
        "index_type": "flux_claim_index",
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "script_address": script_address,
        "flux_policy_hex": manifest["flux_policy_hex"],
        "flux_asset_hex": manifest["flux_asset_hex"],
        "stats": {
            "total_expected_claimants": sum(
                len(b["claimants"]) for b in manifest["batches"]
            ),
            "total_matched": matched_count,
            "total_keyhashes_indexed": len(index_min),
            "missing_keyhashes": len(missing_keyhashes),
            "mismatches": len(mismatches),
            "unmatched_outputs": len(unmatched_outputs),
        },
        "index": index_min,
        "diagnostics": {
            "missing_keyhashes": missing_keyhashes,
            "mismatches": mismatches,
            "unmatched_outputs": unmatched_outputs,
        },
    }

    return {
        "index_min": index_min,
        "index_full": index_full,
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def write_index_csv(index_min: dict[str, list], out_path: Path) -> None:
    """Write claim index to CSV."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["payment_key_hash_hex", "tx_hash", "output_index", "flux_units"])
        for pkh, refs in sorted(index_min.items()):
            for ref in refs:
                writer.writerow([pkh, ref[0], ref[1], ref[2]])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX merger — Phase 7: Build Claim Index",
    )
    parser.add_argument("--manifest", type=str, required=True)
    parser.add_argument("--out-min", type=str, required=True)
    parser.add_argument("--out-full", type=str, required=True)
    parser.add_argument("--out-csv", type=str, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    with open(args.manifest) as f:
        manifest = json.load(f)

    bf = BlockfrostClient()
    result = build_index_from_manifest(bf, manifest)

    # Write minimal index
    min_path = Path(args.out_min)
    min_path.parent.mkdir(parents=True, exist_ok=True)
    with open(min_path, "w") as f:
        json.dump(result["index_min"], f, indent=2)
    logger.info("Minimal index: %s", min_path)

    # Write full index
    full_path = Path(args.out_full)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    with open(full_path, "w") as f:
        json.dump(result["index_full"], f, indent=2)
    logger.info("Full index: %s", full_path)

    # Optional CSV
    if args.out_csv:
        csv_path = Path(args.out_csv)
        write_index_csv(result["index_min"], csv_path)
        logger.info("Index CSV: %s", csv_path)

    # Validate
    stats = result["index_full"]["stats"]
    if stats["missing_keyhashes"] > 0:
        logger.warning("%d missing keyhashes!", stats["missing_keyhashes"])
    if stats["mismatches"] > 0:
        logger.warning("%d quantity mismatches!", stats["mismatches"])
    if stats["unmatched_outputs"] > 0:
        logger.warning("%d unmatched outputs!", stats["unmatched_outputs"])
    if stats["missing_keyhashes"] == 0 and stats["mismatches"] == 0:
        logger.info("Index validation passed.")


if __name__ == "__main__":
    main()
