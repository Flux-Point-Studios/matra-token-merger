"""Tests for tools.admin_reclaim — UTxO discovery and reclaim tx building."""

import pytest

from tools.admin_reclaim import (
    discover_unclaimed_utxos,
    build_reclaim_tx,
    run_admin_reclaim,
    FLUX_ASSET_NAME_HEX,
)
from tools.cardano_utils import encode_claim_datum, posix_ms_to_slot
from tests.conftest import SAMPLE_PKH_1, SAMPLE_PKH_2, SAMPLE_FLUX_POLICY

SCRIPT_ADDR = "addr1w_claim_script"
FLUX_UNIT = SAMPLE_FLUX_POLICY + FLUX_ASSET_NAME_HEX


def _make_utxo_response(
    tx_hash: str = "aa" * 32,
    output_index: int = 0,
    ada: int = 2_000_000,
    flux_qty: int = 1_000_000,
    pkh: str = SAMPLE_PKH_1,
) -> dict:
    """Create a mock Blockfrost UTxO response entry."""
    datum_hex = encode_claim_datum(pkh).hex()
    return {
        "tx_hash": tx_hash,
        "output_index": output_index,
        "amount": [
            {"unit": "lovelace", "quantity": str(ada)},
            {"unit": FLUX_UNIT, "quantity": str(flux_qty)},
        ],
        "inline_datum": datum_hex,
    }


class TestDiscoverUnclaimedUtxos:
    def test_finds_utxos(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(pkh=SAMPLE_PKH_1, flux_qty=500_000),
            _make_utxo_response(tx_hash="bb" * 32, pkh=SAMPLE_PKH_2, flux_qty=300_000),
        ]

        result = discover_unclaimed_utxos(mock_bf, SCRIPT_ADDR, SAMPLE_FLUX_POLICY)
        assert len(result) == 2
        assert result[0]["flux_units"] == 500_000
        assert result[0]["datum_pkh"] == SAMPLE_PKH_1
        assert result[1]["flux_units"] == 300_000

    def test_empty_script_address(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = []

        result = discover_unclaimed_utxos(mock_bf, SCRIPT_ADDR, SAMPLE_FLUX_POLICY)
        assert result == []

    def test_utxo_without_datum(self, mocker):
        mock_bf = mocker.MagicMock()
        utxo = _make_utxo_response()
        utxo["inline_datum"] = None
        mock_bf.get_address_utxos.return_value = [utxo]

        result = discover_unclaimed_utxos(mock_bf, SCRIPT_ADDR, SAMPLE_FLUX_POLICY)
        assert len(result) == 1
        assert result[0]["datum_pkh"] is None

    def test_utxo_ada_extraction(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(ada=5_000_000),
        ]

        result = discover_unclaimed_utxos(mock_bf, SCRIPT_ADDR, SAMPLE_FLUX_POLICY)
        assert result[0]["ada_lovelace"] == 5_000_000


class TestPosixMsToSlot:
    def test_mainnet_known_value(self):
        # Mainnet shelley start: slot 4492800 at posix 1596491091
        # So posix_ms = 1596491091000 → slot 4492800
        slot = posix_ms_to_slot(1596491091000, "mainnet")
        assert slot == 4492800

    def test_mainnet_offset(self):
        # 1000 seconds after shelley start
        slot = posix_ms_to_slot(1596492091000, "mainnet")
        assert slot == 4492800 + 1000

    def test_preprod_known_value(self):
        # Preprod: shelley_start_slot=0, shelley_start_time=1655683200
        slot = posix_ms_to_slot(1655683200000, "preprod")
        assert slot == 0

    def test_preprod_offset(self):
        slot = posix_ms_to_slot(1655684200000, "preprod")
        assert slot == 1000


class TestRunAdminReclaim:
    def test_check_only_mode(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(flux_qty=500_000),
        ]

        report = run_admin_reclaim(
            mock_bf,
            admin_skey_path="unused",
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
            check_only=True,
        )
        assert report["mode"] == "check_only"
        assert report["unclaimed_count"] == 1
        assert report["total_flux_units"] == 500_000

    def test_no_unclaimed_utxos(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = []

        report = run_admin_reclaim(
            mock_bf,
            admin_skey_path="unused",
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
        )
        assert report["mode"] == "no_action"
        assert report["unclaimed_count"] == 0

    def test_deadline_utc_in_report(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = []

        report = run_admin_reclaim(
            mock_bf,
            admin_skey_path="unused",
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
        )
        assert "deadline_utc" in report
        assert "2023" in report["deadline_utc"]  # 1.7T ms ≈ Nov 2023

    def test_multiple_utxos_summed(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(flux_qty=100_000, ada=2_000_000),
            _make_utxo_response(tx_hash="bb" * 32, flux_qty=200_000, ada=3_000_000),
        ]

        report = run_admin_reclaim(
            mock_bf,
            admin_skey_path="unused",
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
            check_only=True,
        )
        assert report["total_flux_units"] == 300_000
        assert report["total_ada_lovelace"] == 5_000_000
