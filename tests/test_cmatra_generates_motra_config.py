"""The registration validator anchors the cMATRA policy id in its script hash,
so the id it is built with must be the one the merger minted on each network:
the mainnet id scripts/sweep_params.env pins (tests/test_sweep_params.py holds
it equal to CMATRA_POLICY) and the preprod rehearsal's cMATRA policy."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from tests.cosign_cases import CMATRA_POLICY

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "onchain/cmatra_generates_motra"


def _config_policy(env: str) -> str:
    entry = tomllib.loads((PROJECT / "aiken.toml").read_text())["config"][env]["cmatra_policy"]
    assert entry["encoding"] == "hex"
    return entry["bytes"]


def test_the_default_build_anchors_the_mainnet_cmatra_policy():
    assert _config_policy("default") == CMATRA_POLICY.hex()


def test_the_preprod_build_anchors_the_rehearsal_cmatra_policy():
    rehearsal = json.loads((ROOT / "audit_pack/preprod/rehearsal_state.json").read_text())
    assert _config_policy("preprod") == rehearsal["cmatra_test"]["policy_id"]


def test_the_blueprint_takes_only_the_materios_chain_id():
    """Deployment applies exactly one parameter; the policy id is compiled in."""
    blueprint = json.loads((PROJECT / "plutus.json").read_text())
    titles = [validator["title"] for validator in blueprint["validators"]]
    assert titles == ["cmatra_generates_motra.cmatra_generates_motra.else"]
    assert [p["title"] for p in blueprint["validators"][0]["parameters"]] == ["materios_chain_id"]
