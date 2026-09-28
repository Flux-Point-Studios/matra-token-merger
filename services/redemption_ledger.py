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

Every transaction on a path must be able to land after the ones before it, so
an approval that never could is refused, not recorded: one that spends, or
offers as collateral, an output a transaction it builds on already spends, or
one that needs an output of a recorded transaction on another branch of its
tree. A transaction that lands leaves its collateral unspent, so collateral
of a transaction it builds on stays available to it.

A ledger that went missing would reset every limit, so it is created once,
deliberately, and never implicitly:

    python -m services.redemption_ledger init <path>
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from services.cosign_policy import Approval, CosignRejected

DAY_S = 86_400

_SCHEMA = (
    "CREATE TABLE approvals ("
    " tx_hash BLOB PRIMARY KEY, pool_tx BLOB NOT NULL, pool_index INTEGER NOT NULL,"
    " payout INTEGER NOT NULL, units TEXT NOT NULL, spends TEXT NOT NULL,"
    " signed_at REAL NOT NULL)"
)


def create_ledger(path: str) -> None:
    """Create an empty ledger at ``path``, readable by its owner only.
    Raises FileExistsError if anything is already there."""
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(_SCHEMA)


@dataclass(frozen=True)
class _Row:
    tx_hash: bytes
    pool_input: tuple[bytes, int]
    payout: int
    units: Mapping[str, int]
    spends: frozenset[tuple[bytes, int]]
    signed_at: float


def _upstream(by_hash: Mapping[bytes, _Row],
              pool_input: tuple[bytes, int]) -> tuple[list[_Row], tuple[bytes, int]]:
    """The recorded transactions a spend of ``pool_input`` builds on, nearest
    first, and the pool output their chain starts from."""
    chain: list[_Row] = []
    while pool_input[0] in by_hash:
        chain.append(by_hash[pool_input[0]])
        pool_input = chain[-1].pool_input
    return chain, pool_input


def _refuse_if_it_cannot_land(rows: Iterable[_Row], approval: Approval) -> None:
    """Raise :class:`CosignRejected` if ``approval`` could never land after
    the recorded transactions it builds on."""
    by_hash = {row.tx_hash: row for row in rows}
    ancestors, root = _upstream(by_hash, approval.pool_input)
    ancestor_ids = {row.tx_hash for row in ancestors}
    spent_by = {ref: row.tx_hash for row in ancestors for ref in row.spends}
    for ref in approval.inputs:
        output = f"{ref[0].hex()}#{ref[1]}"
        if ref in spent_by:
            raise CosignRejected(
                "input_conflict",
                f"{output} is spent by {spent_by[ref].hex()}, which this transaction builds on",
            )
        producer = by_hash.get(ref[0])
        if (producer is not None and producer.tx_hash not in ancestor_ids
                and _upstream(by_hash, producer.pool_input)[1] == root):
            # Two branches of one tree spend a common pool output, so at most
            # one of them lands.
            raise CosignRejected(
                "input_conflict",
                f"{output} comes from {ref[0].hex()}, which competes with this transaction's"
                " chain for a pool output",
            )


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
        """Open the ledger at ``path``. Raises FileNotFoundError if there is
        none, and ValueError if the file there is not a ledger."""
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"{path}: no redemption ledger. Find the one this signer has been using;"
                f" only for a signer that has never signed: python -m services.redemption_ledger init {path}"
            )
        self.path = path
        self.max_per_day = max_per_day
        # mode=rw: a connection never creates the file, so a ledger removed
        # while the service runs fails every write instead of starting over.
        self._uri = Path(path).resolve().as_uri() + "?mode=rw"
        with closing(self._connect()) as conn:
            found = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approvals'"
            ).fetchone()
        if found is None or found[0] != _SCHEMA:
            raise ValueError(f"{path} is not a redemption ledger of this version")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._uri, uri=True, timeout=10, isolation_level=None)

    def recorded(self, tx_hash: bytes) -> bool:
        """Whether this signer recorded an approval of ``tx_hash``."""
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM approvals WHERE tx_hash = ?", (tx_hash,)
            ).fetchone() is not None

    def check(self, approval: Approval, now: float, limits: Mapping[str, int]) -> None:
        """Raise :class:`CosignRejected` if ``approval`` could never land after
        the recorded transactions it builds on, or recording it would pass a
        limit. Writes nothing."""
        self._admit(approval, now, limits, record=False)

    def record(self, approval: Approval, now: float, limits: Mapping[str, int]) -> None:
        """Check ``approval`` and record it, atomically with every other
        writer. An approval already recorded is admitted as it stands."""
        self._admit(approval, now, limits, record=True)

    def _admit(self, approval: Approval, now: float, limits: Mapping[str, int], record: bool) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE" if record else "BEGIN")
            try:
                rows = [
                    _Row(tx, (pool_tx, pool_index), payout, json.loads(units),
                         frozenset((bytes.fromhex(ref), index) for ref, index in json.loads(spends)),
                         signed_at)
                    for tx, pool_tx, pool_index, payout, units, spends, signed_at in conn.execute(
                        "SELECT tx_hash, pool_tx, pool_index, payout, units, spends, signed_at"
                        " FROM approvals"
                    )
                ]
                if all(row.tx_hash != approval.tx_hash for row in rows):
                    _refuse_if_it_cannot_land(rows, approval)
                    new = _Row(approval.tx_hash, approval.pool_input, approval.payout,
                               dict(approval.units), frozenset(approval.spends), now)
                    self._within_limits([*rows, new], now, approval, limits)
                    if record:
                        conn.execute(
                            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (new.tx_hash, new.pool_input[0], new.pool_input[1], new.payout,
                             json.dumps(new.units, sort_keys=True),
                             json.dumps([[ref.hex(), index] for ref, index in sorted(new.spends)]),
                             now),
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


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "init":
        sys.exit("usage: python -m services.redemption_ledger init <path>")
    try:
        create_ledger(sys.argv[2])
    except FileExistsError:
        sys.exit(f"{sys.argv[2]} exists; a ledger is never replaced")
    print(f"created an empty redemption ledger at {sys.argv[2]}")
