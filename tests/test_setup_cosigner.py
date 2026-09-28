"""services/deploy/setup-cosigner.sh writes every setting the co-signer needs,
on a new host and on a host an earlier version set up, and never prints or
replaces the API secret.

The script runs in a copy of services/deploy with docker stubbed: the stub
logs its arguments and runs the ledger's init command against the host
directory the compose file mounts at /app/data. The signing key is a
throwaway generated for the test."""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from pycardano import PaymentSigningKey

from tests.cosign_cases import ADMIN_1
from tests.test_cosign_policy import ENV as POLICY_ENV

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "services/deploy"

EXPORTS = {
    **{key: value for key, value in POLICY_ENV.items()
       if key not in ("NETWORK", "RATE_TABLE_PATH", "REDEMPTION_PIN_PATH")},
    "COSIGNER_PRIMARY_ADMIN_PKH": ADMIN_1.hex(),
    "MAX_CMATRA_PER_DAY": str(25_000_000 * 10**6),
    "BLOCKFROST_PROJECT_ID": "mainnet-test-project",
}

DOCKER_STUB = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
if [[ "$*" == *"services.redemption_ledger init /app/data/"* ]]; then
    cd "$REPO_ROOT" && exec python3 -m services.redemption_ledger init "$HOST_DATA/$(basename "${@: -1}")"
fi
"""


class Host:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "deploy"
        self.dir.mkdir()
        shutil.copy(DEPLOY / "setup-cosigner.sh", self.dir)
        keys = self.dir / "keys"
        keys.mkdir(mode=0o700)
        PaymentSigningKey.generate().save(str(keys / "admin_2.skey"))
        self.env_file = self.dir / ".env.cosigner"
        self.ledger = self.dir / "data/cosigned.sqlite3"
        self.docker_log = tmp_path / "docker.log"
        self.docker_log.touch()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        (bin_dir / "docker").write_text(DOCKER_STUB)
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"

    def write_env(self, text: str) -> None:
        self.env_file.write_text(text)
        self.env_file.chmod(0o600)

    def setup(self, exports: dict[str, str]) -> subprocess.CompletedProcess:
        env = {
            "PATH": self.path, "HOME": str(self.dir), "DOCKER_LOG": str(self.docker_log),
            "REPO_ROOT": str(ROOT), "HOST_DATA": str(self.ledger.parent), **exports,
        }
        return subprocess.run(
            ["bash", str(self.dir / "setup-cosigner.sh")], input="N\n", env=env,
            capture_output=True, text=True, timeout=120,
        )

    def settings(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.env_file.read_text().splitlines() if line)

    def ledger_inits(self) -> int:
        return self.docker_log.read_text().count("redemption_ledger init")

    def boot(self) -> subprocess.CompletedProcess:
        """Start the co-signer's startup check with the written settings, the
        container paths mapped to this host's key and ledger."""
        env = {
            "PATH": os.environ["PATH"], **self.settings(),
            "COSIGNER_SKEY_PATH": str(self.dir / "keys/admin_2.skey"),
            "COSIGNER_LEDGER_PATH": str(self.ledger),
        }
        return subprocess.run(
            [sys.executable, "-c", "import services.cosigner_api as c; c.startup()"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def _secret() -> str:
    return secrets.token_urlsafe(32)


def _earlier_version_env(secret: str) -> str:
    """What the previous version of the script wrote."""
    return f"COSIGNER_SKEY_PATH=/app/keys/admin_2.skey\nCOSIGNER_API_SECRET={secret}\nCOSIGNER_API_PORT=8421\n"


def test_a_host_set_up_by_the_earlier_version_gets_every_new_setting(host):
    secret = _secret()
    host.write_env(_earlier_version_env(secret))
    result = host.setup(EXPORTS)
    assert result.returncode == 0, result.stderr
    settings = host.settings()
    assert settings["COSIGNER_API_SECRET"] == secret
    assert {key: settings[key] for key in EXPORTS} == EXPORTS
    assert settings["COSIGNER_LEDGER_PATH"] == "/app/data/cosigned.sqlite3"
    assert settings["NETWORK"] == "mainnet"
    assert secret not in result.stdout + result.stderr
    assert "mainnet-test-project" not in result.stdout + result.stderr
    assert stat.S_IMODE(host.env_file.stat().st_mode) == 0o600
    # It never kept a ledger, so it gets its first one.
    assert host.ledger_inits() == 1
    assert stat.S_IMODE(host.ledger.parent.stat().st_mode) == 0o700
    booted = host.boot()
    assert booted.returncode == 0, booted.stderr
    assert "Co-signer ready" in booted.stderr


def test_running_it_again_changes_nothing(host):
    host.write_env(_earlier_version_env(_secret()))
    assert host.setup(EXPORTS).returncode == 0
    written = host.env_file.read_bytes()
    again = host.setup(EXPORTS)
    assert again.returncode == 0, again.stderr
    assert host.env_file.read_bytes() == written
    assert host.ledger_inits() == 1


def test_a_setting_already_in_the_file_is_kept(host):
    host.write_env(_earlier_version_env(_secret()) + "MAX_CMATRA_PER_DAY=7\n")
    assert host.setup(EXPORTS).returncode == 0
    assert host.settings()["MAX_CMATRA_PER_DAY"] == "7"


def test_a_file_without_a_final_newline_keeps_its_last_setting(host):
    secret = _secret()
    host.write_env(_earlier_version_env(secret).rstrip("\n"))
    assert host.setup(EXPORTS).returncode == 0
    assert host.settings()["COSIGNER_API_PORT"] == "8421"
    assert host.settings()["COSIGNER_API_SECRET"] == secret


def test_a_setting_neither_in_the_file_nor_exported_stops_it_before_any_change(host):
    host.write_env(_earlier_version_env(_secret()))
    before = host.env_file.read_bytes()
    missing = {key: value for key, value in EXPORTS.items()
               if key not in ("MAX_CMATRA_PER_DAY", "BLOCKFROST_PROJECT_ID")}
    result = host.setup(missing)
    assert result.returncode != 0
    assert "MAX_CMATRA_PER_DAY" in result.stderr and "BLOCKFROST_PROJECT_ID" in result.stderr
    assert host.env_file.read_bytes() == before
    assert host.ledger_inits() == 0
    assert sorted(p.name for p in host.dir.iterdir()) == [".env.cosigner", "keys", "setup-cosigner.sh"]


def test_a_new_host_gets_a_generated_secret_it_never_prints(host):
    result = host.setup(EXPORTS)
    assert result.returncode == 0, result.stderr
    settings = host.settings()
    assert len(settings["COSIGNER_API_SECRET"]) >= 32
    assert settings["COSIGNER_API_SECRET"] not in result.stdout + result.stderr
    assert stat.S_IMODE(host.env_file.stat().st_mode) == 0o600
    assert host.ledger_inits() == 1
    booted = host.boot()
    assert booted.returncode == 0, booted.stderr


def test_a_host_that_kept_a_ledger_is_never_given_a_new_one(host):
    host.write_env(_earlier_version_env(_secret()) + "COSIGNER_LEDGER_PATH=/app/data/cosigned.sqlite3\n")
    result = host.setup(EXPORTS)
    assert result.returncode != 0
    assert "Restore the ledger" in result.stderr
    assert host.ledger_inits() == 0
    assert not host.ledger.exists()
