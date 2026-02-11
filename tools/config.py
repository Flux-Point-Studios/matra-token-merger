"""
Shared configuration for the FLUX merger pipeline.

Loads environment variables and provides typed constants used across all tools.
Supports network switching via NETWORK env var (mainnet / preprod).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Try env.local first, then .env
for _env_file in ("env.local", ".env"):
    _candidate = _PROJECT_ROOT / _env_file
    if _candidate.exists():
        load_dotenv(_candidate)
        break

# ---------------------------------------------------------------------------
# Network selection
# ---------------------------------------------------------------------------

NETWORK: str = os.environ.get("NETWORK", "mainnet").lower()
assert NETWORK in ("mainnet", "preprod", "preview"), (
    f"NETWORK must be mainnet, preprod, or preview — got {NETWORK!r}"
)

# ---------------------------------------------------------------------------
# API keys (network-aware)
# ---------------------------------------------------------------------------

BLOCKFROST_PROJECT_ID: str = os.environ.get(
    f"BLOCKFROST_PROJECT_ID_{NETWORK.upper()}",
    os.environ.get("BLOCKFROST_PROJECT_ID", ""),
)
TAP_TOOLS_API_KEY: str = os.environ.get("TAP_TOOLS_API_KEY", "")

# ---------------------------------------------------------------------------
# Token identifiers (mainnet defaults — overridden for testnet runs)
# ---------------------------------------------------------------------------

AGENT_POLICY: str = os.environ.get(
    "AGENT_POLICY",
    "97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec",
)
AGENT_NAME_HEX: str = os.environ.get("AGENT_NAME_HEX", "54616c6f73")
AGENT_UNIT: str = AGENT_POLICY + AGENT_NAME_HEX
AGENT_DECIMALS: int = 0

SHARDS_POLICY: str = os.environ.get(
    "SHARDS_POLICY",
    "ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a",
)
SHARDS_NAME_HEX: str = os.environ.get("SHARDS_NAME_HEX", "0014df10536861726473")
SHARDS_UNIT: str = SHARDS_POLICY + SHARDS_NAME_HEX
SHARDS_DECIMALS: int = 6

# ---------------------------------------------------------------------------
# FLUX parameters
# ---------------------------------------------------------------------------

FLUX_TICKER: str = "FLUX"
FLUX_DECIMALS: int = 6
FLUX_MAX_SUPPLY_DISPLAY: int = 1_000_000_000  # 1 billion
FLUX_MAX_SUPPLY_BASE: int = FLUX_MAX_SUPPLY_DISPLAY * (10 ** FLUX_DECIMALS)  # 1e15

# ---------------------------------------------------------------------------
# API base URLs (network-aware)
# ---------------------------------------------------------------------------

_BLOCKFROST_BASE_URLS = {
    "mainnet": "https://cardano-mainnet.blockfrost.io/api/v0",
    "preprod": "https://cardano-preprod.blockfrost.io/api/v0",
    "preview": "https://cardano-preview.blockfrost.io/api/v0",
}
BLOCKFROST_BASE_URL: str = _BLOCKFROST_BASE_URLS[NETWORK]
TAPTOOLS_BASE_URL: str = "https://openapi.taptools.io/api/v1"

# ---------------------------------------------------------------------------
# Default pipeline parameters
# ---------------------------------------------------------------------------

DEFAULT_TWAP_WINDOW_HOURS: int = 168  # 7 days
DEFAULT_TWAP_CANDLE_INTERVAL: str = "1h"
DEFAULT_MIN_TVL_ADA: int = 10_000
DEFAULT_TOP_POOLS: int = 3
DEFAULT_COMBINE_MODE: str = "median"
DEFAULT_QUOTE_CURRENCY: str = "USD"


@dataclass(frozen=True)
class TokenInfo:
    """Immutable descriptor for a legacy token."""

    name: str
    policy_id: str
    asset_name_hex: str
    decimals: int

    @property
    def unit(self) -> str:
        return self.policy_id + self.asset_name_hex


AGENT = TokenInfo(
    name="AGENT",
    policy_id=AGENT_POLICY,
    asset_name_hex=AGENT_NAME_HEX,
    decimals=AGENT_DECIMALS,
)

SHARDS = TokenInfo(
    name="SHARDS",
    policy_id=SHARDS_POLICY,
    asset_name_hex=SHARDS_NAME_HEX,
    decimals=SHARDS_DECIMALS,
)

LEGACY_TOKENS: list[TokenInfo] = [AGENT, SHARDS]
