"""Tests for tools.twap_snapshot_pools — TWAP computation and pool filtering."""

import pytest

from tools.twap_snapshot_pools import (
    combine_twaps,
    compute_twap,
    discover_pools,
)
from tools.config import AGENT, TokenInfo


class TestComputeTwap:
    def test_uniform_candles(self):
        candles = [{"close": 1.0}, {"close": 2.0}, {"close": 3.0}]
        assert compute_twap(candles) == pytest.approx(2.0)

    def test_single_candle(self):
        candles = [{"close": 5.5}]
        assert compute_twap(candles) == pytest.approx(5.5)

    def test_empty_candles(self):
        assert compute_twap([]) == 0.0

    def test_none_prices_skipped(self):
        candles = [{"close": 1.0}, {"close": None}, {"close": 3.0}]
        assert compute_twap(candles) == pytest.approx(2.0)

    def test_all_same_price(self):
        candles = [{"close": 4.2}] * 100
        assert compute_twap(candles) == pytest.approx(4.2)

    def test_known_average(self):
        candles = [{"close": float(i)} for i in range(1, 11)]
        assert compute_twap(candles) == pytest.approx(5.5)


class TestCombineTwaps:
    def test_median_odd(self):
        assert combine_twaps([1.0, 2.0, 3.0], "median") == 2.0

    def test_median_even(self):
        assert combine_twaps([1.0, 2.0, 3.0, 4.0], "median") == 2.5

    def test_deepest_takes_first(self):
        assert combine_twaps([10.0, 20.0, 30.0], "deepest") == 10.0

    def test_mean(self):
        assert combine_twaps([1.0, 2.0, 3.0], "mean") == pytest.approx(2.0)

    def test_zeros_filtered(self):
        assert combine_twaps([0.0, 0.0, 5.0], "median") == 5.0

    def test_all_zeros(self):
        assert combine_twaps([0.0, 0.0], "median") == 0.0

    def test_empty_list(self):
        assert combine_twaps([], "median") == 0.0

    def test_single_value(self):
        assert combine_twaps([7.7], "median") == 7.7

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="Unknown combine mode"):
            combine_twaps([1.0], "geometric")


class TestDiscoverPools:
    def test_filters_by_tvl(self, mock_pools_agent, mocker):
        """Pools below min TVL should be excluded."""
        mock_client = mocker.MagicMock()
        mock_client.get_token_pools.return_value = mock_pools_agent

        pools = discover_pools(mock_client, AGENT, min_tvl_ada=10000, top_n=3)
        # pool_agent_1 has 500k, pool_agent_2 has 200k, pool_agent_3 has 5k (below 10k)
        assert len(pools) == 2
        assert all(float(p["adaLocked"]) >= 10000 for p in pools)

    def test_top_n_limits(self, mock_pools_agent, mocker):
        mock_client = mocker.MagicMock()
        mock_client.get_token_pools.return_value = mock_pools_agent

        pools = discover_pools(mock_client, AGENT, min_tvl_ada=1000, top_n=1)
        assert len(pools) == 1
        assert pools[0]["pairID"] == "pool_agent_1"  # Highest TVL

    def test_sorted_by_tvl_desc(self, mock_pools_agent, mocker):
        mock_client = mocker.MagicMock()
        mock_client.get_token_pools.return_value = mock_pools_agent

        pools = discover_pools(mock_client, AGENT, min_tvl_ada=1000, top_n=10)
        tvls = [float(p["adaLocked"]) for p in pools]
        assert tvls == sorted(tvls, reverse=True)

    def test_empty_pools(self, mocker):
        mock_client = mocker.MagicMock()
        mock_client.get_token_pools.return_value = []

        pools = discover_pools(mock_client, AGENT, min_tvl_ada=10000, top_n=3)
        assert pools == []
