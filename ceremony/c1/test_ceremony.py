"""The C1 ceremony material, exercised.

The simulation runs offline against the chain reads recorded in chain/,
evaluate.py runs on the transactions it builds, and the mutation run drops
each check of RUNBOOK.md's shell blocks in turn. Needs aiken v1.1.21 and
cardano-cli 11.0.0.0 (the c1-ceremony CI step installs both).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import runbook_mutation
import sim
from cbor2 import CBORTag

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


@pytest.fixture(scope="module")
def simulated(tmp_path_factory) -> tuple[Path, list[dict]]:
    out = tmp_path_factory.mktemp("c1-sim")
    return out, sim.run(out)


def evaluate(snapshot: Path, tx: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / "evaluate.py"), str(snapshot), str(tx)],
                          cwd=cwd, check=False, capture_output=True, text=True, timeout=180)


def test_every_simulated_case_comes_out_as_expected(simulated):
    _, results = simulated
    assert [r["case"] for r in results if not r["ok"]] == []
    assert {r["group"] for r in results} == {"LIVE", "CLAIM", "API", "REHEARSAL"}
    assert len(results) >= 67


def test_evaluate_passes_the_slice_run_from_outside_the_repo(simulated, tmp_path):
    out, _ = simulated
    run = evaluate(out / "snapshot-live.json", out / "C1a.unsigned.tx", tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.stderr.split()[-1] == "PASS"


def test_evaluate_fails_the_slice_without_old_admin_2(simulated, tmp_path):
    out, _ = simulated
    snap = sim.pm.load_snapshot(json.loads((out / "snapshot-live.json").read_text()))
    body, witnesses = sim.pm.migration_parts(snap, sim.pm.load_plan(json.loads((out / "plan-C1a.json").read_text())))
    body[14] = CBORTag(258, [key for key in body[14].value if key.hex() != sim.OLD_ADMINS[1]])
    broken = sim.write_envelope(tmp_path / "broken.tx", sim.pm.encode_tx(body, witnesses), "C1a without old admin_2")
    run = evaluate(out / "snapshot-live.json", broken, tmp_path)
    assert run.returncode == 1
    assert run.stderr.split()[-1] == "FAIL"


def test_every_runbook_mutant_is_killed(tmp_path):
    baseline, rows = runbook_mutation.run(tmp_path)
    assert baseline == []
    assert len(rows) == len(runbook_mutation.MUTANTS)
    assert [name for name, failed in rows if not failed] == []


def test_tracked_ceremony_files_name_no_session_scratch_location():
    tracked = subprocess.run(["git", "-c", f"safe.directory={REPO}", "-C", str(REPO), "ls-files", "ceremony/c1"],
                             check=True, capture_output=True, text=True).stdout.split()
    assert "ceremony/c1/RUNBOOK.md" in tracked
    scratch = re.compile("|".join(("/" + "tmp/", "scratch" + "pad", "wt-" + "c1", "claude-" + "1000")))
    assert [name for name in tracked if scratch.search((REPO / name).read_text())] == []


def test_signer_b_egress_lets_its_pager_reach_discord():
    # The pager fails closed: a co-signer whose pages cannot leave stops signing PAGER_GRACE_S after its first claim.
    text = (HERE / "RUNBOOK.md").read_text()
    egress = next(line for line in text.splitlines() if "Egress from signer-b" in line)
    assert "discord.com" in egress


def test_the_test_claim_waits_for_both_pagers_to_deliver():
    # Step 13's claim passes inside the grace even when a pager cannot deliver, so only a delivered page proves it can.
    text = (HERE / "RUNBOOK.md").read_text()
    step = text[text.index("# After 13c"):text.index("14. Stop the services")]
    for needed in ('"$API/health"', '"$COSIGNER/health"', "COSIGNER_URL", "pages_owed == 0", "webhook_refused == null"):
        assert needed in step, needed


def test_every_repo_path_the_runbook_names_exists():
    text = (HERE / "RUNBOOK.md").read_text()
    named = set(re.findall(r"`((?:ceremony|tools|services|onchain|scripts|tests)/[\w./-]*\w)`", text))
    assert "ceremony/c1/evaluate.py" in named
    assert sorted(path for path in named if not (REPO / path).exists()) == []
