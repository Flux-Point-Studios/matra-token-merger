#!/usr/bin/env python3
"""Run merge valuation with the latest TWAP report (7 assets)."""
import os
import sys

project_root = os.path.dirname(os.path.abspath(__file__))
os.chdir(project_root)
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, "env.local"), override=True)

os.environ["NETWORK"] = "mainnet"

from tools.flux_merge_valuation_int import main
main([
    "--twap-report", "audit_pack/2026-02-12/twap_report.json",
    "--out-json", "audit_pack/2026-02-12/merge_valuation_cmatra.json",
])
