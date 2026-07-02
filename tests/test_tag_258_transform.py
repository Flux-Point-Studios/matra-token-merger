"""Uniform tag-258 set framing for HW-wallet compatibility (task #485).

The pre-fix build path emits a MIXED-set-encoded tx: bare inputs (body[0]) and
bare vkey witnesses (ws[0]) next to tag-258 collateral (body[13]), required
signers (body[14]) and plutus scripts (ws[7]). The CIP-21 canonicalizer every
hardware-wallet stack runs (cardano-hw-interop-lib) either rejects that tx
(TX_INCONSISTENT_SET_TAGS) or re-frames it — so the device signs a different
blake2b-256 body hash than admin_1/admin_2 did and the node rejects
InvalidWitnessesUTXOW.

The fix re-frames every present set-typed field as tag-258 BEFORE the first
signature, using cbor2's pure-Python implementation exclusively: the C
extension decodes tag 258 to an unordered Python set and re-orders set
contents by element hash at dumps() time, so it can neither preserve the
sorted input order the SPEND redeemer indexes against nor round-trip the
bytes the signatures cover.

``tests/fixtures/mixed_set_tags_tx.hex`` is a deterministic real pycardano
build (fixed admin key, fixed synthetic UTxOs, offline context) captured from
the pre-fix path — byte-for-byte what the kill switch (TAG_SETS_258=off) must
keep producing. Its shape replicates the live evidence tx: body keys
{0,1,2,3,8,11,13,14,16,17}, ws keys {0,5,7}, exactly 3 tag-258 framings.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import unittest.mock as mock
from pathlib import Path

import cbor2
import cbor2._decoder
import cbor2._encoder
import cbor2._types
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("NETWORK", "mainnet")
os.environ.setdefault("SURRENDER_API_SECRET", "test-secret")

from pycardano import (  # noqa: E402
    Address,
    Network,
    PaymentSigningKey,
    PlutusV3Script,
    plutus_script_hash,
)
from pycardano.hash import VerificationKeyHash  # noqa: E402

import services.surrender_api as api  # noqa: E402
from tests.test_surrender_redeemer_index import (  # noqa: E402
    _CMATRA_ASSET,
    _CMATRA_POLICY,
    _FakeContext,
    _LEGACY_ASSET,
    _LEGACY_POLICY,
    _POOL_HASH,
    _SCRIPT_HEX,
    _user_utxos_for,
)

_pure_loads = cbor2._decoder.loads
_pure_dumps = cbor2._encoder.dumps
_PureTag = cbor2._types.CBORTag

_TAG_258_HEAD = b"\xd9\x01\x02"

FIXTURES = Path(__file__).parent / "fixtures"
MIXED_TX_HEX = (FIXTURES / "mixed_set_tags_tx.hex").read_text().strip()
MIXED_TX = bytes.fromhex(MIXED_TX_HEX)


def _is_tag258(value) -> bool:
    return getattr(value, "tag", None) == 258


def split_tx(tx: bytes) -> tuple[bytes, bytes, bytes]:
    """Slice a serialized tx (array(4)) into exact (body, witness_set, tail)
    byte ranges by walking the CBOR stream — no re-encoding."""
    assert tx[0] == 0x84, "tx header is not array(4)"
    stream = io.BytesIO(tx)
    stream.read(1)
    body_start = stream.tell()
    cbor2._decoder.CBORDecoder(stream).decode()
    body_end = stream.tell()
    cbor2._decoder.CBORDecoder(stream).decode()
    ws_end = stream.tell()
    return tx[body_start:body_end], tx[body_end:ws_end], tx[ws_end:]


def map_entry_bytes(map_cbor: bytes) -> dict:
    """{decoded key: exact value bytes} for a definite-length CBOR map."""
    stream = io.BytesIO(map_cbor)
    dec = cbor2._decoder.CBORDecoder(stream)
    head = stream.read(1)[0]
    assert head >> 5 == 5, "not a CBOR map"
    n = head & 0x1F
    if n == 24:
        n = stream.read(1)[0]
    else:
        assert n < 24, "unexpected map length width"
    entries = {}
    for _ in range(n):
        key = dec.decode()
        value_start = stream.tell()
        dec.decode()
        entries[key] = map_cbor[value_start:stream.tell()]
    return entries


def build_real_surrender_tx(tag_sets_on: bool) -> tuple[str, str]:
    """Drive the REAL ``_build_cosigned_surrender_tx`` deterministically
    (fixed admin key, fixed synthetic UTxOs, offline context) with the
    tag-258 transform forced on/off. Returns (tx_cbor_hex, tx_hash_hex)."""
    script = PlutusV3Script(bytes.fromhex(_SCRIPT_HEX))
    script_addr = Address(plutus_script_hash(script), network=Network.TESTNET)
    user_addr = Address(
        VerificationKeyHash(bytes.fromhex("22" * 28)), network=Network.TESTNET
    )
    sk = PaymentSigningKey.from_primitive(bytes.fromhex("11" * 32))

    api.state.script_cbor_hex = _SCRIPT_HEX
    api.state.admin_sk = sk
    api.state.admin_pkh = sk.to_verification_key().hash()
    api.state.admin_addr = Address(
        sk.to_verification_key().hash(), network=Network.TESTNET
    )
    # Collateral reservations persist on the module singleton; a stale
    # reservation from a prior build in this process would force a different
    # collateral pick and break byte-determinism.
    api.state.reserved_collateral.clear()
    api.state.user_pending.clear()

    class _BF:
        project_id = "x"
        base_url = "https://cardano-mainnet.blockfrost.io/api/v0"

    api.state.bf = _BF()
    ctx = _FakeContext(_user_utxos_for("cc", user_addr))

    with mock.patch.object(api, "SCRIPT_ADDRESS", script_addr.encode()), \
         mock.patch.object(api, "QUARANTINE_ADDRESS", script_addr.encode()), \
         mock.patch.object(api, "CMATRA_POLICY_HEX", _CMATRA_POLICY), \
         mock.patch.object(api, "CMATRA_ASSET_HEX", _CMATRA_ASSET), \
         mock.patch.object(api, "COSIGNER_URL", ""), \
         mock.patch.object(api, "COLLATERAL_UTXO", ""), \
         mock.patch.object(api, "TAG_SETS_258", tag_sets_on), \
         mock.patch.object(api, "BlockFrostChainContext", lambda **kw: ctx):
        pool_utxo = {
            "tx_hash": _POOL_HASH,
            "output_index": 1,
            "cmatra_amount": 1000,
            "ada_amount": 2_000_000,
        }
        legacy = [
            {"policy_hex": _LEGACY_POLICY, "asset_hex": _LEGACY_ASSET, "quantity": 1}
        ]
        tx_cbor_hex, tx_hash_hex, _pool_out, _chg = api._build_cosigned_surrender_tx(
            user_addr.encode(), 100, legacy, pool_utxo
        )
    return tx_cbor_hex, tx_hash_hex


# ---------------------------------------------------------------------------
# Pure-cbor2 helper trio + startup invariant
# ---------------------------------------------------------------------------


class TestPureCborHelpers:
    def test_module_exports_pure_cbor_trio(self):
        assert api._pure_loads is cbor2._decoder.loads
        assert api._pure_dumps is cbor2._encoder.dumps
        assert api._PureCBORTag is cbor2._types.CBORTag

    def test_semantic_decoder_258_absent(self):
        # pycardano pops cbor2's pure tag-258 semantic decoder at import time;
        # every transform/merge path relies on that to see order-preserving
        # CBORTag(258, [...]) values instead of unordered Python sets. The
        # module-level startup assert pins this — if it ever regresses the
        # service must fail at import, not corrupt signatures at runtime.
        assert 258 not in cbor2._decoder.semantic_decoders

    def test_pure_decoder_preserves_tag_and_order(self):
        raw = _pure_dumps(_PureTag(258, [[b"b"], [b"a"]]))
        out = api._pure_loads(raw)
        assert _is_tag258(out)
        assert out.value == [[b"b"], [b"a"]]
        # the C decoder destroys exactly this — tag dropped, order lost
        assert isinstance(cbor2.loads(raw), set)

    def test_c_dumps_cannot_serialize_pure_tag(self):
        with pytest.raises(Exception):
            cbor2.dumps(_PureTag(258, []))


# ---------------------------------------------------------------------------
# Fixture provenance + shape (the RED baseline the transform starts from)
# ---------------------------------------------------------------------------


class TestMixedFixture:
    def test_fixture_shape_is_mixed(self):
        body_bytes, ws_bytes, tail = split_tx(MIXED_TX)
        body = _pure_loads(body_bytes)
        ws = _pure_loads(ws_bytes)
        assert isinstance(body[0], list), "inputs must be BARE in the fixture"
        assert _is_tag258(body[13]) and _is_tag258(body[14])
        assert isinstance(ws[0], list), "vkeys must be BARE in the fixture"
        assert _is_tag258(ws[7])
        assert MIXED_TX.hex().count(_TAG_258_HEAD.hex()) == 3
        assert tail == b"\xf5\xf6"

    def test_fixture_regenerates_byte_identical_with_flag_off(self):
        tx_hex, _ = build_real_surrender_tx(tag_sets_on=False)
        assert tx_hex == MIXED_TX_HEX


# ---------------------------------------------------------------------------
# Body transform
# ---------------------------------------------------------------------------


class TestWrapBodySets:
    def test_wraps_present_set_keys_only(self):
        body_bytes, _, _ = split_tx(MIXED_TX)
        before = map_entry_bytes(body_bytes)
        out = api._wrap_body_sets_258(body_bytes)
        body = _pure_loads(out)
        for k in (0, 13, 14):
            assert _is_tag258(body[k]), f"body[{k}] not tag-258"
        for k in (4, 18, 20):
            assert k not in body, f"absent key {k} must stay absent"
        after = map_entry_bytes(out)
        assert set(after) == set(before)
        for k, vb in before.items():
            if k in (0, 13, 14) and not vb.startswith(_TAG_258_HEAD):
                assert after[k] == _TAG_258_HEAD + vb, f"body[{k}] not prefix-tagged"
            else:
                assert after[k] == vb, f"body[{k}] bytes changed"

    def test_all_six_set_keys_wrapped(self):
        body = {
            0: [[b"\x01" * 32, 0]],
            2: 170000,
            4: [[0, b"\x05" * 28]],
            13: [[b"\x02" * 32, 1]],
            14: [b"\x03" * 28],
            18: [[b"\x04" * 32, 0]],
            20: [[b"\x06" * 32, 0]],
        }
        out = _pure_loads(api._wrap_body_sets_258(_pure_dumps(body)))
        for k in (0, 4, 13, 14, 18, 20):
            assert _is_tag258(out[k]), f"set key {k} not wrapped"
        assert out[2] == 170000

    def test_idempotent(self):
        body_bytes, _, _ = split_tx(MIXED_TX)
        once = api._wrap_body_sets_258(body_bytes)
        assert api._wrap_body_sets_258(once) == once

    def test_input_order_and_spend_index_preserved(self):
        body_bytes, ws_bytes, _ = split_tx(MIXED_TX)
        pre_inputs = [
            (bytes(e[0]), e[1]) for e in _pure_loads(body_bytes)[0]
        ]
        post = _pure_loads(api._wrap_body_sets_258(body_bytes))
        post_inputs = [(bytes(e[0]), e[1]) for e in post[0].value]
        # canonical sort order survives inside the tag, so the SPEND redeemer
        # index computed against the sorted position stays correct
        assert post_inputs == pre_inputs == sorted(pre_inputs)
        ws = _pure_loads(ws_bytes)
        spend_idx = next(k[1] for k in ws[5] if k[0] == 0)
        assert post_inputs[spend_idx] == (bytes.fromhex(_POOL_HASH), 1)

    def test_transform_changes_body_hash(self):
        body_bytes, _, _ = split_tx(MIXED_TX)
        out = api._wrap_body_sets_258(body_bytes)
        assert out != body_bytes
        assert (
            hashlib.blake2b(out, digest_size=32).digest()
            != hashlib.blake2b(body_bytes, digest_size=32).digest()
        )

    def test_rejects_non_map_body(self):
        with pytest.raises(ValueError):
            api._wrap_body_sets_258(_pure_dumps([1, 2, 3]))


# ---------------------------------------------------------------------------
# Witness-set transform
# ---------------------------------------------------------------------------


class TestWrapWitnessVkeys:
    def test_wraps_key0_never_touches_4_5_7(self):
        # script_data_hash (body[11]) commits to the wire bytes of ws[4]/ws[5]
        # — re-framing them means PPViewHashesDontMatch at the node
        ws = {
            0: [[b"\x0a" * 32, b"\x0b" * 64]],
            4: [_PureTag(121, [])],
            5: {(0, 1): [_PureTag(121, []), [2000000, 900000000]]},
            7: _PureTag(258, [bytes.fromhex("46010000222499")]),
        }
        raw = _pure_dumps(ws)
        before = map_entry_bytes(raw)
        out = api._wrap_witness_vkeys_258(raw)
        after = map_entry_bytes(out)
        assert after[0] == _TAG_258_HEAD + before[0]
        for k in (4, 5, 7):
            assert after[k] == before[k], f"ws[{k}] bytes changed"

    def test_fixture_ws_wrap_and_idempotency(self):
        _, ws_bytes, _ = split_tx(MIXED_TX)
        once = api._wrap_witness_vkeys_258(ws_bytes)
        assert api._wrap_witness_vkeys_258(once) == once
        wrapped = _pure_loads(once)
        assert _is_tag258(wrapped[0])
        before = map_entry_bytes(ws_bytes)
        after = map_entry_bytes(once)
        assert after[5] == before[5]
        assert after[7] == before[7]

    def test_rejects_non_map_witness_set(self):
        with pytest.raises(ValueError):
            api._wrap_witness_vkeys_258(_pure_dumps([]))


# ---------------------------------------------------------------------------
# Pure round-trip stability + wire body slicing
# ---------------------------------------------------------------------------


class TestWireInvariants:
    def test_pure_round_trip_stability(self):
        body_bytes, ws_bytes, _ = split_tx(MIXED_TX)
        for blob in (
            body_bytes,
            ws_bytes,
            api._wrap_body_sets_258(body_bytes),
            api._wrap_witness_vkeys_258(ws_bytes),
            MIXED_TX,
        ):
            assert api._pure_dumps(api._pure_loads(blob)) == blob

    def test_tx_body_bytes_slices_exact_body(self):
        body_bytes, _, _ = split_tx(MIXED_TX)
        assert api._tx_body_bytes(MIXED_TX) == body_bytes

    def test_tx_body_bytes_rejects_non_array4(self):
        with pytest.raises(ValueError):
            api._tx_body_bytes(b"\x83" + MIXED_TX[1:])
        with pytest.raises(ValueError):
            api._tx_body_bytes(b"")


# ---------------------------------------------------------------------------
# Build path integration (real _build_cosigned_surrender_tx)
# ---------------------------------------------------------------------------


class TestBuildPath:
    def test_flag_default_is_on(self):
        assert api.TAG_SETS_258 is True

    def test_flag_on_builds_uniformly_tagged_wire(self):
        tx_hex, tx_hash_hex = build_real_surrender_tx(tag_sets_on=True)
        raw = bytes.fromhex(tx_hex)
        body_bytes, ws_bytes, tail = split_tx(raw)
        body = _pure_loads(body_bytes)
        ws = _pure_loads(ws_bytes)
        for k in (0, 13, 14):
            assert _is_tag258(body[k]), f"body[{k}] not tag-258 on the wire"
        assert _is_tag258(ws[0]), "vkey witnesses not tag-258 on the wire"
        assert _is_tag258(ws[7])
        assert tail == b"\xf5\xf6"

    def test_flag_on_signatures_cover_the_wire_body_hash(self):
        from nacl.signing import VerifyKey

        tx_hex, tx_hash_hex = build_real_surrender_tx(tag_sets_on=True)
        body_bytes, ws_bytes, _ = split_tx(bytes.fromhex(tx_hex))
        # the returned tx_hash IS blake2b-256 of the exact wire body bytes —
        # the single key for stash/cosigner/submit/chaining
        assert tx_hash_hex == hashlib.blake2b(body_bytes, digest_size=32).hexdigest()
        # and the admin witness actually verifies over that hash
        vkeys = _pure_loads(ws_bytes)[0].value
        vk, sig = vkeys[0]
        VerifyKey(bytes(vk)).verify(
            bytes.fromhex(tx_hash_hex), bytes(sig)
        )  # raises BadSignatureError on mismatch

    def test_flag_on_spend_index_matches_tagged_input_position(self):
        tx_hex, _ = build_real_surrender_tx(tag_sets_on=True)
        body_bytes, ws_bytes, _ = split_tx(bytes.fromhex(tx_hex))
        inputs = [(bytes(e[0]), e[1]) for e in _pure_loads(body_bytes)[0].value]
        assert inputs == sorted(inputs), "inputs not canonical inside the tag"
        spend_idx = next(k[1] for k in _pure_loads(ws_bytes)[5] if k[0] == 0)
        assert inputs[spend_idx] == (bytes.fromhex(_POOL_HASH), 1)

    def test_flag_off_reverts_byte_exactly_to_legacy_wire(self):
        # the kill switch: TAG_SETS_258=off must reproduce today's exact bytes
        tx_hex, tx_hash_hex = build_real_surrender_tx(tag_sets_on=False)
        assert tx_hex == MIXED_TX_HEX
        body_bytes, _, _ = split_tx(bytes.fromhex(tx_hex))
        assert tx_hash_hex == hashlib.blake2b(body_bytes, digest_size=32).hexdigest()
