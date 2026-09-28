"""Witness merging at /submit-surrender under Conway tag-258 set framing.

The build returns an unsigned transaction whose body names both admins as
required signers. CIP-30 wallets return their partial witness set with the
vkey list framed either bare (``{0: [[pk, sig]]}``) or as a tag-258 set
(CSL-13-era software wallets, all HW stacks), and HW stacks may return the
whole signed transaction instead. ``_merge_wallet_witnesses`` takes only
well-formed [32-byte key, 64-byte signature] pairs from the wallet and none
under a required signer's key: the admins' witnesses are added afterwards by
``_merge_vkey_witnesses``, and replace anything already there under their
keys. The body bytes are kept verbatim throughout and the user's witness is
never dropped (a dropped witness is a silent InvalidWitnessesUTXOW at submit).
"""

from __future__ import annotations

import hashlib
import os
import sys
import tracemalloc
from pathlib import Path

import cbor2._decoder
import cbor2._encoder
import cbor2._types
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("NETWORK", "mainnet")
os.environ.setdefault("SURRENDER_API_SECRET", "test-secret")

import services.surrender_api as api  # noqa: E402
from tests.test_tag_258_transform import map_entry_bytes, split_tx  # noqa: E402

_pure_dumps = cbor2._encoder.dumps
_pure_loads = cbor2._decoder.loads
_PureTag = cbor2._types.CBORTag

PK_ADMIN1, SIG_ADMIN1 = b"\x01" * 32, b"\xa1" * 64
PK_ADMIN2, SIG_ADMIN2 = b"\x02" * 32, b"\xa2" * 64
PK_USER, SIG_USER = b"\x03" * 32, b"\xa3" * 64


def _pkh(pk: bytes) -> bytes:
    return hashlib.blake2b(pk, digest_size=28).digest()


BODY = _pure_dumps({
    0: _PureTag(258, [[b"\xcd" * 32, 1]]),
    2: 170000,
    14: _PureTag(258, [_pkh(PK_ADMIN1), _pkh(PK_ADMIN2)]),
})
REDEEMERS = {(0, 0): [_PureTag(121, []), [2000000, 900000000]]}
SCRIPTS = _PureTag(258, [bytes.fromhex("46010000222499")])
UNSIGNED = b"\x84" + BODY + _pure_dumps({5: REDEEMERS, 7: SCRIPTS}) + b"\xf5\xf6"


def _signed_by_user(tagged: bool) -> bytes:
    """The build with the claimant's witness merged in, framed bare or tag-258."""
    vkeys = [[PK_USER, SIG_USER]]
    ws = {0: _PureTag(258, vkeys) if tagged else vkeys, 5: REDEEMERS, 7: SCRIPTS}
    return b"\x84" + BODY + _pure_dumps(ws) + b"\xf5\xf6"


def _wallet_ws(tagged: bool, vkeys=None) -> bytes:
    vkeys = [[PK_USER, SIG_USER]] if vkeys is None else vkeys
    return _pure_dumps({0: _PureTag(258, vkeys) if tagged else vkeys})


def _merged_vkeys(merged: bytes) -> tuple[list, bool, dict]:
    """(vkey pairs, was_tagged, ws map-entry bytes) from a merged tx."""
    body_bytes, ws_bytes, tail = split_tx(merged)
    assert body_bytes == BODY, "body bytes not preserved verbatim"
    assert tail == b"\xf5\xf6", "tail bytes not preserved"
    ws = _pure_loads(ws_bytes)
    value = ws[0]
    tagged = getattr(value, "tag", None) == 258
    pairs = list(value.value) if tagged else list(value)
    return [[bytes(pk), bytes(sig)] for pk, sig in pairs], tagged, map_entry_bytes(ws_bytes)


def _assert_other_fields_verbatim(ws_entries: dict, original: bytes) -> None:
    orig_entries = map_entry_bytes(split_tx(original)[1])
    assert ws_entries[5] == orig_entries[5]
    assert ws_entries[7] == orig_entries[7]


@pytest.mark.parametrize("wallet_tagged", [False, True], ids=["bare", "tagged"])
def test_wallet_witnesses_merge_into_the_unsigned_build(wallet_tagged):
    merged = api._merge_wallet_witnesses(UNSIGNED, _wallet_ws(wallet_tagged))
    pairs, merged_tagged, ws_entries = _merged_vkeys(merged)
    assert pairs == [[PK_USER, SIG_USER]]
    assert merged_tagged == wallet_tagged
    _assert_other_fields_verbatim(ws_entries, UNSIGNED)


@pytest.mark.parametrize(
    "user_tagged,admin_tag",
    [(False, None), (False, 258), (True, None), (True, 258)],
    ids=["bare_x_bare", "bare_x_tagged", "tagged_x_bare", "tagged_x_tagged"],
)
def test_admin_witnesses_join_the_claimants(user_tagged, admin_tag):
    original = _signed_by_user(user_tagged)
    merged = api._merge_vkey_witnesses(
        original, [[PK_ADMIN2, SIG_ADMIN2], [PK_ADMIN1, SIG_ADMIN1]], admin_tag,
    )
    pairs, merged_tagged, ws_entries = _merged_vkeys(merged)
    assert pairs == [[PK_USER, SIG_USER], [PK_ADMIN2, SIG_ADMIN2], [PK_ADMIN1, SIG_ADMIN1]]
    assert merged_tagged == (user_tagged or admin_tag == 258)
    _assert_other_fields_verbatim(ws_entries, original)


def test_an_added_witness_replaces_one_under_the_same_key():
    ws = {0: _PureTag(258, [[PK_ADMIN2, bytes(64)], [PK_USER, SIG_USER]]), 5: REDEEMERS, 7: SCRIPTS}
    original = b"\x84" + BODY + _pure_dumps(ws) + b"\xf5\xf6"
    merged = api._merge_vkey_witnesses(original, [[PK_ADMIN2, SIG_ADMIN2]], 258)
    pairs, _, _ = _merged_vkeys(merged)
    assert pairs == [[PK_USER, SIG_USER], [PK_ADMIN2, SIG_ADMIN2]]


@pytest.mark.parametrize("wallet_tagged", [False, True], ids=["bare", "tagged"])
def test_a_wallet_witness_under_a_required_signer_is_dropped(wallet_tagged):
    wallet = _wallet_ws(wallet_tagged, vkeys=[
        [PK_ADMIN1, bytes(64)], [PK_USER, SIG_USER], [PK_ADMIN2, SIG_ADMIN2],
    ])
    pairs, _, _ = _merged_vkeys(api._merge_wallet_witnesses(UNSIGNED, wallet))
    assert pairs == [[PK_USER, SIG_USER]]


def test_a_wallet_witness_repeated_under_one_key_is_kept_once():
    wallet = _wallet_ws(True, vkeys=[[PK_USER, SIG_USER], [PK_USER, b"\xee" * 64]])
    pairs, _, _ = _merged_vkeys(api._merge_wallet_witnesses(UNSIGNED, wallet))
    assert pairs == [[PK_USER, SIG_USER]]


def test_merge_accepts_tuple_vkey_elements():
    wallet = _pure_dumps({0: _PureTag(258, [(PK_USER, SIG_USER)])})
    pairs, _, _ = _merged_vkeys(api._merge_wallet_witnesses(UNSIGNED, wallet))
    assert pairs == [[PK_USER, SIG_USER]]


def test_merge_accepts_indefinite_length_arrays():
    wallet = b"\xa1\x00\x9f\x9f\x58\x20" + PK_USER + b"\x58\x40" + SIG_USER + b"\xff\xff"
    pairs, _, _ = _merged_vkeys(api._merge_wallet_witnesses(UNSIGNED, wallet))
    assert pairs == [[PK_USER, SIG_USER]]


@pytest.mark.parametrize("wallet_tagged", [False, True], ids=["bare", "tagged"])
def test_merge_accepts_full_tx_wallet_return(wallet_tagged):
    # HW stacks may return the ENTIRE signed tx [body, witness_set, is_valid, aux]
    wallet_vkeys = [[PK_ADMIN1, SIG_ADMIN1], [PK_USER, SIG_USER]]
    full_tx = b"\x84" + BODY + _wallet_ws(wallet_tagged, vkeys=wallet_vkeys) + b"\xf5\xf6"
    pairs, _, _ = _merged_vkeys(api._merge_wallet_witnesses(UNSIGNED, full_tx))
    assert pairs == [[PK_USER, SIG_USER]]


def test_merge_rejects_garbage_payloads():
    with pytest.raises(ValueError):
        api._merge_wallet_witnesses(UNSIGNED, _pure_dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        api._merge_wallet_witnesses(b"\x83" + UNSIGNED[1:], _wallet_ws(False))


@pytest.mark.parametrize("vkeys", [
    [[1 << 20, SIG_USER]],
    [[32, SIG_USER]],
    [[PK_USER[:31], SIG_USER]],
    [[PK_USER + b"\x00", SIG_USER]],
    [[PK_USER, SIG_USER[:63]]],
    [[PK_USER, SIG_USER + b"\x00"]],
    [[PK_USER, 7]],
    [[PK_USER, SIG_USER, b""]],
    [[PK_USER]],
    [PK_USER],
    [{0: PK_USER}],
    [[PK_USER, SIG_USER], [1 << 20, SIG_USER]],
    _PureTag(259, [[PK_USER, SIG_USER]]),
    _PureTag(258, {0: [PK_USER, SIG_USER]}),
    {0: [PK_USER, SIG_USER]},
    1 << 20,
], ids=[
    "integer_key", "integer_key_length", "short_key", "long_key", "short_signature", "long_signature",
    "integer_signature", "three_items", "one_item", "bare_bytes", "map_entry",
    "one_bad_of_two", "other_tag", "tagged_map", "map", "integer",
])
def test_merge_refuses_a_malformed_vkey_witness_before_reading_it(vkeys):
    with pytest.raises(ValueError):
        api._merge_wallet_witnesses(UNSIGNED, _pure_dumps({0: vkeys}))


def test_refusing_an_integer_key_allocates_nothing_it_sizes():
    wallet = _pure_dumps({0: [[1 << 26, SIG_USER]]})
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            api._merge_wallet_witnesses(UNSIGNED, wallet)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1 << 20
