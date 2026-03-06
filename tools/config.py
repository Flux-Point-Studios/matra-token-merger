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
# Output token parameters (cMATRA — Cardano representation of MATRA)
# ---------------------------------------------------------------------------

MERGE_TOKEN_TICKER: str = "cMATRA"
MERGE_TOKEN_DECIMALS: int = 12
MERGE_TOKEN_SUPPLY_DISPLAY: int = 1_000_000_000  # 1 billion
MERGE_TOKEN_SUPPLY_BASE: int = MERGE_TOKEN_SUPPLY_DISPLAY * (10 ** MERGE_TOKEN_DECIMALS)  # 1e21

# Legacy aliases (used by claim vault / downstream code)
FLUX_TICKER: str = MERGE_TOKEN_TICKER
FLUX_DECIMALS: int = MERGE_TOKEN_DECIMALS
FLUX_MAX_SUPPLY_DISPLAY: int = MERGE_TOKEN_SUPPLY_DISPLAY
FLUX_MAX_SUPPLY_BASE: int = MERGE_TOKEN_SUPPLY_BASE

# ---------------------------------------------------------------------------
# Admin / claim-validator parameters
# ---------------------------------------------------------------------------

ADMIN_PKH: str = os.environ.get("ADMIN_PKH", "")
CLAIM_DEADLINE_POSIX_MS: int = int(os.environ.get("CLAIM_DEADLINE_POSIX_MS", "0"))

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

_KOIOS_BASE_URLS = {
    "mainnet": "https://api.koios.rest/api/v1",
    "preprod": "https://preprod.koios.rest/api/v1",
    "preview": "https://preview.koios.rest/api/v1",
}
KOIOS_BASE_URL: str = _KOIOS_BASE_URLS[NETWORK]
KOIOS_API_KEY: str = os.environ.get("KOIOS_API_KEY", "")

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


@dataclass(frozen=True)
class NftCollectionInfo:
    """Immutable descriptor for an NFT collection participating in the merge."""

    name: str           # Short key, e.g. "FLUX_PASS"
    policy_id: str      # 56-hex-char policy ID
    display_name: str   # Human-readable name

    @property
    def decimals(self) -> int:
        return 0


FLUX_PASS = NftCollectionInfo(
    name="FLUX_PASS",
    policy_id="0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a",
    display_name="Flux Point Team Pass",
)

SE_BRAWLERS = NftCollectionInfo(
    name="SE_BRAWLERS",
    policy_id="25c75bbf105310685d51cd3adbdd50b72fdbd99be2cc3757dde7eafc",
    display_name="SE Brawlers",
)

BRAWL_PASS_ETD = NftCollectionInfo(
    name="BRAWL_PASS_ETD",
    policy_id="d3a197c4814054623432c882c60e6a81e8f3b94158033432529a02d2",
    display_name="Brawl Pass: Enter the Dragon",
)

T1_ADAM_PASS = NftCollectionInfo(
    name="T1_ADAM_PASS",
    policy_id="b46891456b77dbc77c16090fd92a37f087f9a68e953c56b00a20332f",
    display_name="T1 ADAM Launch Pass",
)

T2_ADAM_PASS = NftCollectionInfo(
    name="T2_ADAM_PASS",
    policy_id="06a64965c0ac1144a72a6ddfcb23aa9d4d7742a5b20ddd5cfb1164b9",
    display_name="T2 ADAM Launch Pass",
)

NFT_COLLECTIONS: list[NftCollectionInfo] = [
    FLUX_PASS,
    SE_BRAWLERS,
    BRAWL_PASS_ETD,
    T1_ADAM_PASS,
    T2_ADAM_PASS,
]

ALL_MERGE_ASSETS: list[TokenInfo | NftCollectionInfo] = LEGACY_TOKENS + NFT_COLLECTIONS  # type: ignore[list-item]
