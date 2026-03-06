#!/usr/bin/env python3
"""
Phase 2 — Merge Weights → Integer Token Buckets

Consumes the TWAP report (Phase 1 output or computes fresh),
fetches exact on-chain supplies from Blockfrost, computes valuations
and weights, then derives integer FLUX bucket sizes that sum to exactly 1e15.

Outputs:
  - merge_report.json
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
    NFT_COLLECTIONS,
    NftCollectionInfo,
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

    Assets with quantity > 1 under the same policy are fungible tokens,
    not NFTs, and are excluded from the count.
    """
    assets = bf.get_policy_assets(collection.policy_id)
    return sum(1 for a in assets if int(a.get("quantity", 1)) == 1)


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
    total_flux_base: int = FLUX_MAX_SUPPLY_BASE,
) -> dict[str, int]:
    """Allocate FLUX base units to each legacy token bucket.

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

    return {
        "report_type": "flux_merge_valuation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "flux_total_base_units": FLUX_MAX_SUPPLY_BASE,
        "tokens": token_details,
        "burn_address": burn_adjustments.get("_address"),
        "totals": {
            "total_valuation_usd": val_data["total_valuation_usd"],
            "sum_weights": sum(val_data["weights"].values()),
            "sum_buckets_base_units": sum(buckets.values()),
            "buckets_sum_equals_max": sum(buckets.values()) == FLUX_MAX_SUPPLY_BASE,
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX merger — Phase 2: Merge Weights & Integer Buckets",
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
    if report["totals"]["buckets_sum_equals_max"]:
        logger.info("Bucket sum == %d (OK)", FLUX_MAX_SUPPLY_BASE)
    else:
        logger.error("BUCKET SUM MISMATCH!")
        sys.exit(1)

    if report["warnings"]:
        for w in report["warnings"]:
            logger.warning("WARNING: %s", w)

    logger.info("Merge report written to %s", out_path)


if __name__ == "__main__":
    main()
