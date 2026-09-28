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
from tests.cosign_cases import MAINNET, PIN, FakeChain, blake
from tools.api_clients import BlockfrostClient, BlockfrostUnavailable
from tools.config import AGENT, BLOCKFROST_BASE_URLS, FLUX_PASS, T1_ADAM_PASS, T2_ADAM_PASS

ROOT = Path(__file__).resolve().parent.parent
T1_UNIT = next(u for u in sorted(PIN.nft_units) if u.startswith(T1_ADAM_PASS.policy_id))

CLAIMANT = b"\x61" + b"\x02" * 28
# A producing body with two outputs, and a chain view that has seen it.
PARENT = encode({0: [], 1: [[CLAIMANT, 1_000_000], [CLAIMANT, 2_000_000]], 2: 0})
PARENT_ID = blake(PARENT)
RECORDED_ID = b"\x09" * 32
NOTHING_RECORDED = frozenset().__contains__


def _chain(**kwargs) -> FakeChain:
    return FakeChain({PARENT_ID.hex(): PARENT}, **kwargs)


def _approval(units: dict[str, int], inputs=((PARENT_ID, 0), (PARENT_ID, 1))) -> Approval:
    return Approval(
        tx_hash=b"\x01" * 32, payout=1, claimant=CLAIMANT,
        pool_input=inputs[0], units=units, inputs=tuple(inputs), spends=tuple(inputs),
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
        confirm(_approval({AGENT.unit: 5, T1_UNIT: 1}), MAINNET, grown, NOTHING_RECORDED)
    return run


for _name, _unit in (("nft", T1_UNIT), ("fungible", AGENT.unit)):
    PROBES.append(Probe(f"{_name}_minted_past_its_pinned_supply", "supply_grown",
                        _minted_past_its_pinned_supply(_unit), _unit))


@probe("unavailable", detail=T1_UNIT)
def a_unit_the_chain_view_does_not_know(confirm):
    chain = _chain()
    del chain.supply[T1_UNIT]
    confirm(_approval({T1_UNIT: 1}), MAINNET, chain, NOTHING_RECORDED)


def _forbidden() -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 403
    return requests.HTTPError("403 Forbidden", response=response)


FAILURES = {
    "connection_refused": requests.ConnectionError("connection refused"),
    "timeout": requests.Timeout("read timed out"),
    "retries_exhausted": BlockfrostUnavailable("Exhausted retries"),
    "forbidden": _forbidden(),
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
    confirm(approval, MAINNET, _chain(), {RECORDED_ID}.__contains__)


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
BASE = BLOCKFROST_BASE_URLS["mainnet"]
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
                confirm(_approval(units, RECORDED_INPUTS), MAINNET, chain, {RECORDED_ID}.__contains__)
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
