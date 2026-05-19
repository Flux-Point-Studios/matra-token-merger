#!/usr/bin/env bash
# =============================================================================
# Deterministic Build Verification for cMATRA Mint Policy
# =============================================================================
#
# This script allows anyone to independently verify that the deployed mint
# policy matches the audited source code.  It rebuilds the validator from
# source and compares the resulting script hash against the expected value.
#
# Prerequisites:
#   - Aiken CLI v1.1.21 (EXACT version — different version = different hash)
#   - This repository checked out at the audited commit
#
# Usage:
#   cd onchain/flux_mint_policy
#   bash verify_build.sh
#
# The script will:
#   1. Check the Aiken compiler version
#   2. Build the validator from source
#   3. Print the compiled script hash from the blueprint
#   4. Compare against the expected hash (if EXPECTED_HASH is set below)
#
# =============================================================================

set -euo pipefail

# --- Configuration ---
REQUIRED_AIKEN_VERSION="v1.1.21"

# Unparameterized mint-policy script hash, computed with Aiken v1.1.21+42babe5
# after the I6 (datum-required-at-script-addr) post-mortem fix landed.
# The previous (pre-I6) baseline `5edc287248...` shipped to mainnet on
# 2026-05-18 and caused the no-datum lock catastrophe — see
# `feedback_cardano_script_output_datum_required.md`. The new code
# structurally rejects any tx that places cMATRA at a script address with
# NoDatum, so no replay of that mistake is possible.
EXPECTED_HASH="f885091e5765152c8f65fb4d2cfd55056b79ea94c40dabca5537bdeb"

# --- Check Aiken version ---
echo "=== Deterministic Build Verification (cMATRA Mint Policy) ==="
echo ""

if ! command -v aiken &> /dev/null; then
    echo "ERROR: Aiken CLI not found. Install from https://aiken-lang.org"
    exit 1
fi

AIKEN_VERSION=$(aiken --version | grep -oP 'v\d+\.\d+\.\d+' | head -1)
echo "Aiken version: $AIKEN_VERSION"

if [[ "$AIKEN_VERSION" != "$REQUIRED_AIKEN_VERSION" ]]; then
    echo "WARNING: Expected $REQUIRED_AIKEN_VERSION, got $AIKEN_VERSION"
    echo "Different compiler versions produce different script hashes."
    echo "The audit was performed with $REQUIRED_AIKEN_VERSION."
    echo ""
fi

# --- Build ---
echo "Building validator from source..."
aiken build 2>&1

if [[ ! -f "plutus.json" ]]; then
    echo "ERROR: Build did not produce plutus.json"
    exit 1
fi

# --- Extract script hash ---
echo ""
echo "Extracting script hash from blueprint..."

# The unapplied validator hash (before parameter application)
UNAPPLIED_HASH=$(python3 -c "
import json, hashlib
with open('plutus.json') as f:
    bp = json.load(f)
for v in bp['validators']:
    # The mint handler is the canonical entry; the 'else' clause shares its hash.
    if v.get('title', '').endswith('.mint'):
        code = v['compiledCode']
        print(f'Compiled code: {len(code)//2} bytes')
        print(f'Validator title: {v[\"title\"]}')
        # The script hash is blake2b-224 of the compiled CBOR
        # (with a version prefix byte for Plutus V3)
        compiled_bytes = bytes.fromhex(code)
        # PlutusV3 = 03 prefix
        prefixed = b'\\x03' + compiled_bytes
        h = hashlib.blake2b(prefixed, digest_size=28)
        print(f'Script hash (unapplied): {h.hexdigest()}')
        break
" 2>&1)

echo "$UNAPPLIED_HASH"
echo ""
echo "--- Blueprint Info ---"
python3 -c "
import json
with open('plutus.json') as f:
    bp = json.load(f)
p = bp['preamble']
print(f'Title:    {p[\"title\"]}')
print(f'Version:  {p[\"version\"]}')
print(f'Plutus:   {p[\"plutusVersion\"]}')
print(f'Compiler: {p[\"compiler\"][\"name\"]} {p[\"compiler\"][\"version\"]}')
print(f'Validators: {len(bp[\"validators\"])}')
for v in bp['validators']:
    print(f'  - {v[\"title\"]}')
    if 'parameters' in v:
        for param in v['parameters']:
            print(f'    param: {param[\"title\"]} ({param[\"schema\"].get(\"\$ref\", \"inline\")})')
" 2>&1

# --- Compare against expected ---
if [[ -n "$EXPECTED_HASH" ]]; then
    echo ""
    echo "--- Verification ---"
    if echo "$UNAPPLIED_HASH" | grep -q "$EXPECTED_HASH"; then
        echo "PASS: Script hash matches expected value"
    else
        echo "FAIL: Script hash does NOT match expected value"
        echo "Expected: $EXPECTED_HASH"
        exit 1
    fi
else
    echo ""
    echo "NOTE: No EXPECTED_HASH set. Set it in this script after mainnet deployment"
    echo "to enable hash verification."
fi

echo ""
echo "=== Verification complete ==="
