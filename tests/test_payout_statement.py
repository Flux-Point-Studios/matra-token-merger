"""tools.attestor_payout.statement: a period's certification credits replayed
from Materios events, priced in dollars and capped, committed to by a Merkle
root and a label-8746 record, and recomputed from a second source by the
checker, which refuses any difference."""

from __future__ import annotations

import copy
import json

import pytest
import responses
from pycardano import Address, Network, VerificationKeyHash

from tests.test_payout_events import URL, Node, recorded
from tools.attestor_payout.materios_events import ChainSlice, Event, read_chain
from tools.attestor_payout.merkle import leaf_hash, merkle_root
from tools.attestor_payout.statement import (
    ConfigError,
    HistoryIncomplete,
    StatementError,
    StatementMismatch,
    anchor_metadata,
    build_statement,
    certification_credits,
    check_statement,
    load_period_config,
    statement_digest,
)
from tools.config import ATTESTOR_EMISSIONS_BASE

GENESIS = "0x0e46e33f639a56cc8780fd871d9a15e16d99af248526f907cb560cb40849f7bf"
A, B, C, D = ("0x" + (bytes([n]) * 32).hex() for n in (0xA1, 0xB2, 0xC3, 0xD4))
R1, R2, R3 = ("0x" + (bytes([n]) * 32).hex() for n in (0x01, 0x02, 0x03))


def acct(hex_id: str) -> bytes:
    return bytes.fromhex(hex_id[2:])


class Script:
    """Builds an event stream block by block, one extrinsic per call."""

    def __init__(self):
        self.events: list[Event] = []
        self.block = 100
        self.extrinsic = 0

    def next_block(self, n: int | None = None):
        self.block = n if n is not None else self.block + 1
        self.extrinsic = 0
        return self

    def _emit(self, name, fields, same_extrinsic=False):
        if not same_extrinsic:
            self.extrinsic += 1
        index = sum(e.block == self.block for e in self.events)
        self.events.append(Event(self.block, index, self.extrinsic, name, fields))
        return self

    def attest(self, receipt, who, count, threshold=2):
        return self._emit("AttestationRecorded", {
            "receipt_id": receipt, "attester": who, "count": count, "threshold": threshold})

    def certify(self, receipt):
        """The certification the last attest's extrinsic emits."""
        return self._emit("AvailabilityCertified", {"receipt_id": receipt, "cert_hash": "0x" + "ee" * 32},
                          same_extrinsic=True)

    def root_certify(self, receipt):
        return self._emit("AvailabilityCertified", {"receipt_id": receipt, "cert_hash": "0x" + "ee" * 32})

    def joined(self, who):
        return self._emit("CommitteeMemberJoined", {"who": who, "committee_size": 3})

    def left(self, who):
        return self._emit("CommitteeMemberLeft", {"who": who, "committee_size": 2})

    def ejected(self, who):
        return self._emit("AutoEjected", {"who": who, "remaining_bond": 0})

    def committee(self, *members):
        return self._emit("CommitteeUpdated", {"members": list(members), "threshold": 2})


def test_each_signer_of_a_threshold_certification_in_the_period_gets_one_credit():
    s = Script().attest(R1, A, 1).attest(R1, B, 2).certify(R1)
    s.next_block().attest(R2, B, 1).next_block().attest(R2, C, 2).certify(R2)
    credits, certified = certification_credits(s.events, first=100)
    assert credits == {acct(A): 1, acct(B): 2, acct(C): 1}
    assert certified == 2


def test_a_certification_before_the_period_is_not_credited_but_its_history_is_read():
    s = Script().attest(R1, A, 1).attest(R1, B, 2).certify(R1)
    s.attest(R2, C, 1).next_block(200).attest(R2, A, 2).certify(R2)
    credits, certified = certification_credits(s.events, first=150)
    assert credits == {acct(C): 1, acct(A): 1}
    assert certified == 1


def test_a_root_certification_credits_no_one():
    s = Script().attest(R1, A, 1).root_certify(R1)
    assert certification_credits(s.events, first=100) == ({}, 0)


def test_a_certification_whose_earlier_attestations_were_not_read_is_refused():
    s = Script().attest(R1, B, 2).certify(R1)
    with pytest.raises(HistoryIncomplete, match="lookback"):
        certification_credits(s.events, first=100)


def test_a_signer_who_left_before_the_threshold_is_not_credited():
    s = Script().attest(R1, A, 1).left(A).attest(R1, B, 1).attest(R1, C, 2).certify(R1)
    assert certification_credits(s.events, first=100)[0] == {acct(B): 1, acct(C): 1}


def test_a_signer_who_left_and_rejoined_before_the_next_attest_stays_credited():
    s = Script().attest(R1, A, 1).left(A).joined(A).attest(R1, B, 2).certify(R1)
    assert certification_credits(s.events, first=100)[0] == {acct(A): 1, acct(B): 1}


def test_a_signer_ejected_or_dropped_by_root_is_narrowed_out():
    ejected = Script().attest(R1, A, 1).ejected(A).attest(R1, B, 1).attest(R1, C, 2).certify(R1)
    assert certification_credits(ejected.events, first=100)[0] == {acct(B): 1, acct(C): 1}
    replaced = Script().attest(R1, A, 1).committee(B, C).attest(R1, B, 1).attest(R1, C, 2).certify(R1)
    assert certification_credits(replaced.events, first=100)[0] == {acct(B): 1, acct(C): 1}


def test_a_replay_that_disagrees_with_the_chains_count_is_refused():
    """A departure the events did not show would leave one signer too many."""
    s = Script().attest(R1, A, 1).attest(R1, B, 1).attest(R1, C, 2).certify(R1)
    with pytest.raises(HistoryIncomplete):
        certification_credits(s.events, first=100)


def test_history_missing_only_for_a_certification_before_the_period_does_not_stop_it():
    s = Script().attest(R1, B, 2).certify(R1)
    s.next_block(200).attest(R2, A, 1).attest(R2, C, 2).certify(R2)
    assert certification_credits(s.events, first=150) == ({acct(A): 1, acct(C): 1}, 1)


def test_a_receipt_whose_unread_signers_left_before_its_threshold_is_credited_exactly():
    """The replayed set never holds a signer the chain's does not, so once the
    counts agree again the sets are equal."""
    s = Script().attest(R1, B, 2, threshold=3).left(A).next_block()
    s.attest(R1, C, 2, threshold=3).attest(R1, D, 3, threshold=3).certify(R1)
    assert certification_credits(s.events, first=100)[0] == {acct(B): 1, acct(C): 1, acct(D): 1}


# ── config ───────────────────────────────────────────────────────────────

FLOAT_KEYS = ["aa" * 28, "bb" * 28]
CMATRA = {"policy": "cc" * 28, "asset": "634d41545241"}


def config_doc(**over) -> dict:
    doc = {
        "version": 1,
        "network": "preprod",
        "chain": GENESIS[2:],
        "period": 202611,
        "blocks": {"first": 100, "last": 300, "lookback": 50},
        "rates": {
            "usd_micros_per_credit": 20_000,
            "usd_micros_per_cmatra": 2_000,
            "price_source": "fixture price",
            "cap_per_attestor": 40_000_000,
            "cap_total": 100_000_000,
        },
        "excluded": [D],
        "cmatra": CMATRA,
        "float_keys": FLOAT_KEYS,
    }
    for key, value in over.items():
        if key in doc["rates"]:
            doc["rates"][key] = value
        elif key in doc["blocks"]:
            doc["blocks"][key] = value
        else:
            doc[key] = value
    return doc


def write_config(tmp_path, doc) -> str:
    path = tmp_path / "period.json"
    path.write_text(json.dumps(doc))
    return str(path)


@pytest.fixture
def config(tmp_path):
    return load_period_config(write_config(tmp_path, config_doc()))


def test_the_period_config_loads(config):
    assert config.network == "preprod"
    assert config.chain == GENESIS[2:]
    assert (config.period, config.first_block, config.last_block, config.scan_from) == (202611, 100, 300, 50)
    assert config.excluded == frozenset({acct(D)})
    assert config.float_keys == (bytes.fromhex(FLOAT_KEYS[0]), bytes.fromhex(FLOAT_KEYS[1]))


@pytest.mark.parametrize(
    "over, match",
    [
        ({"version": 2}, "version"),
        ({"network": "testnet"}, "network"),
        ({"chain": "0x" + "ab" * 32}, "chain"),
        ({"period": 202613}, "period"),
        ({"period": 202611.0}, "period"),
        ({"first": 301}, "blocks"),
        ({"lookback": -1}, "blocks"),
        ({"lookback": 101}, "blocks"),
        ({"first": True}, "blocks"),
        ({"usd_micros_per_credit": 0}, "usd_micros_per_credit"),
        ({"usd_micros_per_cmatra": 0}, "usd_micros_per_cmatra"),
        ({"price_source": " "}, "price_source"),
        ({"cap_per_attestor": 100_000_001}, "cap_per_attestor"),
        ({"cap_total": ATTESTOR_EMISSIONS_BASE + 1, "cap_per_attestor": 1}, "cap_total"),
        ({"excluded": [D, D]}, "excluded"),
        ({"excluded": ["0x12"]}, "excluded"),
        ({"float_keys": ["aa" * 28, "aa" * 28]}, "float_keys"),
        ({"float_keys": ["aa" * 28]}, "float_keys"),
        ({"cmatra": {"policy": "cc" * 28}}, "cmatra"),
        ({"extra": 1}, "keys"),
    ],
)
def test_a_period_config_of_another_shape_is_refused(tmp_path, over, match):
    with pytest.raises(ConfigError, match=match):
        load_period_config(write_config(tmp_path, config_doc(**over)))


# ── statement ────────────────────────────────────────────────────────────

def address(n: int) -> Address:
    return Address(payment_part=VerificationKeyHash(bytes([n]) * 28), network=Network.TESTNET)


REGISTRY = {acct(A): address(1), acct(B): address(2), acct(C): address(3)}


def slice_of(events, scan_from=50, first=100, last=300, genesis=GENESIS, first_hash="0x" + "f1" * 32) -> ChainSlice:
    return ChainSlice(genesis, scan_from, first, last, first_hash, "0x" + "f2" * 32, tuple(events))


def busy_period() -> Script:
    """A: 3 credits, B: 2, C: 1, D (an FPS seat): 2."""
    s = Script().next_block(100)
    s.attest(R1, A, 1).attest(R1, B, 2).certify(R1)
    s.next_block().attest(R2, A, 1).attest(R2, D, 2).certify(R2)
    s.next_block().attest(R3, A, 1).attest(R3, B, 2).certify(R3)
    r4 = "0x" + "04" * 32
    s.next_block().attest(r4, C, 1).attest(r4, D, 2).certify(r4)
    return s


def test_the_statement_prices_each_credit_in_dollars_and_excludes_fps_seats(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    # 20,000 micro-USD a credit at 2,000 micro-USD a cMATRA: 10 cMATRA a credit
    assert [(line["attestor"], line["credits"], line["amount"]) for line in statement["lines"]] == [
        (A, 3, 30_000_000), (B, 2, 20_000_000), (C, 1, 10_000_000)]
    assert statement["excluded"] == [{"attestor": D, "credits": 2}]
    assert statement["total"] == 60_000_000
    assert statement["receipts_certified"] == 4
    assert statement["chain"] == GENESIS[2:]
    assert statement["blocks"] == {
        "scan_from": 50, "first": 100, "first_hash": "0x" + "f1" * 32, "last": 300, "last_hash": "0x" + "f2" * 32}
    assert statement["lines"][0]["address"] == address(1).encode()


def test_the_root_commits_to_each_paid_line(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    leaves = [leaf_hash(202611, acct(line["attestor"]), bytes(address(n)), line["amount"])
              for n, line in zip((1, 2, 3), statement["lines"])]
    assert statement["merkle"] == {"root": merkle_root(leaves).hex(), "leaves": 3}


def test_an_attestor_over_its_cap_is_paid_the_cap(tmp_path):
    config = load_period_config(write_config(tmp_path, config_doc(cap_per_attestor=25_000_000)))
    lines = build_statement(config, slice_of(busy_period().events), REGISTRY)["lines"]
    assert [(line["computed"], line["amount"]) for line in lines] == [
        (30_000_000, 25_000_000), (20_000_000, 20_000_000), (10_000_000, 10_000_000)]


def test_a_period_over_its_total_cap_is_scaled_down_pro_rata(tmp_path):
    config = load_period_config(write_config(tmp_path, config_doc(cap_total=40_000_000, cap_per_attestor=40_000_000)))
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    assert [line["amount"] for line in statement["lines"]] == [20_000_000, 13_333_333, 6_666_666]
    assert statement["total"] == 39_999_999 <= 40_000_000


def test_a_line_priced_to_nothing_is_listed_but_not_paid(tmp_path):
    config = load_period_config(write_config(tmp_path, config_doc(usd_micros_per_cmatra=10**12, cap_total=10**8)))
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    assert [line["amount"] for line in statement["lines"]] == [0, 0, 0]
    assert statement["merkle"] == {"root": "00" * 32, "leaves": 0}


def test_a_credited_attestor_without_a_registered_address_is_refused(config):
    with pytest.raises(StatementError, match="registered"):
        build_statement(config, slice_of(busy_period().events), {acct(A): address(1), acct(B): address(2)})


def test_a_slice_of_another_chain_or_range_is_refused(config):
    events = busy_period().events
    with pytest.raises(StatementError, match="genesis"):
        build_statement(config, slice_of(events, genesis="0x" + "99" * 32), REGISTRY)
    with pytest.raises(StatementError, match="range"):
        build_statement(config, slice_of(events, first=101), REGISTRY)


def test_the_statement_names_the_config_it_was_priced_with(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    assert statement["rates"] == config_doc()["rates"]
    assert statement["cmatra"] == CMATRA
    assert statement["period"] == 202611 and statement["network"] == "preprod"


# ── checker ──────────────────────────────────────────────────────────────

def test_the_checker_accepts_the_same_statement_from_a_second_source(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    check_statement(copy.deepcopy(statement), config, slice_of(busy_period().events), REGISTRY)


def test_the_checker_refuses_a_statement_edited_after_it_was_built(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    statement["lines"][1]["amount"] += 1
    with pytest.raises(StatementMismatch, match=r"lines\[1\]\.amount"):
        check_statement(statement, config, slice_of(busy_period().events), REGISTRY)


def test_the_checker_refuses_when_its_source_saw_other_events(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    other = busy_period()
    other.next_block().attest("0x" + "05" * 32, C, 1).attest("0x" + "05" * 32, B, 2).certify("0x" + "05" * 32)
    with pytest.raises(StatementMismatch):
        check_statement(statement, config, slice_of(other.events), REGISTRY)


def test_the_checker_refuses_when_its_source_is_on_another_fork(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    with pytest.raises(StatementMismatch, match="first_hash"):
        check_statement(statement, config, slice_of(busy_period().events, first_hash="0x" + "f3" * 32), REGISTRY)


def test_the_checker_refuses_a_statement_with_a_field_it_would_not_write(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    statement["note"] = "paid early"
    with pytest.raises(StatementMismatch, match="note"):
        check_statement(statement, config, slice_of(busy_period().events), REGISTRY)


# ── anchor record ────────────────────────────────────────────────────────

def test_the_anchor_record_carries_the_root_and_the_statement_digest(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    record = anchor_metadata(statement)
    assert record == {
        "p": "materios-payout",
        "v": 1,
        "chain": GENESIS[2:],
        "period": 202611,
        "blocks": [100, 300],
        "leaves": 3,
        "total": 60_000_000,
        "root": statement["merkle"]["root"],
        "statement": statement_digest(statement),
    }
    assert all(len(v.encode()) <= 64 for v in record.values() if isinstance(v, str))


def test_the_digest_is_of_the_canonical_statement(config):
    statement = build_statement(config, slice_of(busy_period().events), REGISTRY)
    reordered = json.loads(json.dumps(statement, sort_keys=False))
    reordered = dict(reversed(list(reordered.items())))
    assert statement_digest(reordered) == statement_digest(statement)
    reordered["total"] += 1
    assert statement_digest(reordered) != statement_digest(statement)


# ── recorded preprod ─────────────────────────────────────────────────────

PREPROD_SIGNERS = {
    "0x925fe8605fe32a53a7b391498fc1b0ab91d3af7319607bd70b850b4f5fa9d255": 3,
    "0x8ed446c7114fbeb751866e6752dedf36fba9b3d2832a9fc50a005e00ed0ab124": 2,
    "0xb22194bb42236493366d23ee251fae8d73969d79c380e5c7439921d764e41830": 2,
    "0x44f3bafbc393f24fcfabbf57d4ca73a6a6b5df358cdaa9480a517a97f189964b": 2,
    "0x961e7b9152c5b910d61ff0397d7f023f6e28ca489fbb43135a7cd59cbbdebd36": 2,
    "0x64964344fff93f562d94488c5fc340408817de10c4a4783e5e0dde6c8a4ba53e": 1,
}


def preprod_chain(scan_from: int, first: int = 2753509) -> ChainSlice:
    with responses.RequestsMock() as mock:
        mock.add_callback(responses.POST, URL, callback=Node(recorded()), content_type="application/json")
        return read_chain(URL, scan_from, first, 2753537)


def test_recorded_preprod_blocks_credit_each_signer_of_the_four_certifications():
    credits, certified = certification_credits(preprod_chain(2753505).events, first=2753509)
    assert certified == 4
    assert credits == {acct(k): v for k, v in PREPROD_SIGNERS.items()}


def test_recorded_preprod_blocks_read_from_too_late_for_a_receipt_before_the_period_still_build():
    credits, certified = certification_credits(preprod_chain(2753511, 2753512).events, first=2753512)
    assert certified == 2
    assert sum(credits.values()) == 6


def test_recorded_preprod_blocks_read_from_too_late_are_refused():
    """Receipt 0x08fd…'s first attest is in block 2753510, its second in 2753511."""
    with pytest.raises(HistoryIncomplete, match="0x08fd"):
        certification_credits(preprod_chain(2753511, 2753511).events, first=2753511)
