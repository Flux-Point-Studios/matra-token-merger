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
import requests
from fastapi.testclient import TestClient
from pycardano import PaymentSigningKey

import services.cosigner_api as cosigner
from services.cosign_policy import SLOT_OFFSET_S, decode, evaluate_surrender, split_tx
from services.redemption_ledger import create_ledger
from tests.cosign_cases import (
    ADMIN_1,
    BASE_FUNGIBLE,
    BASE_T1,
    CASES,
    CHANGE,
    CMATRA_NAME,
    CMATRA_POLICY,
    CONTINUATION,
    MAINNET,
    MAINNET_BEFORE_SURRENDERS,
    Draft,
    FakeChain,
    Scenario,
    _add_asset,
    _add_coin,
    another_pool_utxo,
    blake,
    golden_scenario,
    ledger_refusing_writes,
    throwaway_key,
    without_claimant_witness,
)
from tests.cbor_encode import encode
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
        "BLOCKFROST_PROJECT_ID": "mainnet-test-project",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    create_ledger(env["COSIGNER_LEDGER_PATH"])

    class Service:
        vkey = bytes(sk.to_verification_key().payload)
        ledger = tmp_path / "cosigned.sqlite3"
        chain = FakeChain.as_at_the_golden_builds()

        clock = [0]

        def client(self, now_slot: int) -> TestClient:
            self.at(now_slot)
            monkeypatch.setattr(cosigner.time, "time", lambda: self.clock[0] + SLOT_OFFSET_S["mainnet"])
            return TestClient(cosigner.app)

        def at(self, now_slot: int) -> None:
            self.clock[0] = now_slot

        def start(self, client: TestClient, pinned: bool = False) -> TestClient:
            """``pinned``: the committed pin, which counts every landed
            surrender; otherwise its limits before any surrender landed."""
            client.__enter__()
            cosigner.state.cfg = MAINNET if pinned else MAINNET_BEFORE_SURRENDERS
            cosigner.state.chain = self.chain
            return client

        def rows(self) -> int:
            with sqlite3.connect(self.ledger) as conn:
                return conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]

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
        conn.execute("UPDATE approvals SET signed_at = signed_at - 86401")
    client = service.start(service.client(second.now_slot))
    try:
        assert client.post("/cosign", json=_request(second), headers=HEADERS).status_code == 200
    finally:
        client.__exit__(None, None, None)
    assert service.rows() == 2


@pytest.mark.parametrize("missing", ["MAX_CMATRA_PER_DAY", "COSIGNER_PRIMARY_ADMIN_PKH",
                                     "COSIGNER_LEDGER_PATH", "SURRENDER_DEADLINE_POSIX_MS",
                                     "COSIGNER_SKEY_PATH", "BLOCKFROST_PROJECT_ID"])
def test_refuses_to_start_without_its_configuration(service, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        with service.client(0):
            pass


def test_refuses_to_start_without_its_ledger(service, tmp_path, monkeypatch):
    """A ledger that went missing would forget every approval, so the
    service never starts on a fresh one it made itself."""
    absent = tmp_path / "moved-away.sqlite3"
    monkeypatch.setenv("COSIGNER_LEDGER_PATH", str(absent))
    with pytest.raises(FileNotFoundError, match="redemption_ledger init"):
        with service.client(0):
            pass
    assert not absent.exists()


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


@pytest.mark.parametrize("field", ["tx_cbor_hex", "parent_bodies_hex", "language_views_hex"])
def test_odd_length_hex_is_refused_as_a_malformed_request(service, field):
    s = golden_scenario(BASE_T1)
    request = _request(s)
    if field == "parent_bodies_hex":
        request[field] = [request[field][0] + "0", *request[field][1:]]
    else:
        request[field] += "0"
    client = service.start(service.client(s.now_slot))
    try:
        resp = TestClient(cosigner.app, raise_server_exceptions=False).post(
            "/cosign", json=request, headers=HEADERS)
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 422, resp.text
    assert "signature_hex" not in resp.text
    assert service.rows() == 0


def _post(service, s: Scenario, pinned: bool = False, raise_server_exceptions: bool = True):
    client = service.start(service.client(s.now_slot), pinned)
    try:
        caller = client if raise_server_exceptions else TestClient(cosigner.app, raise_server_exceptions=False)
        return caller.post("/cosign", json=_request(s), headers=HEADERS)
    finally:
        client.__exit__(None, None, None)


def test_refuses_a_surrender_the_claimant_has_not_signed(service):
    """Building a surrender needs nothing but an address, so the co-signer
    signs, and spends daily budget, only on what the claimant has signed."""
    resp = _post(service, without_claimant_witness(golden_scenario(BASE_T1)))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "claimant_witness"
    assert service.rows() == 0


def test_refuses_a_claimant_signature_over_another_body(service):
    d = Draft(golden_scenario(BASE_T1))
    d.sign_as(throwaway_key(1))
    s = d.build()
    other = Draft(golden_scenario(BASE_T1))
    other.sign_as(throwaway_key(1))
    other.body[2] += 1
    _add_coin(other.body[1][CHANGE], -1)
    forged = other.build()
    # The first body with the witness of the second.
    body, _, tail = split_tx(s.tx)
    _, witnesses, _ = split_tx(forged.tx)
    resp = _post(service, Scenario(b"\x84" + body + witnesses + tail, s.language_views, s.parents, s.now_slot))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "claimant_witness"
    assert service.rows() == 0


def test_second_redemption_of_a_unit_past_its_editions_is_refused(service):
    """A pinned name redeems at most as many editions as existed at the pin,
    so an edition minted later is refused however it reaches the pool."""
    first = golden_scenario(BASE_T1)
    d = Draft(first)
    d.sign_as(throwaway_key(2))
    another_pool_utxo(d)
    second = d.build()
    service.chain.publish(*second.parents)
    client = service.start(service.client(first.now_slot))
    try:
        assert client.post("/cosign", json=_request(first), headers=HEADERS).status_code == 200
        refused = client.post("/cosign", json=_request(second), headers=HEADERS)
    finally:
        client.__exit__(None, None, None)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "redemption_limit"
    assert service.rows() == 1


def test_the_pin_counts_units_already_in_quarantine(service):
    """Replaying a surrender that already landed asks for a unit the pin
    records as quarantined."""
    resp = _post(service, golden_scenario(BASE_T1), pinned=True)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "redemption_limit"
    assert service.rows() == 0


def test_signatures_that_cannot_both_land_count_once_against_the_daily_cap(service, monkeypatch):
    """Two versions of one surrender spend the same pool output, so at most
    one lands; re-signing a rebuilt surrender does not use the cap twice."""
    key = throwaway_key(3)
    a = Draft(golden_scenario(BASE_T1))
    a.sign_as(key)
    first = a.build()
    b = Draft(golden_scenario(BASE_T1))
    b.sign_as(key)
    b.body[2] += 1
    _add_coin(b.body[1][CHANGE], -1)
    rebuilt = b.build()
    service.chain.publish(*first.parents, *rebuilt.parents)
    monkeypatch.setenv("MAX_CMATRA_PER_DAY", str(2 * _payout(first) - 1))
    client = service.start(service.client(first.now_slot))
    try:
        assert client.post("/cosign", json=_request(first), headers=HEADERS).status_code == 200
        again = client.post("/cosign", json=_request(rebuilt), headers=HEADERS)
    finally:
        client.__exit__(None, None, None)
    assert again.status_code == 200, again.text
    assert service.rows() == 2


def test_a_surrender_spending_what_the_one_it_builds_on_spent_is_refused(service):
    """The same surrender rebuilt on the pool continuation of the first,
    which the co-signer has recorded: its claimant outputs are spent once the
    first lands, and before that its pool input does not exist. It can never
    land, so it is neither signed nor counted."""
    key = throwaway_key(4)
    a = Draft(golden_scenario(BASE_FUNGIBLE))
    a.sign_as(key)
    first = a.build()
    first_body_raw = split_tx(first.tx)[0]
    b = Draft(first)
    pool_ref = b.pool_ref()
    pool_coin = b.parent_output(pool_ref)[1][0]
    b.parents.append(decode(first_body_raw))
    b.drop_parents.add(pool_ref[0])
    b.set_inputs([[blake(first_body_raw), CONTINUATION] if r == pool_ref else r for r in b.inputs()])
    _add_asset(b.body[1][CONTINUATION], CMATRA_POLICY, CMATRA_NAME, -_payout(first))
    _add_coin(b.body[1][CHANGE], b.body[1][CONTINUATION][1][0] - pool_coin)
    b.signer = key
    second = b.build()
    service.chain.publish(*first.parents)

    client = service.start(service.client(first.now_slot))
    try:
        assert client.post("/cosign", json=_request(first), headers=HEADERS).status_code == 200
        refused = client.post("/cosign", json=_request(second), headers=HEADERS)
    finally:
        client.__exit__(None, None, None)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "input_conflict"
    assert "signature_hex" not in refused.text
    assert service.rows() == 1


def test_malformed_transaction_is_a_coded_refusal(service):
    d = Draft(golden_scenario(BASE_T1))
    d.body[14] = [[1], [2]]
    resp = _post(service, d.build(), raise_server_exceptions=False)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "body_shape"
    assert service.rows() == 0


def test_unwritable_ledger_is_a_coded_refusal(service):
    s = golden_scenario(BASE_T1)
    client = service.start(service.client(s.now_slot))
    try:
        with ledger_refusing_writes(service.ledger):
            resp = TestClient(cosigner.app, raise_server_exceptions=False).post(
                "/cosign", json=_request(s), headers=HEADERS)
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "ledger_unavailable"
    assert "signature_hex" not in resp.text
    assert service.rows() == 0


# ---------------------------------------------------------------------------
# The co-signer's own view of the chain
# ---------------------------------------------------------------------------


def test_the_chain_view_is_this_hosts_own_blockfrost_project(service):
    with service.client(0):
        chain = cosigner.state.chain
    assert chain.project_id == "mainnet-test-project"
    assert chain.base_url == "https://cardano-mainnet.blockfrost.io/api/v0"


def test_refuses_a_unit_whose_supply_grew_after_the_pin(service):
    """A pinned pass with a second edition minted later: the co-signer cannot
    tell the editions apart, so it redeems neither."""
    s = golden_scenario(BASE_T1)
    (t1,) = [u for u in _units(s) if u.startswith("b4689145")]
    service.chain.supply[t1] = 2
    resp = _post(service, s)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "supply_grown"
    assert "signature_hex" not in resp.text
    assert service.rows() == 0


@pytest.mark.parametrize("action", ["minted", "burned"])
def test_refuses_a_pass_minted_or_burned_after_the_pin(service, action):
    """The supply the chain view reports is still the pinned one; the pass's
    own history shows the change."""
    s = golden_scenario(BASE_T1)
    (t1,) = [u for u in _units(s) if u.startswith("b4689145")]
    service.chain.change_after_pin(t1, action)
    resp = _post(service, s)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "minted_after_pin"
    assert "signature_hex" not in resp.text
    assert service.rows() == 0


def test_an_unreachable_chain_view_is_a_coded_refusal(service):
    service.chain.failure = requests.ConnectionError("no route to host")
    resp = _post(service, golden_scenario(BASE_T1), raise_server_exceptions=False)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "chain_unavailable"
    assert "signature_hex" not in resp.text
    assert service.rows() == 0


def _units(s: Scenario) -> dict[str, int]:
    return dict(evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, s.cfg).units)


def test_a_surrender_spending_outputs_the_chain_never_saw_is_refused(service):
    """Inputs resolved from producing bodies the caller made up: the
    transaction can never land, so it is neither signed nor allowed to use up
    the unit's limit or the daily cap."""
    d = Draft(golden_scenario(BASE_T1))
    d.sign_as(throwaway_key(901))
    another_pool_utxo(d)
    resp = _post(service, d.build())
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "input_unknown"
    assert "signature_hex" not in resp.text
    assert service.rows() == 0
    # The unit's limit is untouched: its holder is still paid.
    assert _post(service, golden_scenario(BASE_T1)).status_code == 200
    assert service.rows() == 1


def test_an_invented_pool_output_is_refused_beside_a_real_claimant_output(service):
    d = Draft(golden_scenario(BASE_T1))
    d.sign_as(throwaway_key(902))
    service.chain.publish(encode(d.parents[-1]))  # the claimant's output exists
    another_pool_utxo(d)
    invented_pool = d.pool_ref()[0]
    resp = _post(service, d.build())
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "input_unknown"
    assert invented_pool.hex() in resp.json()["detail"]["detail"]
    assert service.rows() == 0


def test_a_surrender_spending_a_spent_pool_output_is_refused(service):
    s = golden_scenario(BASE_T1)
    pool_tx, pool_index = Draft(s).pool_ref()
    service.chain.spent[(pool_tx.hex(), pool_index)] = "cd" * 32
    resp = _post(service, s)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "input_spent"
    assert service.rows() == 0
