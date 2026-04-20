#!/usr/bin/env python3
"""Regenerate audit_pack/<date>/ allocation artifacts for v5.1 tokenomics.

Background
----------
The original _regen_v5_1.py short-circuited the pipeline by copying the
2026-03-11 allocations_cmatra.csv and reserve_ledger.json verbatim.  Those
files were computed with v3/v4 buckets (850M public pool) and therefore
over-allocated to claimants by 13.5% — a BLOCK-level accounting bug that
would drain the pool if submitted to chain.

This script regenerates the artifacts *end to end* against v5.1 constants,
reusing the prior snapshot (AGENT/SHARDS/NFT balances per PKH) but
recomputing:

  * rate_table_cmatra.json with rational rate fields
  * allocations_cmatra.csv with per-row flux = balance * rate  (per token)
  * allocations_cmatra_summary.json with total_bucket_sum == 722.5M
  * reserve_ledger.json with the 5 Network Incentives sub-buckets
    materialized AND team/NFT-conditional reserves recomputed at v5.1
  * merge_valuation_cmatra.json (inputs to rate_table)
  * funding_report_cmatra.json (uses pool_after_carve)

Inputs
------
* Source snapshot CSV — PKH + per-token balance columns from a prior run.
  Default: audit_pack/2026-03-11/allocations_cmatra.csv
* Source TWAP report (prices are tokenomics-independent).
  Default: audit_pack/2026-03-11/twap_report.json
* Source merge_valuation (for on-chain supply figures).
  Default: audit_pack/2026-03-11/merge_valuation_cmatra.json

Why rational rates?
-------------------
Integer rate_base_per_unit = bucket // redeemable truncates SHARDS from
29.12259 -> 29, losing ~13M cMATRA (~0.02% of pool but flagged as dust by
security review).  The rational form (rate_numerator=bucket,
rate_denominator=redeemable) preserves exactness at allocation and
surrender time.  process_surrender.py prefers rational when present.

Usage
-----
    python scripts/regen_v5_1_artifacts.py \\
        --out-dir audit_pack/2026-04-19 \\
        [--src-dir audit_pack/2026-03-11]

Or from project root:
    python -m scripts.regen_v5_1_artifacts --out-dir audit_pack/2026-04-19
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure tools/ is importable when run as script
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.config import (  # noqa: E402
    ALL_MERGE_ASSETS,
    ATTESTOR_EMISSIONS_BASE,
    ECOSYSTEM_TREASURY_BASE,
    FLUX_DECIMALS,
    FLUX_MAX_SUPPLY_BASE,
    LIQUIDITY_BASE,
    PUBLIC_POOL_BASE,
    STRATEGIC_BASE,
    VALIDATOR_EMISSIONS_BASE,
    VALIDATOR_RESERVE_BASE,
)
from tools.flux_merge_valuation_int import (  # noqa: E402
    build_rate_table,
    compute_integer_buckets,
    compute_valuations,
)
from tools.funding_calculator import compute_pool_funding_report  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Team-waiver balances (v5.1 — matches _run_full_pipeline.py)
TEAM_WAIVERS: dict[str, int] = {
    "AGENT": 29_644_656,
    "SHARDS": 446_969_700_000,
}

# Treasury addresses (labels, used when rebuilding team_treasury section)
TREASURY_ADDRESSES: dict[str, str] = {
    "addr1w9u9mw864yszpqk7374wtwtwludpa0rc9dmante78c7c9sqqdlyy9": "FPS DAO Treasury",
    "addr1wx84ytuumke8gxex0l8par4852ey7l4eq6h325rnez0yluc56x0dj": "$TALOS Treasury",
}

# Which token the dust sweep targets (Fix 3, Option A+sweep for residual):
# rational rate math is exact on eligible holders, but floor-division per
# row still leaves a few base units of dust (usually <10 per token).
# We route that to the Ecosystem Treasury sub-bucket (tracked but not paid
# out at this stage — it becomes part of the treasury inventory).
DUST_SWEEP_TARGET = "ecosystem_treasury"

# CSV column order must match tools.snapshot_allocate_flux.build_csv_columns()
CSV_COLUMNS = [
    "payment_key_hash_hex",
    "addresses",
    "agent_balance_base",
    "agent_flux_units",
    "shards_balance_base",
    "shards_flux_units",
    "flux_pass_balance_base",
    "flux_pass_flux_units",
    "se_brawlers_balance_base",
    "se_brawlers_flux_units",
    "brawl_pass_etd_balance_base",
    "brawl_pass_etd_flux_units",
    "t1_adam_pass_balance_base",
    "t1_adam_pass_flux_units",
    "t2_adam_pass_balance_base",
    "t2_adam_pass_flux_units",
    "flux_total_units",
    "flux_total_display",
]

# Asset name -> CSV column stem
ASSET_COL_STEM: dict[str, str] = {
    "AGENT": "agent",
    "SHARDS": "shards",
    "FLUX_PASS": "flux_pass",
    "SE_BRAWLERS": "se_brawlers",
    "BRAWL_PASS_ETD": "brawl_pass_etd",
    "T1_ADAM_PASS": "t1_adam_pass",
    "T2_ADAM_PASS": "t2_adam_pass",
}


# ---------------------------------------------------------------------------
# Rational allocation math
# ---------------------------------------------------------------------------


def rate_num_den(entry: dict[str, Any]) -> tuple[int, int]:
    """Extract the rational rate from a rate-table token entry.

    Prefers rate_numerator / rate_denominator (exact).  Falls back to
    rate_base_per_unit / 1 for legacy tables.
    """
    num = entry.get("rate_numerator")
    den = entry.get("rate_denominator")
    if num is not None and den is not None and den > 0:
        return int(num), int(den)
    return int(entry["rate_base_per_unit"]), 1


def allocate_row(balance: int, num: int, den: int) -> int:
    """Per-row floor allocation: (balance * num) // den."""
    if balance <= 0:
        return 0
    return (balance * num) // den


# ---------------------------------------------------------------------------
# Snapshot loader — reuses the balance snapshot from a prior run
# ---------------------------------------------------------------------------


def load_snapshot_rows(csv_path: Path) -> list[dict[str, Any]]:
    """Load prior-run allocation rows as our balance snapshot.

    We throw away the stale flux_* columns and keep only PKH + balance cols.
    """
    rows: list[dict[str, Any]] = []
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            rows.append({
                "payment_key_hash_hex": r["payment_key_hash_hex"],
                "addresses": r["addresses"].split("|") if r["addresses"] else [],
                **{
                    f"{stem}_balance_base": int(r[f"{stem}_balance_base"])
                    for stem in ASSET_COL_STEM.values()
                },
            })
    return rows


# ---------------------------------------------------------------------------
# Phase 1: Merge valuation (re-weighted using v5.1 pool)
# ---------------------------------------------------------------------------


def build_merge_valuation(
    twap_report: dict[str, Any],
    supply_source: dict[str, Any],
) -> dict[str, Any]:
    """Build a v5.1 merge_valuation report using prior supplies + TWAP prices.

    This reuses supplies (on-chain counts) and prices (TWAP) from a prior
    run since neither depends on tokenomics.  Only the bucket math runs
    fresh with PUBLIC_POOL_BASE = 722.5M.
    """
    raw_supplies: dict[str, int] = {}
    supplies: dict[str, int] = {}
    burn_adj: dict[str, int] = {}
    for name, entry in supply_source["tokens"].items():
        raw_supplies[name] = int(entry["supply_onchain_base_units"])
        burn_adj[name] = int(entry.get("burn_adjustment_base_units", 0))
        supplies[name] = int(entry["supply_base_units"])

    # Extract prices
    twap_prices_usd: dict[str, float] = {}
    for asset in ALL_MERGE_ASSETS:
        token_data = twap_report["tokens"].get(asset.name, {})
        combined = token_data.get("combined_twap", {})
        twap_prices_usd[asset.name] = combined.get("usd", 0.0)

    val_data = compute_valuations(ALL_MERGE_ASSETS, supplies, twap_prices_usd)
    buckets = compute_integer_buckets(ALL_MERGE_ASSETS, val_data["weights"])

    token_details: dict[str, Any] = {}
    for asset in ALL_MERGE_ASSETS:
        entry: dict[str, Any] = {
            "decimals": asset.decimals,
            "supply_onchain_base_units": raw_supplies[asset.name],
            "burn_adjustment_base_units": burn_adj.get(asset.name, 0),
            "supply_base_units": supplies[asset.name],
            "supply_display": supplies[asset.name] / (10 ** asset.decimals),
            "twap_usd": twap_prices_usd[asset.name],
            "valuation_usd": val_data["valuations_usd"][asset.name],
            "weight": val_data["weights"][asset.name],
            "flux_bucket_base_units": buckets[asset.name],
            "flux_bucket_display": buckets[asset.name] / (10 ** FLUX_DECIMALS),
        }
        if hasattr(asset, "unit"):
            entry["unit"] = asset.unit
        else:
            entry["policy_id"] = asset.policy_id
            entry["display_name"] = asset.display_name
            entry["is_nft"] = True
        token_details[asset.name] = entry

    bucket_total = sum(buckets.values())
    return {
        "report_type": "flux_merge_valuation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_supply_base_units": FLUX_MAX_SUPPLY_BASE,
        "public_pool_base_units": PUBLIC_POOL_BASE,
        "validator_reserve_base_units": VALIDATOR_RESERVE_BASE,
        "tokens": token_details,
        "totals": {
            "total_valuation_usd": val_data["total_valuation_usd"],
            "sum_weights": sum(val_data["weights"].values()),
            "sum_buckets_base_units": bucket_total,
            "buckets_sum_equals_pool": bucket_total == PUBLIC_POOL_BASE,
        },
        "warnings": [],
        "tokenomics_version": "v5.1",
    }


# ---------------------------------------------------------------------------
# Phase 2: Allocations CSV + summary
# ---------------------------------------------------------------------------


def build_allocations(
    rows: list[dict[str, Any]],
    merge_valuation: dict[str, Any],
    rate_table: dict[str, Any],
    prior_reserve_ledger: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Per-token, per-row allocation at v5.1 rates.

    Returns (csv_rows, summary_dict, reserve_ledger_dict).

    Allocation model (surrender-and-redeem at fixed rate):
        flux_units_per_row = (balance * rate_num) // rate_den

    Pool conservation:
        bucket = distributed_to_eligible
               + team_carve  (waiver * rate)
               + nft_conditional_reserve  (unresolvable * bucket / supply)
               + dust  (remainder from floor divisions)

    Dust is swept per-token into the ecosystem_treasury counter.
    """
    per_token_summary: dict[str, Any] = {}
    team_treasury: dict[str, Any] = {}
    nft_conditional: dict[str, Any] = {}
    total_dust_swept = 0

    # --- per-row alloc loop -------------------------------------------------
    # Build output rows with fresh flux_* columns
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        out_rows.append({
            "payment_key_hash_hex": r["payment_key_hash_hex"],
            "addresses": r["addresses"],
            "flux_total_units": 0,
            **{f"{stem}_balance_base": r[f"{stem}_balance_base"] for stem in ASSET_COL_STEM.values()},
            **{f"{stem}_flux_units": 0 for stem in ASSET_COL_STEM.values()},
        })

    # --- per-token computation ----------------------------------------------
    for asset_name, stem in ASSET_COL_STEM.items():
        token_entry = rate_table["tokens"][asset_name]
        bucket = token_entry["bucket_base"]
        total_supply = merge_valuation["tokens"][asset_name]["supply_base_units"]
        num, den = rate_num_den(token_entry)

        # Eligible holder balances from CSV
        eligible_supply = sum(r[f"{stem}_balance_base"] for r in rows)

        # Per-row allocation + sum
        distributed = 0
        for out, src in zip(out_rows, rows):
            bal = src[f"{stem}_balance_base"]
            flux = allocate_row(bal, num, den)
            out[f"{stem}_flux_units"] = flux
            out["flux_total_units"] += flux
            distributed += flux

        # Team carve (waiver at rational rate) — only for fungibles
        team_waiver = TEAM_WAIVERS.get(asset_name, 0)
        team_cmatra = allocate_row(team_waiver, num, den) if team_waiver > 0 else 0
        if team_cmatra > 0 and prior_reserve_ledger is not None:
            # Recompute per-address carves using v5.1 rate
            src_team = prior_reserve_ledger.get("team_treasury", {}).get(asset_name, {})
            addresses = []
            for addr_entry in src_team.get("addresses", []):
                bal_base = int(addr_entry["balance_base"])
                addresses.append({
                    "address": addr_entry["address"],
                    "label": addr_entry.get("label", ""),
                    "balance_base": bal_base,
                    "cmatra_base": allocate_row(bal_base, num, den),
                })
            team_treasury[asset_name] = {
                "total_balance_base": team_waiver,
                "total_cmatra_base": team_cmatra,
                "addresses": addresses,
            }

        # NFT conditional reserve (unresolvable count * bucket / supply)
        nft_res_cmatra = 0
        unresolvable_count = 0
        per_nft = 0
        if prior_reserve_ledger is not None:
            src_nft = prior_reserve_ledger.get("nft_conditional", {}).get(asset_name)
            if src_nft is not None:
                unresolvable_count = int(src_nft["unresolvable_count"])
                if total_supply > 0:
                    nft_res_cmatra = (unresolvable_count * bucket) // total_supply
                    per_nft = bucket // total_supply
                # Rebuild ledger with v5.1 per-NFT amount
                new_ledger = []
                for entry in src_nft.get("ledger", []):
                    new_ledger.append({
                        "asset_unit": entry["asset_unit"],
                        "script_address": entry["script_address"],
                        "reserved_cmatra_base": per_nft,
                        "status": entry.get("status", "pending"),
                    })
                nft_conditional[asset_name] = {
                    "total_supply": total_supply,
                    "unresolvable_count": unresolvable_count,
                    "total_cmatra_base": nft_res_cmatra,
                    "per_nft_cmatra_base": per_nft,
                    "ledger": new_ledger,
                }

        # Dust = bucket - distributed - team_carve - nft_res
        dust = bucket - distributed - team_cmatra - nft_res_cmatra
        assert dust >= 0, (
            f"{asset_name}: negative dust {dust}. "
            f"bucket={bucket} dist={distributed} team={team_cmatra} nft={nft_res_cmatra}"
        )
        total_dust_swept += dust

        # Summary (bucket-level, matches prior schema)
        raw_holders = sum(1 for r in rows if r[f"{stem}_balance_base"] > 0)
        per_token_summary[asset_name] = {
            "raw_holders": raw_holders,
            "eligible_holders": raw_holders,
            "raw_supply_base": total_supply,
            "eligible_supply_base": eligible_supply,
            "bucket_base_units": bucket,
            "team_reserve_base_units": team_cmatra,
            "nft_reserve_base_units": nft_res_cmatra,
            "unresolvable_nfts": unresolvable_count,
            "eligible_bucket_base_units": bucket - team_cmatra - nft_res_cmatra,
            "distributed_base_units": distributed,
            "dust_base_units": dust,
        }

    # Finalise flux_total_display on each row
    for out in out_rows:
        out["flux_total_display"] = out["flux_total_units"] / (10 ** FLUX_DECIMALS)

    # --- reserve_ledger ------------------------------------------------------
    # Preserve the prior structure (team_treasury + nft_conditional) but
    # materialise Network Incentives Reserve sub-buckets.
    reserve_ledger = {
        "team_treasury": team_treasury,
        "nft_conditional": nft_conditional,
        "network_incentives_reserve": {
            "total_base_units": VALIDATOR_RESERVE_BASE,
            "total_display_units": VALIDATOR_RESERVE_BASE // (10 ** FLUX_DECIMALS),
            "sub_buckets": {
                "validator_emissions": {
                    "base": VALIDATOR_EMISSIONS_BASE,
                    "display": VALIDATOR_EMISSIONS_BASE // (10 ** FLUX_DECIMALS),
                    "notes": "Block-reward / validator participation pool",
                },
                "attestor_emissions": {
                    "base": ATTESTOR_EMISSIONS_BASE,
                    "display": ATTESTOR_EMISSIONS_BASE // (10 ** FLUX_DECIMALS),
                    "notes": "Attestation rewards for Materios receipt attestors",
                },
                "ecosystem_treasury": {
                    "base": ECOSYSTEM_TREASURY_BASE,
                    "display": ECOSYSTEM_TREASURY_BASE // (10 ** FLUX_DECIMALS),
                    "notes": (
                        "Discretionary DAO spend + sweep target for per-row "
                        "allocation dust. Swept dust this regen: "
                        f"{total_dust_swept} base units."
                    ),
                },
                "strategic_investor": {
                    "base": STRATEGIC_BASE,
                    "display": STRATEGIC_BASE // (10 ** FLUX_DECIMALS),
                    "notes": "Strategic / investor carve (Orion Fund target)",
                },
                "liquidity": {
                    "base": LIQUIDITY_BASE,
                    "display": LIQUIDITY_BASE // (10 ** FLUX_DECIMALS),
                    "components": {
                        "bridge_peg_reserve": {
                            "base": 5_000_000_000_000,
                            "display": 5_000_000,
                            "notes": "MATRA<->cMATRA bridge peg insurance",
                        },
                        "protocol_owned_dex_liquidity": {
                            "base": 17_500_000_000_000,
                            "display": 17_500_000,
                            "notes": "POL deposits on Cardano DEXes (Minswap/SSwap)",
                        },
                        "maker_rebates": {
                            "base": 5_000_000_000_000,
                            "display": 5_000_000,
                            "notes": "Maker-side fee rebates for DEX LPs",
                        },
                    },
                    "notes": "Liquidity sub-bucket: 5M bridge + 17.5M POL + 5M rebates",
                },
            },
            "assertion": (
                "sub_buckets sum to total_base_units (277.5M); "
                "each sub-bucket is a soft-target cap managed by governance."
            ),
        },
    }

    # --- summary totals ------------------------------------------------------
    total_holder = sum(r["flux_total_units"] for r in out_rows)
    total_team = sum(t["total_cmatra_base"] for t in team_treasury.values())
    total_nft = sum(n["total_cmatra_base"] for n in nft_conditional.values())
    grand_total = total_holder + total_team + total_nft + total_dust_swept
    assert grand_total == PUBLIC_POOL_BASE, (
        f"Conservation failure: {total_holder} + {total_team} + {total_nft} "
        f"+ {total_dust_swept} = {grand_total} != {PUBLIC_POOL_BASE}"
    )

    summary = {
        "snapshot_anchor": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "exclude_script_addresses": True,
            "denominator_mode": "rate_table_rational",
            "dust_to": DUST_SWEEP_TARGET,
            "min_flux_threshold": 1,
            "reserve_addresses": TREASURY_ADDRESSES,
        },
        "per_token": per_token_summary,
        "reserve": {
            "team_treasury": team_treasury,
            "nft_conditional": {
                name: {k: v for k, v in data.items() if k != "ledger"}
                for name, data in nft_conditional.items()
            },
            "total_team_reserve_base": total_team,
            "total_nft_reserve_base": total_nft,
            "total_reserve_base": total_team + total_nft,
        },
        "totals": {
            "unique_claimants": len(out_rows),
            "total_flux_distributed": total_holder,
            "total_reserve": total_team + total_nft,
            "total_dust_swept_base": total_dust_swept,
            "total_bucket_sum": grand_total,
            "sum_equals_bucket_total": grand_total == PUBLIC_POOL_BASE,
        },
        "public_pool_base_units": PUBLIC_POOL_BASE,
        "validator_reserve_base_units": VALIDATOR_RESERVE_BASE,
        "tokenomics_version": "v5.1",
    }

    return out_rows, summary, reserve_ledger


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_allocations_csv(rows: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row_out = dict(r)
            row_out["addresses"] = "|".join(r.get("addresses", []))
            w.writerow(row_out)


def write_json(obj: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(obj, f, indent=2)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def regenerate(src_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Regenerating v5.1 artifacts: src={src_dir} out={out_dir}")

    # Load source data
    with open(src_dir / "twap_report.json") as f:
        twap_report = json.load(f)
    with open(src_dir / "merge_valuation_cmatra.json") as f:
        prior_merge = json.load(f)
    with open(src_dir / "reserve_ledger.json") as f:
        prior_reserve_ledger = json.load(f)

    snapshot_rows = load_snapshot_rows(src_dir / "allocations_cmatra.csv")
    print(f"Loaded snapshot: {len(snapshot_rows)} rows from {src_dir}")

    # --- Phase 1: TWAP pass-through (prices) -------------------------------
    write_json(twap_report, out_dir / "twap_report.json")
    print("  - twap_report.json (copied, prices are tokenomics-independent)")

    # --- Phase 2: merge valuation @ v5.1 -----------------------------------
    merge_valuation = build_merge_valuation(twap_report, prior_merge)
    write_json(merge_valuation, out_dir / "merge_valuation_cmatra.json")
    assert merge_valuation["totals"]["sum_buckets_base_units"] == PUBLIC_POOL_BASE
    print(f"  - merge_valuation_cmatra.json (pool={PUBLIC_POOL_BASE:,})")

    # --- Phase 3: rate table with rational fields + v5.1 team carve --------
    rate_table = build_rate_table(merge_valuation, team_waiver_supplies=TEAM_WAIVERS)
    write_json(rate_table, out_dir / "rate_table_cmatra.json")
    tc = rate_table.get("team_carve", {})
    print(
        f"  - rate_table_cmatra.json "
        f"(AGENT rate={rate_table['tokens']['AGENT']['rate_display']:.6f}, "
        f"SHARDS rational={rate_table['tokens']['SHARDS']['rate_numerator']}/"
        f"{rate_table['tokens']['SHARDS']['rate_denominator']})"
    )
    print(
        f"    team_carve total={tc.get('total_carve_display', 0):,.2f} cMATRA, "
        f"pool_after_carve={tc.get('pool_after_carve_display', 0):,.2f}"
    )

    # --- Phase 4: per-row allocations + summary + ledger -------------------
    alloc_rows, summary, reserve_ledger = build_allocations(
        snapshot_rows, merge_valuation, rate_table, prior_reserve_ledger,
    )
    write_allocations_csv(alloc_rows, out_dir / "allocations_cmatra.csv")
    write_json(summary, out_dir / "allocations_cmatra_summary.json")
    write_json(reserve_ledger, out_dir / "reserve_ledger.json")

    total_flux = sum(r["flux_total_units"] for r in alloc_rows)
    print(
        f"  - allocations_cmatra.csv ({len(alloc_rows)} rows, "
        f"total flux={total_flux:,} "
        f"= {total_flux / (10**FLUX_DECIMALS):,.6f} cMATRA)"
    )
    print(
        f"  - allocations_cmatra_summary.json "
        f"(total_bucket_sum={summary['totals']['total_bucket_sum']:,})"
    )
    print("  - reserve_ledger.json (with 5 Network Incentives sub-buckets)")

    # --- Phase 5: funding report -------------------------------------------
    pool_after_carve = tc.get("pool_after_carve_base", PUBLIC_POOL_BASE)
    funding = compute_pool_funding_report(
        total_cmatra_base=pool_after_carve,
        num_pool_utxos=10,
    )
    write_json(funding, out_dir / "funding_report_cmatra.json")
    print(
        f"  - funding_report_cmatra.json "
        f"(grand_total {funding['grand_total']['ada']:,.2f} ADA)"
    )

    print(f"\nDONE — v5.1 artifacts in {out_dir}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description="Regenerate v5.1 audit_pack artifacts (real pipeline, "
                    "no shortcut-copies)."
    )
    p.add_argument(
        "--src-dir", type=str,
        default=str(_PROJECT_ROOT / "audit_pack" / "2026-03-11"),
        help="Source audit_pack with prior snapshot (default: 2026-03-11)",
    )
    p.add_argument(
        "--out-dir", type=str,
        default=str(_PROJECT_ROOT / "audit_pack" / "2026-04-19"),
        help="Output audit_pack directory (default: 2026-04-19)",
    )
    args = p.parse_args(argv)

    regenerate(Path(args.src_dir), Path(args.out_dir))


if __name__ == "__main__":
    main()
