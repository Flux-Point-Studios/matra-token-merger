"""Tests for tools.config — environment loading and constants."""

from tools.config import (
    AGENT,
    AGENT_DECIMALS,
    AGENT_UNIT,
    ATTESTOR_EMISSIONS_BASE,
    ECOSYSTEM_TREASURY_BASE,
    FLUX_DECIMALS,
    FLUX_MAX_SUPPLY_BASE,
    FLUX_MAX_SUPPLY_DISPLAY,
    LEGACY_TOKENS,
    LIQUIDITY_BASE,
    MERGE_TOKEN_SUPPLY_BASE,
    PUBLIC_POOL_BASE,
    SHARDS,
    SHARDS_DECIMALS,
    SHARDS_UNIT,
    STRATEGIC_BASE,
    TokenInfo,
    VALIDATOR_EMISSIONS_BASE,
    VALIDATOR_RESERVE_BASE,
)


class TestTokenInfo:
    def test_agent_unit_concatenation(self):
        assert AGENT.unit == AGENT_UNIT
        assert AGENT.unit == AGENT.policy_id + AGENT.asset_name_hex

    def test_shards_unit_concatenation(self):
        assert SHARDS.unit == SHARDS_UNIT
        assert SHARDS.unit == SHARDS.policy_id + SHARDS.asset_name_hex

    def test_agent_decimals(self):
        assert AGENT.decimals == 0

    def test_shards_decimals(self):
        assert SHARDS.decimals == 6

    def test_legacy_tokens_list(self):
        assert len(LEGACY_TOKENS) == 2
        assert AGENT in LEGACY_TOKENS
        assert SHARDS in LEGACY_TOKENS

    def test_token_info_frozen(self):
        import pytest
        with pytest.raises(AttributeError):
            AGENT.name = "changed"  # type: ignore


class TestMergeTokenConstants:
    def test_max_supply_base(self):
        assert FLUX_MAX_SUPPLY_BASE == 1_000_000_000_000_000  # 1e15

    def test_max_supply_display(self):
        assert FLUX_MAX_SUPPLY_DISPLAY == 1_000_000_000

    def test_base_from_display(self):
        assert FLUX_MAX_SUPPLY_BASE == FLUX_MAX_SUPPLY_DISPLAY * (10 ** FLUX_DECIMALS)

    def test_decimals(self):
        assert FLUX_DECIMALS == 6

    def test_public_pool_is_72_25_percent(self):
        """v5.1: Public Redemption Pool = 722.5M (72.25% of 1B)."""
        assert PUBLIC_POOL_BASE == 722_500_000_000_000

    def test_network_incentives_reserve_is_27_75_percent(self):
        """v5.1: Network Incentives Reserve = 277.5M (27.75% of 1B).

        Renamed from "validator reserve" — now covers 5 sub-buckets
        (validator emissions, attestor emissions, ecosystem, strategic,
        liquidity).
        """
        assert VALIDATOR_RESERVE_BASE == 277_500_000_000_000

    def test_pool_plus_reserve_equals_max(self):
        assert PUBLIC_POOL_BASE + VALIDATOR_RESERVE_BASE == MERGE_TOKEN_SUPPLY_BASE


class TestNetworkIncentivesSubBuckets:
    """v5.1: Network Incentives Reserve breaks into 5 sub-buckets."""

    def test_validator_emissions_is_115m(self):
        assert VALIDATOR_EMISSIONS_BASE == 115_000_000_000_000

    def test_attestor_emissions_is_65m(self):
        assert ATTESTOR_EMISSIONS_BASE == 65_000_000_000_000

    def test_ecosystem_treasury_is_40m(self):
        assert ECOSYSTEM_TREASURY_BASE == 40_000_000_000_000

    def test_strategic_is_30m(self):
        """30M cMATRA reserved for Strategic/Investor (Orion Fund target)."""
        assert STRATEGIC_BASE == 30_000_000_000_000

    def test_liquidity_is_27_5m(self):
        """27.5M = 5M bridge + 17.5M POL + 5M maker rebates."""
        assert LIQUIDITY_BASE == 27_500_000_000_000

    def test_sub_buckets_sum_to_reserve(self):
        """Sub-buckets must sum to exactly the Network Incentives Reserve."""
        total = (
            VALIDATOR_EMISSIONS_BASE
            + ATTESTOR_EMISSIONS_BASE
            + ECOSYSTEM_TREASURY_BASE
            + STRATEGIC_BASE
            + LIQUIDITY_BASE
        )
        assert total == VALIDATOR_RESERVE_BASE
        assert total == 277_500_000_000_000


class TestNftCollectionInfo:
    def test_flux_pass_policy_id(self):
        from tools.config import FLUX_PASS
        assert len(FLUX_PASS.policy_id) == 56

    def test_decimals_always_zero(self):
        from tools.config import NFT_COLLECTIONS
        for coll in NFT_COLLECTIONS:
            assert coll.decimals == 0

    def test_nft_collections_count(self):
        from tools.config import NFT_COLLECTIONS
        assert len(NFT_COLLECTIONS) == 5

    def test_all_merge_assets_count(self):
        from tools.config import ALL_MERGE_ASSETS
        assert len(ALL_MERGE_ASSETS) == 7

    def test_nft_collection_info_frozen(self):
        import pytest
        from tools.config import FLUX_PASS
        with pytest.raises(AttributeError):
            FLUX_PASS.name = "changed"  # type: ignore

    def test_unique_policy_ids(self):
        from tools.config import NFT_COLLECTIONS
        policy_ids = [c.policy_id for c in NFT_COLLECTIONS]
        assert len(set(policy_ids)) == len(policy_ids)

    def test_unique_names(self):
        from tools.config import NFT_COLLECTIONS, LEGACY_TOKENS
        all_names = [t.name for t in LEGACY_TOKENS] + [c.name for c in NFT_COLLECTIONS]
        assert len(set(all_names)) == len(all_names)


class TestFilterNftAssets:
    """Tests for CIP-68 aware NFT asset filtering."""

    def test_cip68_only_counts_user_tokens(self):
        from tools.config import filter_nft_assets
        # 56-char policy + CIP-68 prefixed asset names
        policy = "a" * 56
        assets = [
            {"asset": policy + "000de140aabb", "quantity": "1"},  # user token
            {"asset": policy + "000643b0aabb", "quantity": "1"},  # reference token
            {"asset": policy + "000de140ccdd", "quantity": "1"},  # user token
            {"asset": policy + "000643b0ccdd", "quantity": "1"},  # reference token
        ]
        result = filter_nft_assets(assets)
        assert len(result) == 2
        assert all("000de140" in a["asset"] for a in result)

    def test_non_cip68_counts_all_qty1(self):
        from tools.config import filter_nft_assets
        assets = [
            {"asset": "nft1", "quantity": "1"},
            {"asset": "nft2", "quantity": "1"},
            {"asset": "fungible1", "quantity": "5"},
        ]
        result = filter_nft_assets(assets)
        assert len(result) == 2

    def test_excludes_fungible_regardless(self):
        from tools.config import filter_nft_assets
        policy = "a" * 56
        assets = [
            {"asset": policy + "000de140aabb", "quantity": "1"},
            {"asset": policy + "000643b0aabb", "quantity": "1"},
            {"asset": policy + "somefungible", "quantity": "100"},
        ]
        result = filter_nft_assets(assets)
        assert len(result) == 1

    def test_cip68_mixed_with_non_cip68(self):
        """CIP-68 collection with extra non-prefixed assets: only user tokens."""
        from tools.config import filter_nft_assets
        policy = "a" * 56
        assets = [
            {"asset": policy + "000de140aabb", "quantity": "1"},  # user token
            {"asset": policy + "000643b0aabb", "quantity": "1"},  # reference token
            {"asset": policy + "otherthing01", "quantity": "1"},  # other qty=1
        ]
        result = filter_nft_assets(assets)
        # Has CIP-68 user tokens, so only those count
        assert len(result) == 1
        assert "000de140" in result[0]["asset"]

    def test_empty_assets(self):
        from tools.config import filter_nft_assets
        assert filter_nft_assets([]) == []
