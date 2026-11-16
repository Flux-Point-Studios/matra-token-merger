"""python -m tools.attestor_payout: build a statement from one node, check it
against a second, then build the unsigned payout transaction from Koios
reads, run end to end on the recorded preprod blocks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import responses
from nacl.signing import SigningKey
from pycardano import Address, Network, Transaction, VerificationKeyHash

from tests.test_payout_events import Node, recorded
from tests.test_payout_registration import STAKE, wallet_sign
from tests.test_payout_statement import PREPROD_SIGNERS
from tests.test_payout_tx import CMATRA, SIGNER_HASHES, koios_row
from tools.attestor_payout.__main__ import main
from tools.attestor_payout.payout_tx import float_address
from tools.attestor_payout.registration import registration_message
from tools.attestor_payout.statement import statement_digest
from tools.config import KOIOS_BASE_URLS

BUILDER_RPC = "https://builder.example/rpc"
CHECKER_RPC = "https://checker.example/rpc"
KOIOS = KOIOS_BASE_URLS["preprod"]
CHAIN = "0e46e33f639a56cc8780fd871d9a15e16d99af248526f907cb560cb40849f7bf"
FPS_SEAT = "0x64964344fff93f562d94488c5fc340408817de10c4a4783e5e0dde6c8a4ba53e"
PARAMS = json.loads((Path(__file__).resolve().parent / "fixtures/payout_koios_cli_protocol_params.json").read_text())


def seed(attestor: str) -> bytes:
    return bytes.fromhex(attestor[2:])


def payout_address(attestor: str) -> Address:
    vk = SigningKey(seed(attestor)).verify_key.encode()
    return Address(payment_part=VerificationKeyHash(hashlib.blake2b(vk, digest_size=28).digest()),
                   staking_part=STAKE, network=Network.TESTNET)


@pytest.fixture
def files(tmp_path):
    config = {
        "version": 1, "network": "preprod", "chain": CHAIN, "period": 202611,
        "blocks": {"first": 2753509, "last": 2753537, "lookback": 4},
        "rates": {"usd_micros_per_credit": 20_000, "usd_micros_per_cmatra": 2_000,
                  "price_source": "fixture price", "cap_per_attestor": 25_000_000, "cap_total": 200_000_000},
        "excluded": [FPS_SEAT], "cmatra": CMATRA, "float_keys": SIGNER_HASHES,
    }
    registrations = []
    for attestor in PREPROD_SIGNERS:
        if attestor == FPS_SEAT:
            continue
        address = payout_address(attestor)
        payload = registration_message(CHAIN, seed(attestor), address.encode())
        registrations.append({"attestor": attestor, "address": address.encode(),
                              **wallet_sign(seed(attestor), payload, address)})
    paths = {name: tmp_path / f"{name}.json" for name in ("config", "registry", "statement", "check", "tx")}
    paths["config"].write_text(json.dumps(config))
    paths["registry"].write_text(json.dumps({"version": 1, "chain": CHAIN, "registrations": registrations}))
    return paths


@pytest.fixture
def chain_and_koios():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        for url in (BUILDER_RPC, CHECKER_RPC):
            mock.add_callback(responses.POST, url, callback=Node(recorded()), content_type="application/json")
        float_addr = float_address(tuple(bytes.fromhex(k) for k in SIGNER_HASHES), Network.TESTNET).encode()
        mock.add(responses.POST, f"{KOIOS}/address_utxos",
                 json=[koios_row(float_addr, 0, 30_000_000, 200_000_000)])
        mock.add(responses.GET, f"{KOIOS}/cli_protocol_params", json=PARAMS)
        yield mock


def run(*args) -> int:
    return main([str(a) for a in args])


def test_build_check_and_tx_pay_each_registered_preprod_signer(files, chain_and_koios, capsys):
    f = files
    assert run("build", "--config", f["config"], "--registry", f["registry"], "--rpc", BUILDER_RPC,
               "--out", f["statement"]) == 0
    statement = json.loads(f["statement"].read_text())
    assert {line["attestor"]: line["credits"] for line in statement["lines"]} == {
        a: n for a, n in PREPROD_SIGNERS.items() if a != FPS_SEAT}
    assert statement["excluded"] == [{"attestor": FPS_SEAT, "credits": 1}]
    amounts = {line["attestor"][:6]: line["amount"] for line in statement["lines"]}
    assert amounts == {"0x44f3": 20_000_000, "0x8ed4": 20_000_000, "0x925f": 25_000_000,
                       "0x961e": 20_000_000, "0xb221": 20_000_000}

    assert run("check", "--config", f["config"], "--registry", f["registry"], "--rpc", CHECKER_RPC,
               "--statement", f["statement"], "--out", f["check"]) == 0
    check = json.loads(f["check"].read_text())
    assert check["statement"] == statement_digest(statement)

    assert run("tx", "--config", f["config"], "--statement", f["statement"], "--check", f["check"],
               "--out", f["tx"]) == 0
    envelope = json.loads(f["tx"].read_text())
    assert envelope["type"] == "Tx ConwayEra"
    tx = Transaction.from_cbor(envelope["cborHex"])
    assert len(tx.transaction_body.outputs) == len(statement["lines"]) + 1
    assert [str(o.address) for o in tx.transaction_body.outputs[:-1]] == [l["address"] for l in statement["lines"]]
    assert tx.transaction_witness_set.vkey_witnesses in (None, [])
    out = capsys.readouterr().out
    assert statement["merkle"]["root"] in out


def test_check_refuses_a_statement_edited_after_it_was_built(files, chain_and_koios, capsys):
    f = files
    run("build", "--config", f["config"], "--registry", f["registry"], "--rpc", BUILDER_RPC, "--out", f["statement"])
    statement = json.loads(f["statement"].read_text())
    statement["lines"][2]["address"] = statement["lines"][0]["address"]
    f["statement"].write_text(json.dumps(statement))
    assert run("check", "--config", f["config"], "--registry", f["registry"], "--rpc", CHECKER_RPC,
               "--statement", f["statement"], "--out", f["check"]) == 1
    assert "lines[2].address" in capsys.readouterr().err
    assert not f["check"].exists()


def test_tx_refuses_a_statement_the_check_did_not_pass(files, chain_and_koios, capsys):
    f = files
    run("build", "--config", f["config"], "--registry", f["registry"], "--rpc", BUILDER_RPC, "--out", f["statement"])
    f["check"].write_text(json.dumps({"statement": "00" * 32}))
    assert run("tx", "--config", f["config"], "--statement", f["statement"], "--check", f["check"],
               "--out", f["tx"]) == 1
    assert "check" in capsys.readouterr().err
    assert not f["tx"].exists()


def test_build_refuses_to_overwrite_a_statement(files, chain_and_koios, capsys):
    f = files
    f["statement"].write_text("{}")
    assert run("build", "--config", f["config"], "--registry", f["registry"], "--rpc", BUILDER_RPC,
               "--out", f["statement"]) == 1
    assert "exists" in capsys.readouterr().err
    assert f["statement"].read_text() == "{}"


def test_message_prints_what_the_attestor_signs(files, capsys):
    address = payout_address(FPS_SEAT).encode()
    assert run("message", "--config", files["config"], "--attestor", FPS_SEAT, "--address", address) == 0
    out = capsys.readouterr().out
    message = registration_message(CHAIN, seed(FPS_SEAT), address)
    assert message.decode() in out and message.hex() in out


def test_registry_lists_each_verified_address(files, capsys):
    assert run("registry", "--config", files["config"], "--registry", files["registry"]) == 0
    out = capsys.readouterr().out
    for attestor in PREPROD_SIGNERS:
        if attestor != FPS_SEAT:
            assert f"{attestor} {payout_address(attestor).encode()}" in out
