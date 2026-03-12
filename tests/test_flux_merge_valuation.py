"""Tests for tools.flux_merge_valuation_int — weights, buckets, invariants."""

import pytest

from tools.flux_merge_valuation_int import (
    build_rate_table,
    compute_integer_buckets,
    compute_valuations,
)
from tools.config import AGENT, FLUX_MAX_SUPPLY_BASE, LEGACY_TOKENS, PUBLIC_POOL_BASE, SHARDS


class TestComputeValuations:
    def test_basic_valuations(self):
        supplies = {"AGENT": 50_000_000, "SHARDS": 100_000_000_000_000}
        prices = {"AGENT": 1.0, "SHARDS": 0.0005}

        result = compute_valuations(LEGACY_TOKENS, supplies, prices)

        # AGENT: 50M tokens * $1.0 = $50M
        assert result["valuations_usd"]["AGENT"] == pytest.approx(50_000_000.0)
        # SHARDS: 100M display * $0.0005 = $50K
        assert result["valuations_usd"]["SHARDS"] == pytest.approx(50_000.0)

        assert result["total_valuation_usd"] == pytest.approx(50_050_000.0)

        # Weights should sum to 1
        assert sum(result["weights"].values()) == pytest.approx(1.0)

    def test_equal_valuations(self):
        # AGENT: 1M supply * 0 decimals → 1M display * $1000 = $1B
        # SHARDS: 1e12 supply / 1e6 decimals → 1M display * $1000 = $1B
        supplies = {"AGENT": 1_000_000, "SHARDS": 1_000_000_000_000}
        prices = {"AGENT": 1000.0, "SHARDS": 1000.0}

        result = compute_valuations(LEGACY_TOKENS, supplies, prices)

        assert result["weights"]["AGENT"] == pytest.approx(0.5)
        assert result["weights"]["SHARDS"] == pytest.approx(0.5)

    def test_zero_price_raises(self):
        supplies = {"AGENT": 1000, "SHARDS": 1000}
        prices = {"AGENT": 0.0, "SHARDS": 0.0}

        with pytest.raises(ValueError, match="zero or negative"):
            compute_valuations(LEGACY_TOKENS, supplies, prices)

    def test_weights_always_sum_to_one(self):
        """Test with various supply/price combos."""
        test_cases = [
            ({"AGENT": 1, "SHARDS": 1_000_000}, {"AGENT": 1000000.0, "SHARDS": 1.0}),
            ({"AGENT": 999999, "SHARDS": 1_000_000_000_000}, {"AGENT": 0.001, "SHARDS": 0.000001}),
            ({"AGENT": 50_000_000, "SHARDS": 100_000_000_000_000}, {"AGENT": 1.0, "SHARDS": 0.0005}),
        ]
        for supplies, prices in test_cases:
            result = compute_valuations(LEGACY_TOKENS, supplies, prices)
            assert sum(result["weights"].values()) == pytest.approx(1.0, abs=1e-10)


class TestComputeIntegerBuckets:
    def test_buckets_sum_to_public_pool(self):
        weights = {"AGENT": 0.7, "SHARDS": 0.3}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert sum(buckets.values()) == PUBLIC_POOL_BASE

    def test_exact_split(self):
        weights = {"AGENT": 0.5, "SHARDS": 0.5}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert sum(buckets.values()) == PUBLIC_POOL_BASE
        # Both should be exactly half
        assert buckets["AGENT"] == PUBLIC_POOL_BASE // 2
        assert buckets["SHARDS"] == PUBLIC_POOL_BASE - buckets["AGENT"]

    def test_extreme_weight_disparity(self):
        """When one token dominates, the other still gets its share."""
        weights = {"AGENT": 0.999999, "SHARDS": 0.000001}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert sum(buckets.values()) == PUBLIC_POOL_BASE
        assert buckets["AGENT"] > 0
        assert buckets["SHARDS"] > 0

    def test_remainder_goes_to_last_token(self):
        """Floor rounding means the last token absorbs any remainder."""
        weights = {"AGENT": 1 / 3, "SHARDS": 2 / 3}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert sum(buckets.values()) == PUBLIC_POOL_BASE

        expected_agent = int((1 / 3) * PUBLIC_POOL_BASE)
        assert buckets["AGENT"] == expected_agent
        assert buckets["SHARDS"] == PUBLIC_POOL_BASE - expected_agent

    def test_custom_total(self):
        weights = {"AGENT": 0.6, "SHARDS": 0.4}
        total = 1_000_000
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights, total)
        assert sum(buckets.values()) == total

    def test_all_weight_to_one(self):
        weights = {"AGENT": 1.0, "SHARDS": 0.0}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert buckets["AGENT"] == PUBLIC_POOL_BASE
        assert buckets["SHARDS"] == 0

    def test_invariant_across_many_weights(self):
        """Buckets must always sum to PUBLIC_POOL_BASE."""
        import random
        random.seed(42)
        for _ in range(100):
            w = random.random()
            weights = {"AGENT": w, "SHARDS": 1 - w}
            buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
            assert sum(buckets.values()) == PUBLIC_POOL_BASE


class TestSevenAssetValuations:
    """Test valuations with all 7 merge assets (2 fungible + 5 NFT)."""

    def _make_seven_assets(self):
        from tools.config import LEGACY_TOKENS, NFT_COLLECTIONS
        return list(LEGACY_TOKENS) + list(NFT_COLLECTIONS)

    def _make_seven_supplies(self):
        return {
            "AGENT": 50_000_000,
            "SHARDS": 100_000_000_000_000,
            "FLUX_PASS": 500,
            "SE_BRAWLERS": 10_000,
            "BRAWL_PASS_ETD": 5_000,
            "T1_ADAM_PASS": 2_000,
            "T2_ADAM_PASS": 3_000,
        }

    def _make_seven_prices(self):
        return {
            "AGENT": 1.0,
            "SHARDS": 0.0005,
            "FLUX_PASS": 50.0,
            "SE_BRAWLERS": 15.0,
            "BRAWL_PASS_ETD": 10.0,
            "T1_ADAM_PASS": 5.0,
            "T2_ADAM_PASS": 2.5,
        }

    def test_seven_weights_sum_to_one(self):
        assets = self._make_seven_assets()
        supplies = self._make_seven_supplies()
        prices = self._make_seven_prices()
        result = compute_valuations(assets, supplies, prices)
        assert sum(result["weights"].values()) == pytest.approx(1.0)

    def test_seven_buckets_sum_to_public_pool(self):
        assets = self._make_seven_assets()
        supplies = self._make_seven_supplies()
        prices = self._make_seven_prices()
        val_data = compute_valuations(assets, supplies, prices)
        buckets = compute_integer_buckets(assets, val_data["weights"])
        assert sum(buckets.values()) == PUBLIC_POOL_BASE

    def test_all_seven_buckets_positive(self):
        assets = self._make_seven_assets()
        supplies = self._make_seven_supplies()
        prices = self._make_seven_prices()
        val_data = compute_valuations(assets, supplies, prices)
        buckets = compute_integer_buckets(assets, val_data["weights"])
        for name, bucket in buckets.items():
            assert bucket > 0, f"{name} bucket is zero"

    def test_seven_buckets_invariant_randomized(self):
        import random
        random.seed(42)
        assets = self._make_seven_assets()
        for _ in range(50):
            weights = {}
            remaining = 1.0
            for i, asset in enumerate(assets):
                if i < len(assets) - 1:
                    w = random.random() * remaining * 0.5
                    weights[asset.name] = w
                    remaining -= w
                else:
                    weights[asset.name] = remaining
            buckets = compute_integer_buckets(assets, weights)
            assert sum(buckets.values()) == PUBLIC_POOL_BASE


class TestBuildRateTable:
    """Tests for the redemption rate table builder."""

    def test_basic_rate_table(self):
        report = {
            "public_pool_base_units": PUBLIC_POOL_BASE,
            "validator_reserve_base_units": FLUX_MAX_SUPPLY_BASE - PUBLIC_POOL_BASE,
            "tokens": {
                "AGENT": {
                    "decimals": 0,
                    "supply_base_units": 1_000_000_000,
                    "flux_bucket_base_units": int(0.667 * PUBLIC_POOL_BASE),
                },
                "SHARDS": {
                    "decimals": 6,
                    "supply_base_units": 3_000_000_000_000,
                    "flux_bucket_base_units": PUBLIC_POOL_BASE - int(0.667 * PUBLIC_POOL_BASE),
                },
            },
        }
        rt = build_rate_table(report)
        assert rt["report_type"] == "redemption_rate_table"
        assert rt["public_pool_base"] == PUBLIC_POOL_BASE
        for name, entry in rt["tokens"].items():
            assert entry["rate_base_per_unit"] > 0
            assert entry["redeemable_supply_base"] == report["tokens"][name]["supply_base_units"]

    def test_team_waiver_reduces_denominator(self):
        bucket = 500_000_000_000_000  # 500T base (realistic for 6-decimal cMATRA)
        supply = 1_000_000_000
        waiver = 100_000_000  # 10% waived
        report = {
            "public_pool_base_units": PUBLIC_POOL_BASE,
            "validator_reserve_base_units": FLUX_MAX_SUPPLY_BASE - PUBLIC_POOL_BASE,
            "tokens": {
                "AGENT": {
                    "decimals": 0,
                    "supply_base_units": supply,
                    "flux_bucket_base_units": bucket,
                },
            },
        }
        rt_no_waiver = build_rate_table(report)
        rt_waiver = build_rate_table(report, team_waiver_supplies={"AGENT": waiver})

        # With waiver, rate should be higher (same bucket, fewer redeemable units)
        assert rt_waiver["tokens"]["AGENT"]["rate_base_per_unit"] > rt_no_waiver["tokens"]["AGENT"]["rate_base_per_unit"]
        assert rt_waiver["tokens"]["AGENT"]["redeemable_supply_base"] == supply - waiver

    def test_nft_rate(self):
        nft_bucket = 30_000_000_000_000_000_000  # 30B base
        nft_count = 401
        report = {
            "public_pool_base_units": PUBLIC_POOL_BASE,
            "validator_reserve_base_units": FLUX_MAX_SUPPLY_BASE - PUBLIC_POOL_BASE,
            "tokens": {
                "FLUX_PASS": {
                    "decimals": 0,
                    "supply_base_units": nft_count,
                    "flux_bucket_base_units": nft_bucket,
                    "is_nft": True,
                },
            },
        }
        rt = build_rate_table(report)
        entry = rt["tokens"]["FLUX_PASS"]
        assert entry["is_nft"] is True
        assert entry["rate_base_per_unit"] == nft_bucket // nft_count


class TestTeamCarve:
    """Tests for team carve-out computation in rate table."""

    def _make_report(self, agent_bucket=500_000_000_000_000, shards_bucket=100_000_000_000_000):
        return {
            "public_pool_base_units": PUBLIC_POOL_BASE,
            "validator_reserve_base_units": FLUX_MAX_SUPPLY_BASE - PUBLIC_POOL_BASE,
            "tokens": {
                "AGENT": {
                    "decimals": 0,
                    "supply_base_units": 1_000_000_000,
                    "flux_bucket_base_units": agent_bucket,
                },
                "SHARDS": {
                    "decimals": 6,
                    "supply_base_units": 3_000_000_000_000,
                    "flux_bucket_base_units": shards_bucket,
                },
            },
        }

    def test_carve_present_when_waivers_given(self):
        rt = build_rate_table(self._make_report(), team_waiver_supplies={"AGENT": 100})
        assert "team_carve" in rt
        assert "AGENT" in rt["team_carve"]["per_asset"]

    def test_carve_absent_without_waivers(self):
        rt = build_rate_table(self._make_report())
        assert "team_carve" not in rt

    def test_carve_math(self):
        waiver = 10_000_000
        rt = build_rate_table(self._make_report(), team_waiver_supplies={"AGENT": waiver})
        rate = rt["tokens"]["AGENT"]["rate_base_per_unit"]
        carve = rt["team_carve"]["per_asset"]["AGENT"]["carve_cmatra_base"]
        assert carve == waiver * rate

    def test_carve_total(self):
        waivers = {"AGENT": 10_000_000, "SHARDS": 500_000_000_000}
        rt = build_rate_table(self._make_report(), team_waiver_supplies=waivers)
        per_asset = rt["team_carve"]["per_asset"]
        expected_total = sum(v["carve_cmatra_base"] for v in per_asset.values())
        assert rt["team_carve"]["total_carve_base"] == expected_total

    def test_pool_after_carve(self):
        waivers = {"AGENT": 10_000_000, "SHARDS": 500_000_000_000}
        rt = build_rate_table(self._make_report(), team_waiver_supplies=waivers)
        tc = rt["team_carve"]
        assert tc["pool_after_carve_base"] == PUBLIC_POOL_BASE - tc["total_carve_base"]

    def test_carve_does_not_exceed_pool(self):
        """If carve would exceed pool, assertion fires."""
        import pytest
        # Supply=2 with waiver=1 gives rate=bucket/1=bucket; carve=1*bucket=bucket > pool
        report = {
            "public_pool_base_units": 100,
            "validator_reserve_base_units": 50,
            "tokens": {
                "AGENT": {
                    "decimals": 0,
                    "supply_base_units": 2,
                    "flux_bucket_base_units": 100,
                },
            },
        }
        # waiver=1, redeemable=1, rate=100, carve=100 == pool → not less than
        with pytest.raises(AssertionError, match="exceeds public pool"):
            build_rate_table(report, team_waiver_supplies={"AGENT": 1})


class TestFetchNftSupply:
    def test_counts_only_nfts(self, mocker):
        from tools.flux_merge_valuation_int import fetch_nft_supply
        from tools.config import FLUX_PASS

        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [
            {"asset": "nft1", "quantity": "1"},
            {"asset": "nft2", "quantity": "1"},
            {"asset": "nft3", "quantity": "1"},
        ]
        assert fetch_nft_supply(mock_bf, FLUX_PASS) == 3

    def test_excludes_fungible_assets(self, mocker):
        """Assets with quantity > 1 are fungible tokens, not NFTs."""
        from tools.flux_merge_valuation_int import fetch_nft_supply
        from tools.config import FLUX_PASS

        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [
            {"asset": "nft1", "quantity": "1"},
            {"asset": "fungible1", "quantity": "3"},
            {"asset": "nft2", "quantity": "1"},
            {"asset": "fungible2", "quantity": "2"},
        ]
        # Only the two qty=1 assets count
        assert fetch_nft_supply(mock_bf, FLUX_PASS) == 2

    def test_empty_policy(self, mocker):
        from tools.flux_merge_valuation_int import fetch_nft_supply
        from tools.config import FLUX_PASS

        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = []
        assert fetch_nft_supply(mock_bf, FLUX_PASS) == 0
