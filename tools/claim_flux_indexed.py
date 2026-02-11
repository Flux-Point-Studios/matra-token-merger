#!/usr/bin/env python3
"""
Phase 8 — Claim Client (Indexed)

Uses the claim index to look up a claimant's UTxOs, verifies on-chain
state (address, datum, FLUX quantity, not-yet-spent), builds a claim
transaction, and optionally submits it.

Modes:
  --check-only: report claimable amounts without building a tx
  --submit:     build, sign, and submit the claim tx
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from pycardano import (
    Address,
    MultiAsset,
    PaymentSigningKey,
    PaymentVerificationKey,
    Redeemer,
    RedeemerTag,
    TransactionBuilder,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    RawPlutusData,
    PlutusV3Script,
    ExecutionUnits,
)
from pycardano.hash import ScriptHash as PycScriptHash, TransactionId
from pycardano import AssetName

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import (
    decode_claim_datum,
    encode_claim_datum,
    payment_key_hash_from_skey,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# UTxO verification
# ---------------------------------------------------------------------------


def verify_claim_utxo(
    utxo_data: dict[str, Any],
    expected_script_address: str,
    expected_pkh: str,
    flux_policy_hex: str,
    flux_asset_hex: str,
) -> dict[str, Any]:
    """Verify a single claim UTxO from Blockfrost data.

    Returns a dict with verification results and extracted data.
    """
    issues: list[str] = []

    # Check address
    if utxo_data.get("address") != expected_script_address:
        issues.append(
            f"address mismatch: expected {expected_script_address}, "
            f"got {utxo_data.get('address')}"
        )

    # Check inline datum
    inline_datum = utxo_data.get("inline_datum")
    if inline_datum is None:
        issues.append("no inline datum")
        datum_pkh = None
    else:
        try:
            datum_pkh = decode_claim_datum(inline_datum)
            if datum_pkh != expected_pkh:
                issues.append(
                    f"datum pkh mismatch: expected {expected_pkh}, got {datum_pkh}"
                )
        except Exception as e:
            issues.append(f"datum decode error: {e}")
            datum_pkh = None

    # Check FLUX asset
    flux_qty = 0
    flux_unit_key = flux_policy_hex + flux_asset_hex
    for amount in utxo_data.get("amount", []):
        unit = amount.get("unit", "")
        if unit == flux_unit_key or unit == f"{flux_policy_hex}.{flux_asset_hex}":
            flux_qty = int(amount["quantity"])
            break

    if flux_qty == 0:
        issues.append("no FLUX asset in output")

    # Check if spent
    is_spent = utxo_data.get("consumed_by_tx") is not None

    # Get ADA amount
    ada_amount = 0
    for amount in utxo_data.get("amount", []):
        if amount.get("unit") == "lovelace":
            ada_amount = int(amount["quantity"])
            break

    return {
        "valid": len(issues) == 0 and not is_spent,
        "is_spent": is_spent,
        "issues": issues,
        "flux_units": flux_qty,
        "ada_lovelace": ada_amount,
        "datum_pkh": datum_pkh,
    }


# ---------------------------------------------------------------------------
# Claim building
# ---------------------------------------------------------------------------


def find_claimable_utxos(
    bf: BlockfrostClient,
    pkh_hex: str,
    index: dict[str, list],
    script_address: str,
    flux_policy_hex: str,
    flux_asset_hex: str,
) -> list[dict[str, Any]]:
    """Look up and verify all claim UTxOs for a claimant."""
    refs = index.get(pkh_hex, [])
    if not refs:
        return []

    claimable = []
    for ref in refs:
        tx_hash, output_index, expected_flux = ref[0], ref[1], ref[2]

        try:
            tx_utxos = bf.get_tx_utxos(tx_hash)
        except Exception as e:
            logger.warning("Failed to fetch tx %s: %s", tx_hash, e)
            continue

        # Find the specific output
        outputs = tx_utxos.get("outputs", [])
        target_out = None
        for out in outputs:
            if out.get("output_index") == output_index:
                target_out = out
                break

        if target_out is None:
            logger.warning("Output %d not found in tx %s", output_index, tx_hash)
            continue

        verification = verify_claim_utxo(
            target_out, script_address, pkh_hex,
            flux_policy_hex, flux_asset_hex,
        )

        claimable.append({
            "tx_hash": tx_hash,
            "output_index": output_index,
            "expected_flux": expected_flux,
            **verification,
        })

    return claimable


def build_claim_tx(
    bf: BlockfrostClient,
    skey_path: str,
    claimable: list[dict[str, Any]],
    script_address: str,
    script_cbor_hex: str,
    flux_policy_hex: str,
    flux_asset_hex: str,
) -> bytes:
    """Build and sign a claim transaction spending all valid claim UTxOs."""
    from pycardano import BlockFrostChainContext
    import cbor2

    context = BlockFrostChainContext(
        project_id=bf.project_id,
        base_url=bf.base_url,
    )

    sk = PaymentSigningKey.load(skey_path)
    vk = PaymentVerificationKey.from_signing_key(sk)
    pkh = vk.hash()
    claimant_addr = Address(payment_part=pkh, network=Address.from_primitive(script_address).network)

    # Load script
    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))
    script_hash = PycScriptHash(bytes.fromhex(script_address_to_hash(script_address)))

    builder = TransactionBuilder(context)

    # Add each claimable UTxO as script input
    total_flux = 0
    total_ada = 0

    for claim in claimable:
        if not claim["valid"]:
            continue

        tx_in = TransactionInput(
            TransactionId(bytes.fromhex(claim["tx_hash"])),
            claim["output_index"],
        )

        # Reconstruct the UTxO value
        policy_id = PycScriptHash(bytes.fromhex(flux_policy_hex))
        asset_name = AssetName(bytes.fromhex(flux_asset_hex))
        multi = MultiAsset()
        multi[policy_id] = {asset_name: claim["flux_units"]}
        value = Value(claim["ada_lovelace"], multi)

        # Reconstruct datum
        datum_cbor = encode_claim_datum(claim["datum_pkh"])
        datum = RawPlutusData(cbor2.loads(datum_cbor))

        script_addr = Address.from_primitive(script_address)
        utxo = UTxO(
            tx_in,
            TransactionOutput(script_addr, value, datum=datum),
        )

        redeemer = Redeemer(RawPlutusData(cbor2.loads(b"\xd8\x79\x80")))  # Constr(0, []) = unit

        builder.add_script_input(
            utxo,
            script=script,
            redeemer=redeemer,
        )

        total_flux += claim["flux_units"]
        total_ada += claim["ada_lovelace"]

    # Required signer (critical!)
    builder.required_signers = [pkh]

    # Build and sign
    signed_tx = builder.build_and_sign(
        signing_keys=[sk],
        change_address=claimant_addr,
    )

    return signed_tx.to_cbor()


def script_address_to_hash(script_address: str) -> str:
    """Extract the script hash from a script address."""
    addr = Address.from_primitive(script_address)
    if isinstance(addr.payment_part, PycScriptHash):
        return addr.payment_part.payload.hex()
    raise ValueError(f"Not a script address: {script_address}")


# ---------------------------------------------------------------------------
# Load script from blueprint
# ---------------------------------------------------------------------------


def load_script_from_blueprint(blueprint_path: str) -> str:
    """Load the compiled script CBOR hex from an Aiken plutus.json blueprint."""
    with open(blueprint_path) as f:
        blueprint = json.load(f)

    validators = blueprint.get("validators", [])
    for v in validators:
        if "claim" in v.get("title", "").lower():
            compiled = v.get("compiledCode", "")
            if compiled:
                return compiled

    # Fallback: first validator
    if validators:
        return validators[0].get("compiledCode", "")

    raise ValueError(f"No validators found in {blueprint_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="FLUX merger — Phase 8: Claim Client (Indexed)",
    )
    parser.add_argument("--index-file", type=str, required=True)
    parser.add_argument("--blueprint", type=str, required=True)
    parser.add_argument("--payment-skey", type=str, required=True)
    parser.add_argument("--script-address", type=str, default=None)
    parser.add_argument("--flux-policy", type=str, default=None)
    parser.add_argument("--flux-asset-hex", type=str, default="464c5558")
    parser.add_argument("--check-only", action="store_true", default=False)
    parser.add_argument("--submit", action="store_true", default=False)
    parser.add_argument("--out-cbor", type=str, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Load index
    with open(args.index_file) as f:
        index_data = json.load(f)

    # Determine if minimal or full index
    if "index" in index_data:
        # Full index format
        index = index_data["index"]
        script_address = args.script_address or index_data.get("script_address", "")
        flux_policy = args.flux_policy or index_data.get("flux_policy_hex", "")
        flux_asset = args.flux_asset_hex or index_data.get("flux_asset_hex", "464c5558")
    else:
        # Minimal index format (just the mapping)
        index = index_data
        script_address = args.script_address
        flux_policy = args.flux_policy
        flux_asset = args.flux_asset_hex
        if not script_address or not flux_policy:
            logger.error("--script-address and --flux-policy required with minimal index")
            sys.exit(1)

    # Derive claimant PKH from signing key
    pkh_hex = payment_key_hash_from_skey(args.payment_skey)
    logger.info("Claimant PKH: %s", pkh_hex)

    bf = BlockfrostClient()

    # Find claimable UTxOs
    claimable = find_claimable_utxos(
        bf, pkh_hex, index, script_address, flux_policy, flux_asset,
    )

    valid_claims = [c for c in claimable if c["valid"]]
    spent_claims = [c for c in claimable if c["is_spent"]]
    invalid_claims = [c for c in claimable if not c["valid"] and not c["is_spent"]]

    total_flux = sum(c["flux_units"] for c in valid_claims)
    total_ada = sum(c["ada_lovelace"] for c in valid_claims)

    logger.info("Found %d claim UTxO(s):", len(claimable))
    logger.info("  Valid (claimable): %d — %d FLUX units", len(valid_claims), total_flux)
    logger.info("  Already spent:     %d", len(spent_claims))
    logger.info("  Invalid:           %d", len(invalid_claims))

    for c in invalid_claims:
        logger.warning("  Invalid UTxO %s#%d: %s", c["tx_hash"][:16], c["output_index"], c["issues"])

    if args.check_only:
        logger.info("Check-only mode — not building transaction.")
        print(json.dumps({
            "claimant_pkh": pkh_hex,
            "total_flux_claimable": total_flux,
            "total_ada_claimable": total_ada,
            "valid_utxos": len(valid_claims),
            "spent_utxos": len(spent_claims),
            "invalid_utxos": len(invalid_claims),
        }, indent=2))
        return

    if not valid_claims:
        logger.info("Nothing to claim.")
        return

    # Load script
    script_cbor = load_script_from_blueprint(args.blueprint)

    # Build claim tx
    tx_cbor = build_claim_tx(
        bf, args.payment_skey, valid_claims,
        script_address, script_cbor, flux_policy, flux_asset,
    )

    if args.out_cbor:
        out_path = Path(args.out_cbor)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(tx_cbor if isinstance(tx_cbor, bytes) else bytes.fromhex(tx_cbor))
        logger.info("Claim tx CBOR written to %s", out_path)

    if args.submit:
        cbor_bytes = tx_cbor if isinstance(tx_cbor, bytes) else bytes.fromhex(tx_cbor)
        tx_hash = bf.submit_tx(cbor_bytes)
        logger.info("Claim submitted! TX: %s", tx_hash)
    else:
        logger.info("Transaction built but not submitted. Use --submit to broadcast.")


if __name__ == "__main__":
    main()
