"""Tests for tools.build_flux_claim_index — index construction and validation."""

import pytest

from tools.build_flux_claim_index import (
    build_index_from_manifest,
    write_index_csv,
)
from tools.cardano_utils import encode_claim_datum
from tests.conftest import (
    SAMPLE_FLUX_ASSET,
    SAMPLE_FLUX_POLICY,
    SAMPLE_PKH_1,
    SAMPLE_PKH_2,
)


def _make_utxo_response(pkh: str, flux_qty: int, output_index: int) -> dict:
    """Create a mock Blockfrost UTxO output."""
    datum_cbor = encode_claim_datum(pkh)
    flux_unit = SAMPLE_FLUX_POLICY + SAMPLE_FLUX_ASSET
    return {
        "address": "addr1w_claim_script",
        "output_index": output_index,
        "inline_datum": datum_cbor.hex(),
        "amount": [
            {"unit": "lovelace", "quantity": "2000000"},
            {"unit": flux_unit, "quantity": str(flux_qty)},
        ],
    }


class TestBuildIndex:
    def test_basic_index_building(self, mock_manifest, mocker):
        """Index should map keyhashes to their UTxO refs."""
        mock_bf = mocker.MagicMock()
        tx_hash = mock_manifest["batches"][0]["tx_hash"]
        mock_bf.get_tx_utxos.return_value = {
            "outputs": [
                _make_utxo_response(SAMPLE_PKH_1, 600000, 0),
                _make_utxo_response(SAMPLE_PKH_2, 400000, 1),
                # Change output (not at script address)
                {
                    "address": "addr1_change",
                    "output_index": 2,
                    "amount": [{"unit": "lovelace", "quantity": "5000000"}],
                },
            ],
        }

        result = build_index_from_manifest(mock_bf, mock_manifest)
        index = result["index_min"]

        assert SAMPLE_PKH_1 in index
        assert SAMPLE_PKH_2 in index
        assert len(index[SAMPLE_PKH_1]) == 1
        assert index[SAMPLE_PKH_1][0][0] == tx_hash  # tx_hash
        assert index[SAMPLE_PKH_1][0][1] == 0  # output_index
        assert index[SAMPLE_PKH_1][0][2] == 600000  # flux_units

    def test_diagnostics_clean(self, mock_manifest, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_tx_utxos.return_value = {
            "outputs": [
                _make_utxo_response(SAMPLE_PKH_1, 600000, 0),
                _make_utxo_response(SAMPLE_PKH_2, 400000, 1),
            ],
        }

        result = build_index_from_manifest(mock_bf, mock_manifest)
        stats = result["index_full"]["stats"]

        assert stats["missing_keyhashes"] == 0
        assert stats["mismatches"] == 0
        assert stats["total_matched"] == 2

    def test_missing_keyhash_detected(self, mock_manifest, mocker):
        """If a UTxO for an expected claimant is missing, flag it."""
        mock_bf = mocker.MagicMock()
        # Only return one of the two expected outputs
        mock_bf.get_tx_utxos.return_value = {
            "outputs": [
                _make_utxo_response(SAMPLE_PKH_1, 600000, 0),
            ],
        }

        result = build_index_from_manifest(mock_bf, mock_manifest)
        stats = result["index_full"]["stats"]

        assert stats["missing_keyhashes"] == 1
        assert SAMPLE_PKH_2 in result["index_full"]["diagnostics"]["missing_keyhashes"]

    def test_quantity_mismatch_detected(self, mock_manifest, mocker):
        mock_bf = mocker.MagicMock()
        # Return wrong quantity for PKH_1
        mock_bf.get_tx_utxos.return_value = {
            "outputs": [
                _make_utxo_response(SAMPLE_PKH_1, 999999, 0),  # wrong qty
                _make_utxo_response(SAMPLE_PKH_2, 400000, 1),
            ],
        }

        result = build_index_from_manifest(mock_bf, mock_manifest)
        mismatches = result["index_full"]["diagnostics"]["mismatches"]

        assert len(mismatches) == 1
        assert mismatches[0]["pkh"] == SAMPLE_PKH_1
        assert mismatches[0]["expected_flux"] == 600000
        assert mismatches[0]["actual_flux"] == 999999

    def test_tx_fetch_failure_handled(self, mock_manifest, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_tx_utxos.side_effect = Exception("network error")

        result = build_index_from_manifest(mock_bf, mock_manifest)
        stats = result["index_full"]["stats"]

        # Both claimants should be missing
        assert stats["missing_keyhashes"] == 2
        assert stats["total_matched"] == 0


class TestIndexCSV:
    def test_csv_write(self, tmp_path):
        index = {
            SAMPLE_PKH_1: [["tx1", 0, 100], ["tx2", 1, 200]],
            SAMPLE_PKH_2: [["tx3", 0, 300]],
        }
        csv_path = tmp_path / "index.csv"
        write_index_csv(index, csv_path)
        assert csv_path.exists()

        import csv
        with open(csv_path) as f:
            rows = list(csv.reader(f))
        # Header + 3 data rows
        assert len(rows) == 4
        assert rows[0] == ["payment_key_hash_hex", "tx_hash", "output_index", "flux_units"]
