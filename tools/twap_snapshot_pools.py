#!/usr/bin/env python3
"""
Phase 1 — TWAP & Pool Selection Report

Discovers DEX pools for AGENT and SHARDS via TapTools, filters by TVL,
fetches OHLCV candles per pool, and computes time-weighted average prices
using a manipulation-resistant aggregation strategy (median by default).

Outputs:
  - JSON report (selected pools, per-pool TWAPs, combined TWAP)
  - Optional CSV candle exports
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.api_clients import TapToolsClient
from tools.config import (
    AGENT,
    DEFAULT_COMBINE_MODE,
    DEFAULT_MIN_TVL_ADA,
    DEFAULT_TOP_POOLS,
    DEFAULT_TWAP_CANDLE_INTERVAL,
    DEFAULT_TWAP_WINDOW_HOURS,
    LEGACY_TOKENS,
    SHARDS,
    TokenInfo,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TWAP math
# ---------------------------------------------------------------------------


def compute_twap(candles: list[dict[str, Any]], price_key: str = "close") -> float:
    """Compute a simple time-weighted average price from uniform candles.

    For uniform-interval candles the TWAP is just the arithmetic mean of
    closing prices (each candle has equal time weight).
    """
    prices = [float(c[price_key]) for c in candles if c.get(price_key) is not None]
    if not prices:
        return 0.0
    return sum(prices) / len(prices)


def combine_twaps(
    per_pool_twaps: list[float],
    mode: str = "median",
) -> float:
    """Combine per-pool TWAPs into a single aggregate value."""
    valid = [t for t in per_pool_twaps if t > 0]
    if not valid:
        return 0.0
    if mode == "median":
        return statistics.median(valid)
    if mode == "deepest":
        # Caller should pre-sort by TVL; take the first (deepest).
        return valid[0]
    if mode == "mean":
        return statistics.mean(valid)
    raise ValueError(f"Unknown combine mode: {mode}")


# ---------------------------------------------------------------------------
# Pool discovery + filtering
# ---------------------------------------------------------------------------


def _get_pool_tvl_ada(pool: dict[str, Any]) -> float:
    """Extract ADA-side TVL from a TapTools pool response.

    TapTools returns tokenALocked/tokenBLocked.  The ADA side is whichever
    token has ticker "ADA" or an empty unit string.
    """
    if pool.get("tokenBTicker") == "ADA" or pool.get("tokenB") == "":
        return float(pool.get("tokenBLocked", 0))
    if pool.get("tokenATicker") == "ADA" or pool.get("tokenA") == "":
        return float(pool.get("tokenALocked", 0))
    # Fallback: try legacy field
    return float(pool.get("adaLocked", 0))


def discover_pools(
    client: TapToolsClient,
    token: TokenInfo,
    min_tvl_ada: int,
    top_n: int,
) -> list[dict[str, Any]]:
    """Return top-N pools for *token* filtered by TVL."""
    raw_pools = client.get_token_pools(token.unit)
    # Filter by minimum TVL
    filtered = [
        p for p in raw_pools
        if _get_pool_tvl_ada(p) >= min_tvl_ada
    ]
    # Sort by TVL descending
    filtered.sort(key=lambda p: _get_pool_tvl_ada(p), reverse=True)
    return filtered[:top_n]


# ---------------------------------------------------------------------------
# Per-window TWAP computation
# ---------------------------------------------------------------------------

# Standard window configs: (label, interval, num_intervals)
WINDOW_CONFIGS = {
    "7d": ("1h", 168),
    "24h": ("15m", 96),
    "30d": ("4h", 180),
}


def compute_pool_twap(
    client: TapToolsClient,
    pool_id: str,
    interval: str,
    num_intervals: int,
) -> dict[str, Any]:
    """Fetch candles for a pool and compute TWAP + metadata."""
    candles = client.get_token_pool_ohlcv(pool_id, interval, num_intervals)
    twap = compute_twap(candles)
    close_prices = [float(c["close"]) for c in candles if c.get("close")]
    return {
        "pool_id": pool_id,
        "interval": interval,
        "num_candles_requested": num_intervals,
        "num_candles_received": len(candles),
        "twap": twap,
        "latest_close": close_prices[-1] if close_prices else None,
        "min_close": min(close_prices) if close_prices else None,
        "max_close": max(close_prices) if close_prices else None,
    }


# ---------------------------------------------------------------------------
# Full report builder
# ---------------------------------------------------------------------------


def build_twap_report(
    client: TapToolsClient,
    tokens: list[TokenInfo] | None = None,
    primary_window: str = "7d",
    extra_windows: list[str] | None = None,
    min_tvl_ada: int = DEFAULT_MIN_TVL_ADA,
    top_pools: int = DEFAULT_TOP_POOLS,
    combine_mode: str = DEFAULT_COMBINE_MODE,
    ada_usd_price: float | None = None,
) -> dict[str, Any]:
    """Build the complete TWAP report for all legacy tokens."""
    tokens = tokens or LEGACY_TOKENS
    extra_windows = extra_windows or ["24h", "30d"]

    if ada_usd_price is None:
        ada_usd_price = client.get_ada_price()

    report_time = datetime.now(timezone.utc).isoformat()
    token_reports: dict[str, Any] = {}

    for token in tokens:
        logger.info("Processing %s ...", token.name)

        pools = discover_pools(client, token, min_tvl_ada, top_pools)
        if not pools:
            logger.warning("No eligible pools for %s (min TVL=%d ADA)", token.name, min_tvl_ada)

        pool_entries = []
        for pool in pools:
            pool_id = pool.get("onchainID") or pool.get("pairID", "")
            entry: dict[str, Any] = {
                "pool_id": pool_id,
                "dex": pool.get("exchange", "unknown"),
                "tvl_ada": _get_pool_tvl_ada(pool),
                "windows": {},
            }

            # Primary window
            interval, num = WINDOW_CONFIGS[primary_window]
            entry["windows"][primary_window] = compute_pool_twap(
                client, pool_id, interval, num,
            )

            # Extra windows (sanity checks)
            for w in extra_windows:
                if w in WINDOW_CONFIGS:
                    wi, wn = WINDOW_CONFIGS[w]
                    entry["windows"][w] = compute_pool_twap(client, pool_id, wi, wn)

            pool_entries.append(entry)

        # Combine per-pool primary TWAPs
        primary_twaps = [
            pe["windows"][primary_window]["twap"]
            for pe in pool_entries
            if pe["windows"].get(primary_window, {}).get("twap", 0) > 0
        ]
        combined_twap_ada = combine_twaps(primary_twaps, combine_mode)
        combined_twap_usd = combined_twap_ada * ada_usd_price

        token_reports[token.name] = {
            "unit": token.unit,
            "decimals": token.decimals,
            "eligible_pools": len(pools),
            "pools": pool_entries,
            "combined_twap": {
                "window": primary_window,
                "mode": combine_mode,
                "ada": combined_twap_ada,
                "usd": combined_twap_usd,
                "per_pool_twaps_ada": primary_twaps,
            },
        }

    return {
        "report_type": "twap_snapshot_pools",
        "generated_at": report_time,
        "parameters": {
            "primary_window": primary_window,
            "extra_windows": extra_windows,
            "min_tvl_ada": min_tvl_ada,
            "top_pools": top_pools,
            "combine_mode": combine_mode,
        },
        "ada_usd_price": ada_usd_price,
        "tokens": token_reports,
    }


# ---------------------------------------------------------------------------
# CSV export helpers
# ---------------------------------------------------------------------------


def export_candles_csv(
    candles: list[dict[str, Any]],
    out_path: Path,
) -> None:
    """Write candle data to CSV."""
    if not candles:
        return
    fieldnames = list(candles[0].keys())
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(candles)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX merger — Phase 1: TWAP & Pool Selection Report",
    )
    parser.add_argument(
        "--interval", default=DEFAULT_TWAP_CANDLE_INTERVAL,
        help="Candle interval for primary window (default: 1h)",
    )
    parser.add_argument(
        "--num-intervals", type=int, default=DEFAULT_TWAP_WINDOW_HOURS,
        help="Number of candles for primary window (default: 168 = 7d)",
    )
    parser.add_argument(
        "--top-pools", type=int, default=DEFAULT_TOP_POOLS,
        help="Top N pools by TVL per token (default: 3)",
    )
    parser.add_argument(
        "--min-tvl-ada", type=int, default=DEFAULT_MIN_TVL_ADA,
        help="Minimum pool TVL in ADA (default: 10000)",
    )
    parser.add_argument(
        "--combine", default=DEFAULT_COMBINE_MODE,
        choices=["median", "deepest", "mean"],
        help="How to combine per-pool TWAPs (default: median)",
    )
    parser.add_argument(
        "--quote", default="USD", choices=["USD", "ADA"],
        help="Quote currency for report (default: USD)",
    )
    parser.add_argument(
        "--out", type=str, required=True,
        help="Output path for JSON report",
    )
    parser.add_argument(
        "--export-candles-dir", type=str, default=None,
        help="Optional directory to export per-pool candle CSVs",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    client = TapToolsClient()
    report = build_twap_report(
        client,
        min_tvl_ada=args.min_tvl_ada,
        top_pools=args.top_pools,
        combine_mode=args.combine,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info("TWAP report written to %s", out_path)


if __name__ == "__main__":
    main()
