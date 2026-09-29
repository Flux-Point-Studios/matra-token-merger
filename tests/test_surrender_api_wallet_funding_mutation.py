"""Mutation test for how /build-surrender judges a claimant's wallet.

Each mutant rewrites one condition in a copy of one function, and the
wallet-funding tests then run against it, each on a world of its own. A
mutant that turns no test red is a condition nothing notices, and this test
names it."""

from __future__ import annotations

import inspect
from typing import Callable

import pytest

import services.surrender_api as api
import tests.test_surrender_api_wallet_funding as funding
from tests.mutation import recompiled
from tests.test_surrender_api_cosign import World

TESTS: dict[str, Callable] = {
    name: test for name, test in vars(funding).items()
    if name.startswith("test_") and list(inspect.signature(test).parameters) == ["world", "monkeypatch"]
}
for test, cases in [
    (funding.test_the_builders_own_error_gets_the_same_answer, funding.UNFUNDED),
    (funding.test_another_builder_error_gets_the_answer_it_got_before, funding.OTHER_BUILDER_ERRORS),
]:
    TESTS.update({
        f"{test.__name__}[{case}]": (lambda world, mp, test=test, case=case: test(world, mp, case))
        for case in cases
    })

_REFUSED = "        state.reserved_collateral.discard(reserved_ref)\n        raise WalletShortfall(shortfall)\n"
_EXPLAINED = "        state.reserved_collateral.discard(reserved_ref)\n        raise WalletShortfall(shortfall) from exc\n"

MUTANTS = {
    "no_collateral_in_the_advice": (
        "_wallet_shortfall", "needed = cost + COLLATERAL_TARGET_LOVELACE", "needed = cost",
    ),
    "kept_tokens_not_paid_for": ("_wallet_shortfall", "cost = paid.coin + change + fee", "cost = paid.coin + fee"),
    "fee_not_paid_for": ("_wallet_shortfall", "cost = paid.coin + change + fee", "cost = paid.coin + change"),
    "ada_not_checked_before_the_build": (
        "_wallet_shortfall", "must_fail = held.coin < cost or not collateral", "must_fail = not collateral",
    ),
    "collateral_not_checked_before_the_build": (
        "_wallet_shortfall", "must_fail = held.coin < cost or not collateral", "must_fail = held.coin < cost",
    ),
    "refused_below_the_advice": (
        "_wallet_shortfall", "must_fail = held.coin < cost or", "must_fail = held.coin < needed or",
    ),
    "a_missing_asset_judged": (
        "_wallet_shortfall", "    if not surrendered <= held.multi_asset:\n        return False, None\n", "",
    ),
    "token_collateral_not_offered": (
        "_wallet_shortfall", "u.output.amount.coin > _BUILDER_COLLATERAL_FLOOR",
        "u.output.amount.coin > _BUILDER_COLLATERAL_FLOOR and not u.output.amount.multi_asset",
    ),
    "small_utxos_offered_as_collateral": (
        "_wallet_shortfall", "u.output.amount.coin > _BUILDER_COLLATERAL_FLOOR", "u.output.amount.coin > 0",
    ),
    "collateral_asked_of_a_wallet_short_of_ada": (
        "_wallet_shortfall", "if held.coin < needed:", "if held.coin < cost:",
    ),
    "collateral_never_asked": (
        "_wallet_shortfall", "if not builder.collaterals and largest < COLLATERAL_TARGET_LOVELACE:", "if False:",
    ),
    "need_rounded_down": (
        "_tenths_of_ada", "-(-lovelace // 100_000) if round_up else", "lovelace // 100_000 if round_up else",
    ),
    "holding_rounded_up": (
        "_tenths_of_ada", "else lovelace // 100_000", "else -(-lovelace // 100_000)",
    ),
    "collateral_kept_reserved_on_refusal": ("_build_surrender_tx", _REFUSED, _REFUSED.split("\n", 1)[1]),
    "collateral_kept_reserved_after_a_builder_error": (
        "_build_surrender_tx", _EXPLAINED, _EXPLAINED.split("\n", 1)[1],
    ),
    "builder_errors_not_explained": ("_build_surrender_tx", "if shortfall is None or not funding:", "if True:"),
    "selection_errors_not_explained": ("_build_surrender_tx", "isinstance(exc, UTxOSelectionException) or ", ""),
    "every_builder_error_explained": (
        "_build_surrender_tx", "if shortfall is None or not funding:", "if shortfall is None:",
    ),
}


def _fails(tmp_path, name: str, replace: tuple[str, Callable] | None) -> bool:
    (tmp_path / name).mkdir()
    with pytest.MonkeyPatch.context() as mp:
        world = World(tmp_path / name, mp)
        if replace is not None:
            mp.setattr(api, *replace)
        try:
            TESTS[name](world, mp)
        except (Exception, pytest.fail.Exception):
            return True
        finally:
            world.close()
    return False


def test_the_unmutated_functions_turn_no_test_red(tmp_path):
    assert [name for name in TESTS if _fails(tmp_path, name, None)] == []


@pytest.mark.parametrize("name", MUTANTS)
def test_every_mutant_turns_a_test_red(tmp_path, name):
    function, original, changed = MUTANTS[name]
    mutant = (function, recompiled(getattr(api, function), original, changed))
    assert any(_fails(tmp_path, test, mutant) for test in TESTS), f"{name} turns no test red"
