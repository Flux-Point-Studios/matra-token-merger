"""Move the surrender pool to the claim validator re-applied with fresh admin keys.

Before its deadline the pool validator's ProcessSurrender arm checks only that
both admins signed and that the transaction closes before the deadline, so the
two admins can send the pool anywhere. A migration sends it to the same
validator applied with a new admin pair (onchain/claim_validator/
apply_params.sh): one transaction spending the pool and one pure-ADA fee input,
paying the pool's cMATRA to the new pool address under the Void inline datum,
or, for a test slice, part of it, with the rest returned to the old pool under
the same datum.

The destination is derived, not taken on trust: the old script with its three
parameter constants swapped for the new admins and deadline, which must hash to
the plan's new_script_hash. The transaction's required signers are the old
admin pair AND that new pair. The old validator needs only the old pair, but
the ledger then refuses the move unless both new keys signed it too, so the
cMATRA never lands in a pool whose keys were not shown to work.

  snapshot   read the two inputs (from their producing transactions, checked
             against their ids) and the protocol parameters into one file
  build      the unsigned transaction, from a snapshot and a plan only, so the
             same files always give the same bytes
  check      what every admin runs before witnessing; prints the facts to
             compare across hosts and exits 1 on any refusal
  assemble   adds cardano-cli key witnesses without re-encoding a byte the
             body hash or script_data_hash covers, then checks the result

Nothing here submits a transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

import cbor2
from cbor2 import CBORTag
from pycardano import Address, Network, ScriptHash

from services.cosign_policy import (
    PROCESS_SURRENDER,
    VOID_INLINE_DATUM,
    CosignRejected,
    Output,
    _elements,
    _head,
    _item,
    _witness_fields,
    decode,
    parse_output,
    signature_verifies,
    split_tx,
)
from tools.api_clients import BlockfrostClient
from tools.cardano_utils import SLOT_OFFSET_S, plutus_v3_script_hash, posix_ms_to_slot
from tools.config import NETWORK

MAX_FEE_LOVELACE = 1_000_000
MAX_TOTAL_COLLATERAL_LOVELACE = 5_000_000
MAX_VALIDITY_SLOTS = 6 * 3600
# Above the fee the formula gives for the exact signed size, so a witness set
# re-framed by another assembler (up to ~200 bytes) still pays.
FEE_MARGIN_LOVELACE = 10_000
# Every output's minimum: coinsPerUTxOByte times this overhead plus its size.
MIN_UTXO_OVERHEAD_BYTES = 160

_VOID_DATUM = bytes.fromhex("d87980")
_TX_ENVELOPE = "Tx ConwayEra"
# inputs, outputs, fee, ttl, validity start, script_data_hash, collateral,
# required signers, collateral return, total collateral
_BODY_KEYS = frozenset({0, 1, 2, 3, 8, 11, 13, 14, 16, 17})


class MigrationRejected(ValueError):
    """The transaction, plan or snapshot is not the migration it must be."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def blake2b_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def blake2b_224(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=28).digest()


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


Ref = tuple[bytes, int]


@dataclass(frozen=True)
class Params:
    min_fee_a: int
    min_fee_b: int
    coins_per_utxo_byte: int
    price_mem: Fraction
    price_step: Fraction
    max_tx_size: int
    max_ex_mem: int
    max_ex_steps: int
    collateral_percent: int
    cost_model_v3: tuple[int, ...]


@dataclass(frozen=True)
class Utxo:
    ref: Ref
    output: Output


@dataclass(frozen=True)
class Snapshot:
    network: str
    tip_slot: int
    pool: Utxo
    fee: Utxo
    params: Params


def _ref(text: str) -> Ref:
    txid, sep, index = text.partition("#")
    if not sep or len(txid) != 64 or not index.isdigit():
        raise MigrationRejected("snapshot", f"not a TXID#INDEX reference: {text!r}")
    return bytes.fromhex(txid), int(index)


def _ref_text(ref: Ref) -> str:
    return f"{ref[0].hex()}#{ref[1]}"


def _is_key_address(address: bytes) -> bool:
    return address[0] >> 4 in (0, 2, 4, 6)


def _is_script_address(address: bytes) -> bool:
    return address[0] >> 4 in (1, 3, 5, 7)


def _network_id(network: str) -> int:
    return 1 if network == "mainnet" else 0


def _resolve(entry: Mapping[str, Any]) -> Utxo:
    """An input from the transaction that produced it, which must hash to the
    referenced id, so nothing about it is taken from a provider's JSON."""
    ref = _ref(entry["ref"])
    if entry.get("consumed_by"):
        raise MigrationRejected("spent", f"{entry['ref']} was spent by {entry['consumed_by']}")
    try:
        body = split_tx(bytes.fromhex(entry["producing_tx"]))[0]
        outputs = decode(body).get(1)
    except CosignRejected as exc:
        raise MigrationRejected("snapshot", f"producing transaction of {entry['ref']}: {exc}") from exc
    if blake2b_256(body) != ref[0]:
        raise MigrationRejected("snapshot", f"the producing transaction does not hash to {entry['ref']}")
    if not isinstance(outputs, list) or ref[1] >= len(outputs):
        raise MigrationRejected("snapshot", f"{entry['ref']} has no such output")
    return Utxo(ref, parse_output(outputs[ref[1]]))


def _params(doc: Mapping[str, Any]) -> Params:
    return Params(
        min_fee_a=int(doc["min_fee_a"]),
        min_fee_b=int(doc["min_fee_b"]),
        coins_per_utxo_byte=int(doc["coins_per_utxo_size"]),
        price_mem=Fraction(str(doc["price_mem"])),
        price_step=Fraction(str(doc["price_step"])),
        max_tx_size=int(doc["max_tx_size"]),
        max_ex_mem=int(doc["max_tx_ex_mem"]),
        max_ex_steps=int(doc["max_tx_ex_steps"]),
        collateral_percent=int(doc["collateral_percent"]),
        cost_model_v3=tuple(int(v) for v in doc["cost_models_raw"]["PlutusV3"]),
    )


def load_snapshot(doc: Mapping[str, Any]) -> Snapshot:
    network = doc["network"]
    if network not in SLOT_OFFSET_S:
        raise MigrationRejected("snapshot", f"unknown network {network!r}")
    pool = _resolve(doc["inputs"]["pool"])
    fee = _resolve(doc["inputs"]["fee"])
    net = _network_id(network)
    if (not _is_script_address(pool.output.address) or pool.output.datum != VOID_INLINE_DATUM
            or pool.output.address[0] & 0x0F != net or pool.output.has_script_ref):
        raise MigrationRejected("pool_input", "the pool input is not a script output under the Void inline datum")
    if (not _is_key_address(fee.output.address) or fee.output.assets or fee.output.datum is not None
            or fee.output.has_script_ref or fee.output.address[0] & 0x0F != net):
        raise MigrationRejected("fee_input", "the fee input must be pure ADA at a key address")
    return Snapshot(network, int(doc["tip"]["slot"]), pool, fee, _params(doc["protocol_parameters"]))


def snapshot_document(client: Any, pool_ref: str, fee_ref: str, network: str) -> dict[str, Any]:
    """What `build` needs, read once from the chain client."""
    inputs = {}
    for name, ref in (("pool", pool_ref), ("fee", fee_ref)):
        txid, index = _ref(ref)
        rows = [row for row in client.get_tx_utxos(txid.hex())["outputs"]
                if row["output_index"] == index and not row.get("collateral")]
        if len(rows) != 1:
            raise MigrationRejected("snapshot", f"the chain lists no output {ref}")
        inputs[name] = {"ref": ref, "producing_tx": client.get_tx_cbor(txid.hex()).hex(),
                        "consumed_by": rows[0].get("consumed_by_tx")}
    tip = client.get_latest_block()
    return {
        "network": network,
        "source": client.base_url,
        "tip": {key: tip[key] for key in ("slot", "height", "hash", "time")},
        "inputs": inputs,
        "protocol_parameters": client.get_protocol_parameters(),
    }


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    network: str
    pool_ref: Ref
    fee_ref: Ref
    cmatra_policy: bytes
    cmatra_name: bytes
    old_script: bytes
    old_admins: tuple[bytes, bytes]
    old_deadline_ms: int
    new_admins: tuple[bytes, bytes]
    new_deadline_ms: int
    new_script_hash: bytes
    move: int | None
    """cMATRA base units for the new pool; None moves all of it."""
    validity_start: int
    ttl: int
    ex_units: tuple[int, int]

    @property
    def old_deadline_slot(self) -> int:
        return posix_ms_to_slot(self.old_deadline_ms, self.network)

    @property
    def signers(self) -> frozenset[bytes]:
        return frozenset(self.old_admins + self.new_admins)


def _key_hashes(values: Any, code: str) -> tuple[bytes, bytes]:
    keys = tuple(bytes.fromhex(v) for v in values)
    if len(keys) != 2 or any(len(k) != 28 for k in keys) or keys[0] == keys[1]:
        raise MigrationRejected(code, "need two distinct 28-byte key hashes")
    return keys


def load_plan(doc: Mapping[str, Any]) -> Plan:
    network = doc["network"]
    if network not in SLOT_OFFSET_S:
        raise MigrationRejected("plan", f"unknown network {network!r}")
    unit = bytes.fromhex(doc["cmatra_unit"])
    old_admins = _key_hashes(doc["old_admins"], "old_admins")
    new_admins = _key_hashes(doc["new_admins"], "new_admins")
    if set(old_admins) & set(new_admins):
        raise MigrationRejected("key_reuse", "a new admin key is an old admin key")
    old_script = bytes.fromhex(doc["old_script_hex"])
    new_script_hash = bytes.fromhex(doc["new_script_hash"])
    if len(new_script_hash) != 28 or new_script_hash == plutus_v3_script_hash(old_script):
        raise MigrationRejected("new_script", "the new script hash must be another 28-byte script hash")
    old_deadline_ms, new_deadline_ms = int(doc["old_deadline_ms"]), int(doc["new_deadline_ms"])
    for code, deadline in (("old_deadline", old_deadline_ms), ("new_deadline", new_deadline_ms)):
        # apply_params.sh's rule, which also keeps every deadline constant 9 bytes long
        if not 10**12 <= deadline < 10**13:
            raise MigrationRejected(code, f"{deadline} is not 13-digit POSIX milliseconds")
    if new_deadline_ms < old_deadline_ms:
        raise MigrationRejected("new_deadline", "the new pool would close before the old one")
    move = doc["move"]
    if move != "all" and not (isinstance(move, int) and not isinstance(move, bool)):
        raise MigrationRejected("move", f"move is 'all' or base units, not {move!r}")
    plan = Plan(
        network=network,
        pool_ref=_ref(doc["pool_ref"]),
        fee_ref=_ref(doc["fee_ref"]),
        cmatra_policy=unit[:28],
        cmatra_name=unit[28:],
        old_script=old_script,
        old_admins=old_admins,
        old_deadline_ms=old_deadline_ms,
        new_admins=new_admins,
        new_deadline_ms=new_deadline_ms,
        new_script_hash=new_script_hash,
        move=None if move == "all" else move,
        validity_start=int(doc["validity_start"]),
        ttl=int(doc["ttl"]),
        ex_units=(int(doc["ex_units"][0]), int(doc["ex_units"][1])),
    )
    _check_validity(plan, plan.validity_start, plan.ttl)
    return plan


def _check_validity(plan: Plan, start: Any, ttl: Any) -> None:
    if not (isinstance(start, int) and isinstance(ttl, int)):
        raise MigrationRejected("validity_window", "validity start and ttl must both be set")
    # ProcessSurrender needs the interval entirely before the deadline; the
    # ttl is its exclusive upper bound, so a ttl one slot short is enough.
    if ttl >= plan.old_deadline_slot:
        raise MigrationRejected("after_deadline", f"ttl {ttl} is not before the deadline slot {plan.old_deadline_slot}")
    if not 0 <= start < ttl or ttl - start > MAX_VALIDITY_SLOTS:
        raise MigrationRejected("validity_window", f"[{start}, {ttl}) is empty or longer than {MAX_VALIDITY_SLOTS} slots")


@dataclass(frozen=True)
class _Bound:
    """A plan read against its snapshot."""

    pool_cmatra: int
    move: int
    old_pool: bytes
    new_pool: bytes
    fee_address: bytes
    fee_payer: bytes
    witness_keys: frozenset[bytes]


def _parameter_constants(admins: tuple[bytes, bytes], deadline_ms: int) -> tuple[bytes, bytes, bytes]:
    """The claim validator's parameters as apply_params.sh passes them: Plutus
    Data CBOR, a 28-byte bytestring per admin and an 8-byte unsigned deadline."""
    return b"\x58\x1c" + admins[0], b"\x58\x1c" + admins[1], b"\x1b" + deadline_ms.to_bytes(8, "big")


def reapplied_script(script: bytes, old_admins: tuple[bytes, bytes], old_deadline_ms: int,
                     new_admins: tuple[bytes, bytes], new_deadline_ms: int) -> bytes:
    """``script``, the claim validator applied to the old parameters, applied
    to the new ones instead. aiken applies each parameter as a Data constant
    whose CBOR the flat encoding carries byte-aligned and verbatim, so swapping
    each constant in place for its new value of the same length gives the bytes
    apply_params.sh gives. Each old constant must occur exactly once."""
    out = bytearray(script)
    codes = ("old_admins", "old_admins", "old_deadline")
    for code, old, new in zip(codes, _parameter_constants(old_admins, old_deadline_ms),
                              _parameter_constants(new_admins, new_deadline_ms), strict=True):
        count = script.count(old)
        if count != 1:
            raise MigrationRejected(code, f"{old.hex()} occurs {count} times in the old script, not once")
        at = script.index(old)
        out[at:at + len(old)] = new
    return bytes(out)


def _bind(snap: Snapshot, plan: Plan) -> _Bound:
    if plan.network != snap.network or plan.pool_ref != snap.pool.ref or plan.fee_ref != snap.fee.ref:
        raise MigrationRejected("snapshot", "the plan names other inputs or another network than the snapshot")
    pool = snap.pool.output
    if pool.address[1:] != plutus_v3_script_hash(plan.old_script):
        raise MigrationRejected("old_script", "the old script does not hash to the pool address")
    derived = plutus_v3_script_hash(reapplied_script(plan.old_script, plan.old_admins, plan.old_deadline_ms,
                                                     plan.new_admins, plan.new_deadline_ms))
    if derived != plan.new_script_hash:
        raise MigrationRejected("new_script", f"the old script applied to the new admins and deadline hashes to "
                                f"{derived.hex()}, not {plan.new_script_hash.hex()}")
    cmatra = pool.assets.get(plan.cmatra_policy, {}).get(plan.cmatra_name, 0)
    if cmatra <= 0 or pool.assets != {plan.cmatra_policy: {plan.cmatra_name: cmatra}}:
        raise MigrationRejected("pool_input", "the pool input must hold cMATRA and nothing else")
    move = cmatra if plan.move is None else plan.move
    if not 0 < move <= cmatra:
        raise MigrationRejected("move", f"move {move} of a pool holding {cmatra}")
    if plan.ex_units[0] > snap.params.max_ex_mem or plan.ex_units[1] > snap.params.max_ex_steps:
        raise MigrationRejected("ex_units", "the redeemer budget exceeds the transaction limit")
    fee_payer = snap.fee.output.address[1:29]
    return _Bound(
        pool_cmatra=cmatra,
        move=move,
        old_pool=pool.address,
        new_pool=bytes([0x70 | _network_id(snap.network)]) + plan.new_script_hash,
        fee_address=snap.fee.output.address,
        fee_payer=fee_payer,
        witness_keys=plan.signers | {fee_payer},
    )


# ---------------------------------------------------------------------------
# Fees and sizes
# ---------------------------------------------------------------------------


def language_views(cost_model_v3: Any) -> bytes:
    """The PlutusV3 language view that closes script_data_hash's preimage."""
    return cbor2.dumps({2: [int(v) for v in cost_model_v3]})


def min_fee(size: int, ex_units: tuple[int, int], params: Params) -> int:
    script_fee = math.ceil(params.price_mem * ex_units[0] + params.price_step * ex_units[1])
    return params.min_fee_a * size + params.min_fee_b + script_fee


def min_ada(output_cbor: bytes, params: Params) -> int:
    return params.coins_per_utxo_byte * (MIN_UTXO_OVERHEAD_BYTES + len(output_cbor))


def _placeholder_vkeys(count: int) -> CBORTag:
    return CBORTag(258, [[bytes([i]) * 32, bytes([i]) * 64] for i in range(count)])


def encode_tx(body: Mapping, witnesses: Mapping, valid: bool = True, auxiliary: Any = None) -> bytes:
    return cbor2.dumps([body, witnesses, valid, auxiliary])


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _pool_output(address: bytes, coin: int, policy: bytes, name: bytes, quantity: int) -> dict:
    return {0: address, 1: [coin, {policy: {name: quantity}}], 2: [1, CBORTag(24, _VOID_DATUM)]}


def migration_parts(snap: Snapshot, plan: Plan) -> tuple[dict, dict]:
    """The unsigned transaction's body and witness set."""
    bound = _bind(snap, plan)
    params, pool = snap.params, snap.pool.output
    policy, name = plan.cmatra_policy, plan.cmatra_name
    inputs = sorted([list(snap.pool.ref), list(snap.fee.ref)])
    redeemers = {(0, inputs.index(list(snap.pool.ref))): [CBORTag(121, []), list(plan.ex_units)]}
    witnesses = {5: redeemers, 7: CBORTag(258, [plan.old_script])}
    script_data_hash = blake2b_256(cbor2.dumps(redeemers) + language_views(params.cost_model_v3))

    full = bound.move == bound.pool_cmatra
    new_pool = _pool_output(bound.new_pool, pool.coin, policy, name, bound.move)
    outputs = [new_pool]
    if not full:
        # The minimum depends on the output's size, which grows with the coin.
        for _ in range(4):
            new_pool[1][0] = min_ada(cbor2.dumps(new_pool), params)
        outputs.append(_pool_output(bound.old_pool, pool.coin, policy, name, bound.pool_cmatra - bound.move))
    change = {0: bound.fee_address, 1: 0}
    outputs.append(change)
    paid_to_new_pool = 0 if full else new_pool[1][0]

    fee_coin = snap.fee.output.coin
    body: dict[int, Any] = {}
    signed_witnesses = {0: _placeholder_vkeys(len(bound.witness_keys)), **witnesses}
    fee = 0
    for _ in range(8):
        total_collateral = math.ceil(fee * params.collateral_percent / 100)
        change[1] = fee_coin - fee - paid_to_new_pool
        body = {
            0: CBORTag(258, inputs),
            1: outputs,
            2: fee,
            3: plan.ttl,
            8: plan.validity_start,
            11: script_data_hash,
            13: CBORTag(258, [list(snap.fee.ref)]),
            14: CBORTag(258, sorted(plan.signers)),
            16: {0: bound.fee_address, 1: fee_coin - total_collateral},
            17: total_collateral,
        }
        needed = min_fee(len(encode_tx(body, signed_witnesses)), plan.ex_units, params) + FEE_MARGIN_LOVELACE
        if needed == fee:
            break
        fee = needed
    else:
        raise MigrationRejected("fee", "the fee did not settle")
    if (change[1] < min_ada(cbor2.dumps(change), params)
            or body[16][1] < min_ada(cbor2.dumps(body[16]), params)):
        raise MigrationRejected("funds", f"the fee input's {fee_coin} lovelace cannot pay the fee, outputs "
                                "and collateral return")
    return body, witnesses


def build_migration_tx(snap: Snapshot, plan: Plan) -> bytes:
    tx = encode_tx(*migration_parts(snap, plan))
    check_migration_tx(tx, snap, plan)
    return tx


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------


def _spans(buf: bytes, major_expected: int) -> list[tuple[Any, bytes]]:
    """The encoded items of a definite CBOR array (major 4) or the encoded
    values of a definite map (major 5) with their decoded keys."""
    major, count, pos = _head(buf, 0)
    if major != major_expected or count is None:
        raise MigrationRejected("cbor", "expected a definite array or map")
    out = []
    for _ in range(count):
        key = None
        if major == 5:
            key, pos = _item(buf, pos, 1)
        start = pos
        _, pos = _item(buf, pos, 1)
        out.append((key, buf[start:pos]))
    return out


def _quantity(output: Output, plan: Plan) -> int:
    return output.assets.get(plan.cmatra_policy, {}).get(plan.cmatra_name, 0)


def _bech32(address: bytes, network: str) -> str:
    net = Network.MAINNET if network == "mainnet" else Network.TESTNET
    return str(Address(payment_part=ScriptHash(address[1:29]), network=net))


def _utc(slot: int, network: str) -> str:
    return datetime.fromtimestamp(slot + SLOT_OFFSET_S[network], tz=timezone.utc).isoformat()


def check_migration_tx(tx: bytes, snap: Snapshot, plan: Plan) -> dict[str, Any]:
    """Refuse ``tx`` unless it is exactly ``plan``'s migration of ``snap``'s
    pool; return the facts each admin compares before witnessing. ``tx`` may
    be unsigned or carry vkey witnesses, which must then all verify."""
    bound = _bind(snap, plan)
    params = snap.params
    try:
        body_raw, witness_raw, tail = split_tx(tx)
        body = decode(body_raw)
        fields = _witness_fields(witness_raw)
    except CosignRejected as exc:
        raise MigrationRejected("cbor", str(exc)) from exc
    if tail != b"\xf5\xf6":
        raise MigrationRejected("tx_tail", "not marked valid, or carries auxiliary data")
    if not isinstance(body, dict) or set(body) - _BODY_KEYS:
        raise MigrationRejected("forbidden_body_field", f"keys {sorted(set(body) - _BODY_KEYS, key=str)}")
    if set(body) != _BODY_KEYS:
        raise MigrationRejected("body_shape", f"missing {sorted(_BODY_KEYS - set(body))}")

    inputs = [tuple(i) for i in _elements(body[0])]
    expected_inputs = sorted([snap.pool.ref, snap.fee.ref])
    if inputs != expected_inputs:
        raise MigrationRejected("inputs", "the inputs must be exactly the pool and the fee input, in ledger order")
    if [tuple(i) for i in _elements(body[13])] != [snap.fee.ref]:
        raise MigrationRejected("collateral", "the collateral must be the fee input alone")
    signers = _elements(body[14])
    if len(signers) != len(plan.signers) or set(signers) != plan.signers:
        raise MigrationRejected("required_signers", "required signers must be both old and both new admins")
    _check_validity(plan, body[8], body[3])
    if (body[8], body[3]) != (plan.validity_start, plan.ttl):
        raise MigrationRejected("validity_window", "the validity interval is not the plan's")

    outputs = [parse_output(raw) for raw in body[1]]
    body_fields = dict(_spans(body_raw, 5))
    output_bytes = [raw for _, raw in _spans(body_fields[1], 4)]
    allowed = {bound.new_pool, bound.old_pool, bound.fee_address}
    for out in outputs:
        if out.address not in allowed:
            raise MigrationRejected("output_address", f"output to {out.address.hex()}")
        if out.has_script_ref:
            raise MigrationRejected("output_script_ref", "an output carries a reference script")
    full = bound.move == bound.pool_cmatra
    new_pool = [o for o in outputs if o.address == bound.new_pool]
    old_pool = [o for o in outputs if o.address == bound.old_pool]
    change = [o for o in outputs if o.address == bound.fee_address]
    if len(new_pool) != 1 or len(old_pool) != (0 if full else 1):
        raise MigrationRejected("new_pool_output", f"{len(new_pool)} new and {len(old_pool)} old pool outputs")
    for out in new_pool + old_pool:
        if out.datum != VOID_INLINE_DATUM:
            raise MigrationRejected("pool_datum", f"output to {out.address.hex()} lacks the Void inline datum")
        if out.assets != {plan.cmatra_policy: {plan.cmatra_name: _quantity(out, plan)}}:
            raise MigrationRejected("pool_assets", "a pool output must hold cMATRA and nothing else")
    if len(change) != 1 or change[0].assets or change[0].datum is not None:
        raise MigrationRejected("change_output", "one pure-ADA change output back to the fee payer")
    if _quantity(new_pool[0], plan) != bound.move or sum(_quantity(o, plan) for o in old_pool) != (
            bound.pool_cmatra - bound.move):
        raise MigrationRejected("moved_cmatra", f"the new pool must get {bound.move} and the old keep the rest")
    pool_coin = snap.pool.output.coin
    if (old_pool[0].coin if old_pool else new_pool[0].coin) != pool_coin:
        raise MigrationRejected("pool_lovelace", "the pool's lovelace must stay with the pool")

    fee = body[2]
    if not (isinstance(fee, int) and 0 < fee < MAX_FEE_LOVELACE):
        raise MigrationRejected("fee", f"fee {fee!r} is not below {MAX_FEE_LOVELACE}")
    coin_in = pool_coin + snap.fee.output.coin
    if coin_in != sum(o.coin for o in outputs) + fee:
        raise MigrationRejected("unbalanced", "the inputs do not equal the outputs plus the fee")
    for out, raw in zip(outputs, output_bytes, strict=True):
        if out.coin < min_ada(raw, params):
            raise MigrationRejected("min_ada", f"output to {out.address.hex()} holds less than its minimum ADA")

    if set(fields) - {0} != {5, 7}:
        raise MigrationRejected("witness_fields", f"witness set fields {sorted(fields)}")
    if _elements(decode(fields[7]), "witness_shape") != [plan.old_script]:
        raise MigrationRejected("script", "the witness set must carry the old pool script alone")
    redeemers = decode(fields[5])
    pool_index = inputs.index(snap.pool.ref)
    if redeemers != {(0, pool_index): [PROCESS_SURRENDER, list(plan.ex_units)]}:
        raise MigrationRejected("redeemer", "one ProcessSurrender redeemer on the pool input, with the plan's budget")
    script_data_hash = blake2b_256(fields[5] + language_views(params.cost_model_v3))
    if body[11] != script_data_hash:
        raise MigrationRejected("script_data_hash", "script_data_hash does not cover these redeemers")

    txid = blake2b_256(body_raw)
    if 0 in fields:
        _check_witnesses(decode(fields[0]), txid, bound.witness_keys)
        signed_size = len(tx)
    else:
        signed_size = len(tx) - len(witness_raw) + len(
            _signed_witness_set(fields, _placeholder_vkeys(len(bound.witness_keys))))
    if signed_size > params.max_tx_size:
        raise MigrationRejected("size", f"{signed_size} bytes signed")
    needed = min_fee(signed_size, plan.ex_units, params)
    if fee < needed:
        raise MigrationRejected("fee_below_min", f"fee {fee} below {needed} for {signed_size} bytes")
    total_collateral, collateral_return = body[17], parse_output(body[16])
    if (not isinstance(total_collateral, int)
            or not math.ceil(fee * params.collateral_percent / 100) <= total_collateral <= MAX_TOTAL_COLLATERAL_LOVELACE
            or collateral_return.address != bound.fee_address or collateral_return.assets
            or collateral_return.coin != snap.fee.output.coin - total_collateral
            or collateral_return.coin < min_ada(body_fields[16], params)):
        raise MigrationRejected("collateral", "collateral must cover the fee's share and return the rest to the payer")

    return {
        "txid": txid.hex(),
        "signed": 0 in fields,
        "network": snap.network,
        "pool_input": _ref_text(snap.pool.ref),
        "fee_input": _ref_text(snap.fee.ref),
        "old_pool_address": _bech32(bound.old_pool, snap.network),
        "new_pool_address": _bech32(bound.new_pool, snap.network),
        "new_script_hash": plan.new_script_hash.hex(),
        "old_admins": [k.hex() for k in plan.old_admins],
        "new_admins": [k.hex() for k in plan.new_admins],
        "new_deadline_ms": plan.new_deadline_ms,
        "new_deadline_utc": datetime.fromtimestamp(plan.new_deadline_ms // 1000, tz=timezone.utc).isoformat(),
        "pool_cmatra_before": bound.pool_cmatra,
        "moved_cmatra": bound.move,
        "left_at_old_pool": bound.pool_cmatra - bound.move,
        "new_pool_lovelace": new_pool[0].coin,
        "fee": fee,
        "min_fee": needed,
        "signed_size": signed_size,
        "total_collateral": total_collateral,
        "change_lovelace": change[0].coin,
        "required_signers": sorted(s.hex() for s in plan.signers),
        "witness_keys": sorted(k.hex() for k in bound.witness_keys),
        "validity_start": plan.validity_start,
        "ttl": plan.ttl,
        "ttl_utc": _utc(plan.ttl, snap.network),
        "old_deadline_slot": plan.old_deadline_slot,
        "redeemer_ex_units": list(plan.ex_units),
        "script_data_hash": script_data_hash.hex(),
    }


def _check_witnesses(vkeys: Any, txid: bytes, expected: frozenset[bytes]) -> None:
    seen = set()
    for entry in _elements(vkeys, "witnesses"):
        if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], bytes)
                and len(entry[0]) == 32 and isinstance(entry[1], bytes) and len(entry[1]) == 64):
            raise MigrationRejected("witnesses", "malformed vkey witness")
        key_hash = blake2b_224(entry[0])
        if key_hash not in expected or key_hash in seen:
            raise MigrationRejected("witnesses", f"unexpected or repeated witness {key_hash.hex()}")
        if not signature_verifies(entry[0], txid, entry[1]):
            raise MigrationRejected("witnesses", f"{key_hash.hex()}'s signature does not verify")
        seen.add(key_hash)
    if seen != expected:
        raise MigrationRejected("witnesses", f"missing {sorted(k.hex() for k in expected - seen)}")


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------


def _signed_witness_set(fields: Mapping[int, bytes], vkeys: CBORTag) -> bytes:
    """The witness set with ``vkeys`` as field 0 and every other field's
    original bytes, so the redeemers script_data_hash covers stay as signed."""
    others = sorted(k for k in fields if k != 0)
    out = bytes([0xA0 + len(others) + 1]) + b"\x00" + cbor2.dumps(vkeys)
    for key in others:
        out += bytes([key]) + fields[key]
    return out


def _key_witness(witness_cbor: bytes) -> Any:
    """The [vkey, signature] pair cardano-cli 11's `transaction witness` writes;
    _check_witnesses checks its shape."""
    try:
        return decode(witness_cbor)
    except CosignRejected as exc:
        raise MigrationRejected("witnesses", f"undecodable witness: {exc}") from exc


def assemble(unsigned: bytes, witness_cbors: list[bytes], snap: Snapshot, plan: Plan) -> bytes:
    check_migration_tx(unsigned, snap, plan)
    body_raw, witness_raw, tail = split_tx(unsigned)
    fields = _witness_fields(witness_raw)
    if 0 in fields:
        raise MigrationRejected("witnesses", "the transaction is already signed")
    vkeys = [_key_witness(w) for w in witness_cbors]
    _check_witnesses(vkeys, blake2b_256(body_raw), _bind(snap, plan).witness_keys)
    ordered = CBORTag(258, sorted(vkeys, key=lambda pair: pair[0]))
    signed = b"\x84" + body_raw + _signed_witness_set(fields, ordered) + tail
    check_migration_tx(signed, snap, plan)
    return signed


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text())


def _read_envelope(path: str) -> bytes:
    return bytes.fromhex(_read_json(path)["cborHex"])


def _write_envelope(path: str, kind: str, description: str, cbor: bytes) -> None:
    Path(path).write_text(json.dumps({"type": kind, "description": description, "cborHex": cbor.hex()}, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.pool_migration", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap_cmd = sub.add_parser("snapshot")
    snap_cmd.add_argument("--pool-ref", required=True)
    snap_cmd.add_argument("--fee-ref", required=True)
    snap_cmd.add_argument("--out", required=True)
    for name in ("build", "check", "assemble"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--snapshot", required=True)
        cmd.add_argument("--plan", required=True)
        if name != "build":
            cmd.add_argument("--tx", required=True)
        if name == "assemble":
            cmd.add_argument("--witness", action="append", required=True)
        if name != "check":
            cmd.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "snapshot":
            doc = snapshot_document(BlockfrostClient(), args.pool_ref, args.fee_ref, NETWORK)
            text = json.dumps(doc, indent=2)
            Path(args.out).write_text(text)
            print(f"snapshot {args.out} sha256 {hashlib.sha256(text.encode()).hexdigest()}")
            return 0
        snap, plan = load_snapshot(_read_json(args.snapshot)), load_plan(_read_json(args.plan))
        if args.command == "build":
            tx = build_migration_tx(snap, plan)
            _write_envelope(args.out, _TX_ENVELOPE, "cMATRA pool migration, unsigned", tx)
        elif args.command == "check":
            tx = _read_envelope(args.tx)
        else:
            tx = assemble(_read_envelope(args.tx), [_read_envelope(w) for w in args.witness], snap, plan)
            _write_envelope(args.out, _TX_ENVELOPE, "cMATRA pool migration, signed", tx)
        print(json.dumps(check_migration_tx(tx, snap, plan), indent=2))
        return 0
    except MigrationRejected as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
