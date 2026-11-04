"""The surrender API starts only with the script that locks its pool.

It spends the pool with the script its blueprint holds, so a blueprint whose
script does not hash to SURRENDER_SCRIPT_ADDRESS's payment credential, as after
a pool moves to fresh keys with only one of the two updated, would build
transactions the ledger refuses. Each case starts the service in a process of
its own, configured the way its host is."""

from __future__ import annotations

from pathlib import Path

from tests.test_signers_share_the_pin import SURRENDER_API, Hosts

ROOT = Path(__file__).resolve().parent.parent


def test_starts_with_the_script_of_its_pool(tmp_path, live_pool_blueprint):
    result = Hosts(tmp_path, live_pool_blueprint).start(SURRENDER_API)
    assert result.returncode == 0, result.stderr


def test_refuses_the_unapplied_blueprint(tmp_path):
    result = Hosts(tmp_path, ROOT / "onchain/claim_validator/plutus.json").start(SURRENDER_API)
    assert result.returncode != 0
    assert "does not hash to SURRENDER_SCRIPT_ADDRESS" in result.stderr


def test_refuses_another_pools_script(tmp_path, live_pool_blueprint):
    live_pool_blueprint.write_text(live_pool_blueprint.read_text().replace("f1eee828", "f1eee829", 1))
    result = Hosts(tmp_path, live_pool_blueprint).start(SURRENDER_API)
    assert result.returncode != 0
    assert "does not hash to SURRENDER_SCRIPT_ADDRESS" in result.stderr
