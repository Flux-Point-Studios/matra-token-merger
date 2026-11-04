"""Evaluate a C1 migration transaction offline with `aiken tx simulate`.

    python ceremony/c1/evaluate.py SNAPSHOT.json TX.envelope

Resolves the transaction's two inputs from the producing transactions the
snapshot holds (each already checked against its id by load_snapshot) and runs
the scripts the transaction carries. Exit 0 only if every script passes. Run
it on the unsigned transaction before anyone witnesses and on the assembled
one before submit, with the repo's Python environment and aiken v1.1.21 on
PATH or named by $AIKEN.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

import cbor2

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from services.cosign_policy import split_tx
from tools import pool_migration as pm

AIKEN_VERSION = "v1.1.21"

Resolved = list[tuple[tuple[bytes, int], bytes]]


@cache
def aiken_binary() -> str:
    """$AIKEN, else aiken on PATH; refused unless it is the audited v1.1.21."""
    path = os.environ.get("AIKEN") or shutil.which("aiken")
    if not path:
        raise SystemExit(f"aiken not found: put {AIKEN_VERSION} on PATH or name it in $AIKEN")
    version = subprocess.run([path, "--version"], check=True, capture_output=True, text=True).stdout
    if not re.search(rf"\b{re.escape(AIKEN_VERSION)}(?![\d.])", version):
        raise SystemExit(f"{path} is {version.strip()}, not aiken {AIKEN_VERSION}")
    return path


def raw_output(producing: bytes, index: int) -> bytes:
    """Output ``index`` of the transaction ``producing``, byte for byte."""
    fields = dict(pm._spans(split_tx(producing)[0], 5))
    return pm._spans(fields[1], 4)[index][1]


def simulate(tx: bytes, resolved: Resolved, stem: Path) -> subprocess.CompletedProcess:
    """`aiken tx simulate` over ``tx``, each input it spends resolved to its
    raw output. The three files aiken reads are left at ``stem``.tx/.in/.out."""
    resolved = sorted(resolved)
    files = [stem.with_name(f"{stem.name}.{ext}") for ext in ("tx", "in", "out")]
    files[0].write_text(tx.hex())
    files[1].write_text(cbor2.dumps([[ref[0], ref[1]] for ref, _ in resolved]).hex())
    files[2].write_text((bytes([0x80 + len(resolved)]) + b"".join(raw for _, raw in resolved)).hex())
    return subprocess.run([aiken_binary(), "tx", "simulate", *map(str, files)], check=False,
                          capture_output=True, text=True, timeout=120)


def main(snapshot_path: str, tx_path: str) -> int:
    doc = json.loads(Path(snapshot_path).read_text())
    pm.load_snapshot(doc)
    tx = bytes.fromhex(json.loads(Path(tx_path).read_text())["cborHex"])
    resolved = []
    for entry in doc["inputs"].values():
        ref = pm._ref(entry["ref"])
        resolved.append((ref, raw_output(bytes.fromhex(entry["producing_tx"]), ref[1])))
    with tempfile.TemporaryDirectory() as work:
        run = simulate(tx, resolved, Path(work, "c1"))
    print(run.stdout.strip() or run.stderr.strip())
    print("PASS" if run.returncode == 0 else "FAIL", file=sys.stderr)
    return 0 if run.returncode == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
