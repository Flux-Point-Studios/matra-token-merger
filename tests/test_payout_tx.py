"""tools.attestor_payout.payout_tx: the unsigned payout transaction, from the
2-of-2 native-script float to each statement line, with the change built
here and never left to a builder that can burn it into the fee."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from nacl.signing import SigningKey
from pycardano import (
    Address,
    AuxiliaryData,
    Network,
    ScriptAll,
    ScriptPubkey,
    Transaction,
    TransactionWitnessSet,
    VerificationKeyHash,
    VerificationKeyWitness,
)
from pycardano.key import VerificationKey
from pycardano.utils import min_lovelace_post_alonzo

from tests.test_payout_statement import (
    CMATRA,
    REGISTRY,
    address,
    busy_period,
    config_doc,
    slice_of,
    write_config,
)
from tools.attestor_payout.payout_tx import (
    PayoutTxError,
    build_payout_tx,
    fee_params,
    float_address,
    float_script,
    float_utxos,
)
from tools.attestor_payout.statement import anchor_metadata, build_statement, load_period_config
from tools.credential_denylist import DenyList

PARAMS = json.loads((Path(__file__).resolve().parent / "fixtures/payout_koios_cli_protocol_params.json").read_text())
NO_DENIALS = DenyList(labels={b"\xee" * 28: "unrelated"}, allowed=frozenset(), sha256="0" * 64)
SIGNER_SEEDS = (b"\x41" * 32, b"\x42" * 32)
SIGNER_HASHES = [VerificationKey.from_primitive(SigningKey(s).verify_key.encode()).hash().payload.hex()
                 for s in SIGNER_SEEDS]
POLICY = bytes.fromhex(CMATRA["policy"])
ASSET = bytes.fromhex(CMATRA["asset"])
OTHER_TOKEN = {"policy_id": "dd" * 28, "asset_name": "6f74686572", "quantity": "7"}


@pytest.fixture
def config(tmp_path):
    return load_period_config(write_config(tmp_path, config_doc(float_keys=SIGNER_HASHES)))


@pytest.fixture
def statement(config):
    return build_statement(config, slice_of(busy_period().events), REGISTRY)


def koios_row(addr: str, n: int, lovelace: int, cmatra: int, *extra) -> dict:
    assets = [{"policy_id": CMATRA["policy"], "asset_name": CMATRA["asset"], "quantity": str(cmatra)}] if cmatra else []
    return {
        "tx_hash": f"{n:02x}" * 32, "tx_index": n, "address": addr, "value": str(lovelace),
        "asset_list": assets + list(extra), "is_spent": False,
        "datum_hash": None, "inline_datum": None, "reference_script": None,
    }


def float_rows(config, lovelace=(20_000_000, 3_000_000), cmatra=(50_000_000, 15_000_000)):
    addr = float_address(config.float_keys, Network.TESTNET).encode()
    rows = [koios_row(addr, n, ada, tok) for n, (ada, tok) in enumerate(zip(lovelace, cmatra))]
    rows[0]["asset_list"].append(OTHER_TOKEN)
    return rows


def build(statement, config, rows=None, denylist=NO_DENIALS) -> Transaction:
    rows = rows if rows is not None else float_rows(config)
    utxos = float_utxos(rows, float_address(config.float_keys, Network.TESTNET))
    return build_payout_tx(statement, config, utxos, fee_params(PARAMS), denylist)


def cmatra_in(value) -> int:
    return value.multi_asset.to_primitive().get(POLICY, {}).get(ASSET, 0)


def test_the_float_is_a_two_key_native_script():
    keys = (b"\x01" * 28, b"\x02" * 28)
    assert float_script(keys) == ScriptAll([ScriptPubkey(VerificationKeyHash(k)) for k in keys])
    assert float_address(keys, Network.MAINNET) == Address(payment_part=float_script(keys).hash(),
                                                           network=Network.MAINNET)


def test_each_line_is_paid_its_cmatra_and_the_rest_returns_to_the_float(statement, config):
    tx = build(statement, config)
    body = tx.transaction_body
    paid = [(str(o.address), cmatra_in(o.amount)) for o in body.outputs[:-1]]
    assert paid == [(address(n).encode(), line["amount"]) for n, line in zip((1, 2, 3), statement["lines"])]
    change = body.outputs[-1]
    assert change.address == float_address(config.float_keys, Network.TESTNET)
    assert cmatra_in(change.amount) == 65_000_000 - 60_000_000
    assert change.amount.multi_asset.to_primitive()[bytes.fromhex("dd" * 28)] == {b"other": 7}
    assert len(body.outputs) == 4


def test_the_transaction_spends_every_float_utxo_and_conserves_value(statement, config):
    tx = build(statement, config)
    body = tx.transaction_body
    assert len(body.inputs) == 2
    out_coin = sum(o.amount.coin for o in body.outputs)
    assert out_coin + body.fee == 23_000_000
    assert sum(cmatra_in(o.amount) for o in body.outputs) == 65_000_000


def test_every_output_holds_its_minimum_ada(statement, config):
    ctx = type("Ctx", (), {"protocol_param": type("P", (), {"coins_per_utxo_byte": PARAMS["utxoCostPerByte"]})()})()
    for out in build(statement, config).transaction_body.outputs:
        assert out.amount.coin >= min_lovelace_post_alonzo(copy.deepcopy(out), ctx)


def test_the_fee_covers_the_transaction_once_both_keys_have_signed(statement, config):
    tx = build(statement, config)
    body_hash = tx.transaction_body.hash()
    signed = Transaction(
        tx.transaction_body,
        TransactionWitnessSet(
            vkey_witnesses=[VerificationKeyWitness(
                VerificationKey.from_primitive(SigningKey(s).verify_key.encode()),
                SigningKey(s).sign(body_hash).signature) for s in SIGNER_SEEDS],
            native_scripts=[float_script(config.float_keys)],
        ),
        auxiliary_data=tx.auxiliary_data,
    )
    assert tx.transaction_body.fee >= PARAMS["txFeePerByte"] * len(signed.to_cbor()) + PARAMS["txFeeFixed"]
    assert tx.transaction_witness_set.vkey_witnesses in (None, [])
    assert tx.transaction_witness_set.native_scripts == [float_script(config.float_keys)]


def test_the_transaction_anchors_the_statement_under_label_8746(statement, config):
    tx = build(statement, config)
    assert isinstance(tx.auxiliary_data, AuxiliaryData)
    assert tx.auxiliary_data.data.metadata[8746] == anchor_metadata(statement)
    assert tx.transaction_body.auxiliary_data_hash == tx.auxiliary_data.hash()


def test_the_same_inputs_build_the_same_bytes(statement, config):
    assert build(statement, config).to_cbor() == build(statement, config).to_cbor()


def test_a_float_short_of_cmatra_is_refused(statement, config):
    with pytest.raises(PayoutTxError, match="cMATRA"):
        build(statement, config, float_rows(config, cmatra=(50_000_000, 9_999_999)))


@pytest.mark.parametrize("lovelace, match", [
    ((3_000_000, 1_000_000), "change of .* under its minimum ADA"),
    ((2_000_000, 1_000_000), "cannot fund the outputs and the fee in ADA"),
])
def test_a_float_short_of_ada_is_refused_not_burned_into_the_fee(statement, config, lovelace, match):
    with pytest.raises(PayoutTxError, match=match):
        build(statement, config, float_rows(config, lovelace=lovelace))


def test_a_line_over_its_cap_is_refused(statement, config):
    statement["lines"][0]["amount"] = config.cap_per_attestor + 1
    with pytest.raises(PayoutTxError, match="cap"):
        build(statement, config)


def test_a_statement_whose_lines_do_not_match_its_root_is_refused(statement, config):
    statement["lines"][1]["amount"] -= 1
    statement["total"] -= 1
    with pytest.raises(PayoutTxError, match="root"):
        build(statement, config)


def test_a_statement_whose_total_is_not_its_lines_is_refused(statement, config):
    statement["total"] += 1
    with pytest.raises(PayoutTxError, match="total"):
        build(statement, config)


def test_a_statement_priced_under_another_config_is_refused(statement, tmp_path):
    other = load_period_config(write_config(tmp_path, config_doc(float_keys=SIGNER_HASHES, cap_total=90_000_000)))
    with pytest.raises(PayoutTxError, match="rates"):
        build(statement, other)


def test_a_denied_payout_address_is_refused(statement, config):
    denied = DenyList(labels={bytes([2]) * 28: "exposed key"}, allowed=frozenset(), sha256="0" * 64)
    with pytest.raises(PayoutTxError, match="exposed key"):
        build(statement, config, denylist=denied)


def test_a_statement_that_pays_no_one_is_refused(statement, config):
    for line in statement["lines"]:
        line["amount"] = 0
    statement["total"] = 0
    statement["merkle"] = {"root": "00" * 32, "leaves": 0}
    with pytest.raises(PayoutTxError, match="no line"):
        build(statement, config)


def test_utxos_of_another_address_or_already_spent_are_refused(config):
    addr = float_address(config.float_keys, Network.TESTNET)
    stray = koios_row(address(9).encode(), 3, 2_000_000, 0)
    with pytest.raises(PayoutTxError, match="address"):
        float_utxos([stray], addr)
    spent = float_rows(config)
    spent[0]["is_spent"] = True
    with pytest.raises(PayoutTxError, match="spent"):
        float_utxos(spent, addr)
    with pytest.raises(PayoutTxError, match="no UTxO"):
        float_utxos([], addr)


def test_protocol_parameters_read_from_koios(config):
    params = fee_params(PARAMS)
    assert (params.fee_per_byte, params.fee_fixed, params.coins_per_utxo_byte, params.max_tx_size) == (
        44, 155_381, 4_310, 16_384)
    with pytest.raises(PayoutTxError, match="utxoCostPerByte"):
        fee_params({k: v for k, v in PARAMS.items() if k != "utxoCostPerByte"})


def test_a_transaction_over_the_size_limit_is_refused(statement, config):
    small = dict(PARAMS, maxTxSize=800)
    utxos = float_utxos(float_rows(config), float_address(config.float_keys, Network.TESTNET))
    with pytest.raises(PayoutTxError, match="size"):
        build_payout_tx(statement, config, utxos, fee_params(small), NO_DENIALS)
