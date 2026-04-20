"""Tests for the rational rate representation (Fix 3).

v5.1 stores rate_numerator + rate_denominator (in addition to the legacy
rate_base_per_unit) so that SHARDS allocation is exact rather than
truncated to ~3% precision.

Redemption math: flux = (balance * rate_numerator) // rate_denominator
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.flux_merge_valuation_int import build_rate_table
from tools.process_surrender import compute_redemption

_AUDIT = Path(__file__).resolve().parent.parent / "audit_pack" / "2026-04-19"
_RATE_TABLE = _AUDIT / "rate_table_cmatra.json"


@pytest.fixture(scope="module")
def rate_table_file() -> dict:
    assert _RATE_TABLE.exists()
    with open(_RATE_TABLE) as f:
        return json.load(f)


class TestRateTableRationalFields:
    """Every token entry must have rate_numerator + rate_denominator."""

    def test_agent_has_rational_fields(self, rate_table_file: dict):
        entry = rate_table_file["tokens"]["AGENT"]
        assert "rate_numerator" in entry
        assert "rate_denominator" in entry
        assert entry["rate_numerator"] > 0
        assert entry["rate_denominator"] > 0

    def test_shards_rational_is_exact(self, rate_table_file: dict):
        """SHARDS: num/den must equal bucket/redeemable_supply exactly."""
        entry = rate_table_file["tokens"]["SHARDS"]
        assert entry["rate_numerator"] == entry["bucket_base"]
        assert entry["rate_denominator"] == entry["redeemable_supply_base"]

    def test_all_tokens_have_rational(self, rate_table_file: dict):
        for name, entry in rate_table_file["tokens"].items():
            assert "rate_numerator" in entry, f"{name} missing rate_numerator"
            assert "rate_denominator" in entry, f"{name} missing rate_denominator"

    def test_rate_base_per_unit_equals_floor(self, rate_table_file: dict):
        """rate_base_per_unit should equal floor(num/den) for backwards compat."""
        for name, entry in rate_table_file["tokens"].items():
            expected = entry["rate_numerator"] // entry["rate_denominator"]
            assert entry["rate_base_per_unit"] == expected, (
                f"{name}: rate_base_per_unit={entry['rate_base_per_unit']} "
                f"!= floor({entry['rate_numerator']}/{entry['rate_denominator']})"
                f" = {expected}"
            )


class TestBuildRateTableRational:
    """build_rate_table() must emit rational fields."""

    def test_rational_fields_in_output(self):
        report = {
            "public_pool_base_units": 722_500_000_000_000,
            "validator_reserve_base_units": 277_500_000_000_000,
            "tokens": {
                "SHARDS": {
                    "decimals": 6,
                    "supply_base_units": 3_088_534_450_001,
                    "flux_bucket_base_units": 76_929_202_239_379,
                },
            },
        }
        rt = build_rate_table(
            report,
            team_waiver_supplies={"SHARDS": 446_969_700_000},
        )
        entry = rt["tokens"]["SHARDS"]
        # Rational must be exact: bucket / (supply - waiver)
        assert entry["rate_numerator"] == 76_929_202_239_379
        assert entry["rate_denominator"] == 3_088_534_450_001 - 446_969_700_000


class TestComputeRedemptionRational:
    """compute_redemption() must honour the rational fields when present."""

    def test_rational_preferred_over_integer_rate(self):
        """When rate_numerator+denominator exist, those are used
        (not the integer rate_base_per_unit, which has precision loss)."""
        rt = {
            "tokens": {
                "SHARDS": {
                    # Integer rate: 29 (floor of 29.12)
                    "rate_base_per_unit": 29,
                    # Rational: exact
                    "rate_numerator": 76_929_202_239_379,
                    "rate_denominator": 2_641_564_750_001,
                    "is_nft": False,
                },
            },
        }
        # 100_000_000 SHARDS * 29 = 2_900_000_000 (integer, lossy)
        # vs rational: 100_000_000 * 76929202239379 / 2641564750001
        #           = 7692920223937900000000 / 2641564750001
        #           = 2,912,258,813 (exact floor)
        qty = 100_000_000
        result = compute_redemption(rt, "SHARDS", qty)

        expected_rational = (qty * 76_929_202_239_379) // 2_641_564_750_001
        expected_integer = qty * 29

        assert result == expected_rational
        assert result != expected_integer
        assert result > expected_integer  # rational is MORE accurate (more cMATRA)

    def test_falls_back_to_integer_rate_when_rational_missing(self):
        """If only rate_base_per_unit present (no rational), use it."""
        rt = {
            "tokens": {
                "AGENT": {
                    "rate_base_per_unit": 462_890,
                    "is_nft": False,
                },
            },
        }
        result = compute_redemption(rt, "AGENT", 1000)
        assert result == 1000 * 462_890

    def test_rational_matches_integer_when_evenly_divisible(self):
        """For AGENT (no team waiver), rational and integer rates agree
        when the bucket is evenly divisible by the supply."""
        # Contrived case: bucket/supply = integer
        rt = {
            "tokens": {
                "TOKEN": {
                    "rate_base_per_unit": 500,
                    "rate_numerator": 5_000_000,
                    "rate_denominator": 10_000,
                    "is_nft": False,
                },
            },
        }
        # 500 * 100 = 50_000
        # (100 * 5_000_000) // 10_000 = 500_000_000 // 10_000 = 50_000
        assert compute_redemption(rt, "TOKEN", 100) == 50_000
