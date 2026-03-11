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
        bucket = 6_000_000_000_000  # 6e12 base units = 6M display at 6 decimals
        result = allocate_flux(SHARDS, holders, bucket_base_units=bucket)
        assert result[0]["token_balance_display"] == 1.0  # 1M / 10^6 (SHARDS decimals)
        assert result[0]["flux_display"] == 6_000_000.0  # 6e12 / 10^6


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


class TestBuildCsvColumns:
    def test_fungible_only(self):
        from tools.snapshot_allocate_flux import build_csv_columns
        from tools.config import LEGACY_TOKENS
        cols = build_csv_columns(tokens=LEGACY_TOKENS, nft_collections=[])
        assert cols[0] == "payment_key_hash_hex"
        assert cols[1] == "addresses"
        assert "agent_balance_base" in cols
        assert "shards_flux_units" in cols
        assert cols[-2] == "flux_total_units"
        assert cols[-1] == "flux_total_display"
        assert len(cols) == 2 + 4 + 2  # header + 2 tokens * 2 cols + footer

    def test_with_nfts(self):
        from tools.snapshot_allocate_flux import build_csv_columns
        from tools.config import LEGACY_TOKENS, NFT_COLLECTIONS
        cols = build_csv_columns(tokens=LEGACY_TOKENS, nft_collections=NFT_COLLECTIONS)
        assert len(cols) == 2 + (7 * 2) + 2  # 18 columns
        assert "flux_pass_balance_base" in cols
        assert "flux_pass_flux_units" in cols
        assert "se_brawlers_balance_base" in cols
        assert "t2_adam_pass_flux_units" in cols

    def test_columns_end_with_totals(self):
        from tools.snapshot_allocate_flux import build_csv_columns
        from tools.config import LEGACY_TOKENS, NFT_COLLECTIONS
        cols = build_csv_columns(tokens=LEGACY_TOKENS, nft_collections=NFT_COLLECTIONS)
        assert cols[-2] == "flux_total_units"
        assert cols[-1] == "flux_total_display"


class TestNftHolders:
    def test_fetch_nft_holders_basic(self, mocker):
        from tools.snapshot_allocate_flux import fetch_nft_holders
        from tools.config import FLUX_PASS

        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [
            {"asset": "policy1nft1"},
            {"asset": "policy1nft2"},
        ]
        mock_bf.get_asset_addresses.side_effect = [
            [{"address": "addr1_alice", "quantity": "1"}],
            [{"address": "addr1_alice", "quantity": "1"}],
        ]

        holders = fetch_nft_holders(mock_bf, FLUX_PASS, resolve_scripts=False)
        assert len(holders) == 1  # aggregated by address
        assert holders[0]["quantity"] == 2

    def test_fetch_nft_holders_multiple_owners(self, mocker):
        from tools.snapshot_allocate_flux import fetch_nft_holders
        from tools.config import SE_BRAWLERS

        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [
            {"asset": "policy1nft1"},
            {"asset": "policy1nft2"},
            {"asset": "policy1nft3"},
        ]
        mock_bf.get_asset_addresses.side_effect = [
            [{"address": "addr1_alice", "quantity": "1"}],
            [{"address": "addr1_bob", "quantity": "1"}],
            [{"address": "addr1_alice", "quantity": "1"}],
        ]

        holders = fetch_nft_holders(mock_bf, SE_BRAWLERS, resolve_scripts=False)
        holders_dict = {h["address"]: h["quantity"] for h in holders}
        assert holders_dict["addr1_alice"] == 2
        assert holders_dict["addr1_bob"] == 1


class TestDatumPkhExtraction:
    def test_find_28_byte_field_direct(self):
        from tools.snapshot_allocate_flux import _find_28_byte_field
        pkh_bytes = bytes.fromhex(SAMPLE_PKH_1)
        assert _find_28_byte_field(pkh_bytes) == SAMPLE_PKH_1

    def test_find_28_byte_field_in_list(self):
        from tools.snapshot_allocate_flux import _find_28_byte_field
        pkh_bytes = bytes.fromhex(SAMPLE_PKH_1)
        obj = [42, pkh_bytes, b"short"]
        assert _find_28_byte_field(obj) == SAMPLE_PKH_1

    def test_find_28_byte_field_in_cbor_tag(self):
        import cbor2
        from tools.snapshot_allocate_flux import _find_28_byte_field
        pkh_bytes = bytes.fromhex(SAMPLE_PKH_1)
        obj = cbor2.CBORTag(121, [pkh_bytes])
        assert _find_28_byte_field(obj) == SAMPLE_PKH_1

    def test_find_28_byte_field_nested(self):
        import cbor2
        from tools.snapshot_allocate_flux import _find_28_byte_field
        pkh_bytes = bytes.fromhex(SAMPLE_PKH_1)
        obj = cbor2.CBORTag(121, [cbor2.CBORTag(0, [pkh_bytes, 1000])])
        # Default max_depth=3 can't reach 4 levels; use higher depth
        assert _find_28_byte_field(obj, max_depth=5) == SAMPLE_PKH_1

    def test_find_28_byte_field_wrong_length(self):
        from tools.snapshot_allocate_flux import _find_28_byte_field
        assert _find_28_byte_field(b"short") is None
        assert _find_28_byte_field(b"x" * 32) is None

    def test_find_28_byte_field_max_depth(self):
        from tools.snapshot_allocate_flux import _find_28_byte_field
        pkh_bytes = bytes.fromhex(SAMPLE_PKH_1)
        deep = [[[[pkh_bytes]]]]  # 4 levels deep
        assert _find_28_byte_field(deep, max_depth=3) is None

    def test_extract_pkh_from_datum(self):
        import cbor2
        from tools.snapshot_allocate_flux import _extract_pkh_from_datum
        pkh_bytes = bytes.fromhex(SAMPLE_PKH_1)
        datum_hex = cbor2.dumps(cbor2.CBORTag(121, [pkh_bytes])).hex()
        utxo = {"inline_datum": datum_hex}
        assert _extract_pkh_from_datum(utxo) == SAMPLE_PKH_1

    def test_extract_pkh_from_datum_no_datum(self):
        from tools.snapshot_allocate_flux import _extract_pkh_from_datum
        assert _extract_pkh_from_datum({}) is None

    def test_extract_pkh_from_datum_invalid_cbor(self):
        from tools.snapshot_allocate_flux import _extract_pkh_from_datum
        assert _extract_pkh_from_datum({"inline_datum": "deadbeef"}) is None


class TestThreeBucketReserve:
    """Tests for the 3-bucket model: claimants + team treasury + NFT reserve."""

    def test_reserve_addresses_carved_out(self, mocker):
        """Holdings at reserve addresses are subtracted from the bucket."""
        from tools.snapshot_allocate_flux import run_snapshot_and_allocate

        mock_bf = mocker.MagicMock()
        mock_bf.get_latest_block.return_value = {
            "hash": "aa" * 32, "height": 100, "time": 1000, "slot": 500,
        }
        # AGENT holders: treasury has 300/1000
        mock_bf.get_asset_addresses.return_value = [
            {"address": "addr1_treasury", "quantity": "300"},
            {"address": "addr1_alice", "quantity": "400"},
            {"address": "addr1_bob", "quantity": "300"},
        ]

        merge_report = {
            "tokens": {
                "AGENT": {
                    "flux_bucket_base_units": 1000,
                    "supply_base_units": 1000,
                },
            },
        }
        result = run_snapshot_and_allocate(
            mock_bf, merge_report,
            exclude_script=False,
            tokens=[AGENT],
            nft_collections=[],
            reserve_addresses={"addr1_treasury": "Treasury"},
        )
        summary = result["summary"]

        # Treasury carved out: 300/1000 * 1000 = 300
        assert summary["per_token"]["AGENT"]["team_reserve_base_units"] == 300
        assert summary["per_token"]["AGENT"]["eligible_bucket_base_units"] == 700

        # Reserve in summary
        assert summary["reserve"]["total_team_reserve_base"] == 300

        # distributed + reserve + dust == bucket
        totals = summary["totals"]
        assert totals["sum_equals_bucket_total"]

    def test_no_reserve_addresses_backward_compatible(self, mocker):
        """Without reserve_addresses, behavior is unchanged."""
        from tools.snapshot_allocate_flux import run_snapshot_and_allocate

        mock_bf = mocker.MagicMock()
        mock_bf.get_latest_block.return_value = {
            "hash": "aa" * 32, "height": 100, "time": 1000, "slot": 500,
        }
        mock_bf.get_asset_addresses.return_value = [
            {"address": "addr1_alice", "quantity": "500"},
            {"address": "addr1_bob", "quantity": "500"},
        ]

        merge_report = {
            "tokens": {
                "AGENT": {
                    "flux_bucket_base_units": 1000,
                    "supply_base_units": 1000,
                },
            },
        }
        result = run_snapshot_and_allocate(
            mock_bf, merge_report,
            exclude_script=False,
            tokens=[AGENT],
            nft_collections=[],
        )
        summary = result["summary"]
        assert summary["per_token"]["AGENT"]["team_reserve_base_units"] == 0
        assert summary["reserve"]["total_reserve_base"] == 0

    def test_nft_unresolved_reserve_ledger(self, mocker):
        """Unresolvable NFTs produce a per-asset reserve ledger."""
        from tools.snapshot_allocate_flux import fetch_nft_holders
        from tools.config import FLUX_PASS

        mock_bf = mocker.MagicMock()
        mock_bf.get_policy_assets.return_value = [
            {"asset": "nft1"}, {"asset": "nft2"}, {"asset": "nft3"},
        ]
        # nft1 -> script (unresolvable), nft2 -> alice, nft3 -> bob
        mock_bf.get_asset_addresses.side_effect = [
            [{"address": "addr1w_script", "quantity": "1"}],
            [{"address": "addr1_alice", "quantity": "1"}],
            [{"address": "addr1_bob", "quantity": "1"}],
        ]
        # Script resolution fails
        mock_bf.get_address_utxos.return_value = []

        mocker.patch(
            "tools.snapshot_allocate_flux.is_script_address",
            side_effect=lambda a: a.startswith("addr1w"),
        )

        unresolved: list = []
        holders = fetch_nft_holders(
            mock_bf, FLUX_PASS, resolve_scripts=True, unresolved_out=unresolved,
        )

        assert len(holders) == 2  # alice + bob
        assert len(unresolved) == 1
        assert unresolved[0]["asset_unit"] == "nft1"
        assert unresolved[0]["script_address"] == "addr1w_script"

    def test_three_bucket_invariant(self, mocker):
        """distributed + team_reserve + nft_reserve + dust == total supply."""
        from tools.snapshot_allocate_flux import run_snapshot_and_allocate
        from tools.config import FLUX_PASS

        mock_bf = mocker.MagicMock()
        mock_bf.get_latest_block.return_value = {
            "hash": "aa" * 32, "height": 100, "time": 1000, "slot": 500,
        }
        # AGENT: treasury holds 200/1000
        mock_bf.get_asset_addresses.return_value = [
            {"address": "addr1_treasury", "quantity": "200"},
            {"address": "addr1_alice", "quantity": "800"},
        ]
        # NFT collection: 3 on-chain, 1 unresolvable
        mock_bf.get_policy_assets.return_value = [
            {"asset": "nft1"}, {"asset": "nft2"}, {"asset": "nft3"},
        ]

        nft_addr_calls = iter([
            [{"address": "addr1w_script", "quantity": "1"}],
            [{"address": "addr1_alice", "quantity": "1"}],
            [{"address": "addr1_bob", "quantity": "1"}],
        ])
        mock_bf.get_asset_addresses.side_effect = lambda *a, **kw: next(nft_addr_calls)
        mock_bf.get_address_utxos.return_value = []

        mocker.patch(
            "tools.snapshot_allocate_flux.is_script_address",
            side_effect=lambda a: a.startswith("addr1w"),
        )
        mocker.patch(
            "tools.snapshot_allocate_flux.address_to_payment_key_hash",
            side_effect=lambda a: SAMPLE_PKH_1 if "alice" in a else "bb" * 14,
        )

        merge_report = {
            "tokens": {
                "FLUX_PASS": {
                    "flux_bucket_base_units": FLUX_MAX_SUPPLY_BASE,
                    "supply_base_units": 3,
                },
            },
        }
        result = run_snapshot_and_allocate(
            mock_bf, merge_report,
            exclude_script=False,
            tokens=[],
            nft_collections=[FLUX_PASS],
            reserve_addresses={"addr1_treasury": "Treasury"},
        )
        totals = result["summary"]["totals"]
        assert totals["sum_equals_bucket_total"]

        # NFT reserve should exist for 1 unresolvable NFT
        nft_res = result["summary"]["reserve"]["nft_conditional"]
        assert "FLUX_PASS" in nft_res
        assert nft_res["FLUX_PASS"]["unresolvable_count"] == 1
