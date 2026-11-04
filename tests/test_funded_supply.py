"""What the pool was funded to redeem, held per asset by every signer's ledger.

The rate table priced each merge asset for a redeemable supply, and the pool
was funded for exactly that. T2_ADAM_PASS has 150 editions on chain but was
priced and funded for 95, 75 of which were in quarantine at the pin, so 20
may be redeemed after it, not the 75 its units' own limits allow. For every
other asset the funded supply is what its units' limits allow already. After
the 36 surrenders landed since the pin (2 of them T2), a ledger admits 18
more T2 editions and refuses the 19th."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from services.cosign_policy import Approval, CosignRejected, evaluate_surrender
from services.redemption_ledger import RedemptionLedger, create_ledger
from tests.cosign_cases import MAINNET, PIN, RATES, recent, recent_scenario
from tools.config import ALL_MERGE_ASSETS, T2_ADAM_PASS
from tools.credential_denylist import DenyList
from tools.process_surrender import funded_limits

T2 = T2_ADAM_PASS.policy_id
NO_DENYLIST = replace(MAINNET, denylist=DenyList(labels={}, allowed=frozenset(), sha256="none"))


def test_t2_was_funded_for_20_editions_after_the_pin():
    assert RATES["tokens"]["T2_ADAM_PASS"]["redeemable_supply_base"] == 95
    assert funded_limits(RATES, PIN)[T2] == 95 - 75 == 20


@pytest.mark.parametrize("asset", [a for a in ALL_MERGE_ASSETS if a.policy_id != T2], ids=lambda a: a.name)
def test_every_other_asset_was_funded_for_what_its_units_allow(asset):
    allowed = sum(left for unit, left in PIN.remaining.items() if unit.startswith(asset.policy_id))
    assert funded_limits(RATES, PIN)[asset.policy_id] == allowed


def test_after_the_recent_surrenders_18_more_t2_editions_are_admitted(tmp_path):
    path = str(tmp_path / "ledger.sqlite3")
    create_ledger(path)
    ledger = RedemptionLedger(path, max_per_day=10**30, funded=funded_limits(RATES, PIN))
    redeemed: Counter = Counter()
    for row in sorted(recent()["transactions"], key=lambda t: t["slot"]):
        s = recent_scenario(row["tx_hash"])
        approval = evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, NO_DENYLIST)
        ledger.record(approval, float(row["slot"]), PIN.remaining)
        redeemed.update(approval.units)
    assert sum(q for unit, q in redeemed.items() if unit.startswith(T2)) == 2

    left = [unit for unit in sorted(PIN.remaining) if unit.startswith(T2) and PIN.remaining[unit] > redeemed[unit]]
    for n, unit in enumerate(left[:19]):
        pool_output = (bytes([0x80 + n]) * 32, 0)
        edition = Approval(
            tx_hash=bytes([0xC0 + n]) * 32, payout=1, claimant=b"\x61" + b"\x01" * 28,
            pool_input=pool_output, units={unit: 1}, inputs=(pool_output,), spends=(pool_output,),
            unit_inputs=(), held={unit: 1}, checked_at=0,
        )
        if n < 18:
            ledger.record(edition, 0.0, PIN.remaining)
        else:
            with pytest.raises(CosignRejected) as refused:
                ledger.record(edition, 0.0, PIN.remaining)
            assert refused.value.code == "funded_supply"
