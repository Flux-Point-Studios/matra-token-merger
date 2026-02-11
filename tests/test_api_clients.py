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
            "https://openapi.taptools.io/api/v1/token/price/ada",
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
