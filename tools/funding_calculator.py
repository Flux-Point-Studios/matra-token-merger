#!/usr/bin/env python3
"""
Funding Calculator — estimate total ADA required for surrender pool deployment.

Two modes:
  1. Pool mode (new): estimates ADA for surrender pool UTxOs
  2. Legacy claim mode: estimates ADA for per-claimant claim UTxOs (deprecated)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

from tools.cardano_utils import estimate_min_ada

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pool funding (surrender-and-redeem model)
# ---------------------------------------------------------------------------


def compute_pool_funding_report(
    total_cmatra_base: int,
    num_pool_utxos: int = 10,
    fee_per_deploy_tx_lovelace: int = 500_000,
    num_deploy_txs: int = 2,
    safety_margin_pct: float = 10.0,
) -> dict[str, Any]:
    """Compute funding report for surrender pool deployment.

    The pool locks all cMATRA in a small number of UTxOs at the script
    address. Each UTxO needs min-ADA plus the cMATRA tokens.
    """
    # Pool UTxOs have a void datum (~4 bytes CBOR) and 1 native asset
    datum_size = 4  # Constr(0, []) = ~4 bytes
    per_utxo_min_ada = estimate_min_ada(num_assets=1, datum_size_bytes=datum_size)

    total_locked_ada = per_utxo_min_ada * num_pool_utxos
    total_fees = fee_per_deploy_tx_lovelace * num_deploy_txs
    subtotal = total_locked_ada + total_fees
    margin = int(subtotal * safety_margin_pct / 100)
    grand_total = subtotal + margin

    return {
        "report_type": "pool_funding_calculator",
        "model": "surrender_pool",
        "total_cmatra_base": total_cmatra_base,
        "num_pool_utxos": num_pool_utxos,
        "per_utxo_min_ada_lovelace": per_utxo_min_ada,
        "locked_ada": {
            "total_min_ada_lovelace": total_locked_ada,
            "total_min_ada_display": total_locked_ada / 1_000_000,
        },
        "fees": {
            "fee_per_deploy_tx_lovelace": fee_per_deploy_tx_lovelace,
            "num_deploy_txs": num_deploy_txs,
            "total_fees_lovelace": total_fees,
            "total_fees_display": total_fees / 1_000_000,
        },
        "safety_margin": {
            "pct": safety_margin_pct,
            "margin_lovelace": margin,
            "margin_display": margin / 1_000_000,
        },
        "grand_total": {
            "lovelace": grand_total,
            "ada": grand_total / 1_000_000,
        },
    }


# ---------------------------------------------------------------------------
# Legacy claim funding (deprecated — kept for backward compatibility)
# ---------------------------------------------------------------------------


def compute_funding_report(
    allocations: list[dict[str, Any]],
    batch_size: int = 40,
    fee_per_batch_lovelace: int = 500_000,
    safety_margin_pct: float = 10.0,
) -> dict[str, Any]:
    """Compute a full funding report from per-claimant allocations.

    DEPRECATED: This is the old per-claimant claim-UTxO model.
    Use compute_pool_funding_report() for the surrender model.
    """
    from tools.cardano_utils import encode_claim_datum

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
        description="cMATRA merger — Funding Calculator",
    )
    subparsers = parser.add_subparsers(dest="mode", help="Funding mode")

    # Pool mode (new)
    pool_parser = subparsers.add_parser("pool", help="Surrender pool funding")
    pool_parser.add_argument("--total-cmatra-base", type=int, default=None,
                             help="Total cMATRA base units for pool (default: PUBLIC_POOL_BASE)")
    pool_parser.add_argument("--num-utxos", type=int, default=10)
    pool_parser.add_argument("--fee-per-tx", type=int, default=500_000)
    pool_parser.add_argument("--num-deploy-txs", type=int, default=2)
    pool_parser.add_argument("--safety-margin", type=float, default=10.0)
    pool_parser.add_argument("--out-json", type=str, default=None)

    # Legacy mode (deprecated)
    legacy_parser = subparsers.add_parser("legacy", help="Legacy claim-UTxO funding (deprecated)")
    legacy_parser.add_argument("--allocations-csv", type=str, required=True)
    legacy_parser.add_argument("--batch-size", type=int, default=40)
    legacy_parser.add_argument("--fee-per-batch", type=int, default=500_000)
    legacy_parser.add_argument("--safety-margin", type=float, default=10.0)
    legacy_parser.add_argument("--out-json", type=str, default=None)

    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.mode == "pool" or args.mode is None:
        from tools.config import PUBLIC_POOL_BASE
        total = args.total_cmatra_base if hasattr(args, "total_cmatra_base") and args.total_cmatra_base else PUBLIC_POOL_BASE

        report = compute_pool_funding_report(
            total_cmatra_base=total,
            num_pool_utxos=getattr(args, "num_utxos", 10),
            fee_per_deploy_tx_lovelace=getattr(args, "fee_per_tx", 500_000),
            num_deploy_txs=getattr(args, "num_deploy_txs", 2),
            safety_margin_pct=getattr(args, "safety_margin", 10.0),
        )

        print(f"\n{'='*60}")
        print("cMATRA Merger - Pool Funding Calculator")
        print(f"{'='*60}")
        print(f"  Pool UTxOs:    {report['num_pool_utxos']}")
        print(f"  Per-UTxO ADA:  {report['per_utxo_min_ada_lovelace'] / 1_000_000:.2f} ADA")
        print(f"  Locked ADA:    {report['locked_ada']['total_min_ada_display']:,.2f} ADA")
        print(f"  Deploy fees:   {report['fees']['total_fees_display']:,.2f} ADA")
        print(f"  Margin ({report['safety_margin']['pct']}%): {report['safety_margin']['margin_display']:,.2f} ADA")
        print(f"  {'-'*41}")
        print(f"  GRAND TOTAL:   {report['grand_total']['ada']:,.2f} ADA")
        print(f"{'='*60}\n")

    elif args.mode == "legacy":
        from tools.build_claim_utxos_flux import load_allocations

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

        print(f"\n{'='*60}")
        print("FLUX Merger - Legacy Funding Calculator (DEPRECATED)")
        print(f"{'='*60}")
        print(f"  Claims:       {report['num_claims']:,}")
        print(f"  Batches:      {report['num_batches']:,} (batch size {report['batch_size']})")
        print(f"  Locked ADA:   {report['locked_ada']['total_min_ada_display']:,.2f} ADA")
        print(f"  Fees:         {report['fees']['total_fees_display']:,.2f} ADA")
        print(f"  Margin ({report['safety_margin']['pct']}%): {report['safety_margin']['margin_display']:,.2f} ADA")
        print(f"  {'-'*41}")
        print(f"  GRAND TOTAL:  {report['grand_total']['ada']:,.2f} ADA")
        print(f"{'='*60}\n")

    else:
        parser.print_help()
        return

    out_json = getattr(args, "out_json", None)
    if out_json:
        out_path = Path(out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Report written to %s", out_path)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
