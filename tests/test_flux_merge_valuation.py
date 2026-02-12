"""Tests for tools.flux_merge_valuation_int — weights, buckets, invariants."""

import pytest

from tools.flux_merge_valuation_int import (
    compute_integer_buckets,
    compute_valuations,
)
from tools.config import AGENT, FLUX_MAX_SUPPLY_BASE, LEGACY_TOKENS, SHARDS


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
    def test_buckets_sum_to_max(self):
        weights = {"AGENT": 0.7, "SHARDS": 0.3}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert sum(buckets.values()) == FLUX_MAX_SUPPLY_BASE

    def test_exact_split(self):
        weights = {"AGENT": 0.5, "SHARDS": 0.5}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert sum(buckets.values()) == FLUX_MAX_SUPPLY_BASE
        # Both should be exactly half
        assert buckets["AGENT"] == FLUX_MAX_SUPPLY_BASE // 2
        assert buckets["SHARDS"] == FLUX_MAX_SUPPLY_BASE - buckets["AGENT"]

    def test_extreme_weight_disparity(self):
        """When one token dominates, the other still gets its share."""
        weights = {"AGENT": 0.999999, "SHARDS": 0.000001}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert sum(buckets.values()) == FLUX_MAX_SUPPLY_BASE
        assert buckets["AGENT"] > 0
        assert buckets["SHARDS"] > 0

    def test_remainder_goes_to_last_token(self):
        """Floor rounding means the last token absorbs any remainder."""
        weights = {"AGENT": 1 / 3, "SHARDS": 2 / 3}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert sum(buckets.values()) == FLUX_MAX_SUPPLY_BASE

        # AGENT bucket should be floor(1/3 * 1e15)
        expected_agent = int((1 / 3) * FLUX_MAX_SUPPLY_BASE)
        assert buckets["AGENT"] == expected_agent
        assert buckets["SHARDS"] == FLUX_MAX_SUPPLY_BASE - expected_agent

    def test_custom_total(self):
        weights = {"AGENT": 0.6, "SHARDS": 0.4}
        total = 1_000_000
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights, total)
        assert sum(buckets.values()) == total

    def test_all_weight_to_one(self):
        weights = {"AGENT": 1.0, "SHARDS": 0.0}
        buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
        assert buckets["AGENT"] == FLUX_MAX_SUPPLY_BASE
        assert buckets["SHARDS"] == 0

    def test_invariant_across_many_weights(self):
        """Buckets must always sum to FLUX_MAX_SUPPLY_BASE."""
        import random
        random.seed(42)
        for _ in range(100):
            w = random.random()
            weights = {"AGENT": w, "SHARDS": 1 - w}
            buckets = compute_integer_buckets(LEGACY_TOKENS, weights)
            assert sum(buckets.values()) == FLUX_MAX_SUPPLY_BASE


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

    def test_seven_buckets_sum_to_max(self):
        assets = self._make_seven_assets()
        supplies = self._make_seven_supplies()
        prices = self._make_seven_prices()
        val_data = compute_valuations(assets, supplies, prices)
        buckets = compute_integer_buckets(assets, val_data["weights"])
        assert sum(buckets.values()) == FLUX_MAX_SUPPLY_BASE

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
            assert sum(buckets.values()) == FLUX_MAX_SUPPLY_BASE


class TestFetchNftSupply:
    def test_counts_assets(self, mocker):
        from tools.flux_merge_valuation_int import fetch_nft_supply
        from tools.config import FLUX_PASS

        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [
            {"asset": "nft1"}, {"asset": "nft2"}, {"asset": "nft3"},
        ]
        assert fetch_nft_supply(mock_bf, FLUX_PASS) == 3

    def test_empty_policy(self, mocker):
        from tools.flux_merge_valuation_int import fetch_nft_supply
        from tools.config import FLUX_PASS

        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = []
        assert fetch_nft_supply(mock_bf, FLUX_PASS) == 0
