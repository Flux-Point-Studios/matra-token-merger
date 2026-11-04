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
from dataclasses import replace
from pathlib import Path
from typing import Callable

import httpx
import nacl.signing
import pytest
import requests
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
    PointerAddress,
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
from services.chain_check import ChainUnavailable
from services.cosign_policy import SLOT_OFFSET_S, CosignRejected, decode, load_config, split_tx
from services.pool_tip import PoolTipManager
from services.redemption_ledger import RedemptionLedger, create_ledger
from services.pager import Pager
from tests.cosign_cases import (
    COMMITTED_DENYLIST,
    PIN,
    TEST_WEBHOOK,
    FakeChain,
    _elements,
    key_address,
    ledger_refusing_writes,
    not_found,
    owed_pages,
    throwaway_key,
    utxos_answer,
)
from tests.test_surrender_redeemer_index import _SCRIPT_HEX, _FakeContext
from tools.config import AGENT, FLUX_PASS, T1_ADAM_PASS
from tools.process_surrender import load_rate_table, load_redemption_pin, surrendered_entitlement

ROOT = Path(__file__).resolve().parent.parent
RATES = load_rate_table(ROOT / "audit_pack/2026-04-19/rate_table_cmatra.json")
REDEEMABLE = load_redemption_pin(ROOT / "audit_pack/2026-09-27/redemption_pin.json").nft_units
T1_UNITS = sorted(u for u in REDEEMABLE if u.startswith(T1_ADAM_PASS.policy_id))
FLUX_UNIT = next(u for u in sorted(REDEEMABLE) if u.startswith(FLUX_PASS.policy_id))
# A pinned pass the pin still redeems; the synthetic claimant holds it.
PASS = next(u for u in T1_UNITS if PIN.remaining[u] > 0)
MAINNET_USER = Address.from_primitive(key_address(throwaway_key(7))).encode()
# Addresses the committed deny-list names: W1, W2 (the FPS treasury), and the
# attacker's collection and claim addresses.
DENIED_USERS = {
    "W1": "addr1q9jzvg6qqefx4c8eqsez4fnsxfdmcjtz4ku9f0fgju6ymc7ehhk8tdd2j0fqtn7975krepy8a7l8cepk5gyzyq7g6l6sktytnt",
    "W2": "addr1q9s6m9d8yedfcf53yhq5j5zsg0s58wpzamwexrxpfelgz2wgk0s9l9fqc93tyc8zu4z7hp9dlska2kew9trdg8nscjcq3sk5s3",
    "attacker": "addr1qxqf5syeture53vzrjgh2j3gwknylp62famesvv9hf5manv7jwqtqyr950fw8anzxavs6c8l6jxg033jquchc8dxns7qm366em",
    "claims": "addr1qx53hw2d3xhh3eqt6skfnqw0r7cjvm09yw3f6exvrlk4j88pjuvalvn7yf4ec5msyjgan7ur5hjs7d7x59jnfmjchngs5sp0tr",
}
POINTER_USER = Address(VerificationKeyHash(b"\x0c" * 28), PointerAddress(1, 2, 3), Network.MAINNET).encode()


# ---------------------------------------------------------------------------
# Layer 1a: request validation
# ---------------------------------------------------------------------------


def _item(asset_key: str, quantity: int, nft_units: list[str] | None = None):
    return api.AssetToSurrender(asset_key=asset_key, quantity_base=quantity, nft_units=nft_units)


@pytest.fixture
def validating(monkeypatch, tmp_path):
    """Everything wired up to the build; the build itself records its
    arguments and stops. Pages go to the outbox at tmp_path/pager.sqlite3."""
    seen: dict = {}
    monkeypatch.setattr(api.state, "pager", Pager(
        TEST_WEBHOOK, str(tmp_path / "pager.sqlite3"), "surrender-api, admin_1"))

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
    monkeypatch.setattr(api.state, "denylist", COMMITTED_DENYLIST)
    monkeypatch.setattr(api.state, "rate_table", RATES)
    monkeypatch.setattr(api.state, "redeemable_nfts", REDEEMABLE)
    monkeypatch.setattr(api.state, "script_cbor_hex", _SCRIPT_HEX)
    monkeypatch.setattr(api.state, "admin_sk", object())
    monkeypatch.setattr(api.state, "tip_mgr", PoolTipManager(
        lambda: [{"tx_hash": "a" * 64, "output_index": 0,
                  "cmatra_amount": 10**15, "ada_amount": 1_500_000}],
        script_address=script_addr.encode(), depth_cap=8))
    return seen


def _build(assets, user: str = MAINNET_USER) -> None:
    asyncio.run(api.build_surrender(
        api.BuildSurrenderRequest(user_address=user, assets=assets)))


def _defrag(user: str) -> None:
    asyncio.run(api.prepare_defrag(api.PrepareDefragRequest(user_address=user)))


def _evaluate(user: str) -> None:
    api.evaluate_surrender(api.BuildSurrenderRequest(user_address=user, assets=[_item("AGENT", 1000)]))


ROUTES = {"build": lambda user: _build([_item("AGENT", 1000)], user), "evaluate": _evaluate, "defrag": _defrag}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("who", DENIED_USERS)
def test_a_denied_address_is_refused_before_anything_is_built(validating, monkeypatch, route, who):
    built = []
    monkeypatch.setattr(api, "_build_defrag_tx_blocking", lambda *args: built.append(args))
    monkeypatch.setattr(api, "find_pool_utxos", lambda *args: built.append(args))
    with pytest.raises(HTTPException) as err:
        ROUTES[route](DENIED_USERS[who])
    assert err.value.status_code == 403
    assert err.value.detail["code"] == "denied_credential"
    assert validating == {} and built == []
    assert api.state.tip_mgr.chain_state() is None


@pytest.mark.parametrize("route", ROUTES)
def test_a_pointer_address_is_refused_before_anything_is_built(validating, monkeypatch, route):
    built = []
    monkeypatch.setattr(api, "_build_defrag_tx_blocking", lambda *args: built.append(args))
    monkeypatch.setattr(api, "find_pool_utxos", lambda *args: built.append(args))
    with pytest.raises(HTTPException) as err:
        ROUTES[route](POINTER_USER)
    assert err.value.status_code == 403
    assert err.value.detail["code"] == "pointer_address"
    assert validating == {} and built == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("unloaded", ["denylist", "pager"])
def test_without_a_denylist_or_a_pager_nothing_is_built(validating, monkeypatch, route, unloaded):
    monkeypatch.setattr(api.state, unloaded, None)
    with pytest.raises(HTTPException) as err:
        ROUTES[route](MAINNET_USER)
    assert err.value.status_code == 503
    assert validating == {}


def test_a_denied_address_pages_once_an_hour(validating, tmp_path):
    for _ in range(3):
        with pytest.raises(HTTPException):
            _build([_item("AGENT", 1000)], DENIED_USERS["W1"])
    with pytest.raises(HTTPException):
        _build([_item("AGENT", 1000)], DENIED_USERS["claims"])
    pages = owed_pages(tmp_path / "pager.sqlite3")
    assert [page.split(":")[0] for page in pages] == ["refused denied_credential"] * 2
    assert DENIED_USERS["W1"] in pages[0] and DENIED_USERS["claims"] in pages[1]


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


def test_evaluate_answers_an_unreachable_chain_view_with_its_code(validating, monkeypatch):
    def unanswerable(**_):
        raise ChainUnavailable("no route to host")

    monkeypatch.setattr(api, "_build_surrender_tx", unanswerable)
    monkeypatch.setattr(api, "find_pool_utxos", lambda *_: [
        {"tx_hash": "a" * 64, "output_index": 0, "cmatra_amount": 10**15, "ada_amount": 1_500_000}])
    with pytest.raises(HTTPException) as err:
        api.evaluate_surrender(api.BuildSurrenderRequest(
            user_address=MAINNET_USER, assets=[_item("AGENT", 1000)]))
    assert err.value.status_code == 503
    assert err.value.detail["code"] == "chain_unavailable"


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
    of a producing body the fake Blockfrost can serve, each placed at slot 0
    unless ``slots`` says otherwise. The claimant got its tokens from a
    seller's output."""

    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.admin_sk = PaymentSigningKey.generate()
        self.cosigner_sk = PaymentSigningKey.generate()
        script = PlutusV3Script(bytes.fromhex(_SCRIPT_HEX))
        self.pool_addr = Address(plutus_script_hash(script), network=Network.TESTNET)
        self.quarantine_addr = Address(ScriptHash(b"\x55" * 28), network=Network.TESTNET)
        self.user_sk = PaymentSigningKey.generate()
        self.user_addr = Address(self.user_sk.to_verification_key().hash(), network=Network.TESTNET)
        self.chain: dict[str, bytes] = {}
        self.slots: dict[str, int] = {}
        self.supply: dict[str, int] = dict(PIN.supply)
        self.spent: set[tuple[str, int]] = set()
        self.chain_failure: Exception | None = None
        # Each unit's mint history, and the slot of each transaction in it.
        self.history = FakeChain()
        self.submitted: list[bytes] = []
        # Rewrites the co-signer's JSON answer on its way back, when set.
        self.edit_answer: Callable[[dict], dict] | None = None

        agent = MultiAsset()
        agent[ScriptHash(bytes.fromhex(AGENT_POLICY))] = Asset({AssetName(bytes.fromhex(AGENT_NAME)): 5_000})
        agent[ScriptHash(bytes.fromhex(PASS[:56]))] = Asset({AssetName(bytes.fromhex(PASS[56:])): 1})
        seller = self._produce([TransactionOutput(
            Address(VerificationKeyHash(b"\x05" * 28), network=Network.TESTNET), Value(5_000_000, agent))])
        user_body = self._produce([
            TransactionOutput(self.user_addr, Value(100_000_000)),
            TransactionOutput(self.user_addr, Value(5_000_000, agent)),
        ], inputs=[TransactionInput(TransactionId(bytes.fromhex(seller)), 0)])
        self.user_body = user_body
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
            "BLOCKFROST_PROJECT_ID": "preprod-test-project",
            "PAGER_DISCORD_WEBHOOK_URL": TEST_WEBHOOK,
            "PAGER_OUTBOX_PATH": str(tmp_path / "cosigner-pager.sqlite3"),
        }
        self.cosigner_sk.save(env["COSIGNER_SKEY_PATH"])
        self.ledger = env["COSIGNER_LEDGER_PATH"]
        create_ledger(self.ledger)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(cosigner.time, "time", lambda: NOW + SLOT_OFFSET_S[NETWORK])

        self.client = TestClient(cosigner.app)
        self.client.__enter__()
        self.signed_by_admin: list[bytes] = []
        # The primary ledger's approvals, and its pages, at each admin_1 signature.
        self.primary_rows_when_signed: list[int] = []
        self.outbox = tmp_path / "primary-pager.sqlite3"
        self.pages_when_signed: list[list[str]] = []
        self.cosign_calls = 0
        self._wire_surrender_api(env)
        create_ledger(str(tmp_path / "primary.sqlite3"))
        self.primary_ledger = RedemptionLedger(
            str(tmp_path / "primary.sqlite3"), 10**15, api.state.cosign_config.funded_limits)
        monkeypatch.setattr(api.state, "ledger", self.primary_ledger)

    def _produce(self, outputs, inputs=None) -> str:
        body = TransactionBody(
            inputs=inputs or [TransactionInput(TransactionId(bytes(32)), len(self.chain))],
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
                world.primary_rows_when_signed.append(world.primary_rows())
                world.pages_when_signed.append(owed_pages(world.outbox))
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

            def submit_tx(self, tx):
                world.submitted.append(tx)
                return hashlib.blake2b(split_tx(tx)[0], digest_size=32).hexdigest()

            def get_tx_utxos(self, tx_hash):
                if world.chain_failure is not None:
                    raise world.chain_failure
                if tx_hash not in world.chain:
                    raise not_found()
                return utxos_answer(world.chain, tx_hash, dict.fromkeys(world.spent, "ee" * 32))

            def get_asset_info(self, unit):
                if world.chain_failure is not None:
                    raise world.chain_failure
                if unit not in world.supply:
                    raise not_found()
                return {"asset": unit, "quantity": str(world.supply[unit])}

            def get_asset_history(self, unit):
                return world.history.get_asset_history(unit)

            def get_tx(self, tx_hash):
                if tx_hash in world.chain:
                    return {"hash": tx_hash, "slot": world.slots.get(tx_hash, 0)}
                return world.history.get_tx(tx_hash)

        def post(url, json, headers, timeout):
            assert url == "http://cosigner.test/cosign"
            world.cosign_calls += 1
            response = world.client.post("/cosign", json=json, headers=headers)
            if world.edit_answer is None:
                return response
            return httpx.Response(200, json=world.edit_answer(response.json()), request=response.request)

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
        m.setattr(api.state, "pager", Pager(TEST_WEBHOOK, str(self.outbox), "surrender-api, admin_1"))
        # Both signers ask the same (fake) chain; each through its own client.
        m.setattr(cosigner.state, "chain", api.state.bf)
        m.setattr(api.state, "built_bodies", {})
        m.setattr(api.state, "cosign_config", load_config(env, [admin_pkh.payload, cosigner_pkh.payload]))
        m.setattr(api, "ALLOWED_USER_ADDRESSES", frozenset())
        m.setattr(api.state, "denylist", COMMITTED_DENYLIST)
        m.setattr(api.state, "rate_table", RATES)
        m.setattr(api.state, "redeemable_nfts", REDEEMABLE)
        m.setattr(api.state, "tip_mgr", PoolTipManager(
            lambda: [world.pool_utxo], script_address=self.pool_addr.encode(), depth_cap=8))
        for stash in ("pending_tx_hashes", "pending_tx_cbor", "build_ctx", "cosign_inputs"):
            m.setattr(api.state, stash, {})
        api.state.reserved_collateral.clear()
        api.state.user_pending.clear()

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    def cosigned_rows(self) -> int:
        return _rows(self.ledger)

    def primary_rows(self) -> int:
        return _rows(self.primary_ledger.path)

    def build(self, total: int, legacy_qty: int = 1_000, pool_utxo=None, user_inputs=None, passes=()):
        legacy = [{"policy_hex": AGENT_POLICY, "asset_hex": AGENT_NAME, "quantity": legacy_qty}]
        legacy += [{"policy_hex": unit[:56], "asset_hex": unit[56:], "quantity": 1} for unit in passes]
        return api._build_surrender_tx(
            self.user_addr.encode(), total, legacy, pool_utxo or self.pool_utxo, user_inputs,
        )

    def build_route(self, agent: int, passes: tuple[str, ...] = ()) -> api.BuildSurrenderResponse:
        """POST /build-surrender for ``agent`` AGENT and the T1 ``passes``
        from the claimant."""
        assets = [api.AssetToSurrender(asset_key="AGENT", quantity_base=agent)] if agent else []
        if passes:
            assets.append(api.AssetToSurrender(
                asset_key="T1_ADAM_PASS", quantity_base=len(passes), nft_units=list(passes)))
        request = api.BuildSurrenderRequest(user_address=self.user_addr.encode(), assets=assets)
        # A pool tip left locked by an earlier call would hang here, not fail.
        return asyncio.run(asyncio.wait_for(api.build_surrender(request), timeout=30))

    def wallet_witnesses(self, tx_hash: str, key: PaymentSigningKey | None = None) -> str:
        """What CIP-30 signTx(partialSign=true) returns for ``tx_hash``."""
        key = key or self.user_sk
        signature = key.sign(bytes.fromhex(tx_hash))
        return api.cbor2.dumps({0: [[key.to_verification_key().payload, signature]]}).hex()

    def submit_route(self, tx_hash: str, witnesses_hex: str) -> api.SubmitResponse:
        return asyncio.run(api.submit_surrender(
            api.SubmitRequest(tx_cbor_hex=witnesses_hex, tx_hash=tx_hash)))


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    w.close()


def _entitlement(qty: int, passes: tuple[str, ...] = ()) -> int:
    return surrendered_entitlement(RATES, {AGENT.unit: qty, **dict.fromkeys(passes, 1)}, REDEEMABLE)


def _rows(ledger_path: str) -> int:
    with sqlite3.connect(ledger_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]


def _vkey_witnesses(tx_hex: str) -> list:
    ws = decode(split_tx(bytes.fromhex(tx_hex))[1])
    vkeys = ws.get(0, [])
    return list(vkeys.value) if hasattr(vkeys, "tag") else vkeys


def _vk(key: PaymentSigningKey) -> bytes:
    return bytes(key.to_verification_key().payload)


def _assert_signed_by(tx: bytes, tx_hash: str, *keys: PaymentSigningKey) -> None:
    """``tx`` carries exactly one witness for each of ``keys``, each a valid
    signature over ``tx_hash``."""
    pairs = [(bytes(vk), bytes(sig)) for vk, sig in _vkey_witnesses(tx.hex())]
    assert sorted(vk for vk, _ in pairs) == sorted(_vk(key) for key in keys)
    for vkey, signature in pairs:
        nacl.signing.VerifyKey(vkey).verify(bytes.fromhex(tx_hash), signature)


def test_chained_builds_carry_no_signature_past_the_primary_cap(world, monkeypatch, tmp_path):
    """/build-surrender needs nothing but an address, so what it returns
    carries neither admin's signature: builds chained past the primary's own
    daily cap obtain nothing that either ledger has not recorded."""
    e = _entitlement(1_000)
    create_ledger(str(tmp_path / "small-cap.sqlite3"))
    small_cap = RedemptionLedger(
        str(tmp_path / "small-cap.sqlite3"), int(1.5 * e), api.state.cosign_config.funded_limits)
    monkeypatch.setattr(api.state, "ledger", small_cap)
    pool, user_inputs = world.pool_utxo, None
    for _ in range(3):
        tx_hex, tx_hash, pool_out, user_inputs = world.build(e, pool_utxo=pool, user_inputs=user_inputs)
        assert _vkey_witnesses(tx_hex) == []
        # Whoever holds the other key puts the build on chain; the next one chains on it.
        world.chain[tx_hash] = split_tx(bytes.fromhex(tx_hex))[0]
        pool = {"tx_hash": tx_hash, "output_index": 1,
                "cmatra_amount": pool["cmatra_amount"] - e, "ada_amount": pool_out["ada_amount"]}
    assert world.signed_by_admin == []
    assert world.cosign_calls == 0
    assert world.cosigned_rows() == 0
    assert _rows(small_cap.path) == 0


def test_admin_1_signs_only_after_its_own_ledger_records_the_surrender(world):
    built = world.build_route(1_000)
    assert world.signed_by_admin == []
    world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert world.signed_by_admin == [bytes.fromhex(built.tx_hash)]
    assert world.primary_rows_when_signed == [1]


def test_without_a_cosigner_admin_1_signs_once_the_claimant_has(world, monkeypatch):
    monkeypatch.setattr(api, "COSIGNER_URL", "")
    built = world.build_route(1_000)
    assert _vkey_witnesses(built.tx_cbor_hex) == []
    world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    (tx,) = world.submitted
    _assert_signed_by(tx, built.tx_hash, world.user_sk, world.admin_sk)
    assert world.cosign_calls == 0


def test_submit_cosigns_after_the_claimant_signs(world):
    built = world.build_route(1_000)
    assert world.cosign_calls == 0
    resp = world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert resp.tx_hash == built.tx_hash
    assert world.cosign_calls == 1
    assert world.cosigned_rows() == 1
    (tx,) = world.submitted
    _assert_signed_by(tx, built.tx_hash, world.user_sk, world.admin_sk, world.cosigner_sk)


@pytest.mark.parametrize("signer", ["cosigner_sk", "admin_sk"])
@pytest.mark.parametrize("junk_first", [False, True], ids=["after", "before"])
def test_a_wallet_witness_under_an_admin_key_never_reaches_the_node(world, signer, junk_first):
    built = world.build_route(1_000)
    claimant = [_vk(world.user_sk), world.user_sk.sign(bytes.fromhex(built.tx_hash))]
    junk = [_vk(getattr(world, signer)), bytes(64)]
    wallet = [junk, claimant] if junk_first else [claimant, junk]
    world.submit_route(built.tx_hash, api.cbor2.dumps({0: wallet}).hex())
    (tx,) = world.submitted
    _assert_signed_by(tx, built.tx_hash, world.user_sk, world.admin_sk, world.cosigner_sk)
    assert world.cosigned_rows() == 1


@pytest.mark.parametrize("entry", [
    [1 << 20, bytes(64)],
    [bytes(31), bytes(64)],
    [bytes(33), bytes(64)],
    [bytes(32), bytes(63)],
    [bytes(32), 7],
    [bytes(32), bytes(64), b""],
    [bytes(32)],
    bytes(32),
], ids=["integer_key", "short_key", "long_key", "short_signature", "integer_signature",
        "three_items", "one_item", "bare_bytes"])
def test_a_malformed_wallet_witness_is_refused_before_it_is_read(world, entry):
    built = world.build_route(1_000)
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, api.cbor2.dumps({0: [entry]}).hex())
    assert err.value.status_code == 400
    assert err.value.detail == "Malformed witness set CBOR"
    assert world.cosign_calls == 0
    assert world.signed_by_admin == []
    assert world.submitted == []


def test_submit_signed_by_another_key_is_refused_before_the_cosigner(world):
    built = world.build_route(1_000)
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash, PaymentSigningKey.generate()))
    assert err.value.status_code == 400
    assert world.cosign_calls == 0
    assert world.cosigned_rows() == 0
    assert world.submitted == []
    world.build_route(1_000)  # the refused surrender released the pool tip


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
    real = api._price_request

    def inflated(assets):
        total, summary, legacy = real(assets)
        return total * 100, summary, legacy

    monkeypatch.setattr(api, "_price_request", inflated)
    monkeypatch.setattr(api, "_check_locally", lambda *a, **k: None)
    built = world.build_route(1_000)
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert err.value.status_code == 503
    assert world.cosign_calls == 1
    assert world.cosigned_rows() == 0
    assert world.submitted == []


def test_chained_surrender_is_cosigned_while_its_parent_is_in_the_mempool(world):
    first = world.build_route(1_000)
    world.submit_route(first.tx_hash, world.wallet_witnesses(first.tx_hash))
    second = world.build_route(2_000)
    assert first.tx_hash not in world.chain  # still "in the mempool" for Blockfrost
    world.submit_route(second.tx_hash, world.wallet_witnesses(second.tx_hash))
    assert second.tx_hash != first.tx_hash
    assert world.cosigned_rows() == 2
    assert len(world.submitted) == 2


def test_primary_refuses_a_unit_past_its_limit_before_signing(world, monkeypatch):
    cfg = api.state.cosign_config
    limits = {**cfg.redemption_limits, AGENT.unit: 999}
    monkeypatch.setattr(api.state, "cosign_config", replace(cfg, redemption_limits=limits))
    with pytest.raises(CosignRejected) as err:
        world.build(_entitlement(1_000))
    assert err.value.code == "redemption_limit"
    assert world.signed_by_admin == []
    assert world.cosign_calls == 0


def test_submit_after_the_build_was_forgotten_asks_for_a_rebuild(world):
    built = world.build_route(1_000)
    api.state.cosign_inputs.pop(built.tx_hash)
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert err.value.status_code == 409
    assert world.cosign_calls == 0
    assert world.submitted == []
    world.build_route(1_000)  # the pool tip was released


def test_unwritable_primary_ledger_refuses_with_a_code(world):
    built = world.build_route(1_000)
    with ledger_refusing_writes(world.primary_ledger.path), pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert err.value.status_code == 503
    assert err.value.detail["code"] == "ledger_unavailable"
    assert world.signed_by_admin == []
    assert world.submitted == []
    world.build_route(1_000)  # the pool tip was released


# ---------------------------------------------------------------------------
# The co-signer's answer, checked before admin 1 signs
# ---------------------------------------------------------------------------


def _flipped(signature: bytes) -> bytes:
    return signature[:-1] + bytes([signature[-1] ^ 1])


def _another_keys_signature(world, answer: dict, body_hash: bytes) -> dict:
    stranger = PaymentSigningKey.generate()
    return {**answer, "vkey_hex": _vk(stranger).hex(), "signature_hex": stranger.sign(body_hash).hex()}


# Each rewrites the co-signer's answer for ``world``; none of them is the
# co-signer's key with its signature of the transaction body.
WRONG_ANSWERS = {
    "another_key": _another_keys_signature,
    "junk_signature": lambda world, answer, body_hash: {
        **answer, "signature_hex": _flipped(bytes.fromhex(answer["signature_hex"])).hex(),
    },
    "admin_1_key_with_junk": lambda world, answer, body_hash: {
        **answer, "vkey_hex": _vk(world.admin_sk).hex(), "signature_hex": bytes(64).hex(),
    },
    "signature_of_another_body": lambda world, answer, body_hash: {
        **answer, "signature_hex": world.cosigner_sk.sign(bytes(32)).hex(),
    },
    "signature_of_the_body_the_answer_names": lambda world, answer, body_hash: {
        **answer, "tx_hash": bytes(32).hex(), "signature_hex": world.cosigner_sk.sign(bytes(32)).hex(),
    },
    "short_key": lambda world, answer, body_hash: {**answer, "vkey_hex": answer["vkey_hex"][:-2]},
    "short_signature": lambda world, answer, body_hash: {
        **answer, "signature_hex": answer["signature_hex"][:-2],
    },
    "long_signature": lambda world, answer, body_hash: {
        **answer, "signature_hex": answer["signature_hex"] + "00",
    },
}


@pytest.mark.parametrize("wrong", WRONG_ANSWERS)
def test_a_cosigner_answer_that_is_not_its_signature_of_the_body_is_refused(world, wrong):
    """admin 1 signs only once the answer carries the co-signer's own key and
    that key's signature of the body. Anything else could not land, or would
    stand in for admin 1's own witness."""
    built = world.build_route(1_000)
    body_hash = bytes.fromhex(built.tx_hash)
    world.edit_answer = lambda answer: WRONG_ANSWERS[wrong](world, answer, body_hash)
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert err.value.status_code == 503
    assert err.value.detail["code"] == "cosigner_witness"
    assert world.signed_by_admin == []
    assert world.primary_rows() == 0
    assert world.submitted == []
    world.edit_answer = None
    world.build_route(1_000)  # the pool tip was released


def test_no_pair_from_the_cosigner_step_displaces_admin_1s_own_witness(world, monkeypatch):
    """Behind the check above: whatever [key, signature] pair the co-signer
    step hands back, admin 1's own witness is the one that reaches the node."""
    built = world.build_route(1_000)
    monkeypatch.setattr(api, "_cosign", lambda tx_hash_hex, tx: [_vk(world.admin_sk), bytes(64)])
    world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    (tx,) = world.submitted
    _assert_signed_by(tx, built.tx_hash, world.user_sk, world.admin_sk)


# ---------------------------------------------------------------------------
# Supply minted after the pin
# ---------------------------------------------------------------------------


def test_a_unit_minted_past_its_pinned_supply_is_refused_before_either_admin_signs(world):
    world.supply[AGENT.unit] += 1
    with pytest.raises(CosignRejected) as err:
        world.build(_entitlement(1_000))
    assert err.value.code == "supply_grown"
    assert world.signed_by_admin == []
    assert world.cosign_calls == 0


def test_a_surrender_is_paged_before_admin_1_signs(world):
    built = world.build_route(1_000)
    world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    [pages] = world.pages_when_signed
    assert len(pages) == 1 and pages[0].startswith(f"signing surrender {built.tx_hash}: ")


def test_an_unwritable_outbox_stops_the_surrender_before_admin_1_signs(world):
    built = world.build_route(1_000)
    with sqlite3.connect(world.outbox) as conn:
        conn.execute("CREATE TRIGGER refuse BEFORE INSERT ON pages BEGIN SELECT RAISE(ABORT, 'full'); END")
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert err.value.status_code == 503
    assert err.value.detail["code"] == "pager_unavailable"
    assert world.signed_by_admin == [] and world.submitted == []


def test_units_that_reached_the_claimant_an_hour_ago_wait_out_the_cooling_off(world):
    world.slots[world.user_body] = NOW - 3600
    with pytest.raises(HTTPException) as err:
        world.build_route(1_000)
    assert err.value.status_code == 422
    assert err.value.detail["code"] == "cooling_off"
    assert world.user_body in err.value.detail["message"]
    assert world.signed_by_admin == [] and world.cosign_calls == 0
    world.monkeypatch.setattr(api.time, "time", lambda: NOW + 60 + SLOT_OFFSET_S[NETWORK])
    with pytest.raises(HTTPException):
        world.build_route(1_000)  # refused again a minute later: paged once
    assert [page.split(":")[0] for page in owed_pages(world.outbox)] == ["refused cooling_off"]
    world.slots[world.user_body] = NOW + 60 - 72 * 3600
    world.build_route(1_000)  # held as long as the window, and the tip was released


def test_a_surrender_spends_the_units_held_longest_first(world):
    """Beside the AGENT held long before, AGENT from another key reached the
    claimant an hour ago in an output with more ADA, which the builder's own
    selection, largest first, would spend instead: the surrender would wait
    out the cooling-off for nothing."""
    agent = MultiAsset()
    agent[ScriptHash(bytes.fromhex(AGENT_POLICY))] = Asset({AssetName(bytes.fromhex(AGENT_NAME)): 5_000})
    seller = world._produce([TransactionOutput(
        Address(VerificationKeyHash(b"\x06" * 28), network=Network.TESTNET), Value(50_000_000, agent))])
    received = world._produce([TransactionOutput(world.user_addr, Value(50_000_000, agent))],
                              inputs=[TransactionInput(TransactionId(bytes.fromhex(seller)), 0)])
    world.slots[received] = NOW - 3600
    world.user_utxos.append(UTxO(TransactionInput(TransactionId(bytes.fromhex(received)), 0),
                                 TransactionBody.from_cbor(world.chain[received]).outputs[0]))
    built = world.build_route(1_000)
    world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    (tx,) = world.submitted
    spent = {(ref[0].hex(), ref[1]) for ref in _elements(decode(split_tx(tx)[0])[0])}
    assert (world.user_body, 1) in spent
    assert (received, 0) not in spent


def test_the_build_leaves_out_only_the_holders_it_need_not_spend(monkeypatch):
    def utxo(n: int, *units: tuple[str, int]) -> UTxO:
        multi = MultiAsset()
        for unit, quantity in units:
            multi.setdefault(ScriptHash(bytes.fromhex(unit[:56])), Asset())[AssetName(bytes.fromhex(unit[56:]))] = (
                quantity)
        user = Address(VerificationKeyHash(b"\x02" * 28), network=Network.TESTNET)
        return UTxO(TransactionInput(TransactionId(bytes([n]) * 32), 0), TransactionOutput(user, Value(2_000_000, multi)))

    old, new, unplaced = utxo(1, (AGENT.unit, 600)), utxo(2, (AGENT.unit, 600), (PASS, 1)), utxo(3, (AGENT.unit, 600))
    slots = {str(old.input.transaction_id): 10, str(new.input.transaction_id): 20}
    asked: list[str] = []

    class Chain:
        def get_tx(self, tx_hash):
            asked.append(tx_hash)
            if tx_hash not in slots:
                raise not_found()
            return {"hash": tx_hash, "slot": slots[tx_hash]}

    monkeypatch.setattr(api.state, "bf", Chain())
    agent = {"policy_hex": AGENT_POLICY, "asset_hex": AGENT_NAME, "quantity": 600}
    a_pass = {"policy_hex": PASS[:56], "asset_hex": PASS[56:], "quantity": 1}
    utxos = [unplaced, new, old]
    assert api._later_holders(utxos, [agent]) == [new, unplaced]
    assert api._later_holders(utxos, [{**agent, "quantity": 601}]) == [unplaced]
    assert api._later_holders(utxos, [agent, a_pass]) == [unplaced]
    asked.clear()
    assert api._later_holders([old, new], [a_pass]) == []
    assert asked == []


def _merge_a_pass_from_another_key_into_the_agent(world) -> str:
    """A T1 pass from another key reaches the claimant 15 minutes ago and is
    merged 10 minutes ago into the output holding its long-held AGENT. Returns
    the pass."""
    unit = next(u for u in T1_UNITS if PIN.remaining[u] > 0 and u != PASS)
    one = MultiAsset()
    one[ScriptHash(bytes.fromhex(unit[:56]))] = Asset({AssetName(bytes.fromhex(unit[56:])): 1})
    seller = world._produce([TransactionOutput(
        Address(VerificationKeyHash(b"\x06" * 28), network=Network.TESTNET), Value(2_000_000, one))])
    received = world._produce([TransactionOutput(world.user_addr, Value(2_000_000, one))],
                              inputs=[TransactionInput(TransactionId(bytes.fromhex(seller)), 0)])
    held = world.user_utxos[1]
    merged = world._produce([TransactionOutput(world.user_addr, held.output.amount + Value(2_000_000, one))],
                            inputs=[held.input, TransactionInput(TransactionId(bytes.fromhex(received)), 0)])
    world.slots[received], world.slots[merged] = NOW - 900, NOW - 600
    world.user_utxos[1] = UTxO(TransactionInput(TransactionId(bytes.fromhex(merged)), 0),
                               TransactionBody.from_cbor(world.chain[merged]).outputs[0])
    return unit


def test_a_pass_that_rode_through_a_recorded_surrender_waits_out_the_cooling_off(world):
    """An AGENT surrender spends the merged output and returns the pass as
    change; both signers record it. Surrendered from that change while the
    AGENT surrender is still in the mempool, the pass is refused."""
    passed = _merge_a_pass_from_another_key_into_the_agent(world)
    first = world.build_route(1_000)
    world.submit_route(first.tx_hash, world.wallet_witnesses(first.tx_hash))
    with pytest.raises(HTTPException) as err:
        world.build_route(0, passes=(passed,))
    assert err.value.status_code == 422
    assert err.value.detail["code"] == "cooling_off"
    assert err.value.detail["message"].startswith(f"{passed} reached the claimant in ")
    assert len(world.submitted) == 1


def test_the_cosigner_refuses_a_pass_that_rode_through_if_the_primary_skips_its_check(world, monkeypatch, tmp_path):
    passed = _merge_a_pass_from_another_key_into_the_agent(world)
    first = world.build_route(1_000)
    world.submit_route(first.tx_hash, world.wallet_witnesses(first.tx_hash))
    monkeypatch.setattr(api, "_check_locally", lambda *a, **k: None)
    second = world.build_route(0, passes=(passed,))
    with pytest.raises(HTTPException) as err:
        world.submit_route(second.tx_hash, world.wallet_witnesses(second.tx_hash))
    assert err.value.status_code == 503
    assert world.cosigned_rows() == 1
    assert len(world.submitted) == 1
    assert [page.split(":")[0] for page in owed_pages(tmp_path / "cosigner-pager.sqlite3")][-1] == (
        "refused cooling_off")


def test_the_build_route_answers_a_grown_supply_with_its_code(world):
    world.supply[AGENT.unit] += 1
    with pytest.raises(HTTPException) as err:
        world.build_route(1_000)
    assert err.value.status_code == 422
    assert err.value.detail["code"] == "supply_grown"
    assert world.signed_by_admin == []
    world.supply[AGENT.unit] -= 1
    world.build_route(1_000)  # the pool tip was released


def test_a_mint_between_build_and_submit_is_refused_before_the_cosigner(world):
    built = world.build_route(1_000)
    world.supply[AGENT.unit] += 1
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert err.value.status_code == 400
    assert err.value.detail["code"] == "supply_grown"
    assert world.cosign_calls == 0
    assert world.submitted == []


def test_cosigner_checks_the_supply_itself_if_the_primary_skips_its_check(world, monkeypatch):
    built = world.build_route(1_000)
    world.supply[AGENT.unit] += 1
    monkeypatch.setattr(api, "_check_locally", lambda *a, **k: None)
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert err.value.status_code == 503
    assert world.cosign_calls == 1
    assert world.cosigned_rows() == 0
    assert world.submitted == []


# ---------------------------------------------------------------------------
# A pass minted or burned after the pin
# ---------------------------------------------------------------------------


def test_a_pass_whose_history_ends_at_the_pin_is_surrendered(world):
    built = world.build_route(1_000, passes=(PASS,))
    world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    (tx,) = world.submitted
    _assert_signed_by(tx, built.tx_hash, world.user_sk, world.admin_sk, world.cosigner_sk)
    assert world.cosigned_rows() == world.primary_rows() == 1


@pytest.mark.parametrize("action", ["minted", "burned"])
def test_a_pass_changed_after_the_pin_is_refused_before_either_admin_signs(world, action):
    world.history.change_after_pin(PASS, action)
    with pytest.raises(CosignRejected) as err:
        world.build(_entitlement(1_000, (PASS,)), passes=(PASS,))
    assert err.value.code == "minted_after_pin"
    assert PASS in err.value.detail
    assert world.signed_by_admin == []
    assert world.cosign_calls == 0


def test_the_build_route_answers_a_pass_changed_after_the_pin_with_its_code(world):
    world.history.change_after_pin(PASS, "minted")
    with pytest.raises(HTTPException) as err:
        world.build_route(1_000, passes=(PASS,))
    assert err.value.status_code == 422
    assert err.value.detail["code"] == "minted_after_pin"
    assert world.signed_by_admin == []
    world.build_route(1_000)  # the pool tip was released


def test_a_pass_minted_between_build_and_submit_is_refused_before_the_cosigner(world):
    built = world.build_route(1_000, passes=(PASS,))
    world.history.change_after_pin(PASS, "minted")
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert err.value.status_code == 400
    assert err.value.detail["code"] == "minted_after_pin"
    assert world.cosign_calls == 0
    assert world.primary_rows() == 0
    assert world.submitted == []


def test_cosigner_checks_the_history_itself_if_the_primary_skips_its_check(world, monkeypatch, caplog):
    built = world.build_route(1_000, passes=(PASS,))
    world.history.change_after_pin(PASS, "burned")
    monkeypatch.setattr(api, "_check_locally", lambda *a, **k: None)
    with pytest.raises(HTTPException) as err:
        world.submit_route(built.tx_hash, world.wallet_witnesses(built.tx_hash))
    assert err.value.status_code == 503
    assert world.cosign_calls == 1
    assert "Refused to co-sign: minted_after_pin" in caplog.text
    assert world.cosigned_rows() == 0
    assert world.submitted == []


def test_an_unreachable_chain_view_stops_the_build_before_signing(world):
    world.chain_failure = requests.ConnectionError("no route to host")
    with pytest.raises(HTTPException) as err:
        world.build_route(1_000)
    assert err.value.status_code == 503
    assert err.value.detail["code"] == "chain_unavailable"
    assert world.signed_by_admin == []
    world.chain_failure = None
    world.build_route(1_000)  # the pool tip was released


# ---------------------------------------------------------------------------
# Inputs the chain does not hold
# ---------------------------------------------------------------------------


def test_a_spent_pool_output_is_refused_before_either_admin_signs(world):
    world.spent.add((world.pool_utxo["tx_hash"], 0))
    with pytest.raises(CosignRejected) as err:
        world.build(_entitlement(1_000))
    assert err.value.code == "input_spent"
    assert world.signed_by_admin == []
    assert world.cosign_calls == 0


def test_an_output_of_a_build_that_was_never_cosigned_is_refused(world):
    """The primary's own build, which no signer recorded and the chain never
    saw, cannot parent another surrender."""
    _, first_hash, pool_out, _ = world.build(_entitlement(1_000))
    orphan = {"tx_hash": first_hash, "output_index": 1,
              "cmatra_amount": pool_out["cmatra_amount"], "ada_amount": pool_out["ada_amount"]}
    with pytest.raises(CosignRejected) as err:
        world.build(_entitlement(1_000), pool_utxo=orphan)
    assert err.value.code == "input_unknown"
    assert world.signed_by_admin == []
    assert world.cosign_calls == 0
