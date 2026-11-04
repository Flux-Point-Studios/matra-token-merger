"""Compromised Cardano credentials, and the address check that refuses them.

The list is one JSON file, its shape published beside it as a JSON Schema:

    {"version": 1,
     "credentials": [{"hash": "<28 bytes, hex>", "label": "...", "evidence": "..."}, ...],
     "allow": [{"hash": "<28 bytes, hex>", "label": "...", "reason": "..."}, ...]}

A credential is the hash of a payment or stake key or script. An address is
denied when it names a listed credential in either part, so a listed key is
not reached through an enterprise address, nor through a base address that
pairs it with an unlisted one. A pointer address names its stake credential
by a pointer to a certificate on chain, not by its hash, so it cannot be
checked and is refused as such; so is any address that names no credential
in a form this check reads.

``allow`` names credentials the list must never deny, such as the wallet that
received the funds rescued from listed ones: a list that denies one of them
is refused, so a regenerated or hand-edited list cannot catch them.

The loader takes exactly that shape and refuses anything else: a missing or
unknown key, a key repeated in one object, a version other than the integer 1,
a hash that is not 56 lowercase hex digits, a hash listed twice, an empty or
blank label, evidence or reason, and an empty credential list. A file that
cannot be read raises as it would anywhere, so a signer that loads the list
at startup does not start without it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_HASH = re.compile(r"[0-9a-f]{56}")


class DenyListError(ValueError):
    """The file is not a deny-list of this version."""


class PointerAddress(ValueError):
    """The address names its stake credential by a pointer, not a hash."""


def address_credentials(address: bytes) -> tuple[bytes, ...]:
    """The credential hashes a Shelley address names: payment and stake for a
    base address, payment for an enterprise address. Raises
    :class:`PointerAddress` for a pointer address, and ValueError for any
    other header or a length its header does not allow."""
    kind = address[0] >> 4 if address else None
    if kind in (0, 1, 2, 3) and len(address) == 57:
        return address[1:29], address[29:57]
    if kind in (6, 7) and len(address) == 29:
        return (address[1:29],)
    if kind in (4, 5):
        raise PointerAddress(f"{address.hex()} names its stake credential by pointer")
    raise ValueError(f"{address.hex()!r} is not a base or enterprise address")


@dataclass(frozen=True)
class DenyList:
    labels: Mapping[bytes, str]
    """Each denied credential and what it is."""
    allowed: frozenset[bytes]
    """Credentials the list may never deny."""
    sha256: str
    """Of the file the list was loaded from."""

    def denied(self, address: bytes) -> str | None:
        """The label of a listed credential ``address`` names, or None. Raises
        as :func:`address_credentials` does for an address it cannot read."""
        for credential in address_credentials(address):
            if credential in self.labels:
                return self.labels[credential]
        return None


def _object(value: Any, keys: set[str], where: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise DenyListError(f"{where} must be an object with exactly the keys {sorted(keys)}")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DenyListError(f"{where} must be nonempty text")
    return value


def _hashes(entries: Any, note: str, where: str) -> dict[bytes, str]:
    """Hash -> label of each entry in ``entries``, whose other field is ``note``."""
    if not isinstance(entries, list):
        raise DenyListError(f"{where} must be a list")
    found: dict[bytes, str] = {}
    for n, entry in enumerate(entries):
        entry = _object(entry, {"hash", "label", note}, f"{where}[{n}]")
        if not (isinstance(entry["hash"], str) and _HASH.fullmatch(entry["hash"])):
            raise DenyListError(f"{where}[{n}].hash must be 56 lowercase hex digits")
        _text(entry[note], f"{where}[{n}].{note}")
        credential = bytes.fromhex(entry["hash"])
        if credential in found:
            raise DenyListError(f"{where} lists {entry['hash']} twice")
        found[credential] = _text(entry["label"], f"{where}[{n}].label")
    return found


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise DenyListError(f"a key is repeated in one object: {sorted(keys)}")
    return dict(pairs)


def load_denylist(path: str | Path) -> DenyList:
    """The deny-list at ``path``. Raises :class:`DenyListError` for anything
    but a well-formed list, and OSError for a file that cannot be read."""
    raw = Path(path).read_bytes()
    try:
        doc = json.loads(raw, object_pairs_hook=_unique_keys)
    except DenyListError as exc:
        raise DenyListError(f"{path}: {exc}") from None
    except ValueError as exc:
        raise DenyListError(f"{path}: not JSON: {exc}") from None
    try:
        doc = _object(doc, {"version", "credentials", "allow"}, "the list")
        if type(doc["version"]) is not int or doc["version"] != 1:
            raise DenyListError(f"version must be the integer 1, not {doc['version']!r}")
        labels = _hashes(doc["credentials"], "evidence", "credentials")
        if not labels:
            raise DenyListError("credentials must not be empty")
        allowed = frozenset(_hashes(doc["allow"], "reason", "allow"))
    except DenyListError as exc:
        raise DenyListError(f"{path}: {exc}") from None
    both = sorted(credential.hex() for credential in allowed & labels.keys())
    if both:
        raise DenyListError(f"{path}: denies credentials it must allow: {both}")
    return DenyList(labels, allowed, hashlib.sha256(raw).hexdigest())
