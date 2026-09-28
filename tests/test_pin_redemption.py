"""scripts.pin_redemption --check compares the chain tip with a committed
pin. A unit minted past its pinned supply, an NFT minted or burned after the
pin's supply slot, a redeemable name the pin does not list, or quarantine
holdings that moved since the pin each mean the pin no longer describes what
may still be redeemed. Re-pinning at the pin's own supply slot cannot show a
later mint, so the check reads current supply and each NFT's history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pin_redemption import QUARANTINE_ADDRESS, check, drift
from tests.cosign_cases import PIN, FakeChain
from tools.config import AGENT, FLUX_PASS, T1_ADAM_PASS

ROOT = Path(__file__).resolve().parent.parent
PIN_PATH = ROOT / "audit_pack/2026-09-27/redemption_pin.json"


def _doc() -> dict:
    return json.loads(PIN_PATH.read_text())


class TipView:
    """Blockfrost at the tip: exactly what the pin describes until a test
    changes ``supply`` (policy -> asset name -> quantity), ``held`` or a
    unit's history in ``chain``."""

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
        return self.chain.get_tx(tx_hash)


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
