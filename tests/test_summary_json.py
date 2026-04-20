"""Tests for allocations_cmatra_summary.json — critical invariants for v5.1.

After the regeneration fix, the summary MUST report 722.5M across both
public_pool_base_units and total_bucket_sum (not the 850M v3/v4 value).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SUMMARY = _PROJECT_ROOT / "audit_pack" / "2026-04-19" / "allocations_cmatra_summary.json"

PUBLIC_POOL_BASE = 722_500_000_000_000


@pytest.fixture(scope="module")
def summary() -> dict:
    assert _SUMMARY.exists()
    with open(_SUMMARY) as f:
        return json.load(f)


class TestBucketTotals:
    def test_total_bucket_sum_equals_public_pool(self, summary: dict):
        """CRITICAL INVARIANT: sum of all per-token buckets == 722.5M."""
        assert (
            summary["totals"]["total_bucket_sum"]
            == summary["public_pool_base_units"]
            == PUBLIC_POOL_BASE
        ), (
            "The three values must be equal and == 722.5M.\n"
            f"  total_bucket_sum         = {summary['totals']['total_bucket_sum']:,}\n"
            f"  public_pool_base_units   = {summary['public_pool_base_units']:,}\n"
            f"  expected (v5.1)          = {PUBLIC_POOL_BASE:,}"
        )

    def test_per_token_buckets_sum_to_total(self, summary: dict):
        """Per-token bucket_base_units across ALL tokens must equal the total."""
        per_token = summary["per_token"]
        total = sum(t["bucket_base_units"] for t in per_token.values())
        assert total == PUBLIC_POOL_BASE, (
            f"Per-token buckets sum to {total:,} but pool is "
            f"{PUBLIC_POOL_BASE:,}"
        )

    def test_validator_reserve_base_is_277_5m(self, summary: dict):
        assert summary["validator_reserve_base_units"] == 277_500_000_000_000

    def test_per_token_flux_units_consistency(self, summary: dict):
        """For each token: distributed + team_reserve + nft_reserve + dust
        <= bucket_base_units.  Strict equality isn't required on NFTs since
        some NFTs are unresolvable, but the invariant caps the bucket."""
        for name, t in summary["per_token"].items():
            bucket = t["bucket_base_units"]
            distributed = t.get("distributed_base_units", 0)
            team = t.get("team_reserve_base_units", 0)
            nft_res = t.get("nft_reserve_base_units", 0)
            dust = t.get("dust_base_units", 0)
            total = distributed + team + nft_res + dust
            assert total <= bucket, (
                f"{name}: distributed+team+nft_reserve+dust = {total:,} "
                f"exceeds bucket {bucket:,}"
            )
