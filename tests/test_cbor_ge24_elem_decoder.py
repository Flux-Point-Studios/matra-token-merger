"""Regression: tx bodies with a >=24-element set field must build (surrender-api).

pycardano <0.19 globally patches cbor2's array decoder (``major_decoders[4]``)
to wrap indefinite-length arrays as ``IndefiniteFrozenList``. Its definite-length
branch re-invokes ``decode_array`` after ``_decode_length`` already consumed the
length prefix, so any DEFINITE array whose length needs a following byte — CBOR
minor 24-27, i.e. >=24 elements — has that prefix read twice. The second read
treats the first element's initial byte as the count and runs off the buffer end
(``CBORDecodeEOF``).

The surrender-api's byte-surgery decoders (``_pure_loads`` and the raw
``CBORDecoder(stream).decode()`` calls) go through that patched global, so a tx
whose inputs set has >=24 entries — routine for a fragmented wallet whose
coin-selection pulls two dozen UTxOs — crashes ``_wrap_body_sets_258`` and the
build returns a generic 422. surrender_api.py reinstalls a corrected drop-in that
keeps the ``IndefiniteFrozenList`` wrap but reads each length exactly once.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import cbor2
import cbor2._decoder
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("NETWORK", "mainnet")
os.environ.setdefault("SURRENDER_API_SECRET", "test-secret")

from pycardano import (  # noqa: E402
    Address,
    TransactionBody,
    TransactionInput,
    TransactionOutput,
    Value,
)
from pycardano.hash import TransactionId  # noqa: E402
from pycardano.serialization import IndefiniteFrozenList  # noqa: E402

import services.surrender_api as api  # noqa: E402

# A key-controlled mainnet wallet address (payment part is a pubkey hash).
_ADDR = (
    "addr1q83qhja7la59capht8jpc57r3kg2ur486tt9p8tk0r5fjtz"
    "zxskva2ter745anttpyvzpzgv95qs3ck6wg858p7askwqgyu0pt"
)


def _body_cbor(n_inputs: int) -> bytes:
    """A minimal tx body carrying exactly ``n_inputs`` inputs (set field 0)."""
    ins = [
        TransactionInput(TransactionId(bytes([i % 256]) * 32), i)
        for i in range(n_inputs)
    ]
    body = TransactionBody(
        inputs=ins,
        outputs=[TransactionOutput(Address.from_primitive(_ADDR), Value(2_000_000))],
        fee=300_000,
    )
    raw = body.to_cbor()
    return raw if isinstance(raw, (bytes, bytearray)) else bytes.fromhex(raw)


def _pycardano_018_broken_decode_array(self, subtype):
    """The pre-fix pycardano <0.19 patch, verbatim in behavior: it pre-reads the
    length via ``_decode_length`` then re-delegates to the stock decoder, which
    reads the length a second time."""
    stock = cbor2._decoder.CBORDecoder.decode_array
    length = self._decode_length(subtype, allow_indefinite=True)
    if length is None:
        ret = IndefiniteFrozenList(stock(self, subtype))
        ret.freeze()
        return ret
    return stock(self, subtype)


def _decode_with(handler, data: bytes):
    saved = cbor2._decoder.major_decoders[4]
    cbor2._decoder.major_decoders[4] = handler
    try:
        return cbor2._decoder.loads(data)
    finally:
        cbor2._decoder.major_decoders[4] = saved


# --- RED: prove the pre-fix decoder is the culprit -------------------------

def test_pycardano_018_patch_corrupts_ge24_element_arrays():
    body24 = _body_cbor(24)
    # The exact pre-fix pycardano decoder blows up on the >=24-input body...
    with pytest.raises(cbor2._types.CBORDecodeError):
        _decode_with(_pycardano_018_broken_decode_array, body24)
    # ...but is fine at 23 (length fits in the initial byte, read-twice is a no-op).
    assert isinstance(_decode_with(_pycardano_018_broken_decode_array, _body_cbor(23)), dict)


# --- GREEN: the installed corrected decoder + the build helpers -------------

def test_installed_decoder_reads_ge24_length_once():
    # surrender_api.py has already reinstalled the corrected major_decoders[4].
    for n in (23, 24, 25, 94):
        decoded = cbor2._decoder.loads(_body_cbor(n))
        assert isinstance(decoded, dict)
        assert len(decoded[0]) == n  # inputs set decoded to the right length


def test_wrap_body_sets_258_handles_ge24_inputs():
    for n in (23, 24, 94):
        wrapped = api._wrap_body_sets_258(_body_cbor(n))
        redecoded = api._pure_loads(wrapped)
        # inputs (key 0) framed as a tag-258 set of the right size
        assert getattr(redecoded[0], "tag", None) == 258
        assert len(redecoded[0].value) == n


def test_wrap_body_sets_258_preserves_input_order_at_ge24():
    # The SPEND redeemer indexes against canonical input order — it must survive.
    body = _body_cbor(30)
    original_inputs = list(api._pure_loads(body)[0])
    wrapped_inputs = list(api._pure_loads(api._wrap_body_sets_258(body))[0].value)
    assert wrapped_inputs == original_inputs


def test_tx_body_bytes_slices_ge24_input_tx():
    # The raw-decoder path (_tx_body_bytes walks the stream) must also handle >=24.
    ins = [TransactionInput(TransactionId(bytes([i % 256]) * 32), i) for i in range(24)]
    body = TransactionBody(
        inputs=ins,
        outputs=[TransactionOutput(Address.from_primitive(_ADDR), Value(2_000_000))],
        fee=300_000,
    )
    from pycardano import Transaction, TransactionWitnessSet

    tx = Transaction(body, TransactionWitnessSet())
    wire = tx.to_cbor()
    wire = wire if isinstance(wire, (bytes, bytearray)) else bytes.fromhex(wire)
    body_bytes = api._tx_body_bytes(wire)
    # The sliced body decodes cleanly and round-trips the input count.
    assert len(api._pure_loads(body_bytes)[0]) == 24
