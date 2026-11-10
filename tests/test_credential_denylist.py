"""tools.credential_denylist: the committed list, a loader that refuses every
other shape, and the address check both signers run.

Every malformed list is also run against the JSON Schema published beside the
list, so a consumer in another language that validates with the schema refuses
what this loader refuses, except what a schema cannot state (a key repeated
in one object, a version written as a fraction, which JSON Schema reads as
the integer, and a hash repeated across entries); those are listed separately
and must be refused by the loader alone."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from pycardano import Address

from tools.credential_denylist import (
    DenyList,
    DenyListError,
    PointerAddress,
    address_credentials,
    load_denylist,
)

ROOT = Path(__file__).resolve().parent.parent
COMMITTED = ROOT / "audit_pack/2026-11-01/denylist.json"
SCHEMA = json.loads((ROOT / "audit_pack/2026-11-01/denylist.schema.json").read_text())

W1 = "addr1q9jzvg6qqefx4c8eqsez4fnsxfdmcjtz4ku9f0fgju6ymc7ehhk8tdd2j0fqtn7975krepy8a7l8cepk5gyzyq7g6l6sktytnt"
W1_PAYMENT = bytes.fromhex("6426234006526ae0f904322aa670325bbc4962adb854bd2897344de3")
W2_STAKE = bytes.fromhex("c8b3e05f9520c162b260e2e545eb84adfc2dd55b2e2ac6d41e70c4b0")
LC_W1 = bytes.fromhex("59f1583d139063526db7acece757b74736dc8dd26b55cacf32202471")
RESCUE = "addr1q8mntzptc95w3sl64ma8gvs3s8nfhzxq33fad9xdlrpnvtmqk59w4x9kg2yve42zkek8n466rl4mjjm4t40hljcmlxfqkpl8zu"
CLEAN_KEY = b"\x0c" * 28


def _address(header: int, *parts: bytes) -> bytes:
    return bytes([header]) + b"".join(parts)


def _list(**changes) -> dict:
    doc = {
        "version": 1,
        "credentials": [{"hash": "ab" * 28, "label": "a key", "evidence": "a drain"}],
        "allow": [{"hash": "cd" * 28, "label": "custody", "reason": "receives rescues"}],
    }
    doc.update(changes)
    return doc


def _write(tmp_path: Path, doc) -> Path:
    path = tmp_path / "denylist.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return path


# -- the committed list ------------------------------------------------------------


def test_the_committed_list_loads_and_matches_its_schema():
    denylist = load_denylist(COMMITTED)
    jsonschema.validate(json.loads(COMMITTED.read_text()), SCHEMA)
    assert denylist.sha256 == hashlib.sha256(COMMITTED.read_bytes()).hexdigest()
    assert len(denylist.labels) == 834
    assert len(denylist.allowed) == 2


def test_the_committed_list_denies_the_attacker_and_the_drained_wallets():
    denylist = load_denylist(COMMITTED)
    for address in (
        W1,
        "addr1q9s6m9d8yedfcf53yhq5j5zsg0s58wpzamwexrxpfelgz2wgk0s9l9fqc93tyc8zu4z7hp9dlska2kew9trdg8nscjcq3sk5s3",
        "addr1qxqf5syeture53vzrjgh2j3gwknylp62famesvv9hf5manv7jwqtqyr950fw8anzxavs6c8l6jxg033jquchc8dxns7qm366em",
        "addr1qx53hw2d3xhh3eqt6skfnqw0r7cjvm09yw3f6exvrlk4j88pjuvalvn7yf4ec5msyjgan7ur5hjs7d7x59jnfmjchngs5sp0tr",
        "addr1z9vlzkpazwgxx5ndk7kwee6hkarndhyd6f44tjk0xgszgu275jq4yvpskgayj55xegdp30g5rfynax66r8vgn9fldnds3rhruq",
    ):
        assert denylist.denied(Address.from_primitive(address).to_primitive()), address


_EXPOSED = json.loads((ROOT / "tests/fixtures/denylist_exposed_since_v1.json").read_text())
EXPOSED_SINCE_V1 = _EXPOSED["addresses"]
EXPOSED_KEYS_WITHOUT_AN_ADDRESS = _EXPOSED["credentials"]
"""Exposed key hashes whose addresses are not known: a .vkey inventory and the claims verifier's deny set."""


def test_the_fixture_names_every_key_exposed_since_the_first_list():
    labels = [entry["label"] for entries in EXPOSED_SINCE_V1.values() for entry in entries]
    assert len(EXPOSED_SINCE_V1) == 204
    assert sum(label.startswith("exposed bot wallet ") for label in labels) == 384
    assert len({label.split(",")[0] for label in labels if label.startswith("exposed bot wallet ")}) == 192


@pytest.mark.parametrize("address", sorted(EXPOSED_SINCE_V1))
def test_the_committed_list_denies_each_key_exposed_since_the_first_list(address):
    """Every credential of each address is listed under its label, so neither
    part of a base address is left for a fresh pairing to reach."""
    denylist = load_denylist(COMMITTED)
    expected = EXPOSED_SINCE_V1[address]
    credentials = address_credentials(Address.from_primitive(address).to_primitive())
    assert [credential.hex() for credential in credentials] == [entry["hash"] for entry in expected]
    for credential, entry in zip(credentials, expected):
        assert denylist.labels.get(credential) == entry["label"]


@pytest.mark.parametrize("credential", sorted(EXPOSED_KEYS_WITHOUT_AN_ADDRESS))
def test_the_committed_list_denies_each_exposed_key_known_only_by_its_hash(credential):
    denylist = load_denylist(COMMITTED)
    assert denylist.labels.get(bytes.fromhex(credential)) == EXPOSED_KEYS_WITHOUT_AN_ADDRESS[credential]


def _sweep_param(name: str) -> str:
    line = next(l for l in (ROOT / "scripts/sweep_params.env").read_text().splitlines() if l.startswith(name + "="))
    return line.split("=", 1)[1].split("#")[0].strip().strip('"')


def test_the_committed_list_never_denies_the_merger_itself():
    """The pool, the quarantine and both admins sit in every surrender; a list
    that names one of them refuses every claim."""
    labels = load_denylist(COMMITTED).labels
    for address in (_sweep_param("SURRENDER_POOL_ADDR"), "addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl"):
        assert not set(address_credentials(Address.from_primitive(address).to_primitive())) & labels.keys(), address
    for admin in ("ADMIN_PKH_1", "ADMIN_PKH_2"):
        assert bytes.fromhex(_sweep_param(admin)) not in labels, admin


def test_the_committed_list_lets_the_rescue_wallet_through():
    denylist = load_denylist(COMMITTED)
    rescue = Address.from_primitive(RESCUE).to_primitive()
    assert denylist.denied(rescue) is None
    assert set(address_credentials(rescue)) == denylist.allowed


# -- address credentials -----------------------------------------------------------


def test_a_base_address_names_both_credentials():
    assert address_credentials(Address.from_primitive(W1).to_primitive()) == (
        W1_PAYMENT, bytes.fromhex("d9bdec75b5aa93d205cfc5f52c3c8487efbe7c6436a2082203c8d7f5"))


@pytest.mark.parametrize("header", [0x01, 0x11, 0x21, 0x31])
def test_every_base_address_kind_names_payment_and_stake(header):
    assert address_credentials(_address(header, b"\x01" * 28, b"\x02" * 28)) == (b"\x01" * 28, b"\x02" * 28)


@pytest.mark.parametrize("header", [0x61, 0x71])
def test_an_enterprise_address_names_its_payment_credential(header):
    assert address_credentials(_address(header, b"\x01" * 28)) == (b"\x01" * 28,)


@pytest.mark.parametrize("header", [0x41, 0x51])
def test_a_pointer_address_cannot_be_checked(header):
    with pytest.raises(PointerAddress):
        address_credentials(_address(header, b"\x01" * 28, b"\x81\x00\x01"))


UNREADABLE = {
    "empty": b"",
    "base_without_its_stake_part": _address(0x01, b"\x01" * 28),
    "base_one_byte_long": _address(0x01, b"\x01" * 28, b"\x02" * 29),
    "enterprise_one_byte_short": _address(0x61, b"\x01" * 27),
    "enterprise_one_byte_long": _address(0x61, b"\x01" * 29),
    "byron": bytes.fromhex("82d818582183581c") + b"\x01" * 28 + b"\xa0\x00\x1a\x01\x02\x03\x04",
    "reward": _address(0xE1, b"\x01" * 28),
    "header_8": _address(0x81, b"\x01" * 28),
}


@pytest.mark.parametrize("name", UNREADABLE)
def test_an_address_without_checkable_credentials_is_refused(name):
    with pytest.raises(ValueError):
        address_credentials(UNREADABLE[name])


DENIALS = {
    "enterprise_listed_key": (_address(0x61, W1_PAYMENT), True),
    "base_listed_payment_key": (_address(0x01, W1_PAYMENT, CLEAN_KEY), True),
    "base_listed_stake_key": (_address(0x01, CLEAN_KEY, W2_STAKE), True),
    "base_listed_script_as_stake": (_address(0x21, CLEAN_KEY, LC_W1), True),
    "enterprise_listed_script": (_address(0x71, LC_W1), True),
    "base_unlisted": (_address(0x01, CLEAN_KEY, CLEAN_KEY), False),
    "enterprise_unlisted": (_address(0x61, CLEAN_KEY), False),
}


@pytest.mark.parametrize("name", DENIALS)
def test_an_address_is_denied_by_either_of_its_credentials(name):
    address, denied = DENIALS[name]
    assert bool(load_denylist(COMMITTED).denied(address)) is denied


def test_a_denial_names_the_listed_credential():
    assert load_denylist(COMMITTED).denied(_address(0x01, CLEAN_KEY, W2_STAKE)) == "W2 (FPS treasury), stake key"


# -- the loader --------------------------------------------------------------------


def test_a_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_denylist(tmp_path / "absent.json")


def test_a_well_formed_list_loads(tmp_path):
    denylist = load_denylist(_write(tmp_path, _list()))
    assert denylist == DenyList(
        labels={b"\xab" * 28: "a key"}, allowed=frozenset({b"\xcd" * 28}),
        sha256=hashlib.sha256(json.dumps(_list()).encode()).hexdigest(),
    )


def test_an_empty_allow_list_loads(tmp_path):
    assert load_denylist(_write(tmp_path, _list(allow=[]))).allowed == frozenset()


def _entry(field: str, value) -> dict:
    doc = _list()
    doc["credentials"][0][field] = value
    return doc


def _without(field: str) -> dict:
    doc = _list()
    del doc["credentials"][0][field]
    return doc


def _allowance(field: str, value) -> dict:
    doc = _list()
    doc["allow"][0][field] = value
    return doc


def _also_denied() -> dict:
    doc = _list()
    doc["credentials"].append({"hash": "cd" * 28, "label": "custody", "evidence": "x"})
    return doc


# Shapes both the loader and the schema refuse.
MALFORMED = {
    "not_an_object": [],
    "version_missing": {k: v for k, v in _list().items() if k != "version"},
    "version_two": _list(version=2),
    "version_true": _list(version=True),
    "version_a_string": _list(version="1"),
    "unknown_top_level_key": _list(units=[]),
    "credentials_missing": {k: v for k, v in _list().items() if k != "credentials"},
    "credentials_empty": _list(credentials=[]),
    "credentials_not_a_list": _list(credentials={}),
    "allow_missing": {k: v for k, v in _list().items() if k != "allow"},
    "allow_not_a_list": _list(allow={}),
    "entry_not_an_object": _list(credentials=["ab" * 28]),
    "hash_uppercase": _entry("hash", "AB" * 28),
    "hash_one_byte_short": _entry("hash", "ab" * 27),
    "hash_one_byte_long": _entry("hash", "ab" * 29),
    "hash_with_a_newline": _entry("hash", "ab" * 28 + "\n"),
    "hash_not_hex": _entry("hash", "zz" * 28),
    "hash_a_number": _entry("hash", 1),
    "hash_missing": _without("hash"),
    "label_empty": _entry("label", ""),
    "label_blank": _entry("label", "   "),
    "label_missing": _without("label"),
    "evidence_empty": _entry("evidence", ""),
    "evidence_not_text": _entry("evidence", ["ab" * 32]),
    "evidence_missing": _without("evidence"),
    "unknown_entry_key": _entry("taint_from_slot", 1),
    "allowance_reason_empty": _allowance("reason", ""),
    "allowance_hash_malformed": _allowance("hash", "cd" * 27),
    "allowance_with_evidence": _allowance("evidence", "x"),
}

# Shapes a JSON Schema cannot state; the loader alone refuses them.
LOADER_ONLY = {
    "a_repeated_json_key": '{"version": 1, "version": 1, "credentials": [{"hash": "' + "ab" * 28
                           + '", "label": "a", "evidence": "b"}], "allow": []}',
    "a_hash_listed_twice": _list(credentials=_list()["credentials"] * 2),
    "an_allowance_listed_twice": _list(allow=_list()["allow"] * 2),
    "a_credential_both_denied_and_allowed": _also_denied(),
    "a_version_written_as_a_fraction": '{"version": 1.0, "credentials": [{"hash": "' + "ab" * 28
                                         + '", "label": "a", "evidence": "b"}], "allow": []}',
    "not_json": "{",
    "nan": '{"version": NaN, "credentials": [], "allow": []}',
}


@pytest.mark.parametrize("name", MALFORMED)
def test_the_loader_refuses_a_malformed_list(tmp_path, name):
    with pytest.raises(DenyListError):
        load_denylist(_write(tmp_path, copy.deepcopy(MALFORMED[name])))


@pytest.mark.parametrize("name", MALFORMED)
def test_the_schema_refuses_a_malformed_list(name):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(MALFORMED[name], SCHEMA)


@pytest.mark.parametrize("name", LOADER_ONLY)
def test_the_loader_refuses_what_a_schema_cannot_state(tmp_path, name):
    with pytest.raises(DenyListError):
        load_denylist(_write(tmp_path, LOADER_ONLY[name]))


def test_the_schema_is_a_valid_draft_2020_12_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
