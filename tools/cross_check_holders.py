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

from tools.api_clients import BlockfrostClient, KoiosClient, TapToolsClient
from tools.config import AGENT, NFT_COLLECTIONS, NftCollectionInfo, SHARDS, TokenInfo

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


def fetch_taptools_nft_holders(
    tt: TapToolsClient,
    collection: NftCollectionInfo,
    max_pages: int = 10,
) -> dict[str, int]:
    """Fetch top NFT holders from TapTools (paginated, best-effort)."""
    all_holders: dict[str, int] = {}
    for page in range(1, max_pages + 1):
        data = tt.get_nft_collection_holders_top(
            collection.policy_id, page=page, per_page=100,
        )
        if not data:
            break
        for entry in data:
            addr = entry.get("address") or entry.get("ownerAddress", "")
            qty = int(entry.get("quantity", entry.get("amount", 0)))
            if addr and qty > 0:
                all_holders[addr] = all_holders.get(addr, 0) + qty
        if len(data) < 100:
            break
    return all_holders


def fetch_blockfrost_nft_holders(
    bf: BlockfrostClient,
    collection: NftCollectionInfo,
) -> dict[str, int]:
    """Fetch all NFT holders via Blockfrost policy asset enumeration."""
    assets = bf.get_policy_assets(collection.policy_id)
    holder_counts: dict[str, int] = {}
    for asset_entry in assets:
        unit = asset_entry.get("asset", "")
        if not unit:
            continue
        addresses = bf.get_asset_addresses(unit)
        for addr_entry in addresses:
            addr = addr_entry["address"]
            qty = int(addr_entry.get("quantity", 1))
            holder_counts[addr] = holder_counts.get(addr, 0) + qty
    return holder_counts


def compare_nft_holders(
    bf_holders: dict[str, int],
    tt_holders: dict[str, int],
    collection_name: str,
) -> dict[str, Any]:
    """Compare Blockfrost vs TapTools NFT holders (best-effort)."""
    # TapTools may only return top holders, so we only compare the overlap
    common_addrs = set(bf_holders.keys()) & set(tt_holders.keys())
    tt_only = set(tt_holders.keys()) - set(bf_holders.keys())

    balance_mismatches: list[dict[str, Any]] = []
    for addr in common_addrs:
        bf_qty = bf_holders.get(addr, 0)
        tt_qty = tt_holders.get(addr, 0)
        if bf_qty != tt_qty:
            balance_mismatches.append({
                "address": addr,
                "blockfrost_qty": bf_qty,
                "taptools_qty": tt_qty,
                "diff": bf_qty - tt_qty,
            })

    bf_total = sum(bf_holders.values())
    tt_total = sum(tt_holders.values())

    return {
        "token": collection_name,
        "mode": "best_effort",
        "is_nft": True,
        "blockfrost_holder_count": len(bf_holders),
        "taptools_holder_count": len(tt_holders),
        "common_addresses": len(common_addrs),
        "taptools_only_addresses": len(tt_only),
        "blockfrost_total_supply": bf_total,
        "taptools_total_supply": tt_total,
        "supply_match": bf_total == tt_total,
        "balance_mismatches": len(balance_mismatches),
        "discrepancies": balance_mismatches[:50],
        "all_match": len(balance_mismatches) == 0 and bf_total == tt_total,
    }


def run_cross_check(
    bf: BlockfrostClient,
    koios: KoiosClient,
    taptools: TapToolsClient | None = None,
    tokens: list[TokenInfo] | None = None,
    nft_collections: list[NftCollectionInfo] | None = None,
) -> dict[str, Any]:
    """Run the full cross-check and return a report dict."""
    if tokens is None:
        tokens = [AGENT, SHARDS]
    if nft_collections is None:
        nft_collections = NFT_COLLECTIONS if taptools is not None else []

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

    # -- NFT collection cross-checks -------------------------------------
    for coll in nft_collections:
        if taptools is None:
            logger.warning("Skipping NFT cross-check for %s (no TapTools client)", coll.name)
            continue

        logger.info("Cross-checking NFT collection %s...", coll.name)

        bf_holders = fetch_blockfrost_nft_holders(bf, coll)
        logger.info("  Blockfrost: %d holders (%d total NFTs)",
                     len(bf_holders), sum(bf_holders.values()))

        tt_holders = fetch_taptools_nft_holders(taptools, coll)
        logger.info("  TapTools: %d holders (%d total NFTs)",
                     len(tt_holders), sum(tt_holders.values()))

        comparison = compare_nft_holders(bf_holders, tt_holders, coll.name)
        results.append(comparison)

        if comparison["all_match"]:
            logger.info("  %s: ALL MATCH", coll.name)
        else:
            logger.warning(
                "  %s: DISCREPANCIES (best-effort) — holders: BF=%d vs TT=%d, "
                "supply: BF=%d vs TT=%d, mismatches=%d",
                coll.name,
                comparison["blockfrost_holder_count"],
                comparison["taptools_holder_count"],
                comparison["blockfrost_total_supply"],
                comparison["taptools_total_supply"],
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
    taptools = TapToolsClient()

    report = run_cross_check(bf, koios, taptools=taptools)

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
