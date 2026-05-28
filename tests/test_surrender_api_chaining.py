"""Integration tests for pool-tip chaining wired through the surrender_api
build/submit endpoints.

These drive the async endpoint coroutines directly (via asyncio.run) against
the real module state, with the heavy bits mocked (admin key, pycardano build,
Blockfrost submit). Driving the coroutines directly — rather than through
Starlette's TestClient — avoids the TestClient single-portal limitation with
asyncio.to_thread while exercising the exact build->submit->advance code path,
the single-flight lock, the depth cap, the datum guard, and cold-start re-seed.

Proven here (the HTTP-layer contract the design pins down):
  - build does NOT advance the tip; submit-accept does
  - the single-flight lock is held build->submit
  - chunk N+1 chains off chunk N's submitted-hash#1 while N is unconfirmed
  - depth cap raises 503 POOL_SETTLING and leaves the lock free
  - the datum guard rejects a post-submit advance when output[1] lost its datum
    (user still gets success; tip flushes to confirmed)
  - submit failure releases the lock, tip unchanged
  - cold-start re-seeds the tip from confirmed Blockfrost state
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("NETWORK", "preprod")
os.environ.setdefault("SURRENDER_API_SECRET", "test-secret")

import services.surrender_api as api  # noqa: E402
from services.pool_tip import PoolTipManager, VOID_DATUM_HEX  # noqa: E402

SCRIPT_ADDR = "addr_test1wrs6rqdjlzm5he27v9s202p8vjumza8qfsmufm2f6dy68hg7n8k3c"
# 58-char placeholder — satisfies BuildSurrenderRequest min_length(40).
USER_ADDR = "addr_test1" + "q" * 50
NFT_UNIT = "cd" * 28 + "4e4654"


def _req():
    from services.surrender_api import AssetToSurrender, BuildSurrenderRequest
    return BuildSurrenderRequest(
        user_address=USER_ADDR,
        assets=[AssetToSurrender(asset_key="AGENT", quantity_base=1,
                                 nft_units=[NFT_UNIT])],
    )


def _submit_req(tx_hash: str):
    from services.surrender_api import SubmitRequest
    # Even-length hex (witness merge is mocked, so contents don't matter).
    return SubmitRequest(tx_cbor_hex="a10081820058" + "00" * 34, tx_hash=tx_hash)


@pytest.fixture
def wired(monkeypatch):
    """Wire module state with a seeded confirmed UTxO and all chain/crypto
    calls mocked. _build_tx_blocking is replaced so build_surrender's
    asyncio.to_thread runs a trivial in-process function."""
    confirmed = [{
        "tx_hash": "a" * 64, "output_index": 0,
        "cmatra_amount": 1000, "ada_amount": 2_000_000,
    }]

    monkeypatch.setattr(api, "SCRIPT_ADDRESS", SCRIPT_ADDR)
    monkeypatch.setattr(api, "CMATRA_POLICY_HEX", "ab" * 28)
    monkeypatch.setattr(api, "CMATRA_ASSET_HEX", "634d41545241")
    monkeypatch.setattr(api, "QUARANTINE_ADDRESS", SCRIPT_ADDR)
    monkeypatch.setattr(api, "ALLOWED_USER_ADDRESSES", frozenset())
    monkeypatch.setattr(api, "POOL_TIP_DEPTH_CAP", 8)
    monkeypatch.setattr(api, "find_pool_utxos",
                        lambda *a, **k: [dict(u) for u in confirmed])
    monkeypatch.setattr(api, "compute_redemption", lambda rt, k, q: 100)
    monkeypatch.setattr(api, "_resolve_legacy_assets",
                        lambda *a, **k: [{"policy_hex": "cd" * 28,
                                          "asset_hex": "4e4654", "quantity": 1}])

    class _FakeAddr:
        payment_part = b"\x00" * 28  # not a PycScriptHash
        staking_part = None
        network = 0
    monkeypatch.setattr(api.Address, "from_primitive",
                        staticmethod(lambda s: _FakeAddr()))

    api.state.rate_table = {"tokens": {"AGENT": {"rate_base_per_unit": 1}}}
    api.state.script_cbor_hex = "00"
    api.state.admin_sk = object()

    class _Sub:
        n = 0

        def submit_tx(self, b):
            _Sub.n += 1
            return f"{_Sub.n:064x}"
    api.state.bf = _Sub()

    def good_build(user_address, total_cmatra, legacy_assets, pool_utxo):
        good_build.n = getattr(good_build, "n", 0) + 1
        rem = pool_utxo["cmatra_amount"] - total_cmatra
        pool_out = None
        if rem > 0:
            pool_out = {"address": SCRIPT_ADDR, "datum_hex": VOID_DATUM_HEX,
                        "cmatra_amount": rem, "ada_amount": 1_500_000}
        return "8400", f"dead{good_build.n:060x}", pool_out
    monkeypatch.setattr(api, "_build_tx_blocking", good_build)
    monkeypatch.setattr(api, "_merge_wallet_witnesses",
                        lambda orig, wallet: b"\x84merged")

    # Fresh manager + clean stashes (no watchdog task in these tests).
    api.state.tip_mgr = PoolTipManager(
        api._seed_pool_utxos, script_address=SCRIPT_ADDR, depth_cap=8)
    api.state.build_ctx = {}
    api.state.pending_tx_hashes = {}
    api.state.pending_tx_cbor = {}

    yield confirmed

    api.state.tip_mgr = None
    api.state.build_ctx = {}
    api.state.pending_tx_hashes = {}
    api.state.pending_tx_cbor = {}


def test_build_does_not_advance_tip_submit_does(wired):
    async def run():
        assert api.state.tip_mgr.tip is None
        b = await api.build_surrender(_req())
        assert b.pool_utxo_used == f"{'a'*64}#0"
        tip = api.state.tip_mgr.tip
        assert tip.status == "confirmed" and tip.depth == 0
        assert tip.cmatra_balance == 1000  # unchanged at build time

        s = await api.submit_surrender(_submit_req(b.tx_hash))
        tip = api.state.tip_mgr.tip
        assert tip.utxo_ref == f"{s.tx_hash}#1"
        assert tip.status == "pending" and tip.depth == 1
        assert tip.cmatra_balance == 900
    asyncio.run(run())


def test_lock_held_between_build_and_submit(wired):
    async def run():
        b = await api.build_surrender(_req())
        # Lock held: a second build cannot acquire within a short timeout.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(api.build_surrender(_req()), timeout=0.05)
        assert api.state.tip_mgr._lock.locked()
        await api.submit_surrender(_submit_req(b.tx_hash))
        assert not api.state.tip_mgr._lock.locked()
    asyncio.run(run())


def test_chunk_two_chains_off_chunk_one_pending(wired):
    async def run():
        b1 = await api.build_surrender(_req())
        s1 = await api.submit_surrender(_submit_req(b1.tx_hash))
        # Chunk 2 builds off the PENDING tip (chunk1#1), not a chain re-query.
        b2 = await api.build_surrender(_req())
        assert b2.pool_utxo_used == f"{s1.tx_hash}#1"
        s2 = await api.submit_surrender(_submit_req(b2.tx_hash))
        tip = api.state.tip_mgr.tip
        assert tip.utxo_ref == f"{s2.tx_hash}#1"
        assert tip.depth == 2 and tip.cmatra_balance == 800
    asyncio.run(run())


def test_depth_cap_returns_503_pool_settling(wired):
    async def run():
        for _ in range(8):
            b = await api.build_surrender(_req())
            await api.submit_surrender(_submit_req(b.tx_hash))
        assert api.state.tip_mgr.tip.depth == 8
        with pytest.raises(HTTPException) as ei:
            await api.build_surrender(_req())
        assert ei.value.status_code == 503
        assert ei.value.detail["code"] == "POOL_SETTLING"
        assert not api.state.tip_mgr._lock.locked()  # lock freed on rejection
    asyncio.run(run())


def test_datum_guard_rejects_post_submit_but_user_still_succeeds(wired, monkeypatch):
    async def run():
        def bad_build(user_address, total_cmatra, legacy_assets, pool_utxo):
            rem = pool_utxo["cmatra_amount"] - total_cmatra
            return "8400", "badbad" + "0" * 58, {
                "address": SCRIPT_ADDR, "datum_hex": "",  # MISSING datum
                "cmatra_amount": rem, "ada_amount": 1_500_000,
            }
        monkeypatch.setattr(api, "_build_tx_blocking", bad_build)

        b = await api.build_surrender(_req())
        s = await api.submit_surrender(_submit_req(b.tx_hash))  # tx lands
        assert s.tx_hash  # user got success
        tip = api.state.tip_mgr.tip
        # Tip flushed to confirmed (depth 0), NOT chained off the bad output.
        assert tip.status == "confirmed" and tip.depth == 0
        assert tip.utxo_ref == f"{'a'*64}#0"
        assert not api.state.tip_mgr._lock.locked()
    asyncio.run(run())


def test_submit_failure_releases_lock_tip_unchanged(wired, monkeypatch):
    async def run():
        b = await api.build_surrender(_req())

        def boom(cbor):
            raise RuntimeError("blockfrost 400")
        monkeypatch.setattr(api.state.bf, "submit_tx", boom)
        with pytest.raises(HTTPException) as ei:
            await api.submit_surrender(_submit_req(b.tx_hash))
        assert ei.value.status_code == 400
        tip = api.state.tip_mgr.tip
        assert tip.status == "confirmed" and tip.depth == 0
        assert not api.state.tip_mgr._lock.locked()
    asyncio.run(run())


def test_pool_status_exposes_chain_state(wired):
    async def run():
        b = await api.build_surrender(_req())
        await api.submit_surrender(_submit_req(b.tx_hash))
        resp = api.pool_status()
        cs = resp.chainState
        assert cs is not None
        assert set(cs.keys()) == {"utxo_ref", "balance", "status", "depth"}
        assert cs["status"] == "pending" and cs["depth"] == 1
        assert resp.depthCap == 8
    asyncio.run(run())


def test_cold_start_reseeds_from_confirmed(wired):
    async def run():
        confirmed = wired
        b = await api.build_surrender(_req())
        await api.submit_surrender(_submit_req(b.tx_hash))
        assert api.state.tip_mgr.tip.status == "pending"

        # "Restart": clear in-memory tip + stashes; chain now reports a
        # DIFFERENT confirmed UTxO (what a just-confirmed chunk looks like).
        api.state.tip_mgr._tip = None
        api.state.build_ctx = {}
        api.state.pending_tx_hashes = {}
        api.state.pending_tx_cbor = {}
        confirmed[:] = [{
            "tx_hash": "f" * 64, "output_index": 1,
            "cmatra_amount": 777, "ada_amount": 2_000_000,
        }]

        b2 = await api.build_surrender(_req())
        assert b2.pool_utxo_used == f"{'f'*64}#1"
        assert api.state.tip_mgr.tip.depth == 0
        assert api.state.tip_mgr.tip.cmatra_balance == 777
    asyncio.run(run())
