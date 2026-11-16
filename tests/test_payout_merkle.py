"""tools.attestor_payout.merkle: the root a payout statement commits to."""

from __future__ import annotations

import hashlib

import pytest

from tools.attestor_payout.merkle import leaf_hash, merkle_root


def _b2(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


ATTESTOR = bytes(range(32))
ADDRESS = bytes.fromhex("61") + b"\x11" * 28


def test_a_leaf_hashes_its_fields_in_fixed_width_behind_a_leaf_tag():
    expected = _b2(
        b"\x00"
        + (202701).to_bytes(4, "big")
        + ATTESTOR
        + (1_234_567).to_bytes(8, "big")
        + ADDRESS
    )
    assert leaf_hash(202701, ATTESTOR, ADDRESS, 1_234_567) == expected


@pytest.mark.parametrize(
    "period, attestor, amount",
    [
        (202701, ATTESTOR[:31], 1),
        (202701, ATTESTOR, -1),
        (202701, ATTESTOR, 1 << 64),
        (-1, ATTESTOR, 1),
        (1 << 32, ATTESTOR, 1),
    ],
)
def test_a_leaf_refuses_fields_that_do_not_fit_their_width(period, attestor, amount):
    with pytest.raises(ValueError):
        leaf_hash(period, attestor, ADDRESS, amount)


def test_every_field_moves_the_leaf():
    base = leaf_hash(202701, ATTESTOR, ADDRESS, 5)
    assert leaf_hash(202702, ATTESTOR, ADDRESS, 5) != base
    assert leaf_hash(202701, b"\x01" * 32, ADDRESS, 5) != base
    assert leaf_hash(202701, ATTESTOR, ADDRESS[:-1] + b"\x12", 5) != base
    assert leaf_hash(202701, ATTESTOR, ADDRESS, 6) != base


def test_the_root_pairs_nodes_behind_a_node_tag_and_moves_an_odd_node_up():
    a, b, c = (_b2(bytes([n])) for n in range(3))
    ab = _b2(b"\x01" + a + b)
    assert merkle_root([a]) == a
    assert merkle_root([a, b]) == ab
    assert merkle_root([a, b, c]) == _b2(b"\x01" + ab + c)


def test_a_repeated_last_leaf_is_a_different_tree():
    a, b, c = (_b2(bytes([n])) for n in range(3))
    assert merkle_root([a, b, c]) != merkle_root([a, b, c, c])


def test_an_empty_statement_commits_to_the_zero_root():
    assert merkle_root([]) == b"\x00" * 32
