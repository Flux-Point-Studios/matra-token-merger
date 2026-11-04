"""The caps the merger reopens with: 4M cMATRA per surrender and 15M per day.

One T1_ADAM_PASS pays 3,191,874.488548 cMATRA, the most any single unit pays,
so a per-surrender cap below that would leave every remaining T1 pass
unredeemable: a signer refuses to start with one. A real mainnet surrender of
one T1 pass, and nothing else, is approved under the reopen caps and refused
under the 2M first proposed."""

from __future__ import annotations

from dataclasses import replace

import pytest

from services.cosign_policy import CosignRejected, evaluate_surrender, load_config
from services.redemption_ledger import RedemptionLedger, create_ledger
from tests.cosign_cases import (
    ADMIN_1,
    ADMIN_2,
    MAINNET_BEFORE_SURRENDERS,
    RATES,
    SURRENDER_HASHES,
    golden_scenario,
)
from tests.test_cosign_policy import ENV
from tools.config import T1_ADAM_PASS
from tools.process_surrender import compute_redemption

CMATRA = 10**6
PER_TX, PER_DAY = 4_000_000 * CMATRA, 15_000_000 * CMATRA
ONE_T1_PASS = "cc4532d89efb7c90"  # the start of a mainnet surrender of one T1 pass and nothing else
T1_PAYS = compute_redemption(RATES, "T1_ADAM_PASS", 1)


def _one_t1_pass():
    return golden_scenario(next(t for t in SURRENDER_HASHES if t.startswith(ONE_T1_PASS)))


def test_one_t1_pass_pays_the_most_of_any_unit():
    assert T1_PAYS == 3_191_874_488_548
    assert all(compute_redemption(RATES, name, 1) <= T1_PAYS for name in RATES["tokens"])


def test_one_t1_pass_is_approved_and_recorded_under_the_reopen_caps(tmp_path):
    s = _one_t1_pass()
    cfg = replace(MAINNET_BEFORE_SURRENDERS, max_payout_per_tx=PER_TX)
    approval = evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, cfg)
    assert approval.payout == T1_PAYS
    assert all(unit.startswith(T1_ADAM_PASS.policy_id) for unit in approval.units)
    path = str(tmp_path / "ledger.sqlite3")
    create_ledger(path)
    RedemptionLedger(path, PER_DAY, cfg.funded_limits).record(approval, 1000.0, cfg.redemption_limits)


def test_one_t1_pass_is_refused_under_a_2m_cap():
    s = _one_t1_pass()
    cfg = replace(MAINNET_BEFORE_SURRENDERS, max_payout_per_tx=2_000_000 * CMATRA)
    with pytest.raises(CosignRejected) as refused:
        evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, cfg)
    assert refused.value.code == "per_tx_cap"


@pytest.mark.parametrize("cap", [2_000_000 * CMATRA, T1_PAYS - 1])
def test_a_signer_does_not_start_with_a_cap_that_strands_a_t1_pass(cap):
    with pytest.raises(ValueError, match="MAX_CMATRA_PER_TX"):
        load_config({**ENV, "MAX_CMATRA_PER_TX": str(cap)}, [ADMIN_1, ADMIN_2])


@pytest.mark.parametrize("cap", [T1_PAYS, PER_TX])
def test_a_signer_starts_with_a_cap_one_t1_pass_fits_under(cap):
    assert load_config({**ENV, "MAX_CMATRA_PER_TX": str(cap)}, [ADMIN_1, ADMIN_2]).max_payout_per_tx == cap
