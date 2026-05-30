"""Tests for the pool-tip tx-chaining state machine (services/pool_tip.py).

Covers the seven behaviors the design pins down:
  - cold-start seed (largest confirmed UTxO, depth 0)
  - advance only on submit-accept (not at build)
  - no-advance on build (lock held, tip unchanged until submit)
  - depth-cap rejection (503 POOL_SETTLING at depth 8)
  - eviction rollback (pending root stale past window -> reseed from chain)
  - datum-guard rejection (output[1] without d87980 -> refuse + flush)
  - balance-guard rejection (post-spend balance < 0 -> refuse + flush)

The manager has no Blockfrost dependency — the chain is injected as a
``seed_fn`` callable returning find_pool_utxos-shaped dicts, so every path is
deterministic and offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("NETWORK", "preprod")

from services.pool_tip import (  # noqa: E402
    POOL_OUTPUT_INDEX,
    VOID_DATUM_HEX,
    PoolSettlingError,
    PoolTip,
    PoolTipError,
    PoolTipManager,
    extract_pool_output,
)

SCRIPT_ADDR = "addr_test1wrs6rqdjlzm5he27v9s202p8vjumza8qfsmufm2f6dy68hg7n8k3c"
TX_A = "a" * 64
TX_B = "b" * 64
TX_C = "c" * 64


def _utxo(tx_hash: str, idx: int, cmatra: int, ada: int = 2_000_000) -> dict:
    return {
        "tx_hash": tx_hash,
        "output_index": idx,
        "cmatra_amount": cmatra,
        "ada_amount": ada,
    }


def _pool_output(cmatra: int, *, addr: str = SCRIPT_ADDR,
                 datum: str = VOID_DATUM_HEX, ada: int = 1_500_000) -> dict:
    return {
        "address": addr,
        "datum_hex": datum,
        "cmatra_amount": cmatra,
        "ada_amount": ada,
    }


def _mgr(utxos: list[dict], **kw) -> PoolTipManager:
    return PoolTipManager(lambda: list(utxos), script_address=SCRIPT_ADDR, **kw)


# ---------------------------------------------------------------------------
# Cold-start seed
# ---------------------------------------------------------------------------


def test_seed_takes_largest_confirmed_utxo_at_depth_zero():
    mgr = _mgr([_utxo(TX_A, 1, 100), _utxo(TX_B, 0, 500), _utxo(TX_C, 2, 50)])
    tip = mgr.seed_from_chain()
    assert tip.utxo_ref == f"{TX_B}#0"
    assert tip.cmatra_balance == 500
    assert tip.status == "confirmed"
    assert tip.depth == 0
    assert tip.parent_tx is None
    assert tip.pending_since is None


def test_seed_raises_when_no_pool_utxos():
    mgr = _mgr([])
    with pytest.raises(PoolTipError, match="No confirmed pool UTxO"):
        mgr.seed_from_chain()


def test_pool_tip_dict_round_trips_to_build_shape():
    tip = PoolTip(
        utxo_ref=f"{TX_A}#1", cmatra_balance=42, ada_amount=1_500_000,
        status="pending", parent_tx=None, depth=3, pending_since=None,
    )
    d = tip.as_pool_utxo_dict()
    assert d == {
        "tx_hash": TX_A, "output_index": 1,
        "cmatra_amount": 42, "ada_amount": 1_500_000,
    }
    assert tip.chain_state() == {
        "utxo_ref": f"{TX_A}#1", "balance": 42,
        "status": "pending", "depth": 3,
    }


# ---------------------------------------------------------------------------
# Advance only on submit-accept; build holds the lock but does NOT advance
# ---------------------------------------------------------------------------


def test_acquire_for_build_does_not_advance_tip():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        tip = await mgr.acquire_for_build("tok1")
        # Tip is the seeded confirmed UTxO; nothing has advanced.
        assert tip.utxo_ref == f"{TX_A}#0"
        assert tip.status == "confirmed"
        assert tip.depth == 0
        # Lock is held — a second build cannot acquire until released.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(mgr.acquire_for_build("tok2"), timeout=0.05)
        mgr.release_build("tok1")
    asyncio.run(run())


def test_advance_on_submit_chains_off_submitted_hash_index_1():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        new_tip = mgr.advance_on_submit(
            "tok1", submitted_tx_hash=TX_B, delivered_cmatra=300,
            built_pool_output=_pool_output(700),
        )
        assert new_tip.utxo_ref == f"{TX_B}#1"
        assert new_tip.cmatra_balance == 700
        assert new_tip.status == "pending"
        assert new_tip.parent_tx == f"{TX_A}#0"
        assert new_tip.depth == 1
        assert new_tip.pending_since is not None
        # Lock released — next build can proceed and chains off the pending tip.
        tip2 = await mgr.acquire_for_build("tok2")
        assert tip2.utxo_ref == f"{TX_B}#1"
        assert tip2.cmatra_balance == 700
        mgr.release_build("tok2")
    asyncio.run(run())


def test_release_build_leaves_tip_unchanged_and_frees_lock():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        mgr.release_build("tok1")
        # tip still the confirmed seed, depth 0
        assert mgr.tip.utxo_ref == f"{TX_A}#0"
        assert mgr.tip.depth == 0
        # lock is free
        await asyncio.wait_for(mgr.acquire_for_build("tok2"), timeout=0.05)
        mgr.release_build("tok2")
    asyncio.run(run())


def test_advance_with_stale_token_raises():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        with pytest.raises(PoolTipError, match="stale or unknown build token"):
            mgr.advance_on_submit("WRONG", TX_B, 1, _pool_output(999))
        mgr.release_build("tok1")
    asyncio.run(run())


def test_full_drain_flushes_to_confirmed_no_phantom_output():
    async def run():
        # After drain the chain reports a different confirmed UTxO (the real
        # remaining pool, or a fresh one). Seed must pick it up.
        utxos = [_utxo(TX_A, 0, 300)]
        mgr = _mgr(utxos)
        await mgr.acquire_for_build("tok1")
        # Simulate chain advancing: drained the 300, but a separate confirmed
        # UTxO now exists.
        utxos.append(_utxo(TX_C, 0, 9999))
        new_tip = mgr.advance_on_submit(
            "tok1", submitted_tx_hash=TX_B, delivered_cmatra=300,
            built_pool_output=None,  # no pool-change output when fully drained
        )
        assert new_tip.status == "confirmed"
        assert new_tip.utxo_ref == f"{TX_C}#0"
        assert new_tip.depth == 0
    asyncio.run(run())


def test_zero_balance_with_pool_output_is_rejected():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 300)])
        await mgr.acquire_for_build("tok1")
        with pytest.raises(PoolTipError, match="balance is zero but a pool-change"):
            mgr.advance_on_submit(
                "tok1", TX_B, delivered_cmatra=300,
                built_pool_output=_pool_output(0),
            )
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Depth-cap rejection
# ---------------------------------------------------------------------------


def test_depth_cap_rejects_build_with_pool_settling():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 100_000)], depth_cap=8)
        token = 0
        # Drive the tip to depth 8 via 8 chained advances.
        for _ in range(8):
            token += 1
            await mgr.acquire_for_build(f"tok{token}")
            remaining = mgr.tip.cmatra_balance - 10
            mgr.advance_on_submit(
                f"tok{token}", submitted_tx_hash=f"{token:064x}",
                delivered_cmatra=10, built_pool_output=_pool_output(remaining),
            )
        assert mgr.tip.depth == 8
        assert mgr.tip.status == "pending"
        # Next build must be rejected with POOL_SETTLING, and the lock must be
        # released (not held) so other surrenders aren't stuck behind it.
        with pytest.raises(PoolSettlingError, match="settling at depth 8"):
            await mgr.acquire_for_build("tok-over")
        # Lock free: a (hypothetical) build can still acquire once depth clears.
        assert not mgr._lock.locked()
    asyncio.run(run())


def test_depth_cap_clears_after_reseed_to_confirmed():
    async def run():
        utxos = [_utxo(TX_A, 0, 100_000)]
        mgr = _mgr(utxos, depth_cap=2)
        for i in range(2):
            await mgr.acquire_for_build(f"t{i}")
            rem = mgr.tip.cmatra_balance - 5
            mgr.advance_on_submit(f"t{i}", f"{i:064x}", 5, _pool_output(rem))
        assert mgr.tip.depth == 2
        # The pending root confirms; chain now reports it as a confirmed UTxO.
        utxos[:] = [_utxo(f"{1:064x}", 1, mgr.tip.cmatra_balance)]
        # Manual reseed (what the post-confirm path / watchdog does).
        mgr.seed_from_chain()
        assert mgr.tip.depth == 0
        assert mgr.tip.status == "confirmed"
        # Build allowed again.
        await asyncio.wait_for(mgr.acquire_for_build("t-new"), timeout=0.05)
        mgr.release_build("t-new")
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Datum-guard + balance-guard rejection (the most important guards)
# ---------------------------------------------------------------------------


def test_datum_guard_rejects_missing_void_datum():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        bad = _pool_output(700, datum="")  # no inline datum at all
        with pytest.raises(PoolTipError, match="datum.*Void"):
            mgr.advance_on_submit("tok1", TX_B, 300, bad)
    asyncio.run(run())


def test_datum_guard_rejects_wrong_datum():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        bad = _pool_output(700, datum="d87a9f00ff")  # some non-Void constr
        with pytest.raises(PoolTipError, match="datum"):
            mgr.advance_on_submit("tok1", TX_B, 300, bad)
        # Tip must NOT have advanced.
        assert mgr.tip.utxo_ref == f"{TX_A}#0"
        assert mgr.tip.depth == 0
    asyncio.run(run())


def test_datum_guard_rejects_wrong_script_address():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        bad = _pool_output(700, addr="addr_test1wq_someone_else")
        with pytest.raises(PoolTipError, match="address"):
            mgr.advance_on_submit("tok1", TX_B, 300, bad)
    asyncio.run(run())


def test_balance_guard_rejects_negative_post_spend():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 100)])
        await mgr.acquire_for_build("tok1")
        # Deliver more than the tip holds.
        with pytest.raises(PoolTipError, match="post-spend balance .* < 0"):
            mgr.advance_on_submit("tok1", TX_B, delivered_cmatra=500,
                                  built_pool_output=_pool_output(0))
        assert mgr.tip.utxo_ref == f"{TX_A}#0"
    asyncio.run(run())


def test_balance_guard_rejects_output_balance_mismatch():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        # delivered=300 implies remainder 700, but the built output claims 999.
        with pytest.raises(PoolTipError, match="balance 999 != expected"):
            mgr.advance_on_submit("tok1", TX_B, 300, _pool_output(999))
    asyncio.run(run())


def test_guard_failure_releases_lock():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        with pytest.raises(PoolTipError):
            mgr.advance_on_submit("tok1", TX_B, 300, _pool_output(700, datum="ff"))
        # Even though advance raised, the lock must be free for the next build.
        assert not mgr._lock.locked()
        await asyncio.wait_for(mgr.acquire_for_build("tok2"), timeout=0.05)
        mgr.release_build("tok2")
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Eviction watchdog rollback
# ---------------------------------------------------------------------------


def test_is_pending_stale_only_after_window():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)], eviction_window_s=300)
        await mgr.acquire_for_build("tok1")
        mgr.advance_on_submit("tok1", TX_B, 100, _pool_output(900))
        # Just advanced — not stale.
        assert not mgr.is_pending_stale()
        # Backdate pending_since beyond the window.
        mgr.tip.pending_since -= 301
        assert mgr.is_pending_stale()
    asyncio.run(run())


def test_evict_rolls_back_when_root_unconfirmed_past_window():
    async def run():
        utxos = [_utxo(TX_A, 0, 1000)]
        mgr = _mgr(utxos, eviction_window_s=300)
        await mgr.acquire_for_build("tok1")
        mgr.advance_on_submit("tok1", TX_B, 100, _pool_output(900))
        assert mgr.tip.status == "pending"
        mgr.tip.pending_since -= 301  # force stale
        # Chain still does not show TX_B; but a confirmed UTxO is reported.
        utxos[:] = [_utxo(TX_A, 0, 1000)]
        rolled = await mgr.evict_if_stale(is_confirmed_fn=lambda h: False)
        assert rolled is True
        assert mgr.tip.status == "confirmed"
        assert mgr.tip.utxo_ref == f"{TX_A}#0"
        assert mgr.tip.depth == 0
    asyncio.run(run())


def test_evict_no_rollback_when_root_confirmed():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)], eviction_window_s=300)
        await mgr.acquire_for_build("tok1")
        mgr.advance_on_submit("tok1", TX_B, 100, _pool_output(900))
        mgr.tip.pending_since -= 301
        # Blockfrost now confirms TX_B -> no rollback, tip stays pending-chainable.
        rolled = await mgr.evict_if_stale(is_confirmed_fn=lambda h: True)
        assert rolled is False
        assert mgr.tip.utxo_ref == f"{TX_B}#1"
    asyncio.run(run())


def test_evict_noop_when_not_stale():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)], eviction_window_s=300)
        await mgr.acquire_for_build("tok1")
        mgr.advance_on_submit("tok1", TX_B, 100, _pool_output(900))
        rolled = await mgr.evict_if_stale(is_confirmed_fn=lambda h: False)
        assert rolled is False
        assert mgr.tip.status == "pending"
    asyncio.run(run())


def test_evict_deferred_when_build_in_flight():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)], eviction_window_s=300)
        await mgr.acquire_for_build("tok1")
        mgr.advance_on_submit("tok1", TX_B, 100, _pool_output(900))
        mgr.tip.pending_since -= 301
        # A new build grabs the lock (chaining off the pending tip).
        await mgr.acquire_for_build("tok2")
        rolled = await mgr.evict_if_stale(is_confirmed_fn=lambda h: False)
        assert rolled is False  # deferred — build holds the lock
        assert mgr.tip.status == "pending"
        mgr.release_build("tok2")
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Stuck-build sweeper (signing timeout)
# ---------------------------------------------------------------------------


def test_sweep_reclaims_lock_after_signing_timeout():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)], signing_timeout_s=90)
        await mgr.acquire_for_build("tok1")
        # Not yet timed out.
        assert await mgr.sweep_stuck_build() is False
        assert mgr._lock.locked()
        # Backdate the build start beyond the timeout.
        mgr._build_started_at -= 91
        assert await mgr.sweep_stuck_build() is True
        assert not mgr._lock.locked()
        # Tip unchanged (no advance on timeout).
        assert mgr.tip.utxo_ref == f"{TX_A}#0"
        assert mgr.tip.depth == 0
    asyncio.run(run())


def test_sweep_noop_when_no_build_in_flight():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        assert await mgr.sweep_stuck_build() is False
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Output-index determinism — extract_pool_output reads output[1] from a real
# pycardano-built tx body, exactly as the surrender builder lays it out.
# ---------------------------------------------------------------------------


def _build_three_output_tx(pool_cmatra: int, *, with_datum: bool = True,
                           include_pool_output: bool = True):
    import cbor2
    from cbor2 import CBORTag
    from pycardano import (
        Asset, AssetName, MultiAsset, Network, PaymentKeyPair, RawPlutusData,
        TransactionBody, TransactionInput, TransactionOutput, Value,
    )
    from pycardano import Address as PycAddress
    from pycardano.hash import ScriptHash as PycScriptHash, TransactionId

    kp = PaymentKeyPair.generate()
    user = PycAddress(payment_part=kp.verification_key.hash(), network=Network.TESTNET)
    script = PycAddress.from_primitive(SCRIPT_ADDR)
    void = RawPlutusData(cbor2.loads(cbor2.dumps(CBORTag(121, []))))
    pol = PycScriptHash(bytes.fromhex("ab" * 28))
    an = AssetName(bytes.fromhex("634d41545241"))

    def cmatra(n: int) -> MultiAsset:
        ma = MultiAsset()
        ma[pol] = Asset({an: n})
        return ma

    # (0) cMATRA->user  (1) pool-change->script(+datum)  (2) quarantine
    outputs = [TransactionOutput(user, Value(1_500_000, cmatra(300)))]
    if include_pool_output:
        kw = {"datum": void} if with_datum else {}
        outputs.append(TransactionOutput(script, Value(1_500_000, cmatra(pool_cmatra)), **kw))
    outputs.append(TransactionOutput(user, Value(1_500_000)))

    ti = TransactionInput(TransactionId(bytes.fromhex("cd" * 32)), 0)
    body = TransactionBody(inputs=[ti], outputs=outputs, fee=200_000)

    class _Tx:
        transaction_body = body

    return _Tx()


def test_extract_pool_output_reads_index_1_address_datum_balance():
    assert POOL_OUTPUT_INDEX == 1
    tx = _build_three_output_tx(700)
    out = extract_pool_output(tx)
    assert out is not None
    assert out["address"] == SCRIPT_ADDR
    assert out["datum_hex"] == VOID_DATUM_HEX
    assert out["cmatra_amount"] == 700
    assert out["ada_amount"] == 1_500_000


def test_extract_pool_output_none_when_no_pool_output():
    tx = _build_three_output_tx(0, include_pool_output=False)
    # Only outputs[0]=user and outputs[1]=quarantine now; index 1 is NOT the
    # script pool output. The guard relies on the datum check to reject it.
    out = extract_pool_output(tx)
    # output[1] exists (quarantine) but carries no inline datum -> guard rejects.
    assert out is None or out["datum_hex"] != VOID_DATUM_HEX


def test_extract_pool_output_datum_hex_none_without_inline_datum():
    tx = _build_three_output_tx(700, with_datum=False)
    out = extract_pool_output(tx)
    assert out is not None
    assert out["datum_hex"] is None  # datum-hash-only / no datum -> guard rejects


def test_advance_rejects_real_built_tx_without_datum():
    """End-to-end: a real pycardano tx whose output[1] lost its datum must
    fail the advance guard — the v1-catastrophe shape."""
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        tx = _build_three_output_tx(700, with_datum=False)
        built = extract_pool_output(tx)
        with pytest.raises(PoolTipError, match="datum"):
            mgr.advance_on_submit("tok1", TX_B, 300, built)
        assert mgr.tip.utxo_ref == f"{TX_A}#0"  # tip not advanced
    asyncio.run(run())


def test_advance_accepts_real_built_tx_with_datum():
    async def run():
        mgr = _mgr([_utxo(TX_A, 0, 1000)])
        await mgr.acquire_for_build("tok1")
        tx = _build_three_output_tx(700, with_datum=True)
        built = extract_pool_output(tx)
        new_tip = mgr.advance_on_submit("tok1", TX_B, 300, built)
        assert new_tip.utxo_ref == f"{TX_B}#1"
        assert new_tip.cmatra_balance == 700
        assert new_tip.status == "pending"
    asyncio.run(run())


# ---------------------------------------------------------------------------
# L1 reconciliation — the tip must agree with the live pool UTxO set, not just
# with "did the tip's tx confirm?". A confirmed tip whose OUTPUT has been spent
# (the chain moved on without us) must re-seed; a confirmed tip that is still
# the live pool is promoted to confirmed/depth-0. This is the fix for the
# poisoned-tip incident: the tip stuck on a spent UTxO, every build 503/422'd
# with extraRedeemers, and the confirm-only watchdog never re-seeded.
# ---------------------------------------------------------------------------


def test_reconcile_reseeds_when_confirmed_tip_spent():
    """Incident repro: tip advanced to TX_B#1; TX_B confirmed; but TX_B#1 was
    then spent and the live pool is now TX_C#1. Reconcile must re-seed."""
    async def run():
        utxos = [_utxo(TX_A, 0, 1000)]
        mgr = _mgr(utxos)
        await mgr.acquire_for_build("tok1")
        mgr.advance_on_submit("tok1", TX_B, 100, _pool_output(900))
        assert mgr.tip.utxo_ref == f"{TX_B}#1"
        # TX_B's tx is on-chain, but TX_B#1 has been spent; the live pool moved
        # to a different confirmed UTxO.
        utxos[:] = [_utxo(TX_C, 1, 850)]
        reseeded = await mgr.reconcile_with_chain(
            is_confirmed_fn=lambda h: True, live_pool_fn=lambda: list(utxos),
        )
        assert reseeded is True
        assert mgr.tip.utxo_ref == f"{TX_C}#1"
        assert mgr.tip.status == "confirmed"
        assert mgr.tip.depth == 0
        assert mgr.tip.cmatra_balance == 850
    asyncio.run(run())


def test_reconcile_promotes_confirmed_pending_tip_still_live():
    """A pending tip whose tx confirmed AND is still the live pool is promoted
    to confirmed/depth-0 (this clears the depth cap — a wave settled)."""
    async def run():
        utxos = [_utxo(TX_A, 0, 1000)]
        mgr = _mgr(utxos)
        await mgr.acquire_for_build("tok1")
        mgr.advance_on_submit("tok1", TX_B, 100, _pool_output(900))
        assert mgr.tip.status == "pending" and mgr.tip.depth == 1
        utxos[:] = [_utxo(TX_B, 1, 900)]  # TX_B#1 IS the live pool now
        promoted = await mgr.reconcile_with_chain(lambda h: True, lambda: list(utxos))
        assert promoted is True
        assert mgr.tip.utxo_ref == f"{TX_B}#1"
        assert mgr.tip.status == "confirmed"
        assert mgr.tip.depth == 0
    asyncio.run(run())


def test_reconcile_noop_when_tip_tx_unconfirmed():
    """A pending tip still in the mempool must be left alone — healthy chaining;
    eviction owns the stalled-unconfirmed case."""
    async def run():
        utxos = [_utxo(TX_A, 0, 1000)]
        mgr = _mgr(utxos)
        await mgr.acquire_for_build("tok1")
        mgr.advance_on_submit("tok1", TX_B, 100, _pool_output(900))
        out = await mgr.reconcile_with_chain(lambda h: False, lambda: list(utxos))
        assert out is False
        assert mgr.tip.utxo_ref == f"{TX_B}#1"
        assert mgr.tip.status == "pending"
        assert mgr.tip.depth == 1
    asyncio.run(run())


def test_reconcile_noop_when_confirmed_depth0_and_live():
    async def run():
        utxos = [_utxo(TX_A, 0, 1000)]
        mgr = _mgr(utxos)
        mgr.seed_from_chain()
        out = await mgr.reconcile_with_chain(lambda h: True, lambda: list(utxos))
        assert out is False
        assert mgr.tip.utxo_ref == f"{TX_A}#0"
        assert mgr.tip.depth == 0
    asyncio.run(run())


def test_reconcile_reseeds_confirmed_depth0_tip_spent_externally():
    """A confirmed depth-0 resting tip spent out-of-band (admin rotate, another
    surrender path) must re-seed to the new live pool."""
    async def run():
        utxos = [_utxo(TX_A, 0, 1000)]
        mgr = _mgr(utxos)
        mgr.seed_from_chain()
        utxos[:] = [_utxo(TX_C, 1, 1000)]  # TX_A#0 spent; pool now TX_C#1
        out = await mgr.reconcile_with_chain(lambda h: True, lambda: list(utxos))
        assert out is True
        assert mgr.tip.utxo_ref == f"{TX_C}#1"
    asyncio.run(run())


def test_reconcile_deferred_when_build_in_flight():
    async def run():
        utxos = [_utxo(TX_A, 0, 1000)]
        mgr = _mgr(utxos)
        mgr.seed_from_chain()
        utxos[:] = [_utxo(TX_C, 1, 1000)]
        await mgr.acquire_for_build("tok1")  # build holds the lock
        out = await mgr.reconcile_with_chain(lambda h: True, lambda: list(utxos))
        assert out is False  # deferred — must not stomp an in-flight build
        assert mgr.tip.utxo_ref == f"{TX_A}#0"
        mgr.release_build("tok1")
    asyncio.run(run())
