"""services.pager writes every page to an outbox before the caller goes on,
and delivers it to the Discord webhook only once Discord saves it.

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


@pytest.fixture
def discord(monkeypatch):
    fake = Discord()
    monkeypatch.setattr(pager, "_post", fake)
    return fake


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


def test_a_repeated_refusal_pages_once_an_hour(tmp_path, discord, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pager.time, "time", lambda: clock[0])
    p = _pager(tmp_path)
    for _ in range(3):
        p.page("refused addr1x", once_per="denied_credential addr1x")
    p.page("refused addr1y", once_per="denied_credential addr1y")
    clock[0] += 3600
    p.page("refused addr1x", once_per="denied_credential addr1x")
    assert [text for text, _ in _owed(tmp_path)] == ["refused addr1x", "refused addr1y", "refused addr1x"]


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
