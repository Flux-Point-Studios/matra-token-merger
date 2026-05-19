#!/usr/bin/env bash
# ====================================================================
# cMATRA Mainnet Mint Ceremony
# ====================================================================
#
# One-shot mint of the entire 1B cMATRA supply, paid into the surrender
# pool address (722.5M) and the admin_1 reserve channel (277.5M), under
# the dual-admin flux_mint_policy validator.
#
# THIS SCRIPT MUST BE REVIEWED BEFORE ANY STAGE IS EXECUTED.
# THIS SCRIPT NEVER AUTO-SUBMITS. THE FINAL SUBMIT IS A SEPARATE STAGE.
#
# Usage:
#
#   ./mint-ceremony.sh apply        # stage 0 — apply mint policy params
#   ./mint-ceremony.sh params       # stage 1 — fetch protocol params (Koios)
#   ./mint-ceremony.sh preflight    # stage 2 — confirm seed + reserve UTxOs still unspent
#   ./mint-ceremony.sh build        # stage 3 — build-raw the mint tx
#   ./mint-ceremony.sh sign-1       # stage 4 — sign with admin_1 on Gemtek
#   ./mint-ceremony.sh sign-2       # stage 5 — generate admin_2 witness via SSH to Node-3
#   ./mint-ceremony.sh assemble     # stage 6 — combine witnesses into final signed tx
#   ./mint-ceremony.sh submit       # stage 7 — submit via Koios (irreversible on confirmation)
#
# Each stage halts on completion. Operator must inspect outputs before
# proceeding to next stage. Stages are idempotent except `submit`.
#
# Stage outputs land in $OUT_DIR (see params.env).
#
# Required local tools: cardano-cli (v11), aiken (v1.1.21), jq, curl, python3.
# Required network: Koios (https://api.koios.rest, no auth needed for these
# endpoints) for protocol params + UTxO existence checks + tx submission.
# ====================================================================

set -euo pipefail

# Load locked params
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/params.env"

# --- Helpers ---

cli() { ~/bin/cardano-cli "$@"; }
aiken_bin() { ~/.aiken/bin/aiken "$@"; }

die() { echo "ERROR: $*" >&2; exit 1; }

require_file() {
  [ -f "$1" ] || die "missing required file: $1 (run prior stage first)"
}

confirm_admin1_skey() {
  [ -f "$ADMIN_1_SKEY" ] || die "admin_1.skey not at $ADMIN_1_SKEY"
  # Check mode is 0400 (owner read-only)
  perms=$(stat -c %a "$ADMIN_1_SKEY")
  [ "$perms" = "400" ] || die "admin_1.skey mode is $perms, expected 400"
}

koios_post() {
  local endpoint="$1"
  local body="$2"
  curl -sS -X POST "${NETWORK_KOIOS}${endpoint}" \
    -H 'Content-Type: application/json' \
    -d "$body"
}

# ====================================================================
# Stage 0 — Apply mint policy parameters
# ====================================================================
stage_apply() {
  echo "=== Stage 0 — Apply flux_mint_policy parameters ==="
  echo "  seed_utxo:    $SEED_UTXO"
  echo "  admin_pkh_1:  $ADMIN_PKH_1"
  echo "  admin_pkh_2:  $ADMIN_PKH_2"
  echo

  require_file "$MINT_POLICY_SRC"
  mkdir -p "$OUT_DIR"

  # Encode each param as Plutus Data CBOR-hex using python3 + cbor2.
  # Write to an owner-only temp file in $OUT_DIR (not /tmp, which is world-
  # readable on multi-user hosts), and clean it up on exit. The values
  # being encoded are public (seed UTxO is on chain; PKHs are public-by-
  # design), so this is defense-in-depth rather than secret protection.
  local CBOR_TMP="${OUT_DIR}/.params.cbor"
  ( umask 077 && python3 - <<PY > "$CBOR_TMP"
import cbor2
seed = cbor2.CBORTag(121, [
    bytes.fromhex("${SEED_UTXO_TXID}"),
    ${SEED_UTXO_IDX},
])
print(cbor2.dumps(seed).hex())
print(cbor2.dumps(bytes.fromhex("${ADMIN_PKH_1}")).hex())
print(cbor2.dumps(bytes.fromhex("${ADMIN_PKH_2}")).hex())
PY
  )
  # Use mapfile (newline-terminated array) — `read -r` with space-separated
  # input returns nonzero at EOF-without-newline and trips set -e.
  local _cbor_lines
  mapfile -t _cbor_lines < "$CBOR_TMP"
  SEED_CBOR="${_cbor_lines[0]}"
  ADMIN_1_CBOR="${_cbor_lines[1]}"
  ADMIN_2_CBOR="${_cbor_lines[2]}"
  rm -f "$CBOR_TMP"

  echo "  seed_utxo Plutus Data CBOR:  $SEED_CBOR"
  echo "  admin_pkh_1 Plutus Data CBOR: $ADMIN_1_CBOR"
  echo "  admin_pkh_2 Plutus Data CBOR: $ADMIN_2_CBOR"
  echo

  # Apply each param in declaration order
  local TMP_A="${OUT_DIR}/.applied-1.json"
  local TMP_B="${OUT_DIR}/.applied-2.json"
  aiken_bin blueprint apply -i "$MINT_POLICY_SRC" "$SEED_CBOR"     -o "$TMP_A"
  aiken_bin blueprint apply -i "$TMP_A"          "$ADMIN_1_CBOR"   -o "$TMP_B"
  aiken_bin blueprint apply -i "$TMP_B"          "$ADMIN_2_CBOR"   -o "$APPLIED_BLUEPRINT"
  rm -f "$TMP_A" "$TMP_B"

  # Derive the applied policy ID — MUST match CMATRA_POLICY_ID.
  # We compute via blake2b-28(0x03 || compiledCode) directly rather than
  # via `aiken blueprint hash`, which insists on finding an `aiken.toml`
  # in cwd and is awkward to invoke against a free-standing blueprint
  # file in this script's working directory.
  local DERIVED
  DERIVED=$(python3 -c "
import json, hashlib
d = json.load(open('${APPLIED_BLUEPRINT}'))
v = next(v for v in d['validators'] if 'mint' in v['title'].lower())
print(hashlib.blake2b(b'\\x03' + bytes.fromhex(v['compiledCode']), digest_size=28).hexdigest())
")

  echo "  applied policy ID (derived):  $DERIVED"
  echo "  applied policy ID (expected): $CMATRA_POLICY_ID"
  if [ "$DERIVED" != "$CMATRA_POLICY_ID" ]; then
    die "policy ID mismatch — refuse to proceed"
  fi
  echo "  ✓ match"

  # Convert applied blueprint into cardano-cli .plutus envelope.
  # The envelope wraps the plutus.json compiledCode (which is already a
  # CBOR bytestring of the raw UPLC) in one more CBOR-bytestring layer.
  # `aiken blueprint convert` would do this but it insists on having an
  # aiken.toml in cwd, which is awkward against a free-standing blueprint.
  python3 - <<PY > "$APPLIED_SCRIPT_FILE"
import json, cbor2
d = json.load(open("${APPLIED_BLUEPRINT}"))
v = next(v for v in d['validators'] if 'mint' in v['title'].lower())
inner = bytes.fromhex(v['compiledCode'])
wrapped = cbor2.dumps(inner)
envelope = {
    "type": "PlutusScriptV3",
    "description": "applied flux_mint_policy (cMATRA mainnet)",
    "cborHex": wrapped.hex(),
}
print(json.dumps(envelope, indent=2))
PY
  echo "  wrote $APPLIED_SCRIPT_FILE ($(wc -c < "$APPLIED_SCRIPT_FILE") bytes)"

  # Mint redeemer is `_redeemer: Data` (validator ignores it).
  # Use unit Data: Constr 0 [].
  echo '{"constructor":0,"fields":[]}' > "$MINT_REDEEMER_FILE"
  echo "  wrote $MINT_REDEEMER_FILE (unit Data)"

  echo
  echo "Stage 0 complete. Inspect $APPLIED_BLUEPRINT + $APPLIED_SCRIPT_FILE before proceeding."
}

# ====================================================================
# Stage 1 — Fetch current protocol params from Koios
# ====================================================================
stage_params() {
  echo "=== Stage 1 — Fetch protocol parameters from Koios ==="
  mkdir -p "$OUT_DIR"

  # Koios v1 endpoint: /epoch_params returns a 1-element array.
  # We translate Koios's snake_case field names into cardano-cli's
  # camelCase protocol-parameters JSON.
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
print(f'  tx fee: {out["txFeeFixed"]} fixed + {out["txFeePerByte"]}/byte')
print(f'  max tx ex-units: mem={out["maxTxExecutionUnits"]["memory"]:,}, cpu={out["maxTxExecutionUnits"]["steps"]:,}')
print(f'  our ex-units:    mem=${EX_UNITS_MEM}, cpu=${EX_UNITS_CPU}  (must be <= max)')
PY
  rm -f "${OUT_DIR}/.koios-raw.json"

  echo
  echo "Stage 1 complete. Inspect $PROTOCOL_PARAMS_FILE."
}

# ====================================================================
# Stage 2 — Preflight: confirm seed + reserve UTxOs are still unspent
# ====================================================================
stage_preflight() {
  echo "=== Stage 2 — Preflight: verify seed_utxo + reserve_utxo are unspent ==="

  # Fetch UTxOs at admin_1's address via Koios. Store in a temp file so
  # python reads it without bash-heredoc-quoting hazards.
  local UTXO_FILE="${OUT_DIR}/.address-utxos.json"
  koios_post "/address_utxos" "{\"_addresses\":[\"$ADMIN_1_ADDR\"],\"_extended\":false}" > "$UTXO_FILE"

  # Pass values via env vars (no heredoc interpolation) so python sees them
  # as plain strings, not embedded in source code.
  EXPECTED_SEED_TXID="$SEED_UTXO_TXID" \
  EXPECTED_SEED_IDX="$SEED_UTXO_IDX" \
  EXPECTED_RESERVE_TXID="$RESERVE_UTXO_TXID" \
  EXPECTED_RESERVE_IDX="$RESERVE_UTXO_IDX" \
  ADMIN_1_ADDR="$ADMIN_1_ADDR" \
  UTXO_FILE="$UTXO_FILE" \
  python3 - <<'PY'
import json, os, sys
utxos = json.load(open(os.environ['UTXO_FILE']))
admin_addr = os.environ['ADMIN_1_ADDR']
seed_txid = os.environ['EXPECTED_SEED_TXID']
seed_idx = int(os.environ['EXPECTED_SEED_IDX'])
reserve_txid = os.environ['EXPECTED_RESERVE_TXID']
reserve_idx = int(os.environ['EXPECTED_RESERVE_IDX'])

print(f'  found {len(utxos)} UTxO(s) at admin_1 ({admin_addr})')
seed_ok = False
reserve_ok = False
for u in utxos:
    val = int(u['value'])
    print(f"    {u['tx_hash']}#{u['tx_index']}  {val:>12,} lovelace")
    if u['tx_hash'] == seed_txid and u['tx_index'] == seed_idx:
        seed_ok = True
        if val != 5000000:
            print(f'    WARN: seed UTxO value {val:,} != expected 5,000,000')
    if u['tx_hash'] == reserve_txid and u['tx_index'] == reserve_idx:
        reserve_ok = True
        if val != 35000000:
            print(f'    WARN: reserve UTxO value {val:,} != expected 35,000,000')

if not seed_ok:
    print('  X SEED UTXO NOT FOUND — abort, do not proceed')
    sys.exit(1)
if not reserve_ok:
    print('  X RESERVE UTXO NOT FOUND — abort, do not proceed')
    sys.exit(1)
print('  OK - both UTxOs still unspent at admin_1')
PY

  rm -f "$UTXO_FILE"
  echo
  echo "Stage 2 complete."
}

# ====================================================================
# Stage 3 — Build the mint tx (raw, no node connection required)
# ====================================================================
stage_build() {
  echo "=== Stage 3 — Build raw mint tx ==="
  require_file "$APPLIED_SCRIPT_FILE"
  require_file "$MINT_REDEEMER_FILE"
  require_file "$PROTOCOL_PARAMS_FILE"

  # Compute current slot via Koios for the validity window.
  local TIP_SLOT TIP_TIME
  read -r TIP_SLOT TIP_TIME < <(curl -sS "${NETWORK_KOIOS}/tip" | python3 -c "import json,sys; d=json.load(sys.stdin)[0]; print(d['abs_slot'], d['block_time'])")
  echo "  current tip:    slot=$TIP_SLOT, time=$TIP_TIME"
  local INVALID_BEFORE=$TIP_SLOT
  local INVALID_AFTER=$(( TIP_SLOT + TX_VALIDITY_HOURS * 3600 ))
  echo "  validity range: slot $INVALID_BEFORE .. $INVALID_AFTER ($TX_VALIDITY_HOURS h)"

  # `build-raw` requires explicit fee + explicit change output (it does
  # NOT support --change-address; that's `build`-only, which needs a
  # local node socket). We compute change manually:
  #   total_in  = SEED (5 ADA) + RESERVE (35 ADA) = 40_000_000 lovelace
  #   asset_out = POOL (2 ADA) + RESERVE_OUT (2 ADA) = 4_000_000 lovelace
  #   change    = total_in - asset_out - fee  (all to admin_1)
  local TOTAL_IN=40000000
  local ASSET_OUT=$(( POOL_OUTPUT_ADA + RESERVE_OUTPUT_ADA ))

  # First pass: placeholder fee, compute actual min-fee.
  local PLACEHOLDER_FEE=500000
  local CHANGE_PLACEHOLDER=$(( TOTAL_IN - ASSET_OUT - PLACEHOLDER_FEE ))

  # The surrender_pool output MUST carry an inline datum. The claim_validator
  # at this script address has `expect Some(_) = datum` on every spend path
  # (ProcessSurrender + AdminWithdraw); a no-datum output here would lock the
  # tokens forever. cMATRA v1 (2026-05-18) shipped without this flag and
  # permanently locked 722.5M cMATRA. The post-mortem flux_mint_policy
  # (commit 421ab3ff, invariant I6) structurally rejects a no-datum cMATRA
  # output at a script address — but we also belt-and-braces enforce it
  # here at tx construction time.
  local VOID_INLINE_DATUM='{"constructor":0,"fields":[]}'

  _build_raw() {
    local fee="$1"
    local change="$2"
    cli latest transaction build-raw \
      --tx-in "$SEED_UTXO" \
      --tx-in "$RESERVE_UTXO" \
      --tx-in-collateral "$RESERVE_UTXO" \
      --tx-out "${SURRENDER_POOL_ADDR}+${POOL_OUTPUT_ADA}+${CMATRA_POOL_AMOUNT_BASE} ${CMATRA_UNIT}" \
      --tx-out-inline-datum-value "$VOID_INLINE_DATUM" \
      --tx-out "${ADMIN_1_ADDR}+${RESERVE_OUTPUT_ADA}+${CMATRA_RESERVE_AMOUNT_BASE} ${CMATRA_UNIT}" \
      --tx-out "${ADMIN_1_ADDR}+${change}" \
      --mint "${CMATRA_TOTAL_SUPPLY_BASE} ${CMATRA_UNIT}" \
      --mint-script-file "$APPLIED_SCRIPT_FILE" \
      --mint-redeemer-file "$MINT_REDEEMER_FILE" \
      --mint-execution-units "(${EX_UNITS_CPU}, ${EX_UNITS_MEM})" \
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

  # Compute actual min-fee
  local MIN_FEE_LINE
  MIN_FEE_LINE=$(cli latest transaction calculate-min-fee \
    --tx-body-file "$TX_RAW" \
    --tx-in-count 2 \
    --tx-out-count 3 \
    --witness-count 2 \
    --byron-witness-count 0 \
    --protocol-params-file "$PROTOCOL_PARAMS_FILE" 2>&1)
  local MIN_FEE
  MIN_FEE=$(echo "$MIN_FEE_LINE" | grep -oE '[0-9]+' | head -1)
  if [ -z "$MIN_FEE" ]; then
    echo "  WARN: could not parse min-fee from: $MIN_FEE_LINE"
    MIN_FEE="$PLACEHOLDER_FEE"
  fi
  echo "  computed min-fee: $MIN_FEE lovelace"

  # Buffer 50k lovelace for safety (Plutus ref-script byte costs not always
  # reflected by calculate-min-fee for inline mint-script-file).
  local FINAL_FEE=$(( MIN_FEE + 50000 ))
  local FINAL_CHANGE=$(( TOTAL_IN - ASSET_OUT - FINAL_FEE ))
  echo "  applying fee:     $FINAL_FEE lovelace (with 50k buffer)"
  echo "  change to admin_1: $FINAL_CHANGE lovelace ($(python3 -c "print(f'{$FINAL_CHANGE / 1_000_000:.6f}')" ) ADA)"

  # Second pass: rebuild with correct fee + correct change
  _build_raw "$FINAL_FEE" "$FINAL_CHANGE"

  # Print the tx hash + summary
  echo
  echo "--- tx body summary ---"
  echo "  tx_hash:       $(cli latest transaction txid --tx-body-file "$TX_RAW")"
  python3 - <<PY
import json
d = json.load(open("$TX_RAW"))
print(f"  type:          {d['type']}")
print(f"  description:   {d.get('description', '')}")
print(f"  cborHex bytes: {len(d['cborHex']) // 2}")
PY

  echo
  echo "Stage 3 complete. Inspect $TX_RAW directly:"
  echo "  python3 -c \"import json; print(json.dumps(json.load(open('$TX_RAW')), indent=2))\""
}

# ====================================================================
# Stage 4 — Sign with admin_1 on Gemtek
# ====================================================================
stage_sign1() {
  echo "=== Stage 4 — Sign with admin_1 (Gemtek-local) ==="
  require_file "$TX_RAW"
  confirm_admin1_skey

  cli latest transaction sign \
    --tx-body-file "$TX_RAW" \
    --signing-key-file "$ADMIN_1_SKEY" \
    $NETWORK_MAGIC \
    --out-file "$TX_ADMIN1_SIGNED"

  echo "  wrote $TX_ADMIN1_SIGNED"
  echo "  Inspect via: cardano-cli latest transaction view --tx-file $TX_ADMIN1_SIGNED"
  echo
  echo "Stage 4 complete."
}

# ====================================================================
# Stage 5 — Sign with admin_2 on Node-3 (via SSH)
# ====================================================================
stage_sign2() {
  echo "=== Stage 5 — Generate admin_2 witness on Node-3 ==="
  require_file "$TX_RAW"

  # Send tx body to Node-3, generate witness, stream witness content back
  # via stdout (base64-encoded between markers), trap-cleanup local files
  # on the remote at end of the SAME session. Single round-trip; no race
  # between scp and cleanup.
  scp -q "$TX_RAW" "${ADMIN_2_HOST}:/tmp/mint-tx.raw"

  local remote_out
  remote_out=$(ssh "$ADMIN_2_HOST" 'bash -s -- "$@"' \
      "$ADMIN_2_SKEY_REMOTE" "$NETWORK_MAGIC" <<'REMOTE'
set -euo pipefail
SKEY="$1"
NETMAGIC="$2"

cleanup() {
  for f in /tmp/mint-tx.raw /tmp/mint-tx.admin_2.witness; do
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
  --tx-body-file /tmp/mint-tx.raw \
  --signing-key-file "$SKEY" \
  $NETMAGIC \
  --out-file /tmp/mint-tx.admin_2.witness 2>&1 >&2

# Stream witness content back via stdout, sandwiched between markers so
# local can parse it cleanly. base64 encode for safe transport (the
# witness is JSON with a cborHex field; ASCII-only, but base64 keeps
# it tidy across SSH and avoids any newline/quoting surprises).
echo "=====WITNESS-BEGIN====="
base64 -w0 /tmp/mint-tx.admin_2.witness
echo
echo "=====WITNESS-END====="
echo "  Node-3 witness produced" >&2
REMOTE
  )

  # Parse the marker-delimited base64 + decode locally
  local b64
  b64=$(echo "$remote_out" | awk '/=====WITNESS-BEGIN=====/{flag=1; next} /=====WITNESS-END=====/{flag=0} flag')
  [ -n "$b64" ] || die "admin_2 witness was empty — check Node-3 stderr above"
  echo "$b64" | base64 -d > "$TX_ADMIN2_WITNESS"
  [ -s "$TX_ADMIN2_WITNESS" ] || die "decoded witness was empty"
  echo "  wrote $TX_ADMIN2_WITNESS ($(wc -c < "$TX_ADMIN2_WITNESS") bytes)"
  echo
  echo "Stage 5 complete."
}

# ====================================================================
# Stage 6 — Assemble final signed tx from admin_1 signed tx + admin_2 witness
# ====================================================================
stage_assemble() {
  echo "=== Stage 6 — Assemble final signed tx ==="
  require_file "$TX_ADMIN1_SIGNED"
  require_file "$TX_ADMIN2_WITNESS"

  # Extract admin_1's witness from its signed tx and combine with admin_2's witness
  # `transaction assemble` takes the tx body + multiple --witness-file args.
  local ADMIN1_WITNESS="${OUT_DIR}/mint-tx.admin_1.witness"
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
  echo "  final tx view:"
  cli latest transaction view --tx-file "$TX_FINAL" 2>&1 | head -60

  # Compute the tx hash for record-keeping
  local TX_HASH
  TX_HASH=$(cli latest transaction txid --tx-file "$TX_FINAL")
  echo
  echo "  tx_hash: $TX_HASH"
  echo "  policy_id (sanity check): $CMATRA_POLICY_ID"
  echo
  echo "Stage 6 complete. NOTHING IS ON CHAIN YET. Run 'submit' to broadcast."
}

# ====================================================================
# Stage 7 — Submit via Koios
# ====================================================================
stage_submit() {
  echo "=== Stage 7 — Submit signed tx to Cardano mainnet via Koios ==="
  require_file "$TX_FINAL"

  # cardano-cli has CBOR-hex extraction via `transaction txid` and direct submission
  # via `transaction submit` (needs node socket). For socket-less submission, use
  # Koios POST /submittx with the raw CBOR bytes.
  local CBOR_HEX
  CBOR_HEX=$(python3 -c "import json; d=json.load(open('$TX_FINAL')); print(d['cborHex'])")
  local CBOR_BYTES_FILE="${OUT_DIR}/mint-tx.cbor"
  python3 -c "import sys; sys.stdout.buffer.write(bytes.fromhex('$CBOR_HEX'))" > "$CBOR_BYTES_FILE"

  echo "  CBOR size: $(stat -c %s "$CBOR_BYTES_FILE") bytes"

  # FINAL SAFETY GATE — operator must type 'mint' to proceed
  echo
  echo "  THIS WILL SUBMIT THE 1B cMATRA MINT TX TO CARDANO MAINNET."
  echo "  After acceptance, the mint is IRREVERSIBLE."
  echo "  Type the word 'mint' to proceed, anything else to abort:"
  read -r CONFIRM
  [ "$CONFIRM" = "mint" ] || die "aborted"

  curl -sS -X POST "${NETWORK_KOIOS}/submittx" \
    -H 'Content-Type: application/cbor' \
    --data-binary "@${CBOR_BYTES_FILE}" \
    --write-out '\nHTTP %{http_code}\n'

  echo
  echo "  After Koios accepts the tx, watch the explorer for confirmation:"
  echo "  https://cexplorer.io/asset/asset...   (resolve via $CMATRA_UNIT)"
  echo "  Or query Koios: $NETWORK_KOIOS/tx_info  with [\"<tx_hash>\"]"
}

# ====================================================================
# Dispatch
# ====================================================================
case "${1:-help}" in
  apply)      stage_apply ;;
  params)     stage_params ;;
  preflight)  stage_preflight ;;
  build)      stage_build ;;
  sign-1)     stage_sign1 ;;
  sign-2)     stage_sign2 ;;
  assemble)   stage_assemble ;;
  submit)     stage_submit ;;
  help|*)
    cat <<EOF
cMATRA Mainnet Mint Ceremony — usage:

  ./mint-ceremony.sh apply        # Stage 0 — apply mint policy params
  ./mint-ceremony.sh params       # Stage 1 — fetch protocol params (Koios)
  ./mint-ceremony.sh preflight    # Stage 2 — confirm UTxOs still unspent
  ./mint-ceremony.sh build        # Stage 3 — build raw mint tx
  ./mint-ceremony.sh sign-1       # Stage 4 — sign with admin_1 (Gemtek)
  ./mint-ceremony.sh sign-2       # Stage 5 — generate admin_2 witness (Node-3 via SSH)
  ./mint-ceremony.sh assemble     # Stage 6 — combine witnesses
  ./mint-ceremony.sh submit       # Stage 7 — submit to mainnet (IRREVERSIBLE)

Run each stage manually. Inspect outputs before proceeding.
Locked params: see params.env in this directory.
EOF
    ;;
esac
