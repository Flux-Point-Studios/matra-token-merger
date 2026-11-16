#!/usr/bin/env bash
# Rebuilds the cMATRA registration validator and checks two things: the
# unapplied build is the reviewed one, and the Materios chain id parameter
# reaches the script hash (two chain ids give two addresses).
#
# Usage: cd onchain/cmatra_generates_motra && ./verify_build.sh
# Requires Aiken v1.1.21; other compiler versions give other hashes.

set -euo pipefail

REQUIRED_AIKEN_VERSION="v1.1.21"
# The unapplied compiled code's hash (blake2b-224 of 0x03 ‖ code).
EXPECTED_UNAPPLIED_HASH="b5cbd1bbe7d0d5f0b68f8a77a99af871b68d66665ff8c4476db97422"

AIKEN_VERSION=$(aiken --version | grep -oP 'v\d+\.\d+\.\d+' | head -1)
if [[ "$AIKEN_VERSION" != "$REQUIRED_AIKEN_VERSION" ]]; then
    echo "FAIL: need Aiken $REQUIRED_AIKEN_VERSION, got $AIKEN_VERSION" >&2
    exit 1
fi

aiken build

UNAPPLIED_HASH=$(python3 -I -c '
import hashlib, json
code = json.load(open("plutus.json"))["validators"][0]["compiledCode"]
print(hashlib.blake2b(b"\x03" + bytes.fromhex(code), digest_size=28).hexdigest())
')
echo "unapplied hash: $UNAPPLIED_HASH"
if [[ "$UNAPPLIED_HASH" != "$EXPECTED_UNAPPLIED_HASH" ]]; then
    echo "FAIL: expected $EXPECTED_UNAPPLIED_HASH" >&2
    exit 1
fi

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
applied_hash() {
    local cbor
    cbor=$(python3 -I -c 'import sys; b = sys.argv[1].encode(); assert len(b) < 24; print(bytes([0x40 + len(b)]).hex() + b.hex())' "$1")
    aiken blueprint apply -o "$scratch/$1.json" "$cbor" >/dev/null
    aiken blueprint hash -i "$scratch/$1.json"
}
MAINNET_HASH=$(applied_hash materios-mainnet)
PREPROD_HASH=$(applied_hash materios-preprod)
echo "materios-mainnet: $MAINNET_HASH"
echo "materios-preprod: $PREPROD_HASH"
if [[ "$MAINNET_HASH" == "$PREPROD_HASH" ]]; then
    echo "FAIL: the chain id does not reach the script hash" >&2
    exit 1
fi

echo "PASS"
