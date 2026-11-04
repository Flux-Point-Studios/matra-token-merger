"""Mutation test for the order services.pager delivers pages in and the budget
refusals page within: every test in tests/test_pager.py's BUDGET_AND_ORDER
against a Pager with one method changed in one place. A mutant that turns no
test red is a part of the order or the budget nothing notices, and this test
names it."""

from __future__ import annotations

import pytest

from services.pager import Pager
from tests.mutation import recompiled
from tests.test_pager import BUDGET_AND_ORDER

MUTANTS = {
    "pages_sent_in_the_order_owed": ("deliver", "ORDER BY priority, id", "ORDER BY id"),
    "a_surrender_page_owed_as_a_refusal": ("page", "SURRENDER", "REFUSAL"),
    "a_refusal_page_owed_as_a_surrender": (
        "refusal", "self._owe(text, REFUSAL, now)", "self._owe(text, SURRENDER, now)",
    ),
    "a_page_not_yet_due_sent": ("deliver", 'AND due_at <= ?"', 'AND due_at <= ? + 3600"'),
    "a_page_sent_a_moment_after_it_is_due": ("deliver", 'AND due_at <= ?"', 'AND due_at < ?"'),
    "a_page_counted_owed_a_moment_after_it_is_due": ("owed", "due_at <= ?", "due_at < ?"),
    "no_wait_for_the_page_due_next": ("deliver", "return None if later is None else later - now", "return None"),
    "the_sender_not_woken_for_a_new_page": ("_owe", "    self._wake.set()\n", ""),
    "a_repeated_subject_paged_again": ("_first_this_hour", "    if key in self._repeats:\n", "    if False:\n"),
    "subjects_remembered_without_bound": (
        "_first_this_hour", " and len(self._repeats) < REMEMBERED_SUBJECTS", "",
    ),
    "a_subject_remembered_past_the_hour": ("_first_this_hour", "now - at < REPEAT_S", "now - at <= REPEAT_S"),
    "one_refusal_page_more_an_hour": (
        "refusal", "hour.paged < REFUSAL_PAGES_PER_HOUR", "hour.paged <= REFUSAL_PAGES_PER_HOUR",
    ),
    "a_new_hour_started_a_moment_late": ("refusal", "now - hour.start >= REPEAT_S", "now - hour.start > REPEAT_S"),
    "the_refusals_only_logged_not_counted": ("refusal", "        hour.logged += 1\n", ""),
    "the_count_page_not_brought_up_to_date": (
        "refusal", "UPDATE pages SET text = ? WHERE id = ?", "UPDATE pages SET text = text WHERE ? AND id = ?",
    ),
    "the_count_page_due_at_once": (
        "refusal", "self._owe(count, REFUSAL, hour.start + REPEAT_S)", "self._owe(count, REFUSAL, now)",
    ),
    "every_refusal_past_the_budget_waking_the_sender": (
        "refusal", "(count, hour.count_page))\n", "(count, hour.count_page))\n            self._wake.set()\n",
    ),
}


def _red(tmp_path, replace: tuple | None) -> list[str]:
    """The tests that fail with ``replace`` (a method's name and its stand-in)
    on Pager, each in a directory of its own."""
    red = []
    for test in BUDGET_AND_ORDER:
        where = tmp_path / test.__name__
        where.mkdir()
        with pytest.MonkeyPatch.context() as mp:
            if replace is not None:
                mp.setattr(Pager, *replace)
            try:
                test(where, mp)
            except (Exception, pytest.fail.Exception):
                red.append(test.__name__)
    return red


def test_the_unmutated_pager_turns_no_test_red(tmp_path):
    assert _red(tmp_path, None) == []


@pytest.mark.parametrize("name", MUTANTS)
def test_every_mutant_turns_a_test_red(tmp_path, name):
    method, original, changed = MUTANTS[name]
    assert _red(tmp_path, (method, recompiled(getattr(Pager, method), original, changed))), (
        f"{name} turns no test red"
    )
