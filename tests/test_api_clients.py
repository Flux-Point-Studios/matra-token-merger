"""Tests for tools.api_clients — retry logic and client methods."""

import json

import pytest
import requests
import responses
from pycardano import Network

from tools.api_clients import (
    BlockfrostClient,
    BlockfrostUnavailable,
    EvaluationFailed,
    TapToolsClient,
    _request_with_retry,
    chain_context,
)

BASE = "https://mainnet.insight.iagon.com/api/v0"
PREPROD_BASE = "https://preprod.insight.iagon.com/api/v0"


@pytest.fixture(autouse=True)
def _mainnet_insight(monkeypatch):
    """A client made without a base URL reads Insight's mainnet API."""
    monkeypatch.delenv("CHAIN_API_BASE_URL", raising=False)
    monkeypatch.setattr("tools.api_clients.NETWORK", "mainnet")


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    """Every wait the retry loop asks for, instead of sleeping."""
    waits: list[float] = []
    monkeypatch.setattr("tools.api_clients.time.sleep", waits.append)
    return waits


class TestRetryLogic:
    @responses.activate
    def test_success_on_first_try(self):
        responses.add(responses.GET, "https://example.com/api", json={"ok": True})
        result = _request_with_retry("GET", "https://example.com/api", {})
        assert result == {"ok": True}

    @responses.activate
    def test_retry_on_429(self, slept):
        responses.add(responses.GET, "https://example.com/api", status=429)
        responses.add(responses.GET, "https://example.com/api", json={"ok": True})
        result = _request_with_retry(
            "GET", "https://example.com/api", {}, max_retries=2,
        )
        assert result == {"ok": True}
        assert len(responses.calls) == 2

    @responses.activate
    def test_retry_on_500(self, slept):
        responses.add(responses.GET, "https://example.com/api", status=500)
        responses.add(responses.GET, "https://example.com/api", json={"ok": True})
        result = _request_with_retry(
            "GET", "https://example.com/api", {}, max_retries=2,
        )
        assert result == {"ok": True}

    @responses.activate
    def test_exhausted_retries_raises(self, slept):
        for _ in range(6):
            responses.add(responses.GET, "https://example.com/api", status=429)
        with pytest.raises(BlockfrostUnavailable, match="Exhausted retries"):
            _request_with_retry(
                "GET", "https://example.com/api", {}, max_retries=5,
            )

    @responses.activate
    def test_non_retryable_error_raises(self):
        responses.add(responses.GET, "https://example.com/api", status=404)
        with pytest.raises(Exception):
            _request_with_retry("GET", "https://example.com/api", {})

    @responses.activate
    def test_without_retry_after_the_waits_back_off(self, slept):
        for _ in range(6):
            responses.add(responses.GET, "https://example.com/api", status=503)
        with pytest.raises(BlockfrostUnavailable):
            _request_with_retry("GET", "https://example.com/api", {}, max_retries=5)
        assert slept == [1, 1.5, 2.25, 3.375, 5.0625]
        assert len(responses.calls) == 6

    @responses.activate
    def test_the_wait_a_busy_api_asks_for_is_honoured(self, slept):
        responses.add(responses.GET, "https://example.com/api", status=429, headers={"Retry-After": "4"})
        responses.add(responses.GET, "https://example.com/api", status=503, headers={"Retry-After": "0"})
        responses.add(responses.GET, "https://example.com/api", json={"ok": True})
        assert _request_with_retry("GET", "https://example.com/api", {}) == {"ok": True}
        assert slept == [4, 0]

    @responses.activate
    def test_a_wait_past_the_retry_budget_gives_up_at_once(self, slept):
        """A spent daily credit allowance answers 429 until midnight UTC; a
        caller waiting on its answer must not wait that long."""
        responses.add(responses.GET, "https://example.com/api", status=429, headers={"Retry-After": "3600"})
        with pytest.raises(BlockfrostUnavailable, match="3600"):
            _request_with_retry("GET", "https://example.com/api", {})
        assert slept == []
        assert len(responses.calls) == 1

    @responses.activate
    def test_the_waits_together_stay_within_the_retry_budget(self, slept):
        for _ in range(6):
            responses.add(responses.GET, "https://example.com/api", status=429, headers={"Retry-After": "8"})
        with pytest.raises(BlockfrostUnavailable):
            _request_with_retry("GET", "https://example.com/api", {}, max_retries=5)
        assert slept == [8]
        assert len(responses.calls) == 2

    @responses.activate
    def test_an_unreadable_retry_after_falls_back_to_the_backoff(self, slept):
        responses.add(responses.GET, "https://example.com/api", status=429,
                      headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        responses.add(responses.GET, "https://example.com/api", json={"ok": True})
        assert _request_with_retry("GET", "https://example.com/api", {}) == {"ok": True}
        assert slept == [1]


class TestDefaultProvider:
    def test_a_client_reads_the_configured_api_and_key_when_made(self, tmp_path, monkeypatch):
        key_file = tmp_path / "chain-api.key"
        key_file.write_text("k3y\n")
        monkeypatch.setenv("CHAIN_API_KEY_FILE", str(key_file))
        client = BlockfrostClient()
        assert client.base_url == BASE
        assert client.project_id == "k3y"

    @responses.activate
    def test_the_key_authenticates_every_call(self, tmp_path, monkeypatch):
        key_file = tmp_path / "chain-api.key"
        key_file.write_text("k3y")
        monkeypatch.setenv("CHAIN_API_KEY_FILE", str(key_file))
        responses.add(responses.GET, f"{BASE}/blocks/latest", json={})
        BlockfrostClient().get_latest_block()
        assert responses.calls[0].request.headers["project_id"] == "k3y"


class TestTxInBlock:
    @responses.activate
    def test_a_transaction_the_api_knows_is_in_a_block(self):
        responses.add(responses.GET, f"{BASE}/txs/ab12", json={"slot": 42})
        assert BlockfrostClient("k3y", BASE).tx_in_block("ab12") is True

    @responses.activate
    def test_a_transaction_the_api_does_not_know_is_not_yet(self):
        responses.add(responses.GET, f"{BASE}/txs/ab12", status=404, json={"status_code": 404})
        assert BlockfrostClient("k3y", BASE).tx_in_block("ab12") is False

    @responses.activate
    def test_an_api_that_cannot_answer_is_not_taken_for_no(self, slept):
        for _ in range(6):
            responses.add(responses.GET, f"{BASE}/txs/ab12", status=502)
        with pytest.raises(BlockfrostUnavailable):
            BlockfrostClient("k3y", BASE).tx_in_block("ab12")

    @responses.activate
    def test_a_refusal_is_not_taken_for_no(self):
        responses.add(responses.GET, f"{BASE}/txs/ab12", status=403, json={"status_code": 403})
        with pytest.raises(requests.HTTPError):
            BlockfrostClient("k3y", BASE).tx_in_block("ab12")


EPOCH = {"epoch": 600, "start_time": 1_760_000_000, "end_time": 4_102_444_800}


class TestChainContext:
    @pytest.mark.parametrize("base, network", [(BASE, Network.MAINNET), (PREPROD_BASE, Network.TESTNET)])
    @responses.activate
    def test_pycardano_asks_the_clients_api(self, base, network):
        """blockfrost-python appends the API version itself; a base URL that
        already carries it would ask .../api/v0/v0/..."""
        responses.add(responses.GET, f"{base}/epochs/latest", json=EPOCH)
        context = chain_context(BlockfrostClient("k3y", base))
        assert context.network == network
        request = responses.calls[0].request
        assert request.url == f"{base}/epochs/latest"
        assert request.headers["project_id"] == "k3y"


EVALUATED = {
    "type": "jsonwsp/response", "version": "1.0", "servicename": "ogmios",
    "methodname": "EvaluateTx",
    "result": {"EvaluationResult": {"spend:0": {"memory": 85058, "steps": 29010333}}},
}


class TestEvaluate:
    @responses.activate
    def test_the_transaction_goes_as_cbor_hex_and_its_units_come_back(self):
        responses.add(responses.POST, f"{BASE}/utils/txs/evaluate", json=EVALUATED)
        units = BlockfrostClient("k3y", BASE).evaluate_tx(bytes.fromhex("84a0a0f5f6"))
        assert units == {"spend:0": {"memory": 85058, "steps": 29010333}}
        request = responses.calls[0].request
        body = request.body.decode() if isinstance(request.body, bytes) else request.body
        assert body == "84a0a0f5f6"
        assert request.headers["Content-Type"] == "application/cbor"
        assert request.headers["project_id"] == "k3y"

    @responses.activate
    def test_a_busy_evaluator_is_asked_again_after_the_wait_it_names(self, slept):
        """Insight evaluates one transaction at a time per account."""
        url = f"{BASE}/utils/txs/evaluate"
        responses.add(responses.POST, url, status=429, headers={"Retry-After": "1"})
        responses.add(responses.POST, url, status=503, headers={"Retry-After": "2"})
        responses.add(responses.POST, url, json=EVALUATED)
        assert BlockfrostClient("k3y", BASE).evaluate_tx(b"\x84") == EVALUATED["result"]["EvaluationResult"]
        assert slept == [1, 2]
        assert len(responses.calls) == 3

    @responses.activate
    def test_a_transaction_the_api_rejects_is_not_asked_again(self):
        responses.add(responses.POST, f"{BASE}/utils/txs/evaluate", status=400,
                      json={"status_code": 400, "error": "Bad Request", "message": "{}"})
        with pytest.raises(requests.HTTPError):
            BlockfrostClient("k3y", BASE).evaluate_tx(b"\x84")
        assert len(responses.calls) == 1

    @pytest.mark.parametrize("answer", [
        {"type": "jsonwsp/response", "result": {"EvaluationFailure": {"ScriptFailures": {"spend:0": []}}}},
        {"type": "jsonwsp/fault", "fault": {"code": "client", "string": "failed to decode payload"}},
        {"result": {"EvaluationResult": {"spend:0": {"memory": 1, "cpu": 2}}}},
        {"result": {"EvaluationResult": {"spend:0": {"memory": "1", "steps": 2}}}},
        {"result": {"EvaluationResult": {"spend:0": {"memory": True, "steps": 2}}}},
        {"result": {"EvaluationResult": []}},
        {"result": None},
        [],
    ], ids=["failure", "fault", "v6-names", "string", "bool", "list", "null", "not-an-object"])
    @responses.activate
    def test_anything_but_units_for_each_redeemer_fails(self, answer):
        responses.add(responses.POST, f"{BASE}/utils/txs/evaluate", json=answer)
        with pytest.raises(EvaluationFailed) as failure:
            BlockfrostClient("k3y", BASE).evaluate_tx(b"\x84")
        assert json.dumps(answer)[:40] in str(failure.value)


class TestBlockfrostClient:
    @responses.activate
    def test_get_latest_block(self):
        responses.add(
            responses.GET,
            f"{BASE}/blocks/latest",
            json={"hash": "abc", "height": 100},
        )
        client = BlockfrostClient(project_id="test")
        result = client.get_latest_block()
        assert result["hash"] == "abc"

    @responses.activate
    def test_get_asset_info(self):
        responses.add(
            responses.GET,
            f"{BASE}/assets/unit123",
            json={"quantity": "1000000"},
        )
        client = BlockfrostClient(project_id="test")
        result = client.get_asset_info("unit123")
        assert result["quantity"] == "1000000"

    @responses.activate
    def test_get_tx_cbor_returns_the_raw_transaction_bytes(self):
        responses.add(
            responses.GET,
            f"{BASE}/txs/ab12/cbor",
            json={"cbor": "84a0a0f5f6"},
        )
        client = BlockfrostClient(project_id="test")
        assert client.get_tx_cbor("ab12") == bytes.fromhex("84a0a0f5f6")

    @responses.activate
    def test_get_tx(self):
        responses.add(
            responses.GET,
            f"{BASE}/txs/ab12",
            json={"slot": 42},
        )
        client = BlockfrostClient(project_id="test")
        assert client.get_tx("ab12")["slot"] == 42

    @responses.activate
    def test_get_address_transactions_pages_oldest_first(self):
        base = f"{BASE}/addresses/addr1x/transactions"
        responses.add(responses.GET, base, json=[{"tx_hash": f"t{i}"} for i in range(100)])
        responses.add(responses.GET, base, json=[{"tx_hash": "last"}])
        client = BlockfrostClient(project_id="test")
        rows = client.get_address_transactions("addr1x")
        assert len(rows) == 101
        assert all("order=asc" in call.request.url for call in responses.calls)

    @responses.activate
    def test_get_asset_history_pages_oldest_first(self):
        base = f"{BASE}/assets/u1/history"
        responses.add(responses.GET, base, json=[{"tx_hash": f"t{i}"} for i in range(100)])
        responses.add(responses.GET, base, json=[{"tx_hash": "last", "action": "burned"}])
        client = BlockfrostClient(project_id="test")
        rows = client.get_asset_history("u1")
        assert len(rows) == 101 and rows[-1]["action"] == "burned"
        assert all("order=asc" in call.request.url for call in responses.calls)

    @responses.activate
    def test_get_epoch_parameters(self):
        responses.add(
            responses.GET,
            f"{BASE}/epochs/640/parameters",
            json={"epoch": 640},
        )
        client = BlockfrostClient(project_id="test")
        assert client.get_epoch_parameters(640)["epoch"] == 640

    @responses.activate
    def test_pagination(self):
        base = f"{BASE}/assets/u/addresses"

        # Page 1: full page
        responses.add(responses.GET, base, json=[{"address": f"a{i}"} for i in range(100)])
        # Page 2: partial page
        responses.add(responses.GET, base, json=[{"address": "last"}])

        client = BlockfrostClient(project_id="test")
        result = client.get_asset_addresses("u")
        assert len(result) == 101

    @responses.activate
    def test_empty_pagination(self):
        base = f"{BASE}/assets/u/addresses"
        responses.add(responses.GET, base, json=[])

        client = BlockfrostClient(project_id="test")
        result = client.get_asset_addresses("u")
        assert result == []

    @pytest.mark.parametrize("page", [1, 2])
    @pytest.mark.parametrize("answer", [
        {"body": "null", "content_type": "application/json"}, {"json": {}}, {"json": ""},
    ], ids=["null", "object", "string"])
    @responses.activate
    def test_a_page_that_is_not_a_list_is_unavailable(self, page, answer):
        """Only an empty or short list ends a listing; any other answer would
        cut it short unnoticed."""
        base = f"{BASE}/assets/u/addresses"
        if page == 2:
            responses.add(responses.GET, base, json=[{"address": f"a{i}"} for i in range(100)])
        responses.add(responses.GET, base, **answer)
        client = BlockfrostClient(project_id="test")
        with pytest.raises(BlockfrostUnavailable, match=f"page {page}"):
            client.get_asset_addresses("u")

    @responses.activate
    def test_project_id_header(self):
        responses.add(
            responses.GET,
            f"{BASE}/blocks/latest",
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
        base = f"{BASE}/assets/policy/abc123"
        responses.add(responses.GET, base, json=[
            {"asset": "abc123nft1", "quantity": "1"},
            {"asset": "abc123nft2", "quantity": "1"},
        ])
        client = BlockfrostClient(project_id="test")
        result = client.get_policy_assets("abc123")
        assert len(result) == 2

    @responses.activate
    def test_get_policy_assets_paginated(self):
        base = f"{BASE}/assets/policy/abc123"
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
        base = f"{BASE}/assets/policy/abc123"
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
