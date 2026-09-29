#!/usr/bin/env python3
"""Pin what each merge asset can still redeem (read-only, mainnet).

Two collection policies are single-key scripts with no time lock, so their
key holder can still mint, and the fungible bucket was sized for a fixed
supply. For every unit that existed at the supply slot the pin records:

  supply       its on-chain quantity at that slot (later mints do not count)
  waiver       the team supply the rate table carves out of it
  quarantined  what the quarantine address holds now; nothing leaves it
  waived_already_quarantined
               waived units among the quarantined ones, with the transaction
               that sent them there (only where the waived reserve no longer
               holds them)

A signer redeems at most supply - (waiver - waived_already_quarantined) -
quarantined of a unit across every surrender it approves after the pin, so a
unit is never paid for twice, and waived units already in quarantine are not
subtracted twice. Pinning refuses to write a record the chain does not bear
out, so a pin never promises more of a unit than exists outside its waived
reserve and quarantine.

    NETWORK=mainnet BLOCKFROST_PROJECT_ID=... python -m scripts.pin_redemption \\
        <supply slot> audit_pack/<date>/redemption_pin.json

--check compares the chain tip with a committed pin and exits 1 if any unit
has more supply now than at the pin, any NFT was minted or burned after the
pin's supply slot (which every signer refuses), a redeemable name exists that
the pin lacks, quarantine holds other amounts than the pin records, or a
record of waived units already in quarantine is not borne out: it exceeds its
waiver or its quarantine count; the transaction it names created no outputs,
came after the pin's quarantine count, or sent fewer of the unit to
quarantine; or the waived reserve did not hold the waiver at the snapshot, or
still holds more than the waiver less the record. Re-pinning at the old
supply slot cannot show a later mint; this reads current supply and each
NFT's own history.

    NETWORK=mainnet BLOCKFROST_PROJECT_ID=... python -m scripts.pin_redemption \\
        --check audit_pack/<date>/redemption_pin.json
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import requests

from services.chain_check import mints_and_burns_after
from tools.api_clients import BlockfrostClient
from tools.config import (
    CIP68_REFERENCE_TOKEN_PREFIX,
    CIP68_USER_TOKEN_PREFIX,
    LEGACY_TOKENS,
    NFT_COLLECTIONS,
)
from tools.process_surrender import load_rate_table, waived_already_quarantined

ROOT = Path(__file__).resolve().parent.parent
RATE_TABLE = ROOT / "audit_pack/2026-04-19/rate_table_cmatra.json"
# The rate table's waived reserves: per token, the addresses whose holdings at
# the snapshot are its waiver.
RESERVES = ROOT / "audit_pack/2026-04-19/allocations_cmatra_summary.json"
QUARANTINE_ADDRESS = "addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl"

# Waived units a surrender sent to quarantine: token -> (the transaction that
# sent them, how many). The waiver and the quarantine count both hold them, so
# the pin records them and they are not subtracted twice. Empty while every
# waived unit is still in its waived reserve.
WAIVED_ALREADY_QUARANTINED: dict[str, tuple[str, int]] = {}


def redeemable_names(policy_assets: list[dict]) -> list[str]:
    """Asset-name hex of every unit in circulation (quantity > 0). A CIP-68
    collection redeems only its user tokens; any other collection redeems every
    live name except CIP-68 reference tokens, editions with supply > 1 included."""
    live = sorted(
        row["asset"][56:] for row in policy_assets if int(row["quantity"]) > 0
    )
    user_tokens = [n for n in live if n.startswith(CIP68_USER_TOKEN_PREFIX)]
    if user_tokens:
        return user_tokens
    return [n for n in live if not n.startswith(CIP68_REFERENCE_TOKEN_PREFIX)]


def supply_at(history: list[dict], slot_of: Callable[[str], int], slot: int) -> int:
    """A unit's supply at ``slot`` from its Blockfrost mint/burn history,
    where a burn carries a negative amount."""
    supply = 0
    for event in history:
        amount = int(event["amount"])
        if (event["action"] == "minted") != (amount > 0):
            raise ValueError(f"{event['tx_hash']}: {event['action']} {amount}")
        if slot_of(event["tx_hash"]) <= slot:
            supply += amount
    return supply


def quarantine_holdings(bf: Any) -> dict[str, int]:
    """Unit -> quantity the quarantine address holds now."""
    held: dict[str, int] = {}
    for utxo in bf.get_address_utxos(QUARANTINE_ADDRESS):
        for amount in utxo["amount"]:
            held[amount["unit"]] = held.get(amount["unit"], 0) + int(amount["quantity"])
    return held


def sent_to_quarantine(tx_hash: str, unit: str, bf: Any) -> tuple[int, int] | None:
    """(slot, quantity of ``unit`` sent to the quarantine address) for
    transaction ``tx_hash``, or None when the chain has no such transaction
    or its scripts failed, so it created none of its outputs. A valid
    transaction's collateral return is listed with them but never created."""
    try:
        tx = bf.get_tx(tx_hash)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    if tx["valid_contract"] is not True:
        return None
    sent = sum(
        int(amount["quantity"])
        for out in bf.get_tx_utxos(tx_hash)["outputs"]
        if out["address"] == QUARANTINE_ADDRESS and out["collateral"] is False
        for amount in out["amount"] if amount["unit"] == unit
    )
    return tx["slot"], sent


def waived_reserve(asset: str, unit: str, bf: Any) -> tuple[int, int]:
    """(what ``asset``'s waived reserve held at the snapshot, how much of
    ``unit`` it holds now)."""
    reserve = json.loads(RESERVES.read_text())["reserve"]["team_treasury"][asset]
    held = sum(
        int(amount["quantity"])
        for entry in reserve["addresses"]
        for utxo in bf.get_address_utxos(entry["address"], unit)
        for amount in utxo["amount"] if amount["unit"] == unit
    )
    return reserve["total_balance_base"], held


def already_quarantined_problems(
    asset: str, unit: str, row: dict, quarantine_slot: int, bf: Any,
) -> list[str]:
    """Every way ``row``'s record of waived units already in quarantine is
    not borne out: a record the signers refuse to load; a transaction that
    created no outputs, came after ``quarantine_slot`` (where the pin's
    quarantine count was read) or sent fewer of ``unit`` to quarantine; or a
    waived reserve that did not hold the waiver at the snapshot, or still
    holds more than the waiver less the record. Waived units still in the
    reserve cannot be in quarantine: a record of them would let the signers
    redeem more than exists outside the reserve and quarantine."""
    try:
        quantity = waived_already_quarantined(row)
    except ValueError as exc:
        return [f"{asset} {unit}: {exc}"]
    if not quantity:
        return []
    tx_hash = row["waived_already_quarantined"]["tx_hash"]
    problems = []
    answer = sent_to_quarantine(tx_hash, unit, bf)
    if answer is None:
        problems.append(f"{asset} {unit}: the pin records waived units quarantined by {tx_hash},"
                        " which created no outputs on chain")
    else:
        slot, sent = answer
        if slot > quarantine_slot:
            problems.append(f"{asset} {unit}: {tx_hash} is at slot {slot},"
                            f" after the quarantine count at slot {quarantine_slot}")
        if sent < quantity:
            problems.append(f"{asset} {unit}: {tx_hash} sent {sent} to quarantine,"
                            f" fewer than the {quantity} waived units the pin records")
    at_snapshot, held = waived_reserve(asset, unit, bf)
    if at_snapshot != row["waiver"]:
        problems.append(f"{asset} {unit}: the waived reserve held {at_snapshot} at the snapshot,"
                        f" not the waiver {row['waiver']}")
    if held > row["waiver"] - quantity:
        problems.append(f"{asset} {unit}: the waived reserve still holds {held}, more than the"
                        f" waiver {row['waiver']} less the {quantity} waived units the pin records")
    return problems


def drift(doc: dict, bf: Any) -> list[str]:
    """Every way the chain tip differs from pin ``doc`` in what may still be
    redeemed; empty while the pin holds."""
    fungibles = {token.name for token in LEGACY_TOKENS}
    held = quarantine_holdings(bf)
    problems = []
    for asset, entry in sorted(doc["assets"].items()):
        policy = entry["policy_id"]
        listing = bf.get_policy_assets(policy)
        current = {row["asset"][56:]: int(row["quantity"]) for row in listing}
        for name, row in sorted(entry["units"].items()):
            unit = policy + name
            if current.get(name, 0) > row["supply"]:
                problems.append(
                    f"{asset} {unit}: supply {current[name]} on chain, {row['supply']} at the pin")
            if held.get(unit, 0) != row["quarantined"]:
                problems.append(
                    f"{asset} {unit}: quarantine holds {held.get(unit, 0)},"
                    f" the pin says {row['quarantined']}")
            problems += already_quarantined_problems(asset, unit, row, doc["quarantine_slot"], bf)
            if asset not in fungibles:
                problems += [
                    f"{asset} {unit}: {action} in {tx_hash} at slot {slot}, after the pin"
                    for action, tx_hash, slot in mints_and_burns_after(unit, doc["supply_slot"], bf)
                ]
        if asset not in fungibles:
            problems += [
                f"{asset} {policy + name}: redeemable name minted after the pin"
                for name in redeemable_names(listing) if name not in entry["units"]
            ]
    return problems


def check(pin: Path, bf: Any) -> int:
    """Print how the tip differs from ``pin``; 1 if it does, else 0."""
    problems = drift(json.loads(pin.read_text()), bf)
    for problem in problems:
        print(problem)
    if problems:
        print(f"PIN NO LONGER HOLDS: {len(problems)} difference(s) from {pin}")
        return 1
    print(f"pin holds: no unit above its pinned supply, no NFT minted or burned since the pin,"
          f" no new name, quarantine as pinned, every record of waived units in quarantine"
          f" borne out by its transaction and its waived reserve ({pin})")
    return 0


def main(supply_slot: int, out: Path) -> None:
    bf = BlockfrostClient()
    waivers = load_rate_table(RATE_TABLE)["team_waiver_supplies"]

    @lru_cache(maxsize=None)
    def slot_of(tx_hash: str) -> int:
        return bf.get_tx(tx_hash)["slot"]

    def supply(unit: str) -> int:
        return supply_at(bf.get_asset_history(unit), slot_of, supply_slot)

    assets: dict[str, dict] = {}
    for token in LEGACY_TOKENS:
        row = {"supply": supply(token.unit), "waiver": waivers[token.name]}
        if token.name in WAIVED_ALREADY_QUARANTINED:
            tx_hash, quantity = WAIVED_ALREADY_QUARANTINED[token.name]
            row["waived_already_quarantined"] = {"quantity": quantity, "tx_hash": tx_hash}
        assets[token.name] = {"policy_id": token.policy_id, "units": {token.asset_name_hex: row}}
    for nft in NFT_COLLECTIONS:
        listing = [
            {"asset": row["asset"], "quantity": supply(row["asset"])}
            for row in bf.get_policy_assets(nft.policy_id)
        ]
        supplies = {row["asset"][56:]: row["quantity"] for row in listing}
        assets[nft.name] = {"policy_id": nft.policy_id, "units": {
            name: {"supply": supplies[name], "waiver": 0} for name in redeemable_names(listing)
        }}

    tip = bf.get_latest_block()
    held = quarantine_holdings(bf)
    for entry in assets.values():
        for name, row in entry["units"].items():
            row["quarantined"] = held.get(entry["policy_id"] + name, 0)

    problems = [
        problem
        for asset, entry in sorted(assets.items()) for name, row in sorted(entry["units"].items())
        for problem in already_quarantined_problems(asset, entry["policy_id"] + name, row, tip["slot"], bf)
    ]
    if problems:
        sys.exit("\n".join(problems))

    doc = {"supply_slot": supply_slot, "quarantine_slot": tip["slot"], "assets": assets}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    for name, entry in assets.items():
        rows = entry["units"].values()
        print(f"{name}: {len(rows)} units, supply {sum(r['supply'] for r in rows)}, "
              f"quarantined {sum(r['quarantined'] for r in rows)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python -m scripts.pin_redemption <supply slot> <out.json>"
                 " | --check <pin.json>")
    if sys.argv[1] == "--check":
        sys.exit(check(Path(sys.argv[2]), BlockfrostClient()))
    main(int(sys.argv[1]), Path(sys.argv[2]))
