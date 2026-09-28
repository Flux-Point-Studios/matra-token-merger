"""services.cosign_policy against every mainnet surrender and the adversarial
corpus in tests/cosign_cases.py."""

from __future__ import annotations

import pytest

import random

from services.cosign_policy import (
    CosignRejected,
    Simple,
    Tag,
    decode,
    evaluate_surrender,
    load_config,
    require_claimant_signature,
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
    WITNESS_CASES,
    MAINNET,
    POOL_BECH32,
    QUARANTINE_BECH32,
    ROOT,
    SURRENDER_HASHES,
    BASE_FUNGIBLE,
    BASE_T1,
    Draft,
    Scenario,
    _elements,
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

    pool_in = sum(cmatra(produced[t][i]) for t, i in _elements(body[0]) if produced[t][i][0] == MAINNET.pool_address)
    pool_out = sum(cmatra(o) for o in body[1] if o[0] == MAINNET.pool_address)
    return pool_in - pool_out


SURRENDERS = SURRENDER_HASHES
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
    body = decode(split_tx(scenario.tx)[0])
    spent = {tuple(ref) for ref in _elements(body[0])}
    collateral = {tuple(ref) for ref in _elements(body.get(13, []))}
    assert set(approval.spends) == spent
    assert set(approval.inputs) == spent | collateral


@pytest.mark.parametrize("tx_hash", SURRENDERS)
def test_every_mainnet_surrender_carries_its_claimants_signature(tx_hash):
    scenario = golden_scenario(tx_hash)
    require_claimant_signature(scenario.tx, _evaluate(scenario))


@pytest.mark.parametrize("case", WITNESS_CASES, ids=[c.name for c in WITNESS_CASES])
def test_unsigned_surrender_is_refused_for_the_right_reason(case):
    s = case.build()
    with pytest.raises(CosignRejected) as err:
        require_claimant_signature(s.tx, _evaluate(s))
    assert err.value.code == case.expected


def test_cbor_booleans_are_not_integers():
    assert decode(b"\xf5") != 1
    assert decode(b"\xf4") != 0


def _garbage(rng: random.Random, depth: int = 0):
    choices = [
        lambda: rng.randrange(-3, 2**40),
        lambda: bytes(rng.randrange(256) for _ in range(rng.choice([0, 1, 28, 29, 32, 57]))),
        lambda: rng.choice([Simple.TRUE, Simple.FALSE, None]),
        lambda: "x",
        lambda: Tag(rng.choice([0, 24, 121, 258, 259]), rng.randrange(3)),
        lambda: [],
    ]
    if depth < 2:
        choices += [
            lambda: [_garbage(rng, depth + 1) for _ in range(rng.randrange(3))],
            lambda: {rng.randrange(4): _garbage(rng, depth + 1)},
            lambda: Tag(258, [_garbage(rng, depth + 1)]),
        ]
    return rng.choice(choices)()


def _garbage_key(rng: random.Random):
    return rng.choice([rng.randrange(20), (0,), Tag(258, (1,)), Simple.TRUE, b"\x05", "x", None])


def _maps(value):
    """Every map at or under ``value``."""
    if isinstance(value, dict):
        yield value
        children = value.values()
    elif isinstance(value, list):
        children = value
    elif isinstance(value, Tag):
        children = [value.value]
    else:
        return
    for child in list(children):
        yield from _maps(child)


def _slots(value):
    """Every (container, key) under ``value`` that a new value can replace."""
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = enumerate(value)
    elif isinstance(value, Tag):
        yield from _slots(value.value)
        return
    else:
        return
    for key, child in list(items):
        yield value, key
        yield from _slots(child)


@pytest.mark.parametrize("base", [BASE_T1, BASE_FUNGIBLE])
def test_garbage_anywhere_is_refused_with_a_code(base):
    """Random values, or random keys in a map, dropped anywhere in a
    surrender's body, witness set or producing bodies are approved or refused
    with a code, never a crash (the co-signer would answer 500)."""
    rng = random.Random(base)
    for _ in range(400):
        d = Draft(golden_scenario(base))
        d.reindex = rng.random() < 0.5
        root = rng.choice([d.body, d.ws, *d.parents])
        if rng.random() < 0.25:
            rng.choice(list(_maps(root)))[_garbage_key(rng)] = _garbage(rng)
        else:
            container, key = rng.choice(list(_slots(root)))
            container[key] = _garbage(rng)
        try:
            s = d.build()
        except (KeyError, IndexError, TypeError, ValueError, StopIteration, AttributeError):
            continue  # the edit broke the test's own re-encoding, not the policy's input
        try:
            require_claimant_signature(s.tx, _evaluate(s))
        except CosignRejected:
            pass


@pytest.mark.parametrize("opens", ["long_before", "absent"])
def test_a_validity_interval_already_open_is_approved(opens):
    """Every mainnet surrender starts 1000 slots before its builder's tip; a
    start further back, or none, leaves the interval open when checked."""
    s = golden_scenario(BASE_T1)
    d = Draft(s)
    if opens == "absent":
        del d.body[8]
    else:
        d.body[8] -= 100_000
    assert _evaluate(d.build()).payout == _evaluate(s).payout


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
    "REDEMPTION_PIN_PATH": str(ROOT / "audit_pack/2026-09-27/redemption_pin.json"),
    "SURRENDER_DEADLINE_POSIX_MS": "1795910400000",
    "MAX_CMATRA_PER_TX": str(MAINNET.max_payout_per_tx),
}


def test_config_from_environment_matches_mainnet():
    assert load_config(ENV, [ADMIN_1, ADMIN_2]) == MAINNET


def test_config_defaults_to_the_committed_rate_table_and_pin():
    env = {k: v for k, v in ENV.items() if k not in ("RATE_TABLE_PATH", "REDEMPTION_PIN_PATH")}
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
