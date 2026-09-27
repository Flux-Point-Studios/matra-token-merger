"""surrender_api validates what it is asked to surrender, prices the exact
units it will quarantine, checks its own transaction with the co-signer's
policy before signing, and hands the co-signer everything it needs to check
the transaction again independently.

The co-signer here is the real service app, run in-process with throwaway
keys against a synthetic preprod-shaped pool."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import nacl.signing
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pycardano import (
    Address,
    Asset,
    AssetName,
    MultiAsset,
    Network,
    PaymentSigningKey,
    PlutusV3Script,
    RawPlutusData,
    TransactionBody,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    plutus_script_hash,
)
from pycardano.hash import ScriptHash, TransactionId, VerificationKeyHash

import services.cosigner_api as cosigner
import services.surrender_api as api
from services.cosign_policy import SLOT_OFFSET_S, CosignRejected, decode, load_config, split_tx
from services.pool_tip import PoolTipManager
from tests.test_surrender_redeemer_index import _SCRIPT_HEX, _FakeContext
from tools.config import AGENT, FLUX_PASS, T1_ADAM_PASS
from tools.process_surrender import load_rate_table, load_redeemable_nfts, surrendered_entitlement

ROOT = Path(__file__).resolve().parent.parent
RATES = load_rate_table(ROOT / "audit_pack/2026-04-19/rate_table_cmatra.json")
REDEEMABLE = load_redeemable_nfts(ROOT / "audit_pack/2026-09-27/redeemable_nft_units.json")
T1_UNITS = sorted(u for u in REDEEMABLE if u.startswith(T1_ADAM_PASS.policy_id))
FLUX_UNIT = next(u for u in sorted(REDEEMABLE) if u.startswith(FLUX_PASS.policy_id))
MAINNET_USER = (
    "addr1q9s6m9d8yedfcf53yhq5j5zsg0s58wpzamwexrxpfelgz2wgk0s9l9fqc93tyc8zu4z7hp9dlska2kew9trdg8nscjcq3sk5s3"
)


# ---------------------------------------------------------------------------
# Layer 1a: request validation
# ---------------------------------------------------------------------------


def _item(asset_key: str, quantity: int, nft_units: list[str] | None = None):
    return api.AssetToSurrender(asset_key=asset_key, quantity_base=quantity, nft_units=nft_units)


@pytest.fixture
def validating(monkeypatch):
    """Everything wired up to the build; the build itself records its
    arguments and stops."""
    seen: dict = {}

    def build(user_address, total_cmatra, legacy_assets, pool_utxo, pending=False, user_inputs=None):
        seen.update(total=total_cmatra, legacy=legacy_assets)
        raise RuntimeError("stop after validation")

    script_addr = Address(plutus_script_hash(PlutusV3Script(bytes.fromhex(_SCRIPT_HEX))), network=Network.MAINNET)
    monkeypatch.setattr(api, "_build_tx_blocking", build)
    monkeypatch.setattr(api, "SCRIPT_ADDRESS", script_addr.encode())
    monkeypatch.setattr(api, "CMATRA_POLICY_HEX", "ab" * 28)
    monkeypatch.setattr(api, "CMATRA_ASSET_HEX", "634d41545241")
    monkeypatch.setattr(api, "QUARANTINE_ADDRESS", script_addr.encode())
    monkeypatch.setattr(api, "ALLOWED_USER_ADDRESSES", frozenset())
    monkeypatch.setattr(api.state, "rate_table", RATES)
    monkeypatch.setattr(api.state, "redeemable_nfts", REDEEMABLE)
    monkeypatch.setattr(api.state, "script_cbor_hex", _SCRIPT_HEX)
    monkeypatch.setattr(api.state, "admin_sk", object())
    monkeypatch.setattr(api.state, "tip_mgr", PoolTipManager(
        lambda: [{"tx_hash": "a" * 64, "output_index": 0,
                  "cmatra_amount": 10**15, "ada_amount": 1_500_000}],
        script_address=script_addr.encode(), depth_cap=8))
    return seen


def _build(assets) -> None:
    asyncio.run(api.build_surrender(
        api.BuildSurrenderRequest(user_address=MAINNET_USER, assets=assets)))


@pytest.mark.parametrize("assets,reason", [
    # The count-inflation reproduction: a hundred passes paid, one junk NFT moved.
    ([_item("T1_ADAM_PASS", 100, ["ee" * 28 + "6a756e6b"])], "names 1 unit"),
    ([_item("T1_ADAM_PASS", 1, ["ee" * 28 + "6a756e6b"])], "not a redeemable"),
    ([_item("T1_ADAM_PASS", 100, [T1_UNITS[0]])], "names 1 unit"),
    ([_item("T1_ADAM_PASS", 2, [T1_UNITS[0], T1_UNITS[0]])], "listed twice"),
    ([_item("T1_ADAM_PASS", 1, [FLUX_UNIT])], "not a redeemable T1_ADAM_PASS"),
    ([_item("T1_ADAM_PASS", 1, [T1_ADAM_PASS.policy_id + "5431414441"])], "not a redeemable"),
    ([_item("T1_ADAM_PASS", 1)], "requires nft_units"),
    ([_item("AGENT", 5, [T1_UNITS[0]])], "nft_units"),
    ([_item("AGENT", 5), _item("AGENT", 6)], "listed twice"),
    ([_item("NOT_A_MERGE_ASSET", 1)], "Unknown asset key"),
])
def test_malformed_surrender_requests_are_refused_before_any_build(validating, assets, reason):
    with pytest.raises(HTTPException) as err:
        _build(assets)
    assert err.value.status_code == 400
    assert reason in str(err.value.detail)
    assert validating == {}
    assert api.state.tip_mgr.chain_state() is None  # the tip was never acquired


def test_valid_request_is_priced_by_the_units_it_quarantines(validating):
    with pytest.raises(HTTPException):
        _build([_item("AGENT", 1000), _item("T1_ADAM_PASS", 2, T1_UNITS[:2])])
    units = {AGENT.unit: 1000, T1_UNITS[0]: 1, T1_UNITS[1]: 1}
    assert validating["total"] == surrendered_entitlement(RATES, units, REDEEMABLE)
    assert sorted((a["policy_hex"] + a["asset_hex"], a["quantity"]) for a in validating["legacy"]) == (
        sorted(units.items())
    )


# ---------------------------------------------------------------------------
# Layers 1b and 2: the built transaction, checked locally and by the co-signer
# ---------------------------------------------------------------------------

SECRET = "c" * 43
NETWORK = "preprod"
NOW = 100_000_000  # _FakeContext.last_block_slot
CMATRA_POLICY = "ab" * 28
CMATRA_NAME = "634d41545241"
AGENT_POLICY, AGENT_NAME = AGENT.policy_id, AGENT.asset_name_hex


def _body_hash(body: TransactionBody) -> str:
    return hashlib.blake2b(body.to_cbor(), digest_size=32).hexdigest()


class World:
    """A synthetic pool, quarantine and claimant whose every UTxO is an output
    of a producing body the fake Blockfrost can serve."""

    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.admin_sk = PaymentSigningKey.generate()
        self.cosigner_sk = PaymentSigningKey.generate()
        script = PlutusV3Script(bytes.fromhex(_SCRIPT_HEX))
        self.pool_addr = Address(plutus_script_hash(script), network=Network.TESTNET)
        self.quarantine_addr = Address(ScriptHash(b"\x55" * 28), network=Network.TESTNET)
        self.user_addr = Address(VerificationKeyHash(b"\x22" * 28), network=Network.TESTNET)
        self.chain: dict[str, bytes] = {}

        agent = MultiAsset()
        agent[ScriptHash(bytes.fromhex(AGENT_POLICY))] = Asset({AssetName(bytes.fromhex(AGENT_NAME)): 5_000})
        user_body = self._produce([
            TransactionOutput(self.user_addr, Value(100_000_000)),
            TransactionOutput(self.user_addr, Value(5_000_000, agent)),
        ])
        self.user_utxos = [
            UTxO(TransactionInput(TransactionId(bytes.fromhex(user_body)), i), out)
            for i, out in enumerate(TransactionBody.from_cbor(self.chain[user_body]).outputs)
        ]
        pool_cmatra = MultiAsset()
        pool_cmatra[ScriptHash(bytes.fromhex(CMATRA_POLICY))] = Asset({AssetName(bytes.fromhex(CMATRA_NAME)): 10**15})
        pool_body = self._produce([TransactionOutput(
            self.pool_addr, Value(1_500_000, pool_cmatra), datum=RawPlutusData(api.cbor2.loads(bytes.fromhex("d87980"))),
        )])
        self.pool_utxo = {"tx_hash": pool_body, "output_index": 0,
                          "cmatra_amount": 10**15, "ada_amount": 1_500_000}

        env = {
            "NETWORK": NETWORK,
            "SURRENDER_SCRIPT_ADDRESS": self.pool_addr.encode(),
            "QUARANTINE_ADDRESS": self.quarantine_addr.encode(),
            "CMATRA_POLICY_HEX": CMATRA_POLICY,
            "CMATRA_ASSET_HEX": CMATRA_NAME,
            "SURRENDER_DEADLINE_POSIX_MS": str((NOW + 10**6 + SLOT_OFFSET_S[NETWORK]) * 1000),
            "MAX_CMATRA_PER_TX": str(10**14),
            "MAX_CMATRA_PER_DAY": str(10**15),
            "COSIGNER_API_SECRET": SECRET,
            "COSIGNER_PRIMARY_ADMIN_PKH": self.admin_sk.to_verification_key().hash().payload.hex(),
            "COSIGNER_LEDGER_PATH": str(tmp_path / "cosigned.sqlite3"),
            "COSIGNER_SKEY_PATH": str(tmp_path / "throwaway.skey"),
        }
        self.cosigner_sk.save(env["COSIGNER_SKEY_PATH"])
        self.ledger = env["COSIGNER_LEDGER_PATH"]
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(cosigner.time, "time", lambda: NOW + SLOT_OFFSET_S[NETWORK])

        self.client = TestClient(cosigner.app)
        self.client.__enter__()
        self.signed_by_admin: list[bytes] = []
        self.cosign_calls = 0
        self._wire_surrender_api(env)

    def _produce(self, outputs) -> str:
        body = TransactionBody(
            inputs=[TransactionInput(TransactionId(bytes(32)), len(self.chain))],
            outputs=outputs, fee=0,
        )
        self.chain[_body_hash(body)] = body.to_cbor()
        return _body_hash(body)

    def _wire_surrender_api(self, env) -> None:
        m, world = self.monkeypatch, self
        cosigner_pkh = self.cosigner_sk.to_verification_key().hash()
        admin_pkh = self.admin_sk.to_verification_key().hash()

        class _SigningSpy:
            def sign(self, data):
                world.signed_by_admin.append(data)
                return world.admin_sk.sign(data)

            def __getattr__(self, name):
                return getattr(world.admin_sk, name)

        class _BF:
            project_id = "x"
            base_url = "https://cardano-preprod.blockfrost.io/api/v0"

            def get_tx_cbor(self, tx_hash):
                if tx_hash not in world.chain:
                    raise RuntimeError(f"404 {tx_hash}")
                return b"\x84" + world.chain[tx_hash] + b"\xa0\xf5\xf6"

        def post(url, json, headers, timeout):
            assert url == "http://cosigner.test/cosign"
            world.cosign_calls += 1
            return world.client.post("/cosign", json=json, headers=headers)

        ctx = _FakeContext(self.user_utxos)
        m.setattr(api, "SCRIPT_ADDRESS", self.pool_addr.encode())
        m.setattr(api, "QUARANTINE_ADDRESS", self.quarantine_addr.encode())
        m.setattr(api, "CMATRA_POLICY_HEX", CMATRA_POLICY)
        m.setattr(api, "CMATRA_ASSET_HEX", CMATRA_NAME)
        m.setattr(api, "COSIGNER_URL", "http://cosigner.test")
        m.setattr(api, "COSIGNER_SECRET", SECRET)
        m.setattr(api, "COLLATERAL_UTXO", "")
        m.setattr(api, "BlockFrostChainContext", lambda **kw: ctx)
        m.setattr(api.httpx, "post", post)
        m.setattr(api.time, "time", lambda: NOW + SLOT_OFFSET_S[NETWORK])
        m.setattr(api.state, "script_cbor_hex", _SCRIPT_HEX)
        m.setattr(api.state, "admin_sk", _SigningSpy())
        m.setattr(api.state, "admin_pkh", admin_pkh)
        m.setattr(api.state, "admin_addr", Address(admin_pkh, network=Network.TESTNET))
        m.setattr(api.state, "cosigner_pkh", cosigner_pkh)
        m.setattr(api.state, "bf", _BF())
        m.setattr(api.state, "built_bodies", {})
        m.setattr(api.state, "cosign_config", load_config(env, [admin_pkh.payload, cosigner_pkh.payload]))
        api.state.reserved_collateral.clear()
        api.state.user_pending.clear()

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    def cosigned_rows(self) -> int:
        with sqlite3.connect(self.ledger) as conn:
            return conn.execute("SELECT COUNT(*) FROM cosigned").fetchone()[0]

    def build(self, total: int, legacy_qty: int = 1_000, pool_utxo=None, user_inputs=None):
        legacy = [{"policy_hex": AGENT_POLICY, "asset_hex": AGENT_NAME, "quantity": legacy_qty}]
        return api._build_cosigned_surrender_tx(
            self.user_addr.encode(), total, legacy, pool_utxo or self.pool_utxo, user_inputs,
        )


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    w.close()


def _entitlement(qty: int) -> int:
    return surrendered_entitlement(RATES, {AGENT.unit: qty}, REDEEMABLE)


def _vkey_witnesses(tx_hex: str) -> list:
    ws = decode(split_tx(bytes.fromhex(tx_hex))[1])
    vkeys = ws[0]
    return list(vkeys.value) if hasattr(vkeys, "tag") else vkeys


def test_valid_surrender_carries_both_admin_signatures(world):
    tx_hex, tx_hash, _, _ = world.build(_entitlement(1_000))
    witnesses = {bytes(vk): bytes(sig) for vk, sig in _vkey_witnesses(tx_hex)}
    cosigner_vk = bytes(world.cosigner_sk.to_verification_key().payload)
    nacl.signing.VerifyKey(cosigner_vk).verify(bytes.fromhex(tx_hash), witnesses[cosigner_vk])
    assert bytes(world.admin_sk.to_verification_key().payload) in witnesses
    assert world.cosigned_rows() == 1


def test_inflated_payout_is_refused_before_either_admin_signs(world):
    with pytest.raises(CosignRejected) as err:
        world.build(_entitlement(1_000) * 100)
    assert err.value.code == "payout_mismatch"
    assert world.signed_by_admin == []
    assert world.cosign_calls == 0
    assert world.cosigned_rows() == 0


def test_cosigner_refuses_even_if_the_primary_skips_its_own_check(world, monkeypatch):
    """The co-signer checks independently: a primary that skips its own check
    still does not obtain the second signature."""
    monkeypatch.setattr(api, "check_surrender", lambda *a, **k: None)
    with pytest.raises(HTTPException) as err:
        world.build(_entitlement(1_000) * 100)
    assert err.value.status_code == 503
    assert world.cosign_calls == 1
    assert world.cosigned_rows() == 0


def test_chained_chunk_resolves_its_unconfirmed_parents_from_built_bodies(world):
    first_hex, first_hash, pool_out, user_change = world.build(_entitlement(1_000))
    assert first_hash not in world.chain  # still "in the mempool" for Blockfrost
    chained_pool = {"tx_hash": first_hash, "output_index": 1,
                    "cmatra_amount": pool_out["cmatra_amount"], "ada_amount": pool_out["ada_amount"]}
    _, second_hash, _, _ = world.build(
        _entitlement(2_000), legacy_qty=2_000, pool_utxo=chained_pool, user_inputs=user_change,
    )
    assert second_hash != first_hash
    assert world.cosigned_rows() == 2
