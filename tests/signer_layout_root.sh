#!/usr/bin/env bash
# CI's signer-layout step, as root in a throwaway container and nowhere else.
#
# Both signers are installed by services/deploy/install-signer.sh as the C1
# runbook's M1 and M2 do, each moving in a ledger the old layout kept and
# proving the copy. RUNBOOK.md's block K2 makes each key as its service user
# (and must refuse a second run), and block W1 witnesses a transaction as
# cmatra-a1 from a directory that user cannot enter. --check must pass, and
# fail once a mode is loosened. Then each service starts dark under its own
# user, with its unit's command, working directory and environment file, and
# /health and the startup log must show the keys and the co-signer.
set -euo pipefail

[[ $EUID -eq 0 && ${CI:-} == woodpecker ]] || { echo "only for CI's throwaway container" >&2; exit 1; }

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install_signer=$repo/services/deploy/install-signer.sh
# shellcheck disable=SC2034  # read by the runbook blocks this script evals
CLI=/opt/cardano-cli-11.0.0.0/cardano-cli
quarantine=addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl

fail() { echo "signer-layout: FAIL $*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

# sudo as the runbook's blocks use it: -u USER runs the rest as that user.
sudo() { if [[ $1 == -u ]]; then local as=$2; shift 2; runuser -u "$as" -- "$@"; else "$@"; fi; }

block() {   # RUNBOOK.md's fenced bash block whose first line starts with "# NAME:", dedented
    python3 - "$repo/ceremony/c1/RUNBOOK.md" "$1" <<'PY'
import re, sys, textwrap
text, name = open(sys.argv[1]).read(), sys.argv[2]
found = [textwrap.dedent(body) for indent, body in
         re.findall(r"^( *)```bash\n((?:\1.*\n)*?)\1```$", text, re.MULTILINE)
         if textwrap.dedent(body).startswith(f"# {name}:")]
assert len(found) == 1, f"{len(found)} blocks named {name}"
print(found[0], end="")
PY
}

in_release() { (cd /opt/cmatra-a1/app && .venv/bin/python "$@"); }

step "the pinned cardano-cli, where the key users can run it"
bash "$repo/services/deploy/install-cardano-cli.sh" /opt/cardano-cli-11.0.0.0

step "both signers from this commit"
"$install_signer" a1
"$install_signer" a2

step "an operator account holding the ledgers the old layout kept, one approval each"
useradd --create-home --shell /bin/bash operator
chmod 0750 /home/operator
old_a1=/home/operator/surrender-api/data/surrender-ledger.sqlite3
old_a2=/home/operator/cmatra-cosigner/data/cosigned.sqlite3
for old in "$old_a1" "$old_a2"; do
    runuser -u operator -- install -d -m 0700 "$(dirname "$old")"
    (cd /opt/cmatra-a1/app && runuser -u operator -- .venv/bin/python - "$old" <<'PY'
import json, sqlite3, sys
from services.redemption_ledger import create_ledger
create_ledger(sys.argv[1])
with sqlite3.connect(sys.argv[1]) as conn:
    conn.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (b"\x01" * 32, b"\x02" * 32, 1, 925780, json.dumps({"ab" * 28: 2}),
                  json.dumps([["02" * 32, 1]]), 1793754311.0))
PY
    )
done

step "M1 and M2: each ledger moved in with its proof, the old file left read-only"
"$install_signer" a1 --ledger-from "$old_a1"
"$install_signer" a2 --ledger-from "$old_a2"
new_a1=/var/lib/cmatra-a1/ledger/surrender-ledger.sqlite3
new_a2=/var/lib/cmatra-a2/ledger/cosigned.sqlite3
for pair in "$old_a1 $new_a1" "$old_a2 $new_a2"; do
    read -r old new <<< "$pair"
    [[ $(sha256sum < "$old") == "$(sha256sum < "$new")" ]] || fail "$new is not $old byte for byte"
    [[ $(stat -c %a "$old") == 400 ]] || fail "$old was left writable"
done
digest() { in_release -m services.redemption_ledger digest "$1" | python3 -c 'import json, sys; print(json.load(sys.stdin)["approvals_digest"])'; }
[[ $(digest "$new_a1") == "$(digest "$new_a2")" ]] || fail "the two signers' approvals differ"
if "$install_signer" a1 --ledger-from "$old_a1" 2>/dev/null; then fail "a second M1 replaced the moved ledger"; fi

step "K2: each key made by its service user, never over an existing one"
k2=$(block K2)
declare -A pkh
for pair in "cmatra-a1 admin_1" "cmatra-a2 admin_2"; do
    read -r U K <<< "$pair"
    line=$(cd /home/operator && eval "$k2") || fail "K2 failed for $U"
    pkh[$U]=${line##* }
    [[ ${pkh[$U]} =~ ^[0-9a-f]{56}$ ]] || fail "K2 printed no key hash for $U: $line"
    before=$(sha256sum < "/var/lib/$U/keys/$K.skey")
    if (cd /home/operator && eval "$k2") >/dev/null 2>&1; then fail "K2 ran over $U's existing key"; fi
    [[ $(sha256sum < "/var/lib/$U/keys/$K.skey") == "$before" ]] || fail "K2 changed $U's existing key"
done
pkh1=${pkh[cmatra-a1]} pkh2=${pkh[cmatra-a2]}

step "the applied blueprint, the chain credential and each env, as M1, M2 and section 6 write them"
install -m 0400 -o cmatra-a1 -g cmatra-a1 "$repo/onchain/claim_validator/plutus.json" /var/lib/cmatra-a1/claim_validator.plutus.json
address=$(in_release - <<'PY'
from pycardano import Address, Network, ScriptHash
from tools.cardano_utils import plutus_v3_script_hash
from tools.process_surrender import load_script_from_blueprint
code = bytes.fromhex(load_script_from_blueprint("/var/lib/cmatra-a1/claim_validator.plutus.json"))
print(Address(payment_part=ScriptHash(plutus_v3_script_hash(code)), network=Network.MAINNET))
PY
)
printf 'mainnetsignerlayoutci\n' > /var/lib/cmatra-a2/chain-api.key
chown cmatra-a2:cmatra-a2 /var/lib/cmatra-a2/chain-api.key
chmod 0400 /var/lib/cmatra-a2/chain-api.key
cosigner_secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
policy="SURRENDER_SCRIPT_ADDRESS=$address
QUARANTINE_ADDRESS=$quarantine
CMATRA_POLICY_HEX=7ff33a5565393dc47b48ac47becc12d92c9952e724e8446dfb6adc66
CMATRA_ASSET_HEX=634d41545241
SURRENDER_DEADLINE_POSIX_MS=1795910400000
MAX_CMATRA_PER_TX=4000000000000
MAX_CMATRA_PER_DAY=15000000000000
NETWORK=mainnet"
(umask 0077 && cat > /etc/cmatra-a1/env.surrender-api <<EOF
$policy
SURRENDER_API_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
ADMIN_SKEY_PATH=/var/lib/cmatra-a1/keys/admin_1.skey
BLUEPRINT_PATH=/var/lib/cmatra-a1/claim_validator.plutus.json
SURRENDER_LEDGER_PATH=$new_a1
COSIGNER_URL=http://127.0.0.1:8421
COSIGNER_API_SECRET=$cosigner_secret
COSIGNER_PKH=$pkh2
BLOCKFROST_PROJECT_ID=mainnetsignerlayoutci
EOF
)
(umask 0077 && cat > /etc/cmatra-a2/env.cosigner <<EOF
$policy
COSIGNER_API_SECRET=$cosigner_secret
COSIGNER_SKEY_PATH=/var/lib/cmatra-a2/keys/admin_2.skey
COSIGNER_PRIMARY_ADMIN_PKH=$pkh1
COSIGNER_LEDGER_PATH=$new_a2
CHAIN_API_KEY_FILE=/var/lib/cmatra-a2/chain-api.key
EOF
)

step "--check passes on both, and names a loosened env file"
"$install_signer" a1 --check
"$install_signer" a2 --check
chmod 0640 /etc/cmatra-a1/env.surrender-api
if "$install_signer" a1 --check > /tmp/loose.txt; then fail "--check passed a 0640 env file"; fi
grep -q "^FAIL /etc/cmatra-a1/env.surrender-api: regular file 640" /tmp/loose.txt || fail "--check did not name the 0640 env file"
chmod 0600 /etc/cmatra-a1/env.surrender-api

step "what each user can and cannot touch"
runuser -u cmatra-a1 -- test -w /var/lib/cmatra-a1/ledger || fail "cmatra-a1 cannot write its ledger directory"
for denied in "cmatra-a1 -w /opt/cmatra-a1/app/services" "cmatra-a1 -r /etc/cmatra-a1/env.surrender-api" \
              "cmatra-a1 -r /var/lib/cmatra-a2/keys/admin_2.skey" "cmatra-a2 -r /var/lib/cmatra-a1/keys/admin_1.skey" \
              "operator -r /var/lib/cmatra-a1/keys/admin_1.skey" "operator -r /etc/cmatra-a2/env.cosigner"; do
    read -r who mode path <<< "$denied"
    if runuser -u "$who" -- test "$mode" "$path"; then fail "$who passes test $mode $path"; fi
done

step "W1: new admin_1 witnesses a transaction as cmatra-a1, from a directory it cannot enter"
install -d -m 0700 -o operator -g operator /home/operator/c1-ceremony
python3 - "$repo/ceremony/c1/chain/pool-parent.cbor.json" /home/operator/c1-ceremony/c1a.unsigned.tx <<'PY'
import json, sys
json.dump({"type": "Tx ConwayEra", "description": "", "cborHex": json.load(open(sys.argv[1]))["cbor"]}, open(sys.argv[2], "w"))
PY
w1=$(block W1)
# shellcheck disable=SC2034  # W1 reads FILE
(cd /home/operator/c1-ceremony && FILE=c1a && eval "$w1")
in_release - /home/operator/c1-ceremony/c1a.new1.witness /home/operator/c1-ceremony/c1a.unsigned.tx "$pkh1" <<'PY'
import hashlib, json, sys
from services.cosign_policy import signature_verifies, split_tx
from tools.pool_migration import _key_witness
vkey, signature = _key_witness(bytes.fromhex(json.load(open(sys.argv[1]))["cborHex"]))
body = split_tx(bytes.fromhex(json.load(open(sys.argv[2]))["cborHex"]))[0]
assert hashlib.blake2b(vkey, digest_size=28).hexdigest() == sys.argv[3], "the witness is not admin_1's"
assert signature_verifies(vkey, hashlib.blake2b(body, digest_size=32).digest(), signature), "the signature does not verify"
print(f"W1: admin_1 {sys.argv[3]} signed the body")
PY

step "each service started dark under its own user, as its unit runs it"
dark_start() {   # UNIT: its ExecStart as its User, in its WorkingDirectory, with only its EnvironmentFile
    local unit=/etc/systemd/system/$1 user workdir envfile command
    user=$(sed -n 's/^User=//p' "$unit")
    workdir=$(sed -n 's/^WorkingDirectory=//p' "$unit")
    envfile=$(sed -n 's/^EnvironmentFile=//p' "$unit")
    command=$(sed -n 's/^ExecStart=//p' "$unit")
    mapfile -t settings < <(grep -E '^[A-Z_]+=' "$envfile")
    # shellcheck disable=SC2086  # ExecStart is split into words, as systemd splits it
    (cd "$workdir" && exec runuser -u "$user" -- env -i PATH=/usr/bin:/bin "HOME=/var/lib/$user" "${settings[@]}" \
        $command) > "/tmp/$1.log" 2>&1 &
}
health() {   # PORT: /health once the service answers, within 60 s
    for _ in $(seq 60); do
        curl -sf "http://127.0.0.1:$1/health" && return 0
        sleep 1
    done
    return 1
}
expect_json() { python3 -c 'import json, sys; got, want = json.loads(sys.argv[1]), json.loads(sys.argv[2]); assert got == want, (got, want)' "$1" "$2"; }

dark_start cmatra-cosigner.service
answer=$(health 8421) || { cat /tmp/cmatra-cosigner.service.log; fail "the co-signer did not start"; }
expect_json "$answer" "{\"status\": \"ok\", \"key_loaded\": true, \"pkh\": \"${pkh2:0:16}...\"}"
grep -q "Co-signer ready: PKH=$pkh2" /tmp/cmatra-cosigner.service.log || fail "the co-signer did not load admin_2"

dark_start surrender-api.service
answer=$(health 8420) || { cat /tmp/surrender-api.service.log; fail "surrender-api did not start"; }
expect_json "$answer" '{"status": "ok", "rate_table_loaded": true, "script_loaded": true, "admin_key_loaded": true, "configured": true}'
grep -q "Admin key loaded: PKH=$pkh1" /tmp/surrender-api.service.log || fail "surrender-api did not load admin_1"
grep -q "Co-signer PKH: ${pkh2:0:16}" /tmp/surrender-api.service.log || fail "surrender-api did not configure the co-signer"

# shellcheck disable=SC2046  # one job id per word
kill $(jobs -p) 2>/dev/null || true
printf '\nsigner-layout: every check passed\n'
