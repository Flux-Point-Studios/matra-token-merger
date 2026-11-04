"""services.redemption_ledger moves a signer's ledger to a new home with a
proof, and digests what two signers recorded alike.

A signer that starts on a fresh ledger forgets every approval and with them
its limits, so the one-time move under a dedicated service user copies the
ledger byte for byte, proves the copy, and keeps the old file read-only."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from services import redemption_ledger
from services.redemption_ledger import approvals_digest, copy_ledger, create_ledger

ROOT = Path(__file__).resolve().parent.parent
ME = pwd.getpwuid(os.getuid()).pw_name


def ledger_with(path: Path, rows: list[tuple]) -> Path:
    """A ledger holding ``rows`` of (n, payout, signed_at)."""
    create_ledger(str(path))
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(n.to_bytes(32, "big"), b"\x00" * 32, n, payout, json.dumps({"ab" * 28: 1}),
              json.dumps([["00" * 32, n]]), signed_at) for n, payout, signed_at in rows],
        )
    return path


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def source(tmp_path) -> Path:
    return ledger_with(tmp_path / "old" / "surrender-ledger.sqlite3", [(1, 10, 1000.0), (2, 20, 2000.0), (3, 5, 3000.0)])


@pytest.fixture(autouse=True)
def old_dir(tmp_path):
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()


def test_the_copy_is_byte_identical_private_and_the_source_read_only(source, tmp_path):
    before = sha256_of(source)
    destination = tmp_path / "new" / "surrender-ledger.sqlite3"
    proof = copy_ledger(str(source), str(destination))
    assert proof["sha256"] == before == sha256_of(destination)
    assert proof["approvals"] == 3
    assert proof["approvals_digest"] == approvals_digest(str(source))[1]
    assert mode(destination) == 0o600 and mode(source) == 0o400
    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_the_copy_is_handed_to_its_owner(source, tmp_path):
    destination = tmp_path / "new" / "ledger.sqlite3"
    copy_ledger(str(source), str(destination), owner=ME)
    assert os.stat(destination).st_uid == os.getuid()


def test_an_unknown_owner_is_refused_before_anything_is_written(source, tmp_path):
    destination = tmp_path / "new" / "ledger.sqlite3"
    with pytest.raises(ValueError, match="no user"):
        copy_ledger(str(source), str(destination), owner="no-such-user-c1")
    assert not destination.exists() and mode(source) == 0o600


def test_a_destination_is_never_replaced(source, tmp_path):
    destination = ledger_with(tmp_path / "new" / "ledger.sqlite3", [(9, 1, 1.0)])
    kept = sha256_of(destination)
    with pytest.raises(FileExistsError, match="never replaced"):
        copy_ledger(str(source), str(destination))
    assert sha256_of(destination) == kept and mode(source) == 0o600


@pytest.mark.parametrize("sidecar", ["-journal", "-wal"])
def test_a_source_with_a_journal_beside_it_is_refused(source, tmp_path, sidecar):
    Path(f"{source}{sidecar}").write_bytes(b"\x00" * 512)
    destination = tmp_path / "new" / "ledger.sqlite3"
    with pytest.raises(ValueError, match="in flight or was interrupted"):
        copy_ledger(str(source), str(destination))
    assert not destination.exists() and mode(source) == 0o600


def test_a_file_that_is_no_ledger_is_refused(tmp_path):
    other = tmp_path / "old" / "other.sqlite3"
    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE approvals (tx_hash BLOB)")
    destination = tmp_path / "new" / "ledger.sqlite3"
    with pytest.raises(ValueError, match="not a redemption ledger"):
        copy_ledger(str(other), str(destination))
    assert not destination.exists()


def test_no_writer_can_change_the_source_while_it_is_copied(source, tmp_path, monkeypatch):
    destination = tmp_path / "new" / "ledger.sqlite3"
    refused = []
    real_open = os.open

    def open_after_a_write_attempt(path, *args, **kwargs):
        if str(path) == str(destination):
            writer = sqlite3.connect(source, timeout=0)
            try:
                writer.execute("DELETE FROM approvals")
                writer.commit()
            except sqlite3.OperationalError as exc:
                refused.append(str(exc))
                writer.rollback()
            finally:
                writer.close()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(redemption_ledger.os, "open", open_after_a_write_attempt)
    proof = copy_ledger(str(source), str(destination))
    assert refused == ["database is locked"]
    assert proof["approvals"] == 3


def test_a_copy_that_does_not_match_is_removed_and_the_source_left_writable(source, tmp_path, monkeypatch):
    destination = tmp_path / "new" / "ledger.sqlite3"
    real_write = os.write

    def flip_the_last_byte(fd, data):
        return real_write(fd, bytes(data[:-1]) + bytes([data[-1] ^ 1]))

    monkeypatch.setattr(redemption_ledger.os, "write", flip_the_last_byte)
    with pytest.raises(ValueError, match="does not match"):
        copy_ledger(str(source), str(destination))
    assert not destination.exists() and mode(source) == 0o600


def test_the_digest_is_what_two_signers_record_alike(tmp_path):
    primary = ledger_with(tmp_path / "old" / "primary.sqlite3", [(1, 10, 1000.0), (2, 20, 2000.0)])
    cosigner = ledger_with(tmp_path / "new" / "cosigner.sqlite3", [(2, 20, 2001.5), (1, 10, 1000.4)])
    other = ledger_with(tmp_path / "new" / "other.sqlite3", [(1, 10, 1000.0), (2, 21, 2000.0)])
    assert approvals_digest(str(primary)) == approvals_digest(str(cosigner))
    assert approvals_digest(str(primary))[0] == 2
    assert approvals_digest(str(other)) != approvals_digest(str(primary))


def test_the_command_copies_once_and_digests(source, tmp_path):
    destination = tmp_path / "new" / "ledger.sqlite3"

    def command(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "services.redemption_ledger", *args], cwd=ROOT,
                              check=False, capture_output=True, text=True)

    copied = command("copy", "--owner", ME, str(source), str(destination))
    assert copied.returncode == 0, copied.stderr
    assert json.loads(copied.stdout)["sha256"] == sha256_of(destination)
    digested = command("digest", str(destination))
    assert digested.returncode == 0, digested.stderr
    assert json.loads(digested.stdout)["approvals"] == 3
    again = command("copy", str(destination), str(tmp_path / "new" / "ledger.sqlite3"))
    assert again.returncode == 1
    assert "never replaced" in again.stderr
