"""
Shared test fixtures and mock data for the FLUX merger test suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_UNIT = (
    "97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec"
    "54616c6f73"
)
SHARDS_UNIT = (
    "ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a"
    "0014df10536861726473"
)

FLUX_MAX_SUPPLY_BASE = 1_000_000_000_000_000  # 1e15 (cMATRA: 6 decimals)
PUBLIC_POOL_BASE = 722_500_000_000_000  # v5.1: 72.25% of max supply (15% dilution)

# A valid 28-byte payment key hash (64 hex chars → 56 hex chars)
SAMPLE_PKH_1 = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
SAMPLE_PKH_2 = "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5"
SAMPLE_PKH_3 = "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"

# Admin reclaim fixtures
SAMPLE_ADMIN_PKH = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
SAMPLE_DEADLINE_MS = 1_750_000_000_000  # POSIX ms — mid-2025

SAMPLE_SCRIPT_ADDRESS = (
    "addr1w999999999999999999999999999999999999999999999999999999"
)

SAMPLE_FLUX_POLICY = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef01"
SAMPLE_FLUX_ASSET = "464c5558"

SAMPLE_NFT_POLICY = "0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a"


# ---------------------------------------------------------------------------
# Mock API response factories
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_block() -> dict[str, Any]:
    """A realistic Blockfrost block response."""
    return {
        "hash": "abc123def456abc123def456abc123def456abc123def456abc123def456abc1",
        "height": 10_500_000,
        "time": 1700000000,
        "slot": 110_000_000,
        "epoch": 450,
        "epoch_slot": 50000,
        "block_vrf": "vrf123",
        "output": "1000000000",
        "fees": "500000",
        "previous_block": "prev_hash",
        "next_block": None,
        "confirmations": 0,
        "size": 5000,
        "tx_count": 10,
    }


@pytest.fixture
def mock_agent_asset_info() -> dict[str, Any]:
    """Blockfrost asset info for AGENT (0 decimals)."""
    return {
        "asset": AGENT_UNIT,
        "policy_id": "97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec",
        "asset_name": "54616c6f73",
        "fingerprint": "asset1agent",
        "quantity": "50000000",
        "initial_mint_tx_hash": "tx_hash_agent",
        "mint_or_burn_count": 1,
        "onchain_metadata": None,
        "metadata": None,
    }


@pytest.fixture
def mock_shards_asset_info() -> dict[str, Any]:
    """Blockfrost asset info for SHARDS (6 decimals)."""
    return {
        "asset": SHARDS_UNIT,
        "policy_id": "ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a",
        "asset_name": "0014df10536861726473",
        "fingerprint": "asset1shards",
        "quantity": "100000000000000",
        "initial_mint_tx_hash": "tx_hash_shards",
        "mint_or_burn_count": 1,
        "onchain_metadata": None,
        "metadata": None,
    }


@pytest.fixture
def mock_agent_holders() -> list[dict[str, Any]]:
    """Sample AGENT holders from Blockfrost."""
    return [
        {"address": "addr1_holder_a", "quantity": "20000000"},
        {"address": "addr1_holder_b", "quantity": "15000000"},
        {"address": "addr1_holder_c", "quantity": "10000000"},
        {"address": "addr1_script_pool", "quantity": "5000000"},
    ]


@pytest.fixture
def mock_shards_holders() -> list[dict[str, Any]]:
    """Sample SHARDS holders from Blockfrost."""
    return [
        {"address": "addr1_holder_a", "quantity": "40000000000000"},
        {"address": "addr1_holder_b", "quantity": "30000000000000"},
        {"address": "addr1_holder_d", "quantity": "20000000000000"},
        {"address": "addr1_script_pool", "quantity": "10000000000000"},
    ]


@pytest.fixture
def mock_pools_agent() -> list[dict[str, Any]]:
    """Sample TapTools pool data for AGENT."""
    return [
        {
            "onchainID": "pool_agent_1",
            "exchange": "minswap",
            "tokenA": AGENT_UNIT,
            "tokenATicker": "AGENT",
            "tokenALocked": 1000000,
            "tokenB": "",
            "tokenBTicker": "ADA",
            "tokenBLocked": 500000,
        },
        {
            "onchainID": "pool_agent_2",
            "exchange": "sundaeswap",
            "tokenA": AGENT_UNIT,
            "tokenATicker": "AGENT",
            "tokenALocked": 400000,
            "tokenB": "",
            "tokenBTicker": "ADA",
            "tokenBLocked": 200000,
        },
        {
            "onchainID": "pool_agent_3",
            "exchange": "wingriders",
            "tokenA": AGENT_UNIT,
            "tokenATicker": "AGENT",
            "tokenALocked": 10000,
            "tokenB": "",
            "tokenBTicker": "ADA",
            "tokenBLocked": 5000,
        },
    ]


@pytest.fixture
def mock_candles() -> list[dict[str, Any]]:
    """Sample OHLCV candle data."""
    candles = []
    base_price = 0.50
    for i in range(168):
        p = base_price + (i % 10) * 0.01
        candles.append({
            "open": p - 0.005,
            "high": p + 0.01,
            "low": p - 0.01,
            "close": p,
            "volume": 10000 + i * 100,
            "time": 1700000000 + i * 3600,
        })
    return candles


@pytest.fixture
def mock_twap_report() -> dict[str, Any]:
    """A complete TWAP report as produced by Phase 1."""
    return {
        "report_type": "twap_snapshot_pools",
        "generated_at": "2024-01-01T00:00:00+00:00",
        "parameters": {
            "primary_window": "7d",
            "extra_windows": ["24h", "30d"],
            "min_tvl_ada": 10000,
            "top_pools": 3,
            "combine_mode": "median",
        },
        "ada_usd_price": 0.50,
        "tokens": {
            "AGENT": {
                "unit": AGENT_UNIT,
                "decimals": 0,
                "eligible_pools": 2,
                "pools": [],
                "combined_twap": {
                    "window": "7d",
                    "mode": "median",
                    "ada": 2.0,
                    "usd": 1.0,
                    "per_pool_twaps_ada": [1.8, 2.0, 2.2],
                },
            },
            "SHARDS": {
                "unit": SHARDS_UNIT,
                "decimals": 6,
                "eligible_pools": 2,
                "pools": [],
                "combined_twap": {
                    "window": "7d",
                    "mode": "median",
                    "ada": 0.001,
                    "usd": 0.0005,
                    "per_pool_twaps_ada": [0.0009, 0.001, 0.0011],
                },
            },
        },
    }


@pytest.fixture
def mock_merge_report() -> dict[str, Any]:
    """A complete merge report as produced by Phase 2."""
    # AGENT: 50M supply * $1.0 = $50M valuation
    # SHARDS: 100M display supply * $0.0005 = $50K valuation
    # Total: $50,050,000
    # AGENT weight ≈ 0.999001
    # SHARDS weight ≈ 0.000999
    agent_bucket = int(0.999001 * PUBLIC_POOL_BASE)
    shards_bucket = PUBLIC_POOL_BASE - agent_bucket

    return {
        "report_type": "flux_merge_valuation",
        "generated_at": "2024-01-01T00:00:00+00:00",
        "max_supply_base_units": FLUX_MAX_SUPPLY_BASE,
        "public_pool_base_units": PUBLIC_POOL_BASE,
        "validator_reserve_base_units": FLUX_MAX_SUPPLY_BASE - PUBLIC_POOL_BASE,
        "tokens": {
            "AGENT": {
                "unit": AGENT_UNIT,
                "decimals": 0,
                "supply_base_units": 50_000_000,
                "supply_display": 50_000_000.0,
                "twap_usd": 1.0,
                "valuation_usd": 50_000_000.0,
                "weight": 0.999001,
                "flux_bucket_base_units": agent_bucket,
                "flux_bucket_display": agent_bucket / 1e12,
            },
            "SHARDS": {
                "unit": SHARDS_UNIT,
                "decimals": 6,
                "supply_base_units": 100_000_000_000_000,
                "supply_display": 100_000_000.0,
                "twap_usd": 0.0005,
                "valuation_usd": 50_000.0,
                "weight": 0.000999,
                "flux_bucket_base_units": shards_bucket,
                "flux_bucket_display": shards_bucket / 1e12,
            },
        },
        "totals": {
            "total_valuation_usd": 50_050_000.0,
            "sum_weights": 1.0,
            "sum_buckets_base_units": PUBLIC_POOL_BASE,
            "buckets_sum_equals_pool": True,
        },
        "warnings": [],
    }


@pytest.fixture
def mock_manifest() -> dict[str, Any]:
    """A minimal claim vault manifest."""
    return {
        "manifest_type": "claim_vault",
        "script_address": "addr1w_claim_script",
        "flux_policy_hex": SAMPLE_FLUX_POLICY,
        "flux_asset_hex": SAMPLE_FLUX_ASSET,
        "totals": {
            "num_batches": 1,
            "num_claimants": 2,
            "total_flux_units": 1000000,
            "total_ada_lovelace": 3000000,
        },
        "batches": [
            {
                "batch_index": 0,
                "tx_hash": "aabbccdd" * 8,
                "num_outputs": 2,
                "total_flux_units": 1000000,
                "total_ada_lovelace": 3000000,
                "claimants": [
                    {
                        "payment_key_hash_hex": SAMPLE_PKH_1,
                        "flux_units": 600000,
                    },
                    {
                        "payment_key_hash_hex": SAMPLE_PKH_2,
                        "flux_units": 400000,
                    },
                ],
            },
        ],
    }


@pytest.fixture
def tmp_audit_dir(tmp_path: Path) -> Path:
    """Create a temporary audit pack directory."""
    d = tmp_path / "audit_pack" / "test"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mock_nft_candles() -> list[dict[str, Any]]:
    """Sample NFT floor-price OHLCV candles."""
    candles = []
    base_price = 50.0  # 50 ADA floor
    for i in range(7):
        p = base_price + (i % 3) * 5.0
        candles.append({
            "open": p - 2.0,
            "high": p + 5.0,
            "low": p - 5.0,
            "close": p,
            "volume": 100 + i * 10,
            "time": 1700000000 + i * 86400,
        })
    return candles


@pytest.fixture
def mock_policy_assets() -> list[dict[str, Any]]:
    """Sample Blockfrost policy assets response (3 NFTs under a policy)."""
    return [
        {"asset": SAMPLE_NFT_POLICY + "4e465431", "quantity": "1"},
        {"asset": SAMPLE_NFT_POLICY + "4e465432", "quantity": "1"},
        {"asset": SAMPLE_NFT_POLICY + "4e465433", "quantity": "1"},
    ]


@pytest.fixture
def mock_twap_report_with_nfts() -> dict[str, Any]:
    """A TWAP report with both fungible tokens and NFT collections."""
    return {
        "report_type": "twap_snapshot_pools",
        "generated_at": "2024-01-01T00:00:00+00:00",
        "ada_usd_price": 0.50,
        "tokens": {
            "AGENT": {
                "unit": AGENT_UNIT,
                "decimals": 0,
                "combined_twap": {"ada": 2.0, "usd": 1.0},
            },
            "SHARDS": {
                "unit": SHARDS_UNIT,
                "decimals": 6,
                "combined_twap": {"ada": 0.001, "usd": 0.0005},
            },
            "FLUX_PASS": {
                "policy_id": "0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a",
                "is_nft": True,
                "decimals": 0,
                "combined_twap": {"ada": 100.0, "usd": 50.0},
            },
            "SE_BRAWLERS": {
                "policy_id": "25c75bbf105310685d51cd3adbdd50b72fdbd99be2cc3757dde7eafc",
                "is_nft": True,
                "decimals": 0,
                "combined_twap": {"ada": 30.0, "usd": 15.0},
            },
            "BRAWL_PASS_ETD": {
                "policy_id": "d3a197c4814054623432c882c60e6a81e8f3b94158033432529a02d2",
                "is_nft": True,
                "decimals": 0,
                "combined_twap": {"ada": 20.0, "usd": 10.0},
            },
            "T1_ADAM_PASS": {
                "policy_id": "b46891456b77dbc77c16090fd92a37f087f9a68e953c56b00a20332f",
                "is_nft": True,
                "decimals": 0,
                "combined_twap": {"ada": 10.0, "usd": 5.0},
            },
            "T2_ADAM_PASS": {
                "policy_id": "06a64965c0ac1144a72a6ddfcb23aa9d4d7742a5b20ddd5cfb1164b9",
                "is_nft": True,
                "decimals": 0,
                "combined_twap": {"ada": 5.0, "usd": 2.5},
            },
        },
    }
