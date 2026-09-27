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
