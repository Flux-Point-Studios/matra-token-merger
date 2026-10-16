"""Provider configuration must reach the HTTP wire without key-prefix inference."""
import ast
from pathlib import Path

import pytest
import responses

from tools import config
from tools.api_clients import BlockfrostClient


@pytest.fixture(autouse=True)
def provider_env(monkeypatch):
    for name in ("CHAIN_API_BASE_URL", "CHAIN_API_KEY_FILE", "BLOCKFROST_BASE_URL",
                 "BLOCKFROST_PROJECT_ID", "BLOCKFROST_PROJECT_ID_PREPROD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NETWORK", "preprod")


@pytest.mark.parametrize("network", ["mainnet", "preprod"])
def test_insight_default(monkeypatch, network):
    monkeypatch.setenv("NETWORK", network)
    assert BlockfrostClient("test").base_url == f"https://{network}.insight.iagon.com/api/v0"


@responses.activate
def test_override_reaches_rest_and_pycardano(monkeypatch):
    from pycardano import BlockFrostChainContext
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://provider.example/api/v0/")
    responses.get("https://provider.example/api/v0/blocks/latest", json={"height": 123})
    responses.get("https://provider.example/api/v0/epochs/latest", json={"epoch": 100})
    client = BlockfrostClient("opaque-test-key")
    assert client.get_latest_block()["height"] == 123
    ctx = BlockFrostChainContext(client.project_id, base_url=client.base_url.removesuffix("/v0"))
    assert ctx.api.url == "https://provider.example/api/v0"
    assert all(c.request.headers["project_id"] == "opaque-test-key" for c in responses.calls)


def test_legacy_url_override(monkeypatch):
    monkeypatch.setenv("BLOCKFROST_BASE_URL", "https://rollback.example/api/v0")
    assert BlockfrostClient("test").base_url == "https://rollback.example/api/v0"
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://preferred.example/api/v0")
    assert BlockfrostClient("test").base_url == "https://preferred.example/api/v0"


def test_key_file_overrides_legacy_env(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_text("opaque-test-key\n")
    monkeypatch.setenv("CHAIN_API_KEY_FILE", str(path))
    monkeypatch.setenv("BLOCKFROST_PROJECT_ID_PREPROD", "stale-test-key")
    assert BlockfrostClient().project_id == "opaque-test-key"


@pytest.mark.parametrize("contents", ["", "\n", "first\nsecond", "two words"])
def test_invalid_key_file_fails_closed(tmp_path, monkeypatch, contents):
    path = tmp_path / "key"
    path.write_text(contents)
    monkeypatch.setenv("CHAIN_API_KEY_FILE", str(path))
    with pytest.raises(ValueError, match="CHAIN_API_KEY_FILE"):
        BlockfrostClient()


def test_missing_key_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAIN_API_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(OSError):
        BlockfrostClient()


def test_all_operator_contexts_use_explicit_api_base():
    offenders = []
    for folder in ("tools", "scripts"):
        for path in Path(folder).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "BlockFrostChainContext":
                    base = next((kw.value for kw in node.keywords if kw.arg == "base_url"), None)
                    if base is None or ast.unparse(base) == "bf.base_url":
                        offenders.append(f"{path}:{node.lineno}")
    assert not offenders, offenders


def test_rehearsal_reads_use_configured_provider(monkeypatch):
    import httpx
    from scripts.preprod_chain_rehearsal import _bf_get
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://provider.example/api/v0")
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={"hash": "abc"})
    monkeypatch.setattr(httpx, "get", get)
    assert _bf_get("/txs/abc", "opaque-test-key").status_code == 200
    assert calls == ["https://provider.example/api/v0/txs/abc"]


@pytest.mark.parametrize("method,path,field,rows", [
    ("get_asset_addresses", "/assets/unit/addresses", "address", [
        {"address": "z", "quantity": "20"}, {"address": "a", "quantity": "1"}]),
    ("get_policy_assets", "/assets/policy/unit", "asset", [
        {"asset": "ff", "quantity": "1"}, {"asset": "aa", "quantity": "1"}]),
])
@responses.activate
def test_snapshot_lists_are_provider_order_independent(method, path, field, rows):
    responses.get("https://preprod.insight.iagon.com/api/v0" + path, json=rows)
    assert getattr(BlockfrostClient("test"), method)("unit") == sorted(rows, key=lambda r: r[field])


def test_preprod_setup_accepts_opaque_key(monkeypatch, tmp_path):
    import sys
    from scripts import preprod_setup
    monkeypatch.setattr(sys, "argv", ["preprod_setup", "--funder-skey", "unused"])
    monkeypatch.setattr(preprod_setup, "chain_api_key", lambda network: "opaque-test-key")
    class ReachedSetup(Exception):
        pass
    def stop(*args, **kwargs):
        raise ReachedSetup
    monkeypatch.setattr(Path, "mkdir", stop)
    with pytest.raises(ReachedSetup):
        preprod_setup.main()


def test_setup_preserves_file_credential_and_provider(tmp_path, monkeypatch):
    import json
    import sys
    from scripts import preprod_setup as setup
    work = tmp_path / "fixture"
    work.mkdir()
    (work / "setup_state.json").write_text(json.dumps({
        "funded": True, "funding_tx": "fund", "cmatra_policy": "cmatra",
        "cmatra_mint_tx": "mint", "nft_policy": "nft", "nft_mint_tx": "nftmint",
        "script_address": "pool", "pool_tx": "pooltx", "deadline_posix_ms": 1,
        "applied_blueprint": "blueprint",
    }))
    key = tmp_path / "insight.key"
    key.write_text("opaque-test-key")
    monkeypatch.setenv("CHAIN_API_KEY_FILE", str(key))
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://preprod.provider.example/api/v0")
    monkeypatch.setattr(setup, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(setup, "gen_or_load_key", lambda path: (None, None, "pkh", "address"))
    monkeypatch.setattr(sys, "argv", ["setup", "--funder-skey", "unused", "--work-dir", str(work)])
    setup.main()
    env = (tmp_path / "env.local").read_text()
    assert f"CHAIN_API_KEY_FILE={key}" in env
    assert "CHAIN_API_BASE_URL=https://preprod.provider.example/api/v0" in env
    assert "opaque-test-key" not in env
    assert "BLOCKFROST_PROJECT_ID" not in env
