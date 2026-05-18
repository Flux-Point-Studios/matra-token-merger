#!/usr/bin/env bash
# ====================================================================
# cMATRA Post-Deadline AdminWithdraw Sweep Ceremony  (task-332)
# ====================================================================
#
# Spends the cMATRA surrender-pool UTxO via the `AdminWithdraw` redeemer
# AFTER the on-chain deadline (2026-11-29 00:00:00 UTC). Both admins must
# co-sign. The pool is one UTxO carrying remaining cMATRA + min-ADA — we
# sweep its full contents (less fee) to a configurable destination.
#
# This script does NOT auto-submit. The submit stage is gated behind an
# interactive type-the-word confirmation, and the --dry-run flag below
# skips that stage entirely.
#
# Usage:
#
#   ./admin-sweep-ceremony.sh apply        # stage 0 — apply spend-script params
#   ./admin-sweep-ceremony.sh params       # stage 1 — fetch protocol params (Koios)
#   ./admin-sweep-ceremony.sh preflight    # stage 2 — discover live pool UTxO + sanity
#   ./admin-sweep-ceremony.sh build        # stage 3 — build raw sweep tx
#   ./admin-sweep-ceremony.sh sign-1       # stage 4 — sign with admin_1 on Gemtek
#   ./admin-sweep-ceremony.sh sign-2       # stage 5 — generate admin_2 witness via SSH
#   ./admin-sweep-ceremony.sh assemble     # stage 6 — combine witnesses
#   ./admin-sweep-ceremony.sh submit       # stage 7 — broadcast (IRREVERSIBLE)
#
#   ./admin-sweep-ceremony.sh dry-run      # convenience — runs apply..build all at once
#                                          # with --dry-run gating so no key access happens
#
# Flags (parsed before the stage name):
#
#   --to <addr>      Override the sweep destination (default: admin_1 reserve)
#   --fee-input <utxo>  txhash#idx of a fee-input UTxO at admin_1 (auto-discovered
#                    by stage_preflight if omitted). Required if the pool UTxO has
#                    insufficient ADA to cover the fee.
#   --dry-run        Run apply/params/preflight/build only; refuse sign/submit
#   --out-dir <dir>  Override $OUT_DIR (default: /home/deci/cmatra-sweep-ceremony)
#   --snapshot       In dry-run, use the pinned 9a68849f...#0 instead of live Koios.
#
# Required local tools: cardano-cli (v11+), aiken (v1.1.21+), jq, curl,
# python3 with `cbor2` + `requests` installed (`pip install -e .[dev]` works).
# ====================================================================

set -euo pipefail

# ---- Pre-stage arg parsing ----
DRY_RUN=0
USE_SNAPSHOT=0
SWEEP_DEST_ADDR_OVERRIDE=""
FEE_INPUT_OVERRIDE=""
OUT_DIR_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift ;;
    --snapshot)
      USE_SNAPSHOT=1
      shift ;;
    --to)
      [[ $# -ge 2 ]] || { echo "ERROR: --to requires an address" >&2; exit 2; }
      SWEEP_DEST_ADDR_OVERRIDE="$2"
      shift 2 ;;
    --fee-input)
      [[ $# -ge 2 ]] || { echo "ERROR: --fee-input requires <txhash>#<idx>" >&2; exit 2; }
      FEE_INPUT_OVERRIDE="$2"
      shift 2 ;;
    --out-dir)
      [[ $# -ge 2 ]] || { echo "ERROR: --out-dir requires a directory" >&2; exit 2; }
      OUT_DIR_OVERRIDE="$2"
      shift 2 ;;
    -h|--help|help)
      STAGE="help"; break ;;
    *)
      STAGE="$1"
      shift; break ;;
  esac
done
STAGE="${STAGE:-help}"

# ---- Load params ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARAMS_FILE="${SCRIPT_DIR}/sweep_params.env"
[[ -f "$PARAMS_FILE" ]] || { echo "ERROR: missing $PARAMS_FILE" >&2; exit 1; }

# OUT_DIR can be overridden via flag before params.env reads it.
if [[ -n "$OUT_DIR_OVERRIDE" ]]; then
  export OUT_DIR="$OUT_DIR_OVERRIDE"
fi

# shellcheck disable=SC1090
source "$PARAMS_FILE"

# Apply --to override after params load so DEFAULT_SWEEP_DEST_ADDR is fallback.
SWEEP_DEST_ADDR="${SWEEP_DEST_ADDR_OVERRIDE:-$DEFAULT_SWEEP_DEST_ADDR}"

REPO_ROOT="${REPO_ROOT:?REPO_ROOT must be set by sweep_params.env}"
PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
export PYTHONPATH

# ---- Helpers ----

cli() { ~/bin/cardano-cli "$@"; }
aiken_bin() { ~/.aiken/bin/aiken "$@"; }

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_file() {
  [[ -f "$1" ]] || die "missing required file: $1 (run prior stage first)"
}

ensure_out_dir() {
  mkdir -p "$OUT_DIR"
}

py_helper() {
  # Run the sweep_helpers CLI; stdout is JSON, stderr is logs.
  python3 -m tools.sweep_helpers "$@"
}

assert_not_dry_run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    die "stage '$1' is disabled in --dry-run mode (refuses to touch admin keys / submit)"
  fi
}

# ====================================================================
# Stage 0 — Apply claim_validator.surrender_pool spend-script parameters
# ====================================================================
stage_apply() {
  echo "=== Stage 0 — Apply claim_validator.surrender_pool parameters ==="
  echo "  admin_pkh_1:  $ADMIN_PKH_1"
  echo "  admin_pkh_2:  $ADMIN_PKH_2"
  echo "  deadline_ms:  $CLAIM_DEADLINE_POSIX_MS"
  echo

  require_file "$CLAIM_VALIDATOR_BLUEPRINT"
  ensure_out_dir

  local CBOR_TMP="${OUT_DIR}/.params.cbor"
  ( umask 077 && python3 - <<PY > "$CBOR_TMP"
import cbor2
print(cbor2.dumps(bytes.fromhex("${ADMIN_PKH_1}")).hex())
print(cbor2.dumps(bytes.fromhex("${ADMIN_PKH_2}")).hex())
print(cbor2.dumps(${CLAIM_DEADLINE_POSIX_MS}).hex())
PY
  )

  local _cbor_lines
  mapfile -t _cbor_lines < "$CBOR_TMP"
  local A1_CBOR="${_cbor_lines[0]}"
  local A2_CBOR="${_cbor_lines[1]}"
  local DEAD_CBOR="${_cbor_lines[2]}"
  rm -f "$CBOR_TMP"

  echo "  admin_pkh_1 CBOR: $A1_CBOR"
  echo "  admin_pkh_2 CBOR: $A2_CBOR"
  echo "  deadline CBOR:    $DEAD_CBOR"
  echo

  local TMP_A="${OUT_DIR}/.applied-1.json"
  local TMP_B="${OUT_DIR}/.applied-2.json"
  aiken_bin blueprint apply -i "$CLAIM_VALIDATOR_BLUEPRINT" "$A1_CBOR"   -o "$TMP_A"
  aiken_bin blueprint apply -i "$TMP_A"                     "$A2_CBOR"   -o "$TMP_B"
  aiken_bin blueprint apply -i "$TMP_B"                     "$DEAD_CBOR" -o "$APPLIED_SPEND_BLUEPRINT"
  rm -f "$TMP_A" "$TMP_B"

  # Derive script hash for sanity check
  local DERIVED
  DERIVED=$(python3 -c "
import json, hashlib
d = json.load(open('${APPLIED_SPEND_BLUEPRINT}'))
v = next(v for v in d['validators'] if v['title'].endswith('.spend'))
print(hashlib.blake2b(b'\\x03' + bytes.fromhex(v['compiledCode']), digest_size=28).hexdigest())
")
  echo "  applied script hash (derived):  $DERIVED"
  echo "  applied script hash (expected): $SURRENDER_POOL_SCRIPT_HASH"
  if [[ "$DERIVED" != "$SURRENDER_POOL_SCRIPT_HASH" ]]; then
    die "spend-script hash mismatch — refuse to proceed"
  fi
  echo "  OK - match"

  # Emit cardano-cli .plutus envelope (CBOR-wrap the compiledCode bytes)
  python3 - <<PY > "$APPLIED_SPEND_SCRIPT_FILE"
import json, cbor2
d = json.load(open("${APPLIED_SPEND_BLUEPRINT}"))
v = next(v for v in d['validators'] if v['title'].endswith('.spend'))
inner = bytes.fromhex(v['compiledCode'])
wrapped = cbor2.dumps(inner)
envelope = {
    "type": "PlutusScriptV3",
    "description": "applied claim_validator.surrender_pool (cMATRA mainnet)",
    "cborHex": wrapped.hex(),
}
print(json.dumps(envelope, indent=2))
PY
  echo "  wrote $APPLIED_SPEND_SCRIPT_FILE ($(wc -c < "$APPLIED_SPEND_SCRIPT_FILE") bytes)"

  # Emit datum + redeemer JSON via the Python helper
  py_helper emit-cbor \
    --datum-out "$SWEEP_DATUM_FILE" \
    --redeemer-out "$SWEEP_REDEEMER_FILE" \
    >/dev/null

  echo "  wrote $SWEEP_DATUM_FILE (Some(Void) — Constr 121 [])"
  echo "  wrote $SWEEP_REDEEMER_FILE (AdminWithdraw — Constr 122 [])"
  echo
  echo "Stage 0 complete. Inspect $APPLIED_SPEND_BLUEPRINT + $APPLIED_SPEND_SCRIPT_FILE."
}

# ====================================================================
# Stage 1 — Fetch protocol params from Koios
# ====================================================================
stage_params() {
  echo "=== Stage 1 — Fetch protocol parameters from Koios ==="
  ensure_out_dir

  # Reuse the exact transformer from mint-ceremony.sh (Koios snake_case →
  # cardano-cli camelCase). The shape is stable across both ceremonies.
  curl -sS "${NETWORK_KOIOS}/epoch_params" > "${OUT_DIR}/.koios-raw.json"

  python3 - <<PY
import json
raw = json.load(open("${OUT_DIR}/.koios-raw.json"))
data = raw[0] if isinstance(raw, list) else raw

def _i(k, default=0):
    v = data.get(k, default)
    return int(v) if v is not None else default

out = {
    'collateralPercentage': data['collateral_percent'],
    'committeeMaxTermLength': data.get('committee_max_term_length', 146),
    'committeeMinSize': data.get('committee_min_size', 7),
    'costModels': data['cost_models'],
    'dRepActivity': data.get('drep_activity', 20),
    'dRepDeposit': _i('drep_deposit', 500000000),
    'dRepVotingThresholds': {
        'committeeNoConfidence': data.get('dvt_committee_no_confidence', 0.6),
        'committeeNormal':       data.get('dvt_committee_normal', 0.67),
        'hardForkInitiation':    data.get('dvt_hard_fork_initiation', 0.6),
        'motionNoConfidence':    data.get('dvt_motion_no_confidence', 0.67),
        'ppEconomicGroup':       data.get('dvt_p_p_economic_group', 0.67),
        'ppGovGroup':            data.get('dvt_p_p_gov_group', 0.75),
        'ppNetworkGroup':        data.get('dvt_p_p_network_group', 0.67),
        'ppTechnicalGroup':      data.get('dvt_p_p_technical_group', 0.67),
        'treasuryWithdrawal':    data.get('dvt_treasury_withdrawal', 0.67),
        'updateToConstitution':  data.get('dvt_update_to_constitution', 0.75),
    },
    'executionUnitPrices': {
        'priceMemory': data['price_mem'],
        'priceSteps':  data['price_step'],
    },
    'govActionDeposit': _i('gov_action_deposit', 100000000000),
    'govActionLifetime': data.get('gov_action_lifetime', 6),
    'maxBlockBodySize': data['max_block_size'],
    'maxBlockExecutionUnits': {
        'memory': data['max_block_ex_mem'],
        'steps':  data['max_block_ex_steps'],
    },
    'maxBlockHeaderSize': data['max_bh_size'],
    'maxCollateralInputs': data['max_collateral_inputs'],
    'maxTxExecutionUnits': {
        'memory': data['max_tx_ex_mem'],
        'steps':  data['max_tx_ex_steps'],
    },
    'maxTxSize': data['max_tx_size'],
    'maxValueSize': data['max_val_size'],
    'minFeeRefScriptCostPerByte': data.get('min_fee_ref_script_cost_per_byte', 15),
    'minPoolCost': _i('min_pool_cost', 170000000),
    'monetaryExpansion': data.get('monetary_expand_rate', data.get('expansion_rate', 0.003)),
    'poolPledgeInfluence': data['influence'],
    'poolRetireMaxEpoch': data['max_epoch'],
    'poolVotingThresholds': {
        'committeeNoConfidence': data.get('pvt_committee_no_confidence', 0.51),
        'committeeNormal':       data.get('pvt_committee_normal', 0.51),
        'hardForkInitiation':    data.get('pvt_hard_fork_initiation', 0.51),
        'motionNoConfidence':    data.get('pvt_motion_no_confidence', 0.51),
        'ppSecurityGroup':       data.get('pvtpp_security_group', 0.51),
    },
    'protocolVersion': {
        'major': data['protocol_major'],
        'minor': data['protocol_minor'],
    },
    'stakeAddressDeposit': _i('key_deposit', 2000000),
    'stakePoolDeposit':    _i('pool_deposit', 500000000),
    'stakePoolTargetNum':  data['optimal_pool_count'],
    'treasuryCut': data['treasury_growth_rate'],
    'txFeeFixed':   data['min_fee_b'],
    'txFeePerByte': data['min_fee_a'],
    'utxoCostPerByte': _i('coins_per_utxo_size', 4310),
}
json.dump(out, open('${PROTOCOL_PARAMS_FILE}', 'w'), indent=2)
print(f'wrote ${PROTOCOL_PARAMS_FILE} ({len(json.dumps(out))} bytes)')
print(f'  protocol version: {out["protocolVersion"]["major"]}.{out["protocolVersion"]["minor"]}  era={data.get("era","?")}  epoch={data.get("epoch_no","?")}')
print(f'  tx fee:   {out["txFeeFixed"]} fixed + {out["txFeePerByte"]}/byte')
print(f'  max tx ex-units: mem={out["maxTxExecutionUnits"]["memory"]:,}, cpu={out["maxTxExecutionUnits"]["steps"]:,}')
print(f'  our ex-units:    mem=${EX_UNITS_MEM}, cpu=${EX_UNITS_CPU}  (must be <= max)')
PY
  rm -f "${OUT_DIR}/.koios-raw.json"

  # Sanity-check ex-units vs max with 50% headroom.
  py_helper validate-ex-units \
    --pparams "$PROTOCOL_PARAMS_FILE" \
    --mem "$EX_UNITS_MEM" \
    --cpu "$EX_UNITS_CPU" \
    --headroom 0.5 \
    >/dev/null
  echo "  OK - ex-units within 50% headroom of per-tx max"

  echo
  echo "Stage 1 complete. Inspect $PROTOCOL_PARAMS_FILE."
}

# ====================================================================
# Stage 2 — Preflight: discover the live pool UTxO + a fee input
# ====================================================================
stage_preflight() {
  echo "=== Stage 2 — Preflight: discover live pool UTxO ==="
  ensure_out_dir

  # Snapshot mode (dry-run only): use the pinned mint-time UTxO so the
  # script can be exercised today without depending on live state.
  if [[ "$USE_SNAPSHOT" == "1" ]]; then
    [[ "$DRY_RUN" == "1" ]] || die "--snapshot requires --dry-run"
    echo "  --snapshot mode: using pinned pool UTxO ${SNAPSHOT_POOL_TX_HASH}#${SNAPSHOT_POOL_TX_INDEX}"
    # Still call live Koios for the actual current balance — that data is
    # also pinned through the mint tx anyway, so it'll match if no claims
    # have happened.
  fi

  # Fetch live UTxO (or current snapshot of the pinned one — Koios resolves
  # the same UTxO ref either way if still unspent).
  py_helper fetch-pool \
    --koios "$NETWORK_KOIOS" \
    --addr "$SURRENDER_POOL_ADDR" \
    --cmatra-unit "$CMATRA_UNIT" \
    > "$POOL_UTXO_FILE"

  python3 - <<PY
import json
u = json.load(open("$POOL_UTXO_FILE"))
print(f"  pool UTxO:        {u['tx_hash']}#{u['tx_index']}")
print(f"  pool ADA:         {u['ada_lovelace']:,} lovelace ({u['ada_lovelace']/1_000_000:.6f} ADA)")
print(f"  pool cMATRA base: {u['cmatra_units']:,} ({u['cmatra_units']/1_000_000:,.0f} cMATRA)")
print(f"  datum_hash:       {u['datum_hash']}")
print(f"  inline_datum:     {u['inline_datum']!r}")
PY

  # If the pinned snapshot is being used in dry-run, verify the UTxO matches.
  if [[ "$USE_SNAPSHOT" == "1" ]]; then
    python3 - <<PY
import json, sys
u = json.load(open("$POOL_UTXO_FILE"))
expected_tx = "$SNAPSHOT_POOL_TX_HASH"
expected_idx = int("$SNAPSHOT_POOL_TX_INDEX")
if u['tx_hash'] != expected_tx or u['tx_index'] != expected_idx:
    print(f"  WARN: live UTxO {u['tx_hash']}#{u['tx_index']} != snapshot {expected_tx}#{expected_idx}", file=sys.stderr)
    print(f"  (this is expected if claims happened; sweep will still work)", file=sys.stderr)
else:
    print(f"  OK - live UTxO == snapshot")
PY
  fi

  echo
  echo "Stage 2 complete."
}

# ====================================================================
# Stage 3 — Build the raw sweep tx
# ====================================================================
stage_build() {
  echo "=== Stage 3 — Build raw sweep tx ==="
  require_file "$APPLIED_SPEND_SCRIPT_FILE"
  require_file "$SWEEP_DATUM_FILE"
  require_file "$SWEEP_REDEEMER_FILE"
  require_file "$PROTOCOL_PARAMS_FILE"
  require_file "$POOL_UTXO_FILE"

  # ---- Derive validity window (one python invocation, three values) ----
  local INVALID_BEFORE INVALID_AFTER DEADLINE_SLOT
  read -r INVALID_BEFORE INVALID_AFTER DEADLINE_SLOT < <(py_helper derive-validity \
    --deadline-ms "$CLAIM_DEADLINE_POSIX_MS" \
    --buffer-slots "$DEADLINE_BUFFER_SLOTS" \
    --duration-hours "$TX_VALIDITY_HOURS" \
    --network "$NETWORK_NAME" \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d['invalid_before'], d['invalid_hereafter'], d['deadline_slot'])
")
  echo "  deadline slot:     $DEADLINE_SLOT"
  echo "  validity range:    [$INVALID_BEFORE, $INVALID_AFTER)  ($TX_VALIDITY_HOURS h)"

  # ---- Read pool UTxO (single python invocation, three values) ----
  local POOL_TXIN POOL_ADA POOL_CMATRA
  read -r POOL_TXIN POOL_ADA POOL_CMATRA < <(python3 -c "
import json
u = json.load(open('$POOL_UTXO_FILE'))
print(f\"{u['tx_hash']}#{u['tx_index']} {u['ada_lovelace']} {u['cmatra_units']}\")
")
  echo "  pool UTxO:         $POOL_TXIN"
  echo "  pool ADA:          $POOL_ADA"
  echo "  pool cMATRA base:  $POOL_CMATRA"

  # ---- Pick a fee input + collateral ----
  # We always need at least one non-script input for fees + collateral. Either
  # the user provides one with --fee-input, or we discover one at admin_1.
  local FEE_INPUT="$FEE_INPUT_OVERRIDE"
  if [[ -z "$FEE_INPUT" ]]; then
    echo "  no --fee-input given; auto-discovering at admin_1 ($ADMIN_1_ADDR)"
    local UTXO_QUERY_FILE="${OUT_DIR}/.admin1-utxos.json"
    curl -sS -X POST "${NETWORK_KOIOS}/address_utxos" \
      -H 'Content-Type: application/json' \
      -d "{\"_addresses\":[\"$ADMIN_1_ADDR\"],\"_extended\":false}" \
      > "$UTXO_QUERY_FILE"
    FEE_INPUT=$(python3 - <<PY
import json
utxos = json.load(open("$UTXO_QUERY_FILE"))
# pick the largest pure-ADA UTxO (no assets) — least likely to entangle assets
pure = [u for u in utxos if not u.get('asset_list')]
pure.sort(key=lambda u: int(u['value']), reverse=True)
if not pure:
    raise SystemExit("no pure-ADA UTxOs at admin_1 — cannot auto-discover fee input")
u = pure[0]
print(f"{u['tx_hash']}#{u['tx_index']}")
PY
    )
    rm -f "$UTXO_QUERY_FILE"
    echo "  auto-picked fee input: $FEE_INPUT"
  fi

  # ---- Compute change (single fee-pass build-raw, then min-fee, then rebuild) ----
  # We carry the entire pool ADA + cMATRA into the destination output and
  # subtract the tx fee from the fee-input's ADA. This keeps the pool
  # UTxO's min-ADA intact in the dest output.
  #
  # Inputs:  POOL_TXIN (script)  +  FEE_INPUT (admin_1)
  # Outputs: DEST  +  CHANGE-to-admin_1
  #
  # We need to fetch FEE_INPUT's value to compute change. Query Koios.
  local FEE_IN_TXHASH FEE_IN_IDX FEE_IN_ADA
  FEE_IN_TXHASH="${FEE_INPUT%%#*}"
  FEE_IN_IDX="${FEE_INPUT##*#}"
  local FEE_UTXO_FILE="${OUT_DIR}/.fee-utxo.json"
  curl -sS -X POST "${NETWORK_KOIOS}/utxo_info" \
    -H 'Content-Type: application/json' \
    -d "{\"_utxo_refs\":[\"${FEE_IN_TXHASH}#${FEE_IN_IDX}\"],\"_extended\":true}" \
    > "$FEE_UTXO_FILE"
  FEE_IN_ADA=$(python3 -c "
import json, sys
d = json.load(open('$FEE_UTXO_FILE'))
if not d: raise SystemExit('fee input not found on chain: $FEE_INPUT')
u = d[0]
if u.get('asset_list'):
    raise SystemExit(f'fee input $FEE_INPUT carries assets — refuse to use')
print(u['value'])
")
  rm -f "$FEE_UTXO_FILE"
  echo "  fee input ADA:     $FEE_IN_ADA"

  # min-utxo for the dest output (asset bundle = 1 cMATRA token, fits in 2 ADA)
  local DEST_OUT_ADA="$DEST_OUTPUT_ADA"
  # The dest output carries POOL_ADA (= the pool's min-ADA) + the cMATRA bundle.
  # If POOL_ADA is < DEST_OUT_ADA, top up. (Pool was minted with 2 ADA, so equal.)
  if (( POOL_ADA < DEST_OUT_ADA )); then
    die "pool ADA $POOL_ADA < dest min-utxo $DEST_OUT_ADA (refuse to build undercollateralized output)"
  fi
  DEST_OUT_ADA="$POOL_ADA"

  # ---- First pass: placeholder fee ----
  local PLACEHOLDER_FEE=500000
  local CHANGE_PLACEHOLDER=$(( FEE_IN_ADA - PLACEHOLDER_FEE ))
  if (( CHANGE_PLACEHOLDER < 1000000 )); then
    die "fee input $FEE_INPUT has insufficient ADA ($FEE_IN_ADA) to cover placeholder fee + 1 ADA change"
  fi

  _build_raw() {
    local fee="$1"
    local change="$2"
    cli latest transaction build-raw \
      --tx-in "$POOL_TXIN" \
      --tx-in-script-file "$APPLIED_SPEND_SCRIPT_FILE" \
      --tx-in-datum-file "$SWEEP_DATUM_FILE" \
      --tx-in-redeemer-file "$SWEEP_REDEEMER_FILE" \
      --tx-in-execution-units "(${EX_UNITS_CPU}, ${EX_UNITS_MEM})" \
      --tx-in "$FEE_INPUT" \
      --tx-in-collateral "$FEE_INPUT" \
      --tx-out "${SWEEP_DEST_ADDR}+${DEST_OUT_ADA}+${POOL_CMATRA} ${CMATRA_UNIT}" \
      --tx-out "${ADMIN_1_ADDR}+${change}" \
      --required-signer-hash "$ADMIN_PKH_1" \
      --required-signer-hash "$ADMIN_PKH_2" \
      --invalid-before "$INVALID_BEFORE" \
      --invalid-hereafter "$INVALID_AFTER" \
      --fee "$fee" \
      --protocol-params-file "$PROTOCOL_PARAMS_FILE" \
      --out-file "$TX_RAW"
  }

  echo "  first pass: fee=$PLACEHOLDER_FEE, change=$CHANGE_PLACEHOLDER"
  _build_raw "$PLACEHOLDER_FEE" "$CHANGE_PLACEHOLDER"

  # ---- Compute actual min-fee ----
  local MIN_FEE_LINE MIN_FEE
  MIN_FEE_LINE=$(cli latest transaction calculate-min-fee \
    --tx-body-file "$TX_RAW" \
    --tx-in-count 2 \
    --tx-out-count 2 \
    --witness-count 2 \
    --byron-witness-count 0 \
    --protocol-params-file "$PROTOCOL_PARAMS_FILE" 2>&1)
  MIN_FEE=$(echo "$MIN_FEE_LINE" | grep -oE '[0-9]+' | head -1)
  [[ -n "$MIN_FEE" ]] || die "could not parse min-fee from: $MIN_FEE_LINE"
  echo "  computed min-fee: $MIN_FEE lovelace"

  # Plutus-script ref-byte fee is not always reflected by calculate-min-fee
  # for inline scripts — keep a comfortable buffer.
  local FEE_BUFFER=200000
  local FINAL_FEE=$(( MIN_FEE + FEE_BUFFER ))
  local FINAL_CHANGE=$(( FEE_IN_ADA - FINAL_FEE ))
  if (( FINAL_CHANGE < 1000000 )); then
    die "after-fee change ($FINAL_CHANGE) below 1 ADA — pick a larger fee input"
  fi
  echo "  applying fee:     $FINAL_FEE lovelace (with ${FEE_BUFFER} buffer)"
  echo "  change to admin_1: $FINAL_CHANGE lovelace ($(python3 -c "print(f'{$FINAL_CHANGE / 1_000_000:.6f}')") ADA)"

  # ---- Second pass: rebuild with correct fee + change ----
  _build_raw "$FINAL_FEE" "$FINAL_CHANGE"

  # ---- Print summary ----
  echo
  echo "--- tx body summary ---"
  echo "  tx_hash:       $(cli latest transaction txid --tx-body-file "$TX_RAW" --output-text)"
  python3 - <<PY
import json
d = json.load(open("$TX_RAW"))
print(f"  type:          {d['type']}")
print(f"  description:   {d.get('description', '')}")
print(f"  cborHex bytes: {len(d['cborHex']) // 2}")
PY
  echo "  ex-units:      mem=$EX_UNITS_MEM, cpu=$EX_UNITS_CPU"
  echo "  validity:      [$INVALID_BEFORE, $INVALID_AFTER)"
  echo "  dest output:   ${SWEEP_DEST_ADDR}+${DEST_OUT_ADA}+${POOL_CMATRA} cMATRA-base"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo
    echo "  DRY-RUN: tx body built successfully. NOT signing, NOT submitting."
    echo "  Inspect with: cli latest transaction view --tx-body-file $TX_RAW"
  fi

  echo
  echo "Stage 3 complete."
}

# ====================================================================
# Stage 4 — Sign with admin_1 on Gemtek
# ====================================================================
stage_sign1() {
  echo "=== Stage 4 — Sign with admin_1 (Gemtek-local) ==="
  assert_not_dry_run "sign-1"
  require_file "$TX_RAW"
  [[ -f "$ADMIN_1_SKEY" ]] || die "admin_1.skey missing at $ADMIN_1_SKEY"
  local perms
  perms=$(stat -c %a "$ADMIN_1_SKEY")
  [[ "$perms" = "400" ]] || die "admin_1.skey mode $perms != 400"

  cli latest transaction sign \
    --tx-body-file "$TX_RAW" \
    --signing-key-file "$ADMIN_1_SKEY" \
    $NETWORK_MAGIC \
    --out-file "$TX_ADMIN1_SIGNED"

  echo "  wrote $TX_ADMIN1_SIGNED"
  echo "  Inspect via: cardano-cli latest transaction view --tx-file $TX_ADMIN1_SIGNED"
  echo "Stage 4 complete."
}

# ====================================================================
# Stage 5 — Sign with admin_2 on Node-3 (via SSH base64-stream pattern,
#           mirrored from mint-ceremony.sh — single SSH session, no race)
# ====================================================================
stage_sign2() {
  echo "=== Stage 5 — Generate admin_2 witness on Node-3 ==="
  assert_not_dry_run "sign-2"
  require_file "$TX_RAW"

  scp -q "$TX_RAW" "${ADMIN_2_HOST}:/tmp/sweep-tx.raw"

  local remote_out
  remote_out=$(ssh "$ADMIN_2_HOST" 'bash -s -- "$@"' \
      "$ADMIN_2_SKEY_REMOTE" "$NETWORK_MAGIC" <<'REMOTE'
set -euo pipefail
SKEY="$1"
NETMAGIC="$2"

cleanup() {
  for f in /tmp/sweep-tx.raw /tmp/sweep-tx.admin_2.witness; do
    [ -f "$f" ] || continue
    if command -v shred >/dev/null 2>&1; then
      shred -u "$f" 2>/dev/null || rm -f "$f"
    else
      rm -f "$f"
    fi
  done
}
trap cleanup EXIT

[ -f "$SKEY" ] || { echo "ERROR: admin_2.skey missing at $SKEY" >&2; exit 1; }
perms=$(stat -c %a "$SKEY")
[ "$perms" = "400" ] || { echo "ERROR: admin_2.skey mode $perms != 400" >&2; exit 1; }

~/bin/cardano-cli latest transaction witness \
  --tx-body-file /tmp/sweep-tx.raw \
  --signing-key-file "$SKEY" \
  $NETMAGIC \
  --out-file /tmp/sweep-tx.admin_2.witness 2>&1 >&2

echo "=====WITNESS-BEGIN====="
base64 -w0 /tmp/sweep-tx.admin_2.witness
echo
echo "=====WITNESS-END====="
echo "  Node-3 witness produced" >&2
REMOTE
  )

  local b64
  b64=$(echo "$remote_out" | awk '/=====WITNESS-BEGIN=====/{flag=1; next} /=====WITNESS-END=====/{flag=0} flag')
  [[ -n "$b64" ]] || die "admin_2 witness was empty — check Node-3 stderr above"
  echo "$b64" | base64 -d > "$TX_ADMIN2_WITNESS"
  [[ -s "$TX_ADMIN2_WITNESS" ]] || die "decoded witness was empty"
  echo "  wrote $TX_ADMIN2_WITNESS ($(wc -c < "$TX_ADMIN2_WITNESS") bytes)"
  echo "Stage 5 complete."
}

# ====================================================================
# Stage 6 — Assemble final signed tx
# ====================================================================
stage_assemble() {
  echo "=== Stage 6 — Assemble final signed tx ==="
  assert_not_dry_run "assemble"
  require_file "$TX_ADMIN1_SIGNED"
  require_file "$TX_ADMIN2_WITNESS"

  local ADMIN1_WITNESS="${OUT_DIR}/sweep-tx.admin_1.witness"
  cli latest transaction witness \
    --tx-body-file "$TX_RAW" \
    --signing-key-file "$ADMIN_1_SKEY" \
    $NETWORK_MAGIC \
    --out-file "$ADMIN1_WITNESS"

  cli latest transaction assemble \
    --tx-body-file "$TX_RAW" \
    --witness-file "$ADMIN1_WITNESS" \
    --witness-file "$TX_ADMIN2_WITNESS" \
    --out-file "$TX_FINAL"

  echo "  wrote $TX_FINAL"
  cli latest transaction view --tx-file "$TX_FINAL" 2>&1 | head -60

  local TX_HASH
  TX_HASH=$(cli latest transaction txid --tx-file "$TX_FINAL" --output-text)
  echo
  echo "  tx_hash:    $TX_HASH"
  echo "  policy_id:  $CMATRA_POLICY_ID"
  echo "  dest:       $SWEEP_DEST_ADDR"
  echo "Stage 6 complete. NOTHING IS ON CHAIN YET. Run 'submit' to broadcast."
}

# ====================================================================
# Stage 7 — Submit
# ====================================================================
stage_submit() {
  echo "=== Stage 7 — Submit signed sweep tx to Cardano mainnet via Koios ==="
  assert_not_dry_run "submit"
  require_file "$TX_FINAL"

  local CBOR_HEX
  CBOR_HEX=$(python3 -c "import json; d=json.load(open('$TX_FINAL')); print(d['cborHex'])")
  local CBOR_BYTES_FILE="${OUT_DIR}/sweep-tx.cbor"
  python3 -c "import sys; sys.stdout.buffer.write(bytes.fromhex('$CBOR_HEX'))" > "$CBOR_BYTES_FILE"
  echo "  CBOR size: $(stat -c %s "$CBOR_BYTES_FILE") bytes"

  echo
  echo "  THIS WILL SUBMIT THE cMATRA SWEEP TX TO CARDANO MAINNET."
  echo "  After acceptance, the surrender pool is EMPTIED and the sweep is IRREVERSIBLE."
  echo "  Type the word 'sweep' to proceed, anything else to abort:"
  read -r CONFIRM
  [[ "$CONFIRM" = "sweep" ]] || die "aborted"

  curl -sS -X POST "${NETWORK_KOIOS}/submittx" \
    -H 'Content-Type: application/cbor' \
    --data-binary "@${CBOR_BYTES_FILE}" \
    --write-out '\nHTTP %{http_code}\n'

  echo
  echo "  After Koios accepts, watch confirmation via:"
  echo "    $NETWORK_KOIOS/tx_info  with [\"<tx_hash>\"]"
}

# ====================================================================
# Convenience dispatcher — dry-run runs apply..build in one go
# ====================================================================
stage_dry_run() {
  [[ "$DRY_RUN" == "1" ]] || die "stage 'dry-run' requires --dry-run flag"
  stage_apply
  echo; stage_params
  echo; stage_preflight
  echo; stage_build
  echo
  echo "=== DRY-RUN complete. Tx body at $TX_RAW. ==="
  echo "    The tx will be rejected by mainnet until slot $((CLAIM_DEADLINE_SLOT_MAINNET + DEADLINE_BUFFER_SLOTS))"
  echo "    (POSIX $(python3 -c "print((${CLAIM_DEADLINE_POSIX_MS} // 1000) + ${DEADLINE_BUFFER_SLOTS})") — 2026-11-29 +${DEADLINE_BUFFER_SLOTS}s)."
}

# ====================================================================
# Dispatch
# ====================================================================
case "$STAGE" in
  apply)      stage_apply ;;
  params)     stage_params ;;
  preflight)  stage_preflight ;;
  build)      stage_build ;;
  sign-1)     stage_sign1 ;;
  sign-2)     stage_sign2 ;;
  assemble)   stage_assemble ;;
  submit)     stage_submit ;;
  dry-run)    stage_dry_run ;;
  help|*)
    cat <<EOF
cMATRA Post-Deadline AdminWithdraw Sweep Ceremony — usage:

  ./admin-sweep-ceremony.sh apply         # Stage 0 — apply spend-script params
  ./admin-sweep-ceremony.sh params        # Stage 1 — fetch protocol params (Koios)
  ./admin-sweep-ceremony.sh preflight     # Stage 2 — discover live pool UTxO
  ./admin-sweep-ceremony.sh build         # Stage 3 — build raw sweep tx
  ./admin-sweep-ceremony.sh sign-1        # Stage 4 — sign with admin_1 (Gemtek)
  ./admin-sweep-ceremony.sh sign-2        # Stage 5 — generate admin_2 witness (Node-3 via SSH)
  ./admin-sweep-ceremony.sh assemble      # Stage 6 — combine witnesses
  ./admin-sweep-ceremony.sh submit        # Stage 7 — submit to mainnet (IRREVERSIBLE)

  ./admin-sweep-ceremony.sh --dry-run --snapshot dry-run   # full apply..build, no keys touched

Flags:
  --to <addr>       Override sweep destination (default: admin_1 reserve)
  --fee-input <utxo>  txhash#idx of an admin_1 UTxO to use for fee+collateral
                    (auto-discovered if omitted)
  --dry-run         Refuse to touch admin keys or submit; only build the tx body
  --snapshot        In --dry-run, use the pinned mint-time pool UTxO
  --out-dir <dir>   Override the working directory (default /home/deci/cmatra-sweep-ceremony)

Locked params: see scripts/sweep_params.env. The on-chain deadline
is 2026-11-29 00:00:00 UTC; this script will build a tx but the
network will reject it until that POSIX time.
EOF
    ;;
esac
