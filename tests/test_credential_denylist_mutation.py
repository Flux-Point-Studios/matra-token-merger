"""Mutation test for tools.credential_denylist: every probe in
tests/test_credential_denylist.py against one broken copy of the module at a
time.

Each ``raise`` is a check; a mutant replaces one of them with ``pass``.
Changed conditions are listed below. A mutant whose every probe answers as the
module does is a check nothing notices, and this test names it."""

from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path

import pytest

import tools.credential_denylist as denylist
from tests.test_credential_denylist import (
    COMMITTED,
    DENIALS,
    LOADER_ONLY,
    MALFORMED,
    UNREADABLE,
    _address,
)

SOURCE_PATH = Path(denylist.__file__)
SOURCE = SOURCE_PATH.read_text()

OPERATOR_MUTANTS = {
    "stake_half_dropped": ("return address[1:29], address[29:57]", "return (address[1:29],)"),
    "payment_half_dropped": ("return address[1:29], address[29:57]", "return (address[29:57],)"),
    "base_length_unchecked": ("len(address) == 57", "len(address) >= 57"),
    "enterprise_length_unchecked": ("len(address) == 29", "len(address) >= 29"),
    "version_true_read_as_one": ('type(doc["version"]) is not int', 'not isinstance(doc["version"], int)'),
    "blank_text_accepted": ("not value.strip()", "not value"),
    "denied_and_allowed_unchecked": ("allowed & labels.keys()", "allowed & set()"),
}

# Extra probes for lengths only a mutant miscounts.
LONG = {
    "base_two_bytes_long": _address(0x01, b"\x01" * 28, b"\x02" * 30),
    "enterprise_two_bytes_long": _address(0x61, b"\x01" * 30),
}


def _outcome(call) -> str:
    try:
        value = call()
    except Exception as exc:
        return type(exc).__name__
    if isinstance(value, denylist.DenyList) or type(value).__name__ == "DenyList":
        return f"list {len(value.labels)} {len(value.allowed)} {value.sha256}"
    return repr(value)


def _outcomes(module: types.ModuleType, tmp: Path) -> dict[str, str]:
    out = {}
    for name, doc in {**MALFORMED, **LOADER_ONLY}.items():
        path = tmp / f"{name}.json"
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        out[f"load {name}"] = _outcome(lambda: module.load_denylist(path))
    out["load committed"] = _outcome(lambda: module.load_denylist(COMMITTED))
    for name, address in {**UNREADABLE, **LONG}.items():
        out[f"credentials {name}"] = _outcome(lambda: module.address_credentials(address))
    out["credentials pointer"] = _outcome(
        lambda: module.address_credentials(_address(0x41, b"\x01" * 28, b"\x81\x00\x01")))
    for name, (address, _) in DENIALS.items():
        out[f"denied {name}"] = _outcome(lambda: module.load_denylist(COMMITTED).denied(address))
    return out


def _is_check(node: ast.AST) -> bool:
    return isinstance(node, ast.Raise) and node.exc is not None


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


def test_the_unmutated_module_answers_every_probe_as_its_tests_expect(tmp_path):
    out = _outcomes(denylist, tmp_path)
    for name in {**MALFORMED, **LOADER_ONLY}:
        assert out[f"load {name}"] == "DenyListError", name
    for name in {**UNREADABLE, **LONG}:
        assert out[f"credentials {name}"] == "ValueError", name
    assert out["credentials pointer"] == "PointerAddress"
    for name, (_, denied) in DENIALS.items():
        assert (out[f"denied {name}"] != "None") is denied, name
    assert out["load committed"].startswith("list 61 2 ")


def test_every_check_is_found():
    assert len(CHECK_LINES) >= 14


@pytest.mark.parametrize("target", range(len(CHECK_LINES)), ids=[f"line{n}" for n in CHECK_LINES])
def test_deleting_the_check_turns_a_probe_red(target, tmp_path):
    tree = _DeleteCheck(target).visit(ast.parse(SOURCE))
    ast.fix_missing_locations(tree)
    mutant = _load(tree, f"credential_denylist_mutant_{target}")
    assert _outcomes(mutant, tmp_path) != _outcomes(denylist, tmp_path), (
        f"deleting the check on line {CHECK_LINES[target]} changes no probe outcome"
    )


@pytest.mark.parametrize("name", OPERATOR_MUTANTS)
def test_changing_the_condition_turns_a_probe_red(name, tmp_path):
    original, changed = OPERATOR_MUTANTS[name]
    assert SOURCE.count(original) == 1, f"{original!r} is no longer in the module"
    mutant = _load(SOURCE.replace(original, changed), f"credential_denylist_{name}")
    assert _outcomes(mutant, tmp_path) != _outcomes(denylist, tmp_path), f"{name} changes no probe outcome"
