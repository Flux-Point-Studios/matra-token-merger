"""Mutation run for RUNBOOK.md's P11 and step 13 blocks against sim.py's API cases.

Each mutant drops or weakens one command or check in those blocks, and the
API cases then run the mutated blocks. A mutant every API case still accepts
is a line nothing depends on. Nothing is scored unless the unmutated runbook
passes every API case first.

    python ceremony/c1/runbook_mutation.py [--out DIR]
"""

from __future__ import annotations

import argparse
import functools
import sys
from pathlib import Path

import sim

MUTANTS = [
    ("P11 leaves T.skey writable", "  chmod 0400 T.skey\n", ""),
    ("13a keeps an earlier attempt's files", "rm -f claim.*\n", ""),
    ("13a sends no secret header", "curl -sS --fail-with-body -H @- ", "curl -sS --fail-with-body "),
    ("13b runs on after a failed check",
     "# 13b: check the claim, then T signs it\n( set -euo pipefail\n", "# 13b: check the claim, then T signs it\n(\n"),
    ("13b skips the txid comparison", '  test "$("$CLI" latest transaction txid', '  true || test "$("$CLI" latest transaction txid'),
    ("13b output check cannot fail", "  jq -e --arg pool", "  jq --arg pool"),
    ("13b allows outputs anywhere", "([.outputs[].address] - [$pool, $q, $t]) == []", "true"),
    ("13b ignores the new pool datum", "[.outputs[] | select(.address == $pool) | .datum] == [{constructor: 0, fields: []}]",
     "true"),
    ("13b ignores the payout", "([.outputs[] | select(.address == $t) | .amount[$cm] // {} | .[]] | add) == 925780", "true"),
    ("13b allows a mint", ".mint == null", "true"),
    ("13b allows a withdrawal", ".withdrawals == null", "true"),
    ("13b allows a certificate", ".certificates == null", "true"),
    ("13b allows collateral returned elsewhere", '(.["return collateral"].address // $t) == $t', "true"),
    ("13b posts the witness file, not the signed tx",
     '"$CLI" latest transaction sign --tx-file claim.unsigned.tx', '"$CLI" latest transaction witness --tx-body-file claim.unsigned.tx'),
    ("13c submits the unsigned build", "api submit-surrender claim.submit.json", "api submit-surrender claim.build.json"),
]


def run(out: Path) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """The API cases the unmutated runbook fails, and, only if there are none,
    each mutant with the API cases it fails (none: it survived)."""
    text = (sim.HERE / "RUNBOOK.md").read_text()
    sim.prepare(out)
    chain = sim.migration(sim.chain_client(live=False))
    captured: dict = {}
    api_claim, blocks = sim.api_claim, sim.runbook_blocks
    sim.api_claim = lambda *args: captured.setdefault("args", args)
    try:
        sim.claims(chain)
    finally:
        sim.api_claim = api_claim

    def failures(source: str) -> list[str]:
        sim.runbook_blocks = functools.partial(blocks, text=source)
        del sim.results[:]
        sim.api_claim(*captured["args"])
        return [r["case"] for r in sim.results if r["group"] == "API" and not r["ok"]]

    try:
        baseline = failures(text)
        if baseline:
            return baseline, []
        rows = []
        for name, old, new in MUTANTS:
            if text.count(old) != 1:
                raise ValueError(f"mutant {name!r} applies {text.count(old)} times, not once")
            rows.append((name, failures(text.replace(old, new))))
        return [], rows
    finally:
        sim.runbook_blocks = blocks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=sim.HERE / "out/mutation")
    baseline, rows = run(parser.parse_args(argv).out.resolve())
    print("\n--- mutation run ---")
    if baseline:
        print(f"VOID: the unmutated runbook fails {baseline}")
        return 1
    for name, failed in rows:
        print(f"{'KILLED  ' if failed else 'SURVIVED'} {name:48} {failed[0] if failed else ''}")
    survived = [name for name, failed in rows if not failed]
    print(f"\n{len(rows) - len(survived)}/{len(rows)} killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
