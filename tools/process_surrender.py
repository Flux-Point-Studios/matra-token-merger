#!/usr/bin/env python3
"""
tools/process_surrender.py — Admin tool for processing surrender requests.

In the surrender-and-redeem model, users surrender legacy assets (AGENT,
SHARDS, or NFTs) in exchange for cMATRA at the fixed rate from the rate
table.  This tool builds transactions that:

  1. Spend a pool UTxO from the surrender script to release cMATRA.
  2. Send cMATRA to the user's address at the fixed rate.
  3. Route the user's legacy assets to a quarantine/burn address.
  4. Return the remaining pool balance back to the script.

The admin signing key is required as a required-signer on the transaction
(the on-chain validator checks for admin authorization via the
ProcessSurrender redeemer).

Used by:
  - The admin operator to process incoming surrender requests.
  - Reads rate tables produced by tools/flux_merge_valuation_int.py.
  - Uses patterns from tools/claim_flux_indexed.py and
    tools/build_claim_utxos_flux.py for pycardano transaction building.
  - Relies on tools/api_clients.py (BlockfrostClient) and tools/config.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cbor2
from cbor2 import CBORTag

from pycardano import (
    Address,
    AssetName,
    MultiAsset,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    RawPlutusData,
    Redeemer,
    TransactionBuilder,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.hash import ScriptHash as PycScriptHash, TransactionId

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import estimate_min_ada, payment_key_hash_from_skey
from tools.config import FLUX_DECIMALS, PUBLIC_POOL_BASE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CBOR constants for Plutus Data constructors
# ---------------------------------------------------------------------------

# ProcessSurrender redeemer: Constr(0, []) = CBORTag(121, [])
_PROCESS_SURRENDER_REDEEMER_CBOR = cbor2.dumps(CBORTag(121, []))

# AdminWithdraw redeemer: Constr(1, []) = CBORTag(122, [])
_ADMIN_WITHDRAW_REDEEMER_CBOR = cbor2.dumps(CBORTag(122, []))

# Void datum: Constr(0, []) = CBORTag(121, [])
_VOID_DATUM_CBOR = cbor2.dumps(CBORTag(121, []))


# ---------------------------------------------------------------------------
# Rate table loading
# ---------------------------------------------------------------------------


def load_rate_table(rate_table_path: Path) -> dict[str, Any]:
    """Load a rate table JSON produced by build_rate_table().

    The rate table has a ``tokens`` dict keyed by asset name, where each
    entry includes ``rate_base_per_unit`` and ``is_nft``.

    Parameters
    ----------
    rate_table_path:
        Path to the JSON file on disk.

    Returns
    -------
    dict
        The parsed rate table dictionary.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If the file is not valid JSON or is missing expected keys.
    """
    if not rate_table_path.exists():
        raise FileNotFoundError(f"Rate table not found: {rate_table_path}")

    with open(rate_table_path) as f:
        data = json.load(f)

    if "tokens" not in data:
        raise ValueError(
            f"Rate table missing 'tokens' key: {rate_table_path}"
        )

    return data


# ---------------------------------------------------------------------------
# Redemption computation
# ---------------------------------------------------------------------------


def compute_redemption(
    rate_table: dict[str, Any],
    asset_name: str,
    quantity_base: int,
) -> int:
    """Compute the cMATRA redemption amount for a given legacy asset quantity.

    For fungible tokens: ``redemption = quantity_base * rate_base_per_unit``.
    For NFTs (where quantity is a count of individual NFTs):
    ``redemption = quantity * rate_base_per_unit``.

    Parameters
    ----------
    rate_table:
        Parsed rate table from :func:`load_rate_table`.
    asset_name:
        The legacy asset name key (e.g. "AGENT", "SHARDS", "FLUX_PASS").
    quantity_base:
        For fungible tokens, the amount in base units.
        For NFTs, the count of NFTs being surrendered.

    Returns
    -------
    int
        cMATRA amount in base units (12-decimal).

    Raises
    ------
    KeyError
        If the asset_name is not in the rate table.
    ValueError
        If quantity_base is non-positive or rate is zero.
    """
    tokens = rate_table.get("tokens", {})
    if asset_name not in tokens:
        raise KeyError(
            f"Asset '{asset_name}' not found in rate table. "
            f"Available: {sorted(tokens.keys())}"
        )

    entry = tokens[asset_name]
    rate_base = entry["rate_base_per_unit"]

    if rate_base <= 0:
        raise ValueError(
            f"Rate for '{asset_name}' is zero or negative: {rate_base}"
        )
    if quantity_base <= 0:
        raise ValueError(
            f"Quantity must be positive, got {quantity_base}"
        )

    # Prefer rational (numerator/denominator) when available — avoids the
    # floor() precision loss that truncates e.g. SHARDS from 29.12 -> 29
    # (~3% under-redemption).  Rational fields were added in v5.1 by
    # build_rate_table().  Legacy rate tables without them still use the
    # integer rate_base_per_unit for backwards compatibility.
    rate_num = entry.get("rate_numerator")
    rate_den = entry.get("rate_denominator")
    if rate_num is not None and rate_den is not None and rate_den > 0:
        # Floor division — on-chain contract uses the same formula
        return (quantity_base * rate_num) // rate_den

    # Fallback: integer rate (legacy)
    # Both fungible and NFT use the same formula:
    #   fungible: quantity_base (in smallest unit) * rate_base_per_unit
    #   NFT:      count_of_nfts * rate_base_per_unit
    redemption = quantity_base * rate_base
    return redemption


# ---------------------------------------------------------------------------
# Pool UTxO discovery
# ---------------------------------------------------------------------------


def find_pool_utxos(
    bf: BlockfrostClient,
    script_address: str,
    cmatra_policy_hex: str,
    cmatra_asset_hex: str,
) -> list[dict[str, Any]]:
    """Query the surrender script address for pool UTxOs that hold cMATRA.

    Scans all UTxOs at the script address and filters for those containing
    the cMATRA token.

    Parameters
    ----------
    bf:
        Blockfrost client instance.
    script_address:
        Bech32 address of the surrender script.
    cmatra_policy_hex:
        Hex-encoded policy ID for cMATRA.
    cmatra_asset_hex:
        Hex-encoded asset name for cMATRA.

    Returns
    -------
    list[dict]
        Each entry: ``{tx_hash, output_index, cmatra_amount, ada_amount}``,
        sorted by cmatra_amount descending (largest pools first).
    """
    cmatra_unit = cmatra_policy_hex + cmatra_asset_hex
    utxos = bf.get_address_utxos(script_address)

    pool_utxos: list[dict[str, Any]] = []

    for utxo in utxos:
        tx_hash = utxo.get("tx_hash", "")
        output_index = utxo.get("output_index", utxo.get("tx_index", 0))

        # Only accept UTxOs with the correct Void inline datum.
        # Void = Constr(0, []) = CBOR d87980.  Blockfrost returns the
        # inline_datum as a JSON object: {"constructor": 0, "fields": []}.
        # Reject: no datum, datum hash only (no inline), or wrong datum.
        inline = utxo.get("inline_datum")
        if not inline:
            logger.debug(
                "Skipping UTxO %s#%d — no inline datum",
                tx_hash[:16], output_index,
            )
            continue
        # Verify Void: constructor 0, empty fields
        if not (isinstance(inline, dict)
                and inline.get("constructor") == 0
                and inline.get("fields") == []):
            logger.debug(
                "Skipping UTxO %s#%d — non-Void datum: %s",
                tx_hash[:16], output_index, str(inline)[:80],
            )
            continue

        cmatra_amount = 0
        ada_amount = 0

        for amount_entry in utxo.get("amount", []):
            unit = amount_entry.get("unit", "")
            qty = int(amount_entry.get("quantity", 0))

            if unit == "lovelace":
                ada_amount = qty
            elif unit == cmatra_unit:
                cmatra_amount = qty

        if cmatra_amount > 0:
            pool_utxos.append({
                "tx_hash": tx_hash,
                "output_index": output_index,
                "cmatra_amount": cmatra_amount,
                "ada_amount": ada_amount,
            })

    # Sort largest-first so we pick the best pool UTxO for spending
    pool_utxos.sort(key=lambda u: u["cmatra_amount"], reverse=True)

    logger.info(
        "Found %d pool UTxO(s) at %s with total %d cMATRA base units",
        len(pool_utxos),
        script_address[:24] + "...",
        sum(u["cmatra_amount"] for u in pool_utxos),
    )

    return pool_utxos


# ---------------------------------------------------------------------------
# Script address helper (duplicated from claim_flux_indexed for locality)
# ---------------------------------------------------------------------------


def _script_address_to_hash(script_address: str) -> str:
    """Extract the script hash hex from a bech32 script address."""
    addr = Address.from_primitive(script_address)
    if isinstance(addr.payment_part, PycScriptHash):
        return addr.payment_part.payload.hex()
    raise ValueError(f"Not a script address: {script_address}")


# ---------------------------------------------------------------------------
# Transaction building
# ---------------------------------------------------------------------------


def build_surrender_tx(
    bf: BlockfrostClient,
    admin_skey_path: str,
    pool_utxo: dict[str, Any],
    user_address: str,
    cmatra_amount: int,
    legacy_assets: list[dict[str, Any]],
    quarantine_address: str,
    script_address: str,
    script_cbor_hex: str,
    cmatra_policy_hex: str,
    cmatra_asset_hex: str,
) -> bytes:
    """Build and sign a surrender-processing transaction.

    The transaction structure:
      - Script input:  pool UTxO (with ProcessSurrender redeemer)
      - Output 1:      cMATRA to user_address
      - Output 2:      remaining pool balance back to script_address (void datum)
      - Output 3:      legacy assets to quarantine_address
      - Required signer: admin PKH

    Parameters
    ----------
    bf:
        Blockfrost client for chain context.
    admin_skey_path:
        Path to the admin payment signing key (.skey file).
    pool_utxo:
        Dict with ``tx_hash``, ``output_index``, ``cmatra_amount``,
        ``ada_amount`` — from :func:`find_pool_utxos`.
    user_address:
        Bech32 address where the user will receive cMATRA.
    cmatra_amount:
        cMATRA base units to send to the user.
    legacy_assets:
        List of dicts, each: ``{policy_hex, asset_hex, quantity}``.
        These are the legacy tokens the user is surrendering.
    quarantine_address:
        Bech32 address where legacy assets are sent (burn/quarantine).
    script_address:
        Bech32 address of the surrender script.
    script_cbor_hex:
        Hex-encoded compiled PlutusV3 script CBOR.
    cmatra_policy_hex:
        Hex policy ID for cMATRA.
    cmatra_asset_hex:
        Hex asset name for cMATRA.

    Returns
    -------
    bytes
        Signed transaction CBOR bytes.

    Raises
    ------
    ValueError
        If the pool UTxO does not contain enough cMATRA.
    """
    from pycardano import BlockFrostChainContext

    if cmatra_amount > pool_utxo["cmatra_amount"]:
        raise ValueError(
            f"Pool UTxO has {pool_utxo['cmatra_amount']} cMATRA base units "
            f"but {cmatra_amount} requested. Select a larger pool UTxO or "
            f"reduce the batch size."
        )

    # Chain context
    context = BlockFrostChainContext(
        project_id=bf.project_id,
        base_url=bf.base_url,
    )

    # Admin signing key and PKH
    sk = PaymentSigningKey.load(admin_skey_path)
    vk = PaymentVerificationKey.from_signing_key(sk)
    admin_pkh = vk.hash()
    admin_addr = Address(
        payment_part=admin_pkh,
        network=Address.from_primitive(script_address).network,
    )

    # Load the PlutusV3 script
    script = PlutusV3Script(bytes.fromhex(script_cbor_hex))

    # Build the pool UTxO as pycardano UTxO
    policy_id = PycScriptHash(bytes.fromhex(cmatra_policy_hex))
    asset_name = AssetName(bytes.fromhex(cmatra_asset_hex))

    pool_multi = MultiAsset()
    pool_multi[policy_id] = {asset_name: pool_utxo["cmatra_amount"]}
    pool_value = Value(pool_utxo["ada_amount"], pool_multi)

    # Pool datum is Void: Constr(0, [])
    pool_datum = RawPlutusData(cbor2.loads(_VOID_DATUM_CBOR))

    script_addr = Address.from_primitive(script_address)
    tx_in = TransactionInput(
        TransactionId(bytes.fromhex(pool_utxo["tx_hash"])),
        pool_utxo["output_index"],
    )
    utxo = UTxO(
        tx_in,
        TransactionOutput(script_addr, pool_value, datum=pool_datum),
    )

    # ProcessSurrender redeemer: Constr(0, [])
    redeemer = Redeemer(
        RawPlutusData(cbor2.loads(_PROCESS_SURRENDER_REDEEMER_CBOR))
    )

    builder = TransactionBuilder(context)

    # Add script input (the pool UTxO)
    builder.add_script_input(
        utxo,
        script=script,
        redeemer=redeemer,
    )

    # Admin's address for collateral and change
    builder.add_input_address(admin_addr)

    # --- Output 1: cMATRA to user ---
    user_multi = MultiAsset()
    user_multi[policy_id] = {asset_name: cmatra_amount}
    user_min_ada = estimate_min_ada(num_assets=1, datum_size_bytes=0)
    user_value = Value(user_min_ada, user_multi)
    user_addr = Address.from_primitive(user_address)
    builder.add_output(TransactionOutput(user_addr, user_value))

    # --- Output 2: remaining pool back to script ---
    remaining_cmatra = pool_utxo["cmatra_amount"] - cmatra_amount
    if remaining_cmatra > 0:
        return_multi = MultiAsset()
        return_multi[policy_id] = {asset_name: remaining_cmatra}
        # Keep the same ADA in the returned pool UTxO
        return_min_ada = estimate_min_ada(num_assets=1, datum_size_bytes=8)
        return_value = Value(return_min_ada, return_multi)
        return_datum = RawPlutusData(cbor2.loads(_VOID_DATUM_CBOR))
        builder.add_output(
            TransactionOutput(script_addr, return_value, datum=return_datum)
        )

    # --- Output 3: legacy assets to quarantine ---
    if legacy_assets:
        quarantine_multi = MultiAsset()
        for la in legacy_assets:
            la_policy = PycScriptHash(bytes.fromhex(la["policy_hex"]))
            la_asset = AssetName(bytes.fromhex(la["asset_hex"]))
            if la_policy not in quarantine_multi:
                quarantine_multi[la_policy] = {}
            quarantine_multi[la_policy][la_asset] = la["quantity"]

        quarantine_min_ada = estimate_min_ada(
            num_assets=len(legacy_assets), datum_size_bytes=0,
        )
        quarantine_addr = Address.from_primitive(quarantine_address)
        quarantine_value = Value(quarantine_min_ada, quarantine_multi)
        builder.add_output(TransactionOutput(quarantine_addr, quarantine_value))

    # Required signer: admin PKH (on-chain check)
    builder.required_signers = [admin_pkh]

    # Build and sign
    signed_tx = builder.build_and_sign(
        signing_keys=[sk],
        change_address=admin_addr,
    )

    logger.info(
        "Built surrender tx: %d cMATRA -> %s, pool remainder: %d",
        cmatra_amount,
        user_address[:24] + "...",
        remaining_cmatra,
    )

    return signed_tx.to_cbor()


# ---------------------------------------------------------------------------
# Blueprint loader (mirrors claim_flux_indexed pattern)
# ---------------------------------------------------------------------------


def load_script_from_blueprint(blueprint_path: str) -> str:
    """Load the compiled script CBOR hex from an Aiken plutus.json blueprint.

    Searches for a validator whose title contains 'surrender' or 'pool',
    falling back to the first validator if no match is found.

    Parameters
    ----------
    blueprint_path:
        Path to the Aiken plutus.json blueprint file.

    Returns
    -------
    str
        Hex-encoded compiled CBOR of the validator script.
    """
    with open(blueprint_path) as f:
        blueprint = json.load(f)

    validators = blueprint.get("validators", [])

    # Try to find the surrender/pool validator by title
    for keyword in ("surrender", "pool", "spend"):
        for v in validators:
            title = v.get("title", "").lower()
            if keyword in title:
                compiled = v.get("compiledCode", "")
                if compiled:
                    return compiled

    # Fallback: first validator
    if validators:
        compiled = validators[0].get("compiledCode", "")
        if compiled:
            return compiled

    raise ValueError(f"No validators found in {blueprint_path}")


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


def process_surrender_batch(
    bf: BlockfrostClient,
    admin_skey_path: str,
    rate_table: dict[str, Any],
    surrender_requests: list[dict[str, Any]],
    script_address: str,
    script_cbor_hex: str,
    cmatra_policy_hex: str,
    cmatra_asset_hex: str,
    quarantine_address: str,
    out_dir: Path | None = None,
    submit: bool = False,
    preflight: bool = False,
) -> dict[str, Any]:
    """Process a batch of surrender requests.

    Each surrender request is a dict:
        ``{user_address: str, asset_name: str, quantity_base: int}``

    Optionally, a request may include ``legacy_assets`` — a list of
    ``{policy_hex, asset_hex, quantity}`` dicts for the legacy tokens
    being surrendered.  If not provided, the tool resolves them from
    the rate table and config.

    Parameters
    ----------
    bf:
        Blockfrost client.
    admin_skey_path:
        Path to admin .skey file.
    rate_table:
        Parsed rate table dict.
    surrender_requests:
        List of surrender request dicts.
    script_address:
        Bech32 address of the surrender script.
    script_cbor_hex:
        Hex compiled PlutusV3 script.
    cmatra_policy_hex:
        cMATRA policy hex.
    cmatra_asset_hex:
        cMATRA asset name hex.
    quarantine_address:
        Bech32 quarantine/burn address.
    out_dir:
        Directory for output files (CBOR + report).
    submit:
        If True, submit transactions to chain.
    preflight:
        If True, run evaluate_tx before submitting.

    Returns
    -------
    dict
        Report with processed surrenders, totals, and any errors.
    """
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    # Discover pool UTxOs
    pool_utxos = find_pool_utxos(
        bf, script_address, cmatra_policy_hex, cmatra_asset_hex,
    )
    if not pool_utxos:
        raise RuntimeError(
            f"No pool UTxOs with cMATRA found at {script_address}"
        )

    # Resolve legacy asset info from config for known assets
    from tools.config import ALL_MERGE_ASSETS
    asset_lookup: dict[str, Any] = {}
    for asset_info in ALL_MERGE_ASSETS:
        asset_lookup[asset_info.name] = asset_info

    results: list[dict[str, Any]] = []
    total_cmatra_distributed = 0
    pool_idx = 0  # Index into pool_utxos (move to next when exhausted)

    for i, req in enumerate(surrender_requests):
        user_address = req["user_address"]
        asset_name = req["asset_name"]
        quantity_base = req["quantity_base"]

        label = f"request_{i}({user_address[:16]}.../{asset_name})"

        # Compute redemption
        try:
            cmatra_amount = compute_redemption(
                rate_table, asset_name, quantity_base,
            )
        except (KeyError, ValueError) as e:
            logger.error("%s: redemption computation failed: %s", label, e)
            results.append({
                "index": i,
                "user_address": user_address,
                "asset_name": asset_name,
                "quantity_base": quantity_base,
                "status": "error",
                "error": str(e),
            })
            continue

        # Find a pool UTxO with sufficient balance
        selected_pool = None
        for pidx in range(pool_idx, len(pool_utxos)):
            if pool_utxos[pidx]["cmatra_amount"] >= cmatra_amount:
                selected_pool = pool_utxos[pidx]
                pool_idx = pidx
                break

        if selected_pool is None:
            logger.error(
                "%s: no pool UTxO with sufficient cMATRA "
                "(need %d, largest available: %d)",
                label,
                cmatra_amount,
                pool_utxos[0]["cmatra_amount"] if pool_utxos else 0,
            )
            results.append({
                "index": i,
                "user_address": user_address,
                "asset_name": asset_name,
                "quantity_base": quantity_base,
                "cmatra_amount": cmatra_amount,
                "status": "error",
                "error": "insufficient pool balance",
            })
            continue

        # Build legacy_assets list from request or config
        legacy_assets = req.get("legacy_assets")
        if legacy_assets is None and asset_name in asset_lookup:
            info = asset_lookup[asset_name]
            legacy_assets = [{
                "policy_hex": info.policy_id,
                "asset_hex": info.asset_name_hex,
                "quantity": quantity_base,
            }]
        elif legacy_assets is None:
            # Unknown asset — caller must provide legacy_assets explicitly
            logger.warning(
                "%s: no legacy_assets provided and asset not in config; "
                "building tx without quarantine output",
                label,
            )
            legacy_assets = []

        # Build the transaction
        try:
            tx_cbor = build_surrender_tx(
                bf=bf,
                admin_skey_path=admin_skey_path,
                pool_utxo=selected_pool,
                user_address=user_address,
                cmatra_amount=cmatra_amount,
                legacy_assets=legacy_assets,
                quarantine_address=quarantine_address,
                script_address=script_address,
                script_cbor_hex=script_cbor_hex,
                cmatra_policy_hex=cmatra_policy_hex,
                cmatra_asset_hex=cmatra_asset_hex,
            )
        except Exception as e:
            logger.error("%s: tx build failed: %s", label, e)
            results.append({
                "index": i,
                "user_address": user_address,
                "asset_name": asset_name,
                "quantity_base": quantity_base,
                "cmatra_amount": cmatra_amount,
                "status": "error",
                "error": f"tx build failed: {e}",
            })
            continue

        # Save CBOR
        if out_dir:
            cbor_path = out_dir / f"surrender_{i:04d}.cbor"
            cbor_bytes = (
                tx_cbor if isinstance(tx_cbor, bytes) else bytes.fromhex(tx_cbor)
            )
            with open(cbor_path, "wb") as f:
                f.write(cbor_bytes)
            logger.info("%s: CBOR saved to %s", label, cbor_path)

        # Preflight evaluation
        if preflight:
            from pycardano import BlockFrostChainContext
            ctx = BlockFrostChainContext(
                project_id=bf.project_id,
                base_url=bf.base_url,
            )
            try:
                cbor_bytes = (
                    tx_cbor if isinstance(tx_cbor, bytes)
                    else bytes.fromhex(tx_cbor)
                )
                eval_result = ctx.evaluate_tx(cbor_bytes)
                logger.info("%s: preflight OK — %s", label, eval_result)
            except Exception as e:
                logger.error(
                    "%s: preflight FAILED — %s. Skipping submit.", label, e,
                )
                results.append({
                    "index": i,
                    "user_address": user_address,
                    "asset_name": asset_name,
                    "quantity_base": quantity_base,
                    "cmatra_amount": cmatra_amount,
                    "status": "preflight_failed",
                    "error": str(e)[:500],
                })
                continue

        # Submit
        tx_hash_hex = None
        if submit:
            try:
                cbor_bytes = (
                    tx_cbor if isinstance(tx_cbor, bytes)
                    else bytes.fromhex(tx_cbor)
                )
                tx_hash_hex = bf.submit_tx(cbor_bytes)
                logger.info("%s: submitted — tx %s", label, tx_hash_hex)
            except Exception as e:
                logger.error("%s: submit failed — %s", label, e)
                results.append({
                    "index": i,
                    "user_address": user_address,
                    "asset_name": asset_name,
                    "quantity_base": quantity_base,
                    "cmatra_amount": cmatra_amount,
                    "status": "submit_failed",
                    "error": str(e)[:500],
                })
                continue

        # Update pool balance tracking (for next iteration)
        selected_pool["cmatra_amount"] -= cmatra_amount
        total_cmatra_distributed += cmatra_amount

        results.append({
            "index": i,
            "user_address": user_address,
            "asset_name": asset_name,
            "quantity_base": quantity_base,
            "cmatra_amount": cmatra_amount,
            "cmatra_display": cmatra_amount / (10 ** FLUX_DECIMALS),
            "status": "submitted" if submit else "built",
            "tx_hash": tx_hash_hex,
            "pool_utxo": f"{selected_pool['tx_hash']}#{selected_pool['output_index']}",
        })

    report = {
        "report_type": "surrender_batch",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script_address": script_address,
        "quarantine_address": quarantine_address,
        "total_requests": len(surrender_requests),
        "total_succeeded": sum(
            1 for r in results if r["status"] in ("built", "submitted")
        ),
        "total_failed": sum(
            1 for r in results
            if r["status"] in ("error", "preflight_failed", "submit_failed")
        ),
        "total_cmatra_distributed_base": total_cmatra_distributed,
        "total_cmatra_distributed_display": (
            total_cmatra_distributed / (10 ** FLUX_DECIMALS)
        ),
        "results": results,
    }

    if out_dir:
        report_path = out_dir / "surrender_report.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Report written to %s", report_path)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for processing surrender requests.

    Supports two modes for specifying requests:
      --requests-json FILE   : a JSON file with a list of request dicts
      --request USER ASSET QTY : repeatable flag for individual requests
    """
    parser = argparse.ArgumentParser(
        description="cMATRA merger — Process Surrender Requests",
    )
    parser.add_argument(
        "--rate-table", type=str, required=True,
        help="Path to the rate table JSON (from build_rate_table output)",
    )
    parser.add_argument(
        "--requests-json", type=str, default=None,
        help="Path to JSON file with list of surrender requests",
    )
    parser.add_argument(
        "--request", nargs=3, action="append", default=[],
        metavar=("USER_ADDR", "ASSET", "QTY"),
        help="Individual surrender request: user_address asset_name quantity_base "
             "(repeatable)",
    )
    parser.add_argument(
        "--admin-skey", type=str, required=True,
        help="Path to admin payment signing key (.skey)",
    )
    parser.add_argument(
        "--blueprint", type=str, default=None,
        help="Path to Aiken plutus.json blueprint (alternative to --script-cbor)",
    )
    parser.add_argument(
        "--script-cbor", type=str, default=None,
        help="Hex-encoded compiled script CBOR (alternative to --blueprint)",
    )
    parser.add_argument(
        "--script-address", type=str, required=True,
        help="Bech32 surrender script address",
    )
    parser.add_argument(
        "--cmatra-policy", type=str, required=True,
        help="cMATRA policy ID (hex)",
    )
    parser.add_argument(
        "--cmatra-asset-hex", type=str, required=True,
        help="cMATRA asset name (hex)",
    )
    parser.add_argument(
        "--quarantine-address", type=str, required=True,
        help="Bech32 address for quarantined/burned legacy assets",
    )
    parser.add_argument(
        "--out-dir", type=str, default=None,
        help="Output directory for CBOR files and report",
    )
    parser.add_argument(
        "--submit", action="store_true", default=False,
        help="Submit transactions to the chain",
    )
    parser.add_argument(
        "--preflight", action="store_true", default=False,
        help="Run evaluate_tx preflight before submitting",
    )
    parser.add_argument(
        "--check-only", action="store_true", default=False,
        help="Compute redemptions and report without building transactions",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    # Load rate table
    rate_table = load_rate_table(Path(args.rate_table))
    logger.info(
        "Loaded rate table with %d token(s): %s",
        len(rate_table.get("tokens", {})),
        sorted(rate_table.get("tokens", {}).keys()),
    )

    # Gather surrender requests
    surrender_requests: list[dict[str, Any]] = []

    if args.requests_json:
        with open(args.requests_json) as f:
            loaded = json.load(f)
        if isinstance(loaded, list):
            surrender_requests.extend(loaded)
        else:
            raise ValueError(
                f"--requests-json must contain a JSON array, "
                f"got {type(loaded).__name__}"
            )

    for user_addr, asset, qty_str in args.request:
        surrender_requests.append({
            "user_address": user_addr,
            "asset_name": asset,
            "quantity_base": int(qty_str),
        })

    if not surrender_requests:
        logger.error(
            "No surrender requests provided. "
            "Use --requests-json or --request."
        )
        sys.exit(1)

    logger.info("Processing %d surrender request(s)...", len(surrender_requests))

    # Check-only mode: compute and report redemptions without building txs
    if args.check_only:
        check_results = []
        total_cmatra = 0
        for i, req in enumerate(surrender_requests):
            try:
                cmatra = compute_redemption(
                    rate_table, req["asset_name"], req["quantity_base"],
                )
                check_results.append({
                    "index": i,
                    "user_address": req["user_address"],
                    "asset_name": req["asset_name"],
                    "quantity_base": req["quantity_base"],
                    "cmatra_base": cmatra,
                    "cmatra_display": cmatra / (10 ** FLUX_DECIMALS),
                    "status": "ok",
                })
                total_cmatra += cmatra
            except (KeyError, ValueError) as e:
                check_results.append({
                    "index": i,
                    "user_address": req["user_address"],
                    "asset_name": req["asset_name"],
                    "quantity_base": req["quantity_base"],
                    "status": "error",
                    "error": str(e),
                })

        report = {
            "mode": "check_only",
            "total_requests": len(surrender_requests),
            "total_cmatra_base": total_cmatra,
            "total_cmatra_display": total_cmatra / (10 ** FLUX_DECIMALS),
            "public_pool_base": rate_table.get("public_pool_base", PUBLIC_POOL_BASE),
            "results": check_results,
        }
        print(json.dumps(report, indent=2))
        return

    # Load script CBOR
    if args.script_cbor:
        script_cbor_hex = args.script_cbor
    elif args.blueprint:
        script_cbor_hex = load_script_from_blueprint(args.blueprint)
        logger.info(
            "Loaded script from blueprint: %s (%d bytes)",
            args.blueprint,
            len(script_cbor_hex) // 2,
        )
    else:
        logger.error("Either --blueprint or --script-cbor is required.")
        sys.exit(1)

    # Process batch
    bf = BlockfrostClient()
    report = process_surrender_batch(
        bf=bf,
        admin_skey_path=args.admin_skey,
        rate_table=rate_table,
        surrender_requests=surrender_requests,
        script_address=args.script_address,
        script_cbor_hex=script_cbor_hex,
        cmatra_policy_hex=args.cmatra_policy,
        cmatra_asset_hex=args.cmatra_asset_hex,
        quarantine_address=args.quarantine_address,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        submit=args.submit,
        preflight=args.preflight,
    )

    # Summary
    logger.info(
        "Batch complete: %d/%d succeeded, %d cMATRA distributed (%.2f display)",
        report["total_succeeded"],
        report["total_requests"],
        report["total_cmatra_distributed_base"],
        report["total_cmatra_distributed_display"],
    )

    if report["total_failed"] > 0:
        logger.warning(
            "%d request(s) failed — see report for details.",
            report["total_failed"],
        )


if __name__ == "__main__":
    main()
