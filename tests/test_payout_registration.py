"""tools.attestor_payout.registration: an attestor's Cardano payout address,
signed at admission with the address's own payment key through CIP-30
signData (a CIP-8 COSE_Sign1 and its COSE_Key), never a Materios key."""

from __future__ import annotations

import hashlib
import json

import cbor2
import pytest
from nacl.signing import SigningKey
from pycardano import Address, Network, PaymentSigningKey, ScriptHash, VerificationKeyHash
from pycardano.cip import cip8

from tools.attestor_payout.registration import (
    RegistrationError,
    load_registry,
    registration_message,
    verify_registration,
)
from tools.credential_denylist import DenyList

CHAIN = "0e46e33f639a56cc8780fd871d9a15e16d99af248526f907cb560cb40849f7bf"
OTHER_CHAIN = "11" * 32
ATTESTOR = bytes.fromhex("925fe8605fe32a53a7b391498fc1b0ab91d3af7319607bd70b850b4f5fa9d255")
STAKE = VerificationKeyHash(b"\x5a" * 28)
NO_DENIALS = DenyList(labels={b"\xee" * 28: "unrelated"}, allowed=frozenset(), sha256="0" * 64)


def _keyhash(seed: bytes) -> VerificationKeyHash:
    vk = SigningKey(seed).verify_key.encode()
    return VerificationKeyHash(hashlib.blake2b(vk, digest_size=28).digest())


def base_address(seed: bytes, network: Network = Network.TESTNET) -> Address:
    return Address(payment_part=_keyhash(seed), staking_part=STAKE, network=network)


def wallet_sign(
    seed: bytes,
    payload: bytes,
    header_address: Address,
    *,
    key: bytes | None = None,
    hashed: bool = False,
    tagged: bool = False,
) -> dict[str, str]:
    """What a CIP-30 wallet's signData returns: an untagged COSE_Sign1 whose
    protected header names the address it was asked to sign for, and the
    COSE_Key of the key that signed."""
    signer = SigningKey(seed)
    protected = cbor2.dumps({1: -8, "address": bytes(header_address)})
    sig_structure = cbor2.dumps(["Signature1", protected, b"", payload])
    signature = signer.sign(sig_structure).signature
    sign1 = [protected, {"hashed": hashed}, payload, signature]
    encoded = cbor2.dumps(cbor2.CBORTag(18, sign1) if tagged else sign1)
    cose_key = {1: 1, 3: -8, -1: 6, -2: key if key is not None else signer.verify_key.encode()}
    return {"signature": encoded.hex(), "key": cbor2.dumps(cose_key).hex()}


def registration(seed: bytes, address: Address, chain: str = CHAIN, **sign) -> dict[str, str]:
    payload = registration_message(chain, ATTESTOR, address.encode())
    return {
        "attestor": "0x" + ATTESTOR.hex(),
        "address": address.encode(),
        **wallet_sign(seed, payload, sign.pop("header_address", address), **sign),
    }


SEED = b"\x07" * 32
OTHER_SEED = b"\x08" * 32


def test_the_message_names_the_chain_the_attestor_and_the_address():
    address = base_address(SEED).encode()
    assert registration_message(CHAIN, ATTESTOR, address) == (
        "materios-attestor-payout-address/1\n"
        f"chain: {CHAIN}\n"
        f"attestor: 0x{ATTESTOR.hex()}\n"
        f"address: {address}"
    ).encode()


@pytest.mark.parametrize("tagged", [False, True])
def test_a_wallet_signed_base_address_registers(tagged):
    address = base_address(SEED)
    attestor, registered = verify_registration(
        registration(SEED, address, tagged=tagged), CHAIN, Network.TESTNET, NO_DENIALS
    )
    assert attestor == ATTESTOR
    assert registered == address


def test_a_registration_signed_by_pycardano_cip8_verifies():
    """A second implementation's signature: pycardano signs for the key's
    enterprise address with the COSE_Key attached, as CIP-30 returns it."""
    signing_key = PaymentSigningKey(SEED)
    address = Address(payment_part=signing_key.to_verification_key().hash(), network=Network.TESTNET)
    signed = cip8.sign(
        registration_message(CHAIN, ATTESTOR, address.encode()).decode(),
        signing_key,
        attach_cose_key=True,
        network=Network.TESTNET,
    )
    entry = {"attestor": "0x" + ATTESTOR.hex(), "address": address.encode(), **signed}
    assert verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS) == (ATTESTOR, address)


def test_a_signature_over_another_address_does_not_register_this_one():
    entry = registration(SEED, base_address(SEED))
    entry["address"] = Address(
        payment_part=_keyhash(SEED), staking_part=VerificationKeyHash(b"\x5b" * 28), network=Network.TESTNET
    ).encode()
    with pytest.raises(RegistrationError, match="payload"):
        verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS)


def test_a_registration_for_another_chain_does_not_register_on_this_one():
    entry = registration(SEED, base_address(SEED), chain=OTHER_CHAIN)
    with pytest.raises(RegistrationError, match="payload"):
        verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS)


def test_a_key_that_does_not_hold_the_address_cannot_register_it():
    address = base_address(SEED)
    entry = registration(OTHER_SEED, address, header_address=base_address(OTHER_SEED))
    with pytest.raises(RegistrationError, match="payment key"):
        verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS)


def test_a_header_naming_another_payment_key_is_refused():
    entry = registration(SEED, base_address(SEED), header_address=base_address(OTHER_SEED))
    with pytest.raises(RegistrationError, match="header"):
        verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS)


def test_a_forged_signature_is_refused():
    entry = registration(SEED, base_address(SEED))
    sign1 = cbor2.loads(bytes.fromhex(entry["signature"]))
    sign1[3] = bytes([sign1[3][0] ^ 1]) + sign1[3][1:]
    entry["signature"] = cbor2.dumps(sign1).hex()
    with pytest.raises(RegistrationError, match="signature"):
        verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS)


def test_an_extended_key_is_refused():
    vk = SigningKey(SEED).verify_key.encode()
    entry = registration(SEED, base_address(SEED), key=vk + b"\x00" * 32)
    with pytest.raises(RegistrationError, match="Ed25519"):
        verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS)


def test_a_hashed_payload_is_refused():
    entry = registration(SEED, base_address(SEED), hashed=True)
    with pytest.raises(RegistrationError, match="hashed"):
        verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS)


def test_an_address_on_the_other_network_is_refused():
    address = base_address(SEED, Network.MAINNET)
    with pytest.raises(RegistrationError, match="network"):
        verify_registration(registration(SEED, address), CHAIN, Network.TESTNET, NO_DENIALS)


def test_a_script_address_is_refused():
    address = Address(payment_part=ScriptHash(b"\x33" * 28), network=Network.TESTNET)
    entry = registration(SEED, address, header_address=base_address(SEED))
    with pytest.raises(RegistrationError, match="key"):
        verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS)


def test_an_address_naming_a_denied_credential_is_refused():
    denied = DenyList(labels={bytes(STAKE): "drained stake key"}, allowed=frozenset(), sha256="0" * 64)
    with pytest.raises(RegistrationError, match="drained stake key"):
        verify_registration(registration(SEED, base_address(SEED)), CHAIN, Network.TESTNET, denied)


@pytest.mark.parametrize("field", ["signature", "key"])
def test_unreadable_cbor_is_refused(field):
    entry = registration(SEED, base_address(SEED))
    entry[field] = "ff00"
    with pytest.raises(RegistrationError):
        verify_registration(entry, CHAIN, Network.TESTNET, NO_DENIALS)


def _write(tmp_path, doc) -> str:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc))
    return str(path)


def test_the_registry_maps_each_attestor_to_its_verified_address(tmp_path):
    address = base_address(SEED)
    path = _write(tmp_path, {"version": 1, "chain": CHAIN, "registrations": [registration(SEED, address)]})
    assert load_registry(path, CHAIN, Network.TESTNET, NO_DENIALS) == {ATTESTOR: address}


@pytest.mark.parametrize(
    "doc, match",
    [
        ({"version": 2, "chain": CHAIN, "registrations": []}, "version"),
        ({"version": 1, "chain": OTHER_CHAIN, "registrations": []}, "chain"),
        ({"version": 1, "chain": CHAIN}, "keys"),
        ({"version": 1, "chain": CHAIN, "registrations": [], "extra": 1}, "keys"),
        ({"version": 1, "chain": CHAIN, "registrations": {}}, "list"),
    ],
)
def test_a_registry_of_another_shape_is_refused(tmp_path, doc, match):
    with pytest.raises(RegistrationError, match=match):
        load_registry(_write(tmp_path, doc), CHAIN, Network.TESTNET, NO_DENIALS)


def test_an_attestor_registered_twice_is_refused(tmp_path):
    entry = registration(SEED, base_address(SEED))
    path = _write(tmp_path, {"version": 1, "chain": CHAIN, "registrations": [entry, entry]})
    with pytest.raises(RegistrationError, match="twice"):
        load_registry(path, CHAIN, Network.TESTNET, NO_DENIALS)


def test_an_entry_with_an_unknown_field_is_refused(tmp_path):
    entry = {**registration(SEED, base_address(SEED)), "note": "x"}
    path = _write(tmp_path, {"version": 1, "chain": CHAIN, "registrations": [entry]})
    with pytest.raises(RegistrationError, match="keys"):
        load_registry(path, CHAIN, Network.TESTNET, NO_DENIALS)
