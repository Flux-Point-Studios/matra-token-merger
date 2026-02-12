#!/usr/bin/env python3
"""
Funding Calculator — estimate total ADA required for claim vault deployment.

Reads allocations CSV, computes per-UTxO min-ADA, batch count, fee
estimates, and outputs a grand total with safety margin.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

from tools.build_claim_utxos_flux import load_allocations
from tools.cardano_utils import encode_claim_datum, estimate_min_ada

logger = logging.getLogger(__name__)


def compute_funding_report(
    allocations: list[dict[str, Any]],
    batch_size: int = 40,
    fee_per_batch_lovelace: int = 500_000,
    safety_margin_pct: float = 10.0,
) -> dict[str, Any]:
    """Compute a full funding report from allocations.

    Returns a dict with per-UTxO details, batch estimates, and grand total.
    """
    per_utxo_details: list[dict[str, Any]] = []
    total_min_ada_lovelace = 0
    total_flux_units = 0

    for alloc in allocations:
        pkh = alloc["payment_key_hash_hex"]
        flux_qty = alloc["flux_units"]

        datum_cbor = encode_claim_datum(pkh)
        min_ada = estimate_min_ada(num_assets=1, datum_size_bytes=len(datum_cbor))

        per_utxo_details.append({
            "payment_key_hash_hex": pkh,
            "flux_units": flux_qty,
            "datum_size_bytes": len(datum_cbor),
            "min_ada_lovelace": min_ada,
        })

        total_min_ada_lovelace += min_ada
        total_flux_units += flux_qty

    num_claims = len(allocations)
    num_batches = math.ceil(num_claims / batch_size) if num_claims > 0 else 0
    total_fees_lovelace = num_batches * fee_per_batch_lovelace
    subtotal_lovelace = total_min_ada_lovelace + total_fees_lovelace
    margin_lovelace = int(subtotal_lovelace * safety_margin_pct / 100)
    grand_total_lovelace = subtotal_lovelace + margin_lovelace

    report = {
        "report_type": "funding_calculator",
        "num_claims": num_claims,
        "total_flux_units": total_flux_units,
        "batch_size": batch_size,
        "num_batches": num_batches,
        "locked_ada": {
            "total_min_ada_lovelace": total_min_ada_lovelace,
            "total_min_ada_display": total_min_ada_lovelace / 1_000_000,
        },
        "fees": {
            "fee_per_batch_lovelace": fee_per_batch_lovelace,
            "total_fees_lovelace": total_fees_lovelace,
            "total_fees_display": total_fees_lovelace / 1_000_000,
        },
        "safety_margin": {
            "pct": safety_margin_pct,
            "margin_lovelace": margin_lovelace,
            "margin_display": margin_lovelace / 1_000_000,
        },
        "grand_total": {
            "lovelace": grand_total_lovelace,
            "ada": grand_total_lovelace / 1_000_000,
        },
    }

    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX merger — Funding Calculator",
    )
    parser.add_argument("--allocations-csv", type=str, required=True,
                        help="Path to allocations_flux.csv")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--fee-per-batch", type=int, default=500_000,
                        help="Estimated fee per batch in lovelace (default: 500000)")
    parser.add_argument("--safety-margin", type=float, default=10.0,
                        help="Safety margin percentage (default: 10)")
    parser.add_argument("--out-json", type=str, default=None,
                        help="Write report to JSON file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    csv_path = Path(args.allocations_csv)
    if not csv_path.exists():
        logger.error("Allocations CSV not found: %s", csv_path)
        sys.exit(1)

    allocations = load_allocations(csv_path)
    logger.info("Loaded %d allocations from %s", len(allocations), csv_path)

    report = compute_funding_report(
        allocations,
        batch_size=args.batch_size,
        fee_per_batch_lovelace=args.fee_per_batch,
        safety_margin_pct=args.safety_margin,
    )

    # Print summary
    print(f"\n{'='*60}")
    print("FLUX Merger - Funding Calculator")
    print(f"{'='*60}")
    print(f"  Claims:       {report['num_claims']:,}")
    print(f"  Batches:      {report['num_batches']:,} (batch size {report['batch_size']})")
    print(f"  Locked ADA:   {report['locked_ada']['total_min_ada_display']:,.2f} ADA")
    print(f"  Fees:         {report['fees']['total_fees_display']:,.2f} ADA")
    print(f"  Margin ({report['safety_margin']['pct']}%): {report['safety_margin']['margin_display']:,.2f} ADA")
    print(f"  {'-'*41}")
    print(f"  GRAND TOTAL:  {report['grand_total']['ada']:,.2f} ADA")
    print(f"{'='*60}\n")

    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Report written to %s", out_path)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
