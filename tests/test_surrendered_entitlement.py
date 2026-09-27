"""surrendered_entitlement prices a set of surrendered legacy units.

surrender_api prices a build with it and the co-signer re-prices the
quarantine output with it, so both sides agree to the base unit by
construction. NFT units must appear in the pinned redeemable set: two of the
collection policies are single-key scripts with no time lock, so "any unit
under the collection policy" would include units minted after the pin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pin_redeemable_nft_units import redeemable_names
from tools.config import AGENT, FLUX_PASS, NFT_COLLECTIONS, SHARDS, T1_ADAM_PASS
from tools.process_surrender import (
    compute_redemption,
    load_rate_table,
    load_redeemable_nfts,
    surrendered_entitlement,
)

ROOT = Path(__file__).resolve().parent.parent
RATES = load_rate_table(ROOT / "audit_pack/2026-04-19/rate_table_cmatra.json")
PINNED = ROOT / "audit_pack/2026-09-27/redeemable_nft_units.json"

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


def test_pinned_file_loads_as_units_under_the_configured_policies():
    units = load_redeemable_nfts(PINNED)
    policies = {nft.policy_id for nft in NFT_COLLECTIONS}
    assert units and all(unit[:56] in policies for unit in units)
    assert FLUX_PASS.policy_id + "000de140" in {u[:64] for u in units}


def test_pinned_file_with_a_policy_mismatch_is_refused(tmp_path):
    doc = json.loads(PINNED.read_text())
    doc["collections"]["FLUX_PASS"]["policy_id"] = "ee" * 28
    bad = tmp_path / "pin.json"
    bad.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="FLUX_PASS"):
        load_redeemable_nfts(bad)
