"""scripts/sweep_params.env pins the AdminWithdraw sweep of the live pool, and
scripts/admin-sweep-ceremony.sh runs no stage until the params name the
custody address the remainder goes to.

The ceremony runs in a copy of scripts/ with no cardano-cli or aiken, so a
stage it reaches fails on its first tool; a refusal of the destination comes
before that and before the working directory is made."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pycardano import Address

from services.cosign_policy import slot_at
from tests.cosign_cases import ADMIN_1, ADMIN_2, CMATRA_POLICY, POOL_BECH32, key_address, throwaway_key
from tools.cardano_utils import posix_ms_to_slot
from tools.sweep_helpers import derive_sweep_validity_window

ROOT = Path(__file__).resolve().parent.parent
PARAMS = ROOT / "scripts/sweep_params.env"
CEREMONY = ROOT / "scripts/admin-sweep-ceremony.sh"
# A mainnet address that is neither an admin key nor on the deny-list.
CUSTODY = Address.from_primitive(key_address(throwaway_key(1))).encode()
# The ceremony's python3 is the one these tests run under.
PATH = f"{Path(sys.executable).parent}:{os.environ['PATH']}"


def _sourced(path: Path) -> dict[str, str]:
    """Every variable ``path`` sets, as bash sources it."""
    out = subprocess.run(
        ["bash", "-c", 'set -a; source "$1"; env -0', "_", str(path)],
        env={"PATH": os.environ["PATH"]}, capture_output=True, check=True,
    ).stdout
    return dict(item.split("=", 1) for item in out.decode().split("\0") if "=" in item)


PINNED = _sourced(PARAMS)


def test_the_sweep_spends_the_live_pool_under_its_admins():
    assert PINNED["CMATRA_POLICY_ID"] == CMATRA_POLICY.hex()
    assert PINNED["SURRENDER_POOL_ADDR"] == POOL_BECH32
    assert PINNED["SURRENDER_POOL_SCRIPT_HASH"] == Address.from_primitive(POOL_BECH32).payment_part.payload.hex()
    assert (PINNED["ADMIN_PKH_1"], PINNED["ADMIN_PKH_2"]) == (ADMIN_1.hex(), ADMIN_2.hex())


def test_the_deadline_slot_is_the_one_the_validator_compares_with():
    deadline_s = int(PINNED["CLAIM_DEADLINE_POSIX_MS"]) // 1000
    assert int(PINNED["CLAIM_DEADLINE_SLOT_MAINNET"]) == slot_at(deadline_s, "mainnet") == 204_344_109


def test_slot_conversion_agrees_with_the_chain():
    """The v2 mint was included at slot 187,585,545 at 2026-05-19T00:50:36Z."""
    assert posix_ms_to_slot(1_779_151_836_000, "mainnet") == 187_585_545


def test_the_sweep_is_valid_only_after_the_deadline():
    window = derive_sweep_validity_window(int(PINNED["CLAIM_DEADLINE_POSIX_MS"]), 60, 4, "mainnet")
    assert window.invalid_before == 204_344_109 + 60


def test_the_custody_destination_is_unset_until_custody_exists():
    assert PINNED["SWEEP_DEST_ADDR"] == ""


class Ceremony:
    """scripts/ copied to a scratch directory, with its params edited."""

    def __init__(self, tmp_path: Path) -> None:
        self.scripts = tmp_path / "scripts"
        self.scripts.mkdir()
        shutil.copy(CEREMONY, self.scripts)
        self.out_dir = tmp_path / "out"
        self.set_destination("")

    def set_destination(self, address: str) -> None:
        text = PARAMS.read_text().replace('SWEEP_DEST_ADDR=""', f'SWEEP_DEST_ADDR="{address}"')
        text = text.replace('REPO_ROOT="/home/deci/work/matra-merger-prep"', f'REPO_ROOT="{ROOT}"')
        (self.scripts / "sweep_params.env").write_text(text)

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.scripts / CEREMONY.name), "--out-dir", str(self.out_dir), *args],
            env={"PATH": PATH, "HOME": str(self.out_dir.parent)},
            capture_output=True, text=True, timeout=60,
        )


@pytest.fixture
def ceremony(tmp_path):
    return Ceremony(tmp_path)


@pytest.mark.parametrize("stage", ["apply", "preflight", "build", "sign-1", "submit"])
def test_no_stage_runs_while_the_destination_is_unset(ceremony, stage):
    result = ceremony.run(stage)
    assert result.returncode != 0
    assert "SWEEP_DEST_ADDR" in result.stderr
    assert not ceremony.out_dir.exists()


@pytest.mark.parametrize("destination,reason", [
    (lambda: PINNED["ADMIN_1_ADDR"], "admin_1"),
    (lambda: "addr_test1vz2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzerspjrlsz", "mainnet"),
    (lambda: "not-an-address", "mainnet"),
])
def test_a_destination_that_is_not_custody_is_refused(ceremony, destination, reason):
    ceremony.set_destination(destination())
    result = ceremony.run("apply")
    assert result.returncode != 0
    assert reason in result.stderr
    assert not ceremony.out_dir.exists()


def test_the_destination_comes_only_from_the_params(ceremony):
    ceremony.set_destination(CUSTODY)
    result = ceremony.run("--to", PINNED["ADMIN_1_ADDR"], "apply")
    assert result.returncode != 0
    assert "--to" in result.stderr
    assert not ceremony.out_dir.exists()


def test_a_custody_destination_lets_the_stage_run(ceremony):
    """The stage runs, and fails on the first thing this scratch copy lacks."""
    ceremony.set_destination(CUSTODY)
    result = ceremony.run("apply")
    assert "SWEEP_DEST_ADDR" not in result.stderr
    assert "Stage 0" in result.stdout
