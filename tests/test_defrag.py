"""Tests for the fat-UTxO defrag planner (services/defrag.py).

A "fat" UTxO bundles many native tokens with its ADA. Surrendering an asset
that lives in such a UTxO forces every *other* token in it to be returned as a
change output, and a large token map pushes the surrender tx past the 16KB
protocol limit even at batch size 1. The defrag planner splits one fat UTxO
into several lean outputs (<= K tokens each), all paid back to the owner, so
subsequent surrenders fit.

The planner is pure (no pycardano / no network): it takes the fat UTxO's asset
map + a min-ADA estimator and returns the output partition. The one invariant
that matters most is conservation — never drop or duplicate a token, since
these are user funds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("NETWORK", "preprod")

from services.defrag import DefragPlan, LeanOutput, plan_defrag  # noqa: E402


def _assets(n: int) -> dict[str, int]:
    # n distinct units (28-byte policy + 1-byte name hex), qty = index+1
    return {f"{i:056x}{i:02x}": i + 1 for i in range(n)}


# A simple, monotonic min-ADA estimator for tests: 1 ADA base + 0.05 ADA/token.
def _min_ada(n_assets: int) -> int:
    return 1_000_000 + n_assets * 50_000


# ---------------------------------------------------------------------------
# Conservation — the load-bearing invariant
# ---------------------------------------------------------------------------


def test_every_token_preserved_exactly_once():
    assets = _assets(138)
    plan = plan_defrag(assets, max_tokens_per_output=20, min_ada_for=_min_ada)
    merged: dict[str, int] = {}
    for o in plan.outputs:
        for unit, qty in o.assets.items():
            assert unit not in merged, "token duplicated across outputs"
            merged[unit] = qty
    assert merged == assets, "defrag must conserve every token and quantity"


def test_no_output_exceeds_max_tokens():
    plan = plan_defrag(_assets(138), max_tokens_per_output=20, min_ada_for=_min_ada)
    assert all(len(o.assets) <= 20 for o in plan.outputs)
    # 138 / 20 -> 7 outputs (six of 20, one of 18)
    assert len(plan.outputs) == 7
    assert [len(o.assets) for o in plan.outputs] == [20, 20, 20, 20, 20, 20, 18]


def test_total_output_lovelace_is_sum_of_min_ada():
    plan = plan_defrag(_assets(45), max_tokens_per_output=20, min_ada_for=_min_ada)
    assert plan.outputs and len(plan.outputs) == 3
    assert plan.total_output_lovelace == sum(o.min_lovelace for o in plan.outputs)
    # each output's min_lovelace must match the estimator for its token count
    for o in plan.outputs:
        assert o.min_lovelace == _min_ada(len(o.assets))


def test_n_tokens_reported():
    plan = plan_defrag(_assets(45), max_tokens_per_output=20, min_ada_for=_min_ada)
    assert plan.n_tokens == 45


def test_single_chunk_when_under_cap():
    plan = plan_defrag(_assets(5), max_tokens_per_output=20, min_ada_for=_min_ada)
    assert len(plan.outputs) == 1
    assert len(plan.outputs[0].assets) == 5
    assert plan.feasible is True


def test_empty_assets_not_feasible():
    plan = plan_defrag({}, max_tokens_per_output=20, min_ada_for=_min_ada)
    assert plan.outputs == []
    assert plan.n_tokens == 0
    assert plan.feasible is False


def test_partition_is_deterministic():
    assets = _assets(50)
    p1 = plan_defrag(assets, max_tokens_per_output=20, min_ada_for=_min_ada)
    p2 = plan_defrag(assets, max_tokens_per_output=20, min_ada_for=_min_ada)
    units = lambda p: [sorted(o.assets) for o in p.outputs]
    assert units(p1) == units(p2)


def test_invalid_cap_rejected():
    with pytest.raises(ValueError):
        plan_defrag(_assets(5), max_tokens_per_output=0, min_ada_for=_min_ada)
