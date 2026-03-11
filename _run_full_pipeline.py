#!/usr/bin/env python3
"""Run full pipeline with staggered API calls for rate limiting.

Surrender-and-redeem model (v3.0):
1. TWAP report (7 assets — fungible + NFT floor prices)
2. Merge valuation with 85% public pool + rate table
3. Snapshot allocation (audit reference only under new model)
4. Surrender pool funding calculator
"""
import os
import sys
import time

project_root = os.path.dirname(os.path.abspath(__file__))
os.chdir(project_root)
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, "env.local"), override=True)
os.environ["NETWORK"] = "mainnet"

AUDIT_DIR = "audit_pack/2026-03-11"
os.makedirs(AUDIT_DIR, exist_ok=True)

# Use existing TWAP report if available, otherwise generate fresh
TWAP_REPORT = f"{AUDIT_DIR}/twap_report.json"
OLD_TWAP = "audit_pack/2026-02-12/twap_report.json"
if not os.path.exists(TWAP_REPORT) and os.path.exists(OLD_TWAP):
    # Copy old TWAP as starting point — re-run _run_twap.py for fresh data
    import shutil
    shutil.copy2(OLD_TWAP, TWAP_REPORT)
    print(f"Copied existing TWAP report to {TWAP_REPORT}")
    print("Run _run_twap.py separately for fresh TWAP data.\n")

# ---- Phase 1: TWAP (optional — run _run_twap.py separately if needed) ----
if not os.path.exists(TWAP_REPORT):
    print("\n" + "=" * 60)
    print("PHASE 1: TWAP Report (7 assets)")
    print("=" * 60)

    from tools.twap_snapshot_pools import main as twap_main
    twap_main(["--out", TWAP_REPORT, "--include-nfts"])

    print("\nWaiting 10s for rate limit cooldown...")
    time.sleep(10)

# ---- Phase 2: Merge Valuation (85% public pool) + Rate Table ----
print("\n" + "=" * 60)
print("PHASE 2: Merge Valuation (85% public pool) + Rate Table")
print("=" * 60)

from tools.flux_merge_valuation_int import main as valuation_main
valuation_main([
    "--twap-report", TWAP_REPORT,
    "--out-json", f"{AUDIT_DIR}/merge_valuation_cmatra.json",
    "--out-rate-table", f"{AUDIT_DIR}/rate_table_cmatra.json",
    "--team-waiver", "AGENT:29644656", "SHARDS:88551450001",
])

print("\nWaiting 10s before snapshot (rate limit cooldown)...")
time.sleep(10)

# ---- Phase 3: Snapshot Allocation (audit reference) ----
print("\n" + "=" * 60)
print("PHASE 3: Snapshot Allocation (audit reference only)")
print("=" * 60)

from tools.snapshot_allocate_flux import main as snapshot_main
snapshot_main([
    "--merge-report", f"{AUDIT_DIR}/merge_valuation_cmatra.json",
    "--reserve-address",
        "addr1w9u9mw864yszpqk7374wtwtwludpa0rc9dmante78c7c9sqqdlyy9",
        "FPS DAO Treasury",
    "--reserve-address",
        "addr1wx84ytuumke8gxex0l8par4852ey7l4eq6h325rnez0yluc56x0dj",
        "$TALOS Treasury",
    "--out", f"{AUDIT_DIR}/allocations_cmatra.csv",
    "--out-summary", f"{AUDIT_DIR}/allocations_cmatra_summary.json",
    "--out-reserve-ledger", f"{AUDIT_DIR}/reserve_ledger.json",
    "--resolve-nft-scripts",
])

print("\nWaiting 5s before funding calc...")
time.sleep(5)

# ---- Phase 4: Surrender Pool Funding Calculator ----
print("\n" + "=" * 60)
print("PHASE 4: Surrender Pool Funding Calculator")
print("=" * 60)

from tools.funding_calculator import main as funding_main
funding_main([
    "pool",
    "--out-json", f"{AUDIT_DIR}/funding_report_cmatra.json",
])

print("\n" + "=" * 60)
print("DONE - All reports in", AUDIT_DIR)
print("=" * 60)
