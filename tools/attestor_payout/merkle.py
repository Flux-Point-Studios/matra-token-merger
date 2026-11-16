"""The Merkle root a payout statement commits to.

A leaf is blake2b-256 of 0x00, the period (4 bytes), the attestor's
AccountId (32 bytes), the amount in cMATRA base units (8 bytes, all
big-endian) and the payout address's raw bytes, so every field but the last
has a fixed width and a Plutus validator can rebuild a leaf by concatenation.
An inner node is blake2b-256 of 0x01 and its two children; a node with no
sibling moves up a level unchanged. No leaves give the zero root.
"""

from __future__ import annotations

import hashlib
from typing import Sequence


def _blake2b_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def leaf_hash(period: int, attestor: bytes, address: bytes, amount: int) -> bytes:
    if len(attestor) != 32:
        raise ValueError(f"an attestor is a 32-byte AccountId, not {len(attestor)} bytes")
    if not (0 <= period < 1 << 32 and 0 <= amount < 1 << 64):
        raise ValueError(f"period {period} or amount {amount} does not fit its leaf field")
    return _blake2b_256(
        b"\x00" + period.to_bytes(4, "big") + attestor + amount.to_bytes(8, "big") + address
    )


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    level = list(leaves)
    if not level:
        return b"\x00" * 32
    while len(level) > 1:
        paired = [_blake2b_256(b"\x01" + level[i] + level[i + 1]) for i in range(0, len(level) - 1, 2)]
        level = paired + level[len(paired) * 2:]
    return level[0]
