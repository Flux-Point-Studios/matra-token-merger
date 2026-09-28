"""scripts.pin_redemption --check compares the chain tip with a committed
pin. A unit minted past its pinned supply, an NFT minted or burned after the
pin's supply slot, a redeemable name the pin does not list, or quarantine
holdings that moved since the pin each mean the pin no longer describes what
may still be redeemed. Re-pinning at the pin's own supply slot cannot show a
later mint, so the check reads current supply and each NFT's history.

The pin records treasury units already in quarantine so they are not
subtracted twice. The check confirms each record with the chain: the
transaction it names sent at least that many to quarantine, by the slot the
pin's quarantine count was read at. Pinning writes the record and refuses to
write a pin the chain does not bear out."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from pycardano import Address

import scripts.pin_redemption as pin_redemption
from scripts.pin_redemption import QUARANTINE_ADDRESS, check, drift
from services.cosign_policy import decode, parse_output, split_tx
from tests.cosign_cases import PIN, FakeChain, golden, golden_scenario, http_error, not_found
from tools.config import AGENT, FLUX_PASS, T1_ADAM_PASS

ROOT = Path(__file__).resolve().parent.parent
PIN_PATH = ROOT / "audit_pack/2026-09-27/redemption_pin.json"


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
    """Blockfrost at the tip: exactly what the pin describes until a test
    changes ``supply`` (policy -> asset name -> quantity), ``held``, a
    unit's history in ``chain``, or a recorded transaction in ``txs`` or
    ``utxos``."""

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
        self.txs: dict[str, dict] = {}
        self.utxos: dict[str, dict] = {}
        for entry in doc["assets"].values():
            for row in entry["units"].values():
                if "waived_already_quarantined" in row:
                    tx_hash = row["waived_already_quarantined"]["tx_hash"]
                    self.txs[tx_hash], self.utxos[tx_hash] = _tx_answers(tx_hash)
        self.quarantine_slot = doc["quarantine_slot"]

    def get_policy_assets(self, policy_id: str) -> list[dict]:
        return [{"asset": policy_id + name, "quantity": str(quantity)}
                for name, quantity in self.supply[policy_id].items()]

    def get_address_utxos(self, address: str) -> list[dict]:
        assert address == QUARANTINE_ADDRESS
        amounts = [{"unit": unit, "quantity": str(q)} for unit, q in self.held.items()]
        return [{"amount": [{"unit": "lovelace", "quantity": "9000000"}, *amounts]}]

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
    assert ("treasury units it records in quarantine borne out on chain" in out) != changed


# --- treasury units already in quarantine -------------------------------------

def _agent(doc: dict) -> dict:
    return doc["assets"]["AGENT"]["units"][AGENT.asset_name_hex]


def _recorded_tx(doc: dict) -> str:
    return _agent(doc)["waived_already_quarantined"]["tx_hash"]


def _quarantine_output(tip: TipView, doc: dict) -> dict:
    (out,) = [o for o in tip.utxos[_recorded_tx(doc)]["outputs"] if o["address"] == QUARANTINE_ADDRESS]
    return out


def _agent_amount(out: dict) -> dict:
    (amount,) = [a for a in out["amount"] if a["unit"] == AGENT.unit]
    return amount


@dataclass(frozen=True)
class RecordProbe:
    name: str
    change: Callable[[dict, TipView], None]
    """Edits the committed pin and the tip it is checked against."""
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


@record_probe("created no outputs on chain")
def a_recorded_transaction_the_chain_does_not_know(doc, tip):
    del tip.txs[_recorded_tx(doc)], tip.utxos[_recorded_tx(doc)]


@record_probe("created no outputs on chain")
def a_recorded_transaction_whose_scripts_failed(doc, tip):
    tip.txs[_recorded_tx(doc)]["valid_contract"] = False


@record_probe("fewer than the")
def a_recorded_transaction_that_quarantined_one_unit_fewer(doc, tip):
    _agent_amount(_quarantine_output(tip, doc))["quantity"] = str(_agent(doc)["waived_already_quarantined"]["quantity"] - 1)


@record_probe()
def a_recorded_transaction_that_quarantined_exactly_the_record(doc, tip):
    _agent_amount(_quarantine_output(tip, doc))["quantity"] = str(_agent(doc)["waived_already_quarantined"]["quantity"])


@record_probe()
def the_recorded_units_split_across_two_quarantine_outputs(doc, tip):
    out = _quarantine_output(tip, doc)
    quantity = int(_agent_amount(out)["quantity"])
    _agent_amount(out)["quantity"] = str(quantity - quantity // 2)
    tip.utxos[_recorded_tx(doc)]["outputs"].append({
        "output_index": 99, "address": QUARANTINE_ADDRESS, "collateral": False,
        "amount": [{"unit": AGENT.unit, "quantity": str(quantity // 2)}],
    })


@record_probe("fewer than the")
def the_recorded_units_in_a_collateral_return(doc, tip):
    """A valid transaction lists its collateral return but never creates it."""
    _quarantine_output(tip, doc)["collateral"] = True


@record_probe("fewer than the")
def the_recorded_units_sent_to_another_address(doc, tip):
    _quarantine_output(tip, doc)["address"] = "addr1" + "q" * 98


@record_probe("fewer than the")
def the_recorded_units_as_another_unit(doc, tip):
    _agent_amount(_quarantine_output(tip, doc))["unit"] = AGENT.policy_id + "00"


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
    _agent(doc)["quarantined"] = tip.held[AGENT.unit] = 15_000_000


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


def test_the_committed_record_names_the_transaction_it_checks():
    doc = _doc()
    tip = TipView(doc)
    asked = []

    def get_tx_utxos(tx_hash: str) -> dict:
        asked.append(tx_hash)
        return tip.utxos[tx_hash]
    tip.get_tx_utxos = get_tx_utxos
    assert drift(doc, tip) == []
    assert asked == [_recorded_tx(doc)]


def test_check_exits_nonzero_when_the_record_does_not_hold(capsys, tmp_path):
    doc = _doc()
    _agent(doc)["waived_already_quarantined"]["quantity"] = _agent(doc)["waiver"] + 1
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps(doc))
    assert check(pin, TipView(_doc())) == 1
    assert "more than the waiver" in capsys.readouterr().out


def repin(tmp_path: Path, monkeypatch, change: Callable[[dict, TipView], None] | None = None) -> str:
    """Pin an unchanged chain (the committed pin's) at its supply slot and
    return what was written, or "refused: <reason>" when nothing was."""
    doc = _doc()
    tip = TipView(doc)
    if change is not None:
        change(doc, tip)
    out = tmp_path / "repinned.json"
    monkeypatch.setattr(pin_redemption, "BlockfrostClient", lambda: tip)
    try:
        pin_redemption.main(doc["supply_slot"], out)
    except SystemExit as exc:
        assert not out.exists()
        return f"refused: {exc}"
    return out.read_text()


def test_repinning_an_unchanged_chain_writes_the_committed_pin(tmp_path, monkeypatch):
    """The record included, so a re-pin keeps it."""
    assert repin(tmp_path, monkeypatch) == PIN_PATH.read_text()


def test_pinning_refuses_a_record_the_chain_does_not_bear_out(tmp_path, monkeypatch):
    written = repin(tmp_path, monkeypatch, a_recorded_transaction_that_quarantined_one_unit_fewer)
    assert written.startswith("refused: ") and "fewer than the" in written
