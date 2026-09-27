"""The co-signer service signs only what services.cosign_policy approves,
within a persisted 24-hour payout cap, behind a constant-time secret check.

Every key here is a throwaway generated for the test. The service's policy is
swapped for the mainnet one after startup so real surrenders (which name the
real admins as required signers) can be replayed against a throwaway signer."""

from __future__ import annotations

import hmac
import sqlite3

import nacl.signing
import pytest
from fastapi.testclient import TestClient
from pycardano import PaymentSigningKey

import services.cosigner_api as cosigner
from services.cosign_policy import SLOT_OFFSET_S, evaluate_surrender
from tests.cosign_cases import (
    ADMIN_1,
    BASE_T1,
    CASES,
    MAINNET,
    Scenario,
    golden_scenario,
)
from tests.test_cosign_policy import ENV as POLICY_ENV
from tests.test_cosign_policy import SURRENDERS

SECRET = "t" * 43
HEADERS = {"X-API-Secret": SECRET}


def _payout(s: Scenario) -> int:
    return evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, s.cfg).payout


def _request(s: Scenario) -> dict:
    return {
        "tx_cbor_hex": s.tx.hex(),
        "parent_bodies_hex": [p.hex() for p in s.parents],
        "language_views_hex": s.language_views.hex(),
    }


@pytest.fixture
def service(tmp_path, monkeypatch):
    sk = PaymentSigningKey.generate()
    skey = tmp_path / "throwaway.skey"
    sk.save(str(skey))
    env = {
        **POLICY_ENV,
        "COSIGNER_SKEY_PATH": str(skey),
        "COSIGNER_API_SECRET": SECRET,
        "COSIGNER_PRIMARY_ADMIN_PKH": ADMIN_1.hex(),
        "COSIGNER_LEDGER_PATH": str(tmp_path / "cosigned.sqlite3"),
        "MAX_CMATRA_PER_DAY": str(10**18),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    class Service:
        vkey = bytes(sk.to_verification_key().payload)
        ledger = tmp_path / "cosigned.sqlite3"

        clock = [0]

        def client(self, now_slot: int) -> TestClient:
            self.at(now_slot)
            monkeypatch.setattr(cosigner.time, "time", lambda: self.clock[0] + SLOT_OFFSET_S["mainnet"])
            return TestClient(cosigner.app)

        def at(self, now_slot: int) -> None:
            self.clock[0] = now_slot

        def start(self, client: TestClient) -> TestClient:
            client.__enter__()
            cosigner.state.cfg = MAINNET
            return client

        def rows(self) -> int:
            with sqlite3.connect(self.ledger) as conn:
                return conn.execute("SELECT COUNT(*) FROM cosigned").fetchone()[0]

    yield Service()


def test_health_needs_no_secret(service):
    with service.client(0) as client:
        body = client.get("/health").json()
    assert body["key_loaded"] is True


@pytest.mark.parametrize("headers", [{}, {"X-API-Secret": "wrong"}, {"X-API-Secret": SECRET + "x"}])
def test_requests_without_the_secret_are_forbidden(service, headers):
    s = golden_scenario(BASE_T1)
    with service.client(s.now_slot) as client:
        assert client.post("/cosign", json=_request(s), headers=headers).status_code == 403
    assert service.rows() == 0


def test_secret_is_compared_in_constant_time(service, monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(cosigner.hmac, "compare_digest", spy)
    with service.client(0) as client:
        client.post("/cosign", json={}, headers={"X-API-Secret": "wrong"})
    assert calls == [(b"wrong", SECRET.encode())]


def test_signs_a_real_surrender_it_has_verified(service):
    s = golden_scenario(BASE_T1)
    client = service.start(service.client(s.now_slot))
    try:
        resp = client.post("/cosign", json=_request(s), headers=HEADERS)
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    tx_hash = bytes.fromhex(body["tx_hash"])
    assert body["tx_hash"] == BASE_T1
    assert bytes.fromhex(body["vkey_hex"]) == service.vkey
    nacl.signing.VerifyKey(service.vkey).verify(tx_hash, bytes.fromhex(body["signature_hex"]))
    assert body["payout"] == _payout(s)
    assert service.rows() == 1


@pytest.mark.parametrize("name", [
    "admin_withdraw_redeemer",
    "quarantined_nft_under_a_foreign_policy",
    "paid_for_a_hundred_passes_while_quarantining_one",
    "pool_drained_to_a_third_output",
    "extra_mint",
])
def test_refuses_without_signing_or_recording(service, name):
    case = next(c for c in CASES if c.name == name)
    s = case.build()
    client = service.start(service.client(s.now_slot))
    try:
        resp = client.post("/cosign", json=_request(s), headers=HEADERS)
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == case.expected
    assert "signature_hex" not in resp.text
    assert service.rows() == 0


def _same_day_pair() -> tuple[Scenario, Scenario]:
    """Two real surrenders built less than an hour apart, in build order."""
    built = sorted((golden_scenario(h) for h in SURRENDERS), key=lambda s: s.now_slot)
    return next((a, b) for a, b in zip(built, built[1:]) if 0 < b.now_slot - a.now_slot < 3_600)


def test_daily_cap_is_persisted_across_restarts(service, monkeypatch):
    first, second = _same_day_pair()
    monkeypatch.setenv("MAX_CMATRA_PER_DAY", str(_payout(first) + _payout(second) - 1))

    client = service.start(service.client(first.now_slot))
    try:
        assert client.post("/cosign", json=_request(first), headers=HEADERS).status_code == 200
        service.at(second.now_slot)
        refused = client.post("/cosign", json=_request(second), headers=HEADERS)
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "daily_cap"
    finally:
        client.__exit__(None, None, None)

    client = service.start(service.client(second.now_slot))
    try:
        again = client.post("/cosign", json=_request(second), headers=HEADERS)
        assert again.json()["detail"]["code"] == "daily_cap"
        # Re-signing an already counted transaction does not count it twice.
        service.at(first.now_slot)
        assert client.post("/cosign", json=_request(first), headers=HEADERS).status_code == 200
    finally:
        client.__exit__(None, None, None)
    assert service.rows() == 1

    # A day later the first payout no longer counts against the cap.
    with sqlite3.connect(service.ledger) as conn:
        conn.execute("UPDATE cosigned SET signed_at = signed_at - 86401")
    client = service.start(service.client(second.now_slot))
    try:
        assert client.post("/cosign", json=_request(second), headers=HEADERS).status_code == 200
    finally:
        client.__exit__(None, None, None)
    assert service.rows() == 2


@pytest.mark.parametrize("missing", ["MAX_CMATRA_PER_DAY", "COSIGNER_PRIMARY_ADMIN_PKH",
                                     "COSIGNER_LEDGER_PATH", "SURRENDER_DEADLINE_POSIX_MS",
                                     "COSIGNER_SKEY_PATH"])
def test_refuses_to_start_without_its_configuration(service, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        with service.client(0):
            pass


def test_refuses_to_start_with_a_short_secret(service, monkeypatch):
    monkeypatch.setenv("COSIGNER_API_SECRET", "short")
    with pytest.raises(ValueError, match="COSIGNER_API_SECRET"):
        with service.client(0):
            pass


def test_policy_names_this_key_and_the_primary_admin(service):
    with service.client(0):
        admins = cosigner.state.cfg.admin_pkhs
    assert admins == {ADMIN_1, bytes.fromhex(cosigner.state.pkh_hex)}


def test_oversized_requests_are_refused_before_parsing(service):
    s = golden_scenario(BASE_T1)
    request = _request(s)
    request["parent_bodies_hex"] = request["parent_bodies_hex"] * 200
    with service.client(s.now_slot) as client:
        assert client.post("/cosign", json=request, headers=HEADERS).status_code == 422
    assert service.rows() == 0
