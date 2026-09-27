"""services.redemption_ledger counts what the approved surrenders could take
from the pool if they landed, and refuses an approval that would pass the
daily cap or a unit's pinned limit.

Approvals here are synthetic: the ledger sees only the pool output a
transaction spends, its payout and the units it quarantines."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from services.cosign_policy import Approval, CosignRejected
from services.redemption_ledger import DAY_S, RedemptionLedger, create_ledger

ROOT = Path(__file__).resolve().parent.parent

UNIT = "ab" * 28 + "01"
OTHER = "ab" * 28 + "02"
POOL_ROOT = (b"\x00" * 32, 0)


def _tx(n: int) -> bytes:
    return n.to_bytes(32, "big")


def approval(n: int, spends: tuple[bytes, int], payout: int = 10, units=None) -> Approval:
    """Transaction ``n``, spending pool output ``spends``, whose pool
    continuation is ``(_tx(n), 1)``."""
    return Approval(
        tx_hash=_tx(n), payout=payout, claimant=b"\x61" + b"\x01" * 28,
        pool_input=spends, units={UNIT: 1} if units is None else units,
        inputs=(spends,),
    )


def after(n: int) -> tuple[bytes, int]:
    return (_tx(n), 1)


@pytest.fixture
def ledger(tmp_path):
    path = str(tmp_path / "ledger.sqlite3")
    create_ledger(path)
    return RedemptionLedger(path, max_per_day=100)


def rows(ledger: RedemptionLedger) -> int:
    with sqlite3.connect(ledger.path) as conn:
        return conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]


LIMITS = {UNIT: 1, OTHER: 5}


def test_records_an_approval_within_its_limits(ledger):
    ledger.record(approval(1, POOL_ROOT), 1000.0, LIMITS)
    assert rows(ledger) == 1


def test_a_unit_is_refused_past_its_pinned_limit(ledger):
    ledger.record(approval(1, POOL_ROOT), 1000.0, LIMITS)
    with pytest.raises(CosignRejected) as err:
        ledger.record(approval(2, after(1)), 1000.0, LIMITS)
    assert err.value.code == "redemption_limit"
    assert rows(ledger) == 1


def test_a_unit_the_pin_does_not_list_is_refused(ledger):
    with pytest.raises(CosignRejected) as err:
        ledger.record(approval(1, POOL_ROOT, units={"cd" * 28: 1}), 1000.0, LIMITS)
    assert err.value.code == "redemption_limit"


def test_surrenders_of_one_pool_output_count_once(ledger):
    """At most one of several transactions spending one pool output lands."""
    for n in range(1, 4):
        ledger.record(approval(n, POOL_ROOT, payout=60), 1000.0 + n, LIMITS)
    assert rows(ledger) == 3


def test_a_surrender_superseded_by_another_frees_its_units(ledger):
    """1 and 2 both spend the root; 3 spends 2's continuation. 1 and 3 can
    never both land, so the unit counts once on every possible chain."""
    ledger.record(approval(1, POOL_ROOT), 1000.0, LIMITS)
    ledger.record(approval(2, POOL_ROOT, units={OTHER: 1}), 1001.0, LIMITS)
    ledger.record(approval(3, after(2)), 1002.0, LIMITS)
    with pytest.raises(CosignRejected) as err:
        ledger.record(approval(4, after(3)), 1003.0, LIMITS)
    assert err.value.code == "redemption_limit"


def test_surrenders_of_different_pool_outputs_add_up(ledger):
    """Two outputs of a transaction no signer approved can both be spent."""
    ledger.record(approval(1, (b"\x09" * 32, 0), units={OTHER: 3}), 1000.0, LIMITS)
    with pytest.raises(CosignRejected) as err:
        ledger.record(approval(2, (b"\x09" * 32, 1), units={OTHER: 3}), 1000.0, LIMITS)
    assert err.value.code == "redemption_limit"


def test_the_order_approvals_arrive_in_does_not_matter(ledger):
    """A continuation's spender recorded before the transaction that makes it."""
    ledger.record(approval(2, after(1), units={OTHER: 2}), 1000.0, LIMITS)
    ledger.record(approval(1, POOL_ROOT, units={OTHER: 2}), 1000.0, LIMITS)
    with pytest.raises(CosignRejected):
        ledger.record(approval(3, after(2), units={OTHER: 2}), 1000.0, LIMITS)


def test_daily_cap_counts_the_heaviest_chain(ledger):
    ledger.record(approval(1, POOL_ROOT, payout=60, units={}), 1000.0, LIMITS)
    ledger.record(approval(2, POOL_ROOT, payout=70, units={}), 1000.0, LIMITS)
    with pytest.raises(CosignRejected) as err:
        ledger.record(approval(3, after(2), payout=31, units={}), 1000.0, LIMITS)
    assert err.value.code == "daily_cap"
    ledger.record(approval(4, after(2), payout=30, units={}), 1000.0, LIMITS)


def test_daily_cap_rolls_over_but_unit_limits_do_not(ledger):
    ledger.record(approval(1, POOL_ROOT, payout=100), 1000.0, LIMITS)
    later = 1000.0 + DAY_S + 1
    ledger.record(approval(2, after(1), payout=100, units={}), later, LIMITS)
    with pytest.raises(CosignRejected) as err:
        ledger.record(approval(3, after(2), payout=0), later, LIMITS)
    assert err.value.code == "redemption_limit"


def test_an_approval_already_recorded_is_admitted_again_unchanged(ledger):
    ledger.record(approval(1, POOL_ROOT, payout=100), 1000.0, LIMITS)
    ledger.record(approval(1, POOL_ROOT, payout=100), 1001.0, LIMITS)
    ledger.check(approval(1, POOL_ROOT, payout=100), 1002.0, LIMITS)
    assert rows(ledger) == 1


def test_recorded_names_the_approvals_written_and_nothing_else(ledger):
    ledger.check(approval(1, POOL_ROOT), 1000.0, LIMITS)
    assert not ledger.recorded(_tx(1))
    ledger.record(approval(1, POOL_ROOT), 1000.0, LIMITS)
    assert ledger.recorded(_tx(1))
    assert not ledger.recorded(_tx(2))


def test_check_writes_nothing(ledger):
    ledger.check(approval(1, POOL_ROOT), 1000.0, LIMITS)
    assert rows(ledger) == 0
    with pytest.raises(CosignRejected):
        ledger.check(approval(2, POOL_ROOT, payout=101), 1000.0, LIMITS)


def test_the_record_survives_a_restart(ledger):
    ledger.record(approval(1, POOL_ROOT), 1000.0, LIMITS)
    reopened = RedemptionLedger(ledger.path, max_per_day=100)
    with pytest.raises(CosignRejected):
        reopened.record(approval(2, after(1)), 1000.0, LIMITS)


def test_a_long_chain_is_counted_without_recursion(ledger):
    with sqlite3.connect(ledger.path) as conn:
        conn.executemany(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?)",
            [(_tx(n), (_tx(n - 1) if n > 1 else POOL_ROOT[0]), 1 if n > 1 else 0, 0,
              '{"%s": 1}' % OTHER if n == 5 else "{}", 1000.0) for n in range(1, 5001)],
        )
    with pytest.raises(CosignRejected) as err:
        ledger.check(approval(5001, after(5000), units={OTHER: 5}), 1000.0, LIMITS)
    assert err.value.code == "redemption_limit"


def test_concurrent_writers_never_pass_the_cap(ledger):
    admitted, refused, errors = [], [], []
    barrier = threading.Barrier(40)

    def worker(n: int) -> None:
        barrier.wait()
        try:
            ledger.record(approval(n, (_tx(10_000 + n), 0), payout=10, units={}), 1000.0, LIMITS)
            admitted.append(n)
        except CosignRejected:
            refused.append(n)
        except Exception as exc:  # a lock timeout would land here
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 41)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(admitted) == 10
    assert rows(ledger) == 10


# ---------------------------------------------------------------------------
# A ledger is created once, deliberately; losing it is never a fresh start
# ---------------------------------------------------------------------------


def test_a_missing_ledger_file_is_refused_and_not_created(tmp_path):
    path = tmp_path / "absent.sqlite3"
    with pytest.raises(FileNotFoundError, match="redemption_ledger init"):
        RedemptionLedger(str(path), max_per_day=100)
    assert not path.exists()


@pytest.mark.parametrize("content", [b"", b"not a database"])
def test_a_file_that_is_not_a_ledger_is_refused(tmp_path, content):
    path = tmp_path / "other.sqlite3"
    path.write_bytes(content)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        RedemptionLedger(str(path), max_per_day=100)


def test_a_database_without_the_approvals_table_is_refused(tmp_path):
    path = tmp_path / "other.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated (x)")
    with pytest.raises(ValueError, match="not a redemption ledger"):
        RedemptionLedger(str(path), max_per_day=100)


def test_a_ledger_removed_while_in_use_is_not_recreated(ledger):
    ledger.record(approval(1, POOL_ROOT), 1000.0, LIMITS)
    os.remove(ledger.path)
    with pytest.raises(sqlite3.OperationalError):
        ledger.record(approval(2, after(1)), 1000.0, LIMITS)
    assert not os.path.exists(ledger.path)


def test_create_ledger_makes_an_empty_private_ledger(tmp_path):
    path = str(tmp_path / "new.sqlite3")
    create_ledger(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert rows(RedemptionLedger(path, max_per_day=100)) == 0


def test_create_ledger_never_replaces_an_existing_ledger(ledger):
    ledger.record(approval(1, POOL_ROOT), 1000.0, LIMITS)
    with pytest.raises(FileExistsError):
        create_ledger(ledger.path)
    assert rows(ledger) == 1


def test_init_command_creates_the_ledger_once(tmp_path):
    path = tmp_path / "cli.sqlite3"
    command = [sys.executable, "-m", "services.redemption_ledger", "init", str(path)]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert rows(RedemptionLedger(str(path), max_per_day=100)) == 0
    again = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert again.returncode != 0
    assert "exists" in again.stderr
