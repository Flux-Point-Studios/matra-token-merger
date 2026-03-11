#!/usr/bin/env python3
"""Run full snapshot & allocation for all 7 assets."""
import os
import sys

project_root = os.path.dirname(os.path.abspath(__file__))
os.chdir(project_root)
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, "env.local"), override=True)

os.environ["NETWORK"] = "mainnet"

from tools.snapshot_allocate_flux import main
main([
    "--merge-report", "audit_pack/2026-02-12/merge_valuation_cmatra.json",
    "--out", "audit_pack/2026-02-12/allocations_cmatra.csv",
    "--out-summary", "audit_pack/2026-02-12/allocations_cmatra_summary.json",
    "--resolve-nft-scripts",
])
