"""services.cosign_policy against every mainnet surrender and the adversarial
corpus in tests/cosign_cases.py."""

from __future__ import annotations

import pytest

from services.cosign_policy import (
    CosignRejected,
    decode,
    evaluate_surrender,
    load_config,
    slot_at,
    split_tx,
)
from tests.cosign_cases import (
    ADMIN_1,
    ADMIN_2,
    CASES,
    DECODE_ACCEPTED,
    DECODE_REFUSED,
    SPLIT_REFUSED,
    MAINNET,
    POOL_BECH32,
    QUARANTINE_BECH32,
    ROOT,
    Scenario,
    blake,
    golden,
    golden_scenario,
)


def _evaluate(s: Scenario):
    return evaluate_surrender(s.tx, s.language_views, s.parents, s.now_slot, s.cfg)


def _pool_outflow(s: Scenario) -> int:
    """cMATRA the pool lost, read straight from the ledger view: the pool
    input's producing output minus the pool outputs of the transaction."""
    body = decode(split_tx(s.tx)[0])
    produced = {blake(parent): decode(parent)[1] for parent in s.parents}

    def cmatra(output) -> int:
        value = output[1]
        return value[1].get(MAINNET.cmatra_policy, {}).get(MAINNET.cmatra_name, 0) if isinstance(value, list) else 0

    refs = body[0].value if hasattr(body[0], "tag") else body[0]
    pool_in = sum(cmatra(produced[t][i]) for t, i in refs if produced[t][i][0] == MAINNET.pool_address)
    pool_out = sum(cmatra(o) for o in body[1] if o[0] == MAINNET.pool_address)
    return pool_in - pool_out


SURRENDERS = [t["tx_hash"] for t in golden()["transactions"] if t["kind"] == "surrender"]
CEREMONIES = [t["tx_hash"] for t in golden()["transactions"] if t["kind"] == "ceremony"]


def test_golden_set_covers_every_pool_transaction():
    assert len(SURRENDERS) == 266
    assert len(CEREMONIES) == 3


@pytest.mark.parametrize("tx_hash", SURRENDERS)
def test_every_mainnet_surrender_is_approved_at_its_real_payout(tx_hash):
    scenario = golden_scenario(tx_hash)
    approval = _evaluate(scenario)
    assert approval.tx_hash.hex() == tx_hash
    assert approval.payout == _pool_outflow(scenario) > 0


@pytest.mark.parametrize("tx_hash", CEREMONIES)
def test_pool_ceremonies_are_not_surrenders(tx_hash):
    with pytest.raises(CosignRejected):
        _evaluate(golden_scenario(tx_hash))


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_adversarial_transaction_is_refused_for_the_right_reason(case):
    with pytest.raises(CosignRejected) as err:
        _evaluate(case.build())
    assert err.value.code == case.expected


def test_corpus_has_one_case_per_name():
    names = [c.name for c in CASES]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("name,raw", DECODE_REFUSED, ids=[n for n, _ in DECODE_REFUSED])
def test_decoder_refuses_what_the_ledger_would_read_differently(name, raw):
    with pytest.raises(CosignRejected) as err:
        decode(raw)
    assert err.value.code == "cbor"


@pytest.mark.parametrize("name,raw", SPLIT_REFUSED, ids=[n for n, _ in SPLIT_REFUSED])
def test_split_tx_refuses_a_truncated_final_item(name, raw):
    with pytest.raises(CosignRejected) as err:
        split_tx(raw)
    assert err.value.code == "cbor"


@pytest.mark.parametrize("raw,value", DECODE_ACCEPTED)
def test_decoder_reads_valid_encodings(raw, value):
    assert decode(raw) == value


def test_split_tx_returns_exact_body_witnesses_and_tail():
    tx = golden_scenario(SURRENDERS[0]).tx
    body, witnesses, tail = split_tx(tx)
    assert tx == b"\x84" + body + witnesses + tail
    assert tail == b"\xf5\xf6"


def test_slot_at_matches_mainnet_shelley_offset():
    # The v2 mint was included at slot 187,585,545 at 2026-05-19T00:50:36Z.
    assert slot_at(1_779_151_836, "mainnet") == 187_585_545


# --- configuration --------------------------------------------------------------

ENV = {
    "NETWORK": "mainnet",
    "SURRENDER_SCRIPT_ADDRESS": POOL_BECH32,
    "QUARANTINE_ADDRESS": QUARANTINE_BECH32,
    "CMATRA_POLICY_HEX": MAINNET.cmatra_policy.hex(),
    "CMATRA_ASSET_HEX": MAINNET.cmatra_name.hex(),
    "RATE_TABLE_PATH": str(ROOT / "audit_pack/2026-04-19/rate_table_cmatra.json"),
    "REDEEMABLE_NFTS_PATH": str(ROOT / "audit_pack/2026-09-27/redeemable_nft_units.json"),
    "SURRENDER_DEADLINE_POSIX_MS": "1795910400000",
    "MAX_CMATRA_PER_TX": str(MAINNET.max_payout_per_tx),
}


def test_config_from_environment_matches_mainnet():
    assert load_config(ENV, [ADMIN_1, ADMIN_2]) == MAINNET


def test_config_defaults_to_the_committed_rate_table_and_pin():
    env = {k: v for k, v in ENV.items() if k not in ("RATE_TABLE_PATH", "REDEEMABLE_NFTS_PATH")}
    assert load_config(env, [ADMIN_1, ADMIN_2]) == MAINNET


@pytest.mark.parametrize("missing", [
    "NETWORK", "SURRENDER_SCRIPT_ADDRESS", "QUARANTINE_ADDRESS", "CMATRA_POLICY_HEX",
    "CMATRA_ASSET_HEX", "SURRENDER_DEADLINE_POSIX_MS", "MAX_CMATRA_PER_TX",
])
def test_config_refuses_to_guess(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        load_config(env, [ADMIN_1, ADMIN_2])


def test_config_needs_two_distinct_admins():
    with pytest.raises(ValueError, match="two distinct admin"):
        load_config(ENV, [ADMIN_1, ADMIN_1])
