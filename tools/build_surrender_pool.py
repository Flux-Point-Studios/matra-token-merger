#!/usr/bin/env python3
"""
tools/build_surrender_pool.py
Build Surrender-Pool UTxOs (Replacement for build_claim_utxos_flux.py)

Locks the entire cMATRA public pool into a small number of large UTxOs at the
surrender-and-redeem script address.  Unlike the old per-claimant model (which
built thousands of individually-datummed UTxOs), this creates `num_utxos`
roughly-equal pool UTxOs each carrying a Void datum (Constr(0, [])).

The surrender validator enforces that holders send legacy tokens and receive
the correct cMATRA proportion from these pool UTxOs.

Used by: _run_build_pool.py (untracked runner), deployment scripts.
Depends on: tools.api_clients.BlockfrostClient, tools.cardano_utils,
            tools.config (PUBLIC_POOL_BASE, FLUX_DECIMALS).
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
    TransactionOutput,
    TransactionBuilder,
    Value,
)

from tools.api_clients import BlockfrostClient
from tools.cardano_utils import estimate_min_ada
from tools.config import PUBLIC_POOL_BASE, FLUX_DECIMALS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datum encoding
# ---------------------------------------------------------------------------


def encode_pool_datum() -> bytes:
    """Encode a Void/Unit datum as CBOR.

    The surrender pool UTxOs carry a trivial datum: Constr(0, []).
    In Plutus CBOR encoding this is CBORTag(121, []).

    Returns:
        Raw CBOR bytes representing the Void datum.
    """
    import cbor2
    from cbor2 import CBORTag

    # Constr(0, []) -> CBOR tag 121 with an empty array
    return cbor2.dumps(CBORTag(121, []))


# ---------------------------------------------------------------------------
# Pool output construction
# ---------------------------------------------------------------------------


def build_pool_outputs(
    total_cmatra_base: int,
    script_address: str,
    cmatra_policy_hex: str,
    cmatra_asset_hex: str,
    num_utxos: int = 10,
) -> list[dict[str, Any]]:
    """Split the total cMATRA across *num_utxos* pool UTxOs.

    Each UTxO receives roughly equal cMATRA.  The last UTxO absorbs the
    remainder so the exact sum is preserved (no dust loss).

    Args:
        total_cmatra_base: Total cMATRA base units to lock (e.g. PUBLIC_POOL_BASE).
        script_address:    Bech32 address of the surrender-pool script.
        cmatra_policy_hex: 56-char hex policy ID for the cMATRA token.
        cmatra_asset_hex:  Hex-encoded asset name for cMATRA.
        num_utxos:         Number of pool UTxOs to create (default 10).

    Returns:
        List of output descriptor dicts, one per pool UTxO.
    """
    if num_utxos < 1:
        raise ValueError(f"num_utxos must be >= 1, got {num_utxos}")
    if total_cmatra_base <= 0:
        raise ValueError(f"total_cmatra_base must be > 0, got {total_cmatra_base}")

    datum_cbor = encode_pool_datum()
    datum_cbor_hex = datum_cbor.hex()

    # Floor division for the first (num_utxos - 1), remainder goes to last
    per_utxo = total_cmatra_base // num_utxos
    if per_utxo <= 0:
        raise ValueError(
            f"total_cmatra_base ({total_cmatra_base}) too small for "
            f"{num_utxos} UTxOs"
        )

    # Estimate min ADA: 1 native asset + datum size
    min_ada = estimate_min_ada(num_assets=1, datum_size_bytes=len(datum_cbor))

    outputs: list[dict[str, Any]] = []
    allocated = 0

    for i in range(num_utxos):
        if i < num_utxos - 1:
            qty = per_utxo
        else:
            # Last UTxO gets the remainder for exact sum
            qty = total_cmatra_base - allocated

        outputs.append({
            "pool_utxo_index": i,
            "cmatra_base_units": qty,
            "min_ada_lovelace": min_ada,
            "datum_cbor_hex": datum_cbor_hex,
            "script_address": script_address,
            "cmatra_policy_hex": cmatra_policy_hex,
            "cmatra_asset_hex": cmatra_asset_hex,
        })
        allocated += qty

    # Invariant check: allocated must equal total
    assert allocated == total_cmatra_base, (
        f"Allocation mismatch: {allocated} != {total_cmatra_base}"
    )

    return outputs


# ---------------------------------------------------------------------------
# Transaction builder (per batch)
# ---------------------------------------------------------------------------


def build_pool_tx_cbor(
    bf: BlockfrostClient,
    batch: list[dict[str, Any]],
    funding_address: str,
    funding_skey_path: str,
    script_address: str,
    cmatra_policy_hex: str,
    cmatra_asset_hex: str,
) -> dict[str, Any]:
    """Build, sign, and return a transaction CBOR for one batch of pool outputs.

    Follows the same pattern as build_batch_tx_cbor in build_claim_utxos_flux.py.

    Args:
        bf:                BlockfrostClient instance for chain queries.
        batch:             List of output descriptors from build_pool_outputs().
        funding_address:   Bech32 address that funds the transaction.
        funding_skey_path: Path to the funding address signing key (.skey).
        script_address:    Bech32 surrender-pool script address.
        cmatra_policy_hex: 56-char hex policy ID for cMATRA.
        cmatra_asset_hex:  Hex-encoded asset name for cMATRA.

    Returns:
        Dict with tx_hash, tx_cbor_hex, num_outputs, outputs metadata,
        and the signed_tx object (for preflight evaluation).
    """
    from pycardano import (
        BlockFrostChainContext,
        RawPlutusData,
    )
    from pycardano.hash import ScriptHash as PycScriptHash
    from pycardano import AssetName
    import cbor2

    # Set up chain context
    context = BlockFrostChainContext(
        project_id=bf.project_id,
        base_url=bf.base_url,
    )

    sk = PaymentSigningKey.load(funding_skey_path)
    vk = PaymentVerificationKey.from_signing_key(sk)
    funding_addr = Address.from_primitive(funding_address)

    builder = TransactionBuilder(context)
    builder.add_input_address(funding_addr)

    # Build outputs
    policy_id = PycScriptHash(bytes.fromhex(cmatra_policy_hex))
    asset_name = AssetName(bytes.fromhex(cmatra_asset_hex))
    script_addr = Address.from_primitive(script_address)

    output_details = []
    for out in batch:
        datum_bytes = bytes.fromhex(out["datum_cbor_hex"])
        datum = RawPlutusData(cbor2.loads(datum_bytes))

        multi = MultiAsset()
        multi[policy_id] = {asset_name: out["cmatra_base_units"]}
        value = Value(out["min_ada_lovelace"], multi)

        tx_out = TransactionOutput(script_addr, value, datum=datum)
        builder.add_output(tx_out)

        output_details.append({
            "pool_utxo_index": out["pool_utxo_index"],
            "cmatra_base_units": out["cmatra_base_units"],
            "min_ada_lovelace": out["min_ada_lovelace"],
        })

    # Build and sign
    signed_tx = builder.build_and_sign(
        signing_keys=[sk],
        change_address=funding_addr,
    )

    tx_cbor = signed_tx.to_cbor()
    tx_hash = signed_tx.id.payload.hex()

    return {
        "tx_hash": tx_hash,
        "tx_cbor_hex": tx_cbor.hex() if isinstance(tx_cbor, bytes) else tx_cbor,
        "num_outputs": len(output_details),
        "outputs": output_details,
        "signed_tx": signed_tx,
    }


# ---------------------------------------------------------------------------
# Preflight evaluation
# ---------------------------------------------------------------------------


def preflight_evaluate_tx(
    bf: BlockfrostClient,
    signed_tx,
    batch_label: str = "",
) -> dict[str, Any]:
    """Run evaluate_tx (Blockfrost /utils/txs/evaluate) as preflight check.

    Returns evaluation result dict. Logs warnings on failure.
    """
    from pycardano import BlockFrostChainContext

    context = BlockFrostChainContext(
        project_id=bf.project_id,
        base_url=bf.base_url,
    )

    try:
        tx_cbor = signed_tx.to_cbor()
        cbor_bytes = tx_cbor if isinstance(tx_cbor, bytes) else bytes.fromhex(tx_cbor)
        result = context.evaluate_tx(cbor_bytes)
        logger.info("Preflight %s: evaluation OK -- %s", batch_label, result)
        return {"status": "ok", "result": str(result)}
    except Exception as e:
        logger.warning("Preflight %s: evaluation FAILED -- %s", batch_label, e)
        return {"status": "failed", "error": str(e)[:500]}


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def build_surrender_pool(
    bf: BlockfrostClient,
    total_cmatra_base: int,
    funding_address: str,
    funding_skey_path: str,
    script_address: str,
    cmatra_policy_hex: str,
    cmatra_asset_hex: str,
    num_utxos: int = 10,
    batch_size: int = 5,
    out_dir: Path | None = None,
    submit: bool = False,
    preflight: bool = False,
) -> dict[str, Any]:
    """Build all surrender-pool transactions and optionally submit them.

    Splits *total_cmatra_base* across *num_utxos* pool UTxOs, groups them
    into batches of *batch_size*, builds signed transactions for each batch,
    and writes CBOR files + a manifest to *out_dir*.

    Args:
        bf:                BlockfrostClient instance.
        total_cmatra_base: Total cMATRA base units to lock (e.g. PUBLIC_POOL_BASE).
        funding_address:   Bech32 address that funds the transactions.
        funding_skey_path: Path to the funding signing key (.skey).
        script_address:    Bech32 surrender-pool script address.
        cmatra_policy_hex: 56-char hex policy ID for cMATRA.
        cmatra_asset_hex:  Hex-encoded asset name for cMATRA.
        num_utxos:         Number of pool UTxOs to create (default 10).
        batch_size:        Max outputs per transaction (default 5).
        out_dir:           Directory to write CBOR files and manifest.
        submit:            If True, submit transactions to the chain.
        preflight:         If True, run evaluate_tx before submitting.

    Returns:
        Manifest dict summarising all batches and totals.
    """
    logger.info(
        "Building surrender pool: %s cMATRA base units across %d UTxOs",
        f"{total_cmatra_base:,}", num_utxos,
    )
    logger.info(
        "Display amount: %s %s",
        f"{total_cmatra_base / (10 ** FLUX_DECIMALS):,.0f}",
        "cMATRA",
    )

    # Build all output descriptors
    outputs = build_pool_outputs(
        total_cmatra_base, script_address,
        cmatra_policy_hex, cmatra_asset_hex, num_utxos,
    )

    # Split into batches
    batches: list[list[dict[str, Any]]] = []
    for i in range(0, len(outputs), batch_size):
        batches.append(outputs[i : i + batch_size])

    logger.info(
        "Split %d pool UTxOs into %d batches (max %d outputs each)",
        len(outputs), len(batches), batch_size,
    )

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    manifest_entries: list[dict[str, Any]] = []
    total_cmatra_locked = 0
    total_ada = 0

    for i, batch in enumerate(batches):
        logger.info(
            "Building batch %d/%d (%d outputs)...",
            i + 1, len(batches), len(batch),
        )

        result = build_pool_tx_cbor(
            bf, batch, funding_address, funding_skey_path,
            script_address, cmatra_policy_hex, cmatra_asset_hex,
        )

        # Preflight evaluation
        if preflight and result.get("signed_tx"):
            eval_result = preflight_evaluate_tx(
                bf, result["signed_tx"], f"batch_{i}",
            )
            if eval_result["status"] == "failed":
                logger.error(
                    "Preflight FAILED for batch %d -- aborting. Error: %s",
                    i, eval_result["error"],
                )
                raise RuntimeError(
                    f"Preflight failed for batch {i}: {eval_result['error']}"
                )

        # Save CBOR
        if out_dir:
            cbor_path = out_dir / f"pool_batch_{i:04d}.cbor"
            cbor_bytes = bytes.fromhex(result["tx_cbor_hex"])
            with open(cbor_path, "wb") as f:
                f.write(cbor_bytes)

        # Submit if requested
        if submit:
            cbor_bytes = bytes.fromhex(result["tx_cbor_hex"])
            submitted_hash = bf.submit_tx(cbor_bytes)
            logger.info("Submitted batch %d: %s", i, submitted_hash)

        batch_cmatra = sum(o["cmatra_base_units"] for o in result["outputs"])
        batch_ada = sum(o["min_ada_lovelace"] for o in result["outputs"])
        total_cmatra_locked += batch_cmatra
        total_ada += batch_ada

        manifest_entries.append({
            "batch_index": i,
            "tx_hash": result["tx_hash"],
            "num_outputs": result["num_outputs"],
            "total_cmatra_base_units": batch_cmatra,
            "total_ada_lovelace": batch_ada,
            "pool_utxos": [
                {
                    "pool_utxo_index": o["pool_utxo_index"],
                    "cmatra_base_units": o["cmatra_base_units"],
                }
                for o in result["outputs"]
            ],
        })

    # Final invariant check
    assert total_cmatra_locked == total_cmatra_base, (
        f"Total locked mismatch: {total_cmatra_locked} != {total_cmatra_base}"
    )

    manifest: dict[str, Any] = {
        "manifest_type": "surrender_pool",
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "script_address": script_address,
        "cmatra_policy_hex": cmatra_policy_hex,
        "cmatra_asset_hex": cmatra_asset_hex,
        "datum_type": "Void (Constr(0, []))",
        "totals": {
            "num_batches": len(manifest_entries),
            "num_pool_utxos": num_utxos,
            "total_cmatra_base_units": total_cmatra_locked,
            "total_cmatra_display": total_cmatra_locked / (10 ** FLUX_DECIMALS),
            "total_ada_lovelace": total_ada,
        },
        "batches": manifest_entries,
    }

    if out_dir:
        manifest_path = out_dir / "pool_manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        logger.info("Manifest written to %s", manifest_path)

    logger.info(
        "Surrender pool build complete: %d UTxOs, %s cMATRA, %s ADA",
        num_utxos,
        f"{total_cmatra_locked / (10 ** FLUX_DECIMALS):,.0f}",
        f"{total_ada / 1_000_000:,.2f}",
    )

    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for building the surrender pool."""
    parser = argparse.ArgumentParser(
        description="cMATRA merger -- Build Surrender Pool UTxOs",
    )
    parser.add_argument(
        "--total-cmatra-base", type=int, default=PUBLIC_POOL_BASE,
        help=(
            f"Total cMATRA base units to lock (default: PUBLIC_POOL_BASE = "
            f"{PUBLIC_POOL_BASE:,})"
        ),
    )
    parser.add_argument("--funding-address", type=str, required=True,
                        help="Bech32 address that funds the transactions")
    parser.add_argument("--funding-skey", type=str, required=True,
                        help="Path to the funding address signing key (.skey)")
    parser.add_argument("--script-address", type=str, required=True,
                        help="Bech32 surrender-pool script address")
    parser.add_argument("--cmatra-policy", type=str, required=True,
                        help="56-char hex policy ID for cMATRA")
    parser.add_argument("--cmatra-asset-hex", type=str, required=True,
                        help="Hex-encoded asset name for cMATRA")
    parser.add_argument("--num-utxos", type=int, default=10,
                        help="Number of pool UTxOs to create (default: 10)")
    parser.add_argument("--batch-size", type=int, default=5,
                        help="Max outputs per transaction (default: 5)")
    parser.add_argument("--out-dir", type=str, required=True,
                        help="Directory to write CBOR files and manifest")
    parser.add_argument("--submit", action="store_true", default=False,
                        help="Submit transactions to the chain")
    parser.add_argument("--preflight", action="store_true", default=False,
                        help="Run evaluate_tx preflight check before submitting")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    bf = BlockfrostClient()
    build_surrender_pool(
        bf,
        total_cmatra_base=args.total_cmatra_base,
        funding_address=args.funding_address,
        funding_skey_path=args.funding_skey,
        script_address=args.script_address,
        cmatra_policy_hex=args.cmatra_policy,
        cmatra_asset_hex=args.cmatra_asset_hex,
        num_utxos=args.num_utxos,
        batch_size=args.batch_size,
        out_dir=Path(args.out_dir),
        submit=args.submit,
        preflight=args.preflight,
    )


if __name__ == "__main__":
    main()
