#!/usr/bin/env python3
"""
Phase 2 — Merge Weights → Integer Token Buckets + Redemption Rate Table

Consumes the TWAP report (Phase 1 output or computes fresh),
fetches exact on-chain supplies from Blockfrost, computes valuations
and weights, then derives integer cMATRA bucket sizes that sum to
the public redemption pool (85% of max supply).

Outputs:
  - merge_valuation_cmatra.json  (buckets + weights)
  - rate_table.json              (fixed per-asset redemption rates)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.api_clients import BlockfrostClient, TapToolsClient
from tools.config import (
    AGENT,
    FLUX_DECIMALS,
    FLUX_MAX_SUPPLY_BASE,
    LEGACY_TOKENS,
    MERGE_TOKEN_SUPPLY_BASE,
    NFT_COLLECTIONS,
    NftCollectionInfo,
    PUBLIC_POOL_BASE,
    VALIDATOR_RESERVE_BASE,
    filter_nft_assets,
    SHARDS,
    TokenInfo,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supply fetching
# ---------------------------------------------------------------------------


def fetch_supply(bf: BlockfrostClient, token: TokenInfo) -> int:
    """Fetch total on-chain supply for *token* in base units."""
    info = bf.get_asset_info(token.unit)
    return int(info["quantity"])


def fetch_nft_supply(bf: BlockfrostClient, collection: NftCollectionInfo) -> int:
    """Fetch NFT supply for a collection (count of true 1/1 NFTs only).

    - Assets with quantity > 1 are fungible tokens, excluded.
    - CIP-68 reference tokens (000643b0 prefix) are excluded.
    - For CIP-68 collections, only user tokens (000de140 prefix) are counted.
    """
    assets = bf.get_policy_assets(collection.policy_id)
    return len(filter_nft_assets(assets))


# ---------------------------------------------------------------------------
# Valuation + weights
# ---------------------------------------------------------------------------


def compute_valuations(
    tokens: list[TokenInfo | NftCollectionInfo],
    supplies: dict[str, int],
    twap_prices_usd: dict[str, float],
) -> dict[str, Any]:
    """Compute per-token valuations and weights.

    *twap_prices_usd*: token-name → price per **display unit** in USD.
    *supplies*: token-name → total supply in **base units**.
    """
    valuations: dict[str, float] = {}
    for t in tokens:
        display_supply = supplies[t.name] / (10 ** t.decimals)
        price = twap_prices_usd[t.name]
        valuations[t.name] = price * display_supply

    total_val = sum(valuations.values())
    if total_val <= 0:
        raise ValueError("Total valuation is zero or negative — check TWAP prices")

    weights = {name: v / total_val for name, v in valuations.items()}
    return {
        "valuations_usd": valuations,
        "total_valuation_usd": total_val,
        "weights": weights,
    }


# ---------------------------------------------------------------------------
# Integer bucket allocation
# ---------------------------------------------------------------------------


def compute_integer_buckets(
    tokens: list[TokenInfo | NftCollectionInfo],
    weights: dict[str, float],
    total_flux_base: int = PUBLIC_POOL_BASE,
) -> dict[str, int]:
    """Allocate cMATRA base units to each legacy token bucket.

    Default pool is PUBLIC_POOL_BASE (85% of max supply).
    Uses floor for all tokens except the last, which receives the remainder
    to guarantee sum == total_flux_base.
    """
    buckets: dict[str, int] = {}
    allocated = 0

    for i, t in enumerate(tokens):
        if i < len(tokens) - 1:
            b = int(weights[t.name] * total_flux_base)  # floor
            buckets[t.name] = b
            allocated += b
        else:
            # Last token gets the remainder
            buckets[t.name] = total_flux_base - allocated

    assert sum(buckets.values()) == total_flux_base, (
        f"Bucket sum {sum(buckets.values())} != {total_flux_base}"
    )
    return buckets


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def build_merge_report(
    bf: BlockfrostClient,
    twap_report: dict[str, Any] | None = None,
    twap_report_path: Path | None = None,
    tokens: list[TokenInfo] | None = None,
    nft_collections: list[NftCollectionInfo] | None = None,
    burn_adjustments: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build the merge report containing weights and integer buckets.

    *burn_adjustments*: token-name → base units to subtract from on-chain
    supply (permanently locked / burned tokens).
    """
    tokens = tokens or LEGACY_TOKENS
    burn_adjustments = burn_adjustments or {}
    nft_collections = nft_collections if nft_collections is not None else NFT_COLLECTIONS

    # Load TWAP data
    if twap_report is None and twap_report_path is not None:
        with open(twap_report_path) as f:
            twap_report = json.load(f)

    if twap_report is None:
        raise ValueError("Must provide either twap_report or twap_report_path")

    # Build unified asset list
    all_assets: list[TokenInfo | NftCollectionInfo] = list(tokens) + list(nft_collections)

    # Extract prices
    twap_prices_usd: dict[str, float] = {}
    for asset in all_assets:
        token_data = twap_report["tokens"].get(asset.name, {})
        combined = token_data.get("combined_twap", {})
        twap_prices_usd[asset.name] = combined.get("usd", 0.0)
        if twap_prices_usd[asset.name] <= 0:
            logger.warning("TWAP price for %s is zero or missing!", asset.name)

    # Fetch supplies
    raw_supplies: dict[str, int] = {}
    supplies: dict[str, int] = {}
    for asset in all_assets:
        if isinstance(asset, TokenInfo):
            raw = fetch_supply(bf, asset)
        else:
            raw = fetch_nft_supply(bf, asset)
        raw_supplies[asset.name] = raw
        burned = burn_adjustments.get(asset.name, 0)
        supplies[asset.name] = raw - burned
        if burned > 0:
            logger.info(
                "%s supply: %d on-chain - %d burned = %d circulating (%.2f display)",
                asset.name, raw, burned, supplies[asset.name],
                supplies[asset.name] / (10 ** asset.decimals),
            )
        else:
            logger.info(
                "%s supply: %d base units (%.2f display)",
                asset.name, supplies[asset.name],
                supplies[asset.name] / (10 ** asset.decimals),
            )

    # Valuations + weights
    val_data = compute_valuations(all_assets, supplies, twap_prices_usd)

    # Integer buckets
    buckets = compute_integer_buckets(all_assets, val_data["weights"])

    warnings: list[str] = []
    for asset in all_assets:
        if supplies[asset.name] == 0:
            warnings.append(f"{asset.name} has zero supply")
        if twap_prices_usd[asset.name] <= 0:
            warnings.append(f"{asset.name} has zero/missing TWAP price")

    token_details: dict[str, Any] = {}
    for asset in all_assets:
        entry: dict[str, Any] = {
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
        if isinstance(asset, TokenInfo):
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
        "max_supply_base_units": MERGE_TOKEN_SUPPLY_BASE,
        "public_pool_base_units": PUBLIC_POOL_BASE,
        "validator_reserve_base_units": VALIDATOR_RESERVE_BASE,
        "tokens": token_details,
        "burn_address": burn_adjustments.get("_address"),
        "totals": {
            "total_valuation_usd": val_data["total_valuation_usd"],
            "sum_weights": sum(val_data["weights"].values()),
            "sum_buckets_base_units": bucket_total,
            "buckets_sum_equals_pool": bucket_total == PUBLIC_POOL_BASE,
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Rate table builder
# ---------------------------------------------------------------------------


def build_rate_table(
    merge_report: dict[str, Any],
    team_waiver_supplies: dict[str, int] | None = None,
    legacy_reward_adjustments: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a fixed redemption rate table from the merge report.

    *team_waiver_supplies*: asset-name → base units excluded (team treasury).
    *legacy_reward_adjustments*: asset-name → base units of materialized
    rewards added to redeemable supply.

    Rate = asset_bucket / redeemable_supply  (integer floor division in base units).
    """
    team_waivers = team_waiver_supplies or {}
    reward_adj = legacy_reward_adjustments or {}

    tokens: dict[str, Any] = {}
    for name, entry in merge_report["tokens"].items():
        bucket = entry["flux_bucket_base_units"]
        on_chain_supply = entry["supply_base_units"]
        decimals = entry["decimals"]
        waived = team_waivers.get(name, 0)
        materialized = reward_adj.get(name, 0)

        redeemable = on_chain_supply - waived + materialized
        if redeemable <= 0:
            rate_base = 0
        else:
            rate_base = bucket // redeemable

        tokens[name] = {
            "bucket_base": bucket,
            "bucket_display": bucket / (10 ** FLUX_DECIMALS),
            "on_chain_supply_base": on_chain_supply,
            "team_waiver_base": waived,
            "legacy_reward_adjustment_base": materialized,
            "redeemable_supply_base": redeemable,
            "redeemable_supply_display": redeemable / (10 ** decimals),
            "rate_base_per_unit": rate_base,
            "rate_display": rate_base / (10 ** FLUX_DECIMALS),
            "is_nft": entry.get("is_nft", False),
        }

    # Compute team carve-out: cMATRA owed to team for waived treasury balances
    team_carve: dict[str, Any] = {}
    total_carve_base = 0
    for asset_name, waiver_base in team_waivers.items():
        if waiver_base > 0 and asset_name in tokens:
            rate = tokens[asset_name]["rate_base_per_unit"]
            carve = waiver_base * rate
            team_carve[asset_name] = {
                "waiver_base": waiver_base,
                "rate_base_per_unit": rate,
                "carve_cmatra_base": carve,
                "carve_cmatra_display": carve / (10 ** FLUX_DECIMALS),
            }
            total_carve_base += carve

    pool_base = merge_report["public_pool_base_units"]
    assert total_carve_base < pool_base, (
        f"Team carve ({total_carve_base}) exceeds public pool ({pool_base})"
    )

    result = {
        "report_type": "redemption_rate_table",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "public_pool_base": pool_base,
        "public_pool_display": pool_base / (10 ** FLUX_DECIMALS),
        "validator_reserve_base": merge_report["validator_reserve_base_units"],
        "team_waiver_supplies": team_waivers,
        "legacy_reward_adjustments": reward_adj,
        "tokens": tokens,
    }

    if team_carve:
        pool_after_carve = pool_base - total_carve_base
        result["team_carve"] = {
            "per_asset": team_carve,
            "total_carve_base": total_carve_base,
            "total_carve_display": total_carve_base / (10 ** FLUX_DECIMALS),
            "pool_after_carve_base": pool_after_carve,
            "pool_after_carve_display": pool_after_carve / (10 ** FLUX_DECIMALS),
        }

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="cMATRA merger — Phase 2: Merge Weights, Integer Buckets & Rate Table",
    )
    parser.add_argument(
        "--twap-report", type=str, required=True,
        help="Path to Phase 1 TWAP report JSON",
    )
    parser.add_argument(
        "--out-json", type=str, required=True,
        help="Output path for merge report JSON",
    )
    parser.add_argument(
        "--out-rate-table", type=str, default=None,
        help="Output path for redemption rate table JSON",
    )
    parser.add_argument(
        "--team-waiver", type=str, nargs="*", default=[],
        help="Team waiver as TOKEN:BASE_UNITS (e.g. AGENT:29644656)",
    )
    parser.add_argument(
        "--burn", type=str, nargs="*", default=[],
        help="Burn adjustments as TOKEN:BASE_UNITS (e.g. AGENT:4002 SHARDS:88551450001)",
    )
    parser.add_argument(
        "--burn-address", type=str, default=None,
        help="Address of the burn contract (for audit trail)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Parse burn adjustments
    burn_adj: dict[str, Any] = {}
    for b in args.burn:
        token_name, amount_str = b.split(":")
        burn_adj[token_name] = int(amount_str)
    if args.burn_address:
        burn_adj["_address"] = args.burn_address

    # Parse team waivers
    team_waivers: dict[str, int] = {}
    for w in args.team_waiver:
        token_name, amount_str = w.split(":")
        team_waivers[token_name] = int(amount_str)

    bf = BlockfrostClient()
    report = build_merge_report(
        bf, twap_report_path=Path(args.twap_report),
        burn_adjustments=burn_adj if burn_adj else None,
    )

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    # Validation
    if report["totals"]["buckets_sum_equals_pool"]:
        logger.info("Bucket sum == %d (PUBLIC_POOL_BASE OK)", PUBLIC_POOL_BASE)
    else:
        logger.error("BUCKET SUM MISMATCH!")
        sys.exit(1)

    if report["warnings"]:
        for w in report["warnings"]:
            logger.warning("WARNING: %s", w)

    logger.info("Merge report written to %s", out_path)

    # Rate table
    if args.out_rate_table:
        rate_table = build_rate_table(
            report,
            team_waiver_supplies=team_waivers if team_waivers else None,
        )
        rt_path = Path(args.out_rate_table)
        rt_path.parent.mkdir(parents=True, exist_ok=True)
        with open(rt_path, "w") as f:
            json.dump(rate_table, f, indent=2)
        logger.info("Rate table written to %s", rt_path)


if __name__ == "__main__":
    main()
