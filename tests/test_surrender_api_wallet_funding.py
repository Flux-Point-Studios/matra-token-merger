"""/build-surrender tells a claimant whose wallet cannot fund the surrender
what to add, instead of a bare preflight failure.

The fixture is the shape of a mainnet wallet that could not redeem: one
UTxO of 4.317291 ADA holding the AGENT it surrendered and one token it keeps,
no pure-ADA UTxO, built with mainnet's protocol parameters. The builder then
offered that UTxO as collateral and failed on its collateral return.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest
from fastapi import HTTPException
from pycardano import (
    Address,
    Asset,
    AssetName,
    MultiAsset,
    Network,
    TransactionBody,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.exception import InvalidTransactionException
from pycardano.hash import ScriptHash, TransactionId, VerificationKeyHash
from pycardano.utils import min_lovelace_post_alonzo

import services.surrender_api as api
from services.cosign_policy import split_tx
from tests.cosign_cases import PIN
from tests.test_surrender_api_cosign import AGENT_NAME, AGENT_POLICY, T1_UNITS, World, _entitlement
from tests.test_surrender_redeemer_index import _FakeContext

# The token the fixture wallet keeps: its policy and name are stand-ins of the
# same sizes (a 13-byte CIP-68 fungible name) as the one on chain.
KEPT_POLICY = "0b" * 28
KEPT_NAME = "0014df10" + b"KeepToken".hex()
KEPT_QUANTITY = 111_300_000_000
AGENT_HELD = 166_061
LACE_WALLET_LOVELACE = 4_317_291

# The builder's 1.5 ADA floors: the claimant's cMATRA output and the
# quarantine output; the pool continuation is paid by the pool's own 1.5 ADA.
FUNDED_OUTPUTS = 3_000_000
# The builder's fee estimate for the fixture surrender before it picks the
# claimant's inputs: the script witness, its seed execution units, the three
# outputs and the two admin signers.
FIXTURE_FEE = 365_857


class _MainnetParams(_FakeContext):
    """The offline chain with mainnet's per-transaction memory ceiling, which
    sets the collateral the builder asks for (150 % of the largest fee), and
    its execution prices as the Blockfrost backend reads them."""

    @property
    def protocol_param(self):
        return replace(
            super().protocol_param,
            max_tx_ex_mem=16_500_000,
            price_mem=Fraction(0.0577),
            price_step=Fraction(0.0000721),
        )


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    w.close()


def _tokens(*units: tuple[str, str, int]) -> MultiAsset:
    multi = MultiAsset()
    for policy, name, quantity in units:
        multi.setdefault(ScriptHash(bytes.fromhex(policy)), Asset())[AssetName(bytes.fromhex(name))] = quantity
    return multi


def _wallet(world: World, monkeypatch, *values: Value) -> list[UTxO]:
    """Give the claimant a base address holding exactly ``values``, served by
    a chain context with mainnet's parameters."""
    world.user_addr = Address(
        world.user_sk.to_verification_key().hash(),
        VerificationKeyHash(b"\x5e" * 28),
        network=Network.TESTNET,
    )
    body = world._produce([TransactionOutput(world.user_addr, value) for value in values])
    utxos = [
        UTxO(TransactionInput(TransactionId(bytes.fromhex(body)), i), out)
        for i, out in enumerate(TransactionBody.from_cbor(world.chain[body]).outputs)
    ]
    monkeypatch.setattr(api, "BlockFrostChainContext", lambda **kw: _MainnetParams(utxos))
    return utxos


def _lace_utxo(lovelace: int = LACE_WALLET_LOVELACE) -> Value:
    """The mainnet wallet's one UTxO: the AGENT it surrenders and the token it keeps."""
    return Value(lovelace, _tokens((KEPT_POLICY, KEPT_NAME, KEPT_QUANTITY), (AGENT_POLICY, AGENT_NAME, AGENT_HELD)))


def _agent_only() -> MultiAsset:
    return _tokens((AGENT_POLICY, AGENT_NAME, AGENT_HELD))


def _lace_wallet(world, monkeypatch, *extra: Value) -> list[UTxO]:
    return _wallet(world, monkeypatch, _lace_utxo(), *extra)


def _refusal(world, agent: int, passes: tuple[str, ...] = ()) -> HTTPException:
    with pytest.raises(HTTPException) as err:
        world.build_route(agent, passes)
    return err.value


def _checked_after_the_build_only(monkeypatch):
    """Never refuse before the build, so only the builder's own errors are
    left to explain."""
    check = api._wallet_shortfall
    monkeypatch.setattr(api, "_wallet_shortfall", lambda *args: (False, check(*args)[1]))


def _builder_errors(monkeypatch) -> list[Exception]:
    """Every error TransactionBuilder.build raises from here on."""
    raised: list[Exception] = []
    build = api.TransactionBuilder.build

    def recording(self, *args, **kwargs):
        try:
            return build(self, *args, **kwargs)
        except Exception as error:
            raised.append(error)
            raise

    monkeypatch.setattr(api.TransactionBuilder, "build", recording)
    return raised


def _kept_token_min_ada(world) -> int:
    """The ledger's minimum ADA for the change output carrying the kept token."""
    kept = TransactionOutput(world.user_addr, Value(1_000_000, _tokens((KEPT_POLICY, KEPT_NAME, KEPT_QUANTITY))))
    return min_lovelace_post_alonzo(kept, _MainnetParams([]))


# ---------------------------------------------------------------------------
# Wallets that cannot fund a surrender
# ---------------------------------------------------------------------------

PASSES = tuple(u for u in T1_UNITS if PIN.remaining[u] > 0)[:3]


def _passes_and_agent() -> MultiAsset:
    return _tokens((AGENT_POLICY, AGENT_NAME, 1_000), *((u[:56], u[56:], 1) for u in PASSES))


# name -> (the wallet's UTxOs, AGENT surrendered, passes surrendered, the
# code it is answered with, how the builder fails it when nothing is checked
# first)
UNFUNDED = {
    # The mainnet wallet.
    "one_token_utxo": (
        lambda: [_lace_utxo()],
        AGENT_HELD, (), "wallet_needs_ada", "Minimum lovelace amount for collateral return",
    ),
    # A 5 ADA collateral UTxO, but four assets to quarantine at 1.5 ADA each.
    "collateral_but_four_assets": (
        lambda: [Value(5_000_000), Value(1_500_000, _passes_and_agent())],
        1_000, PASSES, "wallet_needs_ada", "The input UTxOs cannot cover the transaction outputs and tx fee.",
    ),
    "the_asset_and_little_else": (
        lambda: [Value(1_300_000, _agent_only())],
        AGENT_HELD, (), "wallet_needs_ada", "All UTxO selectors failed.",
    ),
    # Enough to pay for the surrender, not to set collateral aside as well.
    "too_little_to_post_collateral": (
        lambda: [Value(1_300_000, _agent_only()), Value(3_000_000)],
        AGENT_HELD, (), "wallet_needs_ada",
        "Minimum collateral amount 3823993 is greater than total provided collateral inputs",
    ),
    # Plenty of ADA, but in UTxOs too small for the builder to post
    # collateral from, beside the mainnet wallet's token UTxO.
    "ada_without_collateral": (
        lambda: [_lace_utxo(), *[Value(1_900_000) for _ in range(4)]],
        AGENT_HELD, (), "wallet_needs_collateral", "Minimum lovelace amount for collateral return",
    ),
}


def _unfunded(world, monkeypatch, name: str) -> HTTPException:
    values, agent, passes, _, _ = UNFUNDED[name]
    _wallet(world, monkeypatch, *values())
    return _refusal(world, agent, passes)


def test_the_fixture_fails_the_build_as_the_mainnet_wallet_did(world, monkeypatch):
    """Built without the check, the fixture fails where the mainnet wallet's
    build failed, with the same amounts."""
    _lace_wallet(world, monkeypatch)
    monkeypatch.setattr(api, "_wallet_shortfall", lambda *args: (False, None), raising=False)
    raised = _builder_errors(monkeypatch)
    _refusal(world, AGENT_HELD)
    assert [str(error) for error in raised] == [
        "Minimum lovelace amount for collateral return 1400750 is greater than collateral "
        "change 493298. Please provide more collateral inputs."
    ]


def test_a_wallet_short_of_ada_is_told_how_much_it_needs(world, monkeypatch):
    """4.317 ADA cannot fund this surrender even with collateral set aside:
    the two outputs it pays for, the change carrying the token it keeps and
    the fee already come to more. So it needs ADA, not only collateral."""
    _lace_wallet(world, monkeypatch)
    raised = _builder_errors(monkeypatch)
    cost = FUNDED_OUTPUTS + _kept_token_min_ada(world) + FIXTURE_FEE
    assert cost > LACE_WALLET_LOVELACE

    refusal = _refusal(world, AGENT_HELD)

    assert refusal.status_code == 422
    assert refusal.detail == {
        "code": "wallet_needs_ada",
        "needed_lovelace": cost + api.COLLATERAL_TARGET_LOVELACE,
        "available_lovelace": LACE_WALLET_LOVELACE,
        "message": (
            "Your wallet needs about 9.6 ADA to redeem (it has 4.3 ADA). Add ADA, then set "
            "collateral in your wallet (Lace: Settings → Collateral), and try again."
        ),
    }
    assert raised == []  # refused before the builder ran
    assert world.cosign_calls == 0 and world.signed_by_admin == []
    # The pool tip was released: the same request answers the same way.
    assert _refusal(world, AGENT_HELD).detail == refusal.detail


def test_adding_what_it_was_told_lets_the_wallet_redeem(world, monkeypatch):
    _lace_wallet(world, monkeypatch)
    detail = _refusal(world, AGENT_HELD).detail
    _lace_wallet(world, monkeypatch, Value(detail["needed_lovelace"] - detail["available_lovelace"]))
    assert world.build_route(AGENT_HELD).tx_hash


def test_a_wallet_with_collateral_but_too_little_ada_needs_ada(world, monkeypatch):
    raised = _builder_errors(monkeypatch)
    refusal = _unfunded(world, monkeypatch, "collateral_but_four_assets")
    assert refusal.status_code == 422
    assert refusal.detail["code"] == "wallet_needs_ada"
    assert refusal.detail["available_lovelace"] == 6_500_000
    # 1.5 ADA for the claimant's cMATRA and per quarantined asset, 5 ADA of
    # collateral, and a fee below 1 ADA; it keeps no token.
    outputs_and_collateral = 1_500_000 + 4 * 1_500_000 + api.COLLATERAL_TARGET_LOVELACE
    assert 0 < refusal.detail["needed_lovelace"] - outputs_and_collateral < 1_000_000
    assert raised == []
    assert api.state.reserved_collateral == set()  # its collateral is free for the next build


def test_adding_what_that_wallet_was_told_lets_it_redeem(world, monkeypatch):
    detail = _unfunded(world, monkeypatch, "collateral_but_four_assets").detail
    shortfall = detail["needed_lovelace"] - detail["available_lovelace"]
    _wallet(world, monkeypatch, Value(5_000_000), Value(1_500_000, _passes_and_agent()), Value(shortfall))
    assert world.build_route(1_000, PASSES).tx_hash


def test_a_wallet_with_ada_but_no_collateral_needs_collateral(world, monkeypatch):
    raised = _builder_errors(monkeypatch)
    refusal = _unfunded(world, monkeypatch, "ada_without_collateral")
    assert refusal.status_code == 422
    assert refusal.detail == {
        "code": "wallet_needs_collateral",
        "needed_lovelace": 5_000_000,
        "available_lovelace": 1_900_000,
        "message": (
            "Your wallet has enough ADA, but it needs 5.0 ADA set aside as collateral to redeem. "
            "Set collateral in your wallet (Lace: Settings → Collateral), and try again."
        ),
    }
    assert raised == []


def test_setting_collateral_lets_that_wallet_redeem(world, monkeypatch):
    _lace_wallet(world, monkeypatch, *[Value(1_900_000) for _ in range(4)], Value(5_000_000))
    assert world.build_route(AGENT_HELD).tx_hash


@pytest.mark.parametrize("name", UNFUNDED)
def test_the_builders_own_error_gets_the_same_answer(world, monkeypatch, name):
    """Were the check before the build to miss a wallet, the builder's own
    balance or collateral error is answered the same way."""
    *_, code, builder_error = UNFUNDED[name]
    before = _unfunded(world, monkeypatch, name)
    assert (before.status_code, before.detail["code"]) == (422, code)
    _checked_after_the_build_only(monkeypatch)
    raised = _builder_errors(monkeypatch)
    after = _unfunded(world, monkeypatch, name)
    assert len(raised) == 1 and str(raised[0]).startswith(builder_error)
    assert (after.status_code, after.detail) == (422, before.detail)
    assert api.state.reserved_collateral == set()


# ---------------------------------------------------------------------------
# Everything else is built, or refused, as before
# ---------------------------------------------------------------------------


def test_a_wallet_holding_everything_in_one_token_utxo_redeems(world, monkeypatch):
    """The builder posts that UTxO as collateral and returns its tokens."""
    _wallet(world, monkeypatch, _lace_utxo(12_000_000))
    assert world.build_route(AGENT_HELD).tx_hash


def test_a_wallet_below_the_advice_that_can_fund_the_surrender_redeems(world, monkeypatch):
    """6.3 ADA is less than it would be told to hold, but its 5 ADA
    collateral and the rest fund this surrender."""
    _wallet(world, monkeypatch, Value(1_300_000, _agent_only()), Value(5_000_000))
    assert world.build_route(AGENT_HELD).tx_hash


def _chained_builds(world) -> tuple[str, str]:
    """A surrender and the next one chained on it, from a funded wallet."""
    api.state.reserved_collateral.clear()
    owed = _entitlement(1_000)
    first, tx_hash, pool_out, change = world.build(owed)
    world.chain[tx_hash] = split_tx(bytes.fromhex(first))[0]
    pool = {"tx_hash": tx_hash, "output_index": 1,
            "cmatra_amount": world.pool_utxo["cmatra_amount"] - owed, "ada_amount": pool_out["ada_amount"]}
    return first, world.build(owed, pool_utxo=pool, user_inputs=change)[0]


def test_a_funded_wallet_builds_exactly_as_before(world, monkeypatch):
    checked = _chained_builds(world)
    monkeypatch.setattr(api, "_wallet_shortfall", lambda *args: (False, None))
    assert _chained_builds(world) == checked


def test_a_wallet_the_chain_cannot_list_gets_the_answer_it_got_before(world, monkeypatch):
    class Unlisted(_MainnetParams):
        def utxos(self, address):
            raise RuntimeError("Blockfrost 500")

    monkeypatch.setattr(api, "BlockFrostChainContext", lambda **kw: Unlisted([]))
    refusal = _refusal(world, 1_000)
    assert refusal.status_code == 422
    assert refusal.detail == "Transaction preflight failed. Please retry."


def test_a_wallet_without_the_asset_gets_the_answer_it_got_before(world, monkeypatch):
    """Short of ADA too, but what it lacks is the asset."""
    _wallet(world, monkeypatch, Value(6_000_000))
    refusal = _refusal(world, 1_000)
    assert refusal.status_code == 422
    assert refusal.detail == "Transaction preflight failed. Please retry."


# name -> (a builder error no wallet causes, its status, its detail or code)
OTHER_BUILDER_ERRORS = {
    "value_error": (ValueError("an unrelated builder failure"), 422,
                    "Transaction preflight failed. Please retry."),
    "invalid_transaction": (InvalidTransactionException("an unrelated invalid transaction"), 500,
                            "Transaction build failed. Please try again."),
    "too_large": (InvalidTransactionException("Transaction size (17000) exceeds the max limit (16384)."), 413,
                  "TX_TOO_LARGE"),
}


@pytest.mark.parametrize("name", OTHER_BUILDER_ERRORS)
def test_another_builder_error_gets_the_answer_it_got_before(world, monkeypatch, name):
    """From a wallet below the advice, so a shortfall is at hand to misuse."""
    error, status, detail = OTHER_BUILDER_ERRORS[name]
    _wallet(world, monkeypatch, Value(1_300_000, _agent_only()), Value(5_000_000))

    def broken(self, **kwargs):
        raise error

    monkeypatch.setattr(api.TransactionBuilder, "build", broken)
    refusal = _refusal(world, AGENT_HELD)
    answer = refusal.detail if isinstance(refusal.detail, str) else refusal.detail["code"]
    assert (refusal.status_code, answer) == (status, detail)


@pytest.mark.parametrize("needed,available,text", [
    (9_400_000, 4_300_000, "about 9.4 ADA to redeem (it has 4.3 ADA)"),
    (9_400_001, 4_399_999, "about 9.5 ADA to redeem (it has 4.3 ADA)"),
    (10_000_000, 0, "about 10.0 ADA to redeem (it has 0.0 ADA)"),
])
def test_amounts_are_shown_to_a_tenth_of_an_ada(needed, available, text):
    """What it needs is rounded up and what it has rounded down, so the
    difference is never understated."""
    assert text in api._shortfall_detail("wallet_needs_ada", needed, available)["message"]
