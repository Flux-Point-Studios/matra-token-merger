"""Pages the on-call channel about what a signer does with surrenders: each
one it is about to sign, and each refusal under a deny rule (a denied
credential, a pointer address, a unit inside its cooling-off).

A page is written to an outbox on disk (PAGER_OUTBOX_PATH) before the caller
goes on, and a sender thread delivers the owed pages to the Discord webhook in
PAGER_DISCORD_WEBHOOK_URL, surrenders' first and each kind oldest first,
posting with ?wait=true: a page counts as delivered only once Discord answers
with the id of the message it saved. Anything else leaves it owed, retried
with backoff (or after Discord's Retry-After), across restarts; /health
reports how many are owed. The webhook must be a discord.com webhook URL, so
no other host is ever contacted, and it is never logged.

A refusal costs its caller nothing, so refusals page within a budget. A
subject refused again under the same rule within the hour is only logged, and
each rule pages at most REFUSAL_PAGES_PER_HOUR refusals an hour; the rest are
logged and counted into one page sent when the hour ends. However many
refusals callers make, the channel and the outbox take no more than that, and
no refusal's page goes before a surrender's.
"""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests
from pycardano import Address

logger = logging.getLogger("pager")

USER_AGENT = "fluxpoint-cmatra-signer/1"
REPEAT_S = 3600
REFUSAL_PAGES_PER_HOUR = 10
REMEMBERED_SUBJECTS = 10_000
FIRST_RETRY_S, LAST_RETRY_S, MAX_RETRY_AFTER_S = 15, 300, 900
# Refusals under a deny rule, which page; any other refusal is only logged.
PAGED_REFUSALS = frozenset({"denied_credential", "pointer_address", "cooling_off"})
# Delivery order: every owed surrender page goes before any refusal's.
SURRENDER, REFUSAL = 0, 1

_WEBHOOK_PATH = re.compile(r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+")
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS pages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " created_at REAL NOT NULL, text TEXT NOT NULL, message_id TEXT,"
    " priority INTEGER NOT NULL, due_at REAL NOT NULL)"
)


def _post(url: str, payload: dict, headers: dict) -> tuple[int, Any, str | None]:
    """(status, JSON body or None, Retry-After header) of a POST to ``url``."""
    response = requests.post(url, json=payload, headers=headers, timeout=10)
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body, response.headers.get("Retry-After")


def _webhook(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in ("discord.com", "discordapp.com")
            or parts.port is not None or parts.username or parts.password or parts.query
            or parts.fragment or not _WEBHOOK_PATH.fullmatch(parts.path)):
        raise ValueError("PAGER_DISCORD_WEBHOOK_URL must be a discord.com webhook URL")
    return f"https://{parts.hostname}{parts.path}"


def _retry_after(header: str | None, body: Any) -> float | None:
    """The seconds Discord asked us to wait, in its header or its body."""
    asked = [header, body.get("retry_after") if isinstance(body, dict) else None]
    seconds = []
    for value in asked:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            seconds.append(number)
    return min(max(seconds), MAX_RETRY_AFTER_S) if seconds else None


def _utc(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(at))


@dataclass
class _Hour:
    """One rule's refusals in the hour from ``start``."""

    start: float
    paged: int = 0
    logged: int = 0
    count_page: int | None = None
    """The outbox row counting the refusals only logged, once there are any."""


class Pager:
    def __init__(self, webhook_url: str, outbox_path: str, source: str) -> None:
        """Page as ``source`` to ``webhook_url``, through the outbox at
        ``outbox_path`` (created owner-only if missing). Raises ValueError for
        a webhook URL that is not Discord's."""
        self._url = _webhook(webhook_url) + "?wait=true"
        self._outbox = outbox_path
        self._source = source
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._repeats: dict[str, float] = {}
        """When each rule and subject last paged, oldest first."""
        self._hours: dict[str, _Hour] = {}
        self._backoff = 0
        os.close(os.open(outbox_path, os.O_WRONLY | os.O_CREAT, 0o600))
        with closing(sqlite3.connect(outbox_path)) as conn, conn:
            conn.execute(_SCHEMA)

    @classmethod
    def from_env(cls, env: Mapping[str, str], source: str) -> "Pager":
        url = env.get("PAGER_DISCORD_WEBHOOK_URL", "").strip()
        if not url:
            raise ValueError("PAGER_DISCORD_WEBHOOK_URL must be set")
        outbox = env.get("PAGER_OUTBOX_PATH", "").strip()
        if not outbox:
            raise ValueError("PAGER_OUTBOX_PATH must be set")
        return cls(url, outbox, source)

    def _owe(self, text: str, priority: int, due_at: float) -> int:
        """Write a page of ``text`` to the outbox, to send from ``due_at``,
        and wake the sender. Returns its row."""
        with closing(sqlite3.connect(self._outbox)) as conn, conn:
            row = conn.execute(
                "INSERT INTO pages (created_at, text, priority, due_at) VALUES (?, ?, ?, ?)",
                (time.time(), text, priority, due_at),
            ).lastrowid
        self._wake.set()
        return row

    def page(self, text: str) -> None:
        """Owe a page of ``text``, sent before any refusal's."""
        with self._lock:
            self._owe(text, SURRENDER, time.time())

    def surrender(self, approval: Any) -> None:
        """Owe the page of a surrender about to be signed."""
        self.page(
            f"signing surrender {approval.tx_hash.hex()}: {approval.payout / 10**6:,.6f} cMATRA to"
            f" {Address.from_primitive(approval.claimant).encode()} for {len(approval.units)} unit(s)"
        )

    def _first_this_hour(self, key: str, now: float) -> bool:
        """Whether ``key`` has not paged within the hour; it has from now.
        Remembers no more than REMEMBERED_SUBJECTS keys, forgetting the
        oldest first."""
        while self._repeats:
            oldest, at = next(iter(self._repeats.items()))
            if now - at < REPEAT_S and len(self._repeats) < REMEMBERED_SUBJECTS:
                break
            del self._repeats[oldest]
        if key in self._repeats:
            return False
        self._repeats[key] = now
        return True

    def refusal(self, code: str, detail: str, subject: str) -> None:
        """Owe the page of a refusal of ``subject`` with ``code``, if a deny
        rule refused it, within the rule's pages for the hour."""
        if code not in PAGED_REFUSALS:
            return
        text = f"refused {code}: {detail}"
        now = time.time()
        with self._lock:
            if not self._first_this_hour(f"{code} {subject}", now):
                logger.info("Paged within the hour already: %s", text)
                return
            hour = self._hours.get(code)
            if hour is None or now - hour.start >= REPEAT_S:
                hour = self._hours[code] = _Hour(now)
            if hour.paged < REFUSAL_PAGES_PER_HOUR:
                hour.paged += 1
                if hour.paged == REFUSAL_PAGES_PER_HOUR:
                    text += f" (the last {code} page until {_utc(hour.start + REPEAT_S)}; any more are counted then)"
                self._owe(text, REFUSAL, now)
                return
            hour.logged += 1
            logger.info("Only logged, past this hour's %s pages: %s", code, text)
            count = f"refused {code}: {hour.logged} more in the hour from {_utc(hour.start)}, only logged"
            if hour.count_page is None:
                hour.count_page = self._owe(count, REFUSAL, hour.start + REPEAT_S)
                return
            with closing(sqlite3.connect(self._outbox)) as conn, conn:
                conn.execute("UPDATE pages SET text = ? WHERE id = ?", (count, hour.count_page))

    def owed(self) -> int:
        """How many pages are due and not delivered."""
        with closing(sqlite3.connect(self._outbox)) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM pages WHERE message_id IS NULL AND due_at <= ?", (time.time(),),
            ).fetchone()[0]

    def deliver(self) -> float | None:
        """Send the pages due, surrenders' first and each kind oldest first,
        until Discord does not save one. Returns the seconds to wait before
        trying again, or None once nothing is owed."""
        while True:
            now = time.time()
            with closing(sqlite3.connect(self._outbox)) as conn:
                due = conn.execute(
                    "SELECT id, text FROM pages WHERE message_id IS NULL AND due_at <= ?"
                    " ORDER BY priority, id LIMIT 1", (now,),
                ).fetchone()
                later = conn.execute("SELECT MIN(due_at) FROM pages WHERE message_id IS NULL").fetchone()[0]
            if due is None:
                self._backoff = 0
                return None if later is None else later - now
            page_id, text = due
            message_id, retry_after = self._send(text)
            if message_id is None:
                self._backoff = min(LAST_RETRY_S, self._backoff * 2) if self._backoff else FIRST_RETRY_S
                return self._backoff if retry_after is None else retry_after
            with closing(sqlite3.connect(self._outbox)) as conn, conn:
                conn.execute("UPDATE pages SET message_id = ? WHERE id = ?", (message_id, page_id))
            logger.info("Paged (Discord message %s): %s", message_id, text)

    def _send(self, text: str) -> tuple[str | None, float | None]:
        """(Discord's message id or None, the seconds it asked us to wait)."""
        payload = {"content": f"[{self._source}] {text}"[:2000], "allowed_mentions": {"parse": []}}
        try:
            status, body, header = _post(self._url, payload, {"User-Agent": USER_AGENT})
        except requests.RequestException as exc:
            logger.error("Page not delivered: %s", type(exc).__name__)
            return None, None
        message_id = body.get("id") if status == 200 and isinstance(body, dict) else None
        if isinstance(message_id, str) and message_id.isdigit():
            return message_id, None
        logger.error("Page not delivered: Discord answered %s", status)
        return None, _retry_after(header, body)

    def start(self) -> None:
        """Deliver owed pages, those of earlier runs first, on a thread of
        their own."""
        threading.Thread(target=self._run, name="pager", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def _run(self) -> None:
        wait: float | None = 0
        while not self._stopping.is_set():
            self._wake.wait(wait)
            self._wake.clear()
            if self._stopping.is_set():
                return
            try:
                wait = self.deliver()
            except sqlite3.Error:
                logger.exception("Pager outbox unreadable; retrying")
                wait = LAST_RETRY_S
