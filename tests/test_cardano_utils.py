"""Tests for tools.cardano_utils — datum encoding, address parsing, min-ADA."""

import pytest

from tools.cardano_utils import (
    decode_claim_datum,
    encode_claim_datum,
    estimate_min_ada,
)
from tests.conftest import SAMPLE_PKH_1, SAMPLE_PKH_2


class TestClaimDatumEncoding:
    def test_roundtrip(self):
        """Encode then decode should return original key hash."""
        encoded = encode_claim_datum(SAMPLE_PKH_1)
        decoded = decode_claim_datum(encoded.hex())
        assert decoded == SAMPLE_PKH_1

    def test_roundtrip_different_pkh(self):
        encoded = encode_claim_datum(SAMPLE_PKH_2)
        decoded = decode_claim_datum(encoded.hex())
        assert decoded == SAMPLE_PKH_2

    def test_encode_returns_bytes(self):
        result = encode_claim_datum(SAMPLE_PKH_1)
        assert isinstance(result, bytes)

    def test_encode_is_deterministic(self):
        a = encode_claim_datum(SAMPLE_PKH_1)
        b = encode_claim_datum(SAMPLE_PKH_1)
        assert a == b

    def test_invalid_pkh_length(self):
        with pytest.raises(AssertionError, match="28-byte"):
            encode_claim_datum("abcd")

    def test_invalid_pkh_not_hex(self):
        with pytest.raises(ValueError):
            encode_claim_datum("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")

    def test_decode_invalid_cbor(self):
        with pytest.raises(Exception):
            decode_claim_datum("deadbeef")

    def test_decode_wrong_structure(self):
        """CBOR that's valid but not a ClaimDatum should raise."""
        import cbor2
        # Just encode a plain integer
        bad_cbor = cbor2.dumps(42).hex()
        with pytest.raises(ValueError, match="Unexpected datum"):
            decode_claim_datum(bad_cbor)


class TestMinAdaEstimate:
    def test_single_asset(self):
        min_ada = estimate_min_ada(num_assets=1, datum_size_bytes=40)
        assert min_ada >= 1_000_000  # at least 1 ADA

    def test_no_assets(self):
        min_ada = estimate_min_ada(num_assets=0, datum_size_bytes=0)
        assert min_ada >= 1_000_000

    def test_more_assets_higher_ada(self):
        low = estimate_min_ada(num_assets=1)
        high = estimate_min_ada(num_assets=5)
        assert high >= low

    def test_larger_datum_higher_ada(self):
        low = estimate_min_ada(datum_size_bytes=30)
        high = estimate_min_ada(datum_size_bytes=200)
        assert high >= low
