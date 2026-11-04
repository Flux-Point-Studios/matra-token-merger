"""C1 simulation: the fresh-key migration of the cMATRA pool, offline.

LIVE       the real pool UTxO d3a1e632#1 and the live e1a181b2 bytes; the
           migration (test slice, then the rest; and the one-step race form)
           evaluated by `aiken tx simulate`, with negatives.
CLAIM      a 2-AGENT surrender against the re-parameterized pool, evaluated by
           aiken and approved by the signers' own cosign_policy; late-window,
           AdminWithdraw and old-key cases.
API        RUNBOOK.md's own C0, P11 and step 13 shell blocks, run as written
           against a local stand-in for surrender-api's two claim routes that
           checks the secret header and uses the service's request models and
           witness merge. Only C0's settings change, to the simulation's own.
REHEARSAL  the whole signing pipeline with real Ed25519 signatures: a throwaway
           old pair applied with the same source and deadline, cardano-cli
           witnesses, tools.pool_migration assemble, cardano-cli txid, aiken.

The pool and the protocol parameters come from the chain reads recorded in
chain/ on 2026-11-04; --live reads them again through the configured chain
provider (CHAIN_API_BASE_URL and its credential, as tools.pool_migration
snapshot does). The keys are sim-only pairs derived from public seeds, which
hold nothing, and onchain/claim_validator/apply_params.sh applies the pools
they parameterize at run time. Nothing is submitted.

    python ceremony/c1/sim.py [--live] [--out DIR]

Needs aiken v1.1.21 and cardano-cli 11.0.0.0 (on PATH, or named by $AIKEN
and $CARDANO_CLI), jq and curl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cbor2
from cbor2 import CBORTag
from evaluate import REPO, aiken_binary, raw_output, simulate
from pycardano import Address, PaymentSigningKey, PaymentVerificationKey

import services.surrender_api as api
from services.cosign_policy import (
    CosignRejected,
    _witness_fields,
    decode,
    evaluate_surrender,
    load_config,
    require_claimant_signature,
    signature_verifies,
    split_tx,
)
from tools import pool_migration as pm
from tools.api_clients import BlockfrostClient
from tools.cardano_utils import posix_ms_to_slot
from tools.process_surrender import load_rate_table, load_redemption_pin, surrendered_entitlement

HERE = Path(__file__).resolve().parent
CHAIN = HERE / "chain"
CLI_SHA256 = "b55687eb503a26a96dd436cdb492cbee15a945588c346b81dc4ca86f3c8a1066"   # cardano-cli 11.0.0.0

POOL_REF = "d3a1e632424794bed36d7062b4c09c5fe547fa7a1addbd2d45f9b84df773c733#1"
CMATRA_UNIT = "7ff33a5565393dc47b48ac47becc12d92c9952e724e8446dfb6adc66634d41545241"
CM_POLICY, CM_NAME = bytes.fromhex(CMATRA_UNIT[:56]), bytes.fromhex(CMATRA_UNIT[56:])
AGENT_UNIT = "97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec54616c6f73"
QUARANTINE = "addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl"
OLD_ADMINS = ["f1eee828ae800a5bad6f3913603e20531162cdf44c5e0ffa49b6a936",
              "77fdb621ad5f926257ce1a2f845fc946ae3d5a683ac89128bad3410d"]
OLD_DEADLINE_MS = 1_795_910_400_000          # 2026-11-29 00:00Z
NEW_DEADLINE_MS = 1_798_502_400_000          # 2026-12-29 00:00Z
OLD_DEADLINE_SLOT = posix_ms_to_slot(OLD_DEADLINE_MS)
NEW_DEADLINE_SLOT = posix_ms_to_slot(NEW_DEADLINE_MS)
EX_UNITS = [2_000_000, 900_000_000]
SLICE = 1_000_000                             # 1 cMATRA
TEST_AGENT_UNITS = 2
KEY_NAMES = ("old_admin_1", "old_admin_2", "new_admin_1", "new_admin_2", "claimant")

results: list[dict] = []
PKHS: dict[str, str] = {}
CODES: dict[str, bytes] = {}
OUT = HERE / "out"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blake2b_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


@cache
def cardano_cli() -> str:
    """$CARDANO_CLI, else cardano-cli on PATH; refused unless it is the binary RUNBOOK.md pins."""
    path = os.environ.get("CARDANO_CLI") or shutil.which("cardano-cli")
    if not path:
        raise SystemExit("cardano-cli not found: put 11.0.0.0 on PATH or name it in $CARDANO_CLI")
    if sha256(Path(path).read_bytes()) != CLI_SHA256:
        raise SystemExit(f"{path} is not the cardano-cli 11.0.0.0 binary RUNBOOK.md pins")
    return path


def make_keys(directory: Path) -> dict[str, str]:
    """Sim-only key pairs from public seeds, so every run signs as the same
    key hashes. Anyone can derive them; they hold nothing."""
    directory.mkdir(parents=True)
    hashes = {}
    for name in KEY_NAMES:
        signing = PaymentSigningKey(blake2b_256(f"C1 simulation throwaway {name}".encode()))
        verification = PaymentVerificationKey.from_signing_key(signing)
        signing.save(str(directory / f"{name}.skey"))
        verification.save(str(directory / f"{name}.vkey"))
        hashes[name] = verification.hash().payload.hex()
    return hashes


def applied_code(blueprint_path: Path) -> bytes:
    blueprint = json.loads(blueprint_path.read_text())
    return bytes.fromhex(next(v["compiledCode"] for v in blueprint["validators"] if v["title"].endswith(".spend")))


def apply_params(admins: list[str], deadline_ms: int, out: Path) -> bytes:
    """The claim validator applied to ``admins`` and ``deadline_ms`` by apply_params.sh."""
    env = {**os.environ, "PATH": f"{OUT / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"}
    subprocess.run([str(REPO / "onchain/claim_validator/apply_params.sh"), *admins, str(deadline_ms), str(out)],
                   check=True, capture_output=True, env=env)
    return applied_code(out)


def prepare(out: Path) -> None:
    """Fresh sim keys, and the pools they parameterize, for a run writing into ``out``."""
    global OUT
    OUT = out
    for sub in ("keys", "applied", "bin"):
        shutil.rmtree(out / sub, ignore_errors=True)
    (out / "bin").mkdir(parents=True)
    (out / "bin/aiken").symlink_to(aiken_binary())
    cardano_cli()
    PKHS.clear()
    PKHS.update(make_keys(out / "keys"))
    (out / "applied").mkdir()
    CODES["old"] = apply_params(OLD_ADMINS, OLD_DEADLINE_MS, out / "applied/old.json")
    CODES["new"] = apply_params([pkh("new_admin_1"), pkh("new_admin_2")], NEW_DEADLINE_MS, out / "applied/new.json")
    CODES["rehearsal"] = apply_params([pkh("old_admin_1"), pkh("old_admin_2")], OLD_DEADLINE_MS,
                                      out / "applied/rehearsal.json")


def pkh(name: str) -> str:
    return PKHS[name]


def key_address(key_hash: str) -> bytes:
    return b"\x61" + bytes.fromhex(key_hash)


def script_address(script_hash: bytes) -> bytes:
    return b"\x71" + script_hash


def parent_tx(outputs: list, seed: bytes) -> bytes:
    return cbor2.dumps([{0: [[seed * 32, 0]], 1: outputs, 2: 200_000}, {}, True, None])


def txid_of(tx: bytes) -> bytes:
    return blake2b_256(split_tx(tx)[0])


def record(group: str, name: str, expect: str, outcome: str, **facts) -> None:
    ok = expect == outcome
    results.append({"group": group, "case": name, "expect": expect, "outcome": outcome, "ok": ok, **facts})
    print(f"{'OK ' if ok else 'BAD'} {group:9} {name:52} expect {expect:6} got {outcome}"
          + (f"  {facts.get('note', '')}" if facts.get("note") else ""))


def aiken(group: str, name: str, tx: bytes, resolved: list[tuple[tuple[bytes, int], bytes]], expect: str,
          note: str = "") -> None:
    """`aiken tx simulate` over ``tx`` with every input it spends resolved."""
    directory = OUT / "aiken"
    directory.mkdir(parents=True, exist_ok=True)
    run = simulate(tx, resolved, directory / name)
    (directory / f"{name}.log").write_text(run.stdout + run.stderr)
    outcome = "PASS" if run.returncode == 0 else "FAIL"
    budget = None
    if run.returncode == 0:
        try:
            budget = json.loads(run.stdout)
        except json.JSONDecodeError:
            budget = run.stdout.strip()[:200]
    record(group, name, expect, outcome, txid=txid_of(tx).hex(), budget=budget, note=note)


def write_envelope(path: Path, tx: bytes, description: str) -> Path:
    path.write_text(json.dumps({"type": "Tx ConwayEra", "description": description, "cborHex": tx.hex()}, indent=2))
    return path


def cli_txid(path: Path) -> str:
    run = subprocess.run([cardano_cli(), "latest", "transaction", "txid", "--tx-file", str(path)],
                         capture_output=True, text=True, check=True)
    return json.loads(run.stdout)["txhash"]


def cli_witness(tx_path: Path, key: str, skey: Path | None = None) -> Path:
    out = tx_path.with_name(f"{tx_path.stem}.{key}.witness")
    subprocess.run([cardano_cli(), "latest", "transaction", "witness", "--tx-body-file", str(tx_path),
                    "--signing-key-file", str(skey or OUT / "keys" / f"{key}.skey"), "--mainnet",
                    "--out-file", str(out)], check=True, capture_output=True)
    return out


def envelope_cbor(path: Path) -> bytes:
    return bytes.fromhex(json.loads(path.read_text())["cborHex"])


class RecordedChain:
    """The chain reads recorded in chain/, answered as the Insight client answers them."""

    base_url = "recorded:ceremony/c1/chain"

    @staticmethod
    def _recorded(name: str, tx_hash: str | None = None):
        if tx_hash not in (None, POOL_REF[:64]):
            raise KeyError(f"no read of {tx_hash} was recorded")
        return json.loads((CHAIN / name).read_text())

    def get_tx_cbor(self, tx_hash: str) -> bytes:
        return bytes.fromhex(self._recorded("pool-parent.cbor.json", tx_hash)["cbor"])

    def get_tx_utxos(self, tx_hash: str) -> dict:
        return self._recorded("pool-tx-utxos.json", tx_hash)

    def get_protocol_parameters(self) -> dict:
        return self._recorded("pparams.json")

    def get_latest_block(self) -> dict:
        return self._recorded("block-latest.json")


class SimClient:
    """A chain client, with the throwaway fee input served locally."""

    def __init__(self, real, local: dict[str, bytes]) -> None:
        self.real, self.local, self.base_url = real, local, real.base_url

    def get_tx_cbor(self, tx_hash: str) -> bytes:
        return self.local[tx_hash] if tx_hash in self.local else self.real.get_tx_cbor(tx_hash)

    def get_tx_utxos(self, tx_hash: str) -> dict:
        if tx_hash in self.local:
            outputs = decode(split_tx(self.local[tx_hash])[0])[1]
            return {"outputs": [{"output_index": i, "consumed_by_tx": None} for i in range(len(outputs))]}
        return self.real.get_tx_utxos(tx_hash)

    def get_protocol_parameters(self) -> dict:
        return self.real.get_protocol_parameters()

    def get_latest_block(self) -> dict:
        return self.real.get_latest_block()


def chain_client(live: bool):
    return BlockfrostClient() if live else RecordedChain()


# ---------------------------------------------------------------------------
# LIVE: the migration against the live validator bytes
# ---------------------------------------------------------------------------


def chained_snapshot(base: dict, producing: bytes, pool_index: int, fee_index: int) -> dict:
    """A snapshot whose inputs are outputs of ``producing``, a transaction of this run."""
    ref = txid_of(producing).hex()

    def entry(index: int) -> dict:
        return {"ref": f"{ref}#{index}", "producing_tx": producing.hex(), "consumed_by": None}

    return {**base, "inputs": {"pool": entry(pool_index), "fee": entry(fee_index)}}


def plan_for(snap_doc: dict, **overrides) -> dict:
    tip = snap_doc["tip"]["slot"]
    plan = {
        "network": "mainnet",
        "pool_ref": snap_doc["inputs"]["pool"]["ref"],
        "fee_ref": snap_doc["inputs"]["fee"]["ref"],
        "cmatra_unit": CMATRA_UNIT,
        "old_script_hex": CODES["old"].hex(),
        "old_admins": OLD_ADMINS,
        "old_deadline_ms": OLD_DEADLINE_MS,
        "new_admins": [pkh("new_admin_1"), pkh("new_admin_2")],
        "new_deadline_ms": NEW_DEADLINE_MS,
        "new_script_hash": pm.plutus_v3_script_hash(CODES["new"]).hex(),
        "move": "all",
        "validity_start": tip - 120,
        "ttl": tip - 120 + 3 * 3600,
        "ex_units": EX_UNITS,
    }
    plan.update(overrides)
    return plan


def save_json(name: str, doc: dict) -> Path:
    path = OUT / name
    path.write_text(json.dumps(doc, indent=2))
    return path


def resolved_of(snap: pm.Snapshot, snap_doc: dict) -> list:
    out = []
    for key, utxo in (("pool", snap.pool), ("fee", snap.fee)):
        producing = bytes.fromhex(snap_doc["inputs"][key]["producing_tx"])
        out.append((utxo.ref, raw_output(producing, utxo.ref[1])))
    return sorted(out)


def migration(chain) -> dict:
    fee_tx = parent_tx([{0: key_address(pkh("new_admin_1")), 1: 20_000_000}], b"\x5c")
    fee_ref = f"{txid_of(fee_tx).hex()}#0"
    client = SimClient(chain, {txid_of(fee_tx).hex(): fee_tx})
    snap_doc = pm.snapshot_document(client, POOL_REF, fee_ref, "mainnet")
    again = pm.snapshot_document(client, POOL_REF, fee_ref, "mainnet")
    save_json("snapshot-live.json", snap_doc)
    snap = pm.load_snapshot(snap_doc)
    pool = snap.pool.output
    record("LIVE", "pool_unspent_at_snapshot", "TRUE", "TRUE" if not snap_doc["inputs"]["pool"]["consumed_by"] else "FALSE",
           note=f"tip slot {snap_doc['tip']['slot']} block {snap_doc['tip']['height']} from {snap_doc['source']}")
    record("LIVE", "pool_value", "301731979205950",
           str(pool.assets[CM_POLICY][CM_NAME]), note=f"{pool.coin} lovelace, Void inline datum")
    record("LIVE", "apply_params_old_pair_is_the_live_pool_script", pool.address[1:].hex(),
           pm.plutus_v3_script_hash(CODES["old"]).hex(), note="aiken v1.1.21 apply of the old admins and 11-29")

    # Race form: the whole pool in one transaction.
    plan_direct = plan_for(snap_doc)
    save_json("plan-direct.json", plan_direct)
    tx_direct = pm.build_migration_tx(snap, pm.load_plan(plan_direct))
    again_tx = pm.build_migration_tx(pm.load_snapshot(again), pm.load_plan(json.loads(json.dumps(plan_direct))))
    record("LIVE", "deterministic_from_two_snapshots", "SAME", "SAME" if again_tx == tx_direct else "DIFFERENT",
           note=f"sha256 {sha256(tx_direct)[:16]}")
    report_direct = pm.check_migration_tx(tx_direct, snap, pm.load_plan(plan_direct))
    save_json("report-direct.json", report_direct)
    write_envelope(OUT / "C1-direct.unsigned.tx", tx_direct, "C1 direct (simulation)")
    record("LIVE", "cardano_cli_txid_matches", report_direct["txid"],
           cli_txid(OUT / "C1-direct.unsigned.tx"))
    aiken("LIVE", "C1_direct_full_move", tx_direct, resolved_of(snap, snap_doc), "PASS",
          note=f"fee {report_direct['fee']} min {report_direct['min_fee']} size {report_direct['signed_size']}")

    # The destination: the tool derives it from the live bytes; aiken's apply must give the same bytes.
    new_admins = tuple(bytes.fromhex(pkh(k)) for k in ("new_admin_1", "new_admin_2"))
    derived = pm.reapplied_script(CODES["old"], tuple(bytes.fromhex(k) for k in OLD_ADMINS),
                                  OLD_DEADLINE_MS, new_admins, NEW_DEADLINE_MS)
    record("LIVE", "derived_destination_equals_aiken_apply", "SAME",
           "SAME" if derived == CODES["new"] else "DIFFERENT",
           note=f"{pm.plutus_v3_script_hash(derived).hex()[:8]}, offsets of the old constants unchanged")
    for name, script_hash in (
            ("N_plan_destination_another_script", pm.blake2b_224(b"\x03" + bytes.fromhex("450101002499")).hex()),
            ("N_plan_destination_validator_for_other_admins", pm.plutus_v3_script_hash(CODES["rehearsal"]).hex())):
        try:
            pm.build_migration_tx(snap, pm.load_plan({**plan_direct, "new_script_hash": script_hash}))
            outcome = "ACCEPTED"
        except pm.MigrationRejected as exc:
            outcome = f"REFUSED:{exc.code}"
        record("LIVE", name, "REFUSED:new_script", outcome, note="required signers unchanged: the real new admins")

    # Negatives against the live validator.
    def variant(name: str, expect: str, mutate, note: str = "") -> None:
        plan = pm.load_plan(plan_direct)
        body, witnesses = pm.migration_parts(snap, plan)
        mutate(body, witnesses)
        aiken("LIVE", name, pm.encode_tx(body, witnesses), resolved_of(snap, snap_doc), expect, note)

    variant("N_without_old_admin_2", "FAIL",
            lambda b, w: b.__setitem__(14, CBORTag(258, [s for s in b[14].value if s.hex() != OLD_ADMINS[1]])))
    variant("N_without_old_admin_1", "FAIL",
            lambda b, w: b.__setitem__(14, CBORTag(258, [s for s in b[14].value if s.hex() != OLD_ADMINS[0]])))
    variant("N_new_admins_only", "FAIL",
            lambda b, w: b.__setitem__(14, CBORTag(258, [bytes.fromhex(k) for k in plan_direct["new_admins"]])))
    variant("N_ttl_past_old_deadline", "FAIL",
            lambda b, w: (b.__setitem__(8, OLD_DEADLINE_SLOT - 600), b.__setitem__(3, OLD_DEADLINE_SLOT + 600)))
    variant("N_admin_withdraw_before_deadline", "FAIL",
            lambda b, w: w[5].__setitem__(next(iter(w[5])), [ADMIN_WITHDRAW, EX_UNITS]))
    variant("P_ttl_last_slot_the_tool_allows", "PASS",
            lambda b, w: (b.__setitem__(8, OLD_DEADLINE_SLOT - 3600), b.__setitem__(3, OLD_DEADLINE_SLOT - 1)))
    variant("P_ttl_at_deadline_slot_chain_allows", "PASS",
            lambda b, w: (b.__setitem__(8, OLD_DEADLINE_SLOT - 3600), b.__setitem__(3, OLD_DEADLINE_SLOT)),
            note="exclusive upper bound = deadline; the tool still stops one slot earlier")

    # Test slice, then the rest from the slice's outputs.
    plan_a = plan_for(snap_doc, move=SLICE)
    save_json("plan-C1a.json", plan_a)
    tx_a = pm.build_migration_tx(snap, pm.load_plan(plan_a))
    report_a = pm.check_migration_tx(tx_a, snap, pm.load_plan(plan_a))
    save_json("report-C1a.json", report_a)
    write_envelope(OUT / "C1a.unsigned.tx", tx_a, "C1a test slice (simulation)")
    aiken("LIVE", "C1a_test_slice", tx_a, resolved_of(snap, snap_doc), "PASS",
          note=f"{SLICE} to new pool, rest back to old pool; fee {report_a['fee']}")

    snap_b_doc = chained_snapshot(snap_doc, tx_a, pool_index=1, fee_index=2)
    save_json("snapshot-C1b.json", snap_b_doc)
    snap_b = pm.load_snapshot(snap_b_doc)
    plan_b = plan_for(snap_b_doc)
    save_json("plan-C1b.json", plan_b)
    tx_b = pm.build_migration_tx(snap_b, pm.load_plan(plan_b))
    report_b = pm.check_migration_tx(tx_b, snap_b, pm.load_plan(plan_b))
    save_json("report-C1b.json", report_b)
    write_envelope(OUT / "C1b.unsigned.tx", tx_b, "C1b rest (simulation)")
    aiken("LIVE", "C1b_rest_after_slice", tx_b, resolved_of(snap_b, snap_b_doc), "PASS",
          note=f"moves {report_b['moved_cmatra']}; fee {report_b['fee']}")
    record("LIVE", "cmatra_conserved_C1a_plus_C1b", str(301_731_979_205_950),
           str(report_a["moved_cmatra"] + report_b["moved_cmatra"]))

    # The pool output must carry the datum: the same C1b with a datum-less old pool output.
    datumless = decode(split_tx(tx_a)[0])[1][1]
    stripped = cbor2.dumps({0: datumless[0], 1: [datumless[1][0], {CM_POLICY: {CM_NAME: datumless[1][1][CM_POLICY][CM_NAME]}}]})
    resolved = [(ref, stripped if ref == snap_b.pool.ref else raw) for ref, raw in resolved_of(snap_b, snap_b_doc)]
    aiken("LIVE", "N_pool_output_without_datum_is_locked", tx_b, resolved, "FAIL",
          note="why every pool output carries D87980")
    return {"snap_doc": snap_doc, "tx_a": tx_a, "tx_b": tx_b, "report_a": report_a, "report_b": report_b}


# ---------------------------------------------------------------------------
# CLAIM: a surrender against the re-parameterized pool
# ---------------------------------------------------------------------------


PROCESS_SURRENDER = CBORTag(121, [])
ADMIN_WITHDRAW = CBORTag(122, [])


def claim_tx(pool_tx: bytes, pool_index: int, claimant_tx: bytes, signers: list[str], start: int, ttl: int,
             redeemer: CBORTag, params: pm.Params, payout: int, script: bytes, collateral_index: int = 0) -> bytes:
    """The claim spends ``claimant_tx``#0 and posts ``claimant_tx``#``collateral_index`` as collateral."""
    pool_ref = (txid_of(pool_tx), pool_index)
    claimant_ref = (txid_of(claimant_tx), 0)
    collateral_ref = (txid_of(claimant_tx), collateral_index)
    pool_out = decode(split_tx(pool_tx)[0])[1][pool_index]
    pool_coin, pool_cm = pool_out[1][0], pool_out[1][1][CM_POLICY][CM_NAME]
    claimant_out = decode(split_tx(claimant_tx)[0])[1][0]
    claimant_addr, claimant_coin = claimant_out[0], claimant_out[1][0]
    collateral_value = decode(split_tx(claimant_tx)[0])[1][collateral_index][1]
    collateral_coin = collateral_value if isinstance(collateral_value, int) else collateral_value[0]
    agent_policy, agent_name = bytes.fromhex(AGENT_UNIT[:56]), bytes.fromhex(AGENT_UNIT[56:])
    inputs = sorted([list(pool_ref), list(claimant_ref)])
    redeemers = {(0, inputs.index(list(pool_ref))): [redeemer, EX_UNITS]}
    witnesses = {5: redeemers, 7: CBORTag(258, [script])}
    sdh = blake2b_256(cbor2.dumps(redeemers) + pm.language_views(params.cost_model_v3))
    quarantine = {0: Address.from_primitive(QUARANTINE).to_primitive(), 1: [0, {agent_policy: {agent_name: TEST_AGENT_UNITS}}]}
    quarantine[1][0] = pm.min_ada(cbor2.dumps(quarantine), params) + 10_000
    continuation = {0: pool_out[0], 1: [pool_coin, {CM_POLICY: {CM_NAME: pool_cm - payout}}], 2: [1, CBORTag(24, bytes.fromhex("d87980"))]}
    change = {0: claimant_addr, 1: [0, {CM_POLICY: {CM_NAME: payout}}]}
    fee = 0
    for _ in range(8):
        total_collateral = math.ceil(fee * params.collateral_percent / 100)
        change[1][0] = claimant_coin - fee - quarantine[1][0]
        body = {0: CBORTag(258, inputs), 1: [continuation, quarantine, change], 2: fee, 3: ttl, 8: start, 11: sdh,
                13: CBORTag(258, [list(collateral_ref)]), 14: CBORTag(258, sorted(bytes.fromhex(s) for s in signers)),
                16: {0: claimant_addr, 1: collateral_coin - total_collateral}, 17: total_collateral}
        signed = {0: CBORTag(258, [[bytes([i]) * 32, bytes([i]) * 64] for i in range(len(signers) + 1)]), **witnesses}
        needed = pm.min_fee(len(pm.encode_tx(body, signed)), tuple(EX_UNITS), params) + 10_000
        if needed == fee:
            break
        fee = needed
    return pm.encode_tx(body, witnesses)


def claims(chain: dict) -> None:
    snap = pm.load_snapshot(chain["snap_doc"])
    params = snap.params
    new_script = CODES["new"]
    new_pool_bech32 = chain["report_a"]["new_pool_address"]
    tx_a = chain["tx_a"]
    claimant = pkh("claimant")
    agent_policy, agent_name = bytes.fromhex(AGENT_UNIT[:56]), bytes.fromhex(AGENT_UNIT[56:])
    claimant_tx = parent_tx([{0: key_address(claimant),
                              1: [10_000_000, {agent_policy: {agent_name: TEST_AGENT_UNITS}}]}], b"\x7e")
    rate_table = load_rate_table(REPO / "audit_pack/2026-04-19/rate_table_cmatra.json")
    pin = load_redemption_pin(REPO / "audit_pack/2026-09-27/redemption_pin.json")
    payout = surrendered_entitlement(rate_table, {AGENT_UNIT: TEST_AGENT_UNITS}, pin.nft_units)
    record("CLAIM", "entitlement_for_2_AGENT", "<=1000000", "<=1000000" if payout <= SLICE else str(payout),
           note=f"payout {payout} base units from the {SLICE} slice")
    new_admins = [pkh("new_admin_1"), pkh("new_admin_2")]
    tip = chain["snap_doc"]["tip"]["slot"]

    def resolved(tx: bytes) -> list:
        return sorted([((txid_of(tx_a), 0), raw_output(tx_a, 0)), ((txid_of(claimant_tx), 0), raw_output(claimant_tx, 0))])

    def build(signers, start, ttl, redeemer=PROCESS_SURRENDER, pay=payout):
        return claim_tx(tx_a, 0, claimant_tx, signers, start, ttl, redeemer, params, pay, new_script)

    main_tx = build(new_admins, tip - 60, tip + 10_000)
    aiken("CLAIM", "T_claim_new_admins_main_window", main_tx, resolved(main_tx), "PASS",
          note=f"2 AGENT -> {payout} cMATRA base units")

    env = {"NETWORK": "mainnet", "SURRENDER_SCRIPT_ADDRESS": new_pool_bech32, "QUARANTINE_ADDRESS": QUARANTINE,
           "CMATRA_POLICY_HEX": CM_POLICY.hex(), "CMATRA_ASSET_HEX": CM_NAME.hex(),
           "RATE_TABLE_PATH": str(REPO / "audit_pack/2026-04-19/rate_table_cmatra.json"),
           "REDEMPTION_PIN_PATH": str(REPO / "audit_pack/2026-09-27/redemption_pin.json"),
           "SURRENDER_DEADLINE_POSIX_MS": str(OLD_DEADLINE_MS), "MAX_CMATRA_PER_TX": str(4_000_000 * 10**6)}
    admins = [bytes.fromhex(k) for k in new_admins]
    views = pm.language_views(params.cost_model_v3)
    parents = [split_tx(tx_a)[0], split_tx(claimant_tx)[0]]

    def cosign(tx: bytes, now: int, deadline_ms: int) -> str:
        cfg = load_config({**env, "SURRENDER_DEADLINE_POSIX_MS": str(deadline_ms)}, admins)
        try:
            approval = evaluate_surrender(tx, views, parents, now, cfg)
        except CosignRejected as exc:
            return f"REFUSED:{exc.code}"
        return f"APPROVED:{approval.payout}"

    record("CLAIM", "cosign_policy_main_window_new_pool", f"APPROVED:{payout}", cosign(main_tx, tip, OLD_DEADLINE_MS),
           note="the signers' own policy, configured for the new pool")

    # Sign with the sim's new admins and claimant through cardano-cli.
    path = write_envelope(OUT / "claim.unsigned.tx", main_tx, "claim against the new pool (simulation)")
    vkeys = [pm._key_witness(envelope_cbor(cli_witness(path, k))) for k in ("new_admin_1", "new_admin_2", "claimant")]
    body_raw, witness_raw, rest = split_tx(main_tx)
    signed = b"\x84" + body_raw + pm._signed_witness_set(_witness_fields(witness_raw), CBORTag(258, sorted(vkeys))) + rest
    write_envelope(OUT / "claim.signed.tx", signed, "claim against the new pool, signed with sim keys")
    cfg = load_config(env, admins)
    approval = evaluate_surrender(signed, views, parents, tip, cfg)
    try:
        require_claimant_signature(signed, approval)
        outcome = "VERIFIED"
    except CosignRejected as exc:
        outcome = f"REFUSED:{exc.code}"
    record("CLAIM", "claimant_and_admin_signatures_verify", "VERIFIED", outcome)
    record("CLAIM", "cardano_cli_txid_of_signed_claim", txid_of(main_tx).hex(), cli_txid(OUT / "claim.signed.tx"))
    aiken("CLAIM", "T_signed_claim", signed, resolved(signed), "PASS")

    old = OLD_ADMINS
    t = build(old, tip - 60, tip + 10_000)
    aiken("CLAIM", "N_old_admins_cannot_spend_new_pool", t, resolved(t), "FAIL", note="the old keys lose all power")
    t = build(new_admins[:1], tip - 60, tip + 10_000)
    aiken("CLAIM", "N_one_new_admin", t, resolved(t), "FAIL")

    late = posix_ms_to_slot(1_797_000_000_000)   # 2026-12-11
    t = build(new_admins, late - 60, late + 10_000)
    aiken("CLAIM", "P_late_window_claim_on_chain", t, resolved(t), "PASS", note="compiled deadline 12-29")
    record("CLAIM", "cosign_late_claim_with_11_29_deadline", "REFUSED:after_deadline", cosign(t, late, OLD_DEADLINE_MS),
           note="the services keep the public window closing 11-29")
    record("CLAIM", "cosign_late_claim_with_12_29_deadline", f"APPROVED:{payout}", cosign(t, late, NEW_DEADLINE_MS),
           note="late window opens by config, no ceremony")

    t = build(new_admins, NEW_DEADLINE_SLOT - 60, NEW_DEADLINE_SLOT + 600)
    aiken("CLAIM", "N_claim_past_new_deadline", t, resolved(t), "FAIL")

    after = NEW_DEADLINE_SLOT + 120
    t = build(new_admins, after, after + 3600, ADMIN_WITHDRAW)
    aiken("CLAIM", "P_admin_withdraw_new_admins_after_12_29", t, resolved(t), "PASS", note="the post-window sweep")
    t = build(new_admins, tip - 60, tip + 10_000, ADMIN_WITHDRAW)
    aiken("CLAIM", "N_admin_withdraw_before_12_29", t, resolved(t), "FAIL")
    t = build(old, after, after + 3600, ADMIN_WITHDRAW)
    aiken("CLAIM", "N_admin_withdraw_old_admins_after_12_29", t, resolved(t), "FAIL")

    # C1b leaves the bulk as the largest UTxO at the new address, which the services spend.
    tx_b = chain["tx_b"]
    t = claim_tx(tx_b, 0, claimant_tx, new_admins, tip - 60, tip + 10_000, PROCESS_SURRENDER, params, payout, new_script)
    aiken("CLAIM", "T_claim_against_C1b_bulk", t,
          sorted([((txid_of(tx_b), 0), raw_output(tx_b, 0)), ((txid_of(claimant_tx), 0), raw_output(claimant_tx, 0))]),
          "PASS")

    api_claim(tx_a, new_pool_bech32, params, new_script, new_admins, env, payout, tip)


# ---------------------------------------------------------------------------
# API: the runbook's C0, P11 and step 13 blocks, run as written
# ---------------------------------------------------------------------------

SIM_API_SECRET = "sim-only-not-a-secret"
REAL_SURRENDER = CHAIN / "pool-parent.cbor.json"   # d3a1e632, built by surrender-api, paid to SUS
SHELL_SETTINGS = ("CLI", "APP", "PY", "API", "API_ENV")
# Where the blocks after C0 may not reach on their own: every host location goes through C0's settings.
HOST_LOCATION = re.compile(r"/home/|/etc/|/opt/|/var/lib/|:8420\b|\bsudo -u\b")


def runbook_blocks(prefix: str, text: str | None = None) -> list[str]:
    """The fenced bash blocks of ``text`` (RUNBOOK.md unless given) whose first
    line starts with ``# <prefix>``, in order."""
    text = (HERE / "RUNBOOK.md").read_text() if text is None else text
    return re.findall(rf"^```bash\n(# {prefix}.*?)^```$", text, re.DOTALL | re.MULTILINE)


def sim_shell(c0: str, settings: dict[str, str]) -> str:
    """Block C0 with each of its settings pointed at the simulation's own, and
    sudo as a plain call: the sim's env file is its own user's."""
    for name in SHELL_SETTINGS:
        c0, count = re.subn(rf"^{name}=\S*$", f"{name}={shlex.quote(settings[name])}", c0, flags=re.MULTILINE)
        if count != 1:
            raise SystemExit(f"block C0 sets {name} {count} times, not once")
    return 'sudo() { "$@"; }\n' + c0


def stand_in(built: bytes, seen: dict, answer_hash: str | None = None) -> ThreadingHTTPServer:
    """surrender-api's two claim routes for one claim: its X-API-Secret check,
    its request models, and its merge of the claimant's witnesses into
    ``built``, the transaction the build answers with, under ``answer_hash``
    (its own txid unless given)."""
    tx_hash = answer_hash or txid_of(built).hex()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def answer(self, status: int, doc: dict) -> None:
            body = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.headers.get("X-API-Secret") != SIM_API_SECRET:
                return self.answer(403, {"detail": "Forbidden"})
            try:
                if self.path == "/build-surrender":
                    seen["request"] = api.BuildSurrenderRequest.model_validate_json(raw)
                    return self.answer(200, {"tx_cbor_hex": built.hex(), "tx_hash": tx_hash, "redemption_summary": {},
                                             "total_cmatra_display": 0.0, "pool_utxo_used": "sim"})
                if self.path == "/submit-surrender":
                    request = api.SubmitRequest.model_validate_json(raw)
                    if request.tx_hash != tx_hash:
                        return self.answer(403, {"detail": "Transaction was not built by this service"})
                    seen["merged"] = api._merge_wallet_witnesses(built, bytes.fromhex(request.tx_cbor_hex))
                    return self.answer(200, {"tx_hash": tx_hash})
            except ValueError as exc:
                return self.answer(400, {"detail": str(exc)[:200]})
            return self.answer(404, {"detail": "Not Found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_blocks(blocks: list[str], workdir: Path, built: bytes | None = None,
               answer_hash: str | None = None) -> tuple[subprocess.CompletedProcess, dict]:
    """Block C0 with the simulation's settings, then ``blocks`` as written, in
    one bash without errexit, as an operator's shell runs them, in
    ``workdir``. The service's env file is a sim-only one, and its URL the
    stand-in serving ``built``. Refuses blocks that reach a host location
    without going through C0."""
    env_file = workdir / "sim.env"
    env_file.write_text(f'SURRENDER_API_SECRET="{SIM_API_SECRET}"\n')
    for block in blocks:
        if HOST_LOCATION.search(block):
            raise SystemExit(f"a runbook block names a host location instead of a C0 setting: {block.splitlines()[0]}")
    seen: dict = {}
    server = stand_in(built, seen, answer_hash) if built is not None else None
    settings = {"CLI": cardano_cli(), "APP": str(REPO), "PY": sys.executable, "API_ENV": str(env_file),
                "API": f"http://127.0.0.1:{server.server_port}" if server else "http://127.0.0.1:9"}
    c0, = runbook_blocks("C0")
    try:
        run = subprocess.run(["bash", "-c", sim_shell(c0, settings) + "".join(blocks)], check=False, cwd=workdir,
                             capture_output=True, text=True, timeout=180)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
    return run, seen


def tail(run: subprocess.CompletedProcess) -> str:
    return " ".join((run.stderr or run.stdout).split())[-160:]


def step13_outcome(step13: list[str], name: str, served: bytes, answer_hash: str | None = None) -> str:
    """13a-13c run with T's files against a build answer of ``served``, in a
    directory an earlier attempt left signed files in: SIGNED if T signed or
    those files survived, otherwise where 13b stopped (before the view, or
    the output check's last word)."""
    workdir = OUT / "api-stops" / name
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    for file in ("T.skey", "T.addr", "new-pool.addr"):
        (workdir / file).symlink_to(OUT / "api" / file)
    for file in ("claim.T.signed.tx", "claim.submit.json"):
        (workdir / file).write_text("left by an earlier attempt\n")
    run, seen = run_blocks(step13, workdir, served, answer_hash)
    if (workdir / "claim.T.signed.tx").exists() or "merged" in seen:
        return "SIGNED"
    if not (workdir / "claim.view.json").exists():
        return "STOPPED:before-view"
    lines = run.stdout.strip().splitlines()
    return f"STOPPED:{lines[-1] if lines else 'nothing'}"


def altered(tx: bytes, change) -> bytes:
    """``tx`` with ``change`` applied to its decoded body and its witness set kept as it was."""
    body_raw, witness_raw, rest = split_tx(tx)
    body = api._pure_loads(body_raw)
    change(body)
    return b"\x84" + api._pure_dumps(body) + witness_raw + rest


def api_claim(tx_a: bytes, new_pool_bech32: str, params: pm.Params, new_script: bytes, new_admins: list[str],
              env: dict, payout: int, tip: int) -> None:
    work = OUT / "api"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    p11, step13 = runbook_blocks("P11"), runbook_blocks("13")
    record("API", "runbook_blocks_P11_and_13abc", "1+3", f"{len(p11)}+{len(step13)}")
    if len(p11) != 1 or len(step13) != 3:
        return

    run, _ = run_blocks(p11, work)
    record("API", "P11_block_makes_T", "0", str(run.returncode), note=tail(run))
    if run.returncode:
        return
    t_addr = (work / "T.addr").read_text().strip()
    t_bytes = Address.from_primitive(t_addr).to_primitive()
    t_key = t_bytes[1:29].hex()
    record("API", "T_is_an_enterprise_key_address", "0x61", hex(t_bytes[0]))
    record("API", "T_skey_mode", "0o400", oct((work / "T.skey").stat().st_mode & 0o777))

    # P11's funding: 2 AGENT with 5 ADA, and 5 ADA alone for the collateral surrender-api takes.
    agent_policy, agent_name = bytes.fromhex(AGENT_UNIT[:56]), bytes.fromhex(AGENT_UNIT[56:])
    t_fund = parent_tx([{0: t_bytes, 1: [5_000_000, {agent_policy: {agent_name: TEST_AGENT_UNITS}}]},
                        {0: t_bytes, 1: 5_000_000}], b"\x7f")
    claim = claim_tx(tx_a, 0, t_fund, new_admins, tip - 60, tip + 10_000, PROCESS_SURRENDER, params, payout,
                     new_script, collateral_index=1)
    (work / "new-pool.addr").write_text(new_pool_bech32 + "\n")
    run, seen = run_blocks(step13, work, claim)
    record("API", "step13_blocks_as_written", "0", str(run.returncode), note=tail(run))
    request = seen.get("request")
    record("API", "build_request_fits_service_model", f"{t_addr}:AGENT:2",
           f"{request.user_address}:{request.assets[0].asset_key}:{request.assets[0].quantity_base}"
           if request and len(request.assets) == 1 else "NONE")
    merged = seen.get("merged")
    if merged is None:
        record("API", "submit_reached_the_merge", "TRUE", "FALSE")
        return
    record("API", "submit_keeps_built_body", "SAME", "SAME" if split_tx(merged)[0] == split_tx(claim)[0] else "DIFFERENT")
    pairs, _ = api._vkey_witness_pairs(api._pure_loads(split_tx(merged)[1]).get(0, []))
    record("API", "submit_takes_only_T_witness", t_key, ",".join(sorted(pm.blake2b_224(p[0]).hex() for p in pairs)))

    # What /submit-surrender does next: both admins' witnesses, then the signers' checks.
    admin_pairs = [pm._key_witness(envelope_cbor(cli_witness(work / "claim.unsigned.tx", k)))
                   for k in ("new_admin_1", "new_admin_2")]
    full = api._merge_vkey_witnesses(merged, admin_pairs, 258)
    cfg = load_config(env, [bytes.fromhex(k) for k in new_admins])
    parents = [split_tx(tx_a)[0], split_tx(t_fund)[0]]
    try:
        approval = evaluate_surrender(full, pm.language_views(params.cost_model_v3), parents, tip, cfg)
        require_claimant_signature(full, approval)
        outcome = f"VERIFIED:{approval.payout}"
    except CosignRejected as exc:
        outcome = f"REFUSED:{exc.code}"
    record("API", "cosign_policy_and_T_signature", f"VERIFIED:{payout}", outcome)
    aiken("API", "T_api_path_signed_claim", full,
          sorted([((txid_of(tx_a), 0), raw_output(tx_a, 0))]
                 + [((txid_of(t_fund), i), raw_output(t_fund, i)) for i in (0, 1)]), "PASS")

    bare = envelope_cbor(cli_witness(work / "claim.unsigned.tx", "T", work / "T.skey"))
    try:
        api._merge_wallet_witnesses(claim, bare)
        outcome = "ACCEPTED"
    except ValueError:
        outcome = "REFUSED"
    record("API", "N_bare_cli_witness_refused_by_submit", "REFUSED", outcome,
           note="`transaction witness` writes one [vkey, sig] pair, not a witness set")

    # 13b must stop before T signs whenever one of its checks fails; each variant fails exactly one.
    sus = Address.from_primitive("addr1qx53hw2d3xhh3eqt6skfnqw0r7cjvm09yw3f6exvrlk4j88pjuvalvn7yf4ec5msyjgan7ur5hjs7d7x59jnfmjchngs5sp0tr").to_primitive()

    def pay_one_more(body: dict) -> None:
        body[1][0][1][1][CM_POLICY][CM_NAME] -= 1
        body[1][2][1][1][CM_POLICY][CM_NAME] += 1

    record("API", "N_13b_stops_on_a_txid_the_body_does_not_hash_to", "STOPPED:before-view",
           step13_outcome(step13, "txid", claim, "00" * 32))
    for name, change in (
            ("an_output_to_another_address", lambda b: b[1].append({0: sus, 1: 1_000_000})),
            ("a_new_pool_output_without_datum", lambda b: b[1][0].pop(2)),
            ("a_payout_other_than_925780", pay_one_more),
            ("a_mint", lambda b: b.__setitem__(9, {agent_policy: {b"x": 1}})),
            ("a_withdrawal", lambda b: b.__setitem__(5, {b"\xe1" + bytes(28): 0})),
            ("a_certificate", lambda b: b.__setitem__(4, [[0, [0, bytes(28)]]])),
            ("collateral_returned_elsewhere", lambda b: b[16].__setitem__(0, sus))):
        record("API", f"N_13b_stops_on_{name}", "STOPPED:false", step13_outcome(step13, name, altered(claim, change)))

    # A transaction the service really built: the last attacker claim, without its vkey witnesses.
    real = bytes.fromhex(json.loads(REAL_SURRENDER.read_text())["cbor"])
    body_raw, witness_raw, rest = split_tx(real)
    witness_set = api._pure_loads(witness_raw)
    witness_set.pop(0, None)
    served = b"\x84" + body_raw + api._pure_dumps(witness_set) + rest
    record("API", "N_13b_stops_on_the_real_attacker_claim", "STOPPED:false", step13_outcome(step13, "real", served),
           note="d3a1e632 pays SUS")

    write_envelope(work / "served.tx", served, "d3a1e632 as surrender-api served it, before any witness")
    record("API", "cardano_cli_txid_of_service_built_tx", txid_of(real).hex(), cli_txid(work / "served.tx"))
    subprocess.run([cardano_cli(), "latest", "transaction", "sign", "--tx-file", str(work / "served.tx"),
                    "--signing-key-file", str(work / "T.skey"), "--mainnet", "--out-file",
                    str(work / "served.T.signed.tx")], check=True, capture_output=True)
    merged = api._merge_wallet_witnesses(served, envelope_cbor(work / "served.T.signed.tx"))
    pairs, _ = api._vkey_witness_pairs(api._pure_loads(split_tx(merged)[1]).get(0, []))
    verifies = (split_tx(merged)[0] == body_raw and len(pairs) == 1
                and pm.blake2b_224(pairs[0][0]).hex() == t_key
                and signature_verifies(pairs[0][0], blake2b_256(body_raw), pairs[0][1]))
    record("API", "cardano_cli_sign_on_service_framing", "VERIFIED", "VERIFIED" if verifies else "BROKEN",
           note="tag-258 framing as surrender-api writes it; body bytes kept")


# ---------------------------------------------------------------------------
# REHEARSAL: the signing pipeline with real signatures
# ---------------------------------------------------------------------------


def rehearsal(chain: dict) -> None:
    snap_doc = chain["snap_doc"]
    old_script = CODES["rehearsal"]
    pool = pm.load_snapshot(snap_doc).pool.output
    pool_tx = parent_tx([{0: script_address(pm.plutus_v3_script_hash(old_script)),
                          1: [pool.coin, {CM_POLICY: {CM_NAME: pool.assets[CM_POLICY][CM_NAME]}}],
                          2: [1, CBORTag(24, bytes.fromhex("d87980"))]}], b"\x3d")
    reh_doc = {**snap_doc, "inputs": {
        "pool": {"ref": f"{txid_of(pool_tx).hex()}#0", "producing_tx": pool_tx.hex(), "consumed_by": None},
        "fee": snap_doc["inputs"]["fee"]}}
    snap_path = save_json("rehearsal-snapshot.json", reh_doc)
    plan = plan_for(reh_doc, old_script_hex=old_script.hex(), old_admins=[pkh("old_admin_1"), pkh("old_admin_2")])
    plan_path = save_json("rehearsal-plan.json", plan)
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    common = ["--snapshot", str(snap_path), "--plan", str(plan_path)]
    unsigned_path = OUT / "rehearsal.unsigned.tx"

    def cli(*args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "tools.pool_migration", *args],
                              check=False, cwd=REPO, env=env, capture_output=True, text=True)

    first = cli("build", *common, "--out", str(unsigned_path))
    first_bytes = envelope_cbor(unsigned_path)
    second = cli("build", *common, "--out", str(OUT / "rehearsal.unsigned.again.tx"))
    record("REHEARSAL", "cli_build_twice_same_bytes", "SAME",
           "SAME" if first.returncode == second.returncode == 0
           and envelope_cbor(OUT / "rehearsal.unsigned.again.tx") == first_bytes else "DIFFERENT",
           note=f"sha256 {sha256(first_bytes)[:16]}")
    report = json.loads(first.stdout)
    record("REHEARSAL", "cardano_cli_txid_unsigned", report["txid"], cli_txid(unsigned_path))

    witness_args = []
    for key in ("old_admin_1", "old_admin_2", "new_admin_1", "new_admin_2"):
        witness_args += ["--witness", str(cli_witness(unsigned_path, key))]
    signed_path = OUT / "rehearsal.signed.tx"
    run = cli("assemble", *common, "--tx", str(unsigned_path), *witness_args, "--out", str(signed_path))
    record("REHEARSAL", "assemble_with_four_cardano_cli_witnesses", "0", str(run.returncode),
           note=run.stderr.strip()[:120])
    signed = envelope_cbor(signed_path)
    signed_report = json.loads(run.stdout)
    record("REHEARSAL", "cardano_cli_txid_signed", report["txid"], cli_txid(signed_path))
    record("REHEARSAL", "fee_covers_signed_size", "TRUE",
           "TRUE" if signed_report["fee"] >= signed_report["min_fee"] and signed_report["signed_size"] == len(signed)
           else "FALSE", note=f"fee {signed_report['fee']} min {signed_report['min_fee']} size {len(signed)}")
    run = cli("check", *common, "--tx", str(signed_path))
    record("REHEARSAL", "check_signed", "0", str(run.returncode))
    resolved = sorted([((txid_of(pool_tx), 0), raw_output(pool_tx, 0)),
                       (pm._ref(reh_doc["inputs"]["fee"]["ref"]),
                        raw_output(bytes.fromhex(reh_doc["inputs"]["fee"]["producing_tx"]), 0))])
    aiken("REHEARSAL", "signed_migration_on_the_same_source", signed, resolved, "PASS")

    run = cli("assemble", *common, "--tx", str(unsigned_path), *witness_args[:6], "--out", str(OUT / "x.tx"))
    record("REHEARSAL", "assemble_refuses_three_of_four", "1", str(run.returncode), note=run.stderr.strip()[:80])

    # What cardano-cli's own assemble does to the bytes the body commits to.
    cli_signed = OUT / "rehearsal.cardano-cli-assembled.tx"
    subprocess.run([cardano_cli(), "latest", "transaction", "assemble", "--tx-body-file", str(unsigned_path),
                    *[a for pair in zip(["--witness-file"] * 4, witness_args[1::2], strict=True) for a in pair],
                    "--out-file", str(cli_signed)], check=True, capture_output=True)
    run = cli("check", *common, "--tx", str(cli_signed))
    record("REHEARSAL", "cardano_cli_assemble_also_passes_check", "0", str(run.returncode),
           note=(run.stderr.strip() or "same body and redeemer bytes")[:100])
    view = subprocess.run([cardano_cli(), "debug", "transaction", "view", "--tx-file", str(signed_path)],
                          check=False, capture_output=True, text=True)
    (OUT / "rehearsal.signed.view.json").write_text(view.stdout + view.stderr)
    record("REHEARSAL", "cardano_cli_decodes_signed", "0", str(view.returncode))


def run(out: Path, live: bool = False) -> list[dict]:
    """Every group, writing into ``out``; the records of this run."""
    out.mkdir(parents=True, exist_ok=True)
    prepare(out)
    del results[:]
    chain = migration(chain_client(live))
    claims(chain)
    rehearsal(chain)
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str))
    return list(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--live", action="store_true", help="read the pool through the configured chain provider")
    parser.add_argument("--out", type=Path, default=HERE / "out")
    args = parser.parse_args(argv)
    records = run(args.out.resolve(), args.live)
    bad = [r for r in records if not r["ok"]]
    print(f"\n{len(records) - len(bad)}/{len(records)} as expected")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
