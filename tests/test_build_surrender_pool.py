"""Tests for tools.build_surrender_pool — encode_pool_datum and build_pool_outputs.

Does NOT test build_pool_tx_cbor or build_surrender_pool (those require chain context).
"""

from __future__ import annotations

import cbor2
import pytest
from cbor2 import CBORTag

from tools.build_surrender_pool import build_pool_outputs, encode_pool_datum

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PUBLIC_POOL_BASE = 850_000_000_000_000_000_000  # from tools.config
SAMPLE_SCRIPT_ADDR = (
    "addr_test1wzyfdmvvs62kerr0s4rjfyqfahq5lup0jj0z7f9mn7mqr6c03dhut"
)
SAMPLE_POLICY = "aa" * 28  # 56-char hex
SAMPLE_ASSET_HEX = "634d41545241"  # "cMATRA"


# ---------------------------------------------------------------------------
# TestEncodePoolDatum
# ---------------------------------------------------------------------------


class TestEncodePoolDatum:
    """Verify that encode_pool_datum produces correct CBOR for Constr(0, [])."""

    def test_returns_bytes(self):
        result = encode_pool_datum()
        assert isinstance(result, bytes)

    def test_decodes_to_cbor_tag_121(self):
        result = encode_pool_datum()
        decoded = cbor2.loads(result)
        assert isinstance(decoded, CBORTag)
        assert decoded.tag == 121
        assert decoded.value == []

    def test_deterministic(self):
        """Multiple calls produce identical bytes."""
        assert encode_pool_datum() == encode_pool_datum()

    def test_non_empty(self):
        result = encode_pool_datum()
        assert len(result) > 0


# ---------------------------------------------------------------------------
# TestBuildPoolOutputs
# ---------------------------------------------------------------------------


class TestBuildPoolOutputs:
    """Verify build_pool_outputs splitting, validation, and output structure."""

    # -- Happy path: 10 UTxOs (default) ------------------------------------

    def test_ten_utxos_count(self):
        outputs = build_pool_outputs(
            PUBLIC_POOL_BASE,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=10,
        )
        assert len(outputs) == 10

    def test_ten_utxos_sum_equals_total(self):
        outputs = build_pool_outputs(
            PUBLIC_POOL_BASE,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=10,
        )
        total = sum(o["cmatra_base_units"] for o in outputs)
        assert total == PUBLIC_POOL_BASE

    def test_ten_utxos_last_gets_remainder(self):
        outputs = build_pool_outputs(
            PUBLIC_POOL_BASE,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=10,
        )
        per_utxo = PUBLIC_POOL_BASE // 10
        remainder = PUBLIC_POOL_BASE - per_utxo * 9
        # First 9 get floor division amount
        for o in outputs[:9]:
            assert o["cmatra_base_units"] == per_utxo
        # Last gets the remainder
        assert outputs[-1]["cmatra_base_units"] == remainder

    # -- Single UTxO --------------------------------------------------------

    def test_single_utxo_gets_all(self):
        outputs = build_pool_outputs(
            PUBLIC_POOL_BASE,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=1,
        )
        assert len(outputs) == 1
        assert outputs[0]["cmatra_base_units"] == PUBLIC_POOL_BASE

    # -- Non-evenly-divisible total -----------------------------------------

    def test_uneven_split_preserves_sum(self):
        """When total is not evenly divisible, remainder goes to last UTxO."""
        total = 1_000_000_007  # prime, not divisible by 3
        outputs = build_pool_outputs(
            total,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=3,
        )
        assert len(outputs) == 3
        assert sum(o["cmatra_base_units"] for o in outputs) == total
        # Last UTxO should be >= the others
        assert outputs[-1]["cmatra_base_units"] >= outputs[0]["cmatra_base_units"]

    # -- Sequential indices -------------------------------------------------

    def test_indices_sequential(self):
        outputs = build_pool_outputs(
            PUBLIC_POOL_BASE,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=5,
        )
        indices = [o["pool_utxo_index"] for o in outputs]
        assert indices == list(range(5))

    # -- Output field correctness -------------------------------------------

    def test_output_fields_present_and_correct(self):
        outputs = build_pool_outputs(
            PUBLIC_POOL_BASE,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=3,
        )
        expected_datum_hex = encode_pool_datum().hex()

        for o in outputs:
            assert o["script_address"] == SAMPLE_SCRIPT_ADDR
            assert o["cmatra_policy_hex"] == SAMPLE_POLICY
            assert o["cmatra_asset_hex"] == SAMPLE_ASSET_HEX
            assert o["datum_cbor_hex"] == expected_datum_hex
            assert isinstance(o["min_ada_lovelace"], int)
            assert o["min_ada_lovelace"] > 0
            assert isinstance(o["pool_utxo_index"], int)
            assert isinstance(o["cmatra_base_units"], int)
            assert o["cmatra_base_units"] > 0

    def test_datum_cbor_hex_is_valid_cbor(self):
        outputs = build_pool_outputs(
            PUBLIC_POOL_BASE,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=2,
        )
        datum_hex = outputs[0]["datum_cbor_hex"]
        decoded = cbor2.loads(bytes.fromhex(datum_hex))
        assert isinstance(decoded, CBORTag)
        assert decoded.tag == 121

    # -- Validation errors --------------------------------------------------

    def test_num_utxos_zero_raises(self):
        with pytest.raises(ValueError, match="num_utxos must be >= 1"):
            build_pool_outputs(
                PUBLIC_POOL_BASE,
                SAMPLE_SCRIPT_ADDR,
                SAMPLE_POLICY,
                SAMPLE_ASSET_HEX,
                num_utxos=0,
            )

    def test_num_utxos_negative_raises(self):
        with pytest.raises(ValueError, match="num_utxos must be >= 1"):
            build_pool_outputs(
                PUBLIC_POOL_BASE,
                SAMPLE_SCRIPT_ADDR,
                SAMPLE_POLICY,
                SAMPLE_ASSET_HEX,
                num_utxos=-5,
            )

    def test_total_zero_raises(self):
        with pytest.raises(ValueError, match="total_cmatra_base must be > 0"):
            build_pool_outputs(
                0,
                SAMPLE_SCRIPT_ADDR,
                SAMPLE_POLICY,
                SAMPLE_ASSET_HEX,
                num_utxos=10,
            )

    def test_total_negative_raises(self):
        with pytest.raises(ValueError, match="total_cmatra_base must be > 0"):
            build_pool_outputs(
                -1,
                SAMPLE_SCRIPT_ADDR,
                SAMPLE_POLICY,
                SAMPLE_ASSET_HEX,
                num_utxos=10,
            )

    def test_total_too_small_for_num_utxos_raises(self):
        """If total // num_utxos == 0, per_utxo is zero -> ValueError."""
        with pytest.raises(ValueError, match="too small"):
            build_pool_outputs(
                5,
                SAMPLE_SCRIPT_ADDR,
                SAMPLE_POLICY,
                SAMPLE_ASSET_HEX,
                num_utxos=10,
            )

    # -- Large num_utxos ----------------------------------------------------

    def test_many_utxos_sum_preserved(self):
        total = 1_000_000_000_000
        n = 100
        outputs = build_pool_outputs(
            total,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=n,
        )
        assert len(outputs) == n
        assert sum(o["cmatra_base_units"] for o in outputs) == total

    # -- Exactly divisible total --------------------------------------------

    def test_evenly_divisible_all_equal(self):
        """When total divides evenly, all UTxOs get the same amount."""
        total = 1_000_000  # divisible by 4
        outputs = build_pool_outputs(
            total,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=4,
        )
        amounts = [o["cmatra_base_units"] for o in outputs]
        assert all(a == 250_000 for a in amounts)

    # -- min_ada_lovelace is consistent across outputs ----------------------

    def test_min_ada_consistent_across_outputs(self):
        outputs = build_pool_outputs(
            PUBLIC_POOL_BASE,
            SAMPLE_SCRIPT_ADDR,
            SAMPLE_POLICY,
            SAMPLE_ASSET_HEX,
            num_utxos=5,
        )
        min_ada_values = {o["min_ada_lovelace"] for o in outputs}
        assert len(min_ada_values) == 1, "All outputs should have the same min_ada"
