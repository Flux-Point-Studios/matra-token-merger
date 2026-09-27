"""What an admin checks before it adds its signature to a surrender-pool spend.

The pool validator checks the two admin signatures and the time window; every
other property of a surrender is checked here, off chain, by each signer. The
policy reads the complete transaction and approves only a surrender:

  * exactly one pool input, holding only ADA and cMATRA under the Void inline
    datum; every other input and all collateral from one key-hash address on
    the pool's network (the claimant), which is not an admin key. Each input is
    resolved from a producing transaction body whose blake2b-256 equals the
    referenced transaction id, so no input value is taken on trust;
  * exactly one pool continuation, back to the pool address with the Void
    inline datum, holding only ADA and cMATRA and at most 0.5 ADA less ADA;
  * the pool loses exactly the entitlement for the legacy units paid to the
    quarantine address in the same transaction, priced by
    ``surrendered_entitlement`` (surrender_api prices with the same function),
    and the claimant's inputs hold those units;
  * every other output, and the collateral return, goes to the claimant;
  * no mint, withdrawal, certificate, governance action, reference input,
    reference script, metadata or extra required signer;
  * one redeemer, ProcessSurrender, on the pool input, bound to the body
    through script_data_hash;
  * bounded fee, collateral and validity interval, still open when checked and
    closing before the surrender deadline (so an AdminWithdraw spend can never
    validate);
  * a per-transaction payout cap (the per-day cap is persisted by the
    co-signer service).

Decoding is strict and self-contained: a duplicated map key anywhere, trailing
bytes, floats or undefined simple values are refused, so the policy never
reads a different value than the ledger would.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple

from pycardano import Address

from tools.process_surrender import (
    load_rate_table,
    load_redeemable_nfts,
    surrendered_entitlement,
)

MAX_FEE_LOVELACE = 2_000_000
MAX_TOTAL_COLLATERAL_LOVELACE = 5_000_000
MAX_POOL_LOVELACE_OUTFLOW = 500_000
# pycardano sets ttl = tip + 10_000 slots; the rest absorbs clock skew between
# the builder's view of the tip and the signer's clock.
MAX_VALIDITY_SLOTS = 12_000

# POSIX seconds minus slot number, per network (1-second slots since Shelley).
SLOT_OFFSET_S = {
    "mainnet": 1_591_566_291,
    "preprod": 1_655_683_200,
    "preview": 1_666_656_000,
}

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RATE_TABLE_PATH = _ROOT / "audit_pack/2026-04-19/rate_table_cmatra.json"
DEFAULT_REDEEMABLE_NFTS_PATH = _ROOT / "audit_pack/2026-09-27/redeemable_nft_units.json"

# Conway tx-body keys a surrender uses: inputs, outputs, fee, ttl, validity
# start, script_data_hash, collateral inputs, required signers, collateral
# return, total collateral.
_ALLOWED_BODY_KEYS = frozenset({0, 1, 2, 3, 8, 11, 13, 14, 16, 17})
_MAX_DEPTH = 64


class CosignRejected(ValueError):
    """The transaction is not a surrender this policy approves."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class Tag(NamedTuple):
    tag: int
    value: Any


VOID_INLINE_DATUM = [1, Tag(24, bytes.fromhex("d87980"))]
PROCESS_SURRENDER = Tag(121, [])


# ---------------------------------------------------------------------------
# Strict CBOR
# ---------------------------------------------------------------------------


def _head(buf: bytes, pos: int) -> tuple[int, int | None, int]:
    """(major type, argument or None for indefinite length, next offset)."""
    if pos >= len(buf):
        raise CosignRejected("cbor", "truncated")
    major, info = buf[pos] >> 5, buf[pos] & 0x1F
    pos += 1
    if info < 24:
        return major, info, pos
    if info <= 27:
        size = 1 << (info - 24)
        if pos + size > len(buf):
            raise CosignRejected("cbor", "truncated")
        return major, int.from_bytes(buf[pos:pos + size], "big"), pos + size
    if info == 31 and major in (2, 3, 4, 5):
        return major, None, pos
    raise CosignRejected("cbor", f"unsupported initial byte {buf[pos - 1]:#04x}")


def _at_break(buf: bytes, pos: int) -> bool:
    if pos >= len(buf):
        raise CosignRejected("cbor", "truncated")
    return buf[pos] == 0xFF


def _hashable(key: Any) -> Any:
    if isinstance(key, list):
        return tuple(_hashable(k) for k in key)
    if isinstance(key, Tag):
        return Tag(key.tag, _hashable(key.value))
    if isinstance(key, dict):
        raise CosignRejected("cbor", "map used as a map key")
    return key


_SIMPLE = {0xF4: False, 0xF5: True, 0xF6: None}


def _string(buf: bytes, pos: int, major: int, length: int | None) -> tuple[Any, int]:
    if length is None:
        chunks = []
        while not _at_break(buf, pos):
            chunk_major, size, pos = _head(buf, pos)
            if chunk_major != major or size is None:
                raise CosignRejected("cbor", "bad string chunk")
            chunks.append(buf[pos:pos + size])
            pos += size
        data, pos = b"".join(chunks), pos + 1
    else:
        data, pos = buf[pos:pos + length], pos + length
    if pos > len(buf):
        raise CosignRejected("cbor", "truncated")
    if major == 2:
        return bytes(data), pos
    try:
        return data.decode("utf-8"), pos
    except UnicodeDecodeError as exc:
        raise CosignRejected("cbor", "invalid utf-8") from exc


def _item(buf: bytes, pos: int, depth: int) -> tuple[Any, int]:
    if depth > _MAX_DEPTH:
        raise CosignRejected("cbor", "nesting too deep")
    if pos < len(buf) and buf[pos] >> 5 == 7:
        if buf[pos] not in _SIMPLE:
            raise CosignRejected("cbor", f"unsupported simple value {buf[pos]:#04x}")
        return _SIMPLE[buf[pos]], pos + 1
    major, arg, pos = _head(buf, pos)
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        return _string(buf, pos, major, arg)
    if major == 4:
        items = []
        while (len(items) < arg) if arg is not None else not _at_break(buf, pos):
            value, pos = _item(buf, pos, depth + 1)
            items.append(value)
        return items, pos if arg is not None else pos + 1
    if major == 5:
        entries: dict[Any, Any] = {}
        pairs = 0
        while (pairs < arg) if arg is not None else not _at_break(buf, pos):
            key, pos = _item(buf, pos, depth + 1)
            value, pos = _item(buf, pos, depth + 1)
            pairs += 1
            key = _hashable(key)
            if key in entries:
                raise CosignRejected("cbor", f"duplicate map key {key!r}")
            entries[key] = value
        return entries, pos if arg is not None else pos + 1
    value, pos = _item(buf, pos, depth + 1)
    return Tag(arg, value), pos


def decode(buf: bytes) -> Any:
    """Decode exactly one CBOR item spanning all of ``buf``."""
    value, end = _item(buf, 0, 0)
    if end != len(buf):
        raise CosignRejected("cbor", "trailing bytes")
    return value


def split_tx(tx_cbor: bytes) -> tuple[bytes, bytes, bytes]:
    """The exact bytes of a serialized transaction's body, its witness set,
    and the ``is_valid`` + auxiliary-data tail."""
    major, count, pos = _head(tx_cbor, 0)
    if major != 4 or count != 4:
        raise CosignRejected("tx_shape", "transaction is not a 4-element array")
    spans = []
    for _ in range(4):
        start = pos
        _, pos = _item(tx_cbor, pos, 1)
        spans.append(tx_cbor[start:pos])
    if pos != len(tx_cbor):
        raise CosignRejected("tx_shape", "trailing bytes after transaction")
    return spans[0], spans[1], spans[2] + spans[3]


def _map_raw_values(buf: bytes) -> dict[Any, bytes]:
    """A definite-length map's values as their exact encoded bytes. ``buf`` has
    already passed ``decode`` (via ``split_tx``), so keys are unique."""
    major, count, pos = _head(buf, 0)
    if major != 5 or count is None:
        raise CosignRejected("witness_shape", "witness set is not a definite map")
    out: dict[Any, bytes] = {}
    for _ in range(count):
        key, pos = _item(buf, pos, 1)
        start = pos
        _, pos = _item(buf, pos, 1)
        out[key] = buf[start:pos]
    return out


def blake2b_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


# ---------------------------------------------------------------------------
# Ledger shapes
# ---------------------------------------------------------------------------


class Output(NamedTuple):
    address: bytes
    coin: int
    assets: dict[bytes, dict[bytes, int]]
    datum: Any
    has_script_ref: bool


def _uint(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _elements(value: Any) -> list:
    if isinstance(value, Tag) and value.tag == 258:
        value = value.value
    if not isinstance(value, list):
        raise CosignRejected("body_shape", "set field is not an array")
    return value


def _value(raw: Any) -> tuple[int, dict[bytes, dict[bytes, int]]]:
    if _uint(raw):
        return raw, {}
    if not (isinstance(raw, list) and len(raw) == 2
            and _uint(raw[0]) and isinstance(raw[1], dict)):
        raise CosignRejected("output_shape", "malformed value")
    coin, assets = raw
    for policy, names in assets.items():
        if not isinstance(policy, bytes) or not isinstance(names, dict):
            raise CosignRejected("output_shape", "malformed asset bundle")
        for name, quantity in names.items():
            if not isinstance(name, bytes) or not _uint(quantity):
                raise CosignRejected("output_shape", "malformed asset quantity")
    return coin, assets


def parse_output(raw: Any) -> Output:
    if isinstance(raw, list) and len(raw) in (2, 3):
        address, value = raw[0], raw[1]
        datum = [0, raw[2]] if len(raw) == 3 else None
        has_script_ref = False
    elif isinstance(raw, dict) and {0, 1} <= set(raw) <= {0, 1, 2, 3}:
        address, value, datum, has_script_ref = raw[0], raw[1], raw.get(2), 3 in raw
    else:
        raise CosignRejected("output_shape", "malformed transaction output")
    if not isinstance(address, bytes) or not address:
        raise CosignRejected("output_shape", "malformed address")
    coin, assets = _value(value)
    return Output(address, coin, assets, datum, has_script_ref)


def _is_key_address(address: bytes) -> bool:
    """Shelley address whose payment credential is a key hash (types 0, 2, 4, 6)."""
    return address[0] >> 4 in (0, 2, 4, 6)


def slot_at(posix_seconds: float, network: str) -> int:
    return int(posix_seconds) - SLOT_OFFSET_S[network]


def _redeemer_entries(redeemers: Any) -> list[tuple[Any, Any, Any]]:
    """(tag, index, data) for both the Conway map and the legacy list form."""
    if isinstance(redeemers, dict):
        entries = []
        for key, value in redeemers.items():
            if not (isinstance(key, tuple) and len(key) == 2
                    and isinstance(value, list) and len(value) == 2):
                raise CosignRejected("redeemers_shape", "malformed redeemer map entry")
            entries.append((key[0], key[1], value[0]))
        return entries
    if isinstance(redeemers, list) and all(
        isinstance(r, list) and len(r) == 4 for r in redeemers
    ):
        return [(r[0], r[1], r[2]) for r in redeemers]
    raise CosignRejected("redeemers_shape", "malformed redeemers")


def _quantity(assets: Mapping[bytes, Mapping[bytes, int]], policy: bytes, name: bytes) -> int:
    return assets.get(policy, {}).get(name, 0)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CosignConfig:
    network: str
    pool_address: bytes
    quarantine_address: bytes
    cmatra_policy: bytes
    cmatra_name: bytes
    admin_pkhs: frozenset[bytes]
    deadline_slot: int
    rate_table: Mapping[str, Any]
    redeemable_nfts: frozenset[str]
    max_payout_per_tx: int


def load_config(env: Mapping[str, str], admin_pkhs: Iterable[bytes]) -> CosignConfig:
    """Build the policy from environment variables. Everything that decides
    what gets signed is required; only the two data files default to the
    committed rate table and NFT pin."""

    def need(key: str) -> str:
        value = env.get(key, "").strip()
        if not value:
            raise ValueError(f"{key} must be set")
        return value

    admins = frozenset(admin_pkhs)
    if len(admins) != 2:
        raise ValueError("the pool needs two distinct admin key hashes")
    network = need("NETWORK")
    deadline_ms = int(need("SURRENDER_DEADLINE_POSIX_MS"))
    return CosignConfig(
        network=network,
        pool_address=Address.from_primitive(need("SURRENDER_SCRIPT_ADDRESS")).to_primitive(),
        quarantine_address=Address.from_primitive(need("QUARANTINE_ADDRESS")).to_primitive(),
        cmatra_policy=bytes.fromhex(need("CMATRA_POLICY_HEX")),
        cmatra_name=bytes.fromhex(need("CMATRA_ASSET_HEX")),
        admin_pkhs=admins,
        deadline_slot=slot_at(deadline_ms // 1000, network),
        rate_table=load_rate_table(Path(env.get("RATE_TABLE_PATH") or DEFAULT_RATE_TABLE_PATH)),
        redeemable_nfts=load_redeemable_nfts(
            Path(env.get("REDEEMABLE_NFTS_PATH") or DEFAULT_REDEEMABLE_NFTS_PATH)
        ),
        max_payout_per_tx=int(need("MAX_CMATRA_PER_TX")),
    )


@dataclass(frozen=True)
class Approval:
    tx_hash: bytes
    payout: int
    claimant: bytes


def evaluate_surrender(
    tx_cbor: bytes,
    language_views: bytes,
    parent_bodies: Iterable[bytes],
    now_slot: int,
    cfg: CosignConfig,
) -> Approval:
    """Approve ``tx_cbor`` as a surrender or raise :class:`CosignRejected`.

    ``language_views`` is the cost-model encoding that closes the
    script_data_hash preimage. It need not be trusted: the preimage starts with
    the redeemers, and a CBOR item is self-delimiting, so a matching hash pins
    the redeemers inspected here whatever suffix the caller supplies.
    """
    body_raw, witness_raw, _ = split_tx(tx_cbor)
    body = decode(body_raw)
    if not isinstance(body, dict):
        raise CosignRejected("body_shape", "transaction body is not a map")
    forbidden = set(body) - _ALLOWED_BODY_KEYS
    if forbidden:
        raise CosignRejected("forbidden_body_field", f"keys {sorted(forbidden, key=str)}")

    signers = _elements(body.get(14, []))
    if len(signers) != len(cfg.admin_pkhs) or set(signers) != cfg.admin_pkhs:
        raise CosignRejected("required_signers", "required signers must be exactly the two admins")

    fee = body.get(2)
    if not _uint(fee) or fee > MAX_FEE_LOVELACE:
        raise CosignRejected("fee", f"fee {fee!r} above {MAX_FEE_LOVELACE}")
    ttl = body.get(3)
    if not _uint(ttl) or ttl > now_slot + MAX_VALIDITY_SLOTS:
        raise CosignRejected("validity_window", f"ttl {ttl!r} not within {MAX_VALIDITY_SLOTS} slots")
    if ttl <= now_slot:
        raise CosignRejected("expired", f"ttl {ttl} has passed")
    if ttl > cfg.deadline_slot:
        raise CosignRejected("after_deadline", "validity interval reaches past the surrender deadline")
    total_collateral = body.get(17)
    if not _uint(total_collateral) or total_collateral > MAX_TOTAL_COLLATERAL_LOVELACE:
        raise CosignRejected("total_collateral", f"total collateral {total_collateral!r}")

    producing: dict[bytes, list] = {}
    for parent in parent_bodies:
        parent_body = decode(parent)
        if not isinstance(parent_body, dict) or not isinstance(parent_body.get(1), list):
            raise CosignRejected("parent_shape", "producing body has no outputs")
        producing[blake2b_256(parent)] = parent_body[1]

    def resolve(raw: Any) -> tuple[tuple[bytes, int], Output]:
        if not (isinstance(raw, list) and len(raw) == 2
                and isinstance(raw[0], bytes) and _uint(raw[1])):
            raise CosignRejected("body_shape", "malformed input reference")
        outputs = producing.get(raw[0])
        if outputs is None or raw[1] >= len(outputs):
            raise CosignRejected("unresolved_input", f"{raw[0].hex()}#{raw[1]}")
        return (raw[0], raw[1]), parse_output(outputs[raw[1]])

    input_refs = _elements(body.get(0))
    spent = dict(resolve(raw) for raw in input_refs)
    if len(spent) != len(input_refs):
        raise CosignRejected("duplicate_input", "an input is listed twice")
    pool_refs = [ref for ref, out in spent.items() if out.address == cfg.pool_address]
    if len(pool_refs) != 1:
        raise CosignRejected("pool_input_count", f"{len(pool_refs)} pool inputs")
    pool_ref = pool_refs[0]
    pool_in = spent.pop(pool_ref)
    pool_in_cmatra = _quantity(pool_in.assets, cfg.cmatra_policy, cfg.cmatra_name)
    if pool_in.assets != {cfg.cmatra_policy: {cfg.cmatra_name: pool_in_cmatra}}:
        raise CosignRejected("pool_input_assets", "pool input holds more than cMATRA")
    if pool_in.datum != VOID_INLINE_DATUM:
        raise CosignRejected("pool_input_datum", "pool input lacks the Void inline datum")

    claimants = {out.address for out in spent.values()}
    if len(claimants) != 1:
        raise CosignRejected("claimant_count", f"inputs from {len(claimants)} addresses")
    claimant = claimants.pop()
    if not _is_key_address(claimant):
        raise CosignRejected("claimant_not_key_address", "claimant inputs are not key-locked")
    if claimant[1:29] in cfg.admin_pkhs:
        raise CosignRejected("claimant_is_admin", "claimant pays with an admin key")
    if claimant[0] & 0x0F != cfg.pool_address[0] & 0x0F:
        raise CosignRejected("claimant_network", "claimant address is on another network")

    for ref in _elements(body.get(13, [])):
        if resolve(ref)[1].address != claimant:
            raise CosignRejected("collateral_not_claimant", "collateral is not the claimant's")
    if 16 in body and parse_output(body[16]).address != claimant:
        raise CosignRejected("collateral_return_not_claimant", "collateral return leaves the claimant")

    pool_outs: list[Output] = []
    quarantined: dict[str, int] = {}
    outputs = body.get(1)
    if not isinstance(outputs, list):
        raise CosignRejected("body_shape", "outputs are not an array")
    for raw in outputs:
        out = parse_output(raw)
        if out.has_script_ref:
            raise CosignRejected("output_script_ref", "an output carries a reference script")
        if out.address == cfg.pool_address:
            amount = _quantity(out.assets, cfg.cmatra_policy, cfg.cmatra_name)
            if out.datum != VOID_INLINE_DATUM:
                raise CosignRejected("pool_datum", "pool continuation lacks the Void inline datum")
            if amount == 0 or out.assets != {cfg.cmatra_policy: {cfg.cmatra_name: amount}}:
                raise CosignRejected("pool_output_assets", "pool continuation must hold only cMATRA")
            pool_outs.append(out)
        elif out.address == cfg.quarantine_address:
            for policy, names in out.assets.items():
                for name, quantity in names.items():
                    unit = (policy + name).hex()
                    quarantined[unit] = quarantined.get(unit, 0) + quantity
        elif out.address != claimant:
            raise CosignRejected("output_address", f"output to {out.address.hex()}")
    if len(pool_outs) != 1:
        raise CosignRejected("pool_output_count", f"{len(pool_outs)} pool continuations")
    continuation = pool_outs[0]

    try:
        entitlement = surrendered_entitlement(cfg.rate_table, quarantined, cfg.redeemable_nfts)
    except (KeyError, ValueError) as exc:
        raise CosignRejected("quarantine_assets", str(exc)) from exc
    if entitlement == 0:
        raise CosignRejected("nothing_surrendered", "no legacy asset reaches quarantine")
    for unit, quantity in quarantined.items():
        policy, name = bytes.fromhex(unit[:56]), bytes.fromhex(unit[56:])
        held = sum(_quantity(out.assets, policy, name) for out in spent.values())
        if held < quantity:
            raise CosignRejected("quarantine_not_claimants", f"inputs hold {held} of {unit}")

    payout = pool_in_cmatra - _quantity(continuation.assets, cfg.cmatra_policy, cfg.cmatra_name)
    if payout != entitlement:
        raise CosignRejected("payout_mismatch", f"pool pays {payout}, entitlement is {entitlement}")
    if payout > cfg.max_payout_per_tx:
        raise CosignRejected("per_tx_cap", f"payout {payout} above {cfg.max_payout_per_tx}")
    if pool_in.coin - continuation.coin > MAX_POOL_LOVELACE_OUTFLOW:
        raise CosignRejected("pool_lovelace", "pool continuation loses lovelace")

    witness = _map_raw_values(witness_raw)
    if 5 not in witness:
        raise CosignRejected("redeemers_missing", "no redeemers")
    preimage = witness[5] + witness.get(4, b"") + language_views
    if body.get(11) != blake2b_256(preimage):
        raise CosignRejected("script_data_hash", "redeemers do not match script_data_hash")
    redeemers = _redeemer_entries(decode(witness[5]))
    if len(redeemers) != 1:
        raise CosignRejected("redeemer_count", f"{len(redeemers)} redeemers")
    tag, index, data = redeemers[0]
    canonical_inputs = sorted([pool_ref, *spent])
    if tag != 0 or index != canonical_inputs.index(pool_ref):
        raise CosignRejected("redeemer_target", "redeemer is not on the pool input")
    if data != PROCESS_SURRENDER:
        raise CosignRejected("redeemer_action", "redeemer is not ProcessSurrender")

    return Approval(tx_hash=blake2b_256(body_raw), payout=payout, claimant=claimant)
