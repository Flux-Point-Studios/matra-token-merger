"""Mutation test for services.chain_check: every probe in
tests/test_chain_check.py against one broken copy of the module at a time.

Each ``raise`` of CosignRejected or ChainUnavailable is a check; a mutant
replaces one of them with ``pass``. Changed conditions and a reordering are
listed below. A mutant that still answers every probe as expected is a check
nothing notices, and this test names it."""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

import services.chain_check as chain_check
from tests.test_chain_check import PROBES, outcome

SOURCE_PATH = Path(chain_check.__file__)
SOURCE = SOURCE_PATH.read_text()
CHECKS = ("CosignRejected", "ChainUnavailable")


def _is_check(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id in CHECKS
    )


class _DeleteCheck(ast.NodeTransformer):
    def __init__(self, target: int | None) -> None:
        self.target = target
        self.seen: list[int] = []

    def visit_Raise(self, node: ast.Raise) -> ast.AST:
        if not _is_check(node):
            return node
        self.seen.append(node.lineno)
        if len(self.seen) - 1 == self.target:
            return ast.copy_location(ast.Pass(), node)
        return node


def _check_lines() -> list[int]:
    finder = _DeleteCheck(None)
    finder.visit(ast.parse(SOURCE))
    return finder.seen


CHECK_LINES = _check_lines()


def _load(code: ast.Module | str, name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(SOURCE_PATH)
    sys.modules[name] = module
    try:
        exec(compile(code, str(SOURCE_PATH), "exec"), module.__dict__)
    finally:
        del sys.modules[name]
    return module


def _history_asked_before_the_inputs(source: str) -> str:
    inputs = source.index("    for tx_id in sorted(")
    history = source.index("    # A query of its own")
    return source[:inputs] + source[history:] + "\n" + source[inputs:history]


OPERATOR_MUTANTS = {
    "the_pins_own_slot_read_as_after_it": ("tx_slot > slot", "tx_slot >= slot"),
    "the_slot_after_the_pin_read_as_inside_it": ("tx_slot > slot", "tx_slot > slot + 1"),
    "fungible_histories_asked_too": (
        "cfg.redeemable_nfts.intersection(approval.units)", "approval.units",
    ),
    "only_the_last_entry_read": ("for event in history:", "for event in history[-1:]:"),
    "a_history_hash_matched_by_its_prefix": ("_TX_HASH.fullmatch(tx_hash)", "_TX_HASH.match(tx_hash)"),
    "a_bool_slot_read_as_a_number": ("type(slot) is not int", "not isinstance(slot, int)"),
    "a_float_slot_read_as_a_number": ("type(slot) is not int", "type(slot) not in (int, float)"),
    "only_a_missing_slot_refused": ("type(slot) is not int", "slot is None"),
    "the_whole_history_read_before_refusing": (
        "next(mints_and_burns_after(unit, cfg.supply_slot, chain), None)",
        "next(iter(list(mints_and_burns_after(unit, cfg.supply_slot, chain))), None)",
    ),
}


def _differences(module: types.ModuleType) -> list[str]:
    return [
        f"{p.name}: {p.expected} -> {got}"
        for p in PROBES
        if (got := outcome(p, module.confirm_on_chain)[0]) != p.expected
    ]


def test_the_unmutated_module_matches_every_probe():
    assert _differences(_load(SOURCE, "chain_check_unmutated")) == []


def test_every_check_is_found():
    assert len(CHECK_LINES) >= 10


@pytest.mark.parametrize("target", range(len(CHECK_LINES)), ids=[f"line{n}" for n in CHECK_LINES])
def test_deleting_the_check_turns_a_probe_red(target):
    tree = _DeleteCheck(target).visit(ast.parse(SOURCE))
    ast.fix_missing_locations(tree)
    assert _differences(_load(tree, f"chain_check_mutant_{target}")), (
        f"deleting the check on line {CHECK_LINES[target]} changes no probe outcome"
    )


@pytest.mark.parametrize("name", OPERATOR_MUTANTS)
def test_changing_the_condition_turns_a_probe_red(name):
    original, changed = OPERATOR_MUTANTS[name]
    assert SOURCE.count(original) == 1, f"{original!r} is no longer in chain_check"
    mutant = _load(SOURCE.replace(original, changed), f"chain_check_{name}")
    assert _differences(mutant), f"{name} changes no probe outcome"


def test_asking_the_history_before_the_inputs_turns_a_probe_red():
    mutant = _load(_history_asked_before_the_inputs(SOURCE), "chain_check_history_first")
    assert _differences(mutant), "asking the history first changes no probe outcome"
