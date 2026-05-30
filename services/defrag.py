"""Fat-UTxO defrag planner for the cMATRA surrender API.

A "fat" UTxO bundles many native tokens with its ADA. To surrender an asset
living in such a UTxO, every *other* token in it must be returned as a change
output; a large token map pushes the surrender tx past the 16KB protocol limit
even at batch size 1. This planner splits one fat UTxO into several lean
outputs (<= ``max_tokens_per_output`` tokens each), all paid back to the owner,
so subsequent surrenders fit under the limit.

Pure: no pycardano, no network. The build path turns a :class:`DefragPlan`
into a real self-send transaction the owner signs (no admin co-sign, no
script, no pool). The one invariant that must never break is conservation —
every token appears in exactly one output with its full quantity, since these
are user funds.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class LeanOutput:
    assets: dict[str, int]  # unit hex (policy id + asset name) -> quantity
    min_lovelace: int  # protocol min-ADA for an output holding these assets


@dataclass
class DefragPlan:
    outputs: list[LeanOutput]
    total_output_lovelace: int  # sum of each output's min_lovelace
    n_tokens: int
    feasible: bool  # at least one token to split

    @property
    def n_outputs(self) -> int:
        return len(self.outputs)


def plan_defrag(
    assets: dict[str, int],
    *,
    max_tokens_per_output: int,
    min_ada_for: Callable[[int], int],
) -> DefragPlan:
    """Partition ``assets`` (unit hex -> quantity) into lean outputs of at most
    ``max_tokens_per_output`` distinct tokens each, paid back to the owner.

    ``min_ada_for(n_assets)`` returns the protocol min-ADA (lovelace) for an
    output carrying ``n_assets`` tokens; the caller supplies it so this module
    stays free of pycardano / protocol-param coupling and fully unit-testable.

    Tokens are partitioned in a deterministic (sorted-unit) order, so rebuilding
    the same UTxO yields the same plan. Conservation is total: the union of every
    output's assets equals ``assets`` exactly — no token is dropped or duplicated.
    """
    if max_tokens_per_output < 1:
        raise ValueError("max_tokens_per_output must be >= 1")

    units = sorted(assets)
    outputs: list[LeanOutput] = []
    for i in range(0, len(units), max_tokens_per_output):
        chunk = units[i : i + max_tokens_per_output]
        outputs.append(
            LeanOutput(
                assets={u: assets[u] for u in chunk},
                min_lovelace=min_ada_for(len(chunk)),
            )
        )

    return DefragPlan(
        outputs=outputs,
        total_output_lovelace=sum(o.min_lovelace for o in outputs),
        n_tokens=len(units),
        feasible=len(units) > 0,
    )
