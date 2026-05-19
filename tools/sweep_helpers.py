#!/usr/bin/env python3
"""
Helpers for the post-deadline AdminWithdraw sweep ceremony (task-332).

Used by scripts/admin-sweep-ceremony.sh stages. The bash side handles
cardano-cli orchestration + SSH dual-signing; this module owns:

  * Koios fetches (live pool UTxO + tip slot) — built on the shared
    retry-with-backoff helper from tools.api_clients
  * CBOR-Plutus-Data shaping for the AdminWithdraw redeemer + Some(Void) datum
  * Validity-range slot derivation from the on-chain deadline POSIX-ms
  * Spend-script ex-units sanity checks against current protocol limits

Plutus encodings emitted (Aiken collapses `Some(Void)` and zero-field enum
variants to bare CBOR Constr tags):

  Some(Void)       = CBORTag(121, [])  → D8 79 80     (sweep datum)
  AdminWithdraw    = CBORTag(122, [])  → D8 7A 80     (sweep redeemer)

The datum byte sequence is identical to `tools.build_surrender_pool.encode_pool_datum`;
we re-export it through a sweep-context alias so both call sites read clearly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import cbor2

from tools.api_clients import _request_with_retry
from tools.build_surrender_pool import encode_pool_datum
from tools.cardano_utils import posix_ms_to_slot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shelley genesis epoch (used to translate slot -> POSIX for sanity output)
# ---------------------------------------------------------------------------

MAINNET_SHELLEY_START_SLOT = 4_492_800
MAINNET_SHELLEY_START_TIME = 1_596_491_091  # POSIX seconds
PREPROD_SHELLEY_START_TIME = 1_655_683_200


def slot_to_posix_ms(slot: int, network: str = "mainnet") -> int:
    """Inverse of posix_ms_to_slot. Used for sanity-checking validity windows.

    posix_ms_to_slot is imported from tools.cardano_utils to avoid drift.
    """
    if slot < 0:
        raise ValueError(f"slot must be non-negative, got {slot}")
    if network == "mainnet":
        posix_sec = (slot - MAINNET_SHELLEY_START_SLOT) + MAINNET_SHELLEY_START_TIME
    elif network in ("preprod", "preview"):
        posix_sec = slot + PREPROD_SHELLEY_START_TIME
    else:
        raise ValueError(f"unknown network: {network!r}")
    return posix_sec * 1000


# ---------------------------------------------------------------------------
# Validity-range derivation
# ---------------------------------------------------------------------------

@dataclass
class ValidityWindow:
    invalid_before: int
    invalid_hereafter: int

    def covers_slot(self, slot: int) -> bool:
        """True if *slot* is strictly inside [invalid_before, invalid_hereafter).

        The Aiken validator uses `is_entirely_after(tx.validity_range, deadline)`
        which requires the LOWER bound of the validity range to be strictly
        greater than `deadline` in milliseconds. `invalid_before` is in
        slots; slot-to-ms conversion happens on chain.
        """
        return self.invalid_before <= slot < self.invalid_hereafter


def derive_sweep_validity_window(
    deadline_posix_ms: int,
    buffer_slots: int,
    duration_hours: int,
    network: str = "mainnet",
) -> ValidityWindow:
    """Build the validity range for a post-deadline AdminWithdraw tx.

    invalid_before = deadline_slot + buffer_slots
    invalid_hereafter = invalid_before + duration_hours * 3600

    *buffer_slots* must be >= 1 so that lower_bound is strictly after the
    deadline (Aiken's is_entirely_after is exclusive at the deadline point).
    """
    if buffer_slots < 1:
        raise ValueError(
            f"buffer_slots must be >= 1 to satisfy is_entirely_after; got {buffer_slots}"
        )
    if duration_hours < 1:
        raise ValueError(f"duration_hours must be >= 1, got {duration_hours}")

    deadline_slot = posix_ms_to_slot(deadline_posix_ms, network)
    lower = deadline_slot + buffer_slots
    upper = lower + duration_hours * 3600
    return ValidityWindow(invalid_before=lower, invalid_hereafter=upper)


# ---------------------------------------------------------------------------
# CBOR / Plutus Data shaping
# ---------------------------------------------------------------------------


def encode_some_void_datum() -> bytes:
    """Encode `Some(Void)` as Plutus Data CBOR — `Constr 121 []`.

    Identical bytes to tools.build_surrender_pool.encode_pool_datum (which we
    re-use directly). The Aiken validator's `expect Some(_) = datum` is
    satisfied by any Constr-121 datum; on-chain the same bytes were used at
    pool deposit time, so this also matches existing pool UTxOs.
    """
    return encode_pool_datum()


def encode_admin_withdraw_redeemer() -> bytes:
    """Encode `AdminWithdraw` redeemer — `Constr 122 []` (= CBOR D87A80).

    Aiken numbers enum variants from 0, so the second arm of `SurrenderAction`
    (AdminWithdraw) gets Plutus Data tag 122 (= 121 + 1).
    """
    return cbor2.dumps(cbor2.CBORTag(122, []))


def write_plutus_data_json_constr(path: str, constructor: int) -> None:
    """Write a zero-field constructor as the JSON-Plutus-Data form
    cardano-cli expects.

    cardano-cli accepts datum/redeemer values via `--*-value` flags as JSON
    of the shape `{"constructor": N, "fields": [...]}`. For `Constr 121 []`
    we emit `{"constructor": 0, "fields": []}`; for `Constr 122 []` we emit
    `{"constructor": 1, "fields": []}`. cardano-cli adds the 121 base.
    """
    if constructor < 0:
        raise ValueError(f"constructor index must be >= 0; got {constructor}")
    payload = {"constructor": constructor, "fields": []}
    with open(path, "w") as f:
        json.dump(payload, f)


# ---------------------------------------------------------------------------
# Koios fetches
# ---------------------------------------------------------------------------

def _koios_post(base_url: str, path: str, body: Any, _fetch=None) -> Any:
    """POST to Koios with the shared retry+backoff. `_fetch` is the test seam."""
    if _fetch is not None:
        return _fetch("POST", f"{base_url}{path}", body)
    return _request_with_retry(
        "POST",
        f"{base_url}{path}",
        headers={"Content-Type": "application/json"},
        json_body=body,
    )


def _koios_get(base_url: str, path: str, _fetch=None) -> Any:
    """GET from Koios with the shared retry+backoff. `_fetch` is the test seam."""
    if _fetch is not None:
        return _fetch("GET", f"{base_url}{path}", None)
    return _request_with_retry("GET", f"{base_url}{path}", headers={})


def koios_get_pool_utxo(
    koios_base_url: str,
    pool_address: str,
    cmatra_unit: str,
    *,
    _fetch=None,
) -> dict[str, Any]:
    """Fetch the (single) live UTxO at the surrender-pool script address.

    Returns a normalized dict with keys: tx_hash, tx_index, ada_lovelace,
    cmatra_units, datum_hash, inline_datum, asset_list.

    Raises if zero or more than one UTxO is present at the script address —
    the pool is a one-UTxO invariant; multiple UTxOs require manual review.

    `_fetch(method, url, body) -> json` is the test seam (default goes
    through tools.api_clients._request_with_retry).
    """
    utxos = _koios_post(
        koios_base_url, "/address_utxos",
        {"_addresses": [pool_address], "_extended": True},
        _fetch=_fetch,
    )
    if not isinstance(utxos, list):
        raise ValueError(f"Koios returned non-list: {utxos!r}")
    if len(utxos) == 0:
        raise RuntimeError(
            f"No UTxOs at pool address {pool_address}. "
            "Cannot sweep an empty pool — verify deadline + address."
        )
    if len(utxos) > 1:
        raise RuntimeError(
            f"Expected exactly one UTxO at {pool_address}, found {len(utxos)}. "
            "Manual review required — the pool should be a single UTxO. "
            f"UTxOs: {[(u['tx_hash'], u['tx_index']) for u in utxos]}"
        )
    return _normalize_pool_utxo(utxos[0], cmatra_unit)


def _normalize_pool_utxo(u: dict[str, Any], cmatra_unit: str) -> dict[str, Any]:
    """Translate a Koios address_utxos row into our internal shape."""
    policy_hex, _, asset_hex = cmatra_unit.partition(".")
    if not policy_hex or not asset_hex:
        raise ValueError(f"cmatra_unit must be '<policy>.<asset_hex>': {cmatra_unit!r}")

    ada_lovelace = int(u["value"])
    cmatra_units = 0
    asset_list = u.get("asset_list") or []
    for a in asset_list:
        if a["policy_id"] == policy_hex and a["asset_name"] == asset_hex:
            cmatra_units = int(a["quantity"])

    return {
        "tx_hash": u["tx_hash"],
        "tx_index": int(u["tx_index"]),
        "ada_lovelace": ada_lovelace,
        "cmatra_units": cmatra_units,
        "datum_hash": u.get("datum_hash"),
        "inline_datum": u.get("inline_datum"),
        "asset_list": asset_list,
    }


def koios_get_tip(
    koios_base_url: str,
    *,
    _fetch=None,
) -> dict[str, int]:
    """Return {'abs_slot': int, 'block_time': int} for the current tip."""
    data = _koios_get(koios_base_url, "/tip", _fetch=_fetch)
    row = data[0] if isinstance(data, list) else data
    return {"abs_slot": int(row["abs_slot"]), "block_time": int(row["block_time"])}


# ---------------------------------------------------------------------------
# Ex-units estimation / validation
# ---------------------------------------------------------------------------


def validate_ex_units(
    proposed_mem: int,
    proposed_cpu: int,
    pparams: dict[str, Any],
    *,
    headroom_fraction: float = 0.5,
) -> None:
    """Raise if the proposed ex-units exceed `headroom_fraction` of the
    per-tx limit in the supplied protocol-params dict.

    *headroom_fraction* defaults to 0.5 — we want to be well under the
    max-tx ex-units ceiling so transient cost-model changes don't bump
    us into rejection territory.

    The pparams shape mirrors what mint-ceremony.sh produces (camelCase,
    cardano-cli protocol-params JSON).
    """
    if not (0 < headroom_fraction <= 1):
        raise ValueError(f"headroom_fraction must be in (0, 1]: {headroom_fraction}")
    try:
        max_mem = int(pparams["maxTxExecutionUnits"]["memory"])
        max_cpu = int(pparams["maxTxExecutionUnits"]["steps"])
    except (KeyError, TypeError) as e:
        raise ValueError(
            f"protocol-params is missing maxTxExecutionUnits.memory/steps: {e}"
        ) from e
    if proposed_mem <= 0 or proposed_cpu <= 0:
        raise ValueError(
            f"ex-units must be positive; got mem={proposed_mem}, cpu={proposed_cpu}"
        )

    mem_budget = int(max_mem * headroom_fraction)
    cpu_budget = int(max_cpu * headroom_fraction)
    if proposed_mem > mem_budget:
        raise ValueError(
            f"proposed mem {proposed_mem:,} > {headroom_fraction:.0%} of "
            f"max_tx mem {max_mem:,} (budget {mem_budget:,})"
        )
    if proposed_cpu > cpu_budget:
        raise ValueError(
            f"proposed cpu {proposed_cpu:,} > {headroom_fraction:.0%} of "
            f"max_tx cpu {max_cpu:,} (budget {cpu_budget:,})"
        )


# ---------------------------------------------------------------------------
# Fee preflight — compute how much ADA the sweep can carry to the dest
# ---------------------------------------------------------------------------


def compute_sweep_change(
    pool_ada_lovelace: int,
    fee_lovelace: int,
    dest_output_ada: int,
) -> int:
    """Compute the ADA going to dest_output as: pool_ada - fee.

    The dest output carries the full cMATRA + (pool_ada - fee) lovelace.
    The min-utxo floor is enforced separately (dest_output_ada is the
    pre-flight minimum; if pool_ada - fee falls below that, the script
    must add a fee-input from admin_1 — handled in the bash side).

    Returns the lovelace remaining for the dest output after subtracting fee.
    Raises if the result would be below the supplied min-utxo floor.
    """
    if pool_ada_lovelace < 0 or fee_lovelace < 0 or dest_output_ada < 0:
        raise ValueError("all inputs must be non-negative")
    remaining = pool_ada_lovelace - fee_lovelace
    if remaining < dest_output_ada:
        raise RuntimeError(
            f"pool_ada {pool_ada_lovelace:,} - fee {fee_lovelace:,} "
            f"= {remaining:,} < min-utxo {dest_output_ada:,}. "
            "Must supply a fee-input from admin_1 (see stage_build_raw)."
        )
    return remaining


# ---------------------------------------------------------------------------
# CLI entry point — used by stages that want a single helper invocation
# ---------------------------------------------------------------------------


def _main() -> int:
    """Tiny CLI for stage hooks. Always emits JSON to stdout."""
    import argparse
    import sys

    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_pool = sub.add_parser("fetch-pool", help="fetch live pool UTxO")
    p_pool.add_argument("--koios", required=True)
    p_pool.add_argument("--addr", required=True)
    p_pool.add_argument("--cmatra-unit", required=True)

    p_tip = sub.add_parser("tip", help="fetch tip slot+time")
    p_tip.add_argument("--koios", required=True)

    p_validity = sub.add_parser("derive-validity", help="derive sweep validity window")
    p_validity.add_argument("--deadline-ms", type=int, required=True)
    p_validity.add_argument("--buffer-slots", type=int, required=True)
    p_validity.add_argument("--duration-hours", type=int, required=True)
    p_validity.add_argument("--network", default="mainnet")

    p_emit = sub.add_parser("emit-cbor", help="emit datum+redeemer JSON files")
    p_emit.add_argument("--datum-out", required=True)
    p_emit.add_argument("--redeemer-out", required=True)

    p_validate = sub.add_parser("validate-ex-units", help="check ex-units vs pparams")
    p_validate.add_argument("--pparams", required=True)
    p_validate.add_argument("--mem", type=int, required=True)
    p_validate.add_argument("--cpu", type=int, required=True)
    p_validate.add_argument("--headroom", type=float, default=0.5)

    args = p.parse_args()
    try:
        if args.cmd == "fetch-pool":
            out = koios_get_pool_utxo(args.koios, args.addr, args.cmatra_unit)
        elif args.cmd == "tip":
            out = koios_get_tip(args.koios)
        elif args.cmd == "derive-validity":
            win = derive_sweep_validity_window(
                args.deadline_ms,
                args.buffer_slots,
                args.duration_hours,
                args.network,
            )
            out = {
                "invalid_before": win.invalid_before,
                "invalid_hereafter": win.invalid_hereafter,
                "deadline_slot": posix_ms_to_slot(args.deadline_ms, args.network),
            }
        elif args.cmd == "emit-cbor":
            write_plutus_data_json_constr(args.datum_out, 0)
            write_plutus_data_json_constr(args.redeemer_out, 1)
            out = {
                "datum_file": args.datum_out,
                "datum_cbor_hex": encode_some_void_datum().hex(),
                "redeemer_file": args.redeemer_out,
                "redeemer_cbor_hex": encode_admin_withdraw_redeemer().hex(),
            }
        elif args.cmd == "validate-ex-units":
            with open(args.pparams) as f:
                pp = json.load(f)
            validate_ex_units(args.mem, args.cpu, pp, headroom_fraction=args.headroom)
            out = {"ok": True, "mem": args.mem, "cpu": args.cpu}
        else:
            raise ValueError(f"unknown cmd: {args.cmd}")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout)
        print()
        return 1
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
