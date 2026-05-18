"""
Cardano-specific helpers: bech32 decoding, payment-key-hash extraction,
datum CBOR encoding, and address classification.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from pycardano import (
    Address,
    PaymentVerificationKey,
    PaymentSigningKey,
    ScriptHash,
    TransactionId,
    TransactionInput,
    VerificationKeyHash,
)

# Re-export for convenience
from pycardano import Network

# ---------------------------------------------------------------------------
# Address helpers
# ---------------------------------------------------------------------------


def address_to_payment_key_hash(addr_str: str) -> Optional[str]:
    """Extract the payment key hash (hex) from a bech32 Cardano address.

    Returns None if the address uses a script credential (no key hash).
    """
    try:
        addr = Address.from_primitive(addr_str)
    except Exception:
        return None

    cred = addr.payment_part
    if cred is None:
        return None

    # VerificationKeyHash → base16
    if isinstance(cred, VerificationKeyHash):
        return cred.payload.hex()

    # ScriptHash → not a key hash
    return None


def is_script_address(addr_str: str) -> bool:
    """Return True if *addr_str* is a script (plutus/native) address."""
    try:
        addr = Address.from_primitive(addr_str)
    except Exception:
        return False
    cred = addr.payment_part
    return isinstance(cred, ScriptHash)


def payment_key_hash_from_skey(skey_path: str) -> str:
    """Derive the payment key hash hex from a signing key file."""
    sk = PaymentSigningKey.load(skey_path)
    vk = PaymentVerificationKey.from_signing_key(sk)
    return vk.hash().payload.hex()


# ---------------------------------------------------------------------------
# Datum / CBOR helpers
# ---------------------------------------------------------------------------


def encode_claim_datum(payment_key_hash_hex: str) -> bytes:
    """Encode a ClaimDatum as CBOR.

    ClaimDatum = Constr(0, [bytes(pkh)])  →  CBOR tag 121 + 1-element array.

    Uses cbor2 directly for deterministic encoding.
    """
    import cbor2
    from cbor2 import CBORTag

    pkh_bytes = bytes.fromhex(payment_key_hash_hex)
    assert len(pkh_bytes) == 28, f"Expected 28-byte key hash, got {len(pkh_bytes)}"

    # Constr(0, fields) is encoded as CBOR tag 121 + array of fields
    return cbor2.dumps(CBORTag(121, [pkh_bytes]))


def decode_claim_datum(cbor_hex: str) -> str:
    """Decode a ClaimDatum CBOR hex and return the contained key hash hex."""
    import cbor2

    obj = cbor2.loads(bytes.fromhex(cbor_hex))
    # Expect CBORTag(121, [bytes])
    if hasattr(obj, "tag") and obj.tag == 121:
        pkh_bytes = obj.value[0]
        return pkh_bytes.hex()
    raise ValueError(f"Unexpected datum structure: {obj}")


# ---------------------------------------------------------------------------
# Script address derivation
# ---------------------------------------------------------------------------


def derive_script_address(
    script_hash_hex: str,
    network: Network = Network.MAINNET,
) -> str:
    """Derive a Cardano script address (enterprise) from a script hash."""
    sh = ScriptHash(bytes.fromhex(script_hash_hex))
    addr = Address(payment_part=sh, network=network)
    return str(addr)


# ---------------------------------------------------------------------------
# Min-ADA calculation (simplified)
# ---------------------------------------------------------------------------


def posix_ms_to_slot(posix_ms: int, network: str = "mainnet") -> int:
    """Convert POSIX milliseconds to a Cardano slot number.

    Mainnet: shelley_start_slot=4492800, shelley_start_time=1596491091
    Preprod: shelley_start_slot=0,       shelley_start_time=1655683200
    """
    posix_sec = posix_ms // 1000
    if network == "mainnet":
        return (posix_sec - 1596491091) + 4492800
    elif network == "preprod":
        return posix_sec - 1655683200
    else:
        # preview
        return posix_sec - 1655683200


# ---------------------------------------------------------------------------
# Parameterized validator helpers
# ---------------------------------------------------------------------------
#
# Apply parameters to a parameterized validator via the Aiken CLI directly:
#
#   aiken blueprint apply <CBOR-hex-encoded-param> -i plutus.json -o applied.json
#
# Aiken applies one parameter at a time, in declaration order. Encode each
# parameter as Plutus Data CBOR (see Aiken docs). For the surrender_pool
# validator the order is: admin_pkh_1, admin_pkh_2, deadline.
#
# Earlier in-repo Python helpers (`load_parameterized_script`,
# `apply_validator_params_cli`) hardcoded the old `claim_validator.claim_validator.spend`
# validator title with a 2-param shape (admin_pkh, deadline). Both were
# removed in this PR — see PR #6 — because the current `surrender_pool`
# validator takes 3 params and they had no in-repo callers. Use `aiken
# blueprint apply` directly from a deploy script. The corresponding mainnet
# deploy values are pinned at the top of the runbook.


def estimate_min_ada(
    num_assets: int = 1,
    datum_size_bytes: int = 40,
    coins_per_utxo_byte: int = 4310,
) -> int:
    """Estimate the minimum ADA (in lovelace) for a UTxO.

    Uses a simplified model: base_size + per-asset overhead + datum size.
    Conservative estimate — real min-ADA depends on serialized UTxO size.
    """
    # Base UTxO overhead ~160 bytes, each additional asset ~28 bytes
    utxo_size = 160 + (num_assets * 28) + datum_size_bytes
    return max(utxo_size * coins_per_utxo_byte, 1_000_000)  # at least 1 ADA
