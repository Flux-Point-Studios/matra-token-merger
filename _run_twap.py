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

from datetime import date
from tools.twap_snapshot_pools import main

today = date.today().isoformat()
out_dir = f"audit_pack/{today}"
os.makedirs(out_dir, exist_ok=True)
main(["--out", f"{out_dir}/twap_report.json", "--include-nfts"])
