#!/usr/bin/env python3
"""Run TWAP report with NFT collections included."""
import os
import sys

project_root = os.path.dirname(os.path.abspath(__file__))
os.chdir(project_root)
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, "env.local"), override=True)

os.environ["NETWORK"] = "mainnet"

from tools.twap_snapshot_pools import main
main(["--out", "audit_pack/2026-02-12/twap_report.json", "--include-nfts"])
