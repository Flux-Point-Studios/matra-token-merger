#!/usr/bin/env python3
"""
Admin Reclaim — post-deadline sweep of unclaimed cMATRA UTxOs.

Modes:
  --check-only: report unclaimed UTxOs at the script address
  --dry-run (default): build + dual-sign + assemble the tx but do NOT submit
  --submit:     build + dual-sign + assemble + broadcast

The on-chain claim_validator (onchain/claim_validator/validators/
claim_validator.ak) requires BOTH admin signatures for the AdminWithdraw
path, plus a validity range entirely after the deadline.

Signing flow (mirrors mint-ceremony.sh stage_sign2 — the surviving final
shape after 3 sec-review rounds + 1 runtime EXIT-trap race fix):

  1. pycardano builds the unsigned tx body locally.
  2. tx body is converted to a cardano-cli `.txbody.json` envelope (so
     remote cardano-cli can witness it directly without re-deriving CBOR).
  3. cardano-cli locally generates the admin_1 witness (admin_1.skey lives
     on Server A under file mode 0400).
  4. The tx body is uploaded via scp to admin_2's host (Server B). A
     single ssh invocation runs cardano-cli to witness it, then streams
     the witness JSON back via stdout, base64-encoded between
     =====WITNESS-BEGIN===== / =====WITNESS-END===== markers. The remote
     side cleans up its temp files via a `trap cleanup EXIT` running in
     the SAME session — no scp-vs-trap race like the broken pattern an
     earlier mint-ceremony.sh draft had.
  5. cardano-cli locally assembles the two witnesses + tx body into the
     final signed tx.
  6. (submit only) Blockfrost broadcasts the signed CBOR.

All shell errors surface as SshSignError with remote stderr included —
no catch-and-swallow.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cbor2
from pycardano import (
    Address,
    Asset,
    AssetName,
    BlockFrostChainContext,
    MultiAsset,
    PlutusV3Script,
    RawPlutusData,
    Redeemer,
    TransactionBuilder,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.hash import ScriptHash, TransactionId, VerificationKeyHash

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import (
    decode_claim_datum,
    encode_claim_datum,
    posix_ms_to_slot,
)
from tools.config import (
    ADMIN_1_SKEY_PATH,
    ADMIN_2_CARDANO_CLI_REMOTE,
    ADMIN_2_SKEY_REMOTE,
    ADMIN_2_SSH_HOST,
    ADMIN_PKH_1,
    ADMIN_PKH_2,
    CARDANO_CLI_LOCAL,
    CLAIM_DEADLINE_POSIX_MS,
    NETWORK,
    NETWORK_MAGIC_FLAG,
)

logger = logging.getLogger(__name__)

FLUX_ASSET_NAME_HEX = "634d41545241"  # hex("cMATRA")

# Markers used by the SSH stage_sign2 protocol — must match the remote
# bash snippet below byte-for-byte. Mirrors mint-ceremony.sh.
_WITNESS_BEGIN_MARKER = "=====WITNESS-BEGIN====="
_WITNESS_END_MARKER = "=====WITNESS-END====="


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SshSignError(RuntimeError):
    """Raised when any phase of the dual-admin signing pipeline fails.

    Carries the captured remote stderr (when applicable) so the operator
    can diagnose without re-running the ceremony.
    """


# ---------------------------------------------------------------------------
# Dual-admin configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DualAdminConfig:
    """Coordinates for the local + remote signing endpoints.

    Built once at the top of run_admin_reclaim() and threaded through all
    signing helpers so they don't depend on global env state — that keeps
    them unit-testable with mock subprocess.run.
    """

    admin_pkh_1: str
    admin_pkh_2: str
    admin_1_skey_path: str
    admin_2_ssh_host: str
    admin_2_skey_remote: str
    admin_2_cardano_cli_remote: str
    cardano_cli_local: str
    network_magic_flag: str

    def __post_init__(self) -> None:
        # Validate PKH shape — 28 bytes hex (56 chars), lowercase or upper.
        for name in ("admin_pkh_1", "admin_pkh_2"):
            v = getattr(self, name)
            if not isinstance(v, str) or len(v) != 56:
                raise ValueError(
                    f"{name} must be 56 hex chars (28 bytes), got len={len(v) if isinstance(v, str) else type(v).__name__}"
                )
            try:
                bytes.fromhex(v)
            except ValueError as e:
                raise ValueError(f"{name} is not valid hex: {e}") from e
        if self.admin_pkh_1.lower() == self.admin_pkh_2.lower():
            raise ValueError(
                "admin_pkh_1 and admin_pkh_2 must differ — using the same key "
                "on both servers defeats the dual-admin trust model"
            )
        if not self.admin_2_ssh_host:
            raise ValueError("admin_2_ssh_host is required (e.g. 'deci@192.168.0.133')")
        if not self.admin_1_skey_path:
            raise ValueError("admin_1_skey_path is required")

    @classmethod
    def from_env(cls) -> "DualAdminConfig":
        """Build a config from the tools.config env-loaded constants.

        Raises ValueError early if any required slot is unset — better to
        fail at config-load time than mid-ceremony.
        """
        if not ADMIN_PKH_1 or not ADMIN_PKH_2:
            raise ValueError(
                "ADMIN_PKH_1 and ADMIN_PKH_2 must both be set in env "
                "(see .env.example — dual-admin section)"
            )
        if not ADMIN_2_SSH_HOST:
            raise ValueError(
                "ADMIN_2_SSH_HOST is required for the reclaim ceremony "
                "(e.g. ADMIN_2_SSH_HOST=deci@192.168.0.133)"
            )
        return cls(
            admin_pkh_1=ADMIN_PKH_1,
            admin_pkh_2=ADMIN_PKH_2,
            admin_1_skey_path=ADMIN_1_SKEY_PATH,
            admin_2_ssh_host=ADMIN_2_SSH_HOST,
            admin_2_skey_remote=ADMIN_2_SKEY_REMOTE,
            admin_2_cardano_cli_remote=ADMIN_2_CARDANO_CLI_REMOTE,
            cardano_cli_local=CARDANO_CLI_LOCAL,
            network_magic_flag=NETWORK_MAGIC_FLAG,
        )


# ---------------------------------------------------------------------------
# UTxO discovery (unchanged from single-admin version)
# ---------------------------------------------------------------------------


def discover_unclaimed_utxos(
    bf: BlockfrostClient,
    script_address: str,
    flux_policy_hex: str,
) -> list[dict[str, Any]]:
    """Query script address for all current UTxOs (unclaimed)."""
    utxos = bf.get_address_utxos(script_address)
    results: list[dict[str, Any]] = []

    for u in utxos:
        flux_qty = 0
        ada_qty = 0
        for amt in u.get("amount", []):
            if amt["unit"] == "lovelace":
                ada_qty = int(amt["quantity"])
            elif amt["unit"].startswith(flux_policy_hex):
                flux_qty = int(amt["quantity"])

        datum_pkh = None
        inline_datum = u.get("inline_datum")
        if inline_datum:
            try:
                datum_pkh = decode_claim_datum(inline_datum)
            except (ValueError, KeyError) as e:
                logger.warning(
                    "Could not decode inline_datum at %s#%d: %s",
                    u["tx_hash"], u["output_index"], e,
                )

        results.append({
            "tx_hash": u["tx_hash"],
            "output_index": u["output_index"],
            "ada_lovelace": ada_qty,
            "flux_units": flux_qty,
            "datum_pkh": datum_pkh,
        })

    return results


# ---------------------------------------------------------------------------
# Build unsigned tx body
# ---------------------------------------------------------------------------


def build_reclaim_tx_body(
    bf: BlockfrostClient,
    cfg: DualAdminConfig,
    script_address: str,
    script_cbor_hex: str,
    unclaimed: list[dict[str, Any]],
    flux_policy_hex: str,
    deadline_posix_ms: int,
    *,
    tx_body_path: Path | None = None,
) -> tuple[bytes, str, Path]:
    """Build an UNSIGNED reclaim tx body via pycardano + cardano-cli envelope.

    Sets:
      - required_signers = [admin_pkh_1, admin_pkh_2]   (both required on chain)
      - validity_start = posix_ms_to_slot(deadline) + 1 (satisfies
        is_entirely_after(validity_range, deadline) on the AdminWithdraw path)

    Returns: (body_cbor_bytes, tx_hash_hex, tx_body_envelope_path)

    The envelope is a cardano-cli-compatible `.txbody.json`:
      {
        "type": "TxBodyConway",
        "description": "...",
        "cborHex": "..."
      }
    Both the local admin_1 witness call and the remote admin_2 witness
    call point at this file via `--tx-body-file`.
    """
    asset_name = AssetName(bytes.fromhex(FLUX_ASSET_NAME_HEX))
    script_addr = Address.from_primitive(script_address)
    flux_policy_id = ScriptHash(bytes.fromhex(flux_policy_hex))

    admin_addr = Address(
        payment_part=VerificationKeyHash(bytes.fromhex(cfg.admin_pkh_1)),
        network=script_addr.network,
    )
    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))

    context = BlockFrostChainContext(
        project_id=bf.project_id,
        base_url=bf.base_url,
    )
    builder = TransactionBuilder(context)

    for u in unclaimed:
        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(u["tx_hash"])),
            u["output_index"],
        )

        multi = MultiAsset()
        if u["flux_units"] > 0:
            multi[flux_policy_id] = Asset({asset_name: u["flux_units"]})
        value = Value(u["ada_lovelace"], multi)

        if u["datum_pkh"]:
            datum_cbor = encode_claim_datum(u["datum_pkh"])
            datum = RawPlutusData(cbor2.loads(datum_cbor))
        else:
            datum = RawPlutusData(cbor2.loads(b"\xd8\x79\x80"))

        utxo = UTxO(tx_in, TransactionOutput(script_addr, value, datum=datum))
        redeemer = Redeemer(RawPlutusData(cbor2.loads(b"\xd8\x79\x80")))

        builder.add_script_input(utxo, script=script, redeemer=redeemer)

    # CRITICAL: both admins are required signers (matches on-chain check)
    builder.required_signers = [
        VerificationKeyHash(bytes.fromhex(cfg.admin_pkh_1)),
        VerificationKeyHash(bytes.fromhex(cfg.admin_pkh_2)),
    ]

    # Validity start AFTER the deadline so is_entirely_after passes
    deadline_slot = posix_ms_to_slot(deadline_posix_ms, NETWORK)
    builder.validity_start = deadline_slot + 1

    # admin_1's address funds collateral + change
    builder.add_input_address(admin_addr)

    body = builder.build(change_address=admin_addr)
    body_cbor = body.to_cbor() if hasattr(body, "to_cbor") else b""
    body_cbor_bytes = body_cbor if isinstance(body_cbor, bytes) else bytes(body_cbor)
    tx_hash_hex = body.hash().payload.hex() if hasattr(body, "hash") else ""

    if tx_body_path is None:
        tx_body_path = Path("/tmp/admin-reclaim.txbody.json")

    envelope = {
        "type": "TxBodyConway",
        "description": "cMATRA admin_reclaim (AdminWithdraw) — dual-admin",
        "cborHex": body_cbor_bytes.hex(),
    }
    tx_body_path.parent.mkdir(parents=True, exist_ok=True)
    tx_body_path.write_text(json.dumps(envelope, indent=2))

    return body_cbor_bytes, tx_hash_hex, tx_body_path


# ---------------------------------------------------------------------------
# admin_1 local witness via cardano-cli
# ---------------------------------------------------------------------------


def sign_with_admin_1_local(
    cfg: DualAdminConfig,
    tx_body_path: Path,
    witness_out_path: Path,
) -> None:
    """Invoke local cardano-cli to produce admin_1's witness file.

    Raises SshSignError on non-zero exit, with captured stderr included.
    """
    network_args = cfg.network_magic_flag.split()
    argv = [
        cfg.cardano_cli_local,
        "latest",
        "transaction",
        "witness",
        "--tx-body-file",
        str(tx_body_path),
        "--signing-key-file",
        cfg.admin_1_skey_path,
        *network_args,
        "--out-file",
        str(witness_out_path),
    ]
    logger.info("admin_1 local witness: %s", " ".join(argv))
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SshSignError(
            f"admin_1 local witness failed: rc={result.returncode}\n"
            f"stderr={result.stderr.strip()}\n"
            f"stdout={result.stdout.strip()}"
        )


# ---------------------------------------------------------------------------
# admin_2 remote witness via single-session SSH (stage_sign2 pattern)
# ---------------------------------------------------------------------------


# Remote bash snippet — kept byte-identical to mint-ceremony.sh stage_sign2:
#   - `set -euo pipefail` so any cardano-cli error fails the whole session.
#   - trap-cleanup at EXIT shreds /tmp/admin-reclaim.* even on partial failure.
#   - cardano-cli stderr → 2>&1 >&2 so it surfaces in the SSH stderr stream.
#   - Witness JSON is base64-encoded with `base64 -w0` (no line wrap) between
#     marker sentinels so the local parser can extract the payload cleanly.
_REMOTE_SIGN_SCRIPT = r"""set -euo pipefail
SKEY="$1"
NETMAGIC_FLAG="$2"
NETMAGIC_VAL="${3:-}"
CCLI="$4"
BODY_PATH="$5"
WIT_PATH="$6"

cleanup() {
  for f in "$BODY_PATH" "$WIT_PATH"; do
    [ -f "$f" ] || continue
    if command -v shred >/dev/null 2>&1; then
      shred -u "$f" 2>/dev/null || rm -f "$f"
    else
      rm -f "$f"
    fi
  done
}
trap cleanup EXIT

[ -f "$SKEY" ] || { echo "ERROR: admin_2.skey missing at $SKEY" >&2; exit 1; }
perms=$(stat -c %a "$SKEY")
[ "$perms" = "400" ] || { echo "ERROR: admin_2.skey mode $perms != 400" >&2; exit 1; }
[ -f "$BODY_PATH" ] || { echo "ERROR: tx body file missing at $BODY_PATH" >&2; exit 1; }

# cardano-cli takes flag PAIRS where applicable: --mainnet OR --testnet-magic N.
# Build the network args as either [--mainnet] or [--testnet-magic, $NETMAGIC_VAL].
if [ "$NETMAGIC_FLAG" = "--mainnet" ]; then
  NET_ARGS=("--mainnet")
elif [ "$NETMAGIC_FLAG" = "--testnet-magic" ]; then
  NET_ARGS=("--testnet-magic" "$NETMAGIC_VAL")
else
  echo "ERROR: unknown NETMAGIC_FLAG: $NETMAGIC_FLAG" >&2
  exit 1
fi

"$CCLI" latest transaction witness \
  --tx-body-file "$BODY_PATH" \
  --signing-key-file "$SKEY" \
  "${NET_ARGS[@]}" \
  --out-file "$WIT_PATH" 2>&1 >&2

echo "=====WITNESS-BEGIN====="
base64 -w0 "$WIT_PATH"
echo
echo "=====WITNESS-END====="
echo "  Node-3 admin_2 witness produced" >&2
"""


def _parse_network_magic_for_remote(flag: str) -> tuple[str, str]:
    """Split '--mainnet' or '--testnet-magic 1' into (flag, value).

    Returns ('--mainnet', '') or ('--testnet-magic', '1') so the remote
    bash snippet can rebuild the cardano-cli flag pair correctly even
    when the value is a separate argv element.
    """
    parts = flag.split()
    if len(parts) == 1 and parts[0] == "--mainnet":
        return "--mainnet", ""
    if len(parts) == 2 and parts[0] == "--testnet-magic":
        return "--testnet-magic", parts[1]
    raise ValueError(f"network magic flag not recognized: {flag!r}")


def sign_with_admin_2_via_ssh(
    cfg: DualAdminConfig,
    tx_body_path: Path,
    witness_out_path: Path,
) -> None:
    """Generate admin_2's witness on the remote host via a single SSH session.

    Protocol (matches mint-ceremony.sh stage_sign2 exactly):
      1. scp the unsigned tx body to /tmp/admin-reclaim.txbody on remote.
      2. ssh runs the remote bash snippet which:
           - loads admin_2.skey,
           - runs cardano-cli witness,
           - base64-streams the witness JSON between marker sentinels,
           - trap-cleans /tmp/admin-reclaim.* in the SAME session.
      3. Local parses the marker block, base64-decodes, writes witness_out_path.

    No exit code, missing markers, or empty payload is ever swallowed —
    each raises SshSignError with the captured remote stderr.
    """
    remote_body_path = "/tmp/admin-reclaim.txbody"
    remote_wit_path = "/tmp/admin-reclaim.admin_2.witness"

    # Step 1 — scp upload. If THIS fails we abort immediately, before ssh.
    scp_argv = [
        "scp",
        "-q",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        str(tx_body_path),
        f"{cfg.admin_2_ssh_host}:{remote_body_path}",
    ]
    logger.info("admin_2 scp upload: %s", " ".join(scp_argv))
    scp_result = subprocess.run(scp_argv, capture_output=True, text=True, check=False)
    if scp_result.returncode != 0:
        raise SshSignError(
            f"admin_2 scp upload failed: rc={scp_result.returncode}\n"
            f"stderr={scp_result.stderr.strip()}\n"
            f"stdout={scp_result.stdout.strip()}"
        )

    # Step 2 — single SSH session: witness + stream + remote cleanup.
    netmagic_flag, netmagic_val = _parse_network_magic_for_remote(cfg.network_magic_flag)
    ssh_argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        cfg.admin_2_ssh_host,
        "bash", "-s", "--",
        cfg.admin_2_skey_remote,
        netmagic_flag,
        netmagic_val,
        cfg.admin_2_cardano_cli_remote,
        remote_body_path,
        remote_wit_path,
    ]
    logger.info("admin_2 ssh witness: %s", " ".join(ssh_argv))
    ssh_result = subprocess.run(
        ssh_argv,
        input=_REMOTE_SIGN_SCRIPT.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    stdout = ssh_result.stdout.decode("utf-8", errors="replace") if isinstance(ssh_result.stdout, bytes) else ssh_result.stdout
    stderr = ssh_result.stderr.decode("utf-8", errors="replace") if isinstance(ssh_result.stderr, bytes) else ssh_result.stderr

    if ssh_result.returncode != 0:
        raise SshSignError(
            f"admin_2 SSH witness failed: rc={ssh_result.returncode}\n"
            f"remote stderr=\n{stderr.strip()}\n"
            f"remote stdout=\n{stdout.strip()}"
        )

    # Step 3 — parse marker-delimited base64 from stdout.
    pattern = re.compile(
        rf"{re.escape(_WITNESS_BEGIN_MARKER)}\s*(.*?)\s*{re.escape(_WITNESS_END_MARKER)}",
        re.DOTALL,
    )
    m = pattern.search(stdout)
    if not m:
        raise SshSignError(
            "admin_2 SSH returned without expected markers — "
            f"stdout=\n{stdout.strip()}\nstderr=\n{stderr.strip()}"
        )
    b64_payload = m.group(1).strip()
    if not b64_payload:
        raise SshSignError(
            "admin_2 SSH returned empty witness payload between markers — "
            f"stderr=\n{stderr.strip()}"
        )

    try:
        witness_bytes = base64.b64decode(b64_payload, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise SshSignError(
            f"admin_2 SSH witness base64 decode failed: {e}\n"
            f"payload first 64 chars: {b64_payload[:64]!r}\n"
            f"stderr=\n{stderr.strip()}"
        ) from e

    if not witness_bytes:
        raise SshSignError("admin_2 witness decoded to zero bytes")

    witness_out_path.parent.mkdir(parents=True, exist_ok=True)
    witness_out_path.write_bytes(witness_bytes)

    logger.info(
        "admin_2 witness written: %s (%d bytes)",
        witness_out_path, len(witness_bytes),
    )


# ---------------------------------------------------------------------------
# Assemble signed tx via cardano-cli
# ---------------------------------------------------------------------------


def assemble_signed_tx(
    cfg: DualAdminConfig,
    tx_body_path: Path,
    admin_1_witness_path: Path,
    admin_2_witness_path: Path,
    signed_tx_out_path: Path,
) -> Path:
    """Combine both witnesses with the tx body into the final signed tx.

    Raises SshSignError on non-zero cardano-cli exit.
    """
    argv = [
        cfg.cardano_cli_local,
        "latest",
        "transaction",
        "assemble",
        "--tx-body-file",
        str(tx_body_path),
        "--witness-file",
        str(admin_1_witness_path),
        "--witness-file",
        str(admin_2_witness_path),
        "--out-file",
        str(signed_tx_out_path),
    ]
    logger.info("assemble: %s", " ".join(argv))
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SshSignError(
            f"cardano-cli assemble failed: rc={result.returncode}\n"
            f"stderr={result.stderr.strip()}\n"
            f"stdout={result.stdout.strip()}"
        )
    return signed_tx_out_path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _read_signed_tx_cbor(signed_tx_path: Path) -> bytes:
    """Read a cardano-cli signed-tx envelope and return its CBOR bytes."""
    envelope = json.loads(signed_tx_path.read_text())
    cbor_hex = envelope["cborHex"]
    return bytes.fromhex(cbor_hex)


def run_admin_reclaim(
    bf: BlockfrostClient,
    *,
    cfg: DualAdminConfig,
    script_address: str,
    script_cbor_hex: str,
    flux_policy_hex: str,
    deadline_posix_ms: int,
    check_only: bool = False,
    submit: bool = False,
    batch_size: int = 20,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """End-to-end reclaim workflow with dual-admin signing.

    - check_only=True  : report unclaimed UTxOs and return; no signing.
    - check_only=False : build + dual-sign + assemble each batch.
    - submit=True      : additionally broadcast each assembled tx.
    - submit=False     : dry-run; the signed tx file is left on disk for
                         the operator to inspect before manual submit.
    """
    unclaimed = discover_unclaimed_utxos(bf, script_address, flux_policy_hex)

    total_flux = sum(u["flux_units"] for u in unclaimed)
    total_ada = sum(u["ada_lovelace"] for u in unclaimed)

    deadline_utc = datetime.fromtimestamp(
        deadline_posix_ms / 1000, tz=timezone.utc
    ).isoformat()

    logger.info("Script address: %s", script_address)
    logger.info("Deadline: %s (POSIX ms: %d)", deadline_utc, deadline_posix_ms)
    logger.info("Unclaimed UTxOs: %d", len(unclaimed))
    logger.info("Total cMATRA: %d base units", total_flux)
    logger.info("Total ADA locked: %.2f", total_ada / 1_000_000)
    logger.info("admin_1 PKH: %s", cfg.admin_pkh_1)
    logger.info("admin_2 PKH: %s", cfg.admin_pkh_2)

    report: dict[str, Any] = {
        "script_address": script_address,
        "deadline_posix_ms": deadline_posix_ms,
        "deadline_utc": deadline_utc,
        "unclaimed_count": len(unclaimed),
        "total_flux_units": total_flux,
        "total_ada_lovelace": total_ada,
        "admin_pkh_1": cfg.admin_pkh_1,
        "admin_pkh_2": cfg.admin_pkh_2,
    }

    if check_only:
        report["mode"] = "check_only"
        report["unclaimed"] = unclaimed
        return report

    if not unclaimed:
        logger.info("No unclaimed UTxOs to reclaim.")
        report["mode"] = "no_action"
        return report

    if work_dir is None:
        work_dir = Path("/tmp/admin-reclaim-out")
    work_dir.mkdir(parents=True, exist_ok=True)

    num_batches = math.ceil(len(unclaimed) / batch_size)
    tx_hashes: list[str] = []
    artifact_paths: list[dict[str, str]] = []

    for i in range(num_batches):
        batch = unclaimed[i * batch_size : (i + 1) * batch_size]
        logger.info(
            "Building reclaim batch %d/%d (%d UTxOs)...",
            i + 1, num_batches, len(batch),
        )

        tx_body_path = work_dir / f"reclaim.batch{i:03d}.txbody.json"
        admin_1_wit = work_dir / f"reclaim.batch{i:03d}.admin_1.witness"
        admin_2_wit = work_dir / f"reclaim.batch{i:03d}.admin_2.witness"
        signed_tx_path = work_dir / f"reclaim.batch{i:03d}.signed.tx.json"

        # 1. Build unsigned tx body
        _body_cbor, tx_hash_hex, tx_body_path = build_reclaim_tx_body(
            bf, cfg, script_address, script_cbor_hex, batch,
            flux_policy_hex, deadline_posix_ms,
            tx_body_path=tx_body_path,
        )
        logger.info("Batch %d tx hash: %s", i, tx_hash_hex)

        # 2. admin_1 local witness
        sign_with_admin_1_local(cfg, tx_body_path, admin_1_wit)

        # 3. admin_2 remote witness over SSH (raises SshSignError on any failure)
        sign_with_admin_2_via_ssh(cfg, tx_body_path, admin_2_wit)

        # 4. Assemble both witnesses + body into final signed tx
        assemble_signed_tx(cfg, tx_body_path, admin_1_wit, admin_2_wit, signed_tx_path)

        artifact_paths.append({
            "tx_body": str(tx_body_path),
            "admin_1_witness": str(admin_1_wit),
            "admin_2_witness": str(admin_2_wit),
            "signed_tx": str(signed_tx_path),
        })

        if submit:
            cbor_bytes = _read_signed_tx_cbor(signed_tx_path)
            submitted_hash = bf.submit_tx(cbor_bytes)
            logger.info("Batch %d submitted: %s", i, submitted_hash)
            tx_hashes.append(submitted_hash)
        else:
            logger.info(
                "Batch %d signed (NOT submitted). Inspect %s before broadcasting.",
                i, signed_tx_path,
            )
            tx_hashes.append(tx_hash_hex)

    report["mode"] = "submit" if submit else "dry_run"
    report["num_batches"] = num_batches
    report["tx_hashes"] = tx_hashes
    report["artifacts"] = artifact_paths
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="cMATRA admin_reclaim — dual-admin post-deadline sweep",
    )
    parser.add_argument("--script-address", type=str, required=True)
    parser.add_argument("--blueprint", type=str, required=True,
                        help="Path to Aiken plutus.json blueprint")
    parser.add_argument("--flux-policy", type=str, required=True,
                        help="cMATRA mint policy id (28-byte hex)")
    parser.add_argument("--deadline-posix-ms", type=int, default=None,
                        help="Override deadline (default: from CLAIM_DEADLINE_POSIX_MS env)")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--check-only", action="store_true", default=False,
                        help="Report unclaimed UTxOs without signing or submitting")
    parser.add_argument("--submit", action="store_true", default=False,
                        help="Broadcast each assembled reclaim tx (default: dry-run)")
    parser.add_argument("--work-dir", type=str, default=None,
                        help="Directory for tx body / witness / signed artifacts")
    parser.add_argument("--out-json", type=str, default=None,
                        help="Write the run report to this path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    deadline = args.deadline_posix_ms or CLAIM_DEADLINE_POSIX_MS
    if deadline == 0:
        logger.error(
            "No deadline set. Use --deadline-posix-ms or set CLAIM_DEADLINE_POSIX_MS."
        )
        sys.exit(1)

    try:
        cfg = DualAdminConfig.from_env()
    except ValueError as e:
        logger.error("Dual-admin config invalid: %s", e)
        sys.exit(1)

    # Lazy import: keep claim_flux_indexed dependency optional for tests that
    # only exercise the discovery + signing helpers in isolation.
    from tools.claim_flux_indexed import load_script_from_blueprint
    script_cbor_hex = load_script_from_blueprint(args.blueprint)

    bf = BlockfrostClient()

    report = run_admin_reclaim(
        bf,
        cfg=cfg,
        script_address=args.script_address,
        script_cbor_hex=script_cbor_hex,
        flux_policy_hex=args.flux_policy,
        deadline_posix_ms=deadline,
        check_only=args.check_only,
        submit=args.submit,
        batch_size=args.batch_size,
        work_dir=Path(args.work_dir) if args.work_dir else None,
    )

    print(json.dumps(report, indent=2))

    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2))
        logger.info("Report written to %s", out_path)


if __name__ == "__main__":
    main()
