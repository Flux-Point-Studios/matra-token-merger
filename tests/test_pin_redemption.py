"""scripts.pin_redemption --check compares the chain tip with a committed
pin. A unit minted past its pinned supply, a redeemable name the pin does not
list, or quarantine holdings that moved since the pin each mean the pin no
longer describes what may still be redeemed. Re-pinning at the pin's own
supply slot cannot show a later mint, so the check reads current supply."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pin_redemption import QUARANTINE_ADDRESS, check, drift
from tools.config import AGENT, FLUX_PASS, T1_ADAM_PASS

ROOT = Path(__file__).resolve().parent.parent
PIN_PATH = ROOT / "audit_pack/2026-09-27/redemption_pin.json"


def _doc() -> dict:
    return json.loads(PIN_PATH.read_text())


class TipView:
    """Blockfrost at the tip: exactly what the pin describes until a test
    changes ``supply`` (policy -> asset name -> quantity) or ``held``."""

    def __init__(self, doc: dict) -> None:
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
