"""The co-signer host installs from a hash-pinned lock, at exactly the versions
this suite runs on (CI installs the same lock before the dev extras)."""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "services/deploy/requirements-cosigner.txt"


def _pins() -> dict[str, tuple[str, int]]:
    """package -> (pinned version, number of hashes)."""
    pins: dict[str, tuple[str, int]] = {}
    current = None
    for line in LOCK.read_text().splitlines():
        pinned = re.match(r"^([A-Za-z0-9_.-]+)==(\S+)", line)
        if pinned:
            current = pinned.group(1).lower()
            pins[current] = (pinned.group(2), 0)
        elif "--hash=sha256:" in line:
            version, hashes = pins[current]
            pins[current] = (version, hashes + 1)
    return pins


def test_every_locked_package_is_pinned_by_hash():
    pins = _pins()
    assert {"pycardano", "cbor2", "fastapi", "pydantic", "pynacl", "uvicorn"} <= set(pins)
    assert all(hashes > 0 for _, hashes in pins.values())


def test_the_lock_pins_the_versions_these_tests_run_on():
    mismatched = {}
    for name, (version, _) in _pins().items():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        if installed != version:
            mismatched[name] = (version, installed)
    assert mismatched == {}


def _image_files() -> set[str]:
    """Every repo file Dockerfile.cosigner copies into the image."""
    copied: set[str] = set()
    for line in (ROOT / "services/deploy/Dockerfile.cosigner").read_text().splitlines():
        if line.startswith("COPY ") and "requirements-cosigner.txt" not in line:
            copied.update(line.split()[1:-1])
    return copied


def test_the_cosigner_image_carries_every_module_and_data_file_it_loads():
    """What the co-signer imports from this repo, and the data files it loads
    by default, as a process starting it finds them."""
    import subprocess
    import sys

    loaded = subprocess.run(
        [sys.executable, "-c",
         "import sys, services.cosigner_api as c, services.cosign_policy as p;"
         " print('\\n'.join([m.__file__ for m in list(sys.modules.values()) if getattr(m, '__file__', None)]"
         " + [str(p.DEFAULT_RATE_TABLE_PATH), str(p.DEFAULT_REDEMPTION_PIN_PATH), str(p.DEFAULT_DENYLIST_PATH)]))"],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=120,
    ).stdout.split()
    needed = {str(Path(f).relative_to(ROOT)) for f in loaded if Path(f).is_relative_to(ROOT)}
    assert "tools/credential_denylist.py" in needed and "audit_pack/2026-11-01/denylist.json" in needed
    assert needed <= _image_files()
