"""Tests for tools.config — environment loading and constants."""

from tools.config import (
    AGENT,
    AGENT_DECIMALS,
    AGENT_UNIT,
    FLUX_DECIMALS,
    FLUX_MAX_SUPPLY_BASE,
    FLUX_MAX_SUPPLY_DISPLAY,
    LEGACY_TOKENS,
    SHARDS,
    SHARDS_DECIMALS,
    SHARDS_UNIT,
    TokenInfo,
)


class TestTokenInfo:
    def test_agent_unit_concatenation(self):
        assert AGENT.unit == AGENT_UNIT
        assert AGENT.unit == AGENT.policy_id + AGENT.asset_name_hex

    def test_shards_unit_concatenation(self):
        assert SHARDS.unit == SHARDS_UNIT
        assert SHARDS.unit == SHARDS.policy_id + SHARDS.asset_name_hex

    def test_agent_decimals(self):
        assert AGENT.decimals == 0

    def test_shards_decimals(self):
        assert SHARDS.decimals == 6

    def test_legacy_tokens_list(self):
        assert len(LEGACY_TOKENS) == 2
        assert AGENT in LEGACY_TOKENS
        assert SHARDS in LEGACY_TOKENS

    def test_token_info_frozen(self):
        import pytest
        with pytest.raises(AttributeError):
            AGENT.name = "changed"  # type: ignore


class TestFluxConstants:
    def test_max_supply_base(self):
        assert FLUX_MAX_SUPPLY_BASE == 1_000_000_000_000_000

    def test_max_supply_display(self):
        assert FLUX_MAX_SUPPLY_DISPLAY == 1_000_000_000

    def test_base_from_display(self):
        assert FLUX_MAX_SUPPLY_BASE == FLUX_MAX_SUPPLY_DISPLAY * (10 ** FLUX_DECIMALS)

    def test_decimals(self):
        assert FLUX_DECIMALS == 6
