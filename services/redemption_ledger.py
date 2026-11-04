"""A signer's record of the surrenders it approved, and the limits it holds them to.

Each signer keeps its own ledger and records an approval before its signature
leaves the process. Three limits are checked against the record:

  * the cMATRA the pool can lose to transactions approved in the last 24 hours
    stays within a cap;
  * what each legacy unit redeems after the pin stays within what the pin
    says remains of it, so an edition minted after the pin, or a unit already
    locked in quarantine, is never paid for again;
  * what each merge asset redeems after the pin, all its units together,
    stays within what the pool was funded to pay for of it
    (tools.process_surrender.funded_limits), so editions beyond the supply
    the rate table priced are not paid from the others' share.

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

For the same reason a signer that moves keeps its ledger. `copy` moves it
byte for byte with a proof and leaves the old file read-only; `digest` counts
the approvals and digests what every signer records alike, so two signers'
ledgers can be compared from their own hosts:

    python -m services.redemption_ledger copy [--owner USER] <source> <destination>
    python -m services.redemption_ledger digest <path>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
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


def _refs(spends: str) -> frozenset[tuple[bytes, int]]:
    """The outputs a row's ``spends`` column names."""
    return frozenset((bytes.fromhex(ref), index) for ref, index in json.loads(spends))


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

    def __init__(self, path: str, max_per_day: int, funded: Mapping[str, int]) -> None:
        """Open the ledger at ``path``, holding each asset policy to what
        ``funded`` says the pool was funded to redeem of it after the pin.
        Raises FileNotFoundError if there is none, and ValueError if the file
        there is not a ledger."""
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"{path}: no redemption ledger. Find the one this signer has been using;"
                f" only for a signer that has never signed: python -m services.redemption_ledger init {path}"
            )
        self.path = path
        self.max_per_day = max_per_day
        self.funded = dict(funded)
        # mode=rw: a connection never creates the file, so a ledger removed
        # while the service runs fails every write instead of starting over.
        self._uri = Path(path).resolve().as_uri() + "?mode=rw"
        with closing(self._connect()) as conn:
            if not _is_ledger(conn):
                raise ValueError(f"{path} is not a redemption ledger of this version")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._uri, uri=True, timeout=10, isolation_level=None)

    def claimant_spends(self, tx_hash: bytes) -> tuple[tuple[bytes, int], ...] | None:
        """The outputs besides the pool's that the surrender ``tx_hash``
        spends, or None if this signer recorded no approval of it."""
        with closing(self._connect()) as conn:
            found = conn.execute(
                "SELECT pool_tx, pool_index, spends FROM approvals WHERE tx_hash = ?", (tx_hash,)
            ).fetchone()
        if found is None:
            return None
        pool_tx, pool_index, spends = found
        return tuple(sorted(_refs(spends) - {(pool_tx, pool_index)}))

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
                    _Row(tx, (pool_tx, pool_index), payout, json.loads(units), _refs(spends), signed_at)
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
        for policy in sorted({unit[:56] for unit in approval.units}):
            redeemed = _heaviest(
                rows, lambda row: sum(q for unit, q in row.units.items() if unit[:56] == policy),
            )
            if redeemed > self.funded.get(policy, 0):
                raise CosignRejected(
                    "funded_supply",
                    f"{policy}: {redeemed} redeemed after the pin would pass the"
                    f" {self.funded.get(policy, 0)} the pool was funded for",
                )
        paid = _heaviest((row for row in rows if row.signed_at > now - DAY_S), lambda row: row.payout)
        if paid > self.max_per_day:
            raise CosignRejected("daily_cap", f"{paid} in 24h would pass {self.max_per_day}")


def _is_ledger(conn: sqlite3.Connection) -> bool:
    found = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approvals'").fetchone()
    return found is not None and found[0] == _SCHEMA


def _read_only(path: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)


def _rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT * FROM approvals ORDER BY tx_hash").fetchall()


def _digest(conn: sqlite3.Connection) -> tuple[int, str]:
    rows = conn.execute(
        "SELECT tx_hash, pool_tx, pool_index, payout, units, spends FROM approvals ORDER BY tx_hash"
    ).fetchall()
    text = "\n".join(json.dumps([tx.hex(), pool_tx.hex(), index, payout, units, spends])
                     for tx, pool_tx, index, payout, units, spends in rows)
    return len(rows), hashlib.sha256(text.encode()).hexdigest()


def approvals_digest(path: str) -> tuple[int, str]:
    """How many approvals the ledger at ``path`` holds, and a sha256 over every
    column but signed_at, in tx_hash order: two signers that approved the same
    surrenders give the same digest, however far apart they signed."""
    with closing(_read_only(path)) as conn:
        if not _is_ledger(conn):
            raise ValueError(f"{path} is not a redemption ledger of this version")
        return _digest(conn)


def copy_ledger(source: str, destination: str, owner: str | None = None) -> dict:
    """Copy the ledger at ``source`` to ``destination`` byte for byte, prove the
    copy, and only then make ``source`` read-only (0400), so the old file is
    kept as it was.

    Refuses a source that is not a ledger, one with a journal beside it (a
    write in flight or interrupted, so the file alone may be inconsistent),
    and any existing destination. The bytes are read under a shared lock, so
    no writer can change the source meanwhile. The destination is created
    owner-only (0600), for ``owner`` if named. The proof: both files have the
    same sha256, the copy passes SQLite's integrity check and holds the same
    approvals row for row. A copy that fails it is removed."""
    try:
        account = pwd.getpwnam(owner) if owner else None
    except KeyError:
        raise ValueError(f"no user {owner!r}") from None
    busy = [source + suffix for suffix in ("-journal", "-wal", "-shm") if os.path.exists(source + suffix)]
    if busy:
        raise ValueError(f"{busy[0]} exists: a write to {source} is in flight or was interrupted. Stop the signer,"
                         " open the ledger once as its owner so SQLite finishes or rolls it back, and copy again")
    created = False
    try:
        with closing(_read_only(source)) as conn:
            conn.execute("BEGIN")
            if not _is_ledger(conn):
                raise ValueError(f"{source} is not a redemption ledger of this version")
            rows = _rows(conn)
            data = Path(source).read_bytes()
            try:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raise FileExistsError(f"{destination} exists; a ledger is never replaced") from None
            created = True
            try:
                if account is not None:
                    os.fchown(fd, account.pw_uid, account.pw_gid)
                remaining = memoryview(data)
                while remaining:
                    remaining = remaining[os.write(fd, remaining):]
                os.fsync(fd)
            finally:
                os.close(fd)
            conn.execute("COMMIT")
        directory = os.open(os.path.dirname(os.path.abspath(destination)), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        proof = _prove_copy(source, destination, hashlib.sha256(data).hexdigest(), rows)
    except BaseException:
        if created:
            os.remove(destination)
        raise
    os.chmod(source, 0o400)
    return {**proof, "source": source, "destination": destination, "bytes": len(data),
            "owner": owner or pwd.getpwuid(os.geteuid()).pw_name, "source_mode": "0400"}


def _prove_copy(source: str, destination: str, sha256: str, rows: list[tuple]) -> dict:
    """The proof that ``destination`` is ``source`` as read: its sha256, SQLite's
    integrity check and every approval row. Raises ValueError naming the
    first that fails."""
    failed = None
    if hashlib.sha256(Path(destination).read_bytes()).hexdigest() != sha256:
        failed = "sha256"
    else:
        try:
            with closing(_read_only(destination)) as copy:
                if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    failed = "integrity_check"
                elif not _is_ledger(copy) or _rows(copy) != rows:
                    failed = "approvals"
                else:
                    count, digest = _digest(copy)
        except sqlite3.DatabaseError:
            failed = "integrity_check"
    if failed:
        raise ValueError(f"the copy at {destination} does not match {source} ({failed}); it was removed")
    return {"sha256": sha256, "integrity_check": "ok", "approvals": count, "approvals_digest": digest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.redemption_ledger",
                                     description="Create, move or digest a signer's redemption ledger.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="an empty ledger, for a signer that has never signed").add_argument("path")
    copy = commands.add_parser("copy", help="move a ledger: byte copy, proof, source left read-only")
    copy.add_argument("--owner", help="the user the copy belongs to (the signer's service user)")
    copy.add_argument("source")
    copy.add_argument("destination")
    commands.add_parser("digest", help="count and digest the approvals every signer records alike").add_argument("path")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            try:
                create_ledger(args.path)
            except FileExistsError:
                raise FileExistsError(f"{args.path} exists; a ledger is never replaced") from None
            print(f"created an empty redemption ledger at {args.path}")
        elif args.command == "copy":
            print(json.dumps(copy_ledger(args.source, args.destination, args.owner), indent=2))
        else:
            count, digest = approvals_digest(args.path)
            print(json.dumps({"path": args.path, "approvals": count, "approvals_digest": digest}, indent=2))
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
