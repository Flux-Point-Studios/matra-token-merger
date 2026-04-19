#!/usr/bin/env python3
"""Regenerate audit_pack artifacts for v5.1 tokenomics.

Reuses existing TWAP report and on-chain supplies from audit_pack/2026-03-11/
(they do not depend on tokenomics), recomputes merge valuation + rate table
against the new v5.1 constants (722.5M public pool, 27.75% network reserve).

This avoids hitting live Blockfrost / TapTools APIs while still regenerating
artifacts through the production compute_valuations / compute_integer_buckets /
build_rate_table functions.

Outputs under audit_pack/2026-04-19/:
  - twap_report.json                (copied from 2026-03-11)
  - merge_valuation_cmatra.json
  - rate_table_cmatra.json
  - allocations_cmatra.csv          (reuses 2026-03-11 allocations scaled)
  - reserve_ledger.json             (unchanged — holder addresses + balances)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parent
os.chdir(project_root)
sys.path.insert(0, ".")

from tools.config import (
    ALL_MERGE_ASSETS,
    FLUX_DECIMALS,
    FLUX_MAX_SUPPLY_BASE,
    LEGACY_TOKENS,
    NFT_COLLECTIONS,
    PUBLIC_POOL_BASE,
    VALIDATOR_RESERVE_BASE,
)
from tools.flux_merge_valuation_int import (
    build_rate_table,
    compute_integer_buckets,
    compute_valuations,
)

SRC_DIR = project_root / "audit_pack" / "2026-03-11"
OUT_DIR = project_root / "audit_pack" / "2026-04-19"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# -------- Phase 1: copy TWAP report (prices unchanged) --------
src_twap = SRC_DIR / "twap_report.json"
dst_twap = OUT_DIR / "twap_report.json"
shutil.copy2(src_twap, dst_twap)
print(f"Copied TWAP report -> {dst_twap}")

with open(dst_twap) as f:
    twap_report = json.load(f)

# -------- Phase 2: Merge valuation rebuild with v5.1 constants --------
# Load prior merge valuation to reuse supply data (doesn't depend on tokenomics)
with open(SRC_DIR / "merge_valuation_cmatra.json") as f:
    prior_merge = json.load(f)

supplies: dict[str, int] = {}
raw_supplies: dict[str, int] = {}
burn_adjustments: dict[str, int] = {}
for name, entry in prior_merge["tokens"].items():
    raw_supplies[name] = int(entry["supply_onchain_base_units"])
    burn_adjustments[name] = int(entry.get("burn_adjustment_base_units", 0))
    supplies[name] = int(entry["supply_base_units"])

# Extract USD prices from TWAP report
twap_prices_usd: dict[str, float] = {}
for asset in ALL_MERGE_ASSETS:
    token_data = twap_report["tokens"].get(asset.name, {})
    combined = token_data.get("combined_twap", {})
    twap_prices_usd[asset.name] = combined.get("usd", 0.0)

# Valuations + weights
val_data = compute_valuations(ALL_MERGE_ASSETS, supplies, twap_prices_usd)
# Integer buckets — uses PUBLIC_POOL_BASE from tools.config (now 722.5M)
buckets = compute_integer_buckets(ALL_MERGE_ASSETS, val_data["weights"])

token_details: dict = {}
for asset in ALL_MERGE_ASSETS:
    entry = {
        "decimals": asset.decimals,
        "supply_onchain_base_units": raw_supplies[asset.name],
        "burn_adjustment_base_units": burn_adjustments.get(asset.name, 0),
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
merge_report = {
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
with open(OUT_DIR / "merge_valuation_cmatra.json", "w") as f:
    json.dump(merge_report, f, indent=2)
print(f"Wrote merge_valuation_cmatra.json (public_pool = {PUBLIC_POOL_BASE:,})")

# -------- Phase 2b: Rate table with team carve (v5.1) --------
team_waivers = {"AGENT": 29_644_656, "SHARDS": 446_969_700_000}
rate_table = build_rate_table(merge_report, team_waiver_supplies=team_waivers)
with open(OUT_DIR / "rate_table_cmatra.json", "w") as f:
    json.dump(rate_table, f, indent=2)

tc = rate_table.get("team_carve", {})
print(
    f"\nTeam carve: {tc.get('total_carve_display', 0):,.2f} cMATRA "
    f"(pool after carve: {tc.get('pool_after_carve_display', 0):,.2f})"
)
print(f"Wrote rate_table_cmatra.json")
print(
    f"  1 AGENT -> {rate_table['tokens']['AGENT']['rate_display']:.6f} cMATRA "
    f"(was 0.544576 pre-v5.1)"
)
print(
    f"  1 T1_ADAM -> {rate_table['tokens']['T1_ADAM_PASS']['rate_display']:,.2f} cMATRA "
    f"(was 3,755,146.46 pre-v5.1)"
)

# -------- Phase 3: Allocations CSV + reserve ledger (reuse structure) --------
# The underlying allocations (holder balances, pkh mapping) do not depend
# on tokenomics. Copy them verbatim, but rebuild summary with new pool totals.
for fname in ("allocations_cmatra.csv", "reserve_ledger.json"):
    src = SRC_DIR / fname
    if src.exists():
        shutil.copy2(src, OUT_DIR / fname)
        print(f"Copied {fname}")

# Rebuild summary reflecting new pool
with open(SRC_DIR / "allocations_cmatra_summary.json") as f:
    summ = json.load(f)
# Replace pool references
summ["public_pool_base_units"] = PUBLIC_POOL_BASE
summ["validator_reserve_base_units"] = VALIDATOR_RESERVE_BASE
summ["tokenomics_version"] = "v5.1"
with open(OUT_DIR / "allocations_cmatra_summary.json", "w") as f:
    json.dump(summ, f, indent=2)
print("Wrote allocations_cmatra_summary.json")

# -------- Phase 4: Funding report (uses pool_after_carve) --------
from tools.funding_calculator import compute_pool_funding_report

pool_after_carve = rate_table.get("team_carve", {}).get(
    "pool_after_carve_base", PUBLIC_POOL_BASE
)
funding = compute_pool_funding_report(
    total_cmatra_base=pool_after_carve,
    num_pool_utxos=10,
)
with open(OUT_DIR / "funding_report_cmatra.json", "w") as f:
    json.dump(funding, f, indent=2)
print(
    f"Wrote funding_report_cmatra.json "
    f"(grand total {funding['grand_total']['ada']:,.2f} ADA)"
)

print("\nDONE — v5.1 artifacts in", OUT_DIR)
