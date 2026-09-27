"""services.chain_check asks the signer's own view of the chain what the
transaction bytes cannot show. A unit whose supply grew after the pin is
refused outright: an edition minted later is indistinguishable from the pinned
one, so paying for either could pay the minter ahead of the holder."""

from __future__ import annotations

import pytest
import requests

from services.chain_check import ChainUnavailable, confirm_on_chain
from services.cosign_policy import Approval, CosignRejected
from tests.cosign_cases import MAINNET, PIN, FakeChain
from tools.api_clients import BlockfrostUnavailable
from tools.config import AGENT, T1_ADAM_PASS

T1_UNIT = next(u for u in sorted(PIN.nft_units) if u.startswith(T1_ADAM_PASS.policy_id))


def _approval(units: dict[str, int]) -> Approval:
    return Approval(
        tx_hash=b"\x01" * 32, payout=1, claimant=b"\x61" + b"\x02" * 28,
        pool_input=(b"\x03" * 32, 0), units=units,
    )


def test_units_at_their_pinned_supply_pass():
    confirm_on_chain(_approval({AGENT.unit: 5, T1_UNIT: 1}), MAINNET, FakeChain())


def test_a_unit_burned_below_its_pinned_supply_passes():
    confirm_on_chain(_approval({AGENT.unit: 5}), MAINNET, FakeChain(supply={AGENT.unit: 10}))


@pytest.mark.parametrize("unit", [T1_UNIT, AGENT.unit])
def test_a_unit_minted_past_its_pinned_supply_is_refused(unit):
    grown = FakeChain(supply={unit: PIN.supply[unit] + 1})
    with pytest.raises(CosignRejected) as err:
        confirm_on_chain(_approval({AGENT.unit: 5, T1_UNIT: 1}), MAINNET, grown)
    assert err.value.code == "supply_grown"
    assert unit in err.value.detail


def test_a_unit_the_chain_view_does_not_know_is_unanswerable():
    chain = FakeChain()
    del chain.supply[T1_UNIT]
    with pytest.raises(ChainUnavailable, match=T1_UNIT):
        confirm_on_chain(_approval({T1_UNIT: 1}), MAINNET, chain)


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("connection refused"),
    requests.Timeout("read timed out"),
    BlockfrostUnavailable("Exhausted retries for /assets/x"),
])
def test_a_chain_view_that_cannot_answer_is_unavailable(failure):
    with pytest.raises(ChainUnavailable):
        confirm_on_chain(_approval({AGENT.unit: 5}), MAINNET, FakeChain(failure=failure))


def test_an_error_answer_from_the_chain_view_is_unavailable():
    response = requests.Response()
    response.status_code = 403
    failure = requests.HTTPError("403 Forbidden", response=response)
    with pytest.raises(ChainUnavailable):
        confirm_on_chain(_approval({AGENT.unit: 5}), MAINNET, FakeChain(failure=failure))
