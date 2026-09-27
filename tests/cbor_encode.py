"""Minimal CBOR encoder for the values services.cosign_policy.decode returns.

Tests decode real transactions, mutate them and re-encode; definite lengths
and shortest-form integers throughout."""

from __future__ import annotations

from typing import Any

from services.cosign_policy import Tag


def _head(major: int, arg: int) -> bytes:
    if arg < 24:
        return bytes([major << 5 | arg])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if arg < 1 << (8 * size):
            return bytes([major << 5 | info]) + arg.to_bytes(size, "big")
    raise ValueError(f"argument too large: {arg}")


def encode(value: Any) -> bytes:
    if value is False:
        return b"\xf4"
    if value is True:
        return b"\xf5"
    if value is None:
        return b"\xf6"
    if isinstance(value, Tag):
        return _head(6, value.tag) + encode(value.value)
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, bytes):
        return _head(2, len(value)) + value
    if isinstance(value, str):
        data = value.encode()
        return _head(3, len(data)) + data
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(encode(v) for v in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(
            encode(k) + encode(v) for k, v in value.items()
        )
    raise TypeError(f"cannot encode {type(value).__name__}")
