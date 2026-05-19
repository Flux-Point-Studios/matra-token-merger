"""Structural test: assert mint-ceremony.sh attaches an inline datum on the
surrender_pool output. This is the regression test for the 2026-05-18
catastrophic mint where the flag was omitted and 722.5M cMATRA were
permanently locked at a Plutus script address that required a datum.

The flux_mint_policy itself now refuses to mint without an inline datum
on cMATRA script-address outputs (see PR #12 invariant I6), but this
ceremony-level check is belt-and-braces: it catches the omission BEFORE
the operator burns a 5-ADA seed UTxO on a tx that would just bounce.
"""
from __future__ import annotations
import re
from pathlib import Path

CEREMONY = Path(__file__).resolve().parent.parent / "scripts" / "mint-ceremony.sh"


def test_mint_ceremony_script_exists():
    assert CEREMONY.exists(), f"missing {CEREMONY}"


def test_surrender_pool_output_has_inline_datum_flag():
    """The --tx-out targeting SURRENDER_POOL_ADDR must be IMMEDIATELY followed
    by --tx-out-inline-datum-value. cardano-cli applies datum flags to the
    most-recent --tx-out, so they must be adjacent."""
    body = CEREMONY.read_text()
    lines = body.splitlines()

    pool_tx_out_idx = None
    for i, line in enumerate(lines):
        if "SURRENDER_POOL_ADDR" in line and "--tx-out " in line:
            pool_tx_out_idx = i
            break
    assert pool_tx_out_idx is not None, (
        "no --tx-out line targeting SURRENDER_POOL_ADDR found"
    )

    # Find the NEXT non-comment, non-blank, non-line-continuation line
    next_line_idx = None
    for j in range(pool_tx_out_idx + 1, min(pool_tx_out_idx + 4, len(lines))):
        stripped = lines[j].strip()
        if stripped and not stripped.startswith("#"):
            next_line_idx = j
            break
    assert next_line_idx is not None, "no line after the pool --tx-out"

    next_line = lines[next_line_idx]
    assert "--tx-out-inline-datum-value" in next_line, (
        f"surrender_pool --tx-out at line {pool_tx_out_idx + 1} is not immediately "
        f"followed by --tx-out-inline-datum-value (got line {next_line_idx + 1}: "
        f"{next_line.strip()!r}). This is the 2026-05-18 catastrophic-mint regression "
        f"shape — DO NOT ship a ceremony with this gap."
    )


def test_datum_value_is_void_constr_zero():
    """The inline datum value must serialize to CBOR `D87980` — i.e.
    Constr(0, []). This matches the claim_validator's `expect Some(Void) =
    datum` requirement (Aiken collapses `Some(Void)` to a bare Constr-121
    on chain). Any other shape would be either rejected by the validator
    OR accepted with semantic mismatch — both are catastrophic.
    """
    body = CEREMONY.read_text()
    # Locate the variable assignment for the inline datum payload
    m = re.search(
        r"""VOID_INLINE_DATUM\s*=\s*['"]({[^}]+})['"]""",
        body,
    )
    assert m is not None, "VOID_INLINE_DATUM variable not found in ceremony script"
    payload = m.group(1).replace(" ", "")
    assert payload == '{"constructor":0,"fields":[]}', (
        f"VOID_INLINE_DATUM payload must be Constr(0, []) — got {payload!r}"
    )
