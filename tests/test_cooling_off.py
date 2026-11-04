"""The 72-hour cooling-off in services.chain_check, against every surrender
made after the golden set up to the stop.

Each claim is checked as its signer would have at its validity start, with no
deny-list and nothing recorded, against what mainnet answered about where its
units came from. Every one of the 19 claims the attacker made is refused at
the transaction that moved the passes from the attacker's collection address
to the claim address, within the hour before. Of the 17 legitimate claims
beside them, 8 pass; 9 would have waited up to 72 hours: 5 surrendered AGENT
or SHARDS bought from a DEX within the hour, 3 surrendered units sent from
another key minutes before, and 1 had moved its AGENT to the claim address
from another payment key of the same stake account. That last kind waits too:
a stake credential is no proof of ownership, since anyone can pair their own
payment key with another's stake credential."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest
from pycardano import Address

from services.chain_check import COOLING_OFF_SLOTS, confirm_on_chain
from services.cosign_policy import CosignRejected, evaluate_surrender
from tests.cosign_cases import (
    FORFEITED_HASHES,
    MAINNET,
    RECENT_SURRENDER_HASHES,
    FakeChain,
    recent_scenario,
    utxos_answer,
)
from tools.credential_denylist import DenyList

NO_DENYLIST = replace(MAINNET, denylist=DenyList(labels={}, allowed=frozenset(), sha256="none"))
NOTHING_RECORDED = {}.get
ATTACKER_COLLECTION_KEY = bytes.fromhex("809a40995f079a45821c91754a2875a64f874a4f77983185ba69becd")
WAITED = {
    "72fe86ba4e7fa9eceb266ea880f65f7c5266a9ae3d9c71e1a28e67b23d1203e4": "bought",
    "4ea74d7c5bd2b75c6fa94e89036ca02b4662a484f16f34043300bcc4c58f2b19": "bought",
    "7c1572870d1dfdda1a6f7c0da7df64fb5d0a3a1ca99ade12f3dc83790c2dba6a": "bought",
    "3dc24f6240cb3a1cf28dbb6760f68a7ba4e6df616b0d6eae496aaa0b49d0eac2": "bought",
    "7e47c36350ab06f41a2f5f8e9e8feaf806f37100dceff58f79283f24119224af": "bought",
    "4f9715be6aa9a709485d5fb92dc536b6a06d94678df9d5f372df5ba7d1f6d3a9": "sent from another key",
    "4a82d2c281dd4c50e403036ee8bafe2f9f4d00d2f88c2c4aaad21daf718776fc": "sent from another key",
    "ae05ecf1d186b059d0ced8fcedeeb414e42121d272f94200824dddf63b0ed70b": "sent from another key",
    "0aa56bdf468437a22169d75e538547d5e1cd52c1b83a573fb9b75b43f4e0b4c0": "moved between payment keys",
}


def _check(tx_hash: str):
    """(the approval, the refusal or None) of ``tx_hash`` checked as above."""
    s = recent_scenario(tx_hash)
    approval = evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, NO_DENYLIST)
    try:
        confirm_on_chain(approval, NO_DENYLIST, FakeChain.as_at_the_recent_builds(), NOTHING_RECORDED)
    except CosignRejected as exc:
        return approval, exc
    return approval, None


def _arrival(refusal: CosignRejected) -> tuple[str, str, int]:
    """(unit, the transaction it reached the claimant in, that slot)."""
    found = re.fullmatch(r"([0-9a-f]+) reached the claimant in ([0-9a-f]{64}) at slot (\d+);.*", refusal.detail)
    assert found, refusal.detail
    return found.group(1), found.group(2), int(found.group(3))


def _sources(tx_hash: str, unit: str) -> list[Address]:
    """The addresses of the inputs of ``tx_hash`` that carried ``unit``."""
    answer = utxos_answer(FakeChain.as_at_the_recent_builds().bodies, tx_hash, {})
    return [
        Address.from_primitive(entry["address"]) for entry in answer["inputs"]
        if not entry["collateral"] and not entry["reference"]
        and any(amount["unit"] == unit for amount in entry["amount"])
    ]


@pytest.mark.parametrize("tx_hash", FORFEITED_HASHES)
def test_a_forfeited_claim_is_refused_at_the_transfer_from_the_attacker(tx_hash):
    approval, refusal = _check(tx_hash)
    assert refusal is not None and refusal.code == "cooling_off"
    unit, arrival, slot = _arrival(refusal)
    assert approval.checked_at - COOLING_OFF_SLOTS < slot < approval.checked_at + 3600
    sources = _sources(arrival, unit)
    assert sources and {a.payment_part.payload for a in sources} == {ATTACKER_COLLECTION_KEY}


@pytest.mark.parametrize("tx_hash", RECENT_SURRENDER_HASHES)
def test_a_legitimate_claim_passes_unless_its_units_had_just_arrived(tx_hash):
    approval, refusal = _check(tx_hash)
    if tx_hash not in WAITED:
        assert refusal is None
        return
    assert refusal is not None and refusal.code == "cooling_off"
    unit, arrival, _ = _arrival(refusal)
    sources = _sources(arrival, unit)
    claimant = Address.from_primitive(approval.claimant)
    if WAITED[tx_hash] == "bought":
        assert all(type(a.payment_part).__name__ == "ScriptHash" for a in sources)
    elif WAITED[tx_hash] == "sent from another key":
        assert all(a.payment_part != claimant.payment_part and a.staking_part != claimant.staking_part
                   for a in sources)
    else:
        assert all(a.payment_part != claimant.payment_part and a.staking_part == claimant.staking_part
                   for a in sources)


def test_eight_legitimate_claims_pass_and_nine_wait():
    assert len(RECENT_SURRENDER_HASHES) - len(WAITED) == 8
    assert set(WAITED) <= set(RECENT_SURRENDER_HASHES)
