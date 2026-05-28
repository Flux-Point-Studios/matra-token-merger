"""Regression guard for the ProcessSurrender SPEND-redeemer index.

The surrender tx spends the pool script UTxO + the user's own UTxOs. Cardano
sorts tx inputs canonically, so the pool (script) input's position depends on
its tx_hash relative to the user UTxO hashes. The SPEND redeemer MUST be
indexed to the script input's position in the SERIALIZED tx body — the order
the ledger evaluates against — or the node rejects the tx with
``extraRedeemers=['spend:<n>']``.

This drives the REAL ``_build_cosigned_surrender_tx`` (not the mocked
``_build_tx_blocking`` used by the chaining tests) against synthetic user
UTxOs whose tx_hashes bracket the pool hash so the script input lands at
serialized positions 0, 1, and 2. For each, it decodes the assembled tx CBOR
and asserts the SPEND redeemer index equals the script input's actual
serialized position.

The bug it guards against is hash-seed-dependent on pycardano < 0.19 (the CI-
pinned version), so the parametrization also forces multiple PYTHONHASHSEED
values via subprocess to exercise both orderings of the inputs set.
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
    """Return (script_input_serialized_pos, spend_redeemer_index) decoded from
    the assembled tx CBOR — exactly what the node sorts and evaluates."""
    arr = cbor2.loads(bytes.fromhex(tx_cbor_hex))
    body, ws = arr[0], arr[1]
    raw_inputs = body[0]
    if isinstance(raw_inputs, CBORTag):
        raw_inputs = raw_inputs.value
    script_pos = None
    for i, e in enumerate(raw_inputs):
        if bytes(e[0]).hex() == _POOL_HASH and e[1] == 1:
            script_pos = i
            break
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
    return script_pos, spend_idx


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
def test_spend_redeemer_index_matches_serialized_script_input_position(prefix):
    """The SPEND redeemer must index the script input's actual position in the
    SERIALIZED tx body — whatever that position turns out to be — or the node
    rejects extraRedeemers. (Which serialized position the script input lands at
    is hash-seed-dependent on pycardano < 0.19; the multi-seed test below pins
    positions 0/1/2 explicitly. This one asserts the alignment invariant under
    the ambient interpreter seed.)"""
    tx_cbor_hex = _build_with_user_prefix(prefix)
    script_pos, spend_idx = _decode_positions(tx_cbor_hex)
    assert script_pos is not None, "pool script input missing from serialized tx"
    assert spend_idx is not None, "SPEND redeemer missing from witness set"
    assert spend_idx == script_pos, (
        f"SPEND redeemer index {spend_idx} != script input's serialized "
        f"position {script_pos} — node would reject extraRedeemers=['spend:{spend_idx}']"
    )


@pytest.mark.parametrize("seed", ["0", "1", "2", "3", "4", "5", "6", "7"])
def test_redeemer_index_correct_under_any_hash_seed(seed):
    """pycardano < 0.19 serializes the inputs set in hash-seed-dependent order.
    Re-run the alignment check in a subprocess with a fixed PYTHONHASHSEED so
    the guard holds regardless of which input ordering the interpreter
    produces, and record which serialized position the script input landed at.

    Across all seeds the suite must observe the pool script input at positions
    0, 1, AND 2 (the bracketing the canary exposed). 'ff' puts both user UTxOs
    after the pool (pool at 0), '00' puts both before (pool at 2), and 'cc'
    splits them around the pool hash (pool at 1) — so each run prints the
    observed positions and the aggregate assertion below proves full coverage.
    """
    script = (
        "import os,sys;"
        f"sys.path.insert(0, {str(_PROJECT_ROOT)!r});"
        "import tests.test_surrender_redeemer_index as t;"
        "import logging; logging.getLogger('PyCardano').setLevel(logging.ERROR);"
        "seen=set()\n"
        "for pfx in ('ff','cc','00'):\n"
        "    h=t._build_with_user_prefix(pfx)\n"
        "    sp,ri=t._decode_positions(h)\n"
        "    assert sp is not None and ri is not None, (pfx,sp,ri)\n"
        "    assert ri==sp, f'seed-dependent mismatch pfx={pfx} script_pos={sp} redeemer_idx={ri}'\n"
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


def test_all_three_serialized_positions_are_exercised():
    """Prove the regression guard actually covers the script input at
    serialized positions 0, 1, AND 2 (the bracketing the live canary exposed).
    Sweep hash seeds until every position has been observed at least once with
    the redeemer correctly aligned — this is the multi-position proof the fix
    must hold for, not just whichever ordering the default seed happens to
    produce."""
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
            "    sp,ri=t._decode_positions(h)\n"
            "    assert sp is not None and ri is not None\n"
            "    assert ri==sp, f'mismatch pfx={pfx} sp={sp} ri={ri}'\n"
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
