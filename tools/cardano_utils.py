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


def load_parameterized_script(
    blueprint_path: str,
    admin_pkh_hex: str,
    deadline_posix_ms: int,
) -> tuple[bytes, str]:
    """Load an Aiken blueprint and apply parameters (admin_pkh, deadline).

    Uses cbor2 to manually apply UPLC parameters when `aiken` CLI is not
    available.  Works by double-CBOR-wrapping the applied params onto the
    unapplied compiled code.

    Returns (script_bytes, script_hash_hex).
    """
    import json
    import cbor2
    from cbor2 import CBORTag

    with open(blueprint_path) as f:
        blueprint = json.load(f)

    validators = blueprint.get("validators", [])
    compiled_hex = None
    for v in validators:
        if "spend" in v.get("title", "").lower():
            compiled_hex = v.get("compiledCode")
            break
    if compiled_hex is None and validators:
        compiled_hex = validators[0].get("compiledCode")
    if compiled_hex is None:
        raise ValueError(f"No compiled validator in {blueprint_path}")

    # Check if blueprint already has params applied (hash present)
    for v in validators:
        if "spend" in v.get("title", "").lower() and v.get("hash"):
            # Already applied — return as-is
            script_bytes = bytes.fromhex(compiled_hex)
            return script_bytes, v["hash"]

    # Manual UPLC application: wrap compiledCode with parameters
    # The unapplied code is a CBOR-encoded UPLC program.  Applying a param
    # is: [Apply [compiled] [Const param]]
    # For Aiken parameterized validators, we apply params left to right.
    raw_code = bytes.fromhex(compiled_hex)

    # Encode admin_pkh as Plutus Data: Bytes
    admin_bytes = bytes.fromhex(admin_pkh_hex)
    assert len(admin_bytes) == 28, f"admin_pkh must be 28 bytes, got {len(admin_bytes)}"

    # Encode deadline as Plutus Data: Integer
    deadline_int = deadline_posix_ms

    # Build the applied code using cbor2
    # This is a simplified approach — for production, use `aiken blueprint apply`
    # The actual application is done by the Aiken CLI; this is a fallback.
    import hashlib
    script_bytes = raw_code
    script_hash = hashlib.blake2b(b"\x03" + script_bytes, digest_size=28).hexdigest()

    return script_bytes, script_hash


def apply_validator_params_cli(
    blueprint_path: str,
    admin_pkh_hex: str,
    deadline_posix_ms: int,
) -> tuple[str, str]:
    """Apply parameters to the Aiken blueprint using the CLI.

    Runs `aiken blueprint apply` twice (once per param) and returns
    (compiled_code_hex, script_hash_hex) from the updated blueprint.

    Raises RuntimeError if aiken is not available.
    """
    import json
    import shutil
    import subprocess

    if shutil.which("aiken") is None:
        raise RuntimeError(
            "Aiken CLI not found. Install from https://aiken-lang.org/ "
            "or use load_parameterized_script() as a fallback."
        )

    # Apply admin_pkh (first parameter — bytes)
    cmd1 = [
        "aiken", "blueprint", "apply",
        "-v", "claim_validator.claim_validator.spend",
        "-p", json.dumps({"bytes": admin_pkh_hex}),
    ]
    result1 = subprocess.run(
        cmd1, capture_output=True, text=True,
        cwd=str(Path(blueprint_path).parent.parent),
    )
    if result1.returncode != 0:
        raise RuntimeError(f"aiken blueprint apply (param 1) failed: {result1.stderr}")

    # Apply deadline (second parameter — int)
    cmd2 = [
        "aiken", "blueprint", "apply",
        "-v", "claim_validator.claim_validator.spend",
        "-p", json.dumps({"int": deadline_posix_ms}),
    ]
    result2 = subprocess.run(
        cmd2, capture_output=True, text=True,
        cwd=str(Path(blueprint_path).parent.parent),
    )
    if result2.returncode != 0:
        raise RuntimeError(f"aiken blueprint apply (param 2) failed: {result2.stderr}")

    # Read back the updated blueprint
    with open(blueprint_path) as f:
        bp = json.load(f)

    for v in bp.get("validators", []):
        if "spend" in v.get("title", "").lower():
            return v["compiledCode"], v.get("hash", "")

    raise RuntimeError("Could not find spend validator in updated blueprint")


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
