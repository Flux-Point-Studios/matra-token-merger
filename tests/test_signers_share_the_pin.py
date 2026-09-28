"""The co-signer and the surrender API hold each legacy unit to the same
remaining: each starts from the committed pin through the same loader, so
treasury units already in quarantine come off the waiver on both hosts.

Each service starts in a process of its own, configured the way its host is,
with throwaway keys and fresh ledgers, and reports the limits it loaded."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pycardano import PaymentSigningKey

from services.redemption_ledger import create_ledger
from tests.test_cosign_policy import ENV as POLICY_ENV
from tools.config import AGENT
from tools.process_surrender import load_redemption_pin

ROOT = Path(__file__).resolve().parent.parent
PIN_PATH = ROOT / "audit_pack/2026-09-27/redemption_pin.json"

COSIGNER = (
    "import json, services.cosigner_api as c; c.startup();"
    " print(json.dumps(dict(c.state.cfg.redemption_limits)))"
)
SURRENDER_API = (
    "import asyncio, json, services.surrender_api as a; asyncio.run(a.startup());"
    " print(json.dumps(dict(a.state.cosign_config.redemption_limits)))"
)


def _limits(code: str, env: dict[str, str]) -> dict[str, int]:
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ["PATH"], **POLICY_ENV, "REDEMPTION_PIN_PATH": str(PIN_PATH), **env},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_both_signers_load_the_same_remaining_for_every_unit(tmp_path):
    admin_1, admin_2 = PaymentSigningKey.generate(), PaymentSigningKey.generate()
    admin_1.save(str(tmp_path / "admin_1.skey"))
    admin_2.save(str(tmp_path / "admin_2.skey"))
    for ledger in ("surrender.sqlite3", "cosigned.sqlite3"):
        create_ledger(str(tmp_path / ledger))
    common = {"MAX_CMATRA_PER_DAY": str(25_000_000 * 10**6), "COSIGNER_API_SECRET": "t" * 43}

    cosigner = _limits(COSIGNER, {
        **common,
        "COSIGNER_SKEY_PATH": str(tmp_path / "admin_2.skey"),
        "COSIGNER_PRIMARY_ADMIN_PKH": admin_1.to_verification_key().hash().payload.hex(),
        "COSIGNER_LEDGER_PATH": str(tmp_path / "cosigned.sqlite3"),
        "BLOCKFROST_PROJECT_ID": "mainnet-test-project",
    })
    surrender_api = _limits(SURRENDER_API, {
        **common,
        "ADMIN_SKEY_PATH": str(tmp_path / "admin_1.skey"),
        "COSIGNER_URL": "http://cosigner.test",
        "COSIGNER_PKH": admin_2.to_verification_key().hash().payload.hex(),
        "SURRENDER_LEDGER_PATH": str(tmp_path / "surrender.sqlite3"),
    })

    assert cosigner == surrender_api == dict(load_redemption_pin(PIN_PATH).remaining)
    assert len(cosigner) == 851
    assert cosigner[AGENT.unit] == 460_538_701
