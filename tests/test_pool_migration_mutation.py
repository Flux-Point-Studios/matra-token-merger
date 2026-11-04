"""Mutation test for tools.pool_migration against tests/test_pool_migration.py.

Each mutant disarms one ``raise MigrationRejected`` or makes one of the
targeted changes below. It runs the whole test file in a fresh interpreter
with the mutated module loaded as tools.pool_migration, so nothing else in
this process sees it. A mutant the file still passes is a check nothing
notices, and this test names it. The unmutated module, loaded the same way,
must pass first, or no mutant counts as killed."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "tools/pool_migration.py").read_text()
TESTS = ROOT / "tests/test_pool_migration.py"

TARGETED = [
    ("duplicate old constant accepted", "        if count != 1:\n", "        if count < 1:\n"),
    ("admin constants without the 581c header",
     'b"\\x58\\x1c" + admins[0], b"\\x58\\x1c" + admins[1],', "admins[0], admins[1],"),
    ("deadline constant without the 1b header",
     'b"\\x1b" + deadline_ms.to_bytes(8, "big")', 'deadline_ms.to_bytes(8, "big")'),
    ("constants not swapped", "        out[at:at + len(old)] = new\n", "        pass\n"),
    ("new admins applied in the other order",
     "_parameter_constants(new_admins, new_deadline_ms)", "_parameter_constants(new_admins[::-1], new_deadline_ms)"),
    ("old deadline applied to the new pool",
     "_parameter_constants(new_admins, new_deadline_ms)", "_parameter_constants(new_admins, old_deadline_ms)"),
    ("deadline upper bound dropped", "if not 10**12 <= deadline < 10**13:", "if not 10**12 <= deadline:"),
    ("deadline lower bound dropped", "if not 10**12 <= deadline < 10**13:", "if not deadline < 10**13:"),
    ("report: old admins from the new pair",
     '"old_admins": [k.hex() for k in plan.old_admins],', '"old_admins": [k.hex() for k in plan.new_admins],'),
    ("report: new admins from the old pair",
     '"new_admins": [k.hex() for k in plan.new_admins],', '"new_admins": [k.hex() for k in plan.old_admins],'),
    ("report: new deadline from the old one",
     '"new_deadline_ms": plan.new_deadline_ms,', '"new_deadline_ms": plan.old_deadline_ms,'),
    ("report: UTC of the old deadline",
     "datetime.fromtimestamp(plan.new_deadline_ms // 1000", "datetime.fromtimestamp(plan.old_deadline_ms // 1000"),
    ("collateral cap raised to 500 ADA",
     "MAX_TOTAL_COLLATERAL_LOVELACE = 5_000_000\n", "MAX_TOTAL_COLLATERAL_LOVELACE = 500_000_000\n"),
    ("fee payer not a required witness", "witness_keys=plan.signers | {fee_payer},", "witness_keys=plan.signers,"),
]

# Loads argv[1] as tools.pool_migration, then runs the test file argv[2].
RUNNER = textwrap.dedent("""
    import importlib.util, sys
    import tools
    spec = importlib.util.spec_from_file_location("tools.pool_migration", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    sys.modules["tools.pool_migration"] = tools.pool_migration = module
    spec.loader.exec_module(module)
    import pytest
    sys.exit(pytest.main(["-q", "-x", "-p", "no:cacheprovider", "-o", "addopts=", sys.argv[2]]))
""")


def _is_refusal(node: ast.AST) -> bool:
    return (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "MigrationRejected")


def _without_refusal(lineno: int) -> str:
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        for value in ast.iter_fields(node):
            if isinstance(value[1], list):
                for i, child in enumerate(value[1]):
                    if _is_refusal(child) and child.lineno == lineno:
                        value[1][i] = ast.copy_location(ast.Pass(), child)
    return ast.unparse(tree)


def _mutants() -> list[tuple[str, str]]:
    refusals = sorted(node.lineno for node in ast.walk(ast.parse(SOURCE)) if _is_refusal(node))
    mutants = [(f"refusal at line {line} disarmed", _without_refusal(line)) for line in refusals]
    for name, original, changed in TARGETED:
        assert SOURCE.count(original) == 1, f"{name}: {original!r} occurs {SOURCE.count(original)} times"
        mutants.append((name, SOURCE.replace(original, changed)))
    return mutants


def _passes(source: str, path: Path) -> bool:
    path.write_text(source)
    run = subprocess.run([sys.executable, "-c", RUNNER, str(path), str(TESTS)], cwd=ROOT, check=False,
                         capture_output=True, text=True, timeout=600)
    return run.returncode == 0


def test_every_mutant_of_the_migration_tool_is_killed(tmp_path):
    assert _passes(SOURCE, tmp_path / "unmutated.py"), "the unmutated module fails when loaded like a mutant"
    mutants = _mutants()
    assert len(mutants) >= 75
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        passed = list(pool.map(lambda pair: _passes(pair[1][1], tmp_path / f"mutant{pair[0]}.py"),
                               enumerate(mutants)))
    assert [name for (name, _), survived in zip(mutants, passed, strict=True) if survived] == []
