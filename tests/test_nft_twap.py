"""Tests for NFT floor-price TWAP computation."""

import pytest

from tools.twap_snapshot_pools import compute_nft_floor_twap, compute_twap, NFT_WINDOW_CONFIGS
from tools.config import FLUX_PASS, SE_BRAWLERS, NftCollectionInfo


class TestComputeNftFloorTwap:
    def test_basic_computation(self, mocker):
        candles = [{"close": 10.0}, {"close": 20.0}, {"close": 30.0}]
        mock_client = mocker.MagicMock()
        mock_client.get_nft_collection_ohlcv.return_value = candles

        result = compute_nft_floor_twap(mock_client, FLUX_PASS, "1d", 3)
        assert result["twap"] == pytest.approx(20.0)
        assert result["num_candles_received"] == 3
        assert result["latest_close"] == 30.0
        assert result["min_close"] == 10.0
        assert result["max_close"] == 30.0

    def test_single_candle(self, mocker):
        mock_client = mocker.MagicMock()
        mock_client.get_nft_collection_ohlcv.return_value = [{"close": 42.5}]

        result = compute_nft_floor_twap(mock_client, FLUX_PASS, "1d", 1)
        assert result["twap"] == pytest.approx(42.5)

    def test_empty_candles(self, mocker):
        mock_client = mocker.MagicMock()
        mock_client.get_nft_collection_ohlcv.return_value = []

        result = compute_nft_floor_twap(mock_client, FLUX_PASS, "1d", 7)
        assert result["twap"] == 0.0
        assert result["latest_close"] is None
        assert result["min_close"] is None
        assert result["max_close"] is None

    def test_uses_policy_id(self, mocker):
        mock_client = mocker.MagicMock()
        mock_client.get_nft_collection_ohlcv.return_value = [{"close": 1.0}]

        compute_nft_floor_twap(mock_client, FLUX_PASS, "1d", 7)
        mock_client.get_nft_collection_ohlcv.assert_called_once_with(
            FLUX_PASS.policy_id, "1d", 7,
        )

    def test_consistent_with_fungible_twap(self, mocker):
        """NFT floor TWAP uses the same math as fungible TWAP."""
        candles = [{"close": float(i)} for i in range(1, 11)]
        mock_client = mocker.MagicMock()
        mock_client.get_nft_collection_ohlcv.return_value = candles

        nft_result = compute_nft_floor_twap(mock_client, SE_BRAWLERS, "1d", 10)
        fungible_twap = compute_twap(candles)
        assert nft_result["twap"] == pytest.approx(fungible_twap)

    def test_none_prices_skipped(self, mocker):
        candles = [{"close": 10.0}, {"close": None}, {"close": 30.0}]
        mock_client = mocker.MagicMock()
        mock_client.get_nft_collection_ohlcv.return_value = candles

        result = compute_nft_floor_twap(mock_client, FLUX_PASS, "1d", 3)
        assert result["twap"] == pytest.approx(20.0)
        assert result["num_candles_received"] == 3


class TestNftWindowConfigs:
    def test_has_7d(self):
        assert "7d" in NFT_WINDOW_CONFIGS

    def test_has_24h(self):
        assert "24h" in NFT_WINDOW_CONFIGS

    def test_has_30d(self):
        assert "30d" in NFT_WINDOW_CONFIGS

    def test_7d_values(self):
        interval, num = NFT_WINDOW_CONFIGS["7d"]
        assert interval == "1d"
        assert num == 7

    def test_24h_values(self):
        interval, num = NFT_WINDOW_CONFIGS["24h"]
        assert interval == "1h"
        assert num == 24
