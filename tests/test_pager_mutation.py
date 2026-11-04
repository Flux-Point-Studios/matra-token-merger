"""Mutation test for services.pager: the order it delivers pages in and the
budget refusals page within (tests/test_pager.py's BUDGET_AND_ORDER), and the
grace after which signing pauses and what /health says of it (FAIL_CLOSED),
each run against a Pager with one method changed in one place. A mutant that
turns no test of its matrix red is a part of the pager nothing notices, and
this test names it."""

from __future__ import annotations

import pytest

from services.pager import Pager
from tests.mutation import recompiled
from tests.test_pager import BUDGET_AND_ORDER, FAIL_CLOSED

BUDGET_AND_ORDER_MUTANTS = {
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

FAIL_CLOSED_MUTANTS = {
    "signing_paused_at_the_grace_itself": ("_waited", "first_due > self._grace_s", "first_due >= self._grace_s"),
    "signing_paused_a_second_late": ("_waited", "first_due > self._grace_s", "first_due > self._grace_s + 1"),
    "the_newest_page_timed": ("_waited", "MIN(due_at)", "MAX(due_at)"),
    "a_page_timed_from_when_it_was_written": ("_waited", "MIN(due_at)", "MIN(created_at)"),
    "a_delivered_page_timed": ("_waited", "WHERE message_id IS NULL AND", "WHERE"),
    "a_page_not_yet_due_timed": ("_waited", 'due_at <= ?"', 'due_at <= ? + 3600"'),
    "the_pause_never_raised": ("require_delivered", "    if paused:\n", "    if False:\n"),
    "health_ok_while_signing_is_paused": ("health", '"paging_failed" if paused or ', '"paging_failed" if '),
    "health_ok_while_the_webhook_is_refused": ("health", " or self._webhook_refused is not None", ""),
    "health_never_names_the_pause": ("health", '"signing_paused": paused,', '"signing_paused": False,'),
    "health_counts_no_page": ("health", '"pages_owed": self.owed(),', '"pages_owed": 0,'),
    "a_refused_webhook_never_named": ("_send", "        self._webhook_refused = status\n", ""),
    "a_refused_webhook_named_for_good": ("_send", "        self._webhook_refused = None\n", ""),
    "any_refusal_taken_for_a_refused_webhook": ("_send", "status in WEBHOOK_REFUSED", "status >= 400"),
    "only_404_taken_for_a_refused_webhook": ("_send", "status in WEBHOOK_REFUSED", "status == 404"),
    "the_grace_set_ignored": ("__init__", "self._grace_s = grace_s", "self._grace_s = GRACE_S"),
    "a_grace_under_a_minute_kept": ("from_env", "MIN_GRACE_S <= int(grace)", "MIN_GRACE_S - 1 <= int(grace)"),
    "a_minute_refused": ("from_env", "MIN_GRACE_S <= int(grace)", "MIN_GRACE_S < int(grace)"),
    "a_grace_over_an_hour_kept": ("from_env", "int(grace) <= MAX_GRACE_S", "int(grace) <= MAX_GRACE_S + 1"),
    "an_hour_refused": ("from_env", "int(grace) <= MAX_GRACE_S", "int(grace) < MAX_GRACE_S"),
    "any_number_int_reads_kept": ("from_env", "not _GRACE.fullmatch(grace) or ", ""),
    "no_default_grace": ("from_env", "or str(GRACE_S)", 'or "0"'),
    "the_grace_never_passed_on": ("from_env", "cls(url, outbox, source, int(grace))", "cls(url, outbox, source)"),
}

MATRICES = {
    "budget_and_order": (BUDGET_AND_ORDER, BUDGET_AND_ORDER_MUTANTS),
    "fail_closed": (FAIL_CLOSED, FAIL_CLOSED_MUTANTS),
}


def _red(tmp_path, tests: list, replace: tuple | None) -> list[str]:
    """The ``tests`` that fail with ``replace`` (a method's name and its
    stand-in) on Pager, each in a directory of its own."""
    red = []
    for test in tests:
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


@pytest.mark.parametrize("matrix", MATRICES)
def test_the_unmutated_pager_turns_no_test_red(tmp_path, matrix):
    assert _red(tmp_path, MATRICES[matrix][0], None) == []


@pytest.mark.parametrize("matrix, name", [(matrix, name) for matrix, (_, mutants) in MATRICES.items()
                                          for name in mutants])
def test_every_mutant_turns_a_test_red(tmp_path, matrix, name):
    tests, mutants = MATRICES[matrix]
    method, original, changed = mutants[name]
    assert _red(tmp_path, tests, (method, recompiled(getattr(Pager, method), original, changed))), (
        f"{name} turns no test red"
    )
