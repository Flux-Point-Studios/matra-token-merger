"""Tests for tools.build_claim_utxos_flux — output construction and batching."""

import pytest

from tools.build_claim_utxos_flux import (
    batch_outputs,
    build_claim_outputs,
    load_allocations,
)
from tests.conftest import SAMPLE_FLUX_ASSET, SAMPLE_FLUX_POLICY, SAMPLE_PKH_1, SAMPLE_PKH_2


class TestLoadAllocations:
    def test_load_from_csv(self, tmp_path):
        csv_content = (
            "payment_key_hash_hex,flux_total_units,addresses\n"
            f"{SAMPLE_PKH_1},1000000,addr1\n"
            f"{SAMPLE_PKH_2},500000,addr2\n"
        )
        csv_path = tmp_path / "alloc.csv"
        csv_path.write_text(csv_content)

        allocs = load_allocations(csv_path)
        assert len(allocs) == 2
        assert allocs[0]["payment_key_hash_hex"] == SAMPLE_PKH_1
        assert allocs[0]["flux_units"] == 1_000_000
        assert allocs[1]["flux_units"] == 500_000

    def test_skip_zero_allocations(self, tmp_path):
        csv_content = (
            "payment_key_hash_hex,flux_total_units,addresses\n"
            f"{SAMPLE_PKH_1},1000000,addr1\n"
            f"{SAMPLE_PKH_2},0,addr2\n"
        )
        csv_path = tmp_path / "alloc.csv"
        csv_path.write_text(csv_content)

        allocs = load_allocations(csv_path)
        assert len(allocs) == 1

    def test_empty_csv(self, tmp_path):
        csv_content = "payment_key_hash_hex,flux_total_units,addresses\n"
        csv_path = tmp_path / "alloc.csv"
        csv_path.write_text(csv_content)

        allocs = load_allocations(csv_path)
        assert len(allocs) == 0


class TestBuildClaimOutputs:
    def test_output_structure(self):
        allocs = [
            {"payment_key_hash_hex": SAMPLE_PKH_1, "flux_units": 1_000_000},
        ]
        outputs = build_claim_outputs(
            allocs,
            script_address="addr1w_script",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            flux_asset_hex=SAMPLE_FLUX_ASSET,
        )
        assert len(outputs) == 1
        out = outputs[0]
        assert out["payment_key_hash_hex"] == SAMPLE_PKH_1
        assert out["flux_units"] == 1_000_000
        assert out["min_ada_lovelace"] >= 1_000_000
        assert len(out["datum_cbor_hex"]) > 0
        assert out["script_address"] == "addr1w_script"

    def test_datum_cbor_is_valid(self):
        from tools.cardano_utils import decode_claim_datum

        allocs = [
            {"payment_key_hash_hex": SAMPLE_PKH_1, "flux_units": 500},
        ]
        outputs = build_claim_outputs(
            allocs, "addr", SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        decoded_pkh = decode_claim_datum(outputs[0]["datum_cbor_hex"])
        assert decoded_pkh == SAMPLE_PKH_1

    def test_multiple_outputs(self):
        allocs = [
            {"payment_key_hash_hex": SAMPLE_PKH_1, "flux_units": 100},
            {"payment_key_hash_hex": SAMPLE_PKH_2, "flux_units": 200},
        ]
        outputs = build_claim_outputs(
            allocs, "addr", SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        assert len(outputs) == 2
        assert outputs[0]["flux_units"] == 100
        assert outputs[1]["flux_units"] == 200


class TestBatchOutputs:
    def test_single_batch(self):
        outputs = [{"id": i} for i in range(10)]
        batches = batch_outputs(outputs, batch_size=40)
        assert len(batches) == 1
        assert len(batches[0]) == 10

    def test_multiple_batches(self):
        outputs = [{"id": i} for i in range(100)]
        batches = batch_outputs(outputs, batch_size=40)
        assert len(batches) == 3
        assert len(batches[0]) == 40
        assert len(batches[1]) == 40
        assert len(batches[2]) == 20

    def test_exact_batch_size(self):
        outputs = [{"id": i} for i in range(80)]
        batches = batch_outputs(outputs, batch_size=40)
        assert len(batches) == 2
        assert all(len(b) == 40 for b in batches)

    def test_empty_list(self):
        batches = batch_outputs([], batch_size=40)
        assert batches == []

    def test_batch_size_one(self):
        outputs = [{"id": i} for i in range(5)]
        batches = batch_outputs(outputs, batch_size=1)
        assert len(batches) == 5
        assert all(len(b) == 1 for b in batches)

    def test_preserves_all_outputs(self):
        outputs = [{"id": i} for i in range(97)]
        batches = batch_outputs(outputs, batch_size=30)
        total = sum(len(b) for b in batches)
        assert total == 97
