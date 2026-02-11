"""Tests for tools.claim_flux_indexed — UTxO verification and claim logic."""

import pytest

from tools.claim_flux_indexed import (
    find_claimable_utxos,
    load_script_from_blueprint,
    verify_claim_utxo,
)
from tools.cardano_utils import encode_claim_datum
from tests.conftest import (
    SAMPLE_FLUX_ASSET,
    SAMPLE_FLUX_POLICY,
    SAMPLE_PKH_1,
    SAMPLE_PKH_2,
)

SCRIPT_ADDR = "addr1w_claim_script"
FLUX_UNIT = SAMPLE_FLUX_POLICY + SAMPLE_FLUX_ASSET


def _make_utxo_data(
    address: str = SCRIPT_ADDR,
    pkh: str = SAMPLE_PKH_1,
    flux_qty: int = 1_000_000,
    spent: bool = False,
) -> dict:
    datum_cbor = encode_claim_datum(pkh)
    return {
        "address": address,
        "output_index": 0,
        "inline_datum": datum_cbor.hex(),
        "amount": [
            {"unit": "lovelace", "quantity": "2000000"},
            {"unit": FLUX_UNIT, "quantity": str(flux_qty)},
        ],
        "consumed_by_tx": "some_tx" if spent else None,
    }


class TestVerifyClaimUtxo:
    def test_valid_utxo(self):
        data = _make_utxo_data()
        result = verify_claim_utxo(
            data, SCRIPT_ADDR, SAMPLE_PKH_1,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        assert result["valid"] is True
        assert result["is_spent"] is False
        assert result["flux_units"] == 1_000_000
        assert result["ada_lovelace"] == 2_000_000
        assert result["issues"] == []

    def test_wrong_address(self):
        data = _make_utxo_data(address="addr1_wrong")
        result = verify_claim_utxo(
            data, SCRIPT_ADDR, SAMPLE_PKH_1,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        assert result["valid"] is False
        assert any("address mismatch" in i for i in result["issues"])

    def test_wrong_pkh_in_datum(self):
        data = _make_utxo_data(pkh=SAMPLE_PKH_2)
        result = verify_claim_utxo(
            data, SCRIPT_ADDR, SAMPLE_PKH_1,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        assert result["valid"] is False
        assert any("datum pkh mismatch" in i for i in result["issues"])

    def test_already_spent(self):
        data = _make_utxo_data(spent=True)
        result = verify_claim_utxo(
            data, SCRIPT_ADDR, SAMPLE_PKH_1,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        assert result["valid"] is False
        assert result["is_spent"] is True

    def test_no_inline_datum(self):
        data = _make_utxo_data()
        data["inline_datum"] = None
        result = verify_claim_utxo(
            data, SCRIPT_ADDR, SAMPLE_PKH_1,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        assert result["valid"] is False
        assert any("no inline datum" in i for i in result["issues"])

    def test_no_flux_asset(self):
        data = _make_utxo_data()
        data["amount"] = [{"unit": "lovelace", "quantity": "2000000"}]
        result = verify_claim_utxo(
            data, SCRIPT_ADDR, SAMPLE_PKH_1,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        assert result["valid"] is False
        assert any("no FLUX asset" in i for i in result["issues"])


class TestFindClaimableUtxos:
    def test_finds_valid_utxos(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_tx_utxos.return_value = {
            "outputs": [
                _make_utxo_data(pkh=SAMPLE_PKH_1, flux_qty=500000),
            ],
        }

        index = {SAMPLE_PKH_1: [["tx_abc", 0, 500000]]}
        result = find_claimable_utxos(
            mock_bf, SAMPLE_PKH_1, index, SCRIPT_ADDR,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )

        assert len(result) == 1
        assert result[0]["valid"] is True
        assert result[0]["flux_units"] == 500000

    def test_no_refs_in_index(self, mocker):
        mock_bf = mocker.MagicMock()
        index = {}  # PKH not in index
        result = find_claimable_utxos(
            mock_bf, SAMPLE_PKH_1, index, SCRIPT_ADDR,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        assert result == []
        mock_bf.get_tx_utxos.assert_not_called()

    def test_spent_utxo_marked(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_tx_utxos.return_value = {
            "outputs": [
                _make_utxo_data(pkh=SAMPLE_PKH_1, spent=True),
            ],
        }

        index = {SAMPLE_PKH_1: [["tx_abc", 0, 500000]]}
        result = find_claimable_utxos(
            mock_bf, SAMPLE_PKH_1, index, SCRIPT_ADDR,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )

        assert len(result) == 1
        assert result[0]["valid"] is False
        assert result[0]["is_spent"] is True

    def test_output_not_found(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_tx_utxos.return_value = {
            "outputs": [
                # Output at index 5, but we're looking for index 0
                {**_make_utxo_data(), "output_index": 5},
            ],
        }

        index = {SAMPLE_PKH_1: [["tx_abc", 0, 500000]]}
        result = find_claimable_utxos(
            mock_bf, SAMPLE_PKH_1, index, SCRIPT_ADDR,
            SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        assert len(result) == 0  # Skipped because output not found


class TestLoadScriptFromBlueprint:
    def test_loads_claim_validator(self, tmp_path):
        blueprint = {
            "validators": [
                {
                    "title": "claim_validator.claim_validator.spend",
                    "compiledCode": "deadbeef01234567",
                },
            ],
        }
        bp_path = tmp_path / "plutus.json"
        bp_path.write_text(__import__("json").dumps(blueprint))

        result = load_script_from_blueprint(str(bp_path))
        assert result == "deadbeef01234567"

    def test_fallback_to_first_validator(self, tmp_path):
        blueprint = {
            "validators": [
                {
                    "title": "some_other_validator",
                    "compiledCode": "aabbccdd",
                },
            ],
        }
        bp_path = tmp_path / "plutus.json"
        bp_path.write_text(__import__("json").dumps(blueprint))

        result = load_script_from_blueprint(str(bp_path))
        assert result == "aabbccdd"

    def test_no_validators_raises(self, tmp_path):
        blueprint = {"validators": []}
        bp_path = tmp_path / "plutus.json"
        bp_path.write_text(__import__("json").dumps(blueprint))

        with pytest.raises(ValueError, match="No validators"):
            load_script_from_blueprint(str(bp_path))
