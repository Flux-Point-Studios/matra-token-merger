"""Each signer runs as its own service user from a root-owned release, with
its env file, key and ledger under paths only that user (or root) can enter.

services/deploy/install-signer.sh lays the paths out from one commit and
installs the units in services/deploy/systemd/; the c1-ceremony runbook's C0
shell and section 6 name the same paths. The CI step signer-layout runs the
script as root and starts both services dark under their users."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "services/deploy"
INSTALL = DEPLOY / "install-signer.sh"
LOCK = DEPLOY / "requirements-cosigner.txt"

SIGNERS = {
    # role: unit, app module, env file, port
    "a1": ("surrender-api.service", "services.surrender_api", "env.surrender-api", "8420"),
    "a2": ("cmatra-cosigner.service", "services.cosigner_api", "env.cosigner", "8421"),
}


def unit(role: str) -> dict[str, list[str]]:
    """The [Service] settings of a signer's unit, each with every value it is given."""
    settings: dict[str, list[str]] = {}
    for line in (DEPLOY / "systemd" / SIGNERS[role][0]).read_text().splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, value = line.split("=", 1)
            settings.setdefault(key, []).append(value)
    return settings


def first_party_closure(module: str) -> set[str]:
    """The top-level third-party modules ``module`` and the repo modules it
    imports pull in, transitively."""
    seen, third_party, todo = set(), set(), [module]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = ROOT / (name.replace(".", "/") + ".py")
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level
                     else [])
            for imported in names:
                top = imported.split(".")[0]
                if top in ("services", "tools"):
                    if (ROOT / (imported.replace(".", "/") + ".py")).exists():
                        todo.append(imported)
                elif top not in sys.stdlib_module_names and top != "__future__":
                    third_party.add(top)
    return third_party


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked() -> set[str]:
    return {normalized(m.group(1)) for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", LOCK.read_text(), re.MULTILINE)}


@pytest.mark.parametrize("role", SIGNERS)
def test_the_lock_installs_everything_the_service_imports(role):
    distributions = metadata.packages_distributions()
    needed = {normalized(dist) for top in first_party_closure(SIGNERS[role][1]) for dist in distributions[top]}
    assert needed - locked() == set()


@pytest.mark.parametrize("role", SIGNERS)
def test_the_unit_runs_the_service_as_its_own_user_from_the_release(role):
    _, module, env_file, port = SIGNERS[role]
    service = unit(role)
    user = f"cmatra-{role}"
    assert service["User"] == [user] and service["Group"] == [user]
    assert service["WorkingDirectory"] == [f"/opt/{user}/app"]
    assert service["EnvironmentFile"] == [f"/etc/{user}/{env_file}"]
    exec_start = service["ExecStart"][0].split()
    assert exec_start[:2] == [f"/opt/{user}/app/.venv/bin/uvicorn", f"{module}:app"]
    assert exec_start[exec_start.index("--port") + 1] == port


@pytest.mark.parametrize("role", SIGNERS)
def test_the_unit_hides_home_and_lets_the_service_write_only_its_ledger(role):
    service = unit(role)
    for setting, value in {"ProtectHome": "yes", "ProtectSystem": "strict", "NoNewPrivileges": "yes",
                           "PrivateTmp": "yes", "UMask": "0077", "CapabilityBoundingSet": ""}.items():
        assert service.get(setting) == [value], setting
    assert service["ReadWritePaths"] == [f"/var/lib/cmatra-{role}/ledger"]
    assert not any("/home" in value for values in service.values() for value in values)


def test_the_units_differ_only_in_what_makes_each_signer_itself():
    a1, a2 = (unit(role) for role in SIGNERS)
    own = {"Description", "User", "Group", "WorkingDirectory", "EnvironmentFile", "ExecStart", "ReadWritePaths",
           "SyslogIdentifier", "After"}
    assert {key: value for key, value in a1.items() if key not in own} == \
           {key: value for key, value in a2.items() if key not in own}


def test_the_install_script_is_valid_bash_and_refuses_an_unknown_role():
    assert subprocess.run(["bash", "-n", str(INSTALL)], check=False, capture_output=True).returncode == 0
    run = subprocess.run(["bash", str(INSTALL), "a3"], check=False, capture_output=True, text=True)
    assert run.returncode == 1 and "usage" in run.stderr


@pytest.mark.parametrize("role", SIGNERS)
def test_the_install_script_installs_the_unit_and_never_starts_it(role):
    script = INSTALL.read_text()
    assert "services/deploy/systemd/$unit" in script
    assert not re.search(r"systemctl\s+(--now\s+)?(start|enable|restart|reload-or-restart)\b", script)
    assert SIGNERS[role][0] in script and SIGNERS[role][2] in script

