"""services.chain_check asks the signer's own view of the chain what the
transaction bytes cannot show.

A unit whose supply grew after the pin is refused outright: an edition minted
later is indistinguishable from the pinned one. A pinned NFT whose own history
shows any mint or burn after the pin's supply slot is refused too. That
history is a separate query, made after the input lookups, so a supply answer
that is stale, or a mint followed by a burn, is not enough to pass.

Every input must be an unspent output on chain, or an output of a surrender
this signer already recorded (chained surrenders spend outputs still in the
mempool). A producing body proves what an output would hold, not that it
exists, so without this a caller could invent outputs, and approvals that
can never land would still use up limits.

A surrendered unit must have reached the claimant's payment key at least 72
hours before the check. Its arrival is followed back through transactions
inside that window while they only move it between the claimant's own
addresses; arriving from anyone else, or minted, inside the window refuses
the surrender. Change of a surrender this signer recorded is followed back
through that surrender's own inputs, whether or not the chain has seen it.

Each scenario is a probe; tests/test_chain_check_mutation.py runs every probe
against mutants of services.chain_check."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from unittest import mock

import pytest
import requests
import responses
from responses.matchers import query_param_matcher

from services.chain_check import confirm_on_chain
from services.cosign_policy import Approval, CosignRejected
from tests.cbor_encode import encode
from tests.cosign_cases import MAINNET, PIN, FakeChain, blake, http_error
from tools.api_clients import BlockfrostClient, BlockfrostUnavailable
from tools.config import AGENT, SHARDS, chain_api_base_url, FLUX_PASS, T1_ADAM_PASS, T2_ADAM_PASS

ROOT = Path(__file__).resolve().parent.parent
T1_UNIT = next(u for u in sorted(PIN.nft_units) if u.startswith(T1_ADAM_PASS.policy_id))

CLAIMANT = b"\x61" + b"\x02" * 28
# A producing body with two outputs, and a chain view that has seen it.
PARENT = encode({0: [], 1: [[CLAIMANT, 1_000_000], [CLAIMANT, 2_000_000]], 2: 0})
PARENT_ID = blake(PARENT)
RECORDED_ID = b"\x09" * 32
# What a signer recorded: a surrender's outputs, besides the pool's, that it spends.
RECORDED_SURRENDER = {RECORDED_ID: ((PARENT_ID, 1),)}.get
NOTHING_RECORDED = {}.get


def _chain(**kwargs) -> FakeChain:
    return FakeChain({PARENT_ID.hex(): PARENT}, **kwargs)


def _approval(units: dict[str, int], inputs=((PARENT_ID, 0), (PARENT_ID, 1)),
              unit_inputs=(), claimant=CLAIMANT, checked_at=0) -> Approval:
    return Approval(
        tx_hash=b"\x01" * 32, payout=1, claimant=claimant,
        pool_input=inputs[0], units=units, inputs=tuple(inputs), spends=tuple(inputs),
        unit_inputs=tuple(unit_inputs), checked_at=checked_at,
    )


@dataclass(frozen=True)
class Probe:
    name: str
    expected: str
    """A refusal code, "unavailable" (ChainUnavailable) or "confirmed"."""
    run: Callable[[Callable[..., None]], None]
    """Runs the scenario through the confirm_on_chain it is given."""
    detail: str = ""
    """Text the refusal or the unavailable error names."""


PROBES: list[Probe] = []


def probe(expected: str, detail: str = ""):
    def register(run: Callable[[Callable[..., None]], None]):
        PROBES.append(Probe(run.__name__, expected, run, detail))
        return run
    return register


def outcome(p: Probe, confirm: Callable[..., None]) -> tuple[str, str]:
    """(outcome, message) of ``p`` run through ``confirm``: a refusal's code
    and detail, "unavailable" and the error, "confirmed", or a crash. By class
    name, so a mutant module's own ChainUnavailable counts too."""
    try:
        p.run(confirm)
    except Exception as exc:
        if type(exc).__name__ == "ChainUnavailable":
            return "unavailable", str(exc)
        code = getattr(exc, "code", None)
        return (code, exc.detail) if code else (f"crash:{type(exc).__name__}", str(exc))
    return "confirmed", ""


# -- supply ----------------------------------------------------------------------


@probe("confirmed")
def units_at_their_pinned_supply(confirm):
    confirm(_approval({AGENT.unit: 5, T1_UNIT: 1}), MAINNET, _chain(), NOTHING_RECORDED)


@probe("confirmed")
def a_unit_burned_below_its_pinned_supply(confirm):
    confirm(_approval({AGENT.unit: 5}), MAINNET, _chain(supply={AGENT.unit: 10}), NOTHING_RECORDED)


def _minted_past_its_pinned_supply(unit: str) -> Callable:
    def run(confirm):
        grown = _chain(supply={unit: PIN.supply[unit] + 1})
        confirm(_approval({AGENT.unit: 5, T1_UNIT: 1, unit: 1}), MAINNET, grown, NOTHING_RECORDED)
    return run


# The T1 and T2 policy keys are exposed, and the SHARDS policy can still
# mint: a unit of any of them minted past its pinned supply is refused.
T2_UNIT = next(u for u in sorted(PIN.nft_units) if u.startswith(T2_ADAM_PASS.policy_id))
for _name, _unit in (("nft", T1_UNIT), ("fungible", AGENT.unit), ("t2", T2_UNIT), ("shards", SHARDS.unit)):
    PROBES.append(Probe(f"{_name}_minted_past_its_pinned_supply", "supply_grown",
                        _minted_past_its_pinned_supply(_unit), _unit))


@probe("unavailable", detail=T1_UNIT)
def a_unit_the_chain_view_does_not_know(confirm):
    chain = _chain()
    del chain.supply[T1_UNIT]
    confirm(_approval({T1_UNIT: 1}), MAINNET, chain, NOTHING_RECORDED)


FAILURES = {
    "connection_refused": requests.ConnectionError("connection refused"),
    "timeout": requests.Timeout("read timed out"),
    "retries_exhausted": BlockfrostUnavailable("Exhausted retries"),
    "forbidden": http_error(403, "Forbidden"),
}


def _chain_view_that_cannot_answer(failure: Exception) -> Callable:
    def run(confirm):
        confirm(_approval({AGENT.unit: 5}), MAINNET, _chain(failure=failure), NOTHING_RECORDED)
    return run


def _input_lookup_that_fails(failure: Exception) -> Callable:
    """Only a 404 means the chain has no such transaction."""
    class InputsUnanswered(FakeChain):
        def get_tx_utxos(self, tx_hash):
            raise failure

    def run(confirm):
        chain = InputsUnanswered({PARENT_ID.hex(): PARENT})
        confirm(_approval({AGENT.unit: 5}), MAINNET, chain, NOTHING_RECORDED)
    return run


for _name, _failure in FAILURES.items():
    PROBES.append(Probe(f"a_chain_view_that_cannot_answer_{_name}", "unavailable",
                        _chain_view_that_cannot_answer(_failure)))
    PROBES.append(Probe(f"an_input_lookup_that_fails_{_name}", "unavailable",
                        _input_lookup_that_fails(_failure)))


# -- inputs --------------------------------------------------------------------


INVENTED = b"\x0e" * 32


@probe("input_unknown", detail=INVENTED.hex())
def an_input_from_a_transaction_the_chain_never_saw(confirm):
    confirm(_approval({AGENT.unit: 5}, [(PARENT_ID, 0), (INVENTED, 0)]), MAINNET, _chain(), NOTHING_RECORDED)


@probe("input_spent", detail=f"{PARENT_ID.hex()}#1")
def an_input_already_spent(confirm):
    chain = _chain()
    chain.spent[(PARENT_ID.hex(), 1)] = "ab" * 32
    confirm(_approval({AGENT.unit: 5}), MAINNET, chain, NOTHING_RECORDED)


@probe("input_spent")
def a_collateral_return_the_chain_lists(confirm):
    """Blockfrost lists a valid transaction's collateral return at the next
    index, although the ledger never created it."""
    confirm(_approval({AGENT.unit: 5}, [(PARENT_ID, 0), (PARENT_ID, 2)]), MAINNET, _chain(), NOTHING_RECORDED)


@probe("confirmed")
def outputs_of_a_surrender_this_signer_recorded(confirm):
    """A chained surrender spends its parent's outputs while the parent is
    still in the mempool; they need no chain answer."""
    approval = _approval({AGENT.unit: 5}, [(RECORDED_ID, 1), (PARENT_ID, 0)])
    confirm(approval, MAINNET, _chain(), RECORDED_SURRENDER)


@probe("unavailable")
def an_answer_without_the_spent_marker(confirm):
    class Truncated(FakeChain):
        def get_tx_utxos(self, tx_hash):
            answer = super().get_tx_utxos(tx_hash)
            for output in answer["outputs"]:
                del output["consumed_by_tx"]
            return answer

    confirm(_approval({AGENT.unit: 5}), MAINNET, Truncated({PARENT_ID.hex(): PARENT}), NOTHING_RECORDED)


@probe("supply_grown")
def a_grown_supply_before_any_input_is_looked_up(confirm):
    """Either refusal is final; the cheaper, broader one comes first."""
    grown = FakeChain(supply={AGENT.unit: PIN.supply[AGENT.unit] + 1})
    confirm(_approval({AGENT.unit: 5}), MAINNET, grown, NOTHING_RECORDED)


# -- NFT history -----------------------------------------------------------------

# What Blockfrost mainnet answered for three pinned passes; see the file's
# "source". Each probe below starts from these answers.
RECORDED = json.loads((ROOT / "tests/fixtures/blockfrost_nft_history.json").read_text())
T1_PASS, T2_PASS, FLUX_PASS_UNIT = (
    next(u for u in RECORDED["history"] if u.startswith(nft.policy_id))
    for nft in (T1_ADAM_PASS, T2_ADAM_PASS, FLUX_PASS)
)
BASE = chain_api_base_url("mainnet")
# Inputs of a surrender this signer recorded, so no input needs a chain answer.
RECORDED_INPUTS = ((RECORDED_ID, 0), (RECORDED_ID, 1))


def _change_hash(unit: str, action: str, slot: int) -> str:
    """The transaction hash :meth:`Blockfrost.event` gives a mint or burn."""
    return blake(f"{unit} {action} {slot}".encode()).hex()


class Blockfrost:
    """Blockfrost mainnet as a BlockfrostClient sees it: the recorded history
    (``pages[unit]``, one answer per page of 100) and transactions
    (``txs[hash]``), each unit's supply at the pin, and ``other`` paths.
    Anything else is a refused connection. An answer is a ``responses``
    keyword set: {"json": ...} or {"status": ...}."""

    def __init__(self) -> None:
        self.pages = {unit: [{"json": copy.deepcopy(events)}] for unit, events in RECORDED["history"].items()}
        self.txs = {tx_hash: {"json": copy.deepcopy(tx)} for tx_hash, tx in RECORDED["txs"].items()}
        self.other: dict[str, dict] = {}
        self.requests: list[str] = []
        """The URL of every request made in :meth:`confirm`."""

    def event(self, unit: str, action: str, slot: int) -> dict:
        """A history entry for a new mint or burn of ``unit`` at ``slot``,
        with the /txs answer for its transaction."""
        tx_hash = _change_hash(unit, action, slot)
        template = next(iter(RECORDED["txs"].values()))
        self.txs[tx_hash] = {"json": {**template, "hash": tx_hash, "slot": slot}}
        return {"tx_hash": tx_hash, "action": action, "amount": "1" if action == "minted" else "-1"}

    def add(self, unit: str, action: str, slot: int) -> str:
        """Append a mint or burn of ``unit`` at ``slot`` to its last page,
        opening a new page once that one holds 100."""
        entry = self.event(unit, action, slot)
        self.pages[unit][-1]["json"].append(entry)
        if len(self.pages[unit][-1]["json"]) == 100:
            self.pages[unit].append({"json": []})
        return entry["tx_hash"]

    def full_first_page(self, unit: str) -> None:
        """Make ``unit``'s history a full page of 100 mints before the pin,
        followed by a second page."""
        self.pages[unit] = [{"json": []}]
        for n in range(100):
            self.add(unit, "minted", PIN.supply_slot - 1_000 - n)

    def confirm(self, confirm: Callable[..., None], units: dict[str, int]) -> None:
        with responses.RequestsMock(assert_all_requests_are_fired=False) as served, \
                mock.patch("tools.api_clients.time.sleep"):
            for unit in units:
                served.get(f"{BASE}/assets/{unit}", json={"asset": unit, "quantity": str(PIN.supply[unit])})
            for unit, pages in self.pages.items():
                for number, answer in enumerate(pages, 1):
                    query = {"count": "100", "page": str(number), "order": "asc"}
                    served.get(f"{BASE}/assets/{unit}/history", match=[query_param_matcher(query)], **answer)
            for tx_hash, answer in self.txs.items():
                served.get(f"{BASE}/txs/{tx_hash}", **answer)
            for path, answer in self.other.items():
                served.get(f"{BASE}{path}", **answer)
            chain = BlockfrostClient("recorded-mainnet", BASE)
            try:
                confirm(_approval(units, RECORDED_INPUTS), MAINNET, chain, RECORDED_SURRENDER)
            finally:
                self.requests = [call.request.url for call in served.calls]


def _recorded_mint(unit: str) -> str:
    return RECORDED["history"][unit][0]["tx_hash"]


@probe("confirmed")
def recorded_histories_that_end_before_the_pin(confirm):
    """A pass minted once, one minted twice (two editions), a CIP-68 user
    token, and a fungible, whose history is never asked for (it is not
    served here)."""
    Blockfrost().confirm(confirm, {T1_PASS: 1, T2_PASS: 1, FLUX_PASS_UNIT: 1, AGENT.unit: 5})


@probe("minted_after_pin", detail=T2_PASS)
def a_mint_after_the_pin(confirm):
    """The supply answer is still the pinned one, as a stale read would be."""
    chain = Blockfrost()
    chain.add(T2_PASS, "minted", PIN.supply_slot + 1)
    chain.confirm(confirm, {T2_PASS: 1})


@probe("minted_after_pin", detail=T1_PASS)
def a_burn_after_the_pin(confirm):
    chain = Blockfrost()
    chain.add(T1_PASS, "burned", PIN.supply_slot + 1)
    chain.confirm(confirm, {T1_PASS: 1})


@probe("minted_after_pin", detail=FLUX_PASS_UNIT)
def a_change_after_the_pin_beside_clean_units(confirm):
    chain = Blockfrost()
    chain.add(FLUX_PASS_UNIT, "minted", PIN.supply_slot + 1)
    chain.confirm(confirm, {T1_PASS: 1, FLUX_PASS_UNIT: 1, AGENT.unit: 5})


@probe("confirmed")
def a_change_in_the_pins_own_slot(confirm):
    """The pin's supplies are read at its slot, so a mint there is part of them."""
    chain = Blockfrost()
    chain.add(T1_PASS, "minted", PIN.supply_slot)
    chain.confirm(confirm, {T1_PASS: 1})


@probe("minted_after_pin", detail=T1_PASS)
def a_history_listed_out_of_chain_order(confirm):
    """Every entry is read, not only the last one listed."""
    chain = Blockfrost()
    chain.add(T1_PASS, "burned", PIN.supply_slot + 1)
    chain.pages[T1_PASS][0]["json"].reverse()
    chain.confirm(confirm, {T1_PASS: 1})


@probe("minted_after_pin", detail=T1_PASS)
def a_mint_on_the_second_page_of_history(confirm):
    chain = Blockfrost()
    chain.full_first_page(T1_PASS)
    chain.add(T1_PASS, "minted", PIN.supply_slot + 1)
    chain.confirm(confirm, {T1_PASS: 1})


@probe("minted_after_pin", detail=T1_PASS)
def entries_after_the_first_change_after_the_pin(confirm):
    """The first change after the pin is refused without reading on, so an
    entry after it that the chain view cannot place changes nothing."""
    chain = Blockfrost()
    chain.add(T1_PASS, "minted", PIN.supply_slot + 1)
    del chain.txs[chain.add(T1_PASS, "burned", PIN.supply_slot + 2)]
    chain.confirm(confirm, {T1_PASS: 1})


@probe("confirmed")
def a_full_first_page_and_an_empty_second(confirm):
    """An empty list is how a history ends. That the provider lists every
    entry before it is part of trusting the provider."""
    chain = Blockfrost()
    chain.full_first_page(T1_PASS)
    chain.confirm(confirm, {T1_PASS: 1})


@probe("unavailable", detail=T1_PASS)
def a_history_answered_with_server_errors(confirm):
    """Blockfrost retries a 5xx, then gives up."""
    chain = Blockfrost()
    chain.pages[T1_PASS] = [{"status": 500}]
    chain.confirm(confirm, {T1_PASS: 1})


@probe("unavailable", detail=T1_PASS)
def a_history_that_is_forbidden(confirm):
    chain = Blockfrost()
    chain.pages[T1_PASS] = [{"status": 403}]
    chain.confirm(confirm, {T1_PASS: 1})


@probe("unavailable", detail=T1_PASS)
def a_unit_without_a_history(confirm):
    chain = Blockfrost()
    chain.pages[T1_PASS] = [{"status": 404}]
    chain.confirm(confirm, {T1_PASS: 1})


@probe("unavailable", detail=T1_PASS)
def an_empty_history(confirm):
    """Every unit on chain was minted at least once."""
    chain = Blockfrost()
    chain.pages[T1_PASS] = [{"json": []}]
    chain.confirm(confirm, {T1_PASS: 1})


@probe("unavailable", detail=T1_PASS)
def a_second_page_answered_with_server_errors(confirm):
    chain = Blockfrost()
    chain.full_first_page(T1_PASS)
    chain.pages[T1_PASS][1] = {"status": 500}
    chain.confirm(confirm, {T1_PASS: 1})


def _a_second_page_answered_with(answer: dict) -> Callable:
    def run(confirm):
        chain = Blockfrost()
        chain.full_first_page(T1_PASS)
        chain.pages[T1_PASS][1] = answer
        chain.confirm(confirm, {T1_PASS: 1})
    return run


# Every page of a history is a list. Only an empty or short one ends it.
NOT_A_PAGE = {
    "null": {"body": "null", "content_type": "application/json"},
    "an_empty_object": {"json": {}},
    "an_empty_string": {"json": ""},
}
for _name, _answer in NOT_A_PAGE.items():
    PROBES.append(Probe(f"a_second_page_answered_with_{_name}", "unavailable",
                        _a_second_page_answered_with(_answer), T1_PASS))


@probe("unavailable", detail=_recorded_mint(T1_PASS))
def a_history_transaction_the_chain_view_does_not_know(confirm):
    chain = Blockfrost()
    chain.txs[_recorded_mint(T1_PASS)] = {"status": 404}
    chain.confirm(confirm, {T1_PASS: 1})


@probe("unavailable", detail=_recorded_mint(T1_PASS))
def a_history_transaction_answered_with_server_errors(confirm):
    chain = Blockfrost()
    chain.txs[_recorded_mint(T1_PASS)] = {"status": 502}
    chain.confirm(confirm, {T1_PASS: 1})


@probe("unavailable", detail=_recorded_mint(T1_PASS))
def a_history_transaction_without_a_slot(confirm):
    chain = Blockfrost()
    del chain.txs[_recorded_mint(T1_PASS)]["json"]["slot"]
    chain.confirm(confirm, {T1_PASS: 1})


def _a_mint_after_the_pin_placed_at(slot: object) -> Callable:
    def run(confirm):
        chain = Blockfrost()
        minted = chain.add(T1_PASS, "minted", PIN.supply_slot + 1)
        chain.txs[minted]["json"]["slot"] = slot
        chain.confirm(confirm, {T1_PASS: 1})
    return run


# A slot is a whole number. JSON true reads as 1 in Python and 1.0 compares
# as 1, so either, taken as a number, would place a mint after the pin
# before it.
for _name, _slot in (("true", True), ("a_float", 1.0)):
    PROBES.append(Probe(f"a_mint_after_the_pin_placed_at_{_name}", "unavailable",
                        _a_mint_after_the_pin_placed_at(_slot),
                        _change_hash(T1_PASS, "minted", PIN.supply_slot + 1)))


@probe("unavailable", detail=T1_PASS)
def a_history_entry_naming_no_transaction(confirm):
    chain = Blockfrost()
    del chain.pages[T1_PASS][0]["json"][0]["tx_hash"]
    chain.confirm(confirm, {T1_PASS: 1})


@probe("unavailable", detail=T1_PASS)
def a_history_entry_naming_a_path_after_its_hash(confirm):
    """The name goes into a request path. A mint after the pin named by its
    hash and then a path would have its slot read from another endpoint,
    here an old block, and pass as a mint before the pin."""
    chain = Blockfrost()
    minted = chain.add(T1_PASS, "minted", PIN.supply_slot + 1)
    chain.pages[T1_PASS][-1]["json"][-1]["tx_hash"] = f"{minted}/../../blocks/{'b1' * 32}"
    chain.other["/blocks/" + "b1" * 32] = {"json": {"hash": "b1" * 32, "slot": 1}}
    chain.confirm(confirm, {T1_PASS: 1})


@probe("minted_after_pin", detail=T1_UNIT)
def a_mint_that_lands_while_the_inputs_are_looked_up(confirm):
    """The supply answer, read first, still shows the pinned supply; the
    history, asked after the inputs, shows the mint."""
    class MintDuringLookups(FakeChain):
        def get_tx_utxos(self, tx_hash):
            self.change_after_pin(T1_UNIT, "minted")
            return super().get_tx_utxos(tx_hash)

    confirm(_approval({T1_UNIT: 1}), MAINNET, MintDuringLookups({PARENT_ID.hex(): PARENT}), NOTHING_RECORDED)


# -- the cooling-off --------------------------------------------------------------

NOW = PIN.supply_slot + 5_000_000
COOLING_OFF = 72 * 3600
OWNER_BASE = b"\x01" + CLAIMANT[1:] + b"\x07" * 28  # the claimant's key, with a stake part
SELLER = b"\x61" + b"\x05" * 28
BYRON_SELLER = "Ae2tdPwUPEZFRbyhz3cpfC2CumGzNkFBN2L42rcUc2yjQpEkxDbkPodpMAi"
T1 = bytes.fromhex(T1_UNIT[:56]), bytes.fromhex(T1_UNIT[56:])
AGENT_ID = bytes.fromhex(AGENT.policy_id), bytes.fromhex(AGENT.asset_name_hex)
OTHER = b"\x09" * 28, b"other"
SURRENDERED = {T1_UNIT: 1, AGENT.unit: 5}


def _holding(address: bytes, *assets: tuple[tuple[bytes, bytes], int], coin: int = 2_000_000) -> list:
    bundle: dict = {}
    for (policy, name), quantity in assets:
        bundle.setdefault(policy, {})[name] = quantity
    return [address, [coin, bundle] if bundle else coin]


class Provenance:
    """Transactions that moved units toward the claimant, each at a slot,
    on a chain view that knows exactly those transactions and slots."""

    def __init__(self) -> None:
        self.chain = FakeChain(slots={})

    def tx(self, outputs: list, inputs=(), collateral=(), reference=(), slot: int = 0) -> bytes:
        body = {0: [list(ref) for ref in inputs], 1: outputs, 2: 0}
        if collateral:
            body[13] = [list(ref) for ref in collateral]
        if reference:
            body[18] = [list(ref) for ref in reference]
        encoded = encode(body)
        self.chain.publish(encoded, slot=slot)
        return blake(encoded)

    @staticmethod
    def in_the_mempool(outputs: list, inputs) -> bytes:
        """A transaction this chain view has not seen yet."""
        return blake(encode({0: [list(ref) for ref in inputs], 1: outputs, 2: 0}))

    def seller(self, *assets, address: bytes = SELLER) -> tuple[bytes, int]:
        """An output at ``address`` holding ``assets`` long before the window."""
        return self.tx([_holding(address, *assets)]), 0

    def confirm(self, confirm, held: tuple[bytes, int], claimant: bytes = CLAIMANT,
                units=SURRENDERED, recorded=NOTHING_RECORDED) -> None:
        approval = _approval(units, [held], unit_inputs=[held], claimant=claimant, checked_at=NOW)
        confirm(approval, MAINNET, self.chain, recorded)


def _arrival(at_slot: int):
    def run(confirm):
        world = Provenance()
        bought = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))],
                          inputs=[world.seller((T1, 1), (AGENT_ID, 5))], slot=at_slot)
        world.confirm(confirm, (bought, 0))
    return run


PROBES.append(Probe("units_that_arrived_as_the_cooling_off_began", "confirmed", _arrival(NOW - COOLING_OFF)))
PROBES.append(Probe("units_that_arrived_one_slot_inside_the_cooling_off", "cooling_off",
                    _arrival(NOW - COOLING_OFF + 1), f"from slot {NOW + 1}"))


@probe("confirmed")
def units_moved_between_the_claimants_own_addresses_after_arriving_long_before(confirm):
    world = Provenance()
    bought = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller((T1, 1), (AGENT_ID, 5))])
    moved = world.tx([_holding(OWNER_BASE, (T1, 1), (AGENT_ID, 5))], inputs=[(bought, 0)], slot=NOW - 10)
    again = world.tx([_holding(OWNER_BASE, (T1, 1), (AGENT_ID, 5))], inputs=[(moved, 0)], slot=NOW - 5)
    world.confirm(confirm, (again, 0), claimant=OWNER_BASE)


@probe("cooling_off")
def units_moved_by_the_claimant_after_arriving_inside_the_window(confirm):
    world = Provenance()
    bought = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))],
                      inputs=[world.seller((T1, 1), (AGENT_ID, 5))], slot=NOW - 3600)
    moved = world.tx([_holding(OWNER_BASE, (T1, 1), (AGENT_ID, 5))], inputs=[(bought, 0)], slot=NOW - 10)
    world.confirm(confirm, (moved, 0), claimant=OWNER_BASE)


@probe("cooling_off")
def a_self_transfer_that_merges_in_a_surrendered_unit_from_another_key(confirm):
    world = Provenance()
    held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 3))], inputs=[world.seller((T1, 1), (AGENT_ID, 3))])
    merged = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))],
                      inputs=[(held, 0), world.seller((AGENT_ID, 2))], slot=NOW - 10)
    world.confirm(confirm, (merged, 0))


@probe("confirmed")
def a_self_transfer_funded_with_ada_from_another_key(confirm):
    """Only inputs that carry a surrendered unit are followed."""
    world = Provenance()
    held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller((T1, 1), (AGENT_ID, 5))])
    moved = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))],
                     inputs=[(held, 0), world.seller()], slot=NOW - 10)
    world.confirm(confirm, (moved, 0))


@probe("confirmed")
def a_unit_received_inside_the_window_that_is_not_surrendered(confirm):
    world = Provenance()
    held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller((T1, 1), (AGENT_ID, 5))])
    merged = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5), (OTHER, 1))],
                      inputs=[(held, 0), world.seller((OTHER, 1))], slot=NOW - 10)
    world.confirm(confirm, (merged, 0))


@probe("cooling_off")
def units_minted_inside_the_window(confirm):
    world = Provenance()
    minted = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller()], slot=NOW - 10)
    world.confirm(confirm, (minted, 0))


@probe("confirmed")
def a_seller_who_posted_collateral_beside_the_claimants_inputs(confirm):
    """A valid transaction consumes none of its collateral."""
    world = Provenance()
    held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller((T1, 1), (AGENT_ID, 5))])
    moved = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[(held, 0)],
                     collateral=[world.seller((AGENT_ID, 5))], slot=NOW - 10)
    world.confirm(confirm, (moved, 0))


@probe("confirmed")
def a_reference_input_holding_the_units_elsewhere(confirm):
    world = Provenance()
    held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller((T1, 1), (AGENT_ID, 5))])
    moved = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[(held, 0)],
                     reference=[world.seller((T1, 1), (AGENT_ID, 5))], slot=NOW - 10)
    world.confirm(confirm, (moved, 0))


# -- change of a surrender this signer recorded -----------------------------------

QUARANTINE = b"\x71" + b"\x0c" * 28
POOL = b"\x71" + b"\x0d" * 28


def _surrender_of(units: tuple, change: tuple) -> list:
    """A surrender's outputs, as surrender_api lays them out: the payout, the
    pool continuation, ``units`` to quarantine, then ``change`` back to the
    claimant (output 3)."""
    return [_holding(CLAIMANT), _holding(POOL), _holding(QUARANTINE, *units), _holding(CLAIMANT, *change)]


@probe("cooling_off", detail=AGENT.unit)
def units_in_the_change_of_a_surrender_this_signer_recorded(confirm):
    """Its change is followed back through the inputs it recorded: units that
    reached those from another key inside the window still wait."""
    world = Provenance()
    bought = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 9))],
                      inputs=[world.seller((T1, 1), (AGENT_ID, 9))], slot=NOW - 3600)
    parent = world.in_the_mempool(_surrender_of(((AGENT_ID, 4),), ((T1, 1), (AGENT_ID, 5))), [(bought, 0)])
    world.confirm(confirm, (parent, 3), recorded={parent: ((bought, 0),)}.get)


@probe("confirmed")
def units_in_the_change_of_a_recorded_surrender_still_in_the_mempool(confirm):
    """A chained surrender spends its parent's change before the chain has
    seen the parent; units the parent's inputs held long before pass."""
    world = Provenance()
    held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 9))], inputs=[world.seller((T1, 1), (AGENT_ID, 9))])
    parent = world.in_the_mempool(_surrender_of(((AGENT_ID, 4),), ((T1, 1), (AGENT_ID, 5))), [(held, 0)])
    world.confirm(confirm, (parent, 3), recorded={parent: ((held, 0),)}.get)


@probe("confirmed")
def units_in_the_change_of_two_chained_surrenders_still_in_the_mempool(confirm):
    world = Provenance()
    held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 9))], inputs=[world.seller((T1, 1), (AGENT_ID, 9))])
    first = world.in_the_mempool(_surrender_of(((AGENT_ID, 2),), ((T1, 1), (AGENT_ID, 7))), [(held, 0)])
    second = world.in_the_mempool(_surrender_of(((AGENT_ID, 2),), ((T1, 1), (AGENT_ID, 5))), [(first, 3)])
    world.confirm(confirm, (second, 3), recorded={first: ((held, 0),), second: ((first, 3),)}.get)


@probe("confirmed")
def units_in_the_change_of_a_recorded_surrender_on_chain_before_the_window(confirm):
    """That change has been the claimant's since before the window, so the
    surrender's inputs, which this chain view does not know, are not asked
    about."""
    world = Provenance()
    parent = world.tx(_surrender_of(((AGENT_ID, 4),), ((T1, 1), (AGENT_ID, 5))),
                      inputs=[(INVENTED, 0)], slot=NOW - COOLING_OFF)
    world.confirm(confirm, (parent, 3), recorded={parent: ((INVENTED, 0),)}.get)


def _ride_through(merged_first: bool, parent_on_chain: bool) -> Callable:
    """A pass from another key reaches the claimant minutes before a surrender
    of AGENT it has held long before. That surrender spends the pass beside
    the AGENT, unsurrendered (in one output if ``merged_first``), and returns
    it as change; this signer checks and records it. A minute later the pass
    is surrendered from that change."""
    def run(confirm):
        world = Provenance()
        aged = world.tx([_holding(CLAIMANT, (AGENT_ID, 5))], inputs=[world.seller((AGENT_ID, 5))])
        stolen = world.tx([_holding(CLAIMANT, (T1, 1))], inputs=[world.seller((T1, 1))], slot=NOW - 900)
        spends = [(aged, 0), (stolen, 0)]
        if merged_first:
            spends = [(world.tx([_holding(CLAIMANT, (AGENT_ID, 5), (T1, 1))], inputs=spends, slot=NOW - 600), 0)]
        pool = (world.tx([_holding(POOL)]), 0)
        outputs = _surrender_of(((AGENT_ID, 5),), ((T1, 1),))
        parent = world.tx(outputs, inputs=[pool, *spends], slot=NOW - 60) if parent_on_chain else (
            world.in_the_mempool(outputs, [pool, *spends]))
        first = Approval(
            tx_hash=parent, payout=1, claimant=CLAIMANT, pool_input=pool, units={AGENT.unit: 5},
            inputs=tuple(sorted([pool, *spends])), spends=tuple(sorted([pool, *spends])),
            unit_inputs=(spends[0],), checked_at=NOW - 120,
        )
        confirm(first, MAINNET, world.chain, NOTHING_RECORDED)
        world.confirm(confirm, (parent, 3), units={T1_UNIT: 1}, recorded={parent: tuple(spends)}.get)
    return run

for _merged, _on_chain in ((False, True), (True, False)):
    PROBES.append(Probe(
        f"a_pass_that_rode_through_a_recorded_surrender{'_merged_into_its_agent' if _merged else ''}"
        f"_{'on_chain' if _on_chain else 'in_the_mempool'}",
        "cooling_off", _ride_through(_merged, _on_chain), T1_UNIT,
    ))


@probe("cooling_off")
def units_from_a_byron_address_inside_the_window(confirm):
    class ByronSeller(FakeChain):
        def get_tx_utxos(self, tx_hash):
            answer = super().get_tx_utxos(tx_hash)
            for entry in answer["inputs"]:
                entry["address"] = BYRON_SELLER
            return answer

    world = Provenance()
    world.chain.__class__ = ByronSeller
    bought = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))],
                      inputs=[world.seller((T1, 1), (AGENT_ID, 5))], slot=NOW - 10)
    world.confirm(confirm, (bought, 0))


@probe("cooling_off", detail="transactions")
def a_walk_through_more_self_transfers_than_the_limit(confirm):
    world = Provenance()
    held = (world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller((T1, 1), (AGENT_ID, 5))]), 0)
    for n in range(33):
        held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[held], slot=NOW - 100 + n), 0
    world.confirm(confirm, held)


@probe("confirmed")
def a_walk_through_as_many_self_transfers_as_the_limit(confirm):
    world = Provenance()
    held = (world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller((T1, 1), (AGENT_ID, 5))]), 0)
    for n in range(32):
        held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[held], slot=NOW - 100 + n), 0
    world.confirm(confirm, held)


@probe("unavailable")
def a_producing_transaction_the_chain_view_cannot_place(confirm):
    world = Provenance()
    bought = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller((T1, 1), (AGENT_ID, 5))])
    del world.chain.slots[bought.hex()]
    world.confirm(confirm, (bought, 0))


INJECTED = "/../../blocks/1"


def _a_listing_with(edit: Callable[[dict], None]) -> Callable:
    """A self-transfer inside the window of units the claimant held long
    before, whose listing ``edit`` changes. A path appended to a hash names
    a transaction the chain view places long before, so following it
    unchecked would pass."""
    def run(confirm):
        class Edited(FakeChain):
            def get_tx_utxos(self, tx_hash):
                answer = super().get_tx_utxos(tx_hash)
                edit(answer)
                return answer

        world = Provenance()
        world.chain.__class__ = Edited
        held = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[world.seller((T1, 1), (AGENT_ID, 5))])
        moved = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[(held, 0)], slot=NOW - 10)
        world.chain.slots[held.hex() + INJECTED] = 0
        world.confirm(confirm, (moved, 0))
    return run


MALFORMED_LISTINGS = {
    "an_input_without_an_address": lambda a: a["inputs"][0].pop("address"),
    "an_input_without_a_collateral_flag": lambda a: a["inputs"][0].pop("collateral"),
    "an_input_without_a_reference_flag": lambda a: a["inputs"][0].pop("reference"),
    "an_input_amount_that_is_not_a_list": lambda a: a["inputs"][0].update(amount=None),
    "an_input_quantity_that_is_not_a_number": lambda a: a["inputs"][0]["amount"][1].update(quantity="x"),
    "an_input_naming_a_path_after_its_hash": lambda a: a["inputs"][0].update(
        tx_hash=a["inputs"][0]["tx_hash"] + INJECTED),
    "an_input_index_that_is_text": lambda a: a["inputs"][0].update(output_index="0"),
    "an_input_index_that_is_true": lambda a: a["inputs"][0].update(output_index=True),
}
PROBES.append(Probe("a_well_formed_listing", "confirmed", _a_listing_with(lambda answer: None)))
for _name, _edit in MALFORMED_LISTINGS.items():
    PROBES.append(Probe(f"a_listing_with_{_name}", "unavailable", _a_listing_with(_edit)))


@probe("unavailable")
def a_source_whose_listing_lacks_the_output_followed(confirm):
    """The units came from an output the listing of the transaction that made
    it does not show: the chain view contradicts itself."""
    class Lacking(FakeChain):
        def get_tx_utxos(self, tx_hash):
            answer = super().get_tx_utxos(tx_hash)
            if tx_hash == bought.hex():
                answer["outputs"].pop(0)
            return answer

    world = Provenance()
    world.chain.__class__ = Lacking
    bought = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))],
                      inputs=[world.seller((T1, 1), (AGENT_ID, 5))], slot=NOW - 3600)
    moved = world.tx([_holding(CLAIMANT, (T1, 1), (AGENT_ID, 5))], inputs=[(bought, 0)], slot=NOW - 10)
    world.confirm(confirm, (moved, 0))


# -- the probes ------------------------------------------------------------------


def test_probes_have_distinct_names():
    names = [p.name for p in PROBES]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("p", PROBES, ids=[p.name for p in PROBES])
def test_probe(p):
    got, message = outcome(p, confirm_on_chain)
    assert got == p.expected, message
    assert p.detail in message


def test_a_history_inflated_after_the_pin_costs_no_lookup_past_its_first_change():
    """Mints and burns after the pin, each offsetting the last, leave the
    supply as pinned. The signer looks up each entry's transaction only up to
    the first of them, however many follow."""
    chain = Blockfrost()
    before_the_pin = len(RECORDED["history"][T1_PASS])
    for n in range(1_000):
        chain.add(T1_PASS, ("minted", "burned")[n % 2], PIN.supply_slot + 1 + n)
    with pytest.raises(CosignRejected) as refused:
        chain.confirm(confirm_on_chain, {T1_PASS: 1})
    assert refused.value.code == "minted_after_pin"
    lookups = [url for url in chain.requests if "/txs/" in url]
    history_pages = [url for url in chain.requests if "/history" in url]
    assert len(lookups) == before_the_pin + 1
    assert len(history_pages) == len(chain.pages[T1_PASS])
    assert len(chain.requests) == 1 + len(history_pages) + len(lookups)
