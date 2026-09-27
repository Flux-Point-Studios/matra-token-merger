"""A signer's record of the surrenders it approved, and the limits it holds them to.

Each signer keeps its own ledger and records an approval before its signature
leaves the process. Two limits are checked against the record:

  * the cMATRA the pool can lose to transactions approved in the last 24 hours
    stays within a cap;
  * what each legacy unit redeems after the pin stays within what the pin
    says remains of it, so an edition minted after the pin, or a unit already
    locked in quarantine, is never paid for again.

Both count what could actually land. Transactions that spend the same pool
output conflict, so at most one of them lands; a transaction spending another's
pool continuation lands only after it. Approvals therefore form a tree over
pool outputs, and a quantity counts as its heaviest root-to-leaf path, summed
over the pool outputs no recorded transaction produced. Any set of approvals
that can all land together is counted in full, while a surrender rebuilt
against the same pool output, or superseded by another surrender of that
output, is not counted twice.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from services.cosign_policy import Approval, CosignRejected

DAY_S = 86_400


@dataclass(frozen=True)
class _Row:
    tx_hash: bytes
    pool_input: tuple[bytes, int]
    payout: int
    units: Mapping[str, int]
    signed_at: float


def _heaviest(rows: Iterable[_Row], weight: Callable[[_Row], int]) -> int:
    """The largest total ``weight`` over approvals that can all land together."""
    rows = list(rows)
    recorded = {row.tx_hash for row in rows}
    spenders: dict[bytes, list[_Row]] = defaultdict(list)
    roots: dict[tuple[bytes, int], list[_Row]] = defaultdict(list)
    for row in rows:
        if row.pool_input[0] in recorded:
            # The policy lets a transaction make exactly one pool output.
            spenders[row.pool_input[0]].append(row)
        else:
            roots[row.pool_input].append(row)

    path: dict[bytes, int] = {}
    stack = [(row, False) for row in rows]
    while stack:
        row, children_done = stack.pop()
        if row.tx_hash in path:
            continue
        if children_done:
            path[row.tx_hash] = weight(row) + max(
                (path[child.tx_hash] for child in spenders[row.tx_hash]), default=0,
            )
        else:
            stack.append((row, True))
            stack.extend((child, False) for child in spenders[row.tx_hash])
    return sum(max(path[row.tx_hash] for row in group) for group in roots.values())


class RedemptionLedger:
    """SQLite record of approved surrenders, shared by every process that
    signs with one key."""

    def __init__(self, path: str, max_per_day: int) -> None:
        self.path = path
        self.max_per_day = max_per_day
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS approvals ("
                " tx_hash BLOB PRIMARY KEY, pool_tx BLOB NOT NULL, pool_index INTEGER NOT NULL,"
                " payout INTEGER NOT NULL, units TEXT NOT NULL, signed_at REAL NOT NULL)"
            )

    def check(self, approval: Approval, now: float, limits: Mapping[str, int]) -> None:
        """Raise :class:`CosignRejected` if recording ``approval`` would pass a
        limit. Writes nothing."""
        self._admit(approval, now, limits, record=False)

    def record(self, approval: Approval, now: float, limits: Mapping[str, int]) -> None:
        """Check ``approval`` and record it, atomically with every other
        writer. An approval already recorded is admitted as it stands."""
        self._admit(approval, now, limits, record=True)

    def _admit(self, approval: Approval, now: float, limits: Mapping[str, int], record: bool) -> None:
        with closing(sqlite3.connect(self.path, timeout=10, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE" if record else "BEGIN")
            try:
                rows = [
                    _Row(tx, (pool_tx, pool_index), payout, json.loads(units), signed_at)
                    for tx, pool_tx, pool_index, payout, units, signed_at in conn.execute(
                        "SELECT tx_hash, pool_tx, pool_index, payout, units, signed_at FROM approvals"
                    )
                ]
                if all(row.tx_hash != approval.tx_hash for row in rows):
                    new = _Row(approval.tx_hash, approval.pool_input, approval.payout,
                               dict(approval.units), now)
                    self._within_limits([*rows, new], now, approval, limits)
                    if record:
                        conn.execute(
                            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?)",
                            (new.tx_hash, new.pool_input[0], new.pool_input[1], new.payout,
                             json.dumps(new.units, sort_keys=True), now),
                        )
                conn.execute("COMMIT" if record else "ROLLBACK")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _within_limits(self, rows: list[_Row], now: float, approval: Approval,
                       limits: Mapping[str, int]) -> None:
        for unit in sorted(approval.units):
            redeemed = _heaviest(rows, lambda row: row.units.get(unit, 0))
            if redeemed > limits.get(unit, 0):
                raise CosignRejected(
                    "redemption_limit",
                    f"{unit}: {redeemed} redeemed would pass the {limits.get(unit, 0)} the pin leaves",
                )
        paid = _heaviest((row for row in rows if row.signed_at > now - DAY_S), lambda row: row.payout)
        if paid > self.max_per_day:
            raise CosignRejected("daily_cap", f"{paid} in 24h would pass {self.max_per_day}")
