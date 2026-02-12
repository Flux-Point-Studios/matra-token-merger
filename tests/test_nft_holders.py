"""Tests for NFT holder scanning, datum PKH extraction, and script resolution."""

import pytest
import cbor2

from tests.conftest import SAMPLE_PKH_1, SAMPLE_PKH_2
from tools.config import FLUX_PASS, SE_BRAWLERS, NftCollectionInfo
from tools.snapshot_allocate_flux import (
    _extract_pkh_from_datum,
    _find_28_byte_field,
    _resolve_nft_script_owner,
    fetch_nft_holders,
)


class TestFind28ByteField:
    def test_direct_bytes(self):
        pkh = bytes.fromhex(SAMPLE_PKH_1)
        assert _find_28_byte_field(pkh) == SAMPLE_PKH_1

    def test_in_list(self):
        pkh = bytes.fromhex(SAMPLE_PKH_1)
        assert _find_28_byte_field([42, pkh, "other"]) == SAMPLE_PKH_1

    def test_in_tuple(self):
        pkh = bytes.fromhex(SAMPLE_PKH_1)
        assert _find_28_byte_field((pkh, 99)) == SAMPLE_PKH_1

    def test_in_cbor_tag(self):
        pkh = bytes.fromhex(SAMPLE_PKH_1)
        tag = cbor2.CBORTag(121, [pkh])
        assert _find_28_byte_field(tag) == SAMPLE_PKH_1

    def test_nested_cbor_tag(self):
        pkh = bytes.fromhex(SAMPLE_PKH_1)
        inner = cbor2.CBORTag(0, [pkh, 1000])
        outer = cbor2.CBORTag(121, [inner])
        # Default max_depth=3 can't reach 4 levels; use higher depth
        assert _find_28_byte_field(outer, max_depth=5) == SAMPLE_PKH_1

    def test_wrong_length_bytes(self):
        assert _find_28_byte_field(b"too_short") is None
        assert _find_28_byte_field(b"x" * 32) is None

    def test_none_input(self):
        assert _find_28_byte_field(None) is None

    def test_int_input(self):
        assert _find_28_byte_field(42) is None

    def test_max_depth_respected(self):
        pkh = bytes.fromhex(SAMPLE_PKH_1)
        nested = [[[[pkh]]]]  # 4 levels
        assert _find_28_byte_field(nested, max_depth=3) is None
        assert _find_28_byte_field(nested, max_depth=5) == SAMPLE_PKH_1

    def test_first_match_returned(self):
        pkh1 = bytes.fromhex(SAMPLE_PKH_1)
        pkh2 = bytes.fromhex(SAMPLE_PKH_2)
        assert _find_28_byte_field([pkh1, pkh2]) == SAMPLE_PKH_1


class TestExtractPkhFromDatum:
    def test_valid_datum(self):
        pkh = bytes.fromhex(SAMPLE_PKH_1)
        datum_hex = cbor2.dumps(cbor2.CBORTag(121, [pkh])).hex()
        assert _extract_pkh_from_datum({"inline_datum": datum_hex}) == SAMPLE_PKH_1

    def test_no_inline_datum(self):
        assert _extract_pkh_from_datum({}) is None
        assert _extract_pkh_from_datum({"inline_datum": None}) is None

    def test_invalid_cbor(self):
        assert _extract_pkh_from_datum({"inline_datum": "zzzzzz"}) is None

    def test_datum_without_28_byte_field(self):
        datum_hex = cbor2.dumps(cbor2.CBORTag(121, [42, b"short"])).hex()
        assert _extract_pkh_from_datum({"inline_datum": datum_hex}) is None


class TestResolveNftScriptOwner:
    def test_strategy1_datum(self, mocker):
        """Strategy 1: extract PKH from inline datum."""
        mock_bf = mocker.MagicMock()
        pkh = bytes.fromhex(SAMPLE_PKH_1)
        datum_hex = cbor2.dumps(cbor2.CBORTag(121, [pkh])).hex()

        mock_bf.get_address_utxos.return_value = [
            {"inline_datum": datum_hex, "tx_hash": "aabb" * 16},
        ]

        result = _resolve_nft_script_owner(mock_bf, "addr1w_script", "nft_unit")
        assert result == SAMPLE_PKH_1

    def test_strategy2_tx_tracing(self, mocker):
        """Strategy 2: trace deposit tx to find sender."""
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            {"inline_datum": None, "tx_hash": "aabb" * 16},
        ]

        # Mock address_to_payment_key_hash to return PKH for pubkey addresses
        from unittest.mock import patch

        mock_bf.get_tx_utxos.return_value = {
            "inputs": [
                {
                    "address": "addr1_sender",
                    "amount": [{"unit": "nft_unit", "quantity": "1"}],
                },
            ],
        }

        with patch("tools.snapshot_allocate_flux.is_script_address", return_value=False):
            with patch(
                "tools.snapshot_allocate_flux.address_to_payment_key_hash",
                return_value=SAMPLE_PKH_1,
            ):
                result = _resolve_nft_script_owner(mock_bf, "addr1w_script", "nft_unit")
                assert result == SAMPLE_PKH_1

    def test_unresolvable_returns_none(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = []
        result = _resolve_nft_script_owner(mock_bf, "addr1w_script", "nft_unit")
        assert result is None


class TestFetchNftHolders:
    def test_basic_aggregation(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [
            {"asset": "p1nft1"}, {"asset": "p1nft2"}, {"asset": "p1nft3"},
        ]
        mock_bf.get_asset_addresses.side_effect = [
            [{"address": "addr_a", "quantity": "1"}],
            [{"address": "addr_b", "quantity": "1"}],
            [{"address": "addr_a", "quantity": "1"}],
        ]

        holders = fetch_nft_holders(mock_bf, FLUX_PASS, resolve_scripts=False)
        by_addr = {h["address"]: h["quantity"] for h in holders}
        assert by_addr["addr_a"] == 2
        assert by_addr["addr_b"] == 1

    def test_empty_policy(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = []

        holders = fetch_nft_holders(mock_bf, FLUX_PASS, resolve_scripts=False)
        assert holders == []

    def test_skips_empty_asset(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [{"asset": ""}, {"asset": "p1nft1"}]
        mock_bf.get_asset_addresses.return_value = [
            {"address": "addr_a", "quantity": "1"},
        ]

        holders = fetch_nft_holders(mock_bf, FLUX_PASS, resolve_scripts=False)
        assert len(holders) == 1
        mock_bf.get_asset_addresses.assert_called_once_with("p1nft1")

    def test_format_matches_fetch_holders(self, mocker):
        """fetch_nft_holders returns same format as fetch_holders."""
        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [{"asset": "p1nft1"}]
        mock_bf.get_asset_addresses.return_value = [
            {"address": "addr_a", "quantity": "1"},
        ]

        holders = fetch_nft_holders(mock_bf, FLUX_PASS, resolve_scripts=False)
        assert len(holders) == 1
        assert "address" in holders[0]
        assert "quantity" in holders[0]
        assert isinstance(holders[0]["quantity"], int)
