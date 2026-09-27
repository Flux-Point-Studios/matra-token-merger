#!/usr/bin/env python3
"""Pin what each merge asset can still redeem (read-only, mainnet).

Two collection policies are single-key scripts with no time lock, so their
key holder can still mint, and the fungible bucket was sized for a fixed
supply. For every unit that existed at the supply slot the pin records:

  supply       its on-chain quantity at that slot (later mints do not count)
  waiver       the team supply the rate table carves out of it
  quarantined  what the quarantine address holds now; nothing leaves it

A signer redeems at most supply - waiver - quarantined of a unit across every
surrender it approves after the pin, so a unit is never paid for twice.

    NETWORK=mainnet BLOCKFROST_PROJECT_ID=... python -m scripts.pin_redemption \\
        <supply slot> audit_pack/<date>/redemption_pin.json
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Callable

from tools.api_clients import BlockfrostClient
from tools.config import (
    CIP68_REFERENCE_TOKEN_PREFIX,
    CIP68_USER_TOKEN_PREFIX,
    LEGACY_TOKENS,
    NFT_COLLECTIONS,
)
from tools.process_surrender import load_rate_table

ROOT = Path(__file__).resolve().parent.parent
RATE_TABLE = ROOT / "audit_pack/2026-04-19/rate_table_cmatra.json"
QUARANTINE_ADDRESS = "addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl"


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
        assets[token.name] = {"policy_id": token.policy_id, "units": {
            token.asset_name_hex: {"supply": supply(token.unit), "waiver": waivers[token.name]},
        }}
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
    held: dict[str, int] = {}
    for utxo in bf.get_address_utxos(QUARANTINE_ADDRESS):
        for amount in utxo["amount"]:
            held[amount["unit"]] = held.get(amount["unit"], 0) + int(amount["quantity"])
    for entry in assets.values():
        for name, row in entry["units"].items():
            row["quarantined"] = held.get(entry["policy_id"] + name, 0)

    doc = {"supply_slot": supply_slot, "quarantine_slot": tip["slot"], "assets": assets}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    for name, entry in assets.items():
        rows = entry["units"].values()
        print(f"{name}: {len(rows)} units, supply {sum(r['supply'] for r in rows)}, "
              f"quarantined {sum(r['quarantined'] for r in rows)}")


if __name__ == "__main__":
    main(int(sys.argv[1]), Path(sys.argv[2]))
