"""Tests for the regenerated v5.1 allocations_cmatra.csv + summary.

These tests assert that the audit_pack/2026-04-19/ allocation artifacts were
actually regenerated against v5.1 constants, not shortcut-copied from the
v3/v4 (2026-03-11) snapshot.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_AUDIT_DIR = _PROJECT_ROOT / "audit_pack" / "2026-04-19"
_ALLOC_CSV = _AUDIT_DIR / "allocations_cmatra.csv"
_ALLOC_SUMMARY = _AUDIT_DIR / "allocations_cmatra_summary.json"
_RATE_TABLE = _AUDIT_DIR / "rate_table_cmatra.json"
_RESERVE_LEDGER = _AUDIT_DIR / "reserve_ledger.json"
_MERGE_VALUATION = _AUDIT_DIR / "merge_valuation_cmatra.json"

PUBLIC_POOL_BASE = 722_500_000_000_000


# ---------------------------------------------------------------------------
# Fixture: load everything once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def alloc_rows() -> list[dict]:
    assert _ALLOC_CSV.exists(), f"{_ALLOC_CSV} missing — regeneration required"
    with open(_ALLOC_CSV) as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def alloc_summary() -> dict:
    assert _ALLOC_SUMMARY.exists(), f"{_ALLOC_SUMMARY} missing"
    with open(_ALLOC_SUMMARY) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def rate_table() -> dict:
    assert _RATE_TABLE.exists(), f"{_RATE_TABLE} missing"
    with open(_RATE_TABLE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def reserve_ledger() -> dict:
    assert _RESERVE_LEDGER.exists(), f"{_RESERVE_LEDGER} missing"
    with open(_RESERVE_LEDGER) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def merge_valuation() -> dict:
    assert _MERGE_VALUATION.exists(), f"{_MERGE_VALUATION} missing"
    with open(_MERGE_VALUATION) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAllocationsCsvSum:
    """The regenerated CSV must allocate at v5.1 rates, not v3/v4 rates."""

    def test_row_count_matches_snapshot(self, alloc_rows: list[dict]):
        """The snapshot has exactly 6627 eligible claimants."""
        assert len(alloc_rows) == 6627

    def test_total_flux_units_bounded_by_pool(self, alloc_rows: list[dict]):
        """Per-row flux_total_units must sum to <= 722.5M pool base units.

        The old (v3/v4) CSV summed to 819.6M — a 13.5% over-allocation that
        would drain the pool if processed.  v5.1 rates yield a smaller sum
        because script-held balances are no longer redistributed (dust goes
        to treasury sweep instead).
        """
        total = sum(int(r["flux_total_units"]) for r in alloc_rows)
        assert total <= PUBLIC_POOL_BASE, (
            f"Allocations sum to {total:,} which exceeds the "
            f"722.5M public pool ({PUBLIC_POOL_BASE:,}). "
            "This is the over-allocation bug — regenerate with v5.1 rates."
        )

    def test_total_plus_reserves_plus_dust_equals_pool(
        self,
        alloc_rows: list[dict],
        alloc_summary: dict,
    ):
        """Full conservation: holder allocations + team carve + NFT reserve
        + dust swept = 722.5M cMATRA (exact).
        """
        total_holders = sum(int(r["flux_total_units"]) for r in alloc_rows)
        reserve = alloc_summary["reserve"]
        team_reserve = reserve["total_team_reserve_base"]
        nft_reserve = reserve["total_nft_reserve_base"]
        dust_swept = alloc_summary["totals"].get("total_dust_swept_base", 0)

        total = total_holders + team_reserve + nft_reserve + dust_swept
        assert total == PUBLIC_POOL_BASE, (
            f"Conservation failure:\n"
            f"  holders       = {total_holders:,}\n"
            f"  team_reserve  = {team_reserve:,}\n"
            f"  nft_reserve   = {nft_reserve:,}\n"
            f"  dust_swept    = {dust_swept:,}\n"
            f"  total         = {total:,}\n"
            f"  expected pool = {PUBLIC_POOL_BASE:,}"
        )

    def test_agent_sample_row(self, alloc_rows: list[dict], rate_table: dict):
        """Spot-check: an AGENT holder with balance N should get N*rate in
        flux_units (for rows with only AGENT)."""
        agent_entry = rate_table["tokens"]["AGENT"]
        if "rate_numerator" in agent_entry:
            num = agent_entry["rate_numerator"]
            den = agent_entry["rate_denominator"]

            def expected(bal: int) -> int:
                return (bal * num) // den
        else:
            rate = agent_entry["rate_base_per_unit"]

            def expected(bal: int) -> int:
                return bal * rate

        # Find a row with only AGENT balance (non-zero AGENT, zero everything else)
        for r in alloc_rows:
            agent_bal = int(r["agent_balance_base"])
            others = sum(
                int(r[k]) for k in (
                    "shards_balance_base", "flux_pass_balance_base",
                    "se_brawlers_balance_base", "brawl_pass_etd_balance_base",
                    "t1_adam_pass_balance_base", "t2_adam_pass_balance_base",
                )
            )
            if agent_bal > 0 and others == 0:
                expected_flux = expected(agent_bal)
                actual_flux = int(r["agent_flux_units"])
                assert actual_flux == expected_flux, (
                    f"AGENT holder PKH={r['payment_key_hash_hex'][:16]}... "
                    f"balance={agent_bal}: expected {expected_flux}, "
                    f"got {actual_flux}"
                )
                return
        pytest.skip("No pure-AGENT row found for spot check")


class TestAllocationSummaryJson:
    """The summary JSON must reflect v5.1 tokenomics, not old v3/v4 values."""

    def test_public_pool_base_units_is_722_5m(self, alloc_summary: dict):
        assert alloc_summary["public_pool_base_units"] == PUBLIC_POOL_BASE

    def test_total_bucket_sum_is_722_5m(self, alloc_summary: dict):
        """CRITICAL: must not say 850M (old v3/v4 total)."""
        total = alloc_summary["totals"]["total_bucket_sum"]
        assert total == PUBLIC_POOL_BASE, (
            f"total_bucket_sum = {total:,} but v5.1 pool is "
            f"{PUBLIC_POOL_BASE:,}. This is the shortcut-copy bug."
        )

    def test_total_bucket_sum_equals_public_pool(self, alloc_summary: dict):
        """Two keys MUST equal: total_bucket_sum == public_pool_base_units."""
        assert (
            alloc_summary["totals"]["total_bucket_sum"]
            == alloc_summary["public_pool_base_units"]
        )

    def test_tokenomics_version(self, alloc_summary: dict):
        assert alloc_summary["tokenomics_version"] == "v5.1"

    def test_generated_at_is_recent(self, alloc_summary: dict):
        """generated_at should be 2026-04-19 (the v5.1 restructure date),
        not the stale 2026-03-12 timestamp."""
        assert alloc_summary["generated_at"].startswith("2026-04-19"), (
            f"generated_at = {alloc_summary['generated_at']}, expected 2026-04-19"
        )

    def test_sum_equals_bucket_total_flag(self, alloc_summary: dict):
        assert alloc_summary["totals"]["sum_equals_bucket_total"] is True
