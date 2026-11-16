"""tools.attestor_payout.materios_events: the read-only Materios JSON-RPC
reader, run against answers recorded from preprod blocks 2753504-2753537
(spec 242), so the events are decoded from the bytes a node serves with the
metadata it serves."""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

import pytest
import responses

from tools.attestor_payout.materios_events import (
    EVENTS_KEY,
    MateriosRpcError,
    read_chain,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures/payout_preprod_events.json.gz"
URL = "https://materios.example/rpc"
GENESIS = "0x0e46e33f639a56cc8780fd871d9a15e16d99af248526f907cb560cb40849f7bf"


def recorded() -> dict:
    with gzip.open(FIXTURE, "rt") as f:
        return json.load(f)


class Node:
    """Answers JSON-RPC, single or batched, from a recording."""

    def __init__(self, rec: dict):
        self.rec = rec
        self.calls: list[str] = []

    def answer(self, call: dict) -> dict:
        method, params = call["method"], call["params"]
        self.calls.append(method)
        rec = self.rec
        if method == "chain_getBlockHash":
            result = rec["genesis"] if params[0] == 0 else rec["blocks"].get(str(params[0]))
        elif method == "chain_getFinalizedHead":
            result = rec["finalized"]["hash"]
        elif method == "chain_getHeader":
            result = {"number": hex(rec["finalized"]["number"])}
        elif method == "state_getRuntimeVersion":
            result = rec["runtime"][params[0]]
        elif method == "state_getMetadata":
            result = rec["metadata"][str(rec["runtime"][params[0]]["specVersion"])]
        elif method == "state_getStorage":
            assert params[0] == EVENTS_KEY
            if params[1] not in rec["storage"]:
                return {"jsonrpc": "2.0", "id": call["id"], "error": {
                    "code": 4003, "message": "Client error: Api called for an unknown Block: State already discarded"}}
            result = rec["storage"][params[1]]
        else:
            raise AssertionError(method)
        return {"jsonrpc": "2.0", "id": call["id"], "result": result}

    def __call__(self, request):
        body = json.loads(request.body)
        if isinstance(body, list):
            return 200, {}, json.dumps([self.answer(c) for c in reversed(body)])
        return 200, {}, json.dumps(self.answer(body))


@pytest.fixture
def node():
    n = Node(recorded())
    with responses.RequestsMock() as mock:
        mock.add_callback(responses.POST, URL, callback=n, content_type="application/json")
        yield n


def test_the_reader_returns_the_genesis_the_bounding_hashes_and_the_tracked_events(node):
    chain = read_chain(URL, 2753505, 2753509, 2753537)
    assert chain.genesis == GENESIS
    assert (chain.scan_from, chain.first, chain.last) == (2753505, 2753509, 2753537)
    assert chain.first_hash == node.rec["blocks"]["2753509"]
    assert chain.last_hash == node.rec["blocks"]["2753537"]
    at_2753534 = [(e.index, e.extrinsic, e.name, dict(e.fields)) for e in chain.events if e.block == 2753534]
    assert at_2753534 == [
        (2, 2, "AttestationRecorded", {
            "receipt_id": "0x1a7feaf3bfd9afd9df4168528d9d6294c8f4ab24f24a321832df2163c6f59268",
            "attester": "0x925fe8605fe32a53a7b391498fc1b0ab91d3af7319607bd70b850b4f5fa9d255",
            "count": 1, "threshold": 3}),
        (4, 3, "AttestationRecorded", {
            "receipt_id": "0x1a7feaf3bfd9afd9df4168528d9d6294c8f4ab24f24a321832df2163c6f59268",
            "attester": "0x961e7b9152c5b910d61ff0397d7f023f6e28ca489fbb43135a7cd59cbbdebd36",
            "count": 2, "threshold": 3}),
        (6, 4, "AttestationRecorded", {
            "receipt_id": "0x1a7feaf3bfd9afd9df4168528d9d6294c8f4ab24f24a321832df2163c6f59268",
            "attester": "0x8ed446c7114fbeb751866e6752dedf36fba9b3d2832a9fc50a005e00ed0ab124",
            "count": 3, "threshold": 3}),
        (21, 4, "AvailabilityCertified", {
            "receipt_id": "0x1a7feaf3bfd9afd9df4168528d9d6294c8f4ab24f24a321832df2163c6f59268",
            "cert_hash": "0x1b38b8af46d7f0640a2a567ee12022e219374d706cbc59445c0176abb6388d77"}),
    ]
    certified = [e.fields["receipt_id"][:10] for e in chain.events if e.name == "AvailabilityCertified"]
    assert certified == ["0x49915bca", "0x08fd0940", "0x1a7feaf3", "0xed71f571"]
    assert all(e.name in {"AttestationRecorded", "AvailabilityCertified"} for e in chain.events)
    assert [e.block for e in chain.events] == sorted(e.block for e in chain.events)


def test_the_reader_fetches_metadata_once_per_runtime_and_batches_blocks(node):
    read_chain(URL, 2753505, 2753505, 2753537)
    assert node.calls.count("state_getMetadata") == 1
    assert node.calls.count("state_getStorage") == 33


def test_a_runtime_upgrade_inside_a_batch_decodes_each_block_with_its_parents_runtime():
    rec = recorded()
    upgraded = copy.deepcopy(rec)
    upgraded["metadata"]["243"] = rec["metadata"]["242"]
    for n in range(2753520, 2753538):
        upgraded["runtime"][rec["blocks"][str(n)]]["specVersion"] = 243
    node = Node(upgraded)
    with responses.RequestsMock() as mock:
        mock.add_callback(responses.POST, URL, callback=node, content_type="application/json")
        chain = read_chain(URL, 2753505, 2753505, 2753537)
    assert node.calls.count("state_getMetadata") == 2
    assert node.calls.count("state_getRuntimeVersion") > 2
    assert sum(e.name == "AvailabilityCertified" for e in chain.events) == 4


def test_a_range_past_the_finalized_head_is_refused(node):
    with pytest.raises(MateriosRpcError, match="finalized"):
        read_chain(URL, 2753505, 2753505, node.rec["finalized"]["number"] + 1)


def test_a_pruned_block_is_refused_rather_than_read_as_empty():
    rec = recorded()
    del rec["storage"][rec["blocks"]["2753520"]]
    with responses.RequestsMock() as mock:
        mock.add_callback(responses.POST, URL, callback=Node(rec), content_type="application/json")
        with pytest.raises(MateriosRpcError, match="State already discarded"):
            read_chain(URL, 2753505, 2753505, 2753537)


def test_a_block_with_no_events_stored_is_refused():
    rec = recorded()
    rec["storage"][rec["blocks"]["2753520"]] = None
    with responses.RequestsMock() as mock:
        mock.add_callback(responses.POST, URL, callback=Node(rec), content_type="application/json")
        with pytest.raises(MateriosRpcError, match="2753520"):
            read_chain(URL, 2753505, 2753505, 2753537)


def test_an_answer_missing_from_a_batch_is_refused():
    rec = recorded()
    node = Node(rec)

    def drop_one(request):
        status, headers, body = node(request)
        answers = json.loads(body)
        return status, headers, json.dumps(answers[1:] if isinstance(answers, list) else answers)

    with responses.RequestsMock() as mock:
        mock.add_callback(responses.POST, URL, callback=drop_one, content_type="application/json")
        with pytest.raises(MateriosRpcError, match="answer"):
            read_chain(URL, 2753505, 2753505, 2753537)


@pytest.mark.parametrize("scan_from, first, last", [(0, 1, 2), (5, 4, 6), (5, 6, 5)])
def test_a_misordered_range_is_refused(scan_from, first, last):
    with pytest.raises(ValueError):
        read_chain(URL, scan_from, first, last)


def test_a_websocket_url_is_refused():
    with pytest.raises(ValueError, match="http"):
        read_chain("wss://materios.example/rpc", 1, 1, 2)
