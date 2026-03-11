"""Tests for tools.funding_calculator — funding estimate computation."""

import math
from pathlib import Path

import pytest

from tools.funding_calculator import compute_funding_report, compute_pool_funding_report


def _make_allocations(n: int, flux_per: int = 1_000_000) -> list[dict]:
    """Generate n synthetic allocations."""
    return [
        {
            "payment_key_hash_hex": f"{i:056x}",
            "flux_units": flux_per,
            "addresses": f"addr_{i}",
        }
        for i in range(1, n + 1)
    ]


class TestComputeFundingReport:
    def test_basic_report_structure(self):
        allocs = _make_allocations(10)
        report = compute_funding_report(allocs, batch_size=5)

        assert report["report_type"] == "funding_calculator"
        assert report["num_claims"] == 10
        assert report["num_batches"] == 2
        assert "locked_ada" in report
        assert "fees" in report
        assert "safety_margin" in report
        assert "grand_total" in report

    def test_num_batches_calculation(self):
        allocs = _make_allocations(100)
        report = compute_funding_report(allocs, batch_size=40)
        assert report["num_batches"] == math.ceil(100 / 40)

    def test_single_claim(self):
        allocs = _make_allocations(1)
        report = compute_funding_report(allocs, batch_size=40)
        assert report["num_claims"] == 1
        assert report["num_batches"] == 1

    def test_empty_allocations(self):
        report = compute_funding_report([], batch_size=40)
        assert report["num_claims"] == 0
        assert report["num_batches"] == 0
        assert report["grand_total"]["lovelace"] == 0

    def test_total_flux_summed_correctly(self):
        allocs = _make_allocations(5, flux_per=2_000_000)
        report = compute_funding_report(allocs)
        assert report["total_flux_units"] == 5 * 2_000_000

    def test_min_ada_positive(self):
        allocs = _make_allocations(3)
        report = compute_funding_report(allocs)
        assert report["locked_ada"]["total_min_ada_lovelace"] > 0

    def test_safety_margin_applied(self):
        allocs = _make_allocations(10)
        report_10 = compute_funding_report(allocs, safety_margin_pct=10.0)
        report_0 = compute_funding_report(allocs, safety_margin_pct=0.0)

        assert report_10["grand_total"]["lovelace"] > report_0["grand_total"]["lovelace"]
        assert report_0["safety_margin"]["margin_lovelace"] == 0

    def test_fee_per_batch_scales(self):
        allocs = _make_allocations(80)
        report_low = compute_funding_report(allocs, batch_size=40, fee_per_batch_lovelace=100_000)
        report_high = compute_funding_report(allocs, batch_size=40, fee_per_batch_lovelace=1_000_000)

        assert report_low["fees"]["total_fees_lovelace"] == 2 * 100_000
        assert report_high["fees"]["total_fees_lovelace"] == 2 * 1_000_000

    def test_grand_total_formula(self):
        allocs = _make_allocations(10)
        report = compute_funding_report(
            allocs,
            batch_size=10,
            fee_per_batch_lovelace=500_000,
            safety_margin_pct=10.0,
        )
        locked = report["locked_ada"]["total_min_ada_lovelace"]
        fees = report["fees"]["total_fees_lovelace"]
        subtotal = locked + fees
        margin = int(subtotal * 10.0 / 100)
        assert report["grand_total"]["lovelace"] == subtotal + margin

    def test_ada_display_conversion(self):
        allocs = _make_allocations(1)
        report = compute_funding_report(allocs)
        lovelace = report["grand_total"]["lovelace"]
        ada = report["grand_total"]["ada"]
        assert ada == lovelace / 1_000_000


class TestComputePoolFundingReport:
    """Tests for the surrender pool funding calculator (new model)."""

    def test_basic_report_structure(self):
        report = compute_pool_funding_report(
            total_cmatra_base=850_000_000_000_000,
            num_pool_utxos=10,
        )
        assert report["report_type"] == "pool_funding_calculator"
        assert report["model"] == "surrender_pool"
        assert report["num_pool_utxos"] == 10
        assert "locked_ada" in report
        assert "fees" in report
        assert "safety_margin" in report
        assert "grand_total" in report

    def test_locked_ada_scales_with_utxos(self):
        r5 = compute_pool_funding_report(total_cmatra_base=1_000_000, num_pool_utxos=5)
        r10 = compute_pool_funding_report(total_cmatra_base=1_000_000, num_pool_utxos=10)
        assert r10["locked_ada"]["total_min_ada_lovelace"] == 2 * r5["locked_ada"]["total_min_ada_lovelace"]

    def test_fees_scale_with_deploy_txs(self):
        report = compute_pool_funding_report(
            total_cmatra_base=1_000_000,
            fee_per_deploy_tx_lovelace=400_000,
            num_deploy_txs=3,
        )
        assert report["fees"]["total_fees_lovelace"] == 3 * 400_000

    def test_safety_margin_applied(self):
        r0 = compute_pool_funding_report(total_cmatra_base=1_000_000, safety_margin_pct=0.0)
        r10 = compute_pool_funding_report(total_cmatra_base=1_000_000, safety_margin_pct=10.0)
        assert r10["grand_total"]["lovelace"] > r0["grand_total"]["lovelace"]
        assert r0["safety_margin"]["margin_lovelace"] == 0

    def test_grand_total_formula(self):
        report = compute_pool_funding_report(
            total_cmatra_base=1_000_000,
            num_pool_utxos=10,
            fee_per_deploy_tx_lovelace=500_000,
            num_deploy_txs=2,
            safety_margin_pct=10.0,
        )
        locked = report["locked_ada"]["total_min_ada_lovelace"]
        fees = report["fees"]["total_fees_lovelace"]
        subtotal = locked + fees
        margin = int(subtotal * 10.0 / 100)
        assert report["grand_total"]["lovelace"] == subtotal + margin

    def test_ada_display_conversion(self):
        report = compute_pool_funding_report(total_cmatra_base=1_000_000)
        assert report["grand_total"]["ada"] == report["grand_total"]["lovelace"] / 1_000_000

    def test_per_utxo_min_ada_positive(self):
        report = compute_pool_funding_report(total_cmatra_base=1_000_000)
        assert report["per_utxo_min_ada_lovelace"] > 0
