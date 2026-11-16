"""Payout-address registration: at admission an attestor signs, with the
payment key of the Cardano address it is to be paid at, a message naming the
Materios chain, its AccountId and that address. The signature is what CIP-30
``signData`` returns: a CIP-8 COSE_Sign1 and the COSE_Key that made it. A
Materios key never signs it, so a stolen Materios hot key cannot redirect a
payout.

The check here is written against RFC 9052 rather than through
``pycardano.cip.cip8.verify``, which re-encodes the protected header before
checking the signature, accepts a stake key or an extended key, and states
its preconditions as ``assert``. It verifies the signature over the protected
header exactly as the wallet encoded it, takes only a 32-byte Ed25519 key,
refuses a hashed payload, and requires that key to be the payout address's
payment key and the one the protected header names.

The registry is one JSON file:

    {"version": 1, "chain": "<genesis, hex>",
     "registrations": [{"attestor": "0x<AccountId>", "address": "<bech32>",
                        "signature": "<COSE_Sign1, hex>", "key": "<COSE_Key, hex>"}]}
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import cbor2
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pycardano import Address, Network, VerificationKeyHash

from tools.credential_denylist import DenyList

DOMAIN = "materios-attestor-payout-address/1"
_COSE_SIGN1_TAG = 18
_ALG, _EDDSA = 1, -8
_KTY, _OKP, _CRV, _ED25519, _X = 1, 1, -1, 6, -2
_ENTRY_KEYS = {"attestor", "address", "signature", "key"}


class RegistrationError(ValueError):
    """A registration, or the registry, that does not prove its address."""


def registration_message(chain: str, attestor: bytes, address: str) -> bytes:
    """What the attestor's wallet signs, as text a wallet shows."""
    return f"{DOMAIN}\nchain: {chain}\nattestor: 0x{attestor.hex()}\naddress: {address}".encode()


def parse_attestor(value: Any) -> bytes:
    if not (isinstance(value, str) and value.startswith("0x") and len(value) == 66):
        raise ValueError(f"an attestor is a 0x-prefixed 32-byte AccountId, not {value!r}")
    return bytes.fromhex(value[2:])


def _cbor(hex_text: Any, what: str) -> Any:
    try:
        return cbor2.loads(bytes.fromhex(hex_text))
    except (TypeError, ValueError, cbor2.CBORDecodeError) as exc:
        raise RegistrationError(f"the {what} is not hex CBOR: {exc}") from None


def _signed_payload(signature_hex: Any, key_hex: Any) -> tuple[bytes, bytes, bytes]:
    """(payload, the header's address, the signing key's hash) of a
    COSE_Sign1 whose Ed25519 signature verifies under the COSE_Key."""
    sign1 = _cbor(signature_hex, "signature")
    if isinstance(sign1, cbor2.CBORTag):
        if sign1.tag != _COSE_SIGN1_TAG:
            raise RegistrationError(f"the signature is CBOR tag {sign1.tag}, not COSE_Sign1")
        sign1 = sign1.value
    if not (isinstance(sign1, list) and len(sign1) == 4):
        raise RegistrationError("the signature is not a COSE_Sign1 array")
    protected, unprotected, payload, signature = sign1
    if not (isinstance(protected, bytes) and isinstance(unprotected, dict)
            and isinstance(payload, bytes) and isinstance(signature, bytes)):
        raise RegistrationError("the signature's COSE_Sign1 fields have the wrong types")
    if unprotected.get("hashed", False) is not False:
        raise RegistrationError("the wallet signed a hashed payload, not the message")
    header = _cbor(protected.hex(), "protected header")
    if not isinstance(header, dict) or header.get(_ALG) != _EDDSA or not isinstance(header.get("address"), bytes):
        raise RegistrationError("the protected header does not name EdDSA and an address")

    key = _cbor(key_hex, "key")
    if not (isinstance(key, dict) and key.get(_KTY) == _OKP and key.get(_CRV) == _ED25519
            and key.get(3, _EDDSA) == _EDDSA and isinstance(key.get(_X), bytes) and len(key[_X]) == 32):
        raise RegistrationError("the key is not a 32-byte Ed25519 COSE_Key")
    sig_structure = cbor2.dumps(["Signature1", protected, b"", payload])
    try:
        VerifyKey(key[_X]).verify(sig_structure, signature)
    except (BadSignatureError, ValueError):
        raise RegistrationError("the signature does not verify under the key") from None
    return payload, header["address"], hashlib.blake2b(key[_X], digest_size=28).digest()


def verify_registration(
    entry: dict, chain: str, network: Network, denylist: DenyList
) -> tuple[bytes, Address]:
    """The attestor and the payout address ``entry`` proves, or
    RegistrationError."""
    try:
        attestor = parse_attestor(entry["attestor"])
        address = Address.from_primitive(entry["address"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistrationError(f"unreadable registration: {exc}") from None
    if address.network != network:
        raise RegistrationError(f"{entry['address']} is not on the {network.name.lower()} network")
    if not isinstance(address.payment_part, VerificationKeyHash):
        raise RegistrationError(f"{entry['address']} is paid to a script, not a key that can sign")
    try:
        denied = denylist.denied(bytes(address))
    except ValueError as exc:
        raise RegistrationError(f"{entry['address']}: {exc}") from None
    if denied:
        raise RegistrationError(f"{entry['address']} names a denied credential: {denied}")

    payload, header_address, signer = _signed_payload(entry.get("signature"), entry.get("key"))
    if payload != registration_message(chain, attestor, entry["address"]):
        raise RegistrationError("the signed payload is not this registration's message")
    try:
        header_payment = Address.from_primitive(header_address).payment_part
    except (TypeError, ValueError) as exc:
        raise RegistrationError(f"the protected header's address is unreadable: {exc}") from None
    if header_payment is None or header_payment.payload != signer:
        raise RegistrationError("the protected header names an address the signing key does not hold")
    if address.payment_part.payload != signer:
        raise RegistrationError(f"the signing key is not the payment key of {entry['address']}")
    return attestor, address


def load_registry(path: str | Path, chain: str, network: Network, denylist: DenyList) -> dict[bytes, Address]:
    """Each registered attestor's verified payout address."""
    try:
        doc = json.loads(Path(path).read_text())
    except ValueError as exc:
        raise RegistrationError(f"{path}: not JSON: {exc}") from None
    if not isinstance(doc, dict) or set(doc) != {"version", "chain", "registrations"}:
        raise RegistrationError(f"{path}: the registry must have exactly the keys chain, registrations, version")
    if type(doc["version"]) is not int or doc["version"] != 1:
        raise RegistrationError(f"{path}: version must be the integer 1")
    if doc["chain"] != chain:
        raise RegistrationError(f"{path}: registered for chain {doc['chain']!r}, not {chain}")
    if not isinstance(doc["registrations"], list):
        raise RegistrationError(f"{path}: registrations must be a list")
    registry: dict[bytes, Address] = {}
    for n, entry in enumerate(doc["registrations"]):
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise RegistrationError(f"{path}: registrations[{n}] must have exactly the keys {sorted(_ENTRY_KEYS)}")
        attestor, address = verify_registration(entry, chain, network, denylist)
        if attestor in registry:
            raise RegistrationError(f"{path}: 0x{attestor.hex()} is registered twice")
        registry[attestor] = address
    return registry
