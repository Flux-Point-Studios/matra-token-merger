#!/usr/bin/env bash
# Apply surrender_pool's three parameters to the committed blueprint.
#
#   apply_params.sh ADMIN_PKH_1 ADMIN_PKH_2 DEADLINE_POSIX_MS OUT.json
#
# Writes the applied blueprint to OUT.json and prints the script hash and the
# mainnet enterprise address. The hash is only meaningful under the audited
# compiler (verify_build.sh), so a different aiken version is refused.
set -euo pipefail

REQUIRED_AIKEN_VERSION="v1.1.21"
VALIDATOR="surrender_pool"
MODULE="claim_validator"

if [[ $# -ne 4 ]]; then
    echo "usage: $0 ADMIN_PKH_1 ADMIN_PKH_2 DEADLINE_POSIX_MS OUT.json" >&2
    exit 2
fi
pkh1="$1" pkh2="$2" deadline="$3" out="$4"

for pkh in "$pkh1" "$pkh2"; do
    [[ "$pkh" =~ ^[0-9a-f]{56}$ ]] || { echo "not a 28-byte lowercase hex key hash: $pkh" >&2; exit 2; }
done
[[ "$pkh1" != "$pkh2" ]] || { echo "the two admin key hashes must differ" >&2; exit 2; }
[[ "$deadline" =~ ^[1-9][0-9]{12}$ ]] || { echo "deadline must be 13-digit POSIX milliseconds: $deadline" >&2; exit 2; }

version=$(aiken --version | grep -oP 'v\d+\.\d+\.\d+' | head -1)
[[ "$version" == "$REQUIRED_AIKEN_VERSION" ]] || { echo "aiken $version, need $REQUIRED_AIKEN_VERSION" >&2; exit 1; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Plutus Data CBOR: a 28-byte bytestring is 581c<hex>; a deadline in this
# range needs CBOR's 8-byte unsigned integer, 1b<16 hex digits>.
printf -v int_cbor '1b%016x' "$deadline"

aiken blueprint apply -i "$here/plutus.json" -o "$work/a1.json" -m "$MODULE" -v "$VALIDATOR" "581c$pkh1" >&2
aiken blueprint apply -i "$work/a1.json" -o "$work/a2.json" -m "$MODULE" -v "$VALIDATOR" "581c$pkh2" >&2
aiken blueprint apply -i "$work/a2.json" -o "$out" -m "$MODULE" -v "$VALIDATOR" "$int_cbor" >&2

python3 - "$out" <<'PY'
import hashlib, json, sys

BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def bech32(hrp, data):
    acc, bits, five = 0, 0, []
    for byte in data:
        acc, bits = acc << 8 | byte, bits + 8
        while bits >= 5:
            bits -= 5
            five.append(acc >> bits & 31)
    if bits:
        five.append(acc << (5 - bits) & 31)
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    mod = polymod(expanded + five + [0] * 6) ^ 1
    return hrp + "1" + "".join(BECH32[d] for d in five + [mod >> 5 * (5 - i) & 31 for i in range(6)])


blueprint = json.load(open(sys.argv[1]))
spend = [v for v in blueprint["validators"] if v["title"].endswith(".spend")]
if len(spend) != 1 or spend[0].get("parameters"):
    sys.exit("expected one fully applied spend validator")
code = bytes.fromhex(spend[0]["compiledCode"])
script_hash = hashlib.blake2b(b"\x03" + code, digest_size=28).digest()
# Header 0x71: mainnet enterprise address with a script payment credential.
address = bech32("addr", b"\x71" + script_hash)
print(f"script_hash {script_hash.hex()}")
print(f"address     {address}")
print(f"size        {len(code)}")
PY
