"""Tests for tools.snapshot_allocate_flux — holder filtering, integer allocation, dust."""

import pytest

from tools.snapshot_allocate_flux import (
    allocate_flux,
    filter_holders,
    write_allocations_csv,
    CSV_COLUMNS,
)
from tools.config import AGENT, FLUX_DECIMALS, FLUX_MAX_SUPPLY_BASE, SHARDS
from tests.conftest import SAMPLE_PKH_1


class TestFilterHolders:
    def test_no_filters(self):
        holders = [
            {"address": "addr1_a", "quantity": 100},
            {"address": "addr1_b", "quantity": 200},
        ]
        result = filter_holders(holders, exclude_script=False)
        assert len(result) == 2

    def test_exclude_specific_addresses(self):
        holders = [
            {"address": "addr1_a", "quantity": 100},
            {"address": "addr1_b", "quantity": 200},
            {"address": "addr1_c", "quantity": 300},
        ]
        result = filter_holders(
            holders,
            exclude_script=False,
            exclude_addresses={"addr1_b"},
        )
        assert len(result) == 2
        assert all(h["address"] != "addr1_b" for h in result)

    def test_exclude_multiple_addresses(self):
        holders = [
            {"address": "addr1_a", "quantity": 100},
            {"address": "addr1_b", "quantity": 200},
            {"address": "addr1_c", "quantity": 300},
        ]
        result = filter_holders(
            holders,
            exclude_script=False,
            exclude_addresses={"addr1_a", "addr1_c"},
        )
        assert len(result) == 1
        assert result[0]["address"] == "addr1_b"


class TestAllocateFlux:
    def test_basic_proportional_allocation(self):
        holders = [
            {"address": "a", "quantity": 600},
            {"address": "b", "quantity": 400},
        ]
        result = allocate_flux(AGENT, holders, bucket_base_units=1_000_000)

        # 600/1000 * 1M = 600K
        assert result[0]["flux_units"] == 600_000
        # 400/1000 * 1M = 400K
        assert result[1]["flux_units"] == 400_000

    def test_allocation_sums_correctly(self):
        holders = [
            {"address": "a", "quantity": 333},
            {"address": "b", "quantity": 333},
            {"address": "c", "quantity": 334},
        ]
        bucket = 1_000_000
        result = allocate_flux(AGENT, holders, bucket_base_units=bucket)
        total = sum(r["flux_units"] for r in result)
        # Floor rounding means total <= bucket
        assert total <= bucket

    def test_floor_rounding_produces_dust(self):
        """When balances don't divide evenly, floor creates dust."""
        holders = [
            {"address": "a", "quantity": 1},
            {"address": "b", "quantity": 1},
            {"address": "c", "quantity": 1},
        ]
        bucket = 10  # 10 / 3 = 3 per holder, 1 dust
        result = allocate_flux(AGENT, holders, bucket_base_units=bucket)
        total = sum(r["flux_units"] for r in result)
        assert total == 9  # 3 * 3 = 9, dust = 1

    def test_single_holder_gets_full_bucket(self):
        holders = [{"address": "a", "quantity": 100}]
        bucket = 999_999
        result = allocate_flux(AGENT, holders, bucket_base_units=bucket)
        assert result[0]["flux_units"] == bucket

    def test_empty_holders(self):
        result = allocate_flux(AGENT, [], bucket_base_units=1_000_000)
        assert result == []

    def test_zero_bucket(self):
        holders = [{"address": "a", "quantity": 100}]
        result = allocate_flux(AGENT, holders, bucket_base_units=0)
        assert result[0]["flux_units"] == 0

    def test_report_denominator_mode(self):
        """With report mode, denom is total supply, not eligible sum."""
        holders = [
            {"address": "a", "quantity": 500},
        ]
        # Eligible is 500 but total supply is 1000 → holder gets half the bucket
        result = allocate_flux(
            AGENT,
            holders,
            bucket_base_units=1_000_000,
            denominator_mode="report",
            total_supply_base=1000,
        )
        assert result[0]["flux_units"] == 500_000

    def test_eligible_denominator_distributes_full_bucket(self):
        holders = [
            {"address": "a", "quantity": 500},
        ]
        result = allocate_flux(
            AGENT,
            holders,
            bucket_base_units=1_000_000,
            denominator_mode="eligible",
        )
        assert result[0]["flux_units"] == 1_000_000

    def test_large_scale_allocation(self):
        """Test with realistic FLUX numbers."""
        bucket = 500_000_000_000_000  # 500T base units
        total_supply = 50_000_000  # 50M AGENT tokens
        holders = [
            {"address": f"addr_{i}", "quantity": total_supply // 100}
            for i in range(100)
        ]
        result = allocate_flux(
            AGENT, holders, bucket_base_units=bucket,
            denominator_mode="eligible",
        )
        total_alloc = sum(r["flux_units"] for r in result)
        # Should distribute nearly all, with minimal dust
        assert total_alloc <= bucket
        assert total_alloc >= bucket - 100  # dust is small

    def test_display_values_correct(self):
        holders = [{"address": "a", "quantity": 1_000_000}]
        result = allocate_flux(SHARDS, holders, bucket_base_units=6_000_000)
        assert result[0]["token_balance_display"] == 1.0  # 1M / 10^6
        assert result[0]["flux_display"] == 6.0  # 6M / 10^6


class TestAllocationsCSV:
    def test_csv_roundtrip(self, tmp_path):
        rows = [
            {
                "payment_key_hash_hex": SAMPLE_PKH_1,
                "addresses": ["addr1", "addr2"],
                "agent_balance_base": 100,
                "agent_flux_units": 50,
                "shards_balance_base": 200,
                "shards_flux_units": 100,
                "flux_total_units": 150,
                "flux_total_display": 0.00015,
            },
        ]
        csv_path = tmp_path / "alloc.csv"
        write_allocations_csv(rows, csv_path)
        assert csv_path.exists()

        import csv
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            read_rows = list(reader)
        assert len(read_rows) == 1
        assert read_rows[0]["payment_key_hash_hex"] == SAMPLE_PKH_1
        assert read_rows[0]["flux_total_units"] == "150"
