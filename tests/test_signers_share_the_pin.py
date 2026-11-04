"""The co-signer and the surrender API hold each legacy unit to the same
remaining, and refuse the same credentials: each starts from the committed pin
and deny-list through the same loaders, so a record of waived units already in
quarantine counts the same on both hosts, and neither starts without the list.

Each service starts in a process of its own, configured the way its host is,
with throwaway keys and fresh ledgers, and reports what it loaded."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pycardano import PaymentSigningKey

from services.redemption_ledger import create_ledger
from tests.test_cosign_policy import ENV as POLICY_ENV
from tests.cosign_cases import TEST_WEBHOOK
from tools.config import AGENT, T2_ADAM_PASS
from tools.process_surrender import load_redemption_pin

ROOT = Path(__file__).resolve().parent.parent
PIN_PATH = ROOT / "audit_pack/2026-09-27/redemption_pin.json"

DENYLIST_PATH = ROOT / "audit_pack/2026-11-01/denylist.json"
REPORT = (
    "print(json.dumps({'limits': dict(cfg.redemption_limits), 'denylist': cfg.denylist.sha256,"
    " 'denied': len(cfg.denylist.labels), 'prechecked': PRECHECKED, 'funded': dict(ledger.funded)}))"
)
COSIGNER = (
    "import json, services.cosigner_api as c; c.startup(); cfg = c.state.cfg; ledger = c.state.ledger;"
    " PRECHECKED = None;" + REPORT
)
SURRENDER_API = (
    "import asyncio, json, services.surrender_api as a; asyncio.run(a.startup());"
    " cfg = a.state.cosign_config; ledger = a.state.ledger; PRECHECKED = a.state.denylist.sha256;" + REPORT
)


class Hosts:
    """Each signer configured the way its host is, with throwaway keys and
    fresh ledgers."""

    def __init__(self, tmp_path: Path) -> None:
        admin_1, admin_2 = PaymentSigningKey.generate(), PaymentSigningKey.generate()
        admin_1.save(str(tmp_path / "admin_1.skey"))
        admin_2.save(str(tmp_path / "admin_2.skey"))
        for ledger in ("surrender.sqlite3", "cosigned.sqlite3"):
            create_ledger(str(tmp_path / ledger))
        common = {"MAX_CMATRA_PER_DAY": str(25_000_000 * 10**6), "COSIGNER_API_SECRET": "t" * 43,
                  "PAGER_DISCORD_WEBHOOK_URL": TEST_WEBHOOK}
        self.env = {
            COSIGNER: {
                **common,
                "COSIGNER_SKEY_PATH": str(tmp_path / "admin_2.skey"),
                "COSIGNER_PRIMARY_ADMIN_PKH": admin_1.to_verification_key().hash().payload.hex(),
                "COSIGNER_LEDGER_PATH": str(tmp_path / "cosigned.sqlite3"),
                "BLOCKFROST_PROJECT_ID": "mainnet-test-project",
                "PAGER_OUTBOX_PATH": str(tmp_path / "cosigner-pager.sqlite3"),
            },
            SURRENDER_API: {
                **common,
                "ADMIN_SKEY_PATH": str(tmp_path / "admin_1.skey"),
                "COSIGNER_URL": "http://cosigner.test",
                "COSIGNER_PKH": admin_2.to_verification_key().hash().payload.hex(),
                "SURRENDER_LEDGER_PATH": str(tmp_path / "surrender.sqlite3"),
                "PAGER_OUTBOX_PATH": str(tmp_path / "surrender-pager.sqlite3"),
            },
        }

    def start(self, code: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
            env={"PATH": os.environ["PATH"], **POLICY_ENV, "REDEMPTION_PIN_PATH": str(PIN_PATH),
                 **self.env[code], **env},
        )

    def loaded(self, code: str) -> dict:
        result = self.start(code)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.splitlines()[-1])


@pytest.fixture
def hosts(tmp_path):
    return Hosts(tmp_path)


def test_both_signers_load_the_same_remaining_for_every_unit(hosts):
    cosigner = hosts.loaded(COSIGNER)["limits"]
    surrender_api = hosts.loaded(SURRENDER_API)["limits"]
    assert cosigner == surrender_api == dict(load_redemption_pin(PIN_PATH).remaining)
    assert len(cosigner) == 851
    assert cosigner[AGENT.unit] == 444_803_187


def test_both_signers_hold_each_asset_to_what_the_pool_was_funded_for(hosts):
    cosigner, surrender_api = hosts.loaded(COSIGNER)["funded"], hosts.loaded(SURRENDER_API)["funded"]
    assert cosigner == surrender_api
    assert cosigner[T2_ADAM_PASS.policy_id] == 20


def test_both_signers_refuse_the_same_credentials(hosts):
    committed = hashlib.sha256(DENYLIST_PATH.read_bytes()).hexdigest()
    cosigner, surrender_api = hosts.loaded(COSIGNER), hosts.loaded(SURRENDER_API)
    assert cosigner["denylist"] == surrender_api["denylist"] == surrender_api["prechecked"] == committed
    assert cosigner["denied"] == surrender_api["denied"] == 61


@pytest.mark.parametrize("service", [COSIGNER, SURRENDER_API], ids=["cosigner", "surrender_api"])
def test_neither_signer_starts_without_its_denylist(hosts, tmp_path, service):
    result = hosts.start(service, DENYLIST_PATH=str(tmp_path / "absent.json"))
    assert result.returncode != 0
    assert "absent.json" in result.stderr


@pytest.mark.parametrize("service", [COSIGNER, SURRENDER_API], ids=["cosigner", "surrender_api"])
def test_neither_signer_starts_without_its_pager(hosts, service):
    result = hosts.start(service, PAGER_DISCORD_WEBHOOK_URL="")
    assert result.returncode != 0
    assert "PAGER_DISCORD_WEBHOOK_URL must be set" in result.stderr


@pytest.mark.parametrize("service", [COSIGNER, SURRENDER_API], ids=["cosigner", "surrender_api"])
def test_each_signer_logs_the_denylist_it_loaded(hosts, service):
    result = hosts.start(service)
    committed = hashlib.sha256(DENYLIST_PATH.read_bytes()).hexdigest()
    assert f"deny-list {committed}: 61 credentials denied, 2 allowed" in result.stderr
