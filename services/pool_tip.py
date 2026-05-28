"""Pool-tip tx-chaining state machine for the cMATRA surrender API.

The surrender pool is a single on-chain UTxO that every surrender spends and
recreates as a pool-change output. Blockfrost lags the mempool ~20-60s, so a
re-query per build cannot see the prior surrender's pending change output and
503s ("No available pool UTxO"). This serializes all surrenders and strands
large (chunked) wallets.

This module tracks the pool tip in memory and chains each surrender off the
prior submit's pending pool-change output (Cardano permits spending an
unconfirmed mempool output). It mirrors the production anchor-worker burst
mode (PendingUtxoCache + withMutex + advance-post-submit + flush-on-stale +
depth cap).

Correctness rests on three guards, none of which is self-asserted:
  1. The tip advances ONLY after a submit returns mempool-accept.
  2. Before every advance, the built pool output is asserted to be the script
     address carrying the Void (`d87980`) inline datum, and the post-spend
     balance is asserted >= 0. (Datum discipline — the v1 catastrophe locked
     722.5M cMATRA from one missing datum.)
  3. An independent watchdog rolls the tip back to the last Blockfrost-confirmed
     state when a pending chain stalls. The tip is verifiable against L1.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("surrender_api.pool_tip")

# Void datum = Constr(0, []) = CBOR d87980. The on-chain validator's
# `expect Some(_) = datum` requires every pool output to carry it.
VOID_DATUM_HEX = "d87980"

# The pool-change output is ALWAYS index 1. Determinism (proven in the design
# doc): _build_cosigned_surrender_tx adds outputs (0) cMATRA->user, (1)
# pool-change->script, (2) quarantine; builder.build() defaults
# merge_change=False; pycardano sorts inputs but NOT explicit outputs and
# appends change AFTER them. So index 1 is fixed even if change splits into
# 2+ outputs.
POOL_OUTPUT_INDEX = 1


def extract_pool_output(tx: Any, output_index: int = POOL_OUTPUT_INDEX) -> dict[str, Any] | None:
    """Read output[``output_index``] from a built pycardano ``Transaction`` and
    return the structural facts the tip-advance datum guard needs:
    ``{address, datum_hex, cmatra_amount, ada_amount}``.

    Returns None if the tx has no such output (e.g. the pool was fully
    drained, so there is no pool-change output to chain off). The caller's
    guard treats None as "do not advance".

    This is the structural assertion the design mandates: output[1]
    determinism rests on pycardano not reordering explicit outputs and
    ``merge_change=False``. Reading it back from the signed body — rather than
    trusting the build inputs — is what makes a future refactor that silently
    moves the pool output fail the guard instead of poisoning the tip.
    """
    body = tx.transaction_body
    outputs = body.outputs
    if output_index >= len(outputs):
        return None
    out = outputs[output_index]

    addr = str(out.address) if out.address is not None else None

    # Inline datum -> canonical hex. pycardano exposes the inline datum on the
    # output; RawPlutusData/PlutusData both implement to_cbor() (bytes in
    # 0.13-0.19). A datum-hash-only output (no inline) yields None here, which
    # the guard rejects.
    datum_hex: str | None = None
    inline = getattr(out, "datum", None)
    if inline is not None and hasattr(inline, "to_cbor"):
        cb = inline.to_cbor()
        datum_hex = cb.hex() if isinstance(cb, (bytes, bytearray)) else str(cb)

    amount = out.amount
    if isinstance(amount, int):
        ada_amount, cmatra_amount = amount, 0
    else:
        ada_amount = amount.coin
        cmatra_amount = 0
        mi = getattr(amount, "multi_asset", None)
        if mi:
            # Sum every asset under every policy — the pool output carries
            # exactly one (cMATRA); summing is robust if that ever changes.
            for _policy, assets in mi.data.items() if hasattr(mi, "data") else mi.items():
                for _asset, qty in (assets.data.items() if hasattr(assets, "data") else assets.items()):
                    cmatra_amount += int(qty)

    return {
        "address": addr,
        "datum_hex": datum_hex,
        "cmatra_amount": cmatra_amount,
        "ada_amount": ada_amount,
    }


class PoolTipError(Exception):
    """Raised when a tip operation violates an invariant (bad datum, negative
    balance, depth cap). Callers translate these into HTTP responses."""


class PoolSettlingError(PoolTipError):
    """Raised at the depth cap: the pending chain must confirm before more
    surrenders can build. Maps to HTTP 503 {code: POOL_SETTLING}."""


@dataclass
class PoolTip:
    """In-memory view of the current spendable pool UTxO.

    ``utxo_ref`` is "txhash#index". When ``status`` is "pending" the referenced
    output exists only in the mempool (the change output of a just-submitted
    surrender); when "confirmed" it is the largest UTxO Blockfrost reports at
    the script address.
    """

    utxo_ref: str
    cmatra_balance: int
    ada_amount: int
    status: str  # "confirmed" | "pending"
    parent_tx: str | None
    depth: int
    pending_since: float | None

    @property
    def tx_hash(self) -> str:
        return self.utxo_ref.split("#", 1)[0]

    @property
    def output_index(self) -> int:
        return int(self.utxo_ref.split("#", 1)[1])

    def as_pool_utxo_dict(self) -> dict[str, Any]:
        """Reconstruct the ``pool_utxo`` dict shape that
        ``_build_cosigned_surrender_tx`` consumes (no signature change)."""
        return {
            "tx_hash": self.tx_hash,
            "output_index": self.output_index,
            "cmatra_amount": self.cmatra_balance,
            "ada_amount": self.ada_amount,
        }

    def chain_state(self) -> dict[str, Any]:
        """Compact view for the /status probe (mirrors anchor-worker
        /status.chainState)."""
        return {
            "utxo_ref": self.utxo_ref,
            "balance": self.cmatra_balance,
            "status": self.status,
            "depth": self.depth,
        }


# Type of the seed callback: returns the list of confirmed pool UTxO dicts
# (as find_pool_utxos does), largest-first. Kept as a callable so the manager
# has no Blockfrost dependency and is unit-testable.
SeedFn = Callable[[], list[dict[str, Any]]]


class PoolTipManager:
    """Single-flight pool-tip chainer.

    A global :class:`asyncio.Lock` is held from build start through submit
    accept (or build failure / signing timeout) so two concurrent surrenders
    can never read the same tip. The lock is acquired in
    :meth:`acquire_for_build` and released in :meth:`advance_on_submit`,
    :meth:`release_build` (failure path), or by the timeout sweeper.
    """

    def __init__(
        self,
        seed_fn: SeedFn,
        *,
        script_address: str,
        depth_cap: int = 8,
        signing_timeout_s: float = 90.0,
        eviction_window_s: float = 240.0,
    ) -> None:
        self._seed_fn = seed_fn
        self._script_address = script_address
        self._depth_cap = depth_cap
        self._signing_timeout_s = signing_timeout_s
        self._eviction_window_s = eviction_window_s

        self._tip: PoolTip | None = None
        self._lock = asyncio.Lock()
        # Identifies which build currently holds the lock so a late/duplicate
        # submit or a timeout sweep only releases the build it belongs to.
        self._build_token: str | None = None
        self._build_started_at: float | None = None

    # -- properties ------------------------------------------------------

    @property
    def tip(self) -> PoolTip | None:
        return self._tip

    @property
    def depth_cap(self) -> int:
        return self._depth_cap

    def chain_state(self) -> dict[str, Any] | None:
        return self._tip.chain_state() if self._tip else None

    # -- seeding ---------------------------------------------------------

    def seed_from_chain(self) -> PoolTip:
        """Cold-start / rollback: take the largest confirmed pool UTxO as the
        tip at depth 0. Raises if the chain reports no funded pool UTxO."""
        pool_utxos = self._seed_fn()
        if not pool_utxos:
            raise PoolTipError("No confirmed pool UTxO available to seed tip")
        # find_pool_utxos already sorts largest-first; be defensive anyway.
        best = max(pool_utxos, key=lambda u: u["cmatra_amount"])
        self._tip = PoolTip(
            utxo_ref=f"{best['tx_hash']}#{best['output_index']}",
            cmatra_balance=best["cmatra_amount"],
            ada_amount=best["ada_amount"],
            status="confirmed",
            parent_tx=None,
            depth=0,
            pending_since=None,
        )
        logger.info(
            "Pool tip seeded from chain: %s (%d cMATRA, depth 0)",
            self._tip.utxo_ref, self._tip.cmatra_balance,
        )
        return self._tip

    def _ensure_tip(self) -> PoolTip:
        if self._tip is None:
            return self.seed_from_chain()
        return self._tip

    # -- build lock ------------------------------------------------------

    async def acquire_for_build(self, build_token: str) -> PoolTip:
        """Acquire the single-flight lock and return the current tip to build
        against. The caller MUST eventually call :meth:`advance_on_submit`
        (success) or :meth:`release_build` (failure). Held across the
        signing round-trip; the timeout sweeper reclaims a stuck lock.

        Raises :class:`PoolSettlingError` at the depth cap, after releasing
        the lock so other surrenders aren't blocked behind a rejected build.
        """
        await self._lock.acquire()
        try:
            tip = self._ensure_tip()
            if tip.depth >= self._depth_cap and tip.status == "pending":
                raise PoolSettlingError(
                    f"Pool settling at depth {tip.depth}; wait for the pending "
                    f"root to confirm"
                )
            self._build_token = build_token
            self._build_started_at = time.monotonic()
            return tip
        except BaseException:
            # Never leave the lock held if we reject the build.
            self._build_token = None
            self._build_started_at = None
            self._lock.release()
            raise

    def release_build(self, build_token: str) -> None:
        """Release the lock without advancing (build/preflight/submit
        failure or signing timeout). The tip is left unchanged. Idempotent
        and token-guarded so a stale caller can't release someone else's
        build."""
        if self._build_token != build_token:
            return
        self._build_token = None
        self._build_started_at = None
        if self._lock.locked():
            self._lock.release()

    def advance_on_submit(
        self,
        build_token: str,
        submitted_tx_hash: str,
        delivered_cmatra: int,
        built_pool_output: dict[str, Any] | None,
    ) -> PoolTip:
        """Advance the tip after a submit returns mempool-accept, then release
        the lock.

        ``built_pool_output`` is the pool-change output[1] extracted from the
        signed tx: ``{address, datum_hex, cmatra_amount, ada_amount}`` — or
        None when the surrender drained the pool to zero (no pool-change
        output). The guards run before any state mutation:

          * output[1] address == script address
          * output[1] datum == d87980
          * post-spend balance (== built_pool_output.cmatra_amount) >= 0

        On guard failure the tip is flushed to confirmed (rolled back from
        Blockfrost) and :class:`PoolTipError` is raised — the surrender
        already landed in the mempool, but we refuse to chain off an output
        we can't prove is a valid, spendable, datum-carrying pool UTxO.
        """
        if self._build_token != build_token:
            raise PoolTipError(
                "advance_on_submit called with a stale or unknown build token"
            )
        tip = self._tip
        assert tip is not None  # lock was held since acquire_for_build seeded it

        try:
            new_balance = tip.cmatra_balance - delivered_cmatra
            if new_balance < 0:
                raise PoolTipError(
                    f"post-spend balance {new_balance} < 0 "
                    f"(tip {tip.cmatra_balance} - delivered {delivered_cmatra})"
                )

            if new_balance == 0:
                # Pool fully drained: there is no pool-change output to chain
                # off. Don't advance to a phantom output — flush to confirmed
                # and let the next build re-seed from chain.
                if built_pool_output is not None:
                    raise PoolTipError(
                        "balance is zero but a pool-change output was built"
                    )
                logger.info(
                    "Pool drained to zero by tx %s; flushing tip to confirmed",
                    submitted_tx_hash[:16],
                )
                self._flush_to_confirmed_locked("pool drained to zero")
                return self._tip  # re-seeded confirmed tip

            # Non-zero remainder: a pool-change output MUST exist and pass the
            # datum + address + balance guards (the single most important
            # check in this module).
            self._assert_valid_pool_output(built_pool_output, new_balance)
            assert built_pool_output is not None

            self._tip = PoolTip(
                utxo_ref=f"{submitted_tx_hash}#1",
                cmatra_balance=new_balance,
                ada_amount=built_pool_output["ada_amount"],
                status="pending",
                parent_tx=tip.utxo_ref,
                depth=tip.depth + 1,
                pending_since=time.time(),
            )
            logger.info(
                "Pool tip advanced: %s -> %s (%d cMATRA, depth %d, pending)",
                tip.utxo_ref, self._tip.utxo_ref,
                self._tip.cmatra_balance, self._tip.depth,
            )
            return self._tip
        finally:
            self._build_token = None
            self._build_started_at = None
            if self._lock.locked():
                self._lock.release()

    def _assert_valid_pool_output(
        self, built_pool_output: dict[str, Any] | None, expected_balance: int,
    ) -> None:
        """Guard 2: the built pool output[1] must be the script address with a
        Void inline datum, holding exactly the expected remaining balance."""
        if built_pool_output is None:
            raise PoolTipError(
                "no pool-change output[1] in built tx (expected non-zero "
                "remainder) — refusing to advance tip"
            )
        addr = built_pool_output.get("address")
        if addr != self._script_address:
            raise PoolTipError(
                f"pool output[1] address {addr!r} != script address "
                f"{self._script_address!r}"
            )
        datum = (built_pool_output.get("datum_hex") or "").lower()
        if datum != VOID_DATUM_HEX:
            raise PoolTipError(
                f"pool output[1] datum {datum!r} != Void {VOID_DATUM_HEX!r} "
                f"— refusing to advance tip (datum discipline)"
            )
        built_balance = built_pool_output.get("cmatra_amount")
        if built_balance != expected_balance:
            raise PoolTipError(
                f"pool output[1] balance {built_balance} != expected "
                f"remainder {expected_balance}"
            )

    # -- rollback / eviction --------------------------------------------

    def _flush_to_confirmed_locked(self, reason: str) -> PoolTip:
        """Re-seed the tip from Blockfrost-confirmed state. Caller holds the
        lock (advance path) or no build is in flight (watchdog path)."""
        logger.warning("Flushing pool tip to confirmed state: %s", reason)
        return self.seed_from_chain()

    def is_pending_stale(self, now: float | None = None) -> bool:
        """True when the tip is pending and has not confirmed within the
        eviction window. Used by the watchdog."""
        tip = self._tip
        if tip is None or tip.status != "pending" or tip.pending_since is None:
            return False
        now = time.time() if now is None else now
        return (now - tip.pending_since) >= self._eviction_window_s

    async def evict_if_stale(self, is_confirmed_fn: Callable[[str], bool]) -> bool:
        """Independent watchdog step (guard 3). If the pending tip's root tx
        has not confirmed on Blockfrost within the eviction window, roll the
        tip back to confirmed state and reset depth.

        ``is_confirmed_fn(tx_hash) -> bool`` checks Blockfrost; True short-
        circuits the rollback (the chain caught up). Returns True if a
        rollback occurred.

        The rollback acquires the build lock so it cannot race a build that is
        mid-flight — if a build holds the lock we skip this round and retry on
        the next tick.
        """
        if not self.is_pending_stale():
            return False

        tip = self._tip
        assert tip is not None
        root_hash = tip.tx_hash

        # Confirmation check happens off-lock (it's a network call); if the
        # root confirmed, promote in place without forcing a rollback.
        try:
            confirmed = await asyncio.to_thread(is_confirmed_fn, root_hash)
        except Exception as e:  # network hiccup — try again next tick
            logger.warning("Eviction confirm-check failed for %s: %s",
                           root_hash[:16], e)
            return False

        if confirmed:
            # Root landed — the chain is no longer behind. Leave the tip;
            # subsequent builds keep chaining. Depth resets naturally once a
            # fresh seed happens, but a confirmed root is safe to chain off.
            logger.info("Pending tip root %s confirmed on-chain; no rollback",
                        root_hash[:16])
            return False

        # Stuck. Acquire the lock so we don't stomp an in-flight build.
        if self._lock.locked():
            logger.info(
                "Tip eviction deferred: build in flight (root %s still pending)",
                root_hash[:16],
            )
            return False
        await self._lock.acquire()
        try:
            # Re-check under the lock — state may have moved.
            if not self.is_pending_stale():
                return False
            current_root = self._tip.tx_hash if self._tip else None
            if current_root != root_hash:
                return False
            self._flush_to_confirmed_locked(
                f"pending root {root_hash[:16]} unconfirmed past "
                f"{self._eviction_window_s:.0f}s eviction window"
            )
            return True
        finally:
            if self._lock.locked():
                self._lock.release()

    async def sweep_stuck_build(self, now: float | None = None) -> bool:
        """Release a build lock held longer than the signing timeout (the
        frontend never came back with a signature). The tip is left
        unchanged. Returns True if a stuck build was reclaimed."""
        if self._build_token is None or self._build_started_at is None:
            return False
        now = time.monotonic() if now is None else now
        if (now - self._build_started_at) < self._signing_timeout_s:
            return False
        logger.warning(
            "Reclaiming stuck build lock (token %s held %.0fs > %.0fs timeout)",
            self._build_token, now - self._build_started_at,
            self._signing_timeout_s,
        )
        self._build_token = None
        self._build_started_at = None
        if self._lock.locked():
            self._lock.release()
        return True
