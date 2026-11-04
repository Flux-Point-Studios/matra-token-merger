"""The deny-list in services.cosign_policy, against real mainnet surrenders.

Before the incident every mainnet surrender was approved. Denying W1 and W2
refuses the five made from them and approves the 261 others exactly as
before. The committed list also refuses the three a drained decimalist
account made in May. Every one of the 19 surrenders the attacker made with
passes taken from W1 was approved by the policy as it stood, and is refused
now."""

from __future__ import annotations

from dataclasses import replace

import pytest

from services.cosign_policy import CosignRejected, evaluate_surrender
from tests.cosign_cases import (
    COMMITTED_DENYLIST,
    FORFEITED_HASHES,
    MAINNET,
    SURRENDER_HASHES,
    Scenario,
    forfeited_scenario,
    golden_scenario,
)
from tools.credential_denylist import DenyList

BEFORE = replace(MAINNET, denylist=DenyList(labels={}, allowed=frozenset(), sha256="none"))
NOW = replace(MAINNET, denylist=COMMITTED_DENYLIST)
W1_AND_W2 = replace(MAINNET, denylist=DenyList(
    labels={credential: label for credential, label in COMMITTED_DENYLIST.labels.items()
            if label.startswith(("W1, ", "W2 (FPS treasury), "))},
    allowed=frozenset(), sha256="W1 and W2",
))
FROM_A_DRAINED_DECIMALIST_ACCOUNT = {
    "83fc64b38c45d33fe0bc5a3a2da24f3f721ab36816bee166541a8182691f91c9",
    "08159cc38c3f7c51cd9b5edb292297920c3ea2992c2389385a178b4d444f150e",
    "48079ec5fda2bfc5b1068d6678f87351f18106d5e747a20d16c17af289649809",
}
FROM_W1_OR_W2 = {
    "eaeb8f1ba880a0bdf07bb0fa5a6bc8e1d7835e2bf7adda6c0ed7580f7f6b7d87",  # W2
    "6d062a5a2548ffd855718495e78836ce24c2988b1733c926353b51b98bfe0580",  # W1
    "99f7ad3e131abe73ce9dabc3662b1f445f6679dbf93c9023bffdd96fc4cc9700",  # W1
    "c103a14143091f38bbd045816a6dd5a40720792c71b15bf5aabb25cc042232b6",  # W1
    "e60943b5ce8bd053b6ccca5409ea3b18b7a30c4cf466c0c474bafb97b6c1c00a",  # W1
}


def _outcome(s: Scenario, cfg) -> str:
    try:
        evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, cfg)
    except CosignRejected as exc:
        return exc.code
    return "approved"


def _refused(cfg) -> dict[str, str]:
    outcomes = {tx_hash: _outcome(golden_scenario(tx_hash), cfg) for tx_hash in SURRENDER_HASHES}
    return {tx_hash: outcome for tx_hash, outcome in outcomes.items() if outcome != "approved"}


def test_denying_w1_and_w2_refuses_their_five_surrenders_and_approves_261():
    assert len(W1_AND_W2.denylist.labels) == 4
    refused = _refused(W1_AND_W2)
    assert refused == dict.fromkeys(FROM_W1_OR_W2, "denied_credential")
    assert len(SURRENDER_HASHES) - len(refused) == 261


def test_the_committed_list_also_refuses_a_drained_decimalist_accounts_surrenders():
    refused = _refused(NOW)
    assert refused == dict.fromkeys(FROM_W1_OR_W2 | FROM_A_DRAINED_DECIMALIST_ACCOUNT, "denied_credential")
    assert len(SURRENDER_HASHES) - len(refused) == 258
    for tx_hash in FROM_A_DRAINED_DECIMALIST_ACCOUNT:
        s = golden_scenario(tx_hash)
        with pytest.raises(CosignRejected, match="drained decimalist account"):
            evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, NOW)


@pytest.mark.parametrize("tx_hash", SURRENDER_HASHES)
def test_every_mainnet_surrender_was_approved_before_the_incident(tx_hash):
    assert _outcome(golden_scenario(tx_hash), BEFORE) == "approved"


@pytest.mark.parametrize("tx_hash", sorted(FROM_W1_OR_W2))
def test_a_surrender_from_w1_or_w2_is_refused_naming_the_wallet(tx_hash):
    s = golden_scenario(tx_hash)
    with pytest.raises(CosignRejected) as refused:
        evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, NOW)
    assert refused.value.code == "denied_credential"
    assert "W1" in refused.value.detail or "W2" in refused.value.detail


def test_the_fixture_holds_the_19_forfeited_claims():
    assert len(FORFEITED_HASHES) == len(set(FORFEITED_HASHES)) == 19


@pytest.mark.parametrize("tx_hash", FORFEITED_HASHES)
def test_a_forfeited_claim_was_approved_before_the_incident(tx_hash):
    assert _outcome(forfeited_scenario(tx_hash), BEFORE) == "approved"


@pytest.mark.parametrize("tx_hash", FORFEITED_HASHES)
def test_a_forfeited_claim_is_refused_for_the_attackers_claim_address(tx_hash):
    s = forfeited_scenario(tx_hash)
    with pytest.raises(CosignRejected) as refused:
        evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, NOW)
    assert refused.value.code == "denied_credential"
    assert "attacker claim address" in refused.value.detail
