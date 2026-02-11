#!/usr/bin/env python3
"""
Phases 3 & 4 — Holder Snapshot + Allocation Generation

Fetches all holders of AGENT and SHARDS from Blockfrost, applies exclusions
(script addresses, specific addresses), extracts payment key hashes, and
computes per-address FLUX allocations using pure integer math.

Outputs:
  - allocations_flux.csv
  - allocations_summary.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import address_to_payment_key_hash, is_script_address
from tools.config import FLUX_DECIMALS, FLUX_MAX_SUPPLY_BASE, LEGACY_TOKENS, TokenInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Holder snapshot
# ---------------------------------------------------------------------------


def fetch_holders(
    bf: BlockfrostClient,
    token: TokenInfo,
) -> list[dict[str, Any]]:
    """Fetch all addresses holding *token*, returning [{address, quantity}]."""
    raw = bf.get_asset_addresses(token.unit)
    return [
        {"address": r["address"], "quantity": int(r["quantity"])}
        for r in raw
    ]


def filter_holders(
    holders: list[dict[str, Any]],
    exclude_script: bool = True,
    exclude_addresses: set[str] | None = None,
    script_whitelist: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter holders based on exclusion rules."""
    exclude_addresses = exclude_addresses or set()
    script_whitelist = script_whitelist or set()
    result = []
    for h in holders:
        addr = h["address"]
        if addr in exclude_addresses:
            continue
        if exclude_script and is_script_address(addr) and addr not in script_whitelist:
            continue
        result.append(h)
    return result


# ---------------------------------------------------------------------------
# Snapshot anchor
# ---------------------------------------------------------------------------


def capture_snapshot_anchor(bf: BlockfrostClient) -> dict[str, Any]:
    """Capture the latest block as the snapshot anchor."""
    block = bf.get_latest_block()
    return {
        "block_hash": block["hash"],
        "block_height": block["height"],
        "block_time": block["time"],
        "block_slot": block["slot"],
    }


# ---------------------------------------------------------------------------
# Integer allocation
# ---------------------------------------------------------------------------


def allocate_flux(
    token: TokenInfo,
    holders: list[dict[str, Any]],
    bucket_base_units: int,
    denominator_mode: str = "eligible",
    total_supply_base: int | None = None,
) -> list[dict[str, Any]]:
    """Compute FLUX allocation for each holder of *token*.

    Uses floor division: alloc(addr) = floor(balance * bucket / denominator)

    *denominator_mode*:
      - "eligible": sum of eligible holder balances (distributes full bucket)
      - "report":   total on-chain supply (may leave some undistributed)
    """
    if denominator_mode == "eligible":
        denominator = sum(h["quantity"] for h in holders)
    elif denominator_mode == "report":
        if total_supply_base is None:
            raise ValueError("denominator_mode='report' requires total_supply_base")
        denominator = total_supply_base
    else:
        raise ValueError(f"Unknown denominator_mode: {denominator_mode}")

    if denominator == 0:
        return []

    allocations = []
    for h in holders:
        flux_units = (h["quantity"] * bucket_base_units) // denominator
        allocations.append({
            "address": h["address"],
            "token_name": token.name,
            "token_balance_base": h["quantity"],
            "token_balance_display": h["quantity"] / (10 ** token.decimals),
            "flux_units": flux_units,
            "flux_display": flux_units / (10 ** FLUX_DECIMALS),
        })
    return allocations


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def run_snapshot_and_allocate(
    bf: BlockfrostClient,
    merge_report: dict[str, Any],
    exclude_script: bool = True,
    exclude_addresses: set[str] | None = None,
    script_whitelist: set[str] | None = None,
    denominator_mode: str = "eligible",
    dust_to: str | None = None,
    tokens: list[TokenInfo] | None = None,
    min_flux_threshold: int = 1,
) -> dict[str, Any]:
    """Run the full snapshot + allocation pipeline."""
    tokens = tokens or LEGACY_TOKENS

    # Capture snapshot anchor
    anchor = capture_snapshot_anchor(bf)
    logger.info(
        "Snapshot anchor: block %s height %d",
        anchor["block_hash"][:16] + "...",
        anchor["block_height"],
    )

    all_allocations: list[dict[str, Any]] = []
    token_summaries: dict[str, Any] = {}

    for token in tokens:
        token_data = merge_report["tokens"][token.name]
        bucket = token_data["flux_bucket_base_units"]

        # Fetch holders
        raw_holders = fetch_holders(bf, token)
        raw_supply = sum(h["quantity"] for h in raw_holders)

        # Filter
        eligible_holders = filter_holders(
            raw_holders,
            exclude_script=exclude_script,
            exclude_addresses=exclude_addresses,
            script_whitelist=script_whitelist,
        )
        eligible_supply = sum(h["quantity"] for h in eligible_holders)

        logger.info(
            "%s: %d raw holders → %d eligible (supply: %d → %d base units)",
            token.name,
            len(raw_holders),
            len(eligible_holders),
            raw_supply,
            eligible_supply,
        )

        # Allocate
        allocs = allocate_flux(
            token,
            eligible_holders,
            bucket,
            denominator_mode=denominator_mode,
            total_supply_base=token_data.get("supply_base_units"),
        )

        distributed = sum(a["flux_units"] for a in allocs)
        dust = bucket - distributed

        token_summaries[token.name] = {
            "raw_holders": len(raw_holders),
            "eligible_holders": len(eligible_holders),
            "raw_supply_base": raw_supply,
            "eligible_supply_base": eligible_supply,
            "bucket_base_units": bucket,
            "distributed_base_units": distributed,
            "dust_base_units": dust,
        }

        all_allocations.extend(allocs)

    # Aggregate by payment key hash (multiple tokens → same claimant)
    pkh_totals: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "addresses": set(),
        "flux_units": 0,
        "per_token": {},
    })

    for a in all_allocations:
        if a["flux_units"] < min_flux_threshold:
            continue
        pkh = address_to_payment_key_hash(a["address"])
        if pkh is None:
            logger.warning("Cannot extract key hash from %s — skipping", a["address"])
            continue
        entry = pkh_totals[pkh]
        entry["addresses"].add(a["address"])
        entry["flux_units"] += a["flux_units"]
        entry["per_token"][a["token_name"]] = {
            "balance_base": a["token_balance_base"],
            "flux_units": a["flux_units"],
        }

    # Build final allocation rows
    final_rows: list[dict[str, Any]] = []
    for pkh, data in sorted(pkh_totals.items()):
        row: dict[str, Any] = {
            "payment_key_hash_hex": pkh,
            "addresses": sorted(data["addresses"]),
            "flux_total_units": data["flux_units"],
            "flux_total_display": data["flux_units"] / (10 ** FLUX_DECIMALS),
        }
        for t in tokens:
            td = data["per_token"].get(t.name, {})
            row[f"{t.name.lower()}_balance_base"] = td.get("balance_base", 0)
            row[f"{t.name.lower()}_flux_units"] = td.get("flux_units", 0)
        final_rows.append(row)

    total_distributed = sum(r["flux_total_units"] for r in final_rows)
    total_dust = FLUX_MAX_SUPPLY_BASE - total_distributed

    # Dust sweep
    if dust_to and total_dust > 0:
        dust_pkh = address_to_payment_key_hash(dust_to)
        if dust_pkh:
            # Find existing entry or create new
            existing = next(
                (r for r in final_rows if r["payment_key_hash_hex"] == dust_pkh),
                None,
            )
            if existing:
                existing["flux_total_units"] += total_dust
                existing["flux_total_display"] = existing["flux_total_units"] / (10 ** FLUX_DECIMALS)
            else:
                final_rows.append({
                    "payment_key_hash_hex": dust_pkh,
                    "addresses": [dust_to],
                    "flux_total_units": total_dust,
                    "flux_total_display": total_dust / (10 ** FLUX_DECIMALS),
                    **{f"{t.name.lower()}_balance_base": 0 for t in tokens},
                    **{f"{t.name.lower()}_flux_units": 0 for t in tokens},
                })
            total_distributed += total_dust
            total_dust = 0
            logger.info("Dust swept %d FLUX base units to %s", total_dust, dust_to)

    summary = {
        "snapshot_anchor": anchor,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "exclude_script_addresses": exclude_script,
            "denominator_mode": denominator_mode,
            "dust_to": dust_to,
            "min_flux_threshold": min_flux_threshold,
        },
        "per_token": token_summaries,
        "totals": {
            "unique_claimants": len(final_rows),
            "total_flux_distributed": total_distributed,
            "total_dust_remaining": total_dust,
            "sum_equals_max_supply": total_distributed + total_dust == FLUX_MAX_SUPPLY_BASE,
        },
    }

    return {
        "allocations": final_rows,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "payment_key_hash_hex",
    "addresses",
    "agent_balance_base",
    "agent_flux_units",
    "shards_balance_base",
    "shards_flux_units",
    "flux_total_units",
    "flux_total_display",
]


def write_allocations_csv(rows: list[dict[str, Any]], out_path: Path) -> None:
    """Write allocation rows to CSV."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            csv_row["addresses"] = "|".join(row.get("addresses", []))
            writer.writerow(csv_row)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX merger — Phase 3+4: Snapshot & Allocation",
    )
    parser.add_argument(
        "--merge-report", type=str, required=True,
        help="Path to Phase 2 merge report JSON",
    )
    parser.add_argument(
        "--exclude-script-addresses", action="store_true", default=True,
        help="Exclude script (contract) addresses (default: true)",
    )
    parser.add_argument(
        "--exclude-addresses", type=str, nargs="*", default=[],
        help="Specific addresses to exclude",
    )
    parser.add_argument(
        "--script-whitelist", type=str, nargs="*", default=[],
        help="Script addresses to include despite --exclude-script-addresses",
    )
    parser.add_argument(
        "--denominator-mode", choices=["eligible", "report"], default="eligible",
        help="Denominator for allocation (default: eligible)",
    )
    parser.add_argument(
        "--dust-to", type=str, default=None,
        help="Treasury address to sweep dust to",
    )
    parser.add_argument(
        "--out", type=str, required=True,
        help="Output path for allocations CSV",
    )
    parser.add_argument(
        "--out-summary", type=str, required=True,
        help="Output path for allocations summary JSON",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    with open(args.merge_report) as f:
        merge_report = json.load(f)

    bf = BlockfrostClient()
    result = run_snapshot_and_allocate(
        bf,
        merge_report,
        exclude_script=args.exclude_script_addresses,
        exclude_addresses=set(args.exclude_addresses) if args.exclude_addresses else None,
        script_whitelist=set(args.script_whitelist) if args.script_whitelist else None,
        denominator_mode=args.denominator_mode,
        dust_to=args.dust_to,
    )

    # Write CSV
    csv_path = Path(args.out)
    write_allocations_csv(result["allocations"], csv_path)
    logger.info("Allocations CSV: %s (%d rows)", csv_path, len(result["allocations"]))

    # Write summary
    summary_path = Path(args.out_summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(result["summary"], f, indent=2)
    logger.info("Summary: %s", summary_path)

    # Validate
    totals = result["summary"]["totals"]
    if totals["sum_equals_max_supply"]:
        logger.info("Total distributed + dust == %d (OK)", FLUX_MAX_SUPPLY_BASE)
    else:
        logger.error("SUPPLY MISMATCH!")
        sys.exit(1)


if __name__ == "__main__":
    main()
