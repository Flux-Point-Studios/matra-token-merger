"""Tests for tools.api_clients — retry logic and client methods."""

import pytest
import responses

from tools.api_clients import BlockfrostClient, TapToolsClient, _request_with_retry


class TestRetryLogic:
    @responses.activate
    def test_success_on_first_try(self):
        responses.add(responses.GET, "https://example.com/api", json={"ok": True})
        result = _request_with_retry("GET", "https://example.com/api", {})
        assert result == {"ok": True}

    @responses.activate
    def test_retry_on_429(self):
        responses.add(responses.GET, "https://example.com/api", status=429)
        responses.add(responses.GET, "https://example.com/api", json={"ok": True})
        result = _request_with_retry(
            "GET", "https://example.com/api", {}, max_retries=2,
        )
        assert result == {"ok": True}
        assert len(responses.calls) == 2

    @responses.activate
    def test_retry_on_500(self):
        responses.add(responses.GET, "https://example.com/api", status=500)
        responses.add(responses.GET, "https://example.com/api", json={"ok": True})
        result = _request_with_retry(
            "GET", "https://example.com/api", {}, max_retries=2,
        )
        assert result == {"ok": True}

    @responses.activate
    def test_exhausted_retries_raises(self):
        for _ in range(6):
            responses.add(responses.GET, "https://example.com/api", status=429)
        with pytest.raises(RuntimeError, match="Exhausted retries"):
            _request_with_retry(
                "GET", "https://example.com/api", {}, max_retries=5,
            )

    @responses.activate
    def test_non_retryable_error_raises(self):
        responses.add(responses.GET, "https://example.com/api", status=404)
        with pytest.raises(Exception):
            _request_with_retry("GET", "https://example.com/api", {})


class TestBlockfrostClient:
    @responses.activate
    def test_get_latest_block(self):
        responses.add(
            responses.GET,
            "https://cardano-mainnet.blockfrost.io/api/v0/blocks/latest",
            json={"hash": "abc", "height": 100},
        )
        client = BlockfrostClient(project_id="test")
        result = client.get_latest_block()
        assert result["hash"] == "abc"

    @responses.activate
    def test_get_asset_info(self):
        responses.add(
            responses.GET,
            "https://cardano-mainnet.blockfrost.io/api/v0/assets/unit123",
            json={"quantity": "1000000"},
        )
        client = BlockfrostClient(project_id="test")
        result = client.get_asset_info("unit123")
        assert result["quantity"] == "1000000"

    @responses.activate
    def test_pagination(self):
        base = "https://cardano-mainnet.blockfrost.io/api/v0/assets/u/addresses"

        # Page 1: full page
        responses.add(responses.GET, base, json=[{"address": f"a{i}"} for i in range(100)])
        # Page 2: partial page
        responses.add(responses.GET, base, json=[{"address": "last"}])

        client = BlockfrostClient(project_id="test")
        result = client.get_asset_addresses("u")
        assert len(result) == 101

    @responses.activate
    def test_empty_pagination(self):
        base = "https://cardano-mainnet.blockfrost.io/api/v0/assets/u/addresses"
        responses.add(responses.GET, base, json=[])

        client = BlockfrostClient(project_id="test")
        result = client.get_asset_addresses("u")
        assert result == []

    @responses.activate
    def test_project_id_header(self):
        responses.add(
            responses.GET,
            "https://cardano-mainnet.blockfrost.io/api/v0/blocks/latest",
            json={},
        )
        client = BlockfrostClient(project_id="my_project_id")
        client.get_latest_block()
        assert responses.calls[0].request.headers["project_id"] == "my_project_id"


class TestTapToolsClient:
    @responses.activate
    def test_get_token_pools(self):
        responses.add(
            responses.GET,
            "https://openapi.taptools.io/api/v1/token/pools",
            json=[{"pairID": "pool1", "adaLocked": "100000"}],
        )
        client = TapToolsClient(api_key="test")
        result = client.get_token_pools("unit1")
        assert len(result) == 1
        assert result[0]["pairID"] == "pool1"

    @responses.activate
    def test_get_ada_price(self):
        responses.add(
            responses.GET,
            "https://openapi.taptools.io/api/v1/token/quote",
            json={"price": "0.55"},
        )
        client = TapToolsClient(api_key="test")
        price = client.get_ada_price()
        assert price == pytest.approx(0.55)

    @responses.activate
    def test_api_key_header(self):
        responses.add(
            responses.GET,
            "https://openapi.taptools.io/api/v1/token/pools",
            json=[],
        )
        client = TapToolsClient(api_key="secret_key")
        client.get_token_pools("u")
        assert responses.calls[0].request.headers["x-api-key"] == "secret_key"


class TestBlockfrostPolicyAssets:
    @responses.activate
    def test_get_policy_assets_single_page(self):
        base = "https://cardano-mainnet.blockfrost.io/api/v0/assets/policy/abc123"
        responses.add(responses.GET, base, json=[
            {"asset": "abc123nft1", "quantity": "1"},
            {"asset": "abc123nft2", "quantity": "1"},
        ])
        client = BlockfrostClient(project_id="test")
        result = client.get_policy_assets("abc123")
        assert len(result) == 2

    @responses.activate
    def test_get_policy_assets_paginated(self):
        base = "https://cardano-mainnet.blockfrost.io/api/v0/assets/policy/abc123"
        responses.add(responses.GET, base, json=[
            {"asset": f"abc123nft{i}", "quantity": "1"} for i in range(100)
        ])
        responses.add(responses.GET, base, json=[
            {"asset": "abc123nft100", "quantity": "1"},
        ])
        client = BlockfrostClient(project_id="test")
        result = client.get_policy_assets("abc123")
        assert len(result) == 101

    @responses.activate
    def test_get_policy_assets_empty(self):
        base = "https://cardano-mainnet.blockfrost.io/api/v0/assets/policy/abc123"
        responses.add(responses.GET, base, json=[])
        client = BlockfrostClient(project_id="test")
        result = client.get_policy_assets("abc123")
        assert result == []


class TestTapToolsNft:
    @responses.activate
    def test_get_nft_collection_ohlcv(self):
        responses.add(
            responses.GET,
            "https://openapi.taptools.io/api/v1/nft/collection/ohlcv",
            json=[{"close": 50.0, "volume": 100}],
        )
        client = TapToolsClient(api_key="test")
        result = client.get_nft_collection_ohlcv("policy123", "1d", 7)
        assert len(result) == 1
        assert result[0]["close"] == 50.0

    @responses.activate
    def test_get_nft_collection_stats(self):
        responses.add(
            responses.GET,
            "https://openapi.taptools.io/api/v1/nft/collection/stats",
            json={"floor": 45.0, "supply": 1000, "owners": 500},
        )
        client = TapToolsClient(api_key="test")
        result = client.get_nft_collection_stats("policy123")
        assert result["floor"] == 45.0
        assert result["supply"] == 1000

    @responses.activate
    def test_get_nft_collection_holders_top(self):
        responses.add(
            responses.GET,
            "https://openapi.taptools.io/api/v1/nft/collection/holders/top",
            json=[
                {"address": "addr1_whale", "quantity": 50},
                {"address": "addr1_holder", "quantity": 10},
            ],
        )
        client = TapToolsClient(api_key="test")
        result = client.get_nft_collection_holders_top("policy123")
        assert len(result) == 2
