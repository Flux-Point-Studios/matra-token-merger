"""Mutation test for the pin's record of waived units already in quarantine:
the loader both signers use (tools.process_surrender) and the pin check and
pinning (scripts.pin_redemption).

Each mutant changes one piece of the arithmetic or of a check on the record,
in a copy of one function, and every record probe in
tests/test_surrendered_entitlement.py and tests/test_pin_redemption.py runs
against it, with the three pinning probes. A mutant that answers every probe
as expected is a change nothing notices, and this test names it."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

import scripts.pin_redemption as pin_redemption
import tools.process_surrender as process_surrender
from tests.mutation import recompiled
from tests.test_pin_redemption import (
    PIN_PATH,
    RECORD_PROBES as CHECK_PROBES,
    matches,
    record_outcome,
    recorded_units_left_the_reserve,
    recorded_units_still_in_the_reserve,
    repin,
    sample_record,
    with_the_record,
)
from tests.test_surrendered_entitlement import RECORD_PROBES as LOADER_PROBES, remaining_with

LOADER = (process_surrender, "load_redemption_pin")
RECORD = (process_surrender, "waived_already_quarantined")
DRIFT = (pin_redemption, "drift")
PROBLEMS = (pin_redemption, "already_quarantined_problems")
SENT = (pin_redemption, "sent_to_quarantine")
RESERVE = (pin_redemption, "waived_reserve")
PINNING = (pin_redemption, "main")

# name -> ((module, function), the piece of its source, what the mutant has instead)
MUTANTS: dict[str, tuple[tuple[ModuleType, str], str, str]] = {
    "the_record_ignored": (LOADER, '(row["waiver"] - already)', 'row["waiver"]'),
    "the_record_added_to_the_waiver": (LOADER, 'row["waiver"] - already', 'row["waiver"] + already'),
    "the_record_taken_off_the_quarantine_count_too": (
        LOADER, '- row["quarantined"])', '- (row["quarantined"] - already))',
    ),
    "no_floor_at_zero": (
        LOADER,
        'max(0, row["supply"] - (row["waiver"] - already) - row["quarantined"])',
        'row["supply"] - (row["waiver"] - already) - row["quarantined"]',
    ),
    "a_refused_record_read_as_none": (
        LOADER, 'raise ValueError(f"{path}: {asset.name} {unit}: {exc}") from exc', "already = 0",
    ),
    "a_null_record_read_as_none": (
        RECORD, 'if "waived_already_quarantined" not in row:', 'if row.get("waived_already_quarantined") is None:',
    ),
    "any_shape_accepted": (RECORD, "if not (", "if False and not ("),
    "a_field_besides_accepted": (
        RECORD, 'set(record) == {"quantity", "tx_hash"}', '{"quantity", "tx_hash"} <= set(record)',
    ),
    "true_read_as_a_quantity": (RECORD, 'type(record["quantity"]) is int', 'isinstance(record["quantity"], int)'),
    "a_float_read_as_a_quantity": (
        RECORD, 'type(record["quantity"]) is int', 'type(record["quantity"]) in (int, float)',
    ),
    "zero_accepted": (RECORD, 'record["quantity"] > 0', 'record["quantity"] >= 0'),
    "a_hash_matched_by_its_prefix": (RECORD, "re.fullmatch(", "re.match("),
    "an_uppercase_hash_accepted": (RECORD, "[0-9a-f]{64}", "[0-9a-fA-F]{64}"),
    "a_short_hash_accepted": (RECORD, "[0-9a-f]{64}", "[0-9a-f]{1,64}"),
    "no_bound_at_the_waiver": (RECORD, 'quantity > row["waiver"]', "False"),
    "the_waiver_itself_refused": (RECORD, 'quantity > row["waiver"]', 'quantity >= row["waiver"]'),
    "no_bound_at_the_quarantine_count": (RECORD, 'quantity > row["quarantined"]', "False"),
    "the_quarantine_count_itself_refused": (RECORD, 'quantity > row["quarantined"]', 'quantity >= row["quarantined"]'),
    "the_record_not_checked": (
        DRIFT, 'problems += already_quarantined_problems(asset, unit, row, doc["quarantine_slot"], bf)', "pass",
    ),
    "a_refused_record_not_reported": (PROBLEMS, 'return [f"{asset} {unit}: {exc}"]', "return []"),
    "an_unknown_transaction_not_reported": (PROBLEMS, "if answer is None:", "if False:"),
    "a_unit_with_no_record_checked_as_if_it_had_one": (PROBLEMS, "if not quantity:", "if False:"),
    "the_waived_reserve_not_checked": (PROBLEMS, 'if held > row["waiver"] - quantity:', "if False:"),
    "the_waived_reserve_held_to_the_waiver_alone": (
        PROBLEMS, 'held > row["waiver"] - quantity', 'held > row["waiver"]',
    ),
    "the_waived_reserve_allowed_one_unit_more": (
        PROBLEMS, 'held > row["waiver"] - quantity', 'held > row["waiver"] - quantity + 1',
    ),
    "the_record_left_in_the_waived_reserve_refused": (
        PROBLEMS, 'held > row["waiver"] - quantity', 'held >= row["waiver"] - quantity',
    ),
    "the_waived_reserve_held_to_exactly_the_record": (
        PROBLEMS, 'held > row["waiver"] - quantity', 'held != row["waiver"] - quantity',
    ),
    "the_waived_reserves_snapshot_not_checked": (PROBLEMS, 'if at_snapshot != row["waiver"]:', "if False:"),
    "only_the_first_waived_reserve_read": (
        RESERVE, 'for entry in reserve["addresses"]', 'for entry in reserve["addresses"][:1]',
    ),
    "any_unit_counted_at_the_waived_reserve": (RESERVE, ' if amount["unit"] == unit', ""),
    "a_later_transaction_accepted": (PROBLEMS, "slot > quarantine_slot", "False"),
    "the_quarantine_counts_own_slot_refused": (PROBLEMS, "slot > quarantine_slot", "slot >= quarantine_slot"),
    "fewer_accepted": (PROBLEMS, "sent < quantity", "False"),
    "exactly_the_record_refused": (PROBLEMS, "sent < quantity", "sent <= quantity"),
    "every_http_error_read_as_no_transaction": (
        SENT, "exc.response is not None and exc.response.status_code == 404", "True",
    ),
    "failed_scripts_counted": (SENT, 'tx["valid_contract"] is not True', "False"),
    "a_collateral_return_counted": (SENT, ' and out["collateral"] is False', ""),
    "any_address_counted": (SENT, 'out["address"] == QUARANTINE_ADDRESS and ', ""),
    "any_unit_counted": (SENT, ' if amount["unit"] == unit', ""),
    "only_the_largest_quarantine_output_counted": (SENT, "sum(", "max("),
    "the_record_not_written": (PINNING, "if token.name in WAIVED_ALREADY_QUARANTINED:", "if False:"),
    "a_pin_written_despite_problems": (PINNING, "if problems:", "if False:"),
}


def _outcome(run) -> object:
    try:
        return run()
    except Exception as exc:
        return f"crash:{type(exc).__name__}"


def _differences(tmp_path: Path) -> list[str]:
    """Every probe whose outcome is not the one it expects."""
    red = []
    for name, (record, changes, expected) in LOADER_PROBES.items():
        (tmp_path / name).mkdir()
        got = _outcome(lambda: remaining_with(tmp_path / name, record, changes))
        if got != expected:
            red.append(f"loader {name}: {expected} -> {got}")
    for p in CHECK_PROBES:
        if not matches(got := record_outcome(p), p.expected):
            red.append(f"check {p.name}: {p.expected} -> {got}")
    if _pinned(tmp_path / "unchanged") != PIN_PATH.read_text():
        red.append("pinning an unchanged chain no longer writes the committed pin")
    borne_out = _pinned(tmp_path / "borne_out", recorded_units_left_the_reserve, sample_record())
    if borne_out != with_the_record(json.loads(PIN_PATH.read_text())):
        red.append(f"pinning a record the chain bears out: {borne_out:.200}")
    refused = _pinned(tmp_path / "still_held", recorded_units_still_in_the_reserve, sample_record())
    if not (refused.startswith("refused: ") and "still holds" in refused):
        red.append(f"pinning a record the waived reserve still holds: {refused:.200}")
    return red


def _pinned(tmp_path: Path, *args) -> object:
    """``repin`` under a monkeypatch of its own, so the records one probe
    pins with never reach the next; a mutant's patch stays in place."""
    tmp_path.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        return _outcome(lambda: repin(tmp_path, mp, *args))


def test_the_unmutated_code_answers_every_probe(tmp_path):
    assert _differences(tmp_path) == []


@pytest.mark.parametrize("name", MUTANTS)
def test_every_mutant_turns_a_probe_red(tmp_path, name):
    """The mutant replaces the function wherever it is bound: the pin check
    calls the loader's own record check."""
    (module, function), original, changed = MUTANTS[name]
    real = getattr(module, function)
    mutant = recompiled(real, original, changed)
    with pytest.MonkeyPatch.context() as mp:
        for bound in (process_surrender, pin_redemption):
            if getattr(bound, function, None) is real:
                mp.setattr(bound, function, mutant)
        assert _differences(tmp_path), f"{name} turns no probe red"
