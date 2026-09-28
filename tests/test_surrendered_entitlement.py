"""surrendered_entitlement prices a set of surrendered legacy units.

surrender_api prices a build with it and the co-signer re-prices the
quarantine output with it, so both sides agree to the base unit by
construction. NFT units must appear in the pinned redeemable set: two of the
collection policies are single-key scripts with no time lock, so "any unit
under the collection policy" would include units minted after the pin.

The pin also records how much of each unit remains redeemable (its supply at
the pin, less the team waiver, less what quarantine already holds); the
signers' ledgers hold every approval to it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pin_redemption import redeemable_names, supply_at
from tests.cosign_cases import QUARANTINE, SURRENDER_HASHES, golden_scenario
from tools.config import (
    AGENT,
    FLUX_PASS,
    LEGACY_TOKENS,
    NFT_COLLECTIONS,
    SHARDS,
    T1_ADAM_PASS,
)
from services.cosign_policy import decode, parse_output, split_tx
from tools.process_surrender import (
    compute_redemption,
    load_rate_table,
    load_redemption_pin,
    surrendered_entitlement,
)

ROOT = Path(__file__).resolve().parent.parent
RATES = load_rate_table(ROOT / "audit_pack/2026-04-19/rate_table_cmatra.json")
PINNED = ROOT / "audit_pack/2026-09-27/redemption_pin.json"

FLUX_1 = FLUX_PASS.policy_id + "000de14001"
FLUX_2 = FLUX_PASS.policy_id + "000de14002"
T1_1 = T1_ADAM_PASS.policy_id + "01"
REDEEMABLE = frozenset({FLUX_1, FLUX_2, T1_1})


def test_fungible_units_price_by_quantity():
    assert surrendered_entitlement(RATES, {AGENT.unit: 2283}, REDEEMABLE) == (
        compute_redemption(RATES, "AGENT", 2283)
    )


def test_each_redeemable_nft_counts_once():
    units = {FLUX_1: 1, FLUX_2: 1}
    assert surrendered_entitlement(RATES, units, REDEEMABLE) == (
        compute_redemption(RATES, "FLUX_PASS", 2)
    )


def test_prices_per_asset_then_sums():
    units = {AGENT.unit: 296_013, SHARDS.unit: 4_621_243_449, T1_1: 1}
    assert surrendered_entitlement(RATES, units, REDEEMABLE) == (
        compute_redemption(RATES, "AGENT", 296_013)
        + compute_redemption(RATES, "SHARDS", 4_621_243_449)
        + compute_redemption(RATES, "T1_ADAM_PASS", 1)
    )


def test_nothing_surrendered_is_worth_nothing():
    assert surrendered_entitlement(RATES, {}, REDEEMABLE) == 0


def test_nft_from_a_foreign_policy_is_refused():
    with pytest.raises(ValueError, match="not a redeemable merge asset"):
        surrendered_entitlement(RATES, {"ee" * 28 + "6a756e6b": 1}, REDEEMABLE)


def test_unpinned_unit_under_a_collection_policy_is_refused():
    with pytest.raises(ValueError, match="not a redeemable merge asset"):
        surrendered_entitlement(RATES, {T1_ADAM_PASS.policy_id + "02": 1}, REDEEMABLE)


def test_unknown_token_under_a_fungible_policy_is_refused():
    with pytest.raises(ValueError, match="not a redeemable merge asset"):
        surrendered_entitlement(RATES, {AGENT.policy_id + "00": 5}, REDEEMABLE)


def test_collection_unit_with_quantity_above_one_is_refused():
    with pytest.raises(ValueError, match="not a redeemable merge asset"):
        surrendered_entitlement(RATES, {FLUX_1: 2}, REDEEMABLE)


def test_zero_quantity_is_refused():
    with pytest.raises(ValueError):
        surrendered_entitlement(RATES, {AGENT.unit: 0}, REDEEMABLE)


# --- the pinned name list -----------------------------------------------------


def _assets(*rows: tuple[str, int]) -> list[dict]:
    return [{"asset": "ab" * 28 + name, "quantity": str(qty)} for name, qty in rows]


def test_cip68_collection_pins_only_live_user_tokens():
    listing = _assets(
        ("000de14001", 1), ("000643b001", 1), ("001f4d70526f79616c7479", 5),
        ("000de14002", 0),
    )
    assert redeemable_names(listing) == ["000de14001"]


def test_plain_collection_pins_every_live_name_including_editions():
    listing = _assets(("5431", 1), ("5432", 3), ("5433", 0), ("000643b0aa", 1))
    assert redeemable_names(listing) == ["5431", "5432"]


def test_supply_at_a_slot_counts_the_mints_and_burns_up_to_it():
    """Blockfrost reports a burn with a negative amount."""
    history = [
        {"tx_hash": "a", "action": "minted", "amount": "3"},
        {"tx_hash": "b", "action": "burned", "amount": "-1"},
        {"tx_hash": "c", "action": "minted", "amount": "5"},
    ]
    slot_of = {"a": 10, "b": 20, "c": 30}.__getitem__
    assert supply_at(history, slot_of, 9) == 0
    assert supply_at(history, slot_of, 25) == 2
    assert supply_at(history, slot_of, 30) == 7


def test_supply_at_refuses_an_event_whose_sign_contradicts_its_action():
    history = [{"tx_hash": "a", "action": "burned", "amount": "1"}]
    with pytest.raises(ValueError, match="burned"):
        supply_at(history, lambda _: 0, 10)


def test_pinned_file_loads_as_units_under_the_configured_policies():
    pin = load_redemption_pin(PINNED)
    policies = {nft.policy_id for nft in NFT_COLLECTIONS}
    assert len(pin.nft_units) == 849
    assert all(unit[:56] in policies for unit in pin.nft_units)
    assert FLUX_PASS.policy_id + "000de140" in {u[:64] for u in pin.nft_units}
    assert set(pin.remaining) == pin.nft_units | {token.unit for token in LEGACY_TOKENS}


def test_pin_records_each_units_supply_at_the_pin():
    """What the chain may hold of a unit while it is still redeemed."""
    pin = load_redemption_pin(PINNED)
    assert set(pin.supply) == set(pin.remaining)
    assert pin.supply[AGENT.unit] == 1_000_000_000
    assert all(pin.supply[unit] >= 1 for unit in pin.nft_units)


def _doc() -> dict:
    return json.loads(PINNED.read_text())


def _write(tmp_path, doc: dict) -> Path:
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(doc))
    return path


def test_pinned_file_with_a_policy_mismatch_is_refused(tmp_path):
    doc = _doc()
    doc["assets"]["FLUX_PASS"]["policy_id"] = "ee" * 28
    with pytest.raises(ValueError, match="FLUX_PASS"):
        load_redemption_pin(_write(tmp_path, doc))


def test_a_fungible_is_pinned_as_its_own_token_only(tmp_path):
    doc = _doc()
    doc["assets"]["AGENT"]["units"]["00"] = {"supply": 5, "waiver": 0, "quarantined": 0}
    with pytest.raises(ValueError, match="AGENT"):
        load_redemption_pin(_write(tmp_path, doc))


def test_remaining_is_supply_less_waiver_less_quarantined(tmp_path):
    doc = _doc()
    doc["assets"]["AGENT"]["units"][AGENT.asset_name_hex] = {"supply": 100, "waiver": 30, "quarantined": 20}
    t1_name = next(iter(doc["assets"]["T1_ADAM_PASS"]["units"]))
    doc["assets"]["T1_ADAM_PASS"]["units"][t1_name] = {"supply": 1, "waiver": 0, "quarantined": 2}
    pin = load_redemption_pin(_write(tmp_path, doc))
    assert pin.remaining[AGENT.unit] == 50
    assert pin.remaining[T1_ADAM_PASS.policy_id + t1_name] == 0


def test_fungible_supply_less_waiver_is_the_rate_tables_bucket():
    doc = _doc()
    for token in LEGACY_TOKENS:
        row = doc["assets"][token.name]["units"][token.asset_name_hex]
        assert row["waiver"] == RATES["team_waiver_supplies"][token.name]
        assert row["supply"] - row["waiver"] == RATES["tokens"][token.name]["redeemable_supply_base"]


def test_the_pin_counts_every_unit_the_pool_has_already_paid_for():
    """Everything a mainnet surrender moved to quarantine is in the pin's
    quarantined count, so no past surrender can be redeemed again."""
    paid: dict[str, int] = {}
    for tx_hash in SURRENDER_HASHES:
        for raw in decode(split_tx(golden_scenario(tx_hash).tx)[0])[1]:
            out = parse_output(raw)
            if out.address == QUARANTINE:
                for policy, names in out.assets.items():
                    for name, quantity in names.items():
                        unit = (policy + name).hex()
                        paid[unit] = paid.get(unit, 0) + quantity
    doc = _doc()
    pinned = {
        entry["policy_id"] + name: row["quarantined"]
        for entry in doc["assets"].values() for name, row in entry["units"].items()
    }
    assert paid and all(pinned[unit] >= quantity for unit, quantity in paid.items())
