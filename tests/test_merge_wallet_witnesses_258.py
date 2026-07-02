"""``_merge_wallet_witnesses`` under Conway tag-258 set framing (task #485).

CIP-30 wallets return their partial witness set with the vkey list framed
either bare (``{0: [[pk, sig]]}``) or as a tag-258 set (CSL-13-era software
wallets, all HW stacks) — and the stashed original can be either too. All
four quadrants must merge to the full witness set with the body bytes
verbatim; the user's witness must NEVER be dropped (a dropped witness is a
silent InvalidWitnessesUTXOW at submit).

RED baselines on the pre-fix code (C-extension cbor2 decodes tag 258 to a
set of tuples, which the merge loop silently skips):
    bare original   x tagged wallet -> user witness DROPPED  (2 vkeys)
    tagged original x bare wallet   -> admin witnesses DROPPED (1 vkey)
    tagged original x tagged wallet -> everything DROPPED    (0 vkeys)
Full-tx wallet returns (HW stacks return the whole signed tx, not a bare
witness set) were rejected outright with ValueError.
"""

from __future__ import annotations

import os
import sys
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

BODY = _pure_dumps({0: _PureTag(258, [[b"\xcd" * 32, 1]]), 2: 170000})
REDEEMERS = {(0, 0): [_PureTag(121, []), [2000000, 900000000]]}
SCRIPTS = _PureTag(258, [bytes.fromhex("46010000222499")])


def _original_tx(tagged: bool) -> bytes:
    """A stashed admin-signed tx with both admin witnesses, framed bare or
    tag-258 like the build path stashes it (tagged with TAG_SETS_258=on,
    bare with the kill switch off)."""
    vkeys = [[PK_ADMIN1, SIG_ADMIN1], [PK_ADMIN2, SIG_ADMIN2]]
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
    return pairs, tagged, map_entry_bytes(ws_bytes)


@pytest.mark.parametrize(
    "orig_tagged,wallet_tagged",
    [(False, False), (False, True), (True, False), (True, True)],
    ids=["bare_x_bare", "bare_x_tagged", "tagged_x_bare", "tagged_x_tagged"],
)
def test_merge_quadrants_keep_all_three_witnesses(orig_tagged, wallet_tagged):
    original = _original_tx(orig_tagged)
    merged = api._merge_wallet_witnesses(original, _wallet_ws(wallet_tagged))
    pairs, merged_tagged, ws_entries = _merged_vkeys(merged)
    assert [bytes(p[0]) for p in pairs] == [PK_ADMIN1, PK_ADMIN2, PK_USER]
    assert [bytes(p[1]) for p in pairs] == [SIG_ADMIN1, SIG_ADMIN2, SIG_USER]
    # tag preserved if either side used it
    assert merged_tagged == (orig_tagged or wallet_tagged)
    # untouched witness fields ride through byte-identical
    orig_entries = map_entry_bytes(split_tx(original)[1])
    assert ws_entries[5] == orig_entries[5]
    assert ws_entries[7] == orig_entries[7]


def test_merge_dedups_wallet_reemitted_admin_keys():
    original = _original_tx(True)
    wallet = _wallet_ws(
        True, vkeys=[[PK_ADMIN1, SIG_ADMIN1], [PK_ADMIN2, SIG_ADMIN2], [PK_USER, SIG_USER]]
    )
    pairs, _, _ = _merged_vkeys(api._merge_wallet_witnesses(original, wallet))
    assert [bytes(p[0]) for p in pairs] == [PK_ADMIN1, PK_ADMIN2, PK_USER]


def test_merge_accepts_tuple_vkey_elements():
    original = _original_tx(True)
    wallet = _pure_dumps({0: _PureTag(258, [(PK_USER, SIG_USER)])})
    pairs, _, _ = _merged_vkeys(api._merge_wallet_witnesses(original, wallet))
    assert bytes(pairs[-1][0]) == PK_USER


@pytest.mark.parametrize("wallet_tagged", [False, True], ids=["bare", "tagged"])
def test_merge_accepts_full_tx_wallet_return(wallet_tagged):
    # HW stacks return the ENTIRE signed tx [body, witness_set, is_valid, aux]
    # — element [1] is the witness set, admins typically re-emitted
    original = _original_tx(True)
    wallet_vkeys = [[PK_ADMIN1, SIG_ADMIN1], [PK_USER, SIG_USER]]
    full_tx = (
        b"\x84"
        + BODY
        + _wallet_ws(wallet_tagged, vkeys=wallet_vkeys)
        + b"\xf5\xf6"
    )
    pairs, _, _ = _merged_vkeys(api._merge_wallet_witnesses(original, full_tx))
    assert [bytes(p[0]) for p in pairs] == [PK_ADMIN1, PK_ADMIN2, PK_USER]


def test_merge_rejects_garbage_payloads():
    with pytest.raises(ValueError):
        api._merge_wallet_witnesses(_original_tx(True), _pure_dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        api._merge_wallet_witnesses(
            b"\x83" + _original_tx(True)[1:], _wallet_ws(False)
        )
