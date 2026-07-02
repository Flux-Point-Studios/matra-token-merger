"""No-device HW acceptance gate (task #485).

Runs ``scripts/hw_interop_gate.mjs`` — the exact CIP-21 canonicalization
pipeline every hardware-wallet integration executes (cardano-hw-interop-lib,
the Ledger/Trezor layer inside Eternl/Lace/Typhon/NuFi) — against a real
built surrender tx. The gate demands:

  1. validateTx == []           (zero errors, fixable OR unfixable — any
                                 FIXABLE error means some wallet transforms
                                 the tx and signs a different body hash)
  2. encodeTx(decodeTx) == tx   (byte round-trip)
  3. transformTxBody identity   (the canonicalizer is a no-op, so the device
                                 signs exactly the hash the admins signed)

The mixed-framing fixture is the negative control: the gate must reject it,
proving the harness discriminates rather than rubber-stamps.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("NETWORK", "mainnet")
os.environ.setdefault("SURRENDER_API_SECRET", "test-secret")

import unittest.mock as mock  # noqa: E402

from pycardano import (  # noqa: E402
    Address,
    Asset,
    AssetName,
    MultiAsset,
    Network,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.hash import (  # noqa: E402
    ScriptHash as PycScriptHash,
    VerificationKeyHash,
)

import services.surrender_api as api  # noqa: E402
from tests.test_surrender_redeemer_index import _FakeContext  # noqa: E402
from tests.test_tag_258_transform import (  # noqa: E402
    MIXED_TX_HEX,
    build_real_surrender_tx,
)

GATE_SCRIPT = _PROJECT_ROOT / "scripts" / "hw_interop_gate.mjs"
GATE_DIR = _PROJECT_ROOT / "scripts" / "hw-gate"


@pytest.fixture(scope="module")
def node_bin() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed — the HW interop gate requires Node.js")
    assert GATE_SCRIPT.exists(), f"{GATE_SCRIPT} missing"
    if not (GATE_DIR / "node_modules" / "cardano-hw-interop-lib").is_dir():
        npm = shutil.which("npm")
        if npm is None:
            pytest.skip("npm not installed — cannot vendor cardano-hw-interop-lib")
        subprocess.run(
            [npm, "ci", "--prefix", str(GATE_DIR)],
            check=True, capture_output=True, text=True,
        )
    return node


def _run_gate(node: str, tx_hex: str, tmp_path: Path) -> subprocess.CompletedProcess:
    tx_file = tmp_path / "tx.hex"
    tx_file.write_text(tx_hex)
    return subprocess.run(
        [node, str(GATE_SCRIPT), str(tx_file)],
        capture_output=True, text=True, timeout=60,
    )


def test_gate_passes_on_tag258_build(node_bin, tmp_path):
    tx_hex, _ = build_real_surrender_tx(tag_sets_on=True)
    proc = _run_gate(node_bin, tx_hex, tmp_path)
    assert proc.returncode == 0, (
        f"HW interop gate failed:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "FAIL" not in proc.stdout


def test_gate_rejects_legacy_mixed_framing(node_bin, tmp_path):
    proc = _run_gate(node_bin, MIXED_TX_HEX, tmp_path)
    assert proc.returncode == 1, (
        f"gate rubber-stamped the known-bad mixed tx:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "FAIL" in proc.stdout


def test_gate_passes_on_defrag_build(node_bin, tmp_path):
    # The defrag path skips the canonicalization surgery, so pycardano 0.18.0
    # emits its inputs tag-258 natively and the empty witness set carries no
    # framing — already uniform. Verify rather than assume.
    user_addr = Address(
        VerificationKeyHash(bytes.fromhex("22" * 28)), network=Network.TESTNET
    )
    fat = MultiAsset()
    fat[PycScriptHash(bytes.fromhex("cd" * 28))] = Asset(
        {AssetName(f"tok{i:02d}".encode()): 1 for i in range(30)}
    )
    utxos = [
        UTxO(
            TransactionInput.from_primitive([bytes.fromhex("aa" * 32), 0]),
            TransactionOutput(user_addr, Value(50_000_000, fat)),
        ),
        UTxO(
            TransactionInput.from_primitive([bytes.fromhex("bb" * 32), 0]),
            TransactionOutput(user_addr, Value(100_000_000)),
        ),
    ]
    ctx = _FakeContext(utxos)

    class _BF:
        project_id = "x"
        base_url = "https://cardano-mainnet.blockfrost.io/api/v0"

    api.state.bf = _BF()
    with mock.patch.object(api, "BlockFrostChainContext", lambda **kw: ctx):
        tx_hex, _hash, _ref, _ntok, _nout, _size = api._build_defrag_tx_blocking(
            user_addr.encode(), None, 20
        )
    proc = _run_gate(node_bin, tx_hex, tmp_path)
    assert proc.returncode == 0, (
        f"HW interop gate failed on defrag tx:\n{proc.stdout}\n{proc.stderr}"
    )
