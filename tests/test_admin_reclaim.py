"""Tests for tools.admin_reclaim — UTxO discovery, dual-admin tx assembly.

Updated 2026-05-18 (task #326): the on-chain claim_validator requires BOTH
admin signatures for the AdminWithdraw path. The reclaim tool used to sign
with a single key; this suite locks in the new SSH-mediated dual-sign flow
that mirrors the surviving mint-ceremony.sh stage_sign2 pattern (single SSH
session, base64-encoded witness stream back via stdout markers — no scp +
trap race on the remote).
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from tools.admin_reclaim import (
    FLUX_ASSET_NAME_HEX,
    DualAdminConfig,
    SshSignError,
    assemble_signed_tx,
    build_reclaim_tx_body,
    discover_unclaimed_utxos,
    run_admin_reclaim,
    sign_with_admin_1_local,
    sign_with_admin_2_via_ssh,
)
from tools.cardano_utils import encode_claim_datum, posix_ms_to_slot
from tests.conftest import SAMPLE_PKH_1, SAMPLE_PKH_2, SAMPLE_FLUX_POLICY

# A real bech32 mainnet script address (Plutus enterprise) — needed because
# pycardano's Address.from_primitive validates the bech32 encoding eagerly.
# This is the live cMATRA surrender-pool mainnet address from the
# 2026-05-18 deploy.
SCRIPT_ADDR = "addr1w8s6rqdjlzm5he27v9s202p8vjumza8qfsmufm2f6dy68hg9mn27a"
FLUX_UNIT = SAMPLE_FLUX_POLICY + FLUX_ASSET_NAME_HEX


def _make_utxo_response(
    tx_hash: str = "aa" * 32,
    output_index: int = 0,
    ada: int = 2_000_000,
    flux_qty: int = 1_000_000,
    pkh: str = SAMPLE_PKH_1,
) -> dict:
    """Create a mock Blockfrost UTxO response entry."""
    datum_hex = encode_claim_datum(pkh).hex()
    return {
        "tx_hash": tx_hash,
        "output_index": output_index,
        "amount": [
            {"unit": "lovelace", "quantity": str(ada)},
            {"unit": FLUX_UNIT, "quantity": str(flux_qty)},
        ],
        "inline_datum": datum_hex,
    }


def _dummy_admin_pkh(byte: int) -> str:
    """A valid 28-byte payment key hash hex (56 chars), filled with one byte."""
    return f"{byte:02x}" * 28


def _make_dual_admin_cfg(tmp_path: Path) -> DualAdminConfig:
    """Build a DualAdminConfig that points at fake skey paths under tmp_path.

    The skey files are NOT created — tests using this fixture must mock
    subprocess.run before any call that would touch the local skey.
    """
    admin_1_skey = tmp_path / "admin_1.skey"
    admin_1_skey.write_text("{}")  # placeholder; subprocess is mocked

    return DualAdminConfig(
        admin_pkh_1=_dummy_admin_pkh(0xA1),
        admin_pkh_2=_dummy_admin_pkh(0xA2),
        admin_1_skey_path=str(admin_1_skey),
        admin_2_ssh_host="deci@192.168.0.133",
        admin_2_skey_remote="/home/deci/cmatra-merger-keys/admin_2.skey",
        admin_2_cardano_cli_remote="~/bin/cardano-cli",
        cardano_cli_local="cardano-cli",
        network_magic_flag="--mainnet",
    )


# ---------------------------------------------------------------------------
# Pure-discovery tests (unchanged — single-admin / dual-admin agnostic)
# ---------------------------------------------------------------------------


class TestDiscoverUnclaimedUtxos:
    def test_finds_utxos(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(pkh=SAMPLE_PKH_1, flux_qty=500_000),
            _make_utxo_response(tx_hash="bb" * 32, pkh=SAMPLE_PKH_2, flux_qty=300_000),
        ]

        result = discover_unclaimed_utxos(mock_bf, SCRIPT_ADDR, SAMPLE_FLUX_POLICY)
        assert len(result) == 2
        assert result[0]["flux_units"] == 500_000
        assert result[0]["datum_pkh"] == SAMPLE_PKH_1
        assert result[1]["flux_units"] == 300_000

    def test_empty_script_address(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = []

        result = discover_unclaimed_utxos(mock_bf, SCRIPT_ADDR, SAMPLE_FLUX_POLICY)
        assert result == []

    def test_utxo_without_datum(self, mocker):
        mock_bf = mocker.MagicMock()
        utxo = _make_utxo_response()
        utxo["inline_datum"] = None
        mock_bf.get_address_utxos.return_value = [utxo]

        result = discover_unclaimed_utxos(mock_bf, SCRIPT_ADDR, SAMPLE_FLUX_POLICY)
        assert len(result) == 1
        assert result[0]["datum_pkh"] is None

    def test_utxo_ada_extraction(self, mocker):
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(ada=5_000_000),
        ]

        result = discover_unclaimed_utxos(mock_bf, SCRIPT_ADDR, SAMPLE_FLUX_POLICY)
        assert result[0]["ada_lovelace"] == 5_000_000


class TestPosixMsToSlot:
    def test_mainnet_known_value(self):
        slot = posix_ms_to_slot(1596491091000, "mainnet")
        assert slot == 4492800

    def test_mainnet_offset(self):
        slot = posix_ms_to_slot(1596492091000, "mainnet")
        assert slot == 4492800 + 1000

    def test_preprod_known_value(self):
        slot = posix_ms_to_slot(1655683200000, "preprod")
        assert slot == 0

    def test_preprod_offset(self):
        slot = posix_ms_to_slot(1655684200000, "preprod")
        assert slot == 1000


# ---------------------------------------------------------------------------
# Dual-admin: config + required-signers
# ---------------------------------------------------------------------------


class TestDualAdminConfig:
    """The reclaim tool MUST carry both PKHs and the SSH coordinates."""

    def test_config_carries_both_pkhs(self, tmp_path):
        cfg = _make_dual_admin_cfg(tmp_path)
        # Both PKHs are exactly 56 hex chars (28 bytes)
        assert len(cfg.admin_pkh_1) == 56
        assert len(cfg.admin_pkh_2) == 56
        assert cfg.admin_pkh_1 != cfg.admin_pkh_2

    def test_config_carries_ssh_target(self, tmp_path):
        cfg = _make_dual_admin_cfg(tmp_path)
        assert cfg.admin_2_ssh_host.endswith("192.168.0.133")
        assert cfg.admin_2_skey_remote.endswith("admin_2.skey")
        assert cfg.admin_2_cardano_cli_remote

    def test_config_rejects_equal_pkhs(self, tmp_path):
        """Defense-in-depth: refuse a config where both admins are the same key."""
        admin_1_skey = tmp_path / "admin_1.skey"
        admin_1_skey.write_text("{}")
        with pytest.raises(ValueError, match="admin_pkh_1 and admin_pkh_2 must differ"):
            DualAdminConfig(
                admin_pkh_1=_dummy_admin_pkh(0xCC),
                admin_pkh_2=_dummy_admin_pkh(0xCC),  # same — invalid
                admin_1_skey_path=str(admin_1_skey),
                admin_2_ssh_host="deci@192.168.0.133",
                admin_2_skey_remote="/home/deci/cmatra-merger-keys/admin_2.skey",
                admin_2_cardano_cli_remote="~/bin/cardano-cli",
                cardano_cli_local="cardano-cli",
                network_magic_flag="--mainnet",
            )

    def test_config_rejects_bad_pkh_length(self, tmp_path):
        admin_1_skey = tmp_path / "admin_1.skey"
        admin_1_skey.write_text("{}")
        with pytest.raises(ValueError, match="admin_pkh_1 must be 56 hex chars"):
            DualAdminConfig(
                admin_pkh_1="aa",  # too short
                admin_pkh_2=_dummy_admin_pkh(0xA2),
                admin_1_skey_path=str(admin_1_skey),
                admin_2_ssh_host="deci@192.168.0.133",
                admin_2_skey_remote="/home/deci/cmatra-merger-keys/admin_2.skey",
                admin_2_cardano_cli_remote="~/bin/cardano-cli",
                cardano_cli_local="cardano-cli",
                network_magic_flag="--mainnet",
            )


class TestBuildReclaimTxBody:
    """The reclaim tx body MUST list BOTH admins as required_signers."""

    def test_required_signers_contains_both_pkhs(self, tmp_path, mocker):
        # We can't easily exercise pycardano's full builder against a mocked
        # chain context, so we mock out the inner build call and inspect the
        # required_signers list we pass to it.
        cfg = _make_dual_admin_cfg(tmp_path)
        unclaimed = [
            {
                "tx_hash": "aa" * 32,
                "output_index": 0,
                "ada_lovelace": 2_000_000,
                "flux_units": 500_000,
                "datum_pkh": SAMPLE_PKH_1,
            }
        ]

        captured_required = []

        class _StubBuilder:
            def __init__(self, *a, **kw):
                self.required_signers = None
                self.validity_start = None
                self.collaterals = []

            def add_script_input(self, *a, **kw):
                pass

            def add_input_address(self, *a, **kw):
                pass

            def build(self, change_address=None):
                captured_required.append(list(self.required_signers))
                # Return an object with a `.hash()` and `.to_cbor()` interface
                stub = mocker.MagicMock()
                stub.hash.return_value.payload = b"\x00" * 32
                stub.to_cbor.return_value = b"\x00" * 64
                return stub

        mocker.patch("tools.admin_reclaim.TransactionBuilder", _StubBuilder)
        mocker.patch("tools.admin_reclaim.BlockFrostChainContext")

        bf = mocker.MagicMock()
        bf.project_id = "p"
        bf.base_url = "u"

        build_reclaim_tx_body(
            bf=bf,
            cfg=cfg,
            script_address=SCRIPT_ADDR,
            script_cbor_hex="4d01000033222220051200120011",
            unclaimed=unclaimed,
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
        )

        assert len(captured_required) == 1
        required = captured_required[0]
        assert len(required) == 2, "must list BOTH admin PKHs as required signers"
        pkh_hexes = sorted(rs.payload.hex() for rs in required)
        assert pkh_hexes == sorted([cfg.admin_pkh_1, cfg.admin_pkh_2])


# ---------------------------------------------------------------------------
# SSH stage_sign2 — the critical surviving pattern from mint-ceremony.sh
# ---------------------------------------------------------------------------


class TestSignWithAdmin2ViaSsh:
    """The remote-witness flow MUST mirror the mint-ceremony stage_sign2:

    - Single SSH session.
    - Remote streams base64-encoded witness JSON back via stdout, sandwiched
      between =====WITNESS-BEGIN===== / =====WITNESS-END===== markers.
    - Local decodes + writes witness file.
    - NO swallowed errors — non-zero exit, missing markers, or empty payload
      raise SshSignError with the captured remote stderr included.
    """

    def _ok_subprocess_result(self, b64_payload: bytes) -> subprocess.CompletedProcess:
        # ssh is invoked WITHOUT text=True, so stdout/stderr come back as bytes.
        stdout_bytes = (
            b"=====WITNESS-BEGIN=====\n"
            + base64.b64encode(b64_payload)
            + b"\n=====WITNESS-END=====\n"
        )
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=stdout_bytes,
            stderr=b"  Node-3 witness produced\n",
        )

    def test_decodes_marker_delimited_base64(self, tmp_path, mocker):
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text(json.dumps({"type": "TxBodyConway", "cborHex": "aa"}))
        witness_out = tmp_path / "admin_2.witness"

        witness_json_bytes = json.dumps(
            {
                "type": "TxWitness ConwayEra",
                "description": "Key Witness ShelleyEra",
                "cborHex": "8201" + "ab" * 64,
            }
        ).encode("utf-8")

        # Two subprocess calls: scp (upload), then ssh (witness + stream).
        def _fake_run(argv, **kwargs):
            if argv[0] == "scp":
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[0] == "ssh":
                return self._ok_subprocess_result(witness_json_bytes)
            raise AssertionError(f"unexpected argv: {argv!r}")

        run = mocker.patch("tools.admin_reclaim.subprocess.run", side_effect=_fake_run)

        sign_with_admin_2_via_ssh(cfg, tx_body_path, witness_out)

        # The witness JSON content matches what the remote produced
        assert witness_out.read_bytes() == witness_json_bytes

        # subprocess.run was invoked exactly twice: scp (upload), then ssh.
        # The ssh invocation is the SINGLE session that runs witness +
        # stream-back, so the trap cleanup on the remote runs in the same
        # session as the witness generation (no scp-vs-trap race).
        assert run.call_count == 2
        scp_call, ssh_call = run.call_args_list
        assert scp_call.args[0][0] == "scp"
        assert ssh_call.args[0][0] == "ssh"
        # The remote bash script must be piped via stdin (input=...), NOT
        # passed inline as a positional command — this guarantees the
        # trap cleanup runs in the SAME session that produced the witness.
        assert ssh_call.kwargs.get("input") is not None
        assert b"=====WITNESS-BEGIN=====" in ssh_call.kwargs["input"]

    def _scp_then_ssh(
        self,
        ssh_result: subprocess.CompletedProcess,
    ):
        """A side_effect that always succeeds on scp and returns ssh_result on ssh."""
        def _fn(argv, **kwargs):
            if argv[0] == "scp":
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[0] == "ssh":
                return ssh_result
            raise AssertionError(f"unexpected argv: {argv!r}")
        return _fn

    def test_ssh_command_passes_skey_and_network(self, tmp_path, mocker):
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        witness_out = tmp_path / "admin_2.witness"

        run = mocker.patch(
            "tools.admin_reclaim.subprocess.run",
            side_effect=self._scp_then_ssh(self._ok_subprocess_result(b"{}")),
        )

        sign_with_admin_2_via_ssh(cfg, tx_body_path, witness_out)

        # Last call is the ssh witness call (after scp).
        ssh_argv = run.call_args.args[0]
        assert ssh_argv[0] == "ssh"
        # SSH target appears in argv
        assert cfg.admin_2_ssh_host in ssh_argv
        # The remote bash invocation must forward SKEY + NETMAGIC + CARDANO_CLI
        # as positional args (so the remote script can read them with $1/$2/$3
        # — mirrors mint-ceremony.sh stage_sign2 exactly)
        forwarded = " ".join(ssh_argv)
        assert cfg.admin_2_skey_remote in forwarded
        # network_magic_flag is split on remote side; we only verify the flag
        # token is present in argv (split into '--mainnet' or '--testnet-magic')
        flag_token = cfg.network_magic_flag.split()[0]
        assert flag_token in ssh_argv
        assert cfg.admin_2_cardano_cli_remote in forwarded

    def test_nonzero_exit_raises_with_stderr(self, tmp_path, mocker):
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        witness_out = tmp_path / "admin_2.witness"

        mocker.patch(
            "tools.admin_reclaim.subprocess.run",
            side_effect=self._scp_then_ssh(
                subprocess.CompletedProcess(
                    args=["ssh"],
                    returncode=1,
                    stdout="",
                    stderr=b"ERROR: admin_2.skey missing at /home/deci/cmatra-merger-keys/admin_2.skey\n",
                ),
            ),
        )

        with pytest.raises(SshSignError) as excinfo:
            sign_with_admin_2_via_ssh(cfg, tx_body_path, witness_out)

        # The original remote stderr MUST be in the exception message
        # so the operator can diagnose without re-running.
        assert "admin_2.skey missing" in str(excinfo.value)
        assert "rc=1" in str(excinfo.value).lower() or "exit" in str(excinfo.value).lower()

    def test_missing_markers_raises(self, tmp_path, mocker):
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        witness_out = tmp_path / "admin_2.witness"

        # Exit 0 but no markers in stdout — almost certainly a protocol
        # mismatch (remote script changed but local parser didn't).
        mocker.patch(
            "tools.admin_reclaim.subprocess.run",
            side_effect=self._scp_then_ssh(
                subprocess.CompletedProcess(
                    args=["ssh"],
                    returncode=0,
                    stdout=b"unexpected output\n",
                    stderr=b"",
                ),
            ),
        )

        with pytest.raises(SshSignError, match="markers"):
            sign_with_admin_2_via_ssh(cfg, tx_body_path, witness_out)

    def test_empty_payload_between_markers_raises(self, tmp_path, mocker):
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        witness_out = tmp_path / "admin_2.witness"

        mocker.patch(
            "tools.admin_reclaim.subprocess.run",
            side_effect=self._scp_then_ssh(
                subprocess.CompletedProcess(
                    args=["ssh"],
                    returncode=0,
                    stdout=b"=====WITNESS-BEGIN=====\n\n=====WITNESS-END=====\n",
                    stderr=b"",
                ),
            ),
        )

        with pytest.raises(SshSignError, match="empty"):
            sign_with_admin_2_via_ssh(cfg, tx_body_path, witness_out)

    def test_scp_uploads_tx_body_before_ssh(self, tmp_path, mocker):
        """The tx body must be uploaded to the remote BEFORE the ssh witness call.

        We assert this by capturing subprocess.run invocations in order:
        first scp (upload), then ssh (witness)."""
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        witness_out = tmp_path / "admin_2.witness"

        calls: list[list[str]] = []

        def _fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[0] == "scp":
                return subprocess.CompletedProcess(argv, 0, "", "")
            # ssh
            stdout = (
                "=====WITNESS-BEGIN=====\n"
                + base64.b64encode(b"{}").decode("ascii")
                + "\n=====WITNESS-END=====\n"
            )
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        mocker.patch("tools.admin_reclaim.subprocess.run", side_effect=_fake_run)

        sign_with_admin_2_via_ssh(cfg, tx_body_path, witness_out)

        assert len(calls) == 2
        assert calls[0][0] == "scp"
        assert calls[1][0] == "ssh"

    def test_scp_failure_raises_without_invoking_ssh(self, tmp_path, mocker):
        """If scp can't reach the remote, abort BEFORE running ssh.

        This avoids the broken pattern of running ssh against a stale or
        missing tx body file on the remote.
        """
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        witness_out = tmp_path / "admin_2.witness"

        calls: list[list[str]] = []

        def _fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[0] == "scp":
                return subprocess.CompletedProcess(
                    argv, 255, "", "ssh: connect to host 192.168.0.133 port 22: No route to host\n"
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        mocker.patch("tools.admin_reclaim.subprocess.run", side_effect=_fake_run)

        with pytest.raises(SshSignError, match="scp"):
            sign_with_admin_2_via_ssh(cfg, tx_body_path, witness_out)

        # ssh must NOT have been invoked
        assert len(calls) == 1
        assert calls[0][0] == "scp"


class TestSignWithAdmin1Local:
    """admin_1 local-side signing must invoke cardano-cli on the local skey."""

    def test_local_witness_invokes_cardano_cli(self, tmp_path, mocker):
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        witness_out = tmp_path / "admin_1.witness"

        run = mocker.patch(
            "tools.admin_reclaim.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        )

        # When cardano-cli "succeeds", it normally creates the output file.
        # Simulate that side effect in the mock by writing a marker file.
        def _side_effect(argv, **kwargs):
            witness_out.write_text('{"type":"TxWitness ConwayEra","cborHex":"aa"}')
            return subprocess.CompletedProcess(argv, 0, "", "")

        run.side_effect = _side_effect

        sign_with_admin_1_local(cfg, tx_body_path, witness_out)

        # cardano-cli was invoked with the local skey + tx-body-file
        args = run.call_args.args[0]
        assert args[0] == cfg.cardano_cli_local
        flat = " ".join(args)
        assert cfg.admin_1_skey_path in flat
        assert str(tx_body_path) in flat
        assert cfg.network_magic_flag in flat
        assert witness_out.read_text().startswith("{")

    def test_local_failure_raises_with_stderr(self, tmp_path, mocker):
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        witness_out = tmp_path / "admin_1.witness"

        mocker.patch(
            "tools.admin_reclaim.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=2, stdout="", stderr="cardano-cli: file not found\n"
            ),
        )

        with pytest.raises(SshSignError, match="admin_1 local"):
            sign_with_admin_1_local(cfg, tx_body_path, witness_out)


class TestAssembleSignedTx:
    """assemble combines both witnesses into the final tx via cardano-cli."""

    def test_assemble_invokes_cardano_cli_with_both_witnesses(self, tmp_path, mocker):
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        admin_1_wit = tmp_path / "admin_1.witness"
        admin_1_wit.write_text("{}")
        admin_2_wit = tmp_path / "admin_2.witness"
        admin_2_wit.write_text("{}")
        out_tx = tmp_path / "reclaim.signed.tx"

        run = mocker.patch(
            "tools.admin_reclaim.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        )

        def _side_effect(argv, **kwargs):
            out_tx.write_text(
                json.dumps({"type": "Witnessed Tx ConwayEra", "cborHex": "aa" * 16})
            )
            return subprocess.CompletedProcess(argv, 0, "", "")

        run.side_effect = _side_effect

        assemble_signed_tx(cfg, tx_body_path, admin_1_wit, admin_2_wit, out_tx)

        argv = run.call_args.args[0]
        flat = " ".join(argv)
        assert "assemble" in flat
        assert str(admin_1_wit) in flat
        assert str(admin_2_wit) in flat
        assert str(tx_body_path) in flat
        assert str(out_tx) in flat

    def test_assemble_failure_raises(self, tmp_path, mocker):
        cfg = _make_dual_admin_cfg(tmp_path)
        tx_body_path = tmp_path / "reclaim.txbody"
        tx_body_path.write_text("{}")
        admin_1_wit = tmp_path / "admin_1.witness"
        admin_1_wit.write_text("{}")
        admin_2_wit = tmp_path / "admin_2.witness"
        admin_2_wit.write_text("{}")
        out_tx = tmp_path / "reclaim.signed.tx"

        mocker.patch(
            "tools.admin_reclaim.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="bad witness format\n"
            ),
        )

        with pytest.raises(SshSignError, match="assemble"):
            assemble_signed_tx(cfg, tx_body_path, admin_1_wit, admin_2_wit, out_tx)


# ---------------------------------------------------------------------------
# Orchestration: run_admin_reclaim drives the pipeline end-to-end
# ---------------------------------------------------------------------------


class TestRunAdminReclaim:
    def test_check_only_mode(self, mocker, tmp_path):
        cfg = _make_dual_admin_cfg(tmp_path)
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(flux_qty=500_000),
        ]

        report = run_admin_reclaim(
            mock_bf,
            cfg=cfg,
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
            check_only=True,
        )
        assert report["mode"] == "check_only"
        assert report["unclaimed_count"] == 1
        assert report["total_flux_units"] == 500_000

    def test_no_unclaimed_utxos(self, mocker, tmp_path):
        cfg = _make_dual_admin_cfg(tmp_path)
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = []

        report = run_admin_reclaim(
            mock_bf,
            cfg=cfg,
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
        )
        assert report["mode"] == "no_action"
        assert report["unclaimed_count"] == 0

    def test_deadline_utc_in_report(self, mocker, tmp_path):
        cfg = _make_dual_admin_cfg(tmp_path)
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = []

        report = run_admin_reclaim(
            mock_bf,
            cfg=cfg,
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
        )
        assert "deadline_utc" in report
        assert "2023" in report["deadline_utc"]

    def test_multiple_utxos_summed(self, mocker, tmp_path):
        cfg = _make_dual_admin_cfg(tmp_path)
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(flux_qty=100_000, ada=2_000_000),
            _make_utxo_response(tx_hash="bb" * 32, flux_qty=200_000, ada=3_000_000),
        ]

        report = run_admin_reclaim(
            mock_bf,
            cfg=cfg,
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
            check_only=True,
        )
        assert report["total_flux_units"] == 300_000
        assert report["total_ada_lovelace"] == 5_000_000

    def test_dry_run_invokes_admin_1_local_and_admin_2_ssh(self, mocker, tmp_path):
        """End-to-end dry-run wires admin_1 local + admin_2 SSH + assemble.

        Dry-run mode (submit=False) MUST still build the signed transaction
        — that's the whole point of validating the dual-sign flow without
        broadcasting. Only the final submit_tx call is skipped.
        """
        cfg = _make_dual_admin_cfg(tmp_path)
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(flux_qty=500_000),
        ]

        # Patch the heavy-lift pipeline functions — we only care about
        # ORDERING + PARAMS at this level.
        b_body = mocker.patch(
            "tools.admin_reclaim.build_reclaim_tx_body",
            return_value=(b"\xde" * 32, "aa" * 32, tmp_path / "reclaim.txbody"),
        )
        sign_1 = mocker.patch("tools.admin_reclaim.sign_with_admin_1_local")
        sign_2 = mocker.patch("tools.admin_reclaim.sign_with_admin_2_via_ssh")
        assemble = mocker.patch(
            "tools.admin_reclaim.assemble_signed_tx",
            return_value=tmp_path / "reclaim.signed.tx",
        )

        report = run_admin_reclaim(
            mock_bf,
            cfg=cfg,
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
            submit=False,
        )

        assert b_body.called
        assert sign_1.called
        assert sign_2.called
        assert assemble.called
        # bf.submit_tx must NOT be called in dry-run
        assert not mock_bf.submit_tx.called
        assert report["mode"] == "dry_run"

    def test_submit_mode_broadcasts_after_assembly(self, mocker, tmp_path):
        cfg = _make_dual_admin_cfg(tmp_path)
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(flux_qty=500_000),
        ]
        mock_bf.submit_tx.return_value = "deadbeefcafebabe" * 4

        work_dir = tmp_path / "work"
        mocker.patch(
            "tools.admin_reclaim.build_reclaim_tx_body",
            return_value=(b"\xde" * 32, "aa" * 32, tmp_path / "reclaim.txbody"),
        )
        mocker.patch("tools.admin_reclaim.sign_with_admin_1_local")
        mocker.patch("tools.admin_reclaim.sign_with_admin_2_via_ssh")

        def _assemble_writes_envelope(_cfg, _body, _w1, _w2, out_path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(
                    {"type": "Witnessed Tx ConwayEra", "cborHex": "aabbccdd" * 8}
                )
            )
            return out_path

        mocker.patch(
            "tools.admin_reclaim.assemble_signed_tx",
            side_effect=_assemble_writes_envelope,
        )

        report = run_admin_reclaim(
            mock_bf,
            cfg=cfg,
            script_address=SCRIPT_ADDR,
            script_cbor_hex="deadbeef",
            flux_policy_hex=SAMPLE_FLUX_POLICY,
            deadline_posix_ms=1_700_000_000_000,
            submit=True,
            work_dir=work_dir,
        )

        assert mock_bf.submit_tx.called
        # The bytes passed to submit_tx must be the CBOR-decoded envelope
        sent_bytes = mock_bf.submit_tx.call_args.args[0]
        assert sent_bytes == bytes.fromhex("aabbccdd" * 8)
        assert report["mode"] == "submit"
        assert "tx_hashes" in report
        assert report["tx_hashes"]

    def test_ssh_failure_aborts_without_submit(self, mocker, tmp_path):
        """If admin_2 SSH fails, we MUST NOT silently fall through to submit."""
        cfg = _make_dual_admin_cfg(tmp_path)
        mock_bf = mocker.MagicMock()
        mock_bf.get_address_utxos.return_value = [
            _make_utxo_response(flux_qty=500_000),
        ]

        mocker.patch(
            "tools.admin_reclaim.build_reclaim_tx_body",
            return_value=(b"\xde" * 32, "aa" * 32, tmp_path / "reclaim.txbody"),
        )
        mocker.patch("tools.admin_reclaim.sign_with_admin_1_local")
        mocker.patch(
            "tools.admin_reclaim.sign_with_admin_2_via_ssh",
            side_effect=SshSignError("admin_2 SSH failed: rc=1\nremote stderr=..."),
        )

        with pytest.raises(SshSignError):
            run_admin_reclaim(
                mock_bf,
                cfg=cfg,
                script_address=SCRIPT_ADDR,
                script_cbor_hex="deadbeef",
                flux_policy_hex=SAMPLE_FLUX_POLICY,
                deadline_posix_ms=1_700_000_000_000,
                submit=True,
            )

        # Submit MUST NOT have been called when admin_2 signing fails.
        assert not mock_bf.submit_tx.called
