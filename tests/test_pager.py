"""services.pager writes every page to an outbox before the caller goes on,
and delivers it to the Discord webhook only once Discord saves it,
surrenders' pages first. Refusals page within a budget: once an hour for a
rule and subject, ten an hour for a rule, the rest counted into one page.

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
