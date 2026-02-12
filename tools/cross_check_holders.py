#!/usr/bin/env python3
"""
Cross-Check Holders — compare Blockfrost vs Koios holder data.

Validates that both indexers agree on:
  - Holder count
  - Per-address balances
  - Total supply consistency

Outputs a discrepancy report JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from tools.api_clients import BlockfrostClient, KoiosClient
from tools.config import AGENT, SHARDS, TokenInfo

logger = logging.getLogger(__name__)


def fetch_blockfrost_holders(
    bf: BlockfrostClient,
    token: TokenInfo,
) -> dict[str, int]:
    """Fetch all holders from Blockfrost, return {address: quantity}."""
    holders = bf.get_asset_addresses(token.unit)
    return {
        h["address"]: int(h["quantity"])
        for h in holders
    }


def fetch_koios_holders(
    koios: KoiosClient,
    token: TokenInfo,
) -> dict[str, int]:
    """Fetch all holders from Koios, return {address: quantity}."""
    holders = koios.get_asset_addresses(token.policy_id, token.asset_name_hex)
    result: dict[str, int] = {}
    for h in holders:
        addr = h.get("payment_address") or h.get("address", "")
        qty = int(h.get("quantity", 0))
        if addr and qty > 0:
            result[addr] = result.get(addr, 0) + qty
    return result


def compare_holders(
    bf_holders: dict[str, int],
    koios_holders: dict[str, int],
    token_name: str,
) -> dict[str, Any]:
    """Compare two holder maps and produce a discrepancy report."""
    all_addrs = set(bf_holders.keys()) | set(koios_holders.keys())
    bf_only = set(bf_holders.keys()) - set(koios_holders.keys())
    koios_only = set(koios_holders.keys()) - set(bf_holders.keys())

    balance_mismatches: list[dict[str, Any]] = []
    for addr in all_addrs:
        bf_qty = bf_holders.get(addr, 0)
        ko_qty = koios_holders.get(addr, 0)
        if bf_qty != ko_qty:
            balance_mismatches.append({
                "address": addr,
                "blockfrost_qty": bf_qty,
                "koios_qty": ko_qty,
                "diff": bf_qty - ko_qty,
            })

    bf_total = sum(bf_holders.values())
    ko_total = sum(koios_holders.values())

    return {
        "token": token_name,
        "blockfrost_holder_count": len(bf_holders),
        "koios_holder_count": len(koios_holders),
        "holder_count_match": len(bf_holders) == len(koios_holders),
        "blockfrost_total_supply": bf_total,
        "koios_total_supply": ko_total,
        "supply_match": bf_total == ko_total,
        "blockfrost_only_addresses": len(bf_only),
        "koios_only_addresses": len(koios_only),
        "balance_mismatches": len(balance_mismatches),
        "discrepancies": balance_mismatches[:50],  # cap detail output
        "all_match": (
            len(bf_holders) == len(koios_holders)
            and bf_total == ko_total
            and len(balance_mismatches) == 0
        ),
    }


def run_cross_check(
    bf: BlockfrostClient,
    koios: KoiosClient,
    tokens: list[TokenInfo] | None = None,
) -> dict[str, Any]:
    """Run the full cross-check and return a report dict."""
    if tokens is None:
        tokens = [AGENT, SHARDS]

    results: list[dict[str, Any]] = []

    for token in tokens:
        logger.info("Cross-checking %s...", token.name)

        bf_holders = fetch_blockfrost_holders(bf, token)
        logger.info("  Blockfrost: %d holders", len(bf_holders))

        koios_holders = fetch_koios_holders(koios, token)
        logger.info("  Koios: %d holders", len(koios_holders))

        comparison = compare_holders(bf_holders, koios_holders, token.name)
        results.append(comparison)

        if comparison["all_match"]:
            logger.info("  %s: ALL MATCH", token.name)
        else:
            logger.warning(
                "  %s: DISCREPANCIES — holders: BF=%d vs Ko=%d, "
                "supply: BF=%d vs Ko=%d, mismatches=%d",
                token.name,
                comparison["blockfrost_holder_count"],
                comparison["koios_holder_count"],
                comparison["blockfrost_total_supply"],
                comparison["koios_total_supply"],
                comparison["balance_mismatches"],
            )

    all_pass = all(r["all_match"] for r in results)

    return {
        "report_type": "cross_check_holders",
        "all_pass": all_pass,
        "tokens": results,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX merger — Cross-Check Holders (Blockfrost vs Koios)",
    )
    parser.add_argument("--out-json", type=str, default=None,
                        help="Write report to JSON file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    bf = BlockfrostClient()
    koios = KoiosClient()

    report = run_cross_check(bf, koios)

    print(json.dumps(report, indent=2))

    if args.out_json:
        from pathlib import Path
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Report written to %s", out_path)

    if not report["all_pass"]:
        logger.warning("Cross-check found discrepancies!")
        sys.exit(1)


if __name__ == "__main__":
    main()
