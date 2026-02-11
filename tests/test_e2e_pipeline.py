"""
End-to-end pipeline tests.

These tests exercise the full off-chain pipeline (phases 1–8) using mocked
API responses, verifying that data flows correctly between stages and that
all invariants hold.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tools.cardano_utils import encode_claim_datum
from tools.config import AGENT, FLUX_DECIMALS, FLUX_MAX_SUPPLY_BASE, LEGACY_TOKENS, SHARDS
from tools.flux_merge_valuation_int import (
    build_merge_report,
    compute_integer_buckets,
    compute_valuations,
)
from tools.snapshot_allocate_flux import (
    allocate_flux,
    run_snapshot_and_allocate,
    write_allocations_csv,
)
from tools.twap_snapshot_pools import build_twap_report
from tools.build_claim_utxos_flux import (
    build_claim_outputs,
    batch_outputs,
    load_allocations,
)
from tools.build_flux_claim_index import build_index_from_manifest
from tools.claim_flux_indexed import verify_claim_utxo

from tests.conftest import (
    SAMPLE_FLUX_ASSET,
    SAMPLE_FLUX_POLICY,
    SAMPLE_PKH_1,
    SAMPLE_PKH_2,
    SAMPLE_PKH_3,
)


# ===================================================================
# E2E: Full pipeline with synthetic data
# ===================================================================


class TestE2EFullPipeline:
    """Simulate the entire pipeline from TWAP → claim index verification."""

    # Phase 1: TWAP report
    @pytest.fixture
    def twap_report(self) -> dict[str, Any]:
        return {
            "report_type": "twap_snapshot_pools",
            "generated_at": "2024-01-15T12:00:00+00:00",
            "ada_usd_price": 0.50,
            "tokens": {
                "AGENT": {
                    "unit": AGENT.unit,
                    "decimals": 0,
                    "combined_twap": {"ada": 10.0, "usd": 5.0},
                },
                "SHARDS": {
                    "unit": SHARDS.unit,
                    "decimals": 6,
                    "combined_twap": {"ada": 0.002, "usd": 0.001},
                },
            },
        }

    # Phase 2: Merge report
    @pytest.fixture
    def merge_report(self, twap_report) -> dict[str, Any]:
        """Build merge report from TWAP data + synthetic supplies."""
        # AGENT: 10M supply, $5 each → $50M valuation
        # SHARDS: 500M display supply (5e14 base), $0.001 each → $500K valuation
        agent_supply = 10_000_000
        shards_supply = 500_000_000_000_000  # 500M * 1e6

        prices = {
            "AGENT": twap_report["tokens"]["AGENT"]["combined_twap"]["usd"],
            "SHARDS": twap_report["tokens"]["SHARDS"]["combined_twap"]["usd"],
        }
        supplies = {"AGENT": agent_supply, "SHARDS": shards_supply}

        val_data = compute_valuations(LEGACY_TOKENS, supplies, prices)
        buckets = compute_integer_buckets(LEGACY_TOKENS, val_data["weights"])

        return {
            "report_type": "flux_merge_valuation",
            "flux_total_base_units": FLUX_MAX_SUPPLY_BASE,
            "tokens": {
                "AGENT": {
                    "unit": AGENT.unit,
                    "decimals": 0,
                    "supply_base_units": agent_supply,
                    "twap_usd": prices["AGENT"],
                    "valuation_usd": val_data["valuations_usd"]["AGENT"],
                    "weight": val_data["weights"]["AGENT"],
                    "flux_bucket_base_units": buckets["AGENT"],
                },
                "SHARDS": {
                    "unit": SHARDS.unit,
                    "decimals": 6,
                    "supply_base_units": shards_supply,
                    "twap_usd": prices["SHARDS"],
                    "valuation_usd": val_data["valuations_usd"]["SHARDS"],
                    "weight": val_data["weights"]["SHARDS"],
                    "flux_bucket_base_units": buckets["SHARDS"],
                },
            },
            "totals": {
                "sum_buckets_base_units": sum(buckets.values()),
                "buckets_sum_equals_max": sum(buckets.values()) == FLUX_MAX_SUPPLY_BASE,
            },
        }

    def test_phase2_buckets_sum_invariant(self, merge_report):
        """Critical invariant: FLUX buckets must sum to exactly 1e15."""
        assert merge_report["totals"]["buckets_sum_equals_max"] is True
        total = sum(
            t["flux_bucket_base_units"]
            for t in merge_report["tokens"].values()
        )
        assert total == FLUX_MAX_SUPPLY_BASE

    # Phase 3+4: Allocation
    @pytest.fixture
    def allocations(self, merge_report) -> dict[str, list[dict]]:
        """Compute allocations for synthetic holders."""
        agent_bucket = merge_report["tokens"]["AGENT"]["flux_bucket_base_units"]
        shards_bucket = merge_report["tokens"]["SHARDS"]["flux_bucket_base_units"]

        # Synthetic holders
        agent_holders = [
            {"address": "addr_alice", "quantity": 5_000_000},
            {"address": "addr_bob", "quantity": 3_000_000},
            {"address": "addr_carol", "quantity": 2_000_000},
        ]
        shards_holders = [
            {"address": "addr_alice", "quantity": 200_000_000_000_000},
            {"address": "addr_dave", "quantity": 300_000_000_000_000},
        ]

        agent_allocs = allocate_flux(AGENT, agent_holders, agent_bucket)
        shards_allocs = allocate_flux(SHARDS, shards_holders, shards_bucket)

        return {"agent": agent_allocs, "shards": shards_allocs}

    def test_phase4_allocation_proportional(self, allocations, merge_report):
        """Each holder's allocation should be proportional to their balance."""
        agent_allocs = allocations["agent"]
        bucket = merge_report["tokens"]["AGENT"]["flux_bucket_base_units"]

        # Alice has 50% of AGENT supply → should get ~50% of AGENT bucket
        alice_alloc = agent_allocs[0]["flux_units"]
        assert alice_alloc == (5_000_000 * bucket) // 10_000_000

    def test_phase4_allocation_no_overflow(self, allocations, merge_report):
        """Total allocated per token must not exceed its bucket."""
        for token_name, allocs in [
            ("AGENT", allocations["agent"]),
            ("SHARDS", allocations["shards"]),
        ]:
            bucket = merge_report["tokens"][token_name]["flux_bucket_base_units"]
            total = sum(a["flux_units"] for a in allocs)
            assert total <= bucket

    def test_phase4_dust_is_small(self, allocations, merge_report):
        """Dust from floor rounding should be minimal (< number of holders)."""
        for token_name, allocs in [
            ("AGENT", allocations["agent"]),
            ("SHARDS", allocations["shards"]),
        ]:
            bucket = merge_report["tokens"][token_name]["flux_bucket_base_units"]
            total = sum(a["flux_units"] for a in allocs)
            dust = bucket - total
            assert dust < len(allocs), f"{token_name} dust {dust} >= {len(allocs)} holders"

    # Phase 6: Claim outputs
    def test_phase6_outputs_match_allocations(self):
        """Claim outputs should contain correct datum and FLUX qty."""
        allocs = [
            {"payment_key_hash_hex": SAMPLE_PKH_1, "flux_units": 500_000},
            {"payment_key_hash_hex": SAMPLE_PKH_2, "flux_units": 300_000},
        ]
        outputs = build_claim_outputs(
            allocs, "addr1w_script", SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )

        assert len(outputs) == 2
        for i, out in enumerate(outputs):
            assert out["payment_key_hash_hex"] == allocs[i]["payment_key_hash_hex"]
            assert out["flux_units"] == allocs[i]["flux_units"]
            assert out["min_ada_lovelace"] >= 1_000_000

    # Phase 7+8: Index + verification
    def test_phase7_index_then_verify(self, mocker):
        """Build index from manifest, then verify each UTxO."""
        manifest = {
            "script_address": "addr1w_script",
            "flux_policy_hex": SAMPLE_FLUX_POLICY,
            "flux_asset_hex": SAMPLE_FLUX_ASSET,
            "batches": [
                {
                    "tx_hash": "aa" * 32,
                    "claimants": [
                        {"payment_key_hash_hex": SAMPLE_PKH_1, "flux_units": 500_000},
                        {"payment_key_hash_hex": SAMPLE_PKH_2, "flux_units": 300_000},
                    ],
                },
            ],
        }

        flux_unit = SAMPLE_FLUX_POLICY + SAMPLE_FLUX_ASSET
        mock_bf = mocker.MagicMock()
        mock_bf.get_tx_utxos.return_value = {
            "outputs": [
                {
                    "address": "addr1w_script",
                    "output_index": 0,
                    "inline_datum": encode_claim_datum(SAMPLE_PKH_1).hex(),
                    "amount": [
                        {"unit": "lovelace", "quantity": "2000000"},
                        {"unit": flux_unit, "quantity": "500000"},
                    ],
                },
                {
                    "address": "addr1w_script",
                    "output_index": 1,
                    "inline_datum": encode_claim_datum(SAMPLE_PKH_2).hex(),
                    "amount": [
                        {"unit": "lovelace", "quantity": "2000000"},
                        {"unit": flux_unit, "quantity": "300000"},
                    ],
                },
            ],
        }

        result = build_index_from_manifest(mock_bf, manifest)
        index = result["index_min"]

        # Index should have both claimants
        assert SAMPLE_PKH_1 in index
        assert SAMPLE_PKH_2 in index

        # Verify each indexed UTxO
        for pkh, refs in index.items():
            for ref in refs:
                tx_hash, output_idx, flux_qty = ref
                utxo_data = mock_bf.get_tx_utxos.return_value["outputs"][output_idx]
                verification = verify_claim_utxo(
                    utxo_data, "addr1w_script", pkh,
                    SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
                )
                assert verification["valid"] is True


# ===================================================================
# E2E: CSV roundtrip
# ===================================================================


class TestE2ECSVRoundtrip:
    """Test that allocations written to CSV can be read back correctly."""

    def test_write_then_load(self, tmp_path):
        rows = [
            {
                "payment_key_hash_hex": SAMPLE_PKH_1,
                "addresses": ["addr1_alice", "addr1_alice_2"],
                "agent_balance_base": 5_000_000,
                "agent_flux_units": 250_000_000_000,
                "shards_balance_base": 1_000_000_000_000,
                "shards_flux_units": 500_000_000,
                "flux_total_units": 250_500_000_000,
                "flux_total_display": 250500.0,
            },
            {
                "payment_key_hash_hex": SAMPLE_PKH_2,
                "addresses": ["addr1_bob"],
                "agent_balance_base": 3_000_000,
                "agent_flux_units": 150_000_000_000,
                "shards_balance_base": 0,
                "shards_flux_units": 0,
                "flux_total_units": 150_000_000_000,
                "flux_total_display": 150000.0,
            },
        ]

        csv_path = tmp_path / "allocations.csv"
        write_allocations_csv(rows, csv_path)

        # Load back
        loaded = load_allocations(csv_path)
        assert len(loaded) == 2
        assert loaded[0]["payment_key_hash_hex"] == SAMPLE_PKH_1
        assert loaded[0]["flux_units"] == 250_500_000_000
        assert loaded[1]["flux_units"] == 150_000_000_000


# ===================================================================
# E2E: Integer math invariants
# ===================================================================


class TestE2EIntegerInvariants:
    """Stress-test integer allocation to ensure no precision loss."""

    def test_many_holders_total_preserved(self):
        """With many small holders, total allocated ≈ bucket."""
        bucket = 500_000_000_000_000  # 500T
        holders = [
            {"address": f"addr_{i}", "quantity": 100 + i}
            for i in range(1000)
        ]
        allocs = allocate_flux(AGENT, holders, bucket)
        total = sum(a["flux_units"] for a in allocs)
        assert total <= bucket
        # Dust should be less than number of holders
        assert bucket - total < len(holders)

    def test_single_holder_gets_exact_bucket(self):
        bucket = 999_999_999_999_999
        holders = [{"address": "a", "quantity": 42}]
        allocs = allocate_flux(AGENT, holders, bucket)
        assert allocs[0]["flux_units"] == bucket

    def test_two_equal_holders(self):
        bucket = 1_000_000_000_000_001  # Odd number
        holders = [
            {"address": "a", "quantity": 500},
            {"address": "b", "quantity": 500},
        ]
        allocs = allocate_flux(AGENT, holders, bucket)
        assert allocs[0]["flux_units"] == allocs[1]["flux_units"]
        total = sum(a["flux_units"] for a in allocs)
        assert total <= bucket

    def test_whale_and_dust_holders(self):
        """One massive holder + many tiny holders."""
        bucket = FLUX_MAX_SUPPLY_BASE
        holders = [
            {"address": "whale", "quantity": 99_000_000},
        ] + [
            {"address": f"dust_{i}", "quantity": 1}
            for i in range(1_000_000 // 1000)  # 1000 dust holders
        ]
        total_supply = sum(h["quantity"] for h in holders)
        allocs = allocate_flux(AGENT, holders, bucket)
        total = sum(a["flux_units"] for a in allocs)
        assert total <= bucket
        # Whale should get the lion's share
        whale_alloc = allocs[0]["flux_units"]
        assert whale_alloc > bucket * 0.98


# ===================================================================
# E2E: Datum encoding consistency
# ===================================================================


class TestE2EDatumConsistency:
    """Verify datum encoding is consistent across all pipeline stages."""

    def test_datum_roundtrip_through_index(self, mocker):
        """Datum encoded in Phase 6 should decode correctly in Phase 7."""
        from tools.cardano_utils import decode_claim_datum

        # Phase 6: encode datum
        encoded = encode_claim_datum(SAMPLE_PKH_1)

        # Phase 7: decode from Blockfrost-style response
        decoded = decode_claim_datum(encoded.hex())
        assert decoded == SAMPLE_PKH_1

    def test_all_sample_pkhs_roundtrip(self):
        from tools.cardano_utils import decode_claim_datum

        for pkh in [SAMPLE_PKH_1, SAMPLE_PKH_2, SAMPLE_PKH_3]:
            encoded = encode_claim_datum(pkh)
            decoded = decode_claim_datum(encoded.hex())
            assert decoded == pkh

    def test_datum_different_pkhs_different_cbor(self):
        cbor1 = encode_claim_datum(SAMPLE_PKH_1)
        cbor2 = encode_claim_datum(SAMPLE_PKH_2)
        assert cbor1 != cbor2


# ===================================================================
# E2E: Batch sizing
# ===================================================================


class TestE2EBatchSizing:
    """Test that batching preserves all allocations."""

    def test_all_outputs_survive_batching(self):
        allocs = [
            {
                "payment_key_hash_hex": format(i, "056x"),
                "flux_units": 1000 + i,
            }
            for i in range(237)
        ]
        outputs = build_claim_outputs(
            allocs, "addr", SAMPLE_FLUX_POLICY, SAMPLE_FLUX_ASSET,
        )
        batches = batch_outputs(outputs, batch_size=40)

        total_outputs = sum(len(b) for b in batches)
        assert total_outputs == 237

        total_flux = sum(o["flux_units"] for batch in batches for o in batch)
        expected_flux = sum(a["flux_units"] for a in allocs)
        assert total_flux == expected_flux
