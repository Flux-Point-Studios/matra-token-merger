#!/usr/bin/env bash
# =============================================================================
# Deterministic Build Verification for cMATRA Quarantine Validator
# =============================================================================
#
# This script lets anyone independently verify that the deployed quarantine
# lockbox matches the audited source code. It rebuilds the validator from
# source and prints the resulting script hash for comparison against the
# expected value.
#
# Prerequisites:
#   - Aiken CLI v1.1.21 (EXACT version — different version = different hash)
#   - This repository checked out at the audited commit
#
# Usage:
#   cd onchain/quarantine_validator
#   bash verify_build.sh
#
# =============================================================================

set -euo pipefail

REQUIRED_AIKEN_VERSION="v1.1.21"

# Expected script hash of the audited build. If the locally rebuilt hash
# does not match this, the deployed validator is NOT the same code as
# this repo at this commit. Pin computed 2026-05-18 with
# aiken v1.1.21+42babe5.
EXPECTED_HASH="288fea77a0f674c6080aadd8ed3ca42cd5a920bf1f00c0d3e63306e4"

echo "=== Deterministic Build Verification (cMATRA Quarantine Validator) ==="
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
    echo ""
fi

echo "Building validator from source..."
aiken build 2>&1

if [[ ! -f "plutus.json" ]]; then
    echo "ERROR: Build did not produce plutus.json"
    exit 1
fi

echo ""
echo "Extracting script hash from blueprint..."

UNAPPLIED_HASH=$(python3 -c "
import json, hashlib
with open('plutus.json') as f:
    bp = json.load(f)
for v in bp['validators']:
    if v.get('title', '').endswith('.spend'):
        code = v['compiledCode']
        print(f'Compiled code: {len(code)//2} bytes')
        print(f'Validator title: {v[\"title\"]}')
        compiled_bytes = bytes.fromhex(code)
        prefixed = b'\\x03' + compiled_bytes
        h = hashlib.blake2b(prefixed, digest_size=28)
        print(f'Script hash (unparameterized): {h.hexdigest()}')
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
" 2>&1

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
    echo "NOTE: No EXPECTED_HASH set. Set it in this script after mainnet"
    echo "deployment to enable hash verification."
fi

echo ""
echo "=== Verification complete ==="
