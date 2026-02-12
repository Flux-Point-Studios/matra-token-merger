"""Tests for tools.cross_check_holders — Blockfrost vs Koios comparison."""

import pytest

from tools.cross_check_holders import (
    compare_holders,
    fetch_blockfrost_holders,
    fetch_koios_holders,
    run_cross_check,
)
from tools.config import TokenInfo


SAMPLE_TOKEN = TokenInfo(
    name="TEST",
    policy_id="aabb" * 14,
    asset_name_hex="deadbeef",
    decimals=0,
)


class TestCompareHolders:
    def test_identical_holders_all_match(self):
        holders = {"addr_a": 100, "addr_b": 200, "addr_c": 300}
        result = compare_holders(holders, holders, "TEST")
        assert result["all_match"] is True
        assert result["holder_count_match"] is True
        assert result["supply_match"] is True
        assert result["balance_mismatches"] == 0

    def test_different_counts(self):
        bf = {"addr_a": 100, "addr_b": 200}
        ko = {"addr_a": 100}
        result = compare_holders(bf, ko, "TEST")
        assert result["holder_count_match"] is False
        assert result["all_match"] is False
        assert result["blockfrost_only_addresses"] == 1
        assert result["koios_only_addresses"] == 0

    def test_balance_mismatch(self):
        bf = {"addr_a": 100, "addr_b": 200}
        ko = {"addr_a": 100, "addr_b": 250}
        result = compare_holders(bf, ko, "TEST")
        assert result["holder_count_match"] is True
        assert result["supply_match"] is False
        assert result["balance_mismatches"] == 1
        assert result["all_match"] is False

    def test_supply_totals(self):
        bf = {"addr_a": 100, "addr_b": 200}
        ko = {"addr_a": 100, "addr_b": 200}
        result = compare_holders(bf, ko, "TEST")
        assert result["blockfrost_total_supply"] == 300
        assert result["koios_total_supply"] == 300

    def test_empty_holders(self):
        result = compare_holders({}, {}, "TEST")
        assert result["all_match"] is True
        assert result["blockfrost_holder_count"] == 0
        assert result["koios_holder_count"] == 0

    def test_koios_only_address(self):
        bf = {"addr_a": 100}
        ko = {"addr_a": 100, "addr_b": 200}
        result = compare_holders(bf, ko, "TEST")
        assert result["koios_only_addresses"] == 1
        assert result["blockfrost_only_addresses"] == 0
        assert result["all_match"] is False

    def test_discrepancy_detail_capped(self):
        bf = {f"addr_{i}": i for i in range(100)}
        ko = {f"addr_{i}": i + 1 for i in range(100)}
        result = compare_holders(bf, ko, "TEST")
        assert len(result["discrepancies"]) <= 50


class TestFetchBlockfrostHolders:
    def test_parses_response(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_asset_addresses.return_value = [
            {"address": "addr_a", "quantity": "100"},
            {"address": "addr_b", "quantity": "200"},
        ]
        result = fetch_blockfrost_holders(mock_bf, SAMPLE_TOKEN)
        assert result == {"addr_a": 100, "addr_b": 200}


class TestFetchKoiosHolders:
    def test_parses_response(self, mocker):
        mock_ko = mocker.MagicMock()
        mock_ko.get_asset_addresses.return_value = [
            {"payment_address": "addr_a", "quantity": "100"},
            {"payment_address": "addr_b", "quantity": "200"},
        ]
        result = fetch_koios_holders(mock_ko, SAMPLE_TOKEN)
        assert result == {"addr_a": 100, "addr_b": 200}

    def test_aggregates_duplicate_addresses(self, mocker):
        mock_ko = mocker.MagicMock()
        mock_ko.get_asset_addresses.return_value = [
            {"payment_address": "addr_a", "quantity": "100"},
            {"payment_address": "addr_a", "quantity": "50"},
        ]
        result = fetch_koios_holders(mock_ko, SAMPLE_TOKEN)
        assert result == {"addr_a": 150}

    def test_skips_zero_quantities(self, mocker):
        mock_ko = mocker.MagicMock()
        mock_ko.get_asset_addresses.return_value = [
            {"payment_address": "addr_a", "quantity": "100"},
            {"payment_address": "addr_b", "quantity": "0"},
        ]
        result = fetch_koios_holders(mock_ko, SAMPLE_TOKEN)
        assert "addr_b" not in result


class TestRunCrossCheck:
    def test_all_pass(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_ko = mocker.MagicMock()

        holders = [
            {"address": "addr_a", "quantity": "100"},
            {"address": "addr_b", "quantity": "200"},
        ]
        mock_bf.get_asset_addresses.return_value = holders

        ko_holders = [
            {"payment_address": "addr_a", "quantity": "100"},
            {"payment_address": "addr_b", "quantity": "200"},
        ]
        mock_ko.get_asset_addresses.return_value = ko_holders

        token = TokenInfo("T", "aa" * 28, "bb", 0)
        report = run_cross_check(mock_bf, mock_ko, tokens=[token])
        assert report["all_pass"] is True
        assert len(report["tokens"]) == 1


class TestCompareNftHolders:
    def test_identical_holders(self):
        from tools.cross_check_holders import compare_nft_holders
        holders = {"addr_a": 5, "addr_b": 3}
        result = compare_nft_holders(holders, holders, "TEST_NFT")
        assert result["all_match"] is True
        assert result["mode"] == "best_effort"
        assert result["is_nft"] is True

    def test_supply_mismatch(self):
        from tools.cross_check_holders import compare_nft_holders
        bf = {"addr_a": 5, "addr_b": 3}
        tt = {"addr_a": 5, "addr_b": 2}
        result = compare_nft_holders(bf, tt, "TEST_NFT")
        assert result["all_match"] is False
        assert result["balance_mismatches"] == 1


class TestFetchTaptoolsNftHolders:
    def test_basic_fetch(self, mocker):
        from tools.cross_check_holders import fetch_taptools_nft_holders
        from tools.config import FLUX_PASS

        mock_tt = mocker.MagicMock()
        mock_tt.get_nft_collection_holders_top.return_value = [
            {"address": "addr_a", "quantity": 5},
            {"address": "addr_b", "quantity": 3},
        ]
        result = fetch_taptools_nft_holders(mock_tt, FLUX_PASS, max_pages=1)
        assert result == {"addr_a": 5, "addr_b": 3}

    def test_empty_response(self, mocker):
        from tools.cross_check_holders import fetch_taptools_nft_holders
        from tools.config import FLUX_PASS

        mock_tt = mocker.MagicMock()
        mock_tt.get_nft_collection_holders_top.return_value = []
        result = fetch_taptools_nft_holders(mock_tt, FLUX_PASS)
        assert result == {}


class TestFetchBlockfrostNftHolders:
    def test_basic_enumeration(self, mocker):
        from tools.cross_check_holders import fetch_blockfrost_nft_holders
        from tools.config import FLUX_PASS

        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [
            {"asset": "policy1nft1"},
            {"asset": "policy1nft2"},
        ]
        mock_bf.get_asset_addresses.side_effect = [
            [{"address": "addr_a", "quantity": "1"}],
            [{"address": "addr_a", "quantity": "1"}],
        ]
        result = fetch_blockfrost_nft_holders(mock_bf, FLUX_PASS)
        assert result == {"addr_a": 2}


class TestRunCrossCheckWithNfts:
    def test_with_taptools(self, mocker):
        from tools.cross_check_holders import run_cross_check
        mock_bf = mocker.MagicMock()
        mock_ko = mocker.MagicMock()
        mock_tt = mocker.MagicMock()

        # Empty fungible results
        mock_bf.get_asset_addresses.return_value = []
        mock_ko.get_asset_addresses.return_value = []

        # NFT results
        mock_bf.get_policy_assets.return_value = [{"asset": "nft1"}]
        mock_bf.get_asset_addresses.return_value = [
            {"address": "addr_a", "quantity": "1"},
        ]
        mock_tt.get_nft_collection_holders_top.return_value = [
            {"address": "addr_a", "quantity": 1},
        ]

        from tools.config import NftCollectionInfo
        test_coll = NftCollectionInfo("TEST", "aa" * 28, "Test")
        report = run_cross_check(
            mock_bf, mock_ko, taptools=mock_tt,
            tokens=[], nft_collections=[test_coll],
        )
        assert len(report["tokens"]) == 1
        assert report["tokens"][0]["is_nft"] is True
