"""Tests for tools/process_surrender.py — pure-logic functions only.

Covers: compute_redemption, load_rate_table, load_script_from_blueprint.
Chain-dependent functions (find_pool_utxos, build_surrender_tx, etc.) are
excluded as they require a live BlockfrostClient / chain context.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.process_surrender import (
    compute_redemption,
    load_rate_table,
    load_script_from_blueprint,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

AGENT_RATE = 500_000_000_000  # 0.5 cMATRA per 1 AGENT base unit
FLUX_PASS_RATE = 2_000_000_000_000_000  # 2000 cMATRA per NFT


def _make_rate_table(
    *,
    agent_rate: int = AGENT_RATE,
    flux_pass_rate: int = FLUX_PASS_RATE,
) -> dict:
    return {
        "tokens": {
            "AGENT": {
                "rate_base_per_unit": agent_rate,
                "is_nft": False,
                "policy_id": "aaa",
                "asset_name_hex": "bbb",
            },
            "FLUX_PASS": {
                "rate_base_per_unit": flux_pass_rate,
                "is_nft": True,
                "policy_id": "ccc",
                "asset_name_hex": "ddd",
            },
        },
        "public_pool_base": 722_500_000_000_000,
    }


# ---------------------------------------------------------------------------
# TestComputeRedemption
# ---------------------------------------------------------------------------


class TestComputeRedemption:
    """Tests for compute_redemption()."""

    def test_basic_fungible(self):
        """100 AGENT base units * rate = expected cMATRA."""
        rt = _make_rate_table()
        result = compute_redemption(rt, "AGENT", 100)
        assert result == 100 * AGENT_RATE

    def test_nft_single(self):
        """1 FLUX_PASS NFT * rate = expected cMATRA."""
        rt = _make_rate_table()
        result = compute_redemption(rt, "FLUX_PASS", 1)
        assert result == 1 * FLUX_PASS_RATE

    def test_nft_multiple(self):
        """3 NFTs surrendered at once."""
        rt = _make_rate_table()
        result = compute_redemption(rt, "FLUX_PASS", 3)
        assert result == 3 * FLUX_PASS_RATE

    def test_large_fungible_quantity(self):
        """Large quantity should not overflow (Python ints are unbounded)."""
        rt = _make_rate_table()
        qty = 10**18
        result = compute_redemption(rt, "AGENT", qty)
        assert result == qty * AGENT_RATE

    def test_unknown_asset_raises_key_error(self):
        """Asset not in the rate table must raise KeyError."""
        rt = _make_rate_table()
        with pytest.raises(KeyError, match="UNKNOWN"):
            compute_redemption(rt, "UNKNOWN", 100)

    def test_zero_quantity_raises_value_error(self):
        """Zero quantity must raise ValueError."""
        rt = _make_rate_table()
        with pytest.raises(ValueError, match="positive"):
            compute_redemption(rt, "AGENT", 0)

    def test_negative_quantity_raises_value_error(self):
        """Negative quantity must raise ValueError."""
        rt = _make_rate_table()
        with pytest.raises(ValueError, match="positive"):
            compute_redemption(rt, "AGENT", -5)

    def test_zero_rate_raises_value_error(self):
        """A rate of zero in the table must raise ValueError."""
        rt = _make_rate_table(agent_rate=0)
        with pytest.raises(ValueError, match="zero or negative"):
            compute_redemption(rt, "AGENT", 100)

    def test_negative_rate_raises_value_error(self):
        """A negative rate in the table must raise ValueError."""
        rt = _make_rate_table(agent_rate=-1)
        with pytest.raises(ValueError, match="zero or negative"):
            compute_redemption(rt, "AGENT", 100)

    def test_empty_tokens_dict_raises_key_error(self):
        """Rate table with empty tokens dict raises KeyError for any asset."""
        rt = {"tokens": {}}
        with pytest.raises(KeyError):
            compute_redemption(rt, "AGENT", 1)

    def test_missing_tokens_key_raises_key_error(self):
        """Rate table without 'tokens' key at all: .get returns {}, KeyError."""
        rt = {"other_key": "value"}
        with pytest.raises(KeyError):
            compute_redemption(rt, "AGENT", 1)


# ---------------------------------------------------------------------------
# TestLoadRateTable
# ---------------------------------------------------------------------------


class TestLoadRateTable:
    """Tests for load_rate_table()."""

    def test_valid_file(self, tmp_path: Path):
        """A well-formed rate table JSON loads successfully."""
        rt_data = _make_rate_table()
        path = tmp_path / "rate_table.json"
        path.write_text(json.dumps(rt_data))

        result = load_rate_table(path)

        assert "tokens" in result
        assert "AGENT" in result["tokens"]
        assert result["tokens"]["AGENT"]["rate_base_per_unit"] == AGENT_RATE

    def test_missing_file_raises_file_not_found(self, tmp_path: Path):
        """Non-existent file must raise FileNotFoundError."""
        path = tmp_path / "does_not_exist.json"
        with pytest.raises(FileNotFoundError, match="not found"):
            load_rate_table(path)

    def test_no_tokens_key_raises_value_error(self, tmp_path: Path):
        """JSON without 'tokens' key must raise ValueError."""
        path = tmp_path / "bad_table.json"
        path.write_text(json.dumps({"public_pool_base": 123}))
        with pytest.raises(ValueError, match="tokens"):
            load_rate_table(path)

    def test_invalid_json_raises(self, tmp_path: Path):
        """Malformed JSON must raise json.JSONDecodeError."""
        path = tmp_path / "garbage.json"
        path.write_text("{not valid json!!!")
        with pytest.raises(json.JSONDecodeError):
            load_rate_table(path)

    def test_preserves_extra_keys(self, tmp_path: Path):
        """Extra top-level keys (e.g. public_pool_base) are preserved."""
        rt_data = _make_rate_table()
        path = tmp_path / "rate_table.json"
        path.write_text(json.dumps(rt_data))

        result = load_rate_table(path)
        assert result["public_pool_base"] == 722_500_000_000_000


# ---------------------------------------------------------------------------
# TestLoadBlueprint
# ---------------------------------------------------------------------------


class TestLoadBlueprint:
    """Tests for load_script_from_blueprint()."""

    def test_finds_surrender_validator(self, tmp_path: Path):
        """Selects the validator whose title contains 'surrender'."""
        blueprint = {
            "validators": [
                {
                    "title": "claim_validator.claim",
                    "compiledCode": "aabbcc",
                },
                {
                    "title": "surrender_pool.surrender",
                    "compiledCode": "ddeeff",
                },
            ]
        }
        path = tmp_path / "plutus.json"
        path.write_text(json.dumps(blueprint))

        result = load_script_from_blueprint(str(path))
        assert result == "ddeeff"

    def test_finds_pool_validator(self, tmp_path: Path):
        """Selects the validator whose title contains 'pool'."""
        blueprint = {
            "validators": [
                {
                    "title": "other_validator.mint",
                    "compiledCode": "111111",
                },
                {
                    "title": "liquidity_pool.spend",
                    "compiledCode": "222222",
                },
            ]
        }
        path = tmp_path / "plutus.json"
        path.write_text(json.dumps(blueprint))

        result = load_script_from_blueprint(str(path))
        assert result == "222222"

    def test_finds_spend_validator(self, tmp_path: Path):
        """Selects the validator whose title contains 'spend' as third keyword."""
        blueprint = {
            "validators": [
                {
                    "title": "my_spend_validator",
                    "compiledCode": "333333",
                },
            ]
        }
        path = tmp_path / "plutus.json"
        path.write_text(json.dumps(blueprint))

        result = load_script_from_blueprint(str(path))
        assert result == "333333"

    def test_fallback_to_first_validator(self, tmp_path: Path):
        """When no title matches keywords, falls back to first validator."""
        blueprint = {
            "validators": [
                {
                    "title": "my_custom_thing",
                    "compiledCode": "aabb11",
                },
                {
                    "title": "another_custom_thing",
                    "compiledCode": "ccdd22",
                },
            ]
        }
        path = tmp_path / "plutus.json"
        path.write_text(json.dumps(blueprint))

        result = load_script_from_blueprint(str(path))
        assert result == "aabb11"

    def test_no_validators_raises_value_error(self, tmp_path: Path):
        """Blueprint with empty validators list must raise ValueError."""
        blueprint = {"validators": []}
        path = tmp_path / "plutus.json"
        path.write_text(json.dumps(blueprint))

        with pytest.raises(ValueError, match="No validators"):
            load_script_from_blueprint(str(path))

    def test_keyword_priority_surrender_over_pool(self, tmp_path: Path):
        """'surrender' keyword is checked before 'pool'."""
        blueprint = {
            "validators": [
                {
                    "title": "token_pool.manage",
                    "compiledCode": "pool_code",
                },
                {
                    "title": "asset_surrender.process",
                    "compiledCode": "surrender_code",
                },
            ]
        }
        path = tmp_path / "plutus.json"
        path.write_text(json.dumps(blueprint))

        result = load_script_from_blueprint(str(path))
        assert result == "surrender_code"

    def test_skips_validator_with_empty_compiled_code(self, tmp_path: Path):
        """A matching title with empty compiledCode is skipped."""
        blueprint = {
            "validators": [
                {
                    "title": "surrender_validator",
                    "compiledCode": "",
                },
                {
                    "title": "fallback_thing",
                    "compiledCode": "fallback_hex",
                },
            ]
        }
        path = tmp_path / "plutus.json"
        path.write_text(json.dumps(blueprint))

        # 'surrender' matches first validator but compiledCode is empty,
        # then 'pool'/'spend' don't match, so fallback to first with code.
        # But first validator has empty code too, so it checks second...
        # Actually fallback logic: first validator[0].compiledCode == "" ->
        # raises ValueError. Let's verify.
        # Looking at code: fallback checks validators[0].compiledCode,
        # which is empty string (falsy), so it falls through to raise.
        # But validators[1] exists... the fallback only checks validators[0].
        with pytest.raises(ValueError, match="No validators"):
            load_script_from_blueprint(str(path))
