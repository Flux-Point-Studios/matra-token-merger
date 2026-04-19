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
MERGE_TOKEN_DECIMALS: int = 6
MERGE_TOKEN_SUPPLY_DISPLAY: int = 1_000_000_000  # 1 billion
MERGE_TOKEN_SUPPLY_BASE: int = MERGE_TOKEN_SUPPLY_DISPLAY * (10 ** MERGE_TOKEN_DECIMALS)  # 1e15

# v5.1 Redemption model: 72.25% public pool, 27.75% Network Incentives Reserve
#
# Public Redemption Pool (722.5M) — distributed to AGENT/SHARDS/NFT holders
# Network Incentives Reserve (277.5M) splits into 5 sub-buckets:
#   * 115.0M Validator emissions
#   * 65.0M Attestor emissions
#   * 40.0M Ecosystem Treasury
#   * 30.0M Strategic / Investor (Orion Fund target)
#   * 27.5M Liquidity (5M bridge + 17.5M POL + 5M maker rebates)
#
# Dilution multiplier vs v3/v4: 722.5 / 850 = 0.85 exactly. Redemption rates
# in the off-chain rate table shrink by the same 0.85 factor, so 1 AGENT
# drops from ~0.5446 cMATRA to ~0.4629 cMATRA.
VALIDATOR_RESERVE_FRACTION: float = 0.2775
PUBLIC_POOL_FRACTION: float = 1.0 - VALIDATOR_RESERVE_FRACTION  # 0.7225
VALIDATOR_RESERVE_DISPLAY: int = 277_500_000  # 277.5M cMATRA
PUBLIC_POOL_DISPLAY: int = MERGE_TOKEN_SUPPLY_DISPLAY - VALIDATOR_RESERVE_DISPLAY  # 722.5M
VALIDATOR_RESERVE_BASE: int = VALIDATOR_RESERVE_DISPLAY * (10 ** MERGE_TOKEN_DECIMALS)
PUBLIC_POOL_BASE: int = MERGE_TOKEN_SUPPLY_BASE - VALIDATOR_RESERVE_BASE

# Network Incentives Reserve sub-buckets (v5.1)
VALIDATOR_EMISSIONS_BASE: int = 115_000_000 * (10 ** MERGE_TOKEN_DECIMALS)
ATTESTOR_EMISSIONS_BASE: int = 65_000_000 * (10 ** MERGE_TOKEN_DECIMALS)
ECOSYSTEM_TREASURY_BASE: int = 40_000_000 * (10 ** MERGE_TOKEN_DECIMALS)
STRATEGIC_BASE: int = 30_000_000 * (10 ** MERGE_TOKEN_DECIMALS)  # Orion Fund target
LIQUIDITY_BASE: int = 27_500_000 * (10 ** MERGE_TOKEN_DECIMALS)  # 5M bridge + 17.5M POL + 5M maker rebates

assert (
    VALIDATOR_EMISSIONS_BASE
    + ATTESTOR_EMISSIONS_BASE
    + ECOSYSTEM_TREASURY_BASE
    + STRATEGIC_BASE
    + LIQUIDITY_BASE
    == VALIDATOR_RESERVE_BASE
), "sub-buckets must sum to Network Incentives Reserve"

# Legacy aliases (used by downstream code)
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

# ---------------------------------------------------------------------------
# CIP-68 asset name prefixes
# ---------------------------------------------------------------------------

CIP68_USER_TOKEN_PREFIX: str = "000de140"      # label 222 — user-facing NFT
CIP68_REFERENCE_TOKEN_PREFIX: str = "000643b0"  # label 100 — on-chain metadata


def filter_nft_assets(assets: list[dict], policy_id_len: int = 56) -> list[dict]:
    """Filter NFT assets from a policy, handling CIP-68 collections.

    CIP-68 policies mint a user token (000de140 prefix) and a reference token
    (000643b0 prefix) per NFT.  Only user tokens should be counted.

    For non-CIP-68 policies, all qty=1 assets are returned.
    """
    qty1 = [a for a in assets if int(a.get("quantity", 1)) == 1]
    user_tokens = [
        a for a in qty1
        if a.get("asset", "")[policy_id_len:].startswith(CIP68_USER_TOKEN_PREFIX)
    ]
    if user_tokens:
        # CIP-68 collection: only count user tokens
        return user_tokens
    # Non-CIP-68: return all qty=1 except any stray reference tokens
    return [
        a for a in qty1
        if not a.get("asset", "")[policy_id_len:].startswith(CIP68_REFERENCE_TOKEN_PREFIX)
    ]
