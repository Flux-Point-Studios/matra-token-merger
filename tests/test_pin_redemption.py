"""scripts.pin_redemption --check compares the chain tip with a committed
pin. A unit minted past its pinned supply, an NFT minted or burned after the
pin's supply slot, a redeemable name the pin does not list, or quarantine
holdings that moved since the pin each mean the pin no longer describes what
may still be redeemed. Re-pinning at the pin's own supply slot cannot show a
later mint, so the check reads current supply and each NFT's history.

A pin may record waived units already in quarantine so they are not
subtracted twice. The check confirms each record with the chain: the
transaction it names sent at least that many to quarantine, by the slot the
pin's quarantine count was read at, and the waived reserve no longer holds
them, so the pin never promises more of a unit than exists outside that
reserve and quarantine. Pinning writes a record only on the same terms."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import pytest
from pycardano import Address

import scripts.pin_redemption as pin_redemption
from scripts.pin_redemption import QUARANTINE_ADDRESS, check, drift
from services.cosign_policy import decode, parse_output, split_tx
from tests.cosign_cases import (
    PIN,
    SURRENDER_HASHES,
    FakeChain,
    golden,
    golden_scenario,
    http_error,
    not_found,
    sent_to_quarantine,
)
from tools.config import AGENT, FLUX_PASS, LEGACY_TOKENS, T1_ADAM_PASS

ROOT = Path(__file__).resolve().parent.parent
PIN_PATH = ROOT / "audit_pack/2026-09-27/redemption_pin.json"
# The rate table's waived reserves: per token, each address and what it held
# at the snapshot the waiver was read from.
RESERVES = json.loads(
    (ROOT / "audit_pack/2026-04-19/allocations_cmatra_summary.json").read_text()
)["reserve"]["team_treasury"]
AGENT_RESERVE, AGENT_RESERVE_2 = (entry["address"] for entry in RESERVES["AGENT"]["addresses"])


def _doc() -> dict:
    return json.loads(PIN_PATH.read_text())


def _tx_answers(tx_hash: str) -> tuple[dict, dict]:
    """Blockfrost's /txs/{hash} and /txs/{hash}/utxos for golden transaction
    ``tx_hash``: its slot, scripts passed, and each output's address and
    amounts, the collateral return last and marked as such."""
    body = decode(split_tx(golden_scenario(tx_hash).tx)[0])
    outputs = []
    for index, raw in enumerate([*body[1], body[16]]):
        out = parse_output(raw)
        outputs.append({
            "output_index": index,
            "address": Address.from_primitive(out.address).encode(),
            "amount": [{"unit": "lovelace", "quantity": str(out.coin)}, *(
                {"unit": (policy + name).hex(), "quantity": str(quantity)}
                for policy, names in out.assets.items() for name, quantity in names.items()
            )],
            "collateral": index == len(body[1]),
        })
    (row,) = [t for t in golden()["transactions"] if t["tx_hash"] == tx_hash]
    return ({"hash": tx_hash, "slot": row["slot"], "valid_contract": True},
            {"hash": tx_hash, "inputs": [], "outputs": outputs})


class TipView:
    """Blockfrost at the tip: exactly what the pin describes, every waived
    reserve holding what it held at the snapshot, until a test changes
    ``supply`` (policy -> asset name -> quantity), ``held``, ``reserve``
    (address -> unit -> quantity), a unit's history in ``chain``, or a
    transaction in ``txs`` or ``utxos``."""

    def __init__(self, doc: dict) -> None:
        self.chain = FakeChain()
        self.supply = {
            entry["policy_id"]: {name: row["supply"] for name, row in entry["units"].items()}
            for entry in doc["assets"].values()
        }
        self.held = {
            entry["policy_id"] + name: row["quarantined"]
            for entry in doc["assets"].values()
            for name, row in entry["units"].items() if row["quarantined"]
        }
        units = {token.name: token.unit for token in LEGACY_TOKENS}
        self.reserve: dict[str, dict[str, int]] = {}
        for token, reserve in RESERVES.items():
            for entry in reserve["addresses"]:
                self.reserve.setdefault(entry["address"], {})[units[token]] = entry["balance_base"]
        self.txs: dict[str, dict] = {}
        self.utxos: dict[str, dict] = {}
        self.quarantine_slot = doc["quarantine_slot"]

    def get_policy_assets(self, policy_id: str) -> list[dict]:
        return [{"asset": policy_id + name, "quantity": str(quantity)}
                for name, quantity in self.supply[policy_id].items()]

    def get_address_utxos(self, address: str, asset: str | None = None) -> list[dict]:
        """Filtered by ``asset``, Blockfrost lists every UTxO holding it,
        each with all its amounts."""
        if address == QUARANTINE_ADDRESS:
            assert asset is None
            amounts = [{"unit": unit, "quantity": str(q)} for unit, q in self.held.items()]
            return [{"amount": [{"unit": "lovelace", "quantity": "9000000"}, *amounts]}]
        held = self.reserve[address]
        if not held.get(asset):
            return []
        amounts = [{"unit": unit, "quantity": str(q)} for unit, q in held.items() if q]
        return [{"amount": [{"unit": "lovelace", "quantity": "2000000"}, *amounts]}]

    def get_asset_history(self, unit: str) -> list[dict]:
        return self.chain.get_asset_history(unit)

    def get_tx(self, tx_hash: str) -> dict:
        if tx_hash in self.txs:
            return self.txs[tx_hash]
        return self.chain.get_tx(tx_hash)

    def get_tx_utxos(self, tx_hash: str) -> dict:
        if tx_hash not in self.utxos:
            raise not_found()
        return self.utxos[tx_hash]

    def get_latest_block(self) -> dict:
        return {"slot": self.quarantine_slot}


T1_NAME = next(iter(_doc()["assets"]["T1_ADAM_PASS"]["units"]))


def test_the_committed_pin_holds_at_an_unchanged_tip():
    assert drift(_doc(), TipView(_doc())) == []


def test_an_edition_minted_after_the_pin_is_drift():
    tip = TipView(_doc())
    tip.supply[T1_ADAM_PASS.policy_id][T1_NAME] = 2
    (problem,) = drift(_doc(), tip)
    assert T1_ADAM_PASS.policy_id + T1_NAME in problem
    assert "supply 2" in problem


def test_a_redeemable_name_minted_after_the_pin_is_drift():
    tip = TipView(_doc())
    tip.supply[T1_ADAM_PASS.policy_id]["54314144414d393939"] = 1
    (problem,) = drift(_doc(), tip)
    assert T1_ADAM_PASS.policy_id + "54314144414d393939" in problem


def test_a_reference_token_is_not_a_redeemable_name():
    tip = TipView(_doc())
    tip.supply[FLUX_PASS.policy_id]["000643b0aa"] = 1
    assert drift(_doc(), tip) == []


def test_fungible_supply_minted_after_the_pin_is_drift():
    tip = TipView(_doc())
    tip.supply[AGENT.policy_id][AGENT.asset_name_hex] += 1
    (problem,) = drift(_doc(), tip)
    assert AGENT.unit in problem


def test_a_burn_below_the_pin_is_not_drift():
    tip = TipView(_doc())
    tip.supply[AGENT.policy_id][AGENT.asset_name_hex] -= 1
    assert drift(_doc(), tip) == []


@pytest.mark.parametrize("action", ["minted", "burned"])
def test_an_nft_minted_or_burned_after_the_pin_is_drift(action):
    """Its supply is still the pinned one; its history shows the change,
    which every signer refuses."""
    tip = TipView(_doc())
    tip.chain.change_after_pin(T1_ADAM_PASS.policy_id + T1_NAME, action)
    (problem,) = drift(_doc(), tip)
    assert T1_ADAM_PASS.policy_id + T1_NAME in problem
    assert f"{action} in " in problem
    assert f"slot {PIN.supply_slot + 1}" in problem


def test_every_change_after_the_pin_is_drift():
    """A signer stops at the first change after the pin; the check lists
    them all."""
    tip = TipView(_doc())
    for action in ("minted", "burned"):
        tip.chain.change_after_pin(T1_ADAM_PASS.policy_id + T1_NAME, action)
    minted, burned = drift(_doc(), tip)
    assert "minted in " in minted and "burned in " in burned


def test_an_nft_minted_in_the_pins_own_slot_is_not_drift():
    """The pin's supplies are read at its slot, so a mint there is part of them."""
    tip = TipView(_doc())
    unit = T1_ADAM_PASS.policy_id + T1_NAME
    tip.chain.slots["5f" * 32] = PIN.supply_slot
    tip.chain.history[unit] = [
        *tip.chain.get_asset_history(unit), {"tx_hash": "5f" * 32, "action": "minted", "amount": "1"},
    ]
    assert drift(_doc(), tip) == []


def test_a_fungible_burned_after_the_pin_is_not_drift():
    """No signer reads a fungible's history: its supply check covers a mint."""
    tip = TipView(_doc())
    tip.chain.change_after_pin(AGENT.unit, "burned")
    assert drift(_doc(), tip) == []


def test_quarantine_that_moved_since_the_pin_is_drift():
    tip = TipView(_doc())
    tip.held[T1_ADAM_PASS.policy_id + T1_NAME] = tip.held.get(T1_ADAM_PASS.policy_id + T1_NAME, 0) + 1
    (problem,) = drift(_doc(), tip)
    assert "quarantine" in problem


@pytest.mark.parametrize("changed,code", [(False, 0), (True, 1)])
def test_check_reports_and_exits_nonzero_on_drift(capsys, changed, code):
    tip = TipView(_doc())
    if changed:
        tip.supply[T1_ADAM_PASS.policy_id][T1_NAME] = 2
    assert check(PIN_PATH, tip) == code
    out = capsys.readouterr().out
    assert ("PIN NO LONGER HOLDS" in out) == changed
    assert ("pin holds" in out) != changed
    assert ("no NFT minted or burned since the pin" in out) != changed
    assert ("every record of waived units in quarantine borne out by its transaction"
            " and its waived reserve" in out) != changed


# --- waived units already in quarantine ---------------------------------------

RECORDED = 1_000_000


@lru_cache(maxsize=None)
def _sample_surrender(quarantine_slot: int) -> str:
    """A mainnet surrender that sent at least RECORDED AGENT to quarantine by
    ``quarantine_slot``, to name in a record."""
    slots = {t["tx_hash"]: t["slot"] for t in golden()["transactions"]}
    return next(
        tx_hash for tx_hash in SURRENDER_HASHES
        if slots[tx_hash] <= quarantine_slot and sent_to_quarantine(tx_hash).get(AGENT.unit, 0) >= RECORDED
    )


def _agent(doc: dict) -> dict:
    return doc["assets"]["AGENT"]["units"][AGENT.asset_name_hex]


def _answer_for(tip: TipView, tx_hash: str) -> None:
    tip.txs[tx_hash], tip.utxos[tx_hash] = _tx_answers(tx_hash)


def _record_on(doc: dict, tip: TipView) -> None:
    """``doc`` records RECORDED waived AGENT in quarantine, sent there by a
    mainnet surrender, and ``tip`` has them gone from the waived reserve."""
    tx_hash = _sample_surrender(doc["quarantine_slot"])
    _agent(doc)["waived_already_quarantined"] = {"quantity": RECORDED, "tx_hash": tx_hash}
    _answer_for(tip, tx_hash)
    tip.reserve[AGENT_RESERVE][AGENT.unit] -= RECORDED


def _recorded_tx(doc: dict) -> str:
    return _agent(doc)["waived_already_quarantined"]["tx_hash"]


def _quarantine_output(tip: TipView, tx_hash: str) -> dict:
    (out,) = [o for o in tip.utxos[tx_hash]["outputs"] if o["address"] == QUARANTINE_ADDRESS]
    return out


def _agent_amount(out: dict) -> dict:
    (amount,) = [a for a in out["amount"] if a["unit"] == AGENT.unit]
    return amount


@dataclass(frozen=True)
class RecordProbe:
    name: str
    change: Callable[[dict, TipView], None]
    """Edits a pin that records RECORDED waived AGENT in quarantine, and the
    tip it is checked against."""
    expected: tuple[str, ...]
    """A fragment of each difference the check lists, in order; none while
    the pin holds, or "crash:<exception>" when the check stops."""


RECORD_PROBES: list[RecordProbe] = []


def record_probe(*expected: str):
    def register(change: Callable[[dict, TipView], None]):
        RECORD_PROBES.append(RecordProbe(change.__name__, change, expected))
        return change
    return register


def record_outcome(p: RecordProbe) -> list[str]:
    """What ``drift``, looked up on the module, lists for ``p``."""
    doc = _doc()
    tip = TipView(doc)
    _record_on(doc, tip)
    p.change(doc, tip)
    try:
        return pin_redemption.drift(doc, tip)
    except Exception as exc:
        return [f"crash:{type(exc).__name__}"]


def matches(problems: list[str], expected: tuple[str, ...]) -> bool:
    return len(problems) == len(expected) and all(f in p for f, p in zip(expected, problems))


@record_probe()
def the_recorded_transaction_as_it_is_on_chain(doc, tip):
    pass


@record_probe()
def no_record_while_the_waived_reserve_holds_more_than_the_waiver(doc, tip):
    """Without a record the reserve is not read: sending it units cannot
    stop the check."""
    del _agent(doc)["waived_already_quarantined"]
    tip.reserve[AGENT_RESERVE][AGENT.unit] += RECORDED + 1


@record_probe("still holds")
def the_waived_reserve_still_holding_every_waived_unit(doc, tip):
    tip.reserve[AGENT_RESERVE][AGENT.unit] += RECORDED


@record_probe("still holds")
def the_second_waived_reserve_holding_one_unit_more_than_the_record_leaves(doc, tip):
    tip.reserve[AGENT_RESERVE_2][AGENT.unit] += 1


@record_probe()
def the_waived_reserve_holding_less_than_the_record_leaves(doc, tip):
    tip.reserve[AGENT_RESERVE][AGENT.unit] -= 1


@record_probe("at the snapshot, not the waiver")
def a_waiver_the_waived_reserve_did_not_hold(doc, tip):
    _agent(doc)["waiver"] += 1


@record_probe("crash:HTTPError")
def a_chain_view_that_refuses_the_waived_reserve(doc, tip):
    answer = tip.get_address_utxos

    def get_address_utxos(address: str, asset: str | None = None) -> list[dict]:
        if address == AGENT_RESERVE:
            raise http_error(403, "Forbidden")
        return answer(address, asset)
    tip.get_address_utxos = get_address_utxos


@record_probe("created no outputs on chain")
def a_recorded_transaction_the_chain_does_not_know(doc, tip):
    del tip.txs[_recorded_tx(doc)], tip.utxos[_recorded_tx(doc)]


@record_probe("created no outputs on chain")
def a_recorded_transaction_whose_scripts_failed(doc, tip):
    tip.txs[_recorded_tx(doc)]["valid_contract"] = False


@record_probe("fewer than the")
def a_recorded_transaction_that_quarantined_one_unit_fewer(doc, tip):
    _agent_amount(_quarantine_output(tip, _recorded_tx(doc)))["quantity"] = str(RECORDED - 1)


@record_probe()
def a_recorded_transaction_that_quarantined_exactly_the_record(doc, tip):
    _agent_amount(_quarantine_output(tip, _recorded_tx(doc)))["quantity"] = str(RECORDED)


@record_probe()
def the_recorded_units_split_across_two_quarantine_outputs(doc, tip):
    out = _quarantine_output(tip, _recorded_tx(doc))
    _agent_amount(out)["quantity"] = str(RECORDED - RECORDED // 2)
    tip.utxos[_recorded_tx(doc)]["outputs"].append({
        "output_index": 99, "address": QUARANTINE_ADDRESS, "collateral": False,
        "amount": [{"unit": AGENT.unit, "quantity": str(RECORDED // 2)}],
    })


@record_probe("fewer than the")
def the_recorded_units_in_a_collateral_return(doc, tip):
    """A valid transaction lists its collateral return but never creates it."""
    _quarantine_output(tip, _recorded_tx(doc))["collateral"] = True


@record_probe("fewer than the")
def the_recorded_units_sent_to_another_address(doc, tip):
    _quarantine_output(tip, _recorded_tx(doc))["address"] = "addr1" + "q" * 98


@record_probe("fewer than the")
def the_recorded_units_as_another_unit(doc, tip):
    _agent_amount(_quarantine_output(tip, _recorded_tx(doc)))["unit"] = AGENT.policy_id + "00"


@record_probe("after the quarantine count")
def a_recorded_transaction_after_the_quarantine_count(doc, tip):
    tip.txs[_recorded_tx(doc)]["slot"] = doc["quarantine_slot"] + 1


@record_probe()
def a_recorded_transaction_in_the_quarantine_counts_own_slot(doc, tip):
    tip.txs[_recorded_tx(doc)]["slot"] = doc["quarantine_slot"]


@record_probe("more than the waiver")
def a_record_above_the_waiver(doc, tip):
    _agent(doc)["waived_already_quarantined"]["quantity"] = _agent(doc)["waiver"] + 1


@record_probe("more than the quarantined")
def a_record_above_what_quarantine_holds(doc, tip):
    _agent(doc)["quarantined"] = tip.held[AGENT.unit] = RECORDED - 1


@record_probe("waived_already_quarantined must")
def a_record_naming_a_path_instead_of_a_transaction(doc, tip):
    """Refused before it reaches a request path."""
    _agent(doc)["waived_already_quarantined"]["tx_hash"] = "../blocks/latest"


@record_probe("crash:HTTPError")
def a_chain_view_that_refuses_the_lookup(doc, tip):
    """Only a 404 means no such transaction; any other error stops the check."""
    recorded, answer = _recorded_tx(doc), tip.get_tx

    def get_tx(tx_hash: str) -> dict:
        if tx_hash == recorded:
            raise http_error(403, "Forbidden")
        return answer(tx_hash)
    tip.get_tx = get_tx


@pytest.mark.parametrize("p", RECORD_PROBES, ids=lambda p: p.name)
def test_the_check_confirms_the_record_with_the_chain(p):
    problems = record_outcome(p)
    assert matches(problems, p.expected), problems


def test_the_committed_pin_asks_for_no_transaction_and_no_waived_reserve():
    """It records no waived units in quarantine."""
    tip = TipView(_doc())
    asked = []
    quarantine = tip.get_address_utxos

    def get_address_utxos(address: str, asset: str | None = None) -> list[dict]:
        asked.append(address)
        return quarantine(address, asset)
    tip.get_address_utxos = get_address_utxos
    tip.get_tx_utxos = asked.append
    assert drift(_doc(), tip) == []
    assert asked == [QUARANTINE_ADDRESS]


def test_the_check_reads_the_recorded_transaction_and_every_waived_reserve():
    doc = _doc()
    tip = TipView(doc)
    _record_on(doc, tip)
    asked = []
    answers = tip.get_address_utxos, tip.get_tx_utxos

    def get_address_utxos(address: str, asset: str | None = None) -> list[dict]:
        asked.append((address, asset))
        return answers[0](address, asset)

    def get_tx_utxos(tx_hash: str) -> dict:
        asked.append(tx_hash)
        return answers[1](tx_hash)
    tip.get_address_utxos, tip.get_tx_utxos = get_address_utxos, get_tx_utxos
    assert drift(doc, tip) == []
    assert asked == [
        (QUARANTINE_ADDRESS, None), _recorded_tx(doc),
        (AGENT_RESERVE, AGENT.unit), (AGENT_RESERVE_2, AGENT.unit),
    ]


def test_check_exits_nonzero_while_the_waived_reserve_still_holds_the_record(capsys, tmp_path):
    doc = _doc()
    tip = TipView(doc)
    _record_on(doc, tip)
    tip.reserve[AGENT_RESERVE][AGENT.unit] += RECORDED
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps(doc))
    assert check(pin, tip) == 1
    out = capsys.readouterr().out
    assert "still holds" in out and "PIN NO LONGER HOLDS" in out


def repin(
    tmp_path: Path, monkeypatch, change: Callable[[TipView], None] | None = None,
    records: dict[str, tuple[str, int]] | None = None,
) -> str:
    """Pin the chain the committed pin describes, after ``change``, at its
    supply slot, with ``records`` in place of WAIVED_ALREADY_QUARANTINED when
    given; return what was written, or "refused: <reason>" when nothing was."""
    doc = _doc()
    tip = TipView(doc)
    if change is not None:
        change(tip)
    if records is not None:
        monkeypatch.setattr(pin_redemption, "WAIVED_ALREADY_QUARANTINED", records)
    out = tmp_path / "repinned.json"
    monkeypatch.setattr(pin_redemption, "BlockfrostClient", lambda: tip)
    try:
        pin_redemption.main(doc["supply_slot"], out)
    except SystemExit as exc:
        assert not out.exists()
        return f"refused: {exc}"
    return out.read_text()


def sample_record() -> dict[str, tuple[str, int]]:
    return {"AGENT": (_sample_surrender(_doc()["quarantine_slot"]), RECORDED)}


def recorded_units_left_the_reserve(tip: TipView) -> None:
    _answer_for(tip, _sample_surrender(tip.quarantine_slot))
    tip.reserve[AGENT_RESERVE][AGENT.unit] -= RECORDED


def recorded_units_still_in_the_reserve(tip: TipView) -> None:
    _answer_for(tip, _sample_surrender(tip.quarantine_slot))


def with_the_record(doc: dict) -> str:
    _agent(doc)["waived_already_quarantined"] = {
        "quantity": RECORDED, "tx_hash": _sample_surrender(doc["quarantine_slot"]),
    }
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def test_repinning_an_unchanged_chain_writes_the_committed_pin(tmp_path, monkeypatch):
    assert repin(tmp_path, monkeypatch) == PIN_PATH.read_text()


def test_pinning_writes_a_record_the_chain_bears_out(tmp_path, monkeypatch):
    written = repin(tmp_path, monkeypatch, recorded_units_left_the_reserve, sample_record())
    assert written == with_the_record(_doc())


def test_pinning_refuses_a_record_while_the_waived_reserve_still_holds_it(tmp_path, monkeypatch):
    written = repin(tmp_path, monkeypatch, recorded_units_still_in_the_reserve, sample_record())
    assert written.startswith("refused: ") and "still holds" in written


def recorded_transaction_one_unit_short(tip: TipView) -> None:
    recorded_units_left_the_reserve(tip)
    out = _quarantine_output(tip, _sample_surrender(tip.quarantine_slot))
    _agent_amount(out)["quantity"] = str(RECORDED - 1)


def test_pinning_refuses_a_record_its_transaction_does_not_bear_out(tmp_path, monkeypatch):
    written = repin(tmp_path, monkeypatch, recorded_transaction_one_unit_short, sample_record())
    assert written.startswith("refused: ") and "fewer than the" in written
