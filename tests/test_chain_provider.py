"""Provider configuration preserves deployed defaults and validates opt-in endpoints."""
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
def test_blockfrost_default(monkeypatch, network):
    monkeypatch.setenv("NETWORK", network)
    assert BlockfrostClient("test").base_url == f"https://cardano-{network}.blockfrost.io/api/v0"


@responses.activate
def test_override_reaches_rest_and_pycardano(monkeypatch):
    from pycardano import BlockFrostChainContext
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://preprod.insight.iagon.com/api/v0/")
    responses.get("https://preprod.insight.iagon.com/api/v0/blocks/latest", json={"height": 123})
    responses.get("https://preprod.insight.iagon.com/api/v0/epochs/latest", json={"epoch": 100})
    client = BlockfrostClient("opaque-test-key")
    assert client.get_latest_block()["height"] == 123
    ctx = BlockFrostChainContext(client.project_id, base_url=client.base_url.removesuffix("/v0"))
    assert ctx.api.url == "https://preprod.insight.iagon.com/api/v0"
    assert all(c.request.headers["project_id"] == "opaque-test-key" for c in responses.calls)


def test_legacy_url_override(monkeypatch):
    monkeypatch.setenv("BLOCKFROST_BASE_URL", "https://rollback.example/api/v0")
    assert BlockfrostClient("test").base_url == "https://cardano-preprod.blockfrost.io/api/v0"
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://preprod.insight.iagon.com/api/v0")
    assert BlockfrostClient("test").base_url == "https://preprod.insight.iagon.com/api/v0"


def test_key_file_overrides_legacy_env(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_text("opaque-test-key\n")
    path.chmod(0o600)
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://preprod.insight.iagon.com/api/v0")
    monkeypatch.setenv("CHAIN_API_KEY_FILE", str(path))
    monkeypatch.setenv("BLOCKFROST_PROJECT_ID_PREPROD", "stale-test-key")
    assert BlockfrostClient().project_id == "opaque-test-key"


@pytest.mark.parametrize("contents", ["", "\n", "first\nsecond", "two words"])
def test_invalid_key_file_fails_closed(tmp_path, monkeypatch, contents):
    path = tmp_path / "key"
    path.write_text(contents)
    path.chmod(0o600)
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
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://preprod.insight.iagon.com/api/v0")
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={"hash": "abc"})
    monkeypatch.setattr(httpx, "get", get)
    assert _bf_get("/txs/abc", "opaque-test-key").status_code == 200
    assert calls == ["https://preprod.insight.iagon.com/api/v0/txs/abc"]


@pytest.mark.parametrize("method,path,field,rows", [
    ("get_asset_addresses", "/assets/unit/addresses", "address", [
        {"address": "z", "quantity": "20"}, {"address": "a", "quantity": "1"}]),
    ("get_policy_assets", "/assets/policy/unit", "asset", [
        {"asset": "ff", "quantity": "1"}, {"asset": "aa", "quantity": "1"}]),
])
@responses.activate
def test_snapshot_lists_are_provider_order_independent(method, path, field, rows):
    responses.get("https://cardano-preprod.blockfrost.io/api/v0" + path, json=rows)
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
    key.chmod(0o600)
    monkeypatch.setenv("CHAIN_API_KEY_FILE", str(key))
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://preprod.insight.iagon.com/api/v0")
    monkeypatch.setattr(setup, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(setup, "gen_or_load_key", lambda path: (None, None, "pkh", "address"))
    monkeypatch.setattr(sys, "argv", ["setup", "--funder-skey", "unused", "--work-dir", str(work)])
    setup.main()
    env = (work / "env.local").read_text()
    assert not (tmp_path / "env.local").exists()
    assert f"CHAIN_API_KEY_FILE={key}" in env
    assert "CHAIN_API_BASE_URL=https://preprod.insight.iagon.com/api/v0" in env
    assert "opaque-test-key" not in env
    assert "BLOCKFROST_PROJECT_ID" not in env


@pytest.mark.parametrize("network,legacy", [("mainnet", "https://cardano-mainnet.blockfrost.io/api"), ("mainnet", None), ("preprod", None)])
def test_deployed_env_shapes(monkeypatch, network, legacy):
    monkeypatch.setenv("NETWORK", network)
    monkeypatch.setenv("BLOCKFROST_PROJECT_ID", network + "-fixture")
    if legacy:
        monkeypatch.setenv("BLOCKFROST_BASE_URL", legacy)
    client = BlockfrostClient()
    assert client.base_url == f"https://cardano-{network}.blockfrost.io/api/v0"
    assert client.project_id == network + "-fixture"


@pytest.mark.parametrize("base", ["", "ftp://preprod.insight.iagon.com/api/v0", "https:///api/v0", "https://preprod.insight.iagon.com/api", "http://preprod.insight.iagon.com/api/v0", "https://mainnet.insight.iagon.com/api/v0", "https://cardano-mainnet.blockfrost.io/api/v0", "https://preprod.insight.iagon.com/api/v0?x=1", "https://preprod.insight.iagon.com/api/v0#x", "https://user@preprod.insight.iagon.com/api/v0"])
def test_invalid_override(monkeypatch, base):
    monkeypatch.setenv("CHAIN_API_BASE_URL", base)
    with pytest.raises(ValueError):
        config.chain_api_base_url("preprod")


def test_explicit_network_and_key_precedence(monkeypatch):
    monkeypatch.setenv("NETWORK", "mainnet")
    monkeypatch.setenv("BLOCKFROST_PROJECT_ID", "mainnet-generic")
    monkeypatch.setenv("BLOCKFROST_PROJECT_ID_PREPROD", "preprod-specific")
    assert config.chain_api_base_url("preprod") == "https://cardano-preprod.blockfrost.io/api/v0"
    assert config.chain_api_key("preprod") == "preprod-specific"
    assert config.chain_api_base_url("preview") == "https://cardano-preview.blockfrost.io/api/v0"


def test_preprod_rejects_wrong_legacy_key(monkeypatch):
    monkeypatch.setenv("BLOCKFROST_PROJECT_ID", "mainnet-fixture")
    with pytest.raises(ValueError, match="preprod"):
        config.chain_api_key("preprod")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700])
def test_key_file_permissions(tmp_path, monkeypatch, mode):
    path = tmp_path / "key"
    path.write_text("opaque-fixture")
    path.chmod(mode)
    monkeypatch.setenv("CHAIN_API_KEY_FILE", str(path))
    with pytest.raises(ValueError, match="CHAIN_API_KEY_FILE"):
        config.chain_api_key()


def test_key_file_owner(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_text("opaque-fixture")
    path.chmod(0o600)
    monkeypatch.setenv("CHAIN_API_KEY_FILE", str(path))
    monkeypatch.setattr(config.os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ValueError, match="CHAIN_API_KEY_FILE"):
        config.chain_api_key()


def test_explicit_client_values_win(monkeypatch):
    monkeypatch.setenv("BLOCKFROST_PROJECT_ID", "preprod-environment")
    client = BlockfrostClient("explicit-key", "https://mainnet.insight.iagon.com/api/v0")
    assert client.project_id == "explicit-key"
    assert client.base_url == "https://mainnet.insight.iagon.com/api/v0"


@responses.activate
@pytest.mark.parametrize("magic", [1, 764824073])
def test_loopback_network_attestation(monkeypatch, magic):
    base = "http://127.0.0.1:9999/api/v0"
    monkeypatch.setenv("CHAIN_API_BASE_URL", base)
    monkeypatch.setenv("BLOCKFROST_PROJECT_ID", "opaque-fixture")
    responses.get(base + "/genesis", json={"network_magic": magic})
    if magic == 1:
        assert config.chain_api_base_url("preprod") == base
    else:
        with pytest.raises(ValueError, match="network"):
            config.chain_api_base_url("preprod")
    assert len(responses.calls) == 1


def test_all_pycardano_context_sites_on_wire(monkeypatch):
    """Execute each production constructor expression with the real SDK."""
    from pycardano import BlockFrostChainContext
    from types import SimpleNamespace
    base = "https://preprod.insight.iagon.com/api/v0"
    monkeypatch.setenv("CHAIN_API_BASE_URL", base)
    count = 0
    for folder in ("tools", "scripts", "services"):
        for path in Path(folder).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "BlockFrostChainContext"):
                    continue
                # Only the credentials vary by caller; execute its actual base expression.
                keywords = {kw.arg: kw.value for kw in node.keywords}
                expression = ast.Expression(keywords["base_url"])
                url = eval(compile(expression, str(path), "eval"), {
                    "chain_api_base_url": config.chain_api_base_url,
                    "bf": SimpleNamespace(base_url=base),
                    "state": SimpleNamespace(bf=SimpleNamespace(base_url=base)),
                })
                with responses.RequestsMock() as mock:
                    mock.get(base + "/epochs/latest", json={"epoch": 100})
                    ctx = BlockFrostChainContext("opaque-fixture", base_url=url)
                    assert ctx.api.url == base, f"{path}:{node.lineno}"
                    assert mock.calls[0].request.url == base + "/epochs/latest"
                count += 1
    assert count >= 15


@pytest.mark.parametrize("network,legacy,local", [
    ("mainnet", "https://cardano-mainnet.blockfrost.io/api", False),
    ("mainnet", None, False), ("preprod", None, False),
    ("preprod", None, True),
])
def test_real_uvicorn_startup(tmp_path, network, legacy, local):
    import os
    import shutil
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    root = Path(__file__).resolve().parents[1]
    for folder in ("services", "tools"):
        shutil.copytree(root / folder, tmp_path / folder, ignore=shutil.ignore_patterns("__pycache__"))
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(tmp_path),
           "NETWORK": network, "BLOCKFROST_PROJECT_ID": network + "-fixture",
           "RATE_TABLE_PATH": str(root / "audit_pack/2026-04-19/rate_table_cmatra.json"),
           "REDEMPTION_PIN_PATH": str(root / "audit_pack/2026-09-27/redemption_pin.json"),
           "ADMIN_SKEY_PATH": str(tmp_path / "absent"), "COSIGNER_URL": ""}
    if legacy:
        env["BLOCKFROST_BASE_URL"] = legacy
    if local:
        (tmp_path / "env.local").write_text("NETWORK=mainnet\nCOSIGNER_URL=http://wrong.invalid\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = tmp_path / "uvicorn.log"
    with log.open("w") as out:
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "services.surrender_api:app",
                                 "--host", "127.0.0.1", "--port", str(port)],
                                cwd=tmp_path, env=env, stdout=out, stderr=out)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        assert response.status == 200
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail(log.read_text())
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)
    assert "Application startup complete" in log.read_text()


def test_unknown_network_is_refused():
    with pytest.raises(ValueError, match="network"):
        config.chain_api_base_url("not-a-cardano-network")


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_owner_only_key_modes_are_accepted(tmp_path, monkeypatch, mode):
    path = tmp_path / "key"
    path.write_text("opaque-fixture\n")
    path.chmod(mode)
    monkeypatch.setenv("CHAIN_API_KEY_FILE", str(path))
    monkeypatch.setenv("CHAIN_API_BASE_URL", "https://preprod.insight.iagon.com/api/v0")
    assert config.chain_api_key("preprod") == "opaque-fixture"


@responses.activate
@pytest.mark.parametrize("status,body", [(302, {}), (403, {}), (200, {}), (200, {"network_magic": True}), (200, {"network_magic": "1"})])
def test_unverifiable_network_refused(monkeypatch, status, body):
    base = "https://custom.example/api/v0"
    monkeypatch.setenv("CHAIN_API_BASE_URL", base)
    responses.get(base + "/genesis", status=status, json=body)
    with pytest.raises(ValueError, match="network"):
        config.chain_api_base_url("preprod")
