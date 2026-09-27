"""Mutation test for services.cosign_policy: delete one check at a time.

Every ``raise CosignRejected(...)`` in the policy is a check. Each mutant
replaces exactly one of them with ``pass`` and re-runs the adversarial corpus
and the decoder probes against it. A check whose deletion no probe notices is
dead weight or untested; either way this test fails and names its line."""

from __future__ import annotations

import ast
import sys
import types
from functools import lru_cache
from pathlib import Path

import pytest

import services.cosign_policy as policy
from tests.cosign_cases import CASES, DECODE_ACCEPTED, DECODE_REFUSED, SPLIT_REFUSED

SOURCE_PATH = Path(policy.__file__)
SOURCE = SOURCE_PATH.read_text()


def _is_check(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "CosignRejected"
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


def _mutant(target: int) -> types.ModuleType:
    tree = _DeleteCheck(target).visit(ast.parse(SOURCE))
    ast.fix_missing_locations(tree)
    module = types.ModuleType(f"cosign_policy_mutant_{target}")
    module.__file__ = str(SOURCE_PATH)
    # dataclass() resolves the defining module through sys.modules.
    sys.modules[module.__name__] = module
    try:
        exec(compile(tree, str(SOURCE_PATH), "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module


@lru_cache(maxsize=1)
def _scenarios():
    return [(case, case.build()) for case in CASES]


def _outcome(call) -> str:
    try:
        call()
    except Exception as exc:  # the mutant's own CosignRejected class, or a crash
        return getattr(exc, "code", None) or f"crash:{type(exc).__name__}"
    return "accepted"


def _differences(module: types.ModuleType) -> list[str]:
    diffs = []
    for case, s in _scenarios():
        got = _outcome(lambda: module.evaluate_surrender(
            s.tx, s.language_views, s.parents, s.now_slot, s.cfg))
        if got != case.expected:
            diffs.append(f"{case.name}: {case.expected} -> {got}")
    for name, raw in DECODE_REFUSED:
        got = _outcome(lambda: module.decode(raw))
        if got != "cbor":
            diffs.append(f"decode {name}: cbor -> {got}")
    for name, raw in SPLIT_REFUSED:
        got = _outcome(lambda: module.split_tx(raw))
        if got != "cbor":
            diffs.append(f"split_tx {name}: cbor -> {got}")
    for raw, value in DECODE_ACCEPTED:
        decoded: list = []
        got = _outcome(lambda: decoded.append(module.decode(raw)))
        if got != "accepted" or decoded != [value]:
            diffs.append(f"decode {raw.hex()}: {value!r} -> {got} {decoded!r}")
    return diffs


def test_the_unmutated_policy_matches_every_probe():
    assert _differences(policy) == []


def test_every_check_is_found():
    assert len(CHECK_LINES) >= 40


@pytest.mark.parametrize("target", range(len(CHECK_LINES)), ids=[f"line{n}" for n in CHECK_LINES])
def test_deleting_the_check_turns_a_probe_red(target):
    assert _differences(_mutant(target)), (
        f"deleting the check on line {CHECK_LINES[target]} changes no probe outcome"
    )
