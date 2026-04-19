"""Tests for reserve_ledger.json — must include the 5 Network Incentives
sub-buckets materialized (not just team treasury + NFT conditional).

v5.1 Network Incentives Reserve breakdown:
    * 115.0M Validator emissions
    *  65.0M Attestor emissions
    *  40.0M Ecosystem Treasury
    *  30.0M Strategic / Investor (Orion Fund)
    *  27.5M Liquidity
          - 5.0M bridge peg reserve
          - 17.5M protocol-owned DEX liquidity
          - 5.0M maker rebates
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RESERVE_LEDGER = _PROJECT_ROOT / "audit_pack" / "2026-04-19" / "reserve_ledger.json"

NIR_TOTAL_BASE = 277_500_000_000_000  # 277.5M cMATRA
VALIDATOR_EMISSIONS_BASE = 115_000_000_000_000
ATTESTOR_EMISSIONS_BASE = 65_000_000_000_000
ECOSYSTEM_TREASURY_BASE = 40_000_000_000_000
STRATEGIC_BASE = 30_000_000_000_000
LIQUIDITY_BASE = 27_500_000_000_000


@pytest.fixture(scope="module")
def ledger() -> dict:
    assert _RESERVE_LEDGER.exists(), f"{_RESERVE_LEDGER} missing"
    with open(_RESERVE_LEDGER) as f:
        return json.load(f)


class TestNetworkIncentivesReserve:
    def test_section_present(self, ledger: dict):
        assert "network_incentives_reserve" in ledger, (
            "reserve_ledger.json must materialize network_incentives_reserve "
            "with the 5 v5.1 sub-buckets (not just team_treasury + nft)."
        )

    def test_total_base_units_is_277_5m(self, ledger: dict):
        nir = ledger["network_incentives_reserve"]
        assert nir["total_base_units"] == NIR_TOTAL_BASE

    def test_total_display_units_is_277_5m(self, ledger: dict):
        nir = ledger["network_incentives_reserve"]
        assert nir["total_display_units"] == 277_500_000

    def test_all_five_sub_buckets_present(self, ledger: dict):
        sb = ledger["network_incentives_reserve"]["sub_buckets"]
        expected = {
            "validator_emissions",
            "attestor_emissions",
            "ecosystem_treasury",
            "strategic_investor",
            "liquidity",
        }
        assert set(sb.keys()) == expected

    def test_validator_emissions_is_115m(self, ledger: dict):
        sb = ledger["network_incentives_reserve"]["sub_buckets"]
        assert sb["validator_emissions"]["base"] == VALIDATOR_EMISSIONS_BASE
        assert sb["validator_emissions"]["display"] == 115_000_000

    def test_attestor_emissions_is_65m(self, ledger: dict):
        sb = ledger["network_incentives_reserve"]["sub_buckets"]
        assert sb["attestor_emissions"]["base"] == ATTESTOR_EMISSIONS_BASE
        assert sb["attestor_emissions"]["display"] == 65_000_000

    def test_ecosystem_treasury_is_40m(self, ledger: dict):
        sb = ledger["network_incentives_reserve"]["sub_buckets"]
        assert sb["ecosystem_treasury"]["base"] == ECOSYSTEM_TREASURY_BASE
        assert sb["ecosystem_treasury"]["display"] == 40_000_000

    def test_strategic_investor_is_30m(self, ledger: dict):
        sb = ledger["network_incentives_reserve"]["sub_buckets"]
        assert sb["strategic_investor"]["base"] == STRATEGIC_BASE
        assert sb["strategic_investor"]["display"] == 30_000_000

    def test_liquidity_total_is_27_5m(self, ledger: dict):
        sb = ledger["network_incentives_reserve"]["sub_buckets"]
        liq = sb["liquidity"]
        assert liq["base"] == LIQUIDITY_BASE
        assert liq["display"] == 27_500_000

    def test_liquidity_components_sum_to_total(self, ledger: dict):
        sb = ledger["network_incentives_reserve"]["sub_buckets"]
        liq = sb["liquidity"]
        comp = liq["components"]
        total = sum(c["base"] for c in comp.values())
        assert total == liq["base"]

    def test_liquidity_bridge_peg_is_5m(self, ledger: dict):
        comp = (
            ledger["network_incentives_reserve"]
            ["sub_buckets"]["liquidity"]["components"]
        )
        assert comp["bridge_peg_reserve"]["base"] == 5_000_000_000_000

    def test_liquidity_pol_is_17_5m(self, ledger: dict):
        comp = (
            ledger["network_incentives_reserve"]
            ["sub_buckets"]["liquidity"]["components"]
        )
        assert comp["protocol_owned_dex_liquidity"]["base"] == 17_500_000_000_000

    def test_liquidity_maker_rebates_is_5m(self, ledger: dict):
        comp = (
            ledger["network_incentives_reserve"]
            ["sub_buckets"]["liquidity"]["components"]
        )
        assert comp["maker_rebates"]["base"] == 5_000_000_000_000

    def test_sub_buckets_sum_to_total(self, ledger: dict):
        """The five sub-buckets MUST sum to exactly 277.5M base units."""
        sb = ledger["network_incentives_reserve"]["sub_buckets"]
        total = (
            sb["validator_emissions"]["base"]
            + sb["attestor_emissions"]["base"]
            + sb["ecosystem_treasury"]["base"]
            + sb["strategic_investor"]["base"]
            + sb["liquidity"]["base"]
        )
        assert total == NIR_TOTAL_BASE

    def test_assertion_string_present(self, ledger: dict):
        """A descriptive assertion field documenting the invariant."""
        nir = ledger["network_incentives_reserve"]
        assert "assertion" in nir


class TestPreservedLegacySections:
    """The existing team_treasury and nft_conditional sections must remain."""

    def test_team_treasury_preserved(self, ledger: dict):
        assert "team_treasury" in ledger
        assert "AGENT" in ledger["team_treasury"]
        assert "SHARDS" in ledger["team_treasury"]

    def test_nft_conditional_preserved(self, ledger: dict):
        assert "nft_conditional" in ledger
