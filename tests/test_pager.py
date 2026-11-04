"""services.pager writes every page to an outbox before the caller goes on,
and delivers it to the Discord webhook only once Discord saves it,
surrenders' pages first. Refusals page within a budget: once an hour for a
rule and subject, ten an hour for a rule, the rest counted into one page.
Signing pauses while a page has gone undelivered past the grace, and resumes
once Discord saves it.

Discord is replaced by a recorder; tests/conftest.py replaces it for every
other test too, so no test reaches the network."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

import pytest
import requests

import services.pager as pager
from services.pager import Pager
from tests.cosign_cases import DELETED_WEBHOOK

WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/test-token_abc"


class Discord:
    """Answers each POST with the next of ``answers`` (status, JSON body,
    Retry-After header), or saves the message once they run out."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.posts: list[tuple[str, dict, dict]] = []
        self.delivered = threading.Event()

    def __call__(self, url: str, payload: dict, headers: dict):
        self.posts.append((url, payload, headers))
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.delivered.set()
        return 200, {"id": str(1000 + len(self.posts))}, None


def _recording(monkeypatch, *answers) -> Discord:
    fake = Discord(*answers)
    monkeypatch.setattr(pager, "_post", fake)
    return fake


@pytest.fixture
def discord(monkeypatch):
    return _recording(monkeypatch)


def _pager(tmp_path, url: str = WEBHOOK) -> Pager:
    return Pager(url, str(tmp_path / "outbox.sqlite3"), "co-signer")


def _owed(tmp_path) -> list[tuple[str, str | None]]:
    with sqlite3.connect(tmp_path / "outbox.sqlite3") as conn:
        return conn.execute("SELECT text, message_id FROM pages ORDER BY id").fetchall()


def test_a_page_is_in_the_outbox_before_anything_is_sent(tmp_path, discord):
    p = _pager(tmp_path)
    p.page("surrender approved")
    assert _owed(tmp_path) == [("surrender approved", None)]
    assert discord.posts == [] and p.owed() == 1


def test_a_page_counts_as_delivered_only_with_discords_message_id(tmp_path, discord):
    p = _pager(tmp_path)
    p.page("surrender approved")
    assert p.deliver() is None
    assert _owed(tmp_path) == [("surrender approved", "1001")]
    url, payload, headers = discord.posts[0]
    assert url == WEBHOOK + "?wait=true"
    assert payload["content"] == "[co-signer] surrender approved"
    assert payload["allowed_mentions"] == {"parse": []}
    assert headers["User-Agent"].startswith("fluxpoint-cmatra-")
    assert p.owed() == 0


@pytest.mark.parametrize("answer", [
    (500, None, None), (200, {}, None), (200, {"id": "x"}, None), (204, None, None),
    requests.ConnectionError("down"), requests.Timeout("slow"),
])
def test_an_unsaved_page_stays_owed_and_is_retried(tmp_path, monkeypatch, answer):
    discord = Discord(answer)
    monkeypatch.setattr(pager, "_post", discord)
    p = _pager(tmp_path)
    p.page("first")
    p.page("second")
    wait = p.deliver()
    assert wait is not None and wait > 0
    assert len(discord.posts) == 1  # the second waits behind the first
    assert p.owed() == 2
    assert p.deliver() is None
    assert [text for text, _ in _owed(tmp_path)] == ["first", "second"]
    assert p.owed() == 0


def test_discords_retry_after_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setattr(pager, "_post", Discord((429, {"retry_after": 7.5}, None)))
    p = _pager(tmp_path)
    p.page("x")
    assert p.deliver() == 7.5


def test_retries_back_off_up_to_five_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(pager, "_post", Discord(*[(502, None, None)] * 8))
    p = _pager(tmp_path)
    p.page("x")
    assert [p.deliver() for _ in range(7)] == [15, 30, 60, 120, 240, 300, 300]


def test_an_owed_page_survives_a_restart(tmp_path, discord, monkeypatch):
    monkeypatch.setattr(pager, "_post", Discord((500, None, None)))
    first = _pager(tmp_path)
    first.page("before the restart")
    first.deliver()
    monkeypatch.setattr(pager, "_post", discord)
    second = _pager(tmp_path)
    assert second.owed() == 1
    assert second.deliver() is None
    assert discord.posts[0][1]["content"] == "[co-signer] before the restart"


def test_the_sender_thread_delivers_without_being_asked(tmp_path, discord):
    p = _pager(tmp_path)
    p.start()
    p.page("approved")
    assert discord.delivered.wait(10)
    deadline = time.monotonic() + 10
    while p.owed() and time.monotonic() < deadline:
        time.sleep(0.01)
    p.stop()
    assert p.owed() == 0


def _clock(monkeypatch) -> list[float]:
    """The pager's clock, at 2027-01-15 08:00Z until moved."""
    now = [1_800_000_000.0]
    monkeypatch.setattr(pager.time, "time", lambda: now[0])
    return now


def _contents(discord: Discord) -> list[str]:
    return [payload["content"] for _, payload, _ in discord.posts]


def _flood(p: Pager, code: str, count: int, first: int = 0) -> None:
    """Refusals of ``count`` subjects, each its own."""
    for n in range(first, first + count):
        p.refusal(code, f"subject {n}", f"subject {n}")


# Each takes (tmp_path, monkeypatch) only, so tests/test_pager_mutation.py
# can run it against mutants of the pager.
BUDGET_AND_ORDER = []


def budget_and_order(test):
    BUDGET_AND_ORDER.append(test)
    return test


@budget_and_order
def test_a_repeated_refusal_pages_once_an_hour(tmp_path, monkeypatch):
    _recording(monkeypatch)
    clock = _clock(monkeypatch)
    p = _pager(tmp_path)
    for _ in range(3):
        p.refusal("denied_credential", "addr1x: W1", "addr1x")
    p.refusal("denied_credential", "addr1y: W2", "addr1y")
    clock[0] += 3600
    p.refusal("denied_credential", "addr1x: W1", "addr1x")
    assert [text for text, _ in _owed(tmp_path)] == [
        "refused denied_credential: addr1x: W1", "refused denied_credential: addr1y: W2",
        "refused denied_credential: addr1x: W1",
    ]


@budget_and_order
def test_a_refusal_under_no_deny_rule_is_not_paged(tmp_path, monkeypatch):
    _recording(monkeypatch)
    p = _pager(tmp_path)
    p.refusal("input_spent", "abc#0 is not an unspent output", "abc#0")
    assert _owed(tmp_path) == []


@budget_and_order
def test_a_surrender_page_goes_before_the_refusal_pages_owed_before_it(tmp_path, monkeypatch):
    discord = _recording(monkeypatch)
    p = _pager(tmp_path)
    for n in range(3):
        p.refusal("denied_credential", f"addr{n}: W1", f"addr{n}")
    p.page("signing surrender abc")
    assert p.deliver() is None
    assert _contents(discord) == ["[co-signer] signing surrender abc"] + [
        f"[co-signer] refused denied_credential: addr{n}: W1" for n in range(3)
    ]


@budget_and_order
def test_a_surrender_page_owed_while_refusals_are_sent_goes_next(tmp_path, monkeypatch):
    p = _pager(tmp_path)

    class SurrenderMidway(Discord):
        def __call__(self, url, payload, headers):
            if len(self.posts) == 0:
                p.page("signing surrender abc")
            return super().__call__(url, payload, headers)

    discord = SurrenderMidway()
    monkeypatch.setattr(pager, "_post", discord)
    for n in range(3):
        p.refusal("cooling_off", f"unit {n} reached the claimant", f"unit {n}")
    assert p.deliver() is None
    assert [text.split(":")[0] for text in _contents(discord)] == [
        "[co-signer] refused cooling_off", "[co-signer] signing surrender abc",
        "[co-signer] refused cooling_off", "[co-signer] refused cooling_off",
    ]


@budget_and_order
def test_each_rule_pages_ten_refusals_an_hour_and_counts_the_rest_into_one_page(tmp_path, monkeypatch):
    discord = _recording(monkeypatch)
    clock = _clock(monkeypatch)
    p = _pager(tmp_path)
    _flood(p, "denied_credential", 25)
    clock[0] += 600
    _flood(p, "pointer_address", 3)
    assert p.owed() == 13
    assert len(_owed(tmp_path)) == 14  # and the count, due when the hour ends
    assert p.deliver() == 3000
    assert _contents(discord)[:9] == [f"[co-signer] refused denied_credential: subject {n}" for n in range(9)]
    assert _contents(discord)[9] == (
        "[co-signer] refused denied_credential: subject 9 (the last denied_credential page until"
        " 2027-01-15 09:00Z; any more are counted then)"
    )
    assert len(discord.posts) == 13
    clock[0] += 2999
    assert p.owed() == 0 and p.deliver() == 1
    clock[0] += 1
    assert p.owed() == 1
    assert p.deliver() is None
    assert _contents(discord)[13] == (
        "[co-signer] refused denied_credential: 15 more in the hour from 2027-01-15 08:00Z, only logged"
    )
    _flood(p, "denied_credential", 1)
    assert p.owed() == 1  # a new hour pages again


@budget_and_order
def test_a_flood_of_refusals_leaves_the_outbox_and_the_subjects_remembered_bounded(tmp_path, monkeypatch):
    _recording(monkeypatch)
    clock = _clock(monkeypatch)
    monkeypatch.setattr(pager, "REMEMBERED_SUBJECTS", 50)
    p = _pager(tmp_path)
    for code in sorted(pager.PAGED_REFUSALS):
        _flood(p, code, 200)
    assert len(_owed(tmp_path)) == 3 * (pager.REFUSAL_PAGES_PER_HOUR + 1)
    assert len(p._repeats) == 50
    clock[0] += 3600
    p.refusal("cooling_off", "late", "late")
    assert list(p._repeats) == ["cooling_off late"]


@budget_and_order
def test_refusals_past_the_hours_pages_do_not_wake_the_sender(tmp_path, monkeypatch):
    """Once each hour a refusal starts the count, and the sender wakes to
    learn when it is due; the refusals after it only add to it, and leave a
    sender waiting out Discord's Retry-After alone."""
    _recording(monkeypatch, (429, {"retry_after": 900}, None))
    _clock(monkeypatch)
    p = _pager(tmp_path)
    _flood(p, "denied_credential", pager.REFUSAL_PAGES_PER_HOUR + 1)
    assert p.deliver() == 900
    p._wake.clear()
    _flood(p, "denied_credential", 50, first=1_000)
    assert not p._wake.is_set()
    assert len(_owed(tmp_path)) == pager.REFUSAL_PAGES_PER_HOUR + 1


@budget_and_order
def test_the_sender_wakes_for_a_page_owed_while_it_waits(tmp_path, monkeypatch):
    _recording(monkeypatch)
    p = _pager(tmp_path)
    p._wake.clear()
    p.page("signing surrender abc")
    assert p._wake.is_set()


# Signing fails closed. Each takes (tmp_path, monkeypatch) only, so
# tests/test_pager_mutation.py can run it against mutants of the pager.
FAIL_CLOSED = []


def fail_closed(test):
    FAIL_CLOSED.append(test)
    return test


HEALTHY = {"status": "ok", "pages_owed": 0, "oldest_page_owed_s": None, "signing_paused": False,
           "webhook_refused": None}


def _signing_paused(p: Pager) -> bool:
    try:
        p.require_delivered()
    except pager.PagesUndelivered as paused:
        assert paused.code == "pager_backlog"
        return True
    return False


def _from_env(where, grace: str | None) -> Pager:
    env = {"PAGER_DISCORD_WEBHOOK_URL": WEBHOOK, "PAGER_OUTBOX_PATH": str(where / "outbox.sqlite3")}
    if grace is not None:
        env["PAGER_GRACE_S"] = grace
    return Pager.from_env(env, "co-signer")


@fail_closed
def test_a_page_undelivered_past_the_grace_pauses_signing_until_discord_saves_it(tmp_path, monkeypatch):
    """The webhook was deleted: Discord answers 404 until it is replaced.
    The grace runs from the first page owed, not the latest."""
    _recording(monkeypatch, *[DELETED_WEBHOOK] * 3)
    clock = _clock(monkeypatch)
    p = _pager(tmp_path)
    p.page("signing surrender abc")
    p.deliver()
    clock[0] += 300
    p.page("signing surrender def")
    p.deliver()
    clock[0] += 300
    assert not _signing_paused(p)
    clock[0] += 1
    assert _signing_paused(p)
    p.deliver()
    assert _signing_paused(p)
    assert p.deliver() is None
    assert not _signing_paused(p)


@fail_closed
def test_a_slow_discord_never_pauses_signing_inside_the_grace(tmp_path, monkeypatch):
    """Discord drops a post, fails one, rate-limits one and resets one: it
    takes the whole grace to save the first page, and every surrender signed
    meanwhile is paged once it does."""
    discord = _recording(monkeypatch, requests.Timeout("slow"), (502, None, None),
                         (429, {"retry_after": 435}, None), requests.ConnectionError("reset"))
    clock = _clock(monkeypatch)
    start = clock[0]
    p = _pager(tmp_path)
    p.page("signing surrender 0")
    for n in range(1, 5):
        clock[0] += p.deliver()
        assert not _signing_paused(p)
        p.page(f"signing surrender {n}")
    assert clock[0] - start == 15 + 30 + 435 + 120
    assert p.deliver() is None
    assert _contents(discord)[-5:] == [f"[co-signer] signing surrender {n}" for n in range(5)]


@fail_closed
def test_the_grace_runs_from_when_a_page_falls_due(tmp_path, monkeypatch):
    """The count of the refusals only logged falls due when the hour ends."""
    _recording(monkeypatch)
    clock = _clock(monkeypatch)
    p = _pager(tmp_path)
    _flood(p, "denied_credential", pager.REFUSAL_PAGES_PER_HOUR + 1)
    assert p.deliver() == 3600
    assert p.health() == HEALTHY
    _recording(monkeypatch, DELETED_WEBHOOK)
    clock[0] += 3600 + 600
    p.deliver()
    assert not _signing_paused(p)
    clock[0] += 1
    assert _signing_paused(p)


@fail_closed
def test_health_names_a_refused_webhook_at_once_and_the_pause_past_the_grace(tmp_path, monkeypatch):
    _recording(monkeypatch, DELETED_WEBHOOK)
    clock = _clock(monkeypatch)
    p = _pager(tmp_path)
    assert p.health() == HEALTHY
    p.page("signing surrender abc")
    clock[0] += 5
    assert p.health() == {**HEALTHY, "pages_owed": 1, "oldest_page_owed_s": 5}
    p.deliver()
    assert p.health() == {**HEALTHY, "status": "paging_failed", "pages_owed": 1, "oldest_page_owed_s": 5,
                          "webhook_refused": 404}
    clock[0] += 600
    assert p.health() == {"status": "paging_failed", "pages_owed": 1, "oldest_page_owed_s": 605,
                          "signing_paused": True, "webhook_refused": 404}
    assert p.deliver() is None
    assert p.health() == HEALTHY


@fail_closed
def test_health_names_a_pause_whatever_discord_answered(tmp_path, monkeypatch):
    _recording(monkeypatch, (503, None, None))
    clock = _clock(monkeypatch)
    p = _pager(tmp_path)
    p.page("signing surrender abc")
    p.deliver()
    assert p.health() == {**HEALTHY, "pages_owed": 1, "oldest_page_owed_s": 0}
    clock[0] += 601
    assert p.health() == {"status": "paging_failed", "pages_owed": 1, "oldest_page_owed_s": 601,
                          "signing_paused": True, "webhook_refused": None}


@fail_closed
def test_a_refused_webhook_stays_named_until_discord_saves_a_page(tmp_path, monkeypatch):
    _recording(monkeypatch, (401, {"code": 50027}, None), (502, None, None), requests.Timeout("slow"),
               (403, {"code": 50001}, None), (500, None, None))
    p = _pager(tmp_path)
    p.page("signing surrender abc")
    named = []
    while p.deliver() is not None:
        named.append(p.health()["webhook_refused"])
    assert named == [401, 401, 401, 403, 403]
    assert p.health()["webhook_refused"] is None


@fail_closed
def test_the_grace_is_ten_minutes_unless_set_from_a_minute_to_an_hour(tmp_path, monkeypatch):
    _recording(monkeypatch, *[DELETED_WEBHOOK] * 10)
    clock = _clock(monkeypatch)
    for n, (setting, grace) in enumerate([(None, 600), ("", 600), ("60", 60), ("3600", 3600), (" 0900 ", 900)]):
        (tmp_path / str(n)).mkdir()
        p = _from_env(tmp_path / str(n), setting)
        p.page("signing surrender abc")
        p.deliver()
        clock[0] += grace
        assert not _signing_paused(p), setting
        clock[0] += 1
        assert _signing_paused(p), setting


@fail_closed
def test_a_grace_outside_a_minute_to_an_hour_refuses_startup(tmp_path, monkeypatch):
    for setting in ["59", "3601", "0", "-600", "+600", "600.5", "6e2", "ten", "0x258", "６００"]:
        with pytest.raises(ValueError, match="PAGER_GRACE_S must be a whole number of seconds from 60 to 3600"):
            _from_env(tmp_path, setting)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_refused_webhook_is_logged_as_critical(tmp_path, monkeypatch, caplog, status):
    _recording(monkeypatch, (status, {"code": 0}, None))
    p = _pager(tmp_path)
    p.page("x")
    with caplog.at_level(logging.INFO, logger="pager"):
        p.deliver()
    [critical] = [record for record in caplog.records if record.levelno == logging.CRITICAL]
    assert f"({status})" in critical.getMessage()
    assert "PAGER_DISCORD_WEBHOOK_URL" in critical.getMessage()


@pytest.mark.parametrize("answer", [
    (500, None, None), (429, {"retry_after": 1}, None), (400, {"code": 50006}, None), (200, {}, None),
    requests.ConnectionError("down"), requests.Timeout("slow"),
])
def test_an_answer_that_may_still_pass_names_no_refused_webhook(tmp_path, monkeypatch, caplog, answer):
    _recording(monkeypatch, answer)
    p = _pager(tmp_path)
    p.page("x")
    with caplog.at_level(logging.INFO, logger="pager"):
        p.deliver()
    assert p.health()["webhook_refused"] is None
    assert not [record for record in caplog.records if record.levelno == logging.CRITICAL]


def test_an_unreadable_outbox_is_not_taken_for_one_delivered(tmp_path):
    p = _pager(tmp_path)
    with sqlite3.connect(tmp_path / "outbox.sqlite3") as conn:
        conn.execute("DROP TABLE pages")
    with pytest.raises(sqlite3.Error):
        p.require_delivered()


@pytest.mark.parametrize("url", [
    "http://discord.com/api/webhooks/1/token",
    "https://discord.com.evil.example/api/webhooks/1/token",
    "https://evil.example/api/webhooks/1/token",
    "https://discord.com:8443/api/webhooks/1/token",
    "https://user@discord.com/api/webhooks/1/token",
    "https://discord.com/api/webhooks/1/token?wait=false",
    "https://discord.com/api/webhooks/1/token#x",
    "https://discord.com/api/channels/1/messages",
    "https://discord.com/api/webhooks/x/token",
    "",
])
def test_a_webhook_that_is_not_discords_is_refused(tmp_path, url):
    with pytest.raises(ValueError, match="PAGER_DISCORD_WEBHOOK_URL"):
        _pager(tmp_path, url)


def test_the_webhook_never_reaches_a_log_line(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pager, "_post", Discord(requests.ConnectionError(f"cannot reach {WEBHOOK}")))
    p = _pager(tmp_path)
    with caplog.at_level(logging.DEBUG):
        p.page("x")
        p.deliver()
    assert caplog.records and "test-token_abc" not in caplog.text


def test_a_pager_needs_its_webhook_and_its_outbox(tmp_path):
    with pytest.raises(ValueError, match="PAGER_DISCORD_WEBHOOK_URL"):
        Pager.from_env({"PAGER_OUTBOX_PATH": str(tmp_path / "o.sqlite3")}, "co-signer")
    with pytest.raises(ValueError, match="PAGER_OUTBOX_PATH"):
        Pager.from_env({"PAGER_DISCORD_WEBHOOK_URL": WEBHOOK}, "co-signer")
