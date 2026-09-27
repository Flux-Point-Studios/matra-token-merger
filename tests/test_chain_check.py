"""services.chain_check asks the signer's own view of the chain what the
transaction bytes cannot show.

A unit whose supply grew after the pin is refused outright: an edition minted
later is indistinguishable from the pinned one.

Every input must be an unspent output on chain, or an output of a surrender
this signer already recorded (chained surrenders spend outputs still in the
mempool). A producing body proves what an output would hold, not that it
exists, so without this a caller could invent outputs, and approvals that
can never land would still use up limits."""

from __future__ import annotations

import pytest
import requests

from services.chain_check import ChainUnavailable, confirm_on_chain
from services.cosign_policy import Approval, CosignRejected
from tests.cbor_encode import encode
from tests.cosign_cases import MAINNET, PIN, FakeChain, blake
from tools.api_clients import BlockfrostUnavailable
from tools.config import AGENT, T1_ADAM_PASS

T1_UNIT = next(u for u in sorted(PIN.nft_units) if u.startswith(T1_ADAM_PASS.policy_id))


CLAIMANT = b"\x61" + b"\x02" * 28
# A producing body with two outputs, and a chain view that has seen it.
PARENT = encode({0: [], 1: [[CLAIMANT, 1_000_000], [CLAIMANT, 2_000_000]], 2: 0})
PARENT_ID = blake(PARENT)
RECORDED_ID = b"\x09" * 32


def _chain(**kwargs) -> FakeChain:
    return FakeChain({PARENT_ID.hex(): PARENT}, **kwargs)


def _approval(units: dict[str, int], inputs=((PARENT_ID, 0), (PARENT_ID, 1))) -> Approval:
    return Approval(
        tx_hash=b"\x01" * 32, payout=1, claimant=CLAIMANT,
        pool_input=inputs[0], units=units, inputs=tuple(inputs),
    )


def _confirm(approval: Approval, chain: FakeChain, recorded=frozenset()) -> None:
    confirm_on_chain(approval, MAINNET, chain, recorded.__contains__)


def test_units_at_their_pinned_supply_pass():
    _confirm(_approval({AGENT.unit: 5, T1_UNIT: 1}), _chain())


def test_a_unit_burned_below_its_pinned_supply_passes():
    _confirm(_approval({AGENT.unit: 5}), _chain(supply={AGENT.unit: 10}))


@pytest.mark.parametrize("unit", [T1_UNIT, AGENT.unit])
def test_a_unit_minted_past_its_pinned_supply_is_refused(unit):
    grown = _chain(supply={unit: PIN.supply[unit] + 1})
    with pytest.raises(CosignRejected) as err:
        _confirm(_approval({AGENT.unit: 5, T1_UNIT: 1}), grown)
    assert err.value.code == "supply_grown"
    assert unit in err.value.detail


def test_a_unit_the_chain_view_does_not_know_is_unanswerable():
    chain = _chain()
    del chain.supply[T1_UNIT]
    with pytest.raises(ChainUnavailable, match=T1_UNIT):
        _confirm(_approval({T1_UNIT: 1}), chain)


def _forbidden() -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 403
    return requests.HTTPError("403 Forbidden", response=response)


FAILURES = [
    requests.ConnectionError("connection refused"),
    requests.Timeout("read timed out"),
    BlockfrostUnavailable("Exhausted retries"),
    _forbidden(),
]


@pytest.mark.parametrize("failure", FAILURES, ids=type)
def test_a_chain_view_that_cannot_answer_is_unavailable(failure):
    with pytest.raises(ChainUnavailable):
        _confirm(_approval({AGENT.unit: 5}), _chain(failure=failure))


@pytest.mark.parametrize("failure", FAILURES, ids=type)
def test_an_input_lookup_that_fails_is_unavailable_not_unknown(failure):
    """Only a 404 means the chain has no such transaction."""
    class InputsUnanswered(FakeChain):
        def get_tx_utxos(self, tx_hash):
            raise failure

    with pytest.raises(ChainUnavailable):
        _confirm(_approval({AGENT.unit: 5}), InputsUnanswered({PARENT_ID.hex(): PARENT}))


# -- inputs --------------------------------------------------------------------


def test_an_input_from_a_transaction_the_chain_never_saw_is_refused():
    invented = b"\x0e" * 32
    with pytest.raises(CosignRejected) as err:
        _confirm(_approval({AGENT.unit: 5}, [(PARENT_ID, 0), (invented, 0)]), _chain())
    assert err.value.code == "input_unknown"
    assert invented.hex() in err.value.detail


def test_an_input_already_spent_is_refused():
    chain = _chain()
    chain.spent[(PARENT_ID.hex(), 1)] = "ab" * 32
    with pytest.raises(CosignRejected) as err:
        _confirm(_approval({AGENT.unit: 5}), chain)
    assert err.value.code == "input_spent"
    assert f"{PARENT_ID.hex()}#1" in err.value.detail


def test_a_collateral_return_the_chain_lists_is_not_an_output():
    """Blockfrost lists a valid transaction's collateral return at the next
    index, although the ledger never created it."""
    with pytest.raises(CosignRejected) as err:
        _confirm(_approval({AGENT.unit: 5}, [(PARENT_ID, 0), (PARENT_ID, 2)]), _chain())
    assert err.value.code == "input_spent"


def test_outputs_of_a_surrender_this_signer_recorded_need_no_chain_answer():
    """A chained surrender spends its parent's outputs while the parent is
    still in the mempool."""
    chain = _chain()
    _confirm(_approval({AGENT.unit: 5}, [(RECORDED_ID, 1), (PARENT_ID, 0)]), chain, {RECORDED_ID})


def test_an_answer_without_the_spent_marker_is_unavailable():
    class Truncated(FakeChain):
        def get_tx_utxos(self, tx_hash):
            answer = super().get_tx_utxos(tx_hash)
            for output in answer["outputs"]:
                del output["consumed_by_tx"]
            return answer

    with pytest.raises(ChainUnavailable):
        _confirm(_approval({AGENT.unit: 5}), Truncated({PARENT_ID.hex(): PARENT}))


def test_the_supply_is_confirmed_before_any_input_is_looked_up():
    """Either refusal is final; the cheaper, broader one comes first."""
    grown = FakeChain(supply={AGENT.unit: PIN.supply[AGENT.unit] + 1})
    with pytest.raises(CosignRejected) as err:
        _confirm(_approval({AGENT.unit: 5}), grown)
    assert err.value.code == "supply_grown"
