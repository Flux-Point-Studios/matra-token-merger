"""Regression guard for the ProcessSurrender SPEND-redeemer index.

The surrender tx spends the pool script UTxO + the user's own UTxOs. The
Conway ledger sorts tx inputs canonically — ascending by (tx_id bytes, output
index) — and evaluates the SPEND redeemer against the script input's position
in THAT order. So the SPEND redeemer index MUST equal the script (pool) input's
position in ``sorted(inputs, key=lambda i: (i.tx_id_bytes, i.index))`` or the
node rejects the tx with ``extraRedeemers=['spend:<n>']``.

This drives the REAL ``_build_cosigned_surrender_tx`` (not the mocked
``_build_tx_blocking`` used by the chaining tests) against synthetic user
UTxOs whose tx_hashes bracket the pool hash so the script input lands at
canonical positions 0, 1, and 2. For each, it decodes the assembled tx CBOR
and asserts the SPEND redeemer index equals the script input's true
ledger-canonical position — computed by re-sorting the inputs ourselves, NOT
by trusting cbor2's decode order.

That distinction is load-bearing. The bug (and the broken PR #20 fix) aligned
the redeemer to cbor2's decode order, which on the pinned pycardano (< 0.19)
is hash-seed-dependent and diverges from the ledger's canonical sort. Asserting
against the decode order is self-consistent but tests nothing real — it passes
on broken and fixed code alike. We assert against the canonical sort instead,
and force multiple PYTHONHASHSEED values via subprocess so the guard holds
regardless of which order cbor2 happened to decode the inputs in.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import cbor2
import pytest
from cbor2 import CBORTag

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("NETWORK", "mainnet")
os.environ.setdefault("SURRENDER_API_SECRET", "test-secret")

from pycardano import (  # noqa: E402
    Address,
    Asset,
    AssetName,
    MultiAsset,
    Network,
    PaymentSigningKey,
    PlutusV3Script,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    plutus_script_hash,
)
from pycardano import ExecutionUnits  # noqa: E402
from pycardano.hash import ScriptHash as PycScriptHash  # noqa: E402
from pycardano.hash import VerificationKeyHash  # noqa: E402

import services.surrender_api as api  # noqa: E402

# A minimal always-true PlutusV3 script; its real hash defines the script addr.
_SCRIPT_HEX = "46010000222499"
_POOL_HASH = "cd" + "60" * 31  # the depth-1-tip-style pool hash from the canary
_CMATRA_POLICY = "ab" * 28
_CMATRA_ASSET = "634d41545241"  # hex("cMATRA")


class _FakeContext:
    """Offline chain context: serves the synthetic user UTxO, fixed protocol
    params, and an evaluate_tx that returns ex_units for any spend index the
    builder asks about (so build() completes without a network)."""

    network = Network.TESTNET

    def __init__(self, user_utxos):
        self._user_utxos = user_utxos

    @property
    def protocol_param(self):
        from pycardano import ProtocolParameters

        return ProtocolParameters(
            min_fee_constant=155381,
            min_fee_coefficient=44,
            max_block_size=90112,
            max_tx_size=16384,
            max_block_header_size=1100,
            key_deposit=2000000,
            pool_deposit=500000000,
            pool_influence=0.3,
            monetary_expansion=0.003,
            treasury_expansion=0.2,
            decentralization_param=0,
            extra_entropy="",
            protocol_major_version=9,
            protocol_minor_version=0,
            min_utxo=1000000,
            min_pool_cost=170000000,
            price_mem=0.0577,
            price_step=0.0000721,
            max_tx_ex_mem=14000000,
            max_tx_ex_steps=10000000000,
            max_block_ex_mem=62000000,
            max_block_ex_steps=20000000000,
            max_val_size=5000,
            collateral_percent=150,
            max_collateral_inputs=3,
            coins_per_utxo_word=34482,
            coins_per_utxo_byte=4310,
            cost_models={},
            maximum_reference_scripts_size={"bytes": 200000},
            min_fee_reference_scripts={"base": 15.0, "range": 25600, "multiplier": 1.2},
        )

    @property
    def last_block_slot(self):
        return 100_000_000

    def utxos(self, address):
        return list(self._user_utxos)

    def evaluate_tx(self, tx):
        return {f"spend:{i}": ExecutionUnits(500000, 200000000) for i in range(8)}


_LEGACY_POLICY = "cd" * 28
_LEGACY_ASSET = "4e4654"  # the surrendered NFT the quarantine output requires

# Two user-UTxO hash prefixes; together they decide the pool input's position
# in the canonically sorted inputs. Pool hash is ``cd6060...``.
#   "ff","ff" -> both AFTER pool  -> pool at index 0
#   "cc","dd" -> straddle pool    -> pool at index 1
#   "00","00" -> both BEFORE pool -> pool at index 2
_BRACKETS = {
    "ff": ("ff", "fe"),  # both > cd60
    "cc": ("cc", "dd"),  # one < cd60, one > cd60
    "00": ("00", "11"),  # both < cd60
}


def _user_utxos_for(prefix: str, user_addr: Address):
    """Two user UTxOs (ADA + the legacy NFT the quarantine output spends) whose
    tx_hashes bracket the pool hash per ``_BRACKETS[prefix]`` so the script
    input lands at serialized position 0, 1, or 2. Enough ADA to cover all
    three outputs plus fee + collateral."""
    p_ada, p_nft = _BRACKETS[prefix]
    h_ada = (p_ada + "11" * 32)[:64]
    h_nft = (p_nft + "22" * 32)[:64]
    nft_multi = MultiAsset()
    nft_multi[PycScriptHash(bytes.fromhex(_LEGACY_POLICY))] = Asset(
        {AssetName(bytes.fromhex(_LEGACY_ASSET)): 1}
    )
    return [
        UTxO(
            TransactionInput.from_primitive([bytes.fromhex(h_ada), 0]),
            TransactionOutput(user_addr, Value(100_000_000)),
        ),
        UTxO(
            TransactionInput.from_primitive([bytes.fromhex(h_nft), 1]),
            TransactionOutput(user_addr, Value(5_000_000, nft_multi)),
        ),
    ]


def _decode_positions(tx_cbor_hex: str):
    """Return (script_input_canonical_pos, spend_redeemer_index, onwire_matches_canonical)
    decoded from the assembled tx CBOR.

    ``script_input_canonical_pos`` is the pool input's index in the inputs
    re-sorted by (tx_id bytes, output index) — the Conway ledger's canonical
    order, which is what the node evaluates the redeemer against. It is computed
    by re-sorting ourselves, NEVER by trusting the order cbor2 decoded the
    inputs in (that order is hash-seed-dependent on the pinned pycardano and is
    exactly the wrong proxy the broken #20 fix asserted against).

    ``onwire_matches_canonical`` reports whether the bytes cbor2 actually
    decoded are already in canonical order — the fix re-emits inputs as a bare
    array so the on-wire order IS canonical, which keeps the redeemer index the
    node derives equal to the one in the witness set.
    """
    arr = cbor2.loads(bytes.fromhex(tx_cbor_hex))
    body, ws = arr[0], arr[1]
    raw_inputs = body[0]
    if isinstance(raw_inputs, CBORTag):
        raw_inputs = raw_inputs.value
    onwire = [(bytes(e[0]), e[1]) for e in raw_inputs]
    canonical = sorted(onwire, key=lambda p: (p[0], p[1]))
    pool = (bytes.fromhex(_POOL_HASH), 1)
    script_pos = canonical.index(pool) if pool in canonical else None
    onwire_matches_canonical = onwire == canonical
    redeemers = ws.get(5)
    spend_idx = None
    if isinstance(redeemers, dict):  # RedeemerMap: {(tag, index): [...]}
        for k in redeemers:
            if k[0] == 0:  # SPEND
                spend_idx = k[1]
    elif isinstance(redeemers, list):  # legacy [tag, index, data, exunits]
        for r in redeemers:
            if r[0] == 0:
                spend_idx = r[1]
    return script_pos, spend_idx, onwire_matches_canonical


def _build_with_user_prefix(prefix: str):
    """Wire module state minimally and build the real cosigned surrender tx
    with a single synthetic user UTxO bracketing the pool hash."""
    script = PlutusV3Script(bytes.fromhex(_SCRIPT_HEX))
    script_addr = Address(plutus_script_hash(script), network=Network.TESTNET)
    user_addr = Address(
        VerificationKeyHash(bytes.fromhex("22" * 28)), network=Network.TESTNET
    )
    user_addr_bech = user_addr.encode()

    sk = PaymentSigningKey.generate()
    admin_addr = Address(
        sk.to_verification_key().hash(), network=Network.TESTNET
    )

    api.state.script_cbor_hex = _SCRIPT_HEX
    api.state.admin_sk = sk
    api.state.admin_pkh = sk.to_verification_key().hash()
    api.state.admin_addr = admin_addr

    class _BF:
        project_id = "x"
        base_url = "https://cardano-mainnet.blockfrost.io/api/v0"

    api.state.bf = _BF()

    ctx = _FakeContext(_user_utxos_for(prefix, user_addr))

    # Patch the constants + the context constructor + the co-signer fetch off.
    import unittest.mock as mock

    with mock.patch.object(api, "SCRIPT_ADDRESS", script_addr.encode()), \
         mock.patch.object(api, "QUARANTINE_ADDRESS", script_addr.encode()), \
         mock.patch.object(api, "CMATRA_POLICY_HEX", _CMATRA_POLICY), \
         mock.patch.object(api, "CMATRA_ASSET_HEX", _CMATRA_ASSET), \
         mock.patch.object(api, "COSIGNER_URL", ""), \
         mock.patch.object(api, "COLLATERAL_UTXO", ""), \
         mock.patch.object(api, "BlockFrostChainContext", lambda **kw: ctx):
        pool_utxo = {
            "tx_hash": _POOL_HASH,
            "output_index": 1,
            "cmatra_amount": 1000,
            "ada_amount": 2_000_000,
        }
        # Surrender 100 cMATRA so a pool-change output (900 remaining) exists;
        # one legacy asset to a quarantine output.
        legacy = [
            {"policy_hex": _LEGACY_POLICY, "asset_hex": _LEGACY_ASSET, "quantity": 1}
        ]
        tx_cbor_hex, _tx_hash, _pool_out = api._build_cosigned_surrender_tx(
            user_addr_bech, 100, legacy, pool_utxo
        )
    return tx_cbor_hex


@pytest.mark.parametrize("prefix", ["ff", "00"])
def test_spend_redeemer_index_matches_canonical_script_input_position(prefix):
    """The SPEND redeemer must index the script input's true ledger-canonical
    position — its index in inputs sorted by (tx_id, output index) — or the node
    rejects extraRedeemers. We also assert the on-wire input bytes are in that
    canonical order (the fix re-emits inputs as a bare array), so the redeemer
    index the node derives equals the one in the witness set."""
    tx_cbor_hex = _build_with_user_prefix(prefix)
    script_pos, spend_idx, onwire_canonical = _decode_positions(tx_cbor_hex)
    assert script_pos is not None, "pool script input missing from serialized tx"
    assert spend_idx is not None, "SPEND redeemer missing from witness set"
    assert onwire_canonical, (
        "tx body inputs are NOT in ledger-canonical order on the wire — cbor2's "
        "tag-258 encoder re-ordered them, so the node would derive a different "
        "redeemer index than the one in the witness set"
    )
    assert spend_idx == script_pos, (
        f"SPEND redeemer index {spend_idx} != script input's canonical "
        f"position {script_pos} — node would reject extraRedeemers=['spend:{spend_idx}']"
    )


@pytest.mark.parametrize("seed", ["0", "1", "2", "3", "7", "42", "99", "999"])
def test_redeemer_index_correct_under_any_hash_seed(seed):
    """The pinned pycardano (< 0.19) lets cbor2 decode/encode the inputs set in
    hash-seed-dependent order. Re-run the alignment check in a subprocess with a
    fixed PYTHONHASHSEED so the guard holds regardless of which order the
    interpreter produces, and record which canonical position the script input
    landed at.

    The seed set includes the values the investigation used to prove the fix
    deterministic (0/1/7/42/999) plus a few neighbours. Across all seeds the
    suite must observe the pool script input at canonical positions 0, 1, AND 2.
    'ff' puts both user UTxOs after the pool (pool at 0), '00' puts both before
    (pool at 2), and 'cc' splits them around the pool hash (pool at 1). Each run
    also asserts the on-wire inputs are canonically ordered, which is what keeps
    the node's derived redeemer index equal to the witness-set one under every
    seed.
    """
    script = (
        "import os,sys;"
        f"sys.path.insert(0, {str(_PROJECT_ROOT)!r});"
        "import tests.test_surrender_redeemer_index as t;"
        "import logging; logging.getLogger('PyCardano').setLevel(logging.ERROR);"
        "seen=set()\n"
        "for pfx in ('ff','cc','00'):\n"
        "    h=t._build_with_user_prefix(pfx)\n"
        "    sp,ri,canon=t._decode_positions(h)\n"
        "    assert sp is not None and ri is not None, (pfx,sp,ri)\n"
        "    assert canon, f'on-wire inputs not canonical pfx={pfx}'\n"
        "    assert ri==sp, f'seed-dependent mismatch pfx={pfx} canonical_pos={sp} redeemer_idx={ri}'\n"
        "    seen.add(sp)\n"
        "print('POSITIONS', sorted(seen))"
    )
    env = dict(os.environ, PYTHONHASHSEED=seed)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"hash seed {seed} failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    )
    assert "POSITIONS" in proc.stdout


def test_all_three_canonical_positions_are_exercised():
    """Prove the regression guard actually covers the script input at canonical
    positions 0, 1, AND 2 (the bracketing the live canary exposed). Sweep hash
    seeds until every position has been observed at least once with the redeemer
    correctly aligned AND the on-wire inputs canonically ordered — this is the
    multi-position proof the fix must hold for, not just whichever ordering the
    default seed happens to produce."""
    seen_positions: set[int] = set()
    for seed in range(24):
        script = (
            "import os,sys;"
            f"sys.path.insert(0, {str(_PROJECT_ROOT)!r});"
            "import tests.test_surrender_redeemer_index as t;"
            "import logging; logging.getLogger('PyCardano').setLevel(logging.ERROR);"
            "out=[]\n"
            "for pfx in ('ff','cc','00'):\n"
            "    h=t._build_with_user_prefix(pfx)\n"
            "    sp,ri,canon=t._decode_positions(h)\n"
            "    assert sp is not None and ri is not None\n"
            "    assert canon, f'on-wire inputs not canonical pfx={pfx}'\n"
            "    assert ri==sp, f'mismatch pfx={pfx} canonical_pos={sp} redeemer_idx={ri}'\n"
            "    out.append(sp)\n"
            "print('POS', out)"
        )
        env = dict(os.environ, PYTHONHASHSEED=str(seed))
        proc = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True
        )
        assert proc.returncode == 0, (
            f"seed {seed} failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
        for line in proc.stdout.splitlines():
            if line.startswith("POS "):
                seen_positions.update(eval(line[4:]))
        if {0, 1, 2}.issubset(seen_positions):
            break
    assert {0, 1, 2}.issubset(seen_positions), (
        f"did not exercise all of positions 0,1,2 — only saw {sorted(seen_positions)}"
    )
