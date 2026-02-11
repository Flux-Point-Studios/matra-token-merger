"""Tests for tools.snapshot_allocate_flux — holder filtering, integer allocation, dust."""

import pytest

from tools.snapshot_allocate_flux import (
    allocate_flux,
    filter_holders,
    write_allocations_csv,
    CSV_COLUMNS,
)
from tools.config import AGENT, FLUX_DECIMALS, FLUX_MAX_SUPPLY_BASE, SHARDS
from tests.conftest import SAMPLE_PKH_1


class TestFilterHolders:
    def test_no_filters(self):
        holders = [
            {"address": "addr1_a", "quantity": 100},
            {"address": "addr1_b", "quantity": 200},
        ]
        result = filter_holders(holders, exclude_script=False)
        assert len(result) == 2

    def test_exclude_specific_addresses(self):
        holders = [
            {"address": "addr1_a", "quantity": 100},
            {"address": "addr1_b", "quantity": 200},
            {"address": "addr1_c", "quantity": 300},
        ]
        result = filter_holders(
            holders,
            exclude_script=False,
            exclude_addresses={"addr1_b"},
        )
        assert len(result) == 2
        assert all(h["address"] != "addr1_b" for h in result)

    def test_exclude_multiple_addresses(self):
        holders = [
            {"address": "addr1_a", "quantity": 100},
            {"address": "addr1_b", "quantity": 200},
            {"address": "addr1_c", "quantity": 300},
        ]
        result = filter_holders(
            holders,
            exclude_script=False,
            exclude_addresses={"addr1_a", "addr1_c"},
        )
        assert len(result) == 1
        assert result[0]["address"] == "addr1_b"


class TestAllocateFlux:
    def test_basic_proportional_allocation(self):
        holders = [
            {"address": "a", "quantity": 600},
            {"address": "b", "quantity": 400},
        ]
        result = allocate_flux(AGENT, holders, bucket_base_units=1_000_000)

        # 600/1000 * 1M = 600K
        assert result[0]["flux_units"] == 600_000
        # 400/1000 * 1M = 400K
        assert result[1]["flux_units"] == 400_000

    def test_allocation_sums_correctly(self):
        holders = [
            {"address": "a", "quantity": 333},
            {"address": "b", "quantity": 333},
            {"address": "c", "quantity": 334},
        ]
        bucket = 1_000_000
        result = allocate_flux(AGENT, holders, bucket_base_units=bucket)
        total = sum(r["flux_units"] for r in result)
        # Floor rounding means total <= bucket
        assert total <= bucket

    def test_floor_rounding_produces_dust(self):
        """When balances don't divide evenly, floor creates dust."""
        holders = [
            {"address": "a", "quantity": 1},
            {"address": "b", "quantity": 1},
            {"address": "c", "quantity": 1},
        ]
        bucket = 10  # 10 / 3 = 3 per holder, 1 dust
        result = allocate_flux(AGENT, holders, bucket_base_units=bucket)
        total = sum(r["flux_units"] for r in result)
        assert total == 9  # 3 * 3 = 9, dust = 1

    def test_single_holder_gets_full_bucket(self):
        holders = [{"address": "a", "quantity": 100}]
        bucket = 999_999
        result = allocate_flux(AGENT, holders, bucket_base_units=bucket)
        assert result[0]["flux_units"] == bucket

    def test_empty_holders(self):
        result = allocate_flux(AGENT, [], bucket_base_units=1_000_000)
        assert result == []

    def test_zero_bucket(self):
        holders = [{"address": "a", "quantity": 100}]
        result = allocate_flux(AGENT, holders, bucket_base_units=0)
        assert result[0]["flux_units"] == 0

    def test_report_denominator_mode(self):
        """With report mode, denom is total supply, not eligible sum."""
        holders = [
            {"address": "a", "quantity": 500},
        ]
        # Eligible is 500 but total supply is 1000 → holder gets half the bucket
        result = allocate_flux(
            AGENT,
            holders,
            bucket_base_units=1_000_000,
            denominator_mode="report",
            total_supply_base=1000,
        )
        assert result[0]["flux_units"] == 500_000

    def test_eligible_denominator_distributes_full_bucket(self):
        holders = [
            {"address": "a", "quantity": 500},
        ]
        result = allocate_flux(
            AGENT,
            holders,
            bucket_base_units=1_000_000,
            denominator_mode="eligible",
        )
        assert result[0]["flux_units"] == 1_000_000

    def test_large_scale_allocation(self):
        """Test with realistic FLUX numbers."""
        bucket = 500_000_000_000_000  # 500T base units
        total_supply = 50_000_000  # 50M AGENT tokens
        holders = [
            {"address": f"addr_{i}", "quantity": total_supply // 100}
            for i in range(100)
        ]
        result = allocate_flux(
            AGENT, holders, bucket_base_units=bucket,
            denominator_mode="eligible",
        )
        total_alloc = sum(r["flux_units"] for r in result)
        # Should distribute nearly all, with minimal dust
        assert total_alloc <= bucket
        assert total_alloc >= bucket - 100  # dust is small

    def test_display_values_correct(self):
        holders = [{"address": "a", "quantity": 1_000_000}]
        result = allocate_flux(SHARDS, holders, bucket_base_units=6_000_000)
        assert result[0]["token_balance_display"] == 1.0  # 1M / 10^6
        assert result[0]["flux_display"] == 6.0  # 6M / 10^6


class TestFrankenAddressGrouping:
    """Franken address attack: two addresses share the same payment key hash
    but have different staking credentials.

    The allocation pipeline groups by payment key hash, so both addresses'
    balances should be combined into a single claim UTxO. Only the legitimate
    keyholder (who owns the payment signing key) can claim.
    """

    def _make_franken_pair(self):
        """Create two bech32 addresses with the same payment key hash
        but different staking key hashes."""
        from pycardano import Address, Network
        from pycardano.hash import VerificationKeyHash

        # Same payment key hash for both addresses
        pkh_bytes = bytes.fromhex(SAMPLE_PKH_1)
        payment_vkh = VerificationKeyHash(pkh_bytes)

        # Different staking key hashes
        stk_1 = VerificationKeyHash(bytes.fromhex(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            "aaaaaa"[:56]
        ))
        stk_2 = VerificationKeyHash(bytes.fromhex(
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            "bbbbbb"[:56]
        ))

        addr_legit = Address(
            payment_part=payment_vkh,
            staking_part=stk_1,
            network=Network.TESTNET,
        )
        addr_franken = Address(
            payment_part=payment_vkh,
            staking_part=stk_2,
            network=Network.TESTNET,
        )

        return str(addr_legit), str(addr_franken)

    def test_same_pkh_extracted(self):
        """Both addresses yield the same payment key hash."""
        from tools.cardano_utils import address_to_payment_key_hash

        addr_legit, addr_franken = self._make_franken_pair()
        assert addr_legit != addr_franken  # different bech32 strings
        pkh_1 = address_to_payment_key_hash(addr_legit)
        pkh_2 = address_to_payment_key_hash(addr_franken)
        assert pkh_1 == pkh_2 == SAMPLE_PKH_1

    def test_allocations_grouped_by_pkh(self):
        """Franken + legitimate addresses merge into one claim row."""
        from collections import defaultdict
        from tools.cardano_utils import address_to_payment_key_hash

        addr_legit, addr_franken = self._make_franken_pair()

        # Simulate two allocation rows (from two different addresses)
        allocs = [
            {"address": addr_legit, "flux_units": 600_000, "token_name": "AGENT",
             "token_balance_base": 600, "token_balance_display": 600, "flux_display": 0.6},
            {"address": addr_franken, "flux_units": 400_000, "token_name": "AGENT",
             "token_balance_base": 400, "token_balance_display": 400, "flux_display": 0.4},
        ]

        # Reproduce the grouping logic from run_snapshot_and_allocate
        pkh_totals: dict[str, dict] = defaultdict(lambda: {
            "addresses": set(), "flux_units": 0, "per_token": {},
        })
        for a in allocs:
            pkh = address_to_payment_key_hash(a["address"])
            assert pkh is not None
            entry = pkh_totals[pkh]
            entry["addresses"].add(a["address"])
            entry["flux_units"] += a["flux_units"]

        # Should produce ONE grouped entry
        assert len(pkh_totals) == 1
        assert SAMPLE_PKH_1 in pkh_totals
        entry = pkh_totals[SAMPLE_PKH_1]
        assert entry["flux_units"] == 1_000_000  # combined
        assert len(entry["addresses"]) == 2  # both addresses tracked

    def test_franken_address_cannot_change_datum(self):
        """The claim datum is keyed by payment key hash, not full address.
        A franken address holder without the payment signing key cannot claim."""
        from tools.cardano_utils import encode_claim_datum, decode_claim_datum

        # The datum encodes only the payment key hash
        datum_cbor = encode_claim_datum(SAMPLE_PKH_1)
        recovered_pkh = decode_claim_datum(datum_cbor.hex())

        # Datum does NOT include staking key — franken address is irrelevant
        assert recovered_pkh == SAMPLE_PKH_1

    def test_enterprise_address_same_pkh(self):
        """An enterprise address (no staking part) with the same payment key hash
        should also group with base addresses sharing that pkh."""
        from pycardano import Address, Network
        from pycardano.hash import VerificationKeyHash
        from tools.cardano_utils import address_to_payment_key_hash

        pkh_bytes = bytes.fromhex(SAMPLE_PKH_1)
        payment_vkh = VerificationKeyHash(pkh_bytes)

        # Enterprise address (no staking credential)
        enterprise_addr = Address(
            payment_part=payment_vkh,
            network=Network.TESTNET,
        )

        # Base address with staking credential
        stk = VerificationKeyHash(bytes.fromhex(
            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
            "cccccc"[:56]
        ))
        base_addr = Address(
            payment_part=payment_vkh,
            staking_part=stk,
            network=Network.TESTNET,
        )

        assert str(enterprise_addr) != str(base_addr)
        assert address_to_payment_key_hash(str(enterprise_addr)) == SAMPLE_PKH_1
        assert address_to_payment_key_hash(str(base_addr)) == SAMPLE_PKH_1


class TestAllocationsCSV:
    def test_csv_roundtrip(self, tmp_path):
        rows = [
            {
                "payment_key_hash_hex": SAMPLE_PKH_1,
                "addresses": ["addr1", "addr2"],
                "agent_balance_base": 100,
                "agent_flux_units": 50,
                "shards_balance_base": 200,
                "shards_flux_units": 100,
                "flux_total_units": 150,
                "flux_total_display": 0.00015,
            },
        ]
        csv_path = tmp_path / "alloc.csv"
        write_allocations_csv(rows, csv_path)
        assert csv_path.exists()

        import csv
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            read_rows = list(reader)
        assert len(read_rows) == 1
        assert read_rows[0]["payment_key_hash_hex"] == SAMPLE_PKH_1
        assert read_rows[0]["flux_total_units"] == "150"
