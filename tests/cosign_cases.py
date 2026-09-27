"""Mainnet golden set and adversarial corpus for services.cosign_policy.

The golden fixture (tests/fixtures/cosign_golden.json.gz, rebuilt by
scripts/fetch_cosign_golden.py) holds every transaction that ever touched the
surrender pool address, the bodies of the transactions that produced their
inputs, and the PlutusV3 language views in force when each was built.

Each adversarial case starts from a real surrender and changes one thing an
attacker holding one admin key could change. The expected value is the exact
rejection code, so a check that stops firing turns its case red even when a
later check would still reject the transaction."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from pycardano import Address

from services.cosign_policy import (
    CosignConfig,
    Tag,
    decode,
    split_tx,
)
from tests.cbor_encode import encode
from tools.config import AGENT, T1_ADAM_PASS
from tools.process_surrender import (
    compute_redemption,
    load_rate_table,
    load_redeemable_nfts,
)

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = ROOT / "tests/fixtures/cosign_golden.json.gz"

POOL_BECH32 = "addr1w8s6rqdjlzm5he27v9s202p8vjumza8qfsmufm2f6dy68hg9mn27a"
QUARANTINE_BECH32 = "addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl"
POOL = Address.from_primitive(POOL_BECH32).to_primitive()
QUARANTINE = Address.from_primitive(QUARANTINE_BECH32).to_primitive()
CMATRA_POLICY = bytes.fromhex("7ff33a5565393dc47b48ac47becc12d92c9952e724e8446dfb6adc66")
CMATRA_NAME = bytes.fromhex("634d41545241")
ADMIN_1 = bytes.fromhex("f1eee828ae800a5bad6f3913603e20531162cdf44c5e0ffa49b6a936")
ADMIN_2 = bytes.fromhex("77fdb621ad5f926257ce1a2f845fc946ae3d5a683ac89128bad3410d")
DEADLINE_SLOT = 1_795_910_400 - 1_591_566_291
ATTACKER = bytes([0x61]) + b"\xee" * 28
RATES = load_rate_table(ROOT / "audit_pack/2026-04-19/rate_table_cmatra.json")
REDEEMABLE = load_redeemable_nfts(ROOT / "audit_pack/2026-09-27/redeemable_nft_units.json")

MAINNET = CosignConfig(
    network="mainnet",
    pool_address=POOL,
    quarantine_address=QUARANTINE,
    cmatra_policy=CMATRA_POLICY,
    cmatra_name=CMATRA_NAME,
    admin_pkhs=frozenset({ADMIN_1, ADMIN_2}),
    deadline_slot=DEADLINE_SLOT,
    rate_table=RATES,
    redeemable_nfts=REDEEMABLE,
    max_payout_per_tx=20_000_000 * 10**6,
)

# Output layout surrender_api produces (and every golden surrender has).
PAYOUT, CONTINUATION, QUARANTINE_OUT, CHANGE = 0, 1, 2, 3
# A surrender of 505,699 AGENT + one T1 ADAM pass, and one of AGENT + SHARDS
# from a different claimant.
BASE_T1 = "2e1925a81ea1d2668c120415b8603ce5c22f89044fb197834c46e022a5757fac"
BASE_FUNGIBLE = "28c89e15c6def6875ecbe992d6027726fdf1aec6fa453e6e8c57b7fd216ba7f1"


def blake(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


@lru_cache(maxsize=1)
def golden() -> dict[str, Any]:
    with gzip.open(GOLDEN_PATH, "rt") as fh:
        return json.load(fh)


@dataclass(frozen=True)
class Scenario:
    tx: bytes
    language_views: bytes
    parents: tuple[bytes, ...]
    now_slot: int
    cfg: CosignConfig = MAINNET


def golden_scenario(tx_hash: str) -> Scenario:
    doc = golden()
    row = next(t for t in doc["transactions"] if t["tx_hash"] == tx_hash)
    tx = bytes.fromhex(row["tx_cbor"])
    body = decode(split_tx(tx)[0])
    refs = _elements(body.get(0, [])) + _elements(body.get(13, [])) + _elements(body.get(18, []))
    parents = tuple(dict.fromkeys(bytes.fromhex(doc["parents"][r[0].hex()]) for r in refs))
    # Signing happens at build time, which is the validity start the builder set.
    now = body.get(8, row["slot"])
    return Scenario(tx, bytes.fromhex(doc["language_views"][row["language_views"]]), parents, now)


def _elements(value: Any) -> list:
    return list(value.value) if isinstance(value, Tag) else list(value)


class Draft:
    """A decoded surrender under edit. ``build`` re-encodes it, keeping the
    script_data_hash and the SPEND redeemer index consistent unless told not to."""

    def __init__(self, base: Scenario) -> None:
        body_raw, ws_raw, _ = split_tx(base.tx)
        self.body = decode(body_raw)
        self.ws = decode(ws_raw)
        self.parents = [decode(p) for p in base.parents]
        self.cfg = base.cfg
        self.now = base.now_slot
        self.language_views = base.language_views
        self.rehash = True
        self.reindex = True
        self.raw_body: bytes | None = None
        self.raw_ws: bytes | None = None
        self.drop_parents: set[bytes] = set()
        self.header = b"\x84"

    # -- navigation --------------------------------------------------------

    def inputs(self) -> list:
        return _elements(self.body[0])

    def set_inputs(self, refs: list) -> None:
        self.body[0] = Tag(258, refs) if isinstance(self.body[0], Tag) else refs

    def parent_output(self, ref: list) -> dict | list:
        for parent in self.parents:
            if blake(encode(parent)) == ref[0]:
                return parent[1][ref[1]]
        raise KeyError(ref[0].hex())

    def pool_ref(self) -> list:
        return next(r for r in self.inputs() if _address(self.parent_output(r)) == POOL)

    def claimant(self) -> bytes:
        return _address(self.body[1][PAYOUT])

    # -- edits -------------------------------------------------------------

    def move_cmatra(self, src: int, dst: int, amount: int) -> None:
        _add_asset(self.body[1][src], CMATRA_POLICY, CMATRA_NAME, -amount)
        _add_asset(self.body[1][dst], CMATRA_POLICY, CMATRA_NAME, amount)

    def replace_parent_output(self, ref: list, output: Any) -> list:
        """Re-point ``ref`` at a producing body whose output ``ref[1]`` is
        ``output``; returns the new reference."""
        for i, parent in enumerate(self.parents):
            if blake(encode(parent)) == ref[0]:
                parent = copy.deepcopy(parent)
                parent[1][ref[1]] = output
                self.parents[i] = parent
                new_ref = [blake(encode(parent)), ref[1]]
                self._rename_ref(ref, new_ref)
                return new_ref
        raise KeyError(ref[0].hex())

    def add_parent(self, outputs: list) -> bytes:
        parent = {0: [], 1: outputs, 2: 0}
        self.parents.append(parent)
        return blake(encode(parent))

    def _rename_ref(self, old: list, new: list) -> None:
        for key in (0, 13):
            if key in self.body:
                refs = [new if r == old else r for r in _elements(self.body[key])]
                self.body[key] = Tag(258, refs) if isinstance(self.body[key], Tag) else refs

    # -- output ------------------------------------------------------------

    def build(self) -> Scenario:
        if self.reindex and 5 in self.ws and isinstance(self.ws[5], dict):
            refs = sorted((r[0], r[1]) for r in self.inputs())
            pool = tuple(self.pool_ref())
            self.ws[5] = {
                (tag, refs.index(pool) if tag == 0 else index): value
                for (tag, index), value in self.ws[5].items()
            }
        ws_raw = self.raw_ws if self.raw_ws is not None else encode(self.ws)
        if self.rehash and 5 in self.ws:
            self.body[11] = blake(encode(self.ws[5]) + self.language_views)
        body_raw = self.raw_body if self.raw_body is not None else encode(self.body)
        parents = tuple(
            encode(p) for p in self.parents if blake(encode(p)) not in self.drop_parents
        )
        return Scenario(
            self.header + body_raw + ws_raw + b"\xf5\xf6",
            self.language_views, parents, self.now, self.cfg,
        )


def _address(output: Any) -> bytes:
    return output[0]


def _add_asset(output: Any, policy: bytes, name: bytes, delta: int) -> None:
    coin, assets = output[1] if isinstance(output[1], list) else (output[1], {})
    names = assets.setdefault(policy, {})
    names[name] = names.get(name, 0) + delta
    if names[name] == 0:
        del names[name]
        if not names:
            del assets[policy]
    output[1] = [coin, assets] if assets else coin


def _add_coin(output: Any, delta: int) -> None:
    if isinstance(output[1], list):
        output[1][0] += delta
    else:
        output[1] += delta


def _retarget_claimant(d: Draft, address: bytes) -> None:
    """Move every claimant input, collateral, output and collateral return to
    ``address``, keeping values, so only the claimant's address changes."""
    claimant = d.claimant()
    moved = {}
    for key in (0, 13):
        for ref in _elements(d.body[key]):
            out = d.parent_output(ref)
            if _address(out) == claimant and tuple(ref) not in moved:
                clone = copy.deepcopy(out)
                clone[0] = address
                moved[tuple(ref)] = clone
    txid = d.add_parent(list(moved.values()))
    mapping = {old: [txid, i] for i, old in enumerate(moved)}
    for key in (0, 13):
        refs = [mapping.get(tuple(r), r) for r in _elements(d.body[key])]
        d.body[key] = Tag(258, refs) if isinstance(d.body[key], Tag) else refs
    for out in d.body[1]:
        if _address(out) == claimant:
            out[0] = address
    d.body[16][0] = address


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    name: str
    expected: str
    build: Callable[[], Scenario]


CASES: list[Case] = []


def case(expected: str, base: str = BASE_T1):
    def register(edit: Callable[[Draft], None]) -> Callable[[Draft], None]:
        def build() -> Scenario:
            draft = Draft(golden_scenario(base))
            edit(draft)
            return draft.build()
        CASES.append(Case(edit.__name__, expected, build))
        return edit
    return register


# -- transaction shape -------------------------------------------------------

@case("tx_shape")
def tx_is_not_a_four_element_array(d):
    d.header = b"\x83"


@case("tx_shape")
def tx_has_trailing_bytes(d):
    d.raw_ws = encode(d.ws) + b"\x00"


@case("body_shape")
def body_is_not_a_map(d):
    d.raw_body = encode([1, 2, 3])


@case("cbor")
def body_repeats_a_map_key(d):
    body = encode(d.body)
    # Re-emit the fee entry (key 2) a second time with a map header one longer.
    d.raw_body = bytes([body[0] + 1]) + body[1:] + encode(2) + encode(0)


@case("body_shape")
def required_signers_not_a_set(d):
    d.body[14] = 5


@case("body_shape")
def outputs_not_an_array(d):
    d.body[1] = 5


@case("body_shape")
def input_reference_is_malformed(d):
    d.set_inputs(d.inputs() + [[b"\x00" * 32]])
    d.reindex = False


# -- forbidden body fields ---------------------------------------------------

@case("forbidden_body_field")
def extra_mint(d):
    d.body[9] = {CMATRA_POLICY: {CMATRA_NAME: 1}}


@case("forbidden_body_field")
def extra_withdrawal(d):
    d.body[5] = {bytes([0xE1]) + b"\xee" * 28: 0}


@case("forbidden_body_field")
def extra_certificate(d):
    d.body[4] = [[0, [0, b"\xee" * 28]]]


@case("forbidden_body_field")
def reference_input(d):
    d.body[18] = [d.pool_ref()]


@case("forbidden_body_field")
def governance_vote(d):
    d.body[19] = {(0, b"\xee" * 28): {}}


@case("forbidden_body_field")
def governance_proposal(d):
    d.body[20] = [[0, ATTACKER, [6], [None, None]]]


@case("forbidden_body_field")
def treasury_donation(d):
    d.body[22] = 1


@case("forbidden_body_field")
def metadata_hash(d):
    d.body[7] = b"\x00" * 32


# -- signers, fee, validity, collateral size ---------------------------------

@case("required_signers")
def extra_required_signer(d):
    d.body[14] = Tag(258, _elements(d.body[14]) + [b"\xee" * 28])


@case("required_signers")
def admin_listed_twice(d):
    d.body[14] = Tag(258, [ADMIN_1, ADMIN_2, ADMIN_2])


@case("required_signers")
def one_admin_missing(d):
    d.body[14] = Tag(258, [ADMIN_1])


@case("fee")
def fee_above_cap(d):
    d.body[2] = 2_000_001


@case("fee")
def fee_not_an_integer(d):
    d.body[2] = b"\x01"


@case("validity_window")
def ttl_beyond_window(d):
    d.body[3] = d.now + 12_001


@case("validity_window")
def ttl_missing(d):
    del d.body[3]


@case("after_deadline")
def ttl_after_deadline(d):
    d.cfg = replace(d.cfg, deadline_slot=d.body[3] - 1)


@case("total_collateral")
def total_collateral_above_cap(d):
    d.body[17] = 5_000_001


@case("total_collateral")
def total_collateral_missing(d):
    del d.body[17]


# -- input resolution --------------------------------------------------------

@case("parent_shape")
def producing_body_without_outputs(d):
    d.parents.append({0: []})


@case("unresolved_input")
def producing_body_withheld(d):
    d.drop_parents.add(d.pool_ref()[0])


@case("unresolved_input")
def output_index_out_of_range(d):
    ref = d.pool_ref()
    d.set_inputs([[ref[0], 99] if r == ref else r for r in d.inputs()])
    d.reindex = False


@case("duplicate_input")
def duplicated_pool_input(d):
    d.set_inputs(d.inputs() + [d.pool_ref()])
    d.reindex = False


@case("pool_input_count")
def second_claimants_surrender_merged_in(d):
    other = Draft(golden_scenario(BASE_FUNGIBLE))
    d.set_inputs(d.inputs() + other.inputs())
    d.body[1] = d.body[1] + other.body[1]
    d.parents += other.parents
    d.reindex = False


@case("pool_input_count")
def pool_input_removed(d):
    d.set_inputs([r for r in d.inputs() if r != d.pool_ref()])
    d.reindex = False


@case("pool_input_assets")
def pool_input_holds_another_token(d):
    out = copy.deepcopy(d.parent_output(d.pool_ref()))
    _add_asset(out, bytes.fromhex(AGENT.policy_id), bytes.fromhex(AGENT.asset_name_hex), 7)
    d.replace_parent_output(d.pool_ref(), out)


@case("pool_input_datum")
def pool_input_without_the_void_datum(d):
    out = copy.deepcopy(d.parent_output(d.pool_ref()))
    out[2] = [1, Tag(24, bytes.fromhex("d87a80"))]
    d.replace_parent_output(d.pool_ref(), out)


# -- claimant ----------------------------------------------------------------

@case("claimant_count")
def input_from_a_second_address(d):
    other = Draft(golden_scenario(BASE_FUNGIBLE))
    foreign = next(r for r in other.inputs() if r != other.pool_ref())
    d.set_inputs(d.inputs() + [foreign])
    d.parents += other.parents


@case("claimant_not_key_address")
def claimant_is_a_script(d):
    _retarget_claimant(d, bytes([0x71]) + b"\xab" * 28)


@case("claimant_is_admin")
def claimant_is_an_admin_key(d):
    _retarget_claimant(d, bytes([0x61]) + ADMIN_1)


@case("claimant_network")
def claimant_on_another_network(d):
    _retarget_claimant(d, bytes([0x60]) + b"\xab" * 28)


@case("collateral_not_claimant")
def collateral_taken_from_the_pool(d):
    d.body[13] = Tag(258, [d.pool_ref()])


@case("collateral_return_not_claimant")
def collateral_return_to_attacker(d):
    d.body[16][0] = ATTACKER


# -- outputs -----------------------------------------------------------------

@case("output_script_ref")
def claimant_output_carries_a_script(d):
    out = d.body[1][CHANGE]
    d.body[1][CHANGE] = {0: out[0], 1: out[1], 3: Tag(24, encode([3, b"\x46\x01\x00\x00\x22\x20\x01"]))}


@case("output_shape")
def output_with_negative_coin(d):
    d.body[1][CHANGE][1][0] = -1


@case("output_shape")
def output_is_neither_array_nor_map(d):
    d.body[1][CHANGE] = 5


@case("output_shape")
def output_array_too_long(d):
    d.body[1][CHANGE] = list(d.body[1][CHANGE]) + [b"\x00" * 32, 0]


@case("output_shape")
def output_map_without_value(d):
    d.body[1][CHANGE] = {0: d.claimant()}


@case("output_shape")
def output_address_not_bytes(d):
    d.body[1][CHANGE][0] = 5


@case("output_shape")
def output_value_malformed(d):
    d.body[1][CHANGE][1] = [1]


@case("output_shape")
def output_asset_quantity_negative(d):
    d.body[1][PAYOUT][1][1][CMATRA_POLICY][CMATRA_NAME] = -5


@case("output_shape")
def output_asset_bundle_malformed(d):
    d.body[1][PAYOUT][1][1][CMATRA_POLICY] = [CMATRA_NAME]


@case("pool_datum")
def pool_datum_altered(d):
    d.body[1][CONTINUATION][2] = [1, Tag(24, bytes.fromhex("d87a80"))]


@case("pool_datum")
def pool_datum_by_hash(d):
    d.body[1][CONTINUATION][2] = [0, blake(bytes.fromhex("d87980"))]


@case("pool_output_assets")
def continuation_carries_another_token(d):
    d.body[1][CONTINUATION][1][1][bytes.fromhex(AGENT.policy_id)] = {
        bytes.fromhex(AGENT.asset_name_hex): 1
    }
    _add_asset(d.body[1][QUARANTINE_OUT], bytes.fromhex(AGENT.policy_id),
               bytes.fromhex(AGENT.asset_name_hex), -1)


@case("pool_output_assets")
def continuation_emptied_of_cmatra(d):
    pool = d.body[1][CONTINUATION][1][1][CMATRA_POLICY][CMATRA_NAME]
    d.move_cmatra(CONTINUATION, PAYOUT, pool)


@case("output_address")
def pool_drained_to_a_third_output(d):
    d.body[1].append([ATTACKER, [2_000_000, {CMATRA_POLICY: {CMATRA_NAME: 10**12}}]])
    _add_asset(d.body[1][CONTINUATION], CMATRA_POLICY, CMATRA_NAME, -(10**12))


@case("pool_output_count")
def continuation_split_in_two(d):
    half = copy.deepcopy(d.body[1][CONTINUATION])
    half[1][1][CMATRA_POLICY][CMATRA_NAME] = 1
    _add_asset(d.body[1][CONTINUATION], CMATRA_POLICY, CMATRA_NAME, -1)
    d.body[1].append(half)


@case("pool_output_count")
def continuation_removed(d):
    pool = d.body[1][CONTINUATION][1][1][CMATRA_POLICY][CMATRA_NAME]
    _add_asset(d.body[1][PAYOUT], CMATRA_POLICY, CMATRA_NAME, pool)
    del d.body[1][CONTINUATION]


# -- entitlement -------------------------------------------------------------

T1_UNIT = next(u for u in sorted(REDEEMABLE) if u.startswith(T1_ADAM_PASS.policy_id))
T1_RATE = compute_redemption(RATES, "T1_ADAM_PASS", 1)


def _quarantine_nft(d: Draft) -> bytes:
    return next(iter(d.body[1][QUARANTINE_OUT][1][1][bytes.fromhex(T1_ADAM_PASS.policy_id)]))


@case("quarantine_assets")
def quarantined_nft_under_a_foreign_policy(d):
    """The count-inflation reproduction: a junk NFT stands in for the pass."""
    policy = bytes.fromhex(T1_ADAM_PASS.policy_id)
    name = _quarantine_nft(d)
    _add_asset(d.body[1][QUARANTINE_OUT], policy, name, -1)
    _add_asset(d.body[1][QUARANTINE_OUT], b"\xee" * 28, b"junk", 1)
    d.move_cmatra(CONTINUATION, PAYOUT, 99 * T1_RATE)


@case("quarantine_assets")
def quarantined_pass_minted_after_the_pin(d):
    policy = bytes.fromhex(T1_ADAM_PASS.policy_id)
    _add_asset(d.body[1][QUARANTINE_OUT], policy, _quarantine_nft(d), -1)
    _add_asset(d.body[1][QUARANTINE_OUT], policy, b"T1ADAM999", 1)


@case("nothing_surrendered")
def quarantine_output_removed(d):
    del d.body[1][QUARANTINE_OUT]


@case("quarantine_not_claimants")
def quarantined_pass_no_input_holds(d):
    policy = bytes.fromhex(T1_ADAM_PASS.policy_id)
    held = _quarantine_nft(d)
    extra = next(
        bytes.fromhex(u[56:]) for u in sorted(REDEEMABLE)
        if u.startswith(T1_ADAM_PASS.policy_id) and bytes.fromhex(u[56:]) != held
    )
    _add_asset(d.body[1][QUARANTINE_OUT], policy, extra, 1)
    d.move_cmatra(CONTINUATION, PAYOUT, T1_RATE)


@case("payout_mismatch")
def paid_for_a_hundred_passes_while_quarantining_one(d):
    d.move_cmatra(CONTINUATION, PAYOUT, 99 * T1_RATE)


@case("payout_mismatch")
def payout_one_base_unit_high(d):
    d.move_cmatra(CONTINUATION, PAYOUT, 1)


@case("payout_mismatch")
def payout_one_base_unit_low(d):
    d.move_cmatra(PAYOUT, CONTINUATION, 1)


@case("per_tx_cap")
def payout_above_the_per_transaction_cap(d):
    d.cfg = replace(d.cfg, max_payout_per_tx=1)


@case("pool_lovelace")
def lovelace_leaks_out_of_the_pool(d):
    d.body[1][CONTINUATION][1][0] -= 500_001
    _add_coin(d.body[1][CHANGE], 500_001)


# -- redeemers ---------------------------------------------------------------

@case("witness_shape")
def witness_set_is_not_a_map(d):
    d.raw_ws = encode([1])
    d.rehash = False


@case("witness_shape")
def witness_set_is_indefinite(d):
    d.raw_ws = b"\xbf" + encode(d.ws)[1:] + b"\xff"


@case("cbor")
def witness_set_repeats_a_key(d):
    ws = encode(d.ws)
    d.raw_ws = bytes([ws[0] + 1]) + ws[1:] + encode(5) + encode({})


@case("redeemers_missing")
def redeemers_removed(d):
    del d.ws[5]


@case("script_data_hash")
def redeemer_swapped_without_rehashing(d):
    d.rehash = False
    key = next(iter(d.ws[5]))
    d.ws[5][key][0] = Tag(122, [])


@case("redeemer_action")
def admin_withdraw_redeemer(d):
    key = next(iter(d.ws[5]))
    d.ws[5][key][0] = Tag(122, [])


@case("redeemer_count")
def second_redeemer(d):
    d.ws[5][(1, 0)] = [Tag(121, []), [1, 1]]


@case("redeemer_target")
def redeemer_on_another_input(d):
    d.reindex = False
    (tag, index), value = next(iter(d.ws[5].items()))
    d.ws[5] = {(tag, 1 - index): value}


@case("redeemer_target")
def redeemer_with_a_mint_purpose(d):
    d.reindex = False
    (_, index), value = next(iter(d.ws[5].items()))
    d.ws[5] = {(1, index): value}


@case("redeemers_shape")
def redeemer_entry_malformed(d):
    key = next(iter(d.ws[5]))
    d.ws[5][key] = d.ws[5][key][:1]


@case("redeemers_shape")
def redeemers_malformed(d):
    d.ws[5] = [[0, 0]]


# ---------------------------------------------------------------------------
# Decoder probes: bytes that must be refused, and that must still decode
# ---------------------------------------------------------------------------

DECODE_REFUSED: list[tuple[str, bytes]] = [
    ("empty", b""),
    ("truncated_head", b"\x19\x01"),
    ("truncated_string", b"\x45\x01\x02"),
    ("truncated_indefinite", b"\x9f\x01"),
    ("half_float", b"\xf9\x3c\x00"),
    ("undefined_simple", b"\xf7"),
    ("indefinite_integer", b"\x1f"),
    ("bad_string_chunk", b"\x5f\x61\x61\xff"),
    ("invalid_utf8", b"\x62\xc3\x28"),
    ("map_as_key", b"\xa1\xa0\x01"),
    ("duplicate_key", b"\xa2\x01\x01\x01\x02"),
    ("trailing_bytes", b"\x01\x02"),
    ("too_deep", b"\x81" * 70 + b"\x01"),
]

DECODE_ACCEPTED: list[tuple[bytes, Any]] = [
    (b"\x9f\x01\x02\xff", [1, 2]),
    (b"\xbf\x01\x02\xff", {1: 2}),
    (b"\x5f\x41\x01\x41\x02\xff", b"\x01\x02"),
    (b"\xd9\x01\x02\x80", Tag(258, [])),
    (b"\x3a\x00\x01\x86\x9f", -100_000),
    (b"\xf5", True),
]

# Transactions whose final item is cut short: the decoder must say "cbor"
# (truncated) rather than read past the end and leave split_tx to notice.
SPLIT_REFUSED: list[tuple[str, bytes]] = [
    ("truncated_uint_in_tail", bytes.fromhex("84a0a0f51901")),
    ("truncated_string_in_tail", bytes.fromhex("84a0a0f5450102")),
]
