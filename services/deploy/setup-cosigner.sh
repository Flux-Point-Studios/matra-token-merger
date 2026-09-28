#!/usr/bin/env bash
# ============================================================================
# Co-Signer Setup Script (Server B — GMKTec Ultra6)
# ============================================================================
#
# Run this ON the GMKTec machine. It will:
#   1. Generate admin key pair 2 (the key NEVER leaves this machine)
#   2. Create .env.cosigner, or add the settings an existing one lacks
#   3. Build and start the Docker container
#   4. Print the PKH for you to configure on Server A
#
# Prerequisites:
#   - Docker + Docker Compose installed
#   - This repo cloned (or at least the services/deploy/ directory)
#   - Internet access (to pull Python base image)
#
# Usage:
#   cd services/deploy
#   bash setup-cosigner.sh
#
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KEYS_DIR="$SCRIPT_DIR/keys"

echo "============================================"
echo "  cMATRA Co-Signer Setup (Server B)"
echo "============================================"
echo ""

# --- Step 1: Generate admin key pair 2 ---
mkdir -p "$KEYS_DIR"
chmod 700 "$KEYS_DIR"

if [[ -f "$KEYS_DIR/admin_2.skey" ]]; then
    echo "Key already exists at $KEYS_DIR/admin_2.skey"
    echo "Skipping key generation. Delete it first if you want a new key."
else
    echo "Generating admin key pair 2..."
    # Use cardano-cli if available, otherwise use Python
    if command -v cardano-cli &> /dev/null; then
        cardano-cli address key-gen \
            --signing-key-file "$KEYS_DIR/admin_2.skey" \
            --verification-key-file "$KEYS_DIR/admin_2.vkey"
        echo "Generated with cardano-cli"
    else
        echo "cardano-cli not found — generating with Python/pycardano"
        python3 -c "
from pycardano import PaymentSigningKey, PaymentVerificationKey
import json

sk = PaymentSigningKey.generate()
vk = PaymentVerificationKey.from_signing_key(sk)

# Save in cardano-cli compatible format
skey_data = {
    'type': 'PaymentSigningKeyShelley_ed25519',
    'description': 'cMATRA Co-Signer Admin Key 2',
    'cborHex': '5820' + sk.payload.hex()
}
vkey_data = {
    'type': 'PaymentVerificationKeyShelley_ed25519',
    'description': 'cMATRA Co-Signer Admin Key 2',
    'cborHex': '5820' + vk.payload.hex()
}

with open('$KEYS_DIR/admin_2.skey', 'w') as f:
    json.dump(skey_data, f, indent=4)
with open('$KEYS_DIR/admin_2.vkey', 'w') as f:
    json.dump(vkey_data, f, indent=4)

print(f'PKH: {vk.hash().payload.hex()}')
"
    fi
    chmod 600 "$KEYS_DIR/admin_2.skey"
    echo "Keys saved to $KEYS_DIR/"
fi

# --- Step 2: Extract PKH ---
echo ""
echo "Extracting PKH from key..."
PKH=$(python3 -c "
from pycardano import PaymentSigningKey, PaymentVerificationKey
sk = PaymentSigningKey.load('$KEYS_DIR/admin_2.skey')
vk = PaymentVerificationKey.from_signing_key(sk)
print(vk.hash().payload.hex())
")
echo ""
echo "============================================"
echo "  ADMIN KEY 2 — PUBLIC KEY HASH"
echo "============================================"
echo ""
echo "  PKH: $PKH"
echo ""
echo "  You need this value for:"
echo "    1. Server A env var:  COSIGNER_PKH=$PKH"
echo "    2. Validator compile: aiken blueprint apply (2nd param)"
echo ""
echo "============================================"

# --- Step 3: Write the settings .env.cosigner lacks ---
# The co-signer refuses to start without its policy. Export these before
# running this script (values for the deployed pool are in the ceremony
# record): COSIGNER_PRIMARY_ADMIN_PKH, SURRENDER_SCRIPT_ADDRESS,
# QUARANTINE_ADDRESS, CMATRA_POLICY_HEX, CMATRA_ASSET_HEX,
# SURRENDER_DEADLINE_POSIX_MS, MAX_CMATRA_PER_TX, MAX_CMATRA_PER_DAY, and
# BLOCKFROST_PROJECT_ID (this host's own view of the chain). A setting already
# in .env.cosigner is kept as it is, so on a host an earlier version set up
# only the settings it lacks are added and its API secret stays.
ENV_FILE="$SCRIPT_DIR/.env.cosigner"
EXPORTED=(COSIGNER_PRIMARY_ADMIN_PKH SURRENDER_SCRIPT_ADDRESS QUARANTINE_ADDRESS
          CMATRA_POLICY_HEX CMATRA_ASSET_HEX SURRENDER_DEADLINE_POSIX_MS
          MAX_CMATRA_PER_TX MAX_CMATRA_PER_DAY BLOCKFROST_PROJECT_ID)
has_setting() { [[ -f "$ENV_FILE" ]] && grep -q "^$1=" "$ENV_FILE"; }

UNSET=()
for name in "${EXPORTED[@]}"; do
    has_setting "$name" || [[ -n "${!name:-}" ]] || UNSET+=("$name")
done
if (( ${#UNSET[@]} )); then
    echo "ERROR: .env.cosigner does not set ${UNSET[*]}; export them and run this again." >&2
    exit 1
fi

ADDED=()
NEW_SECRET=0
# A co-signer whose settings name no ledger never kept one, so it gets its
# first. One that names a ledger must find it.
FIRST_LEDGER=0
add_setting() {
    has_setting "$1" && return
    echo "$1=$2" >> "$ENV_NEXT"
    ADDED+=("$1")
}
ENV_NEXT=$(mktemp "$SCRIPT_DIR/.env.cosigner.XXXXXX")
trap 'rm -f "$ENV_NEXT"' EXIT
if [[ -f "$ENV_FILE" ]]; then
    cat "$ENV_FILE" > "$ENV_NEXT"
    # A last line without a newline would run into the first added setting.
    [[ -z "$(tail -c1 "$ENV_FILE")" ]] || echo >> "$ENV_NEXT"
fi
add_setting COSIGNER_SKEY_PATH /app/keys/admin_2.skey
if ! has_setting COSIGNER_API_SECRET; then
    SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    add_setting COSIGNER_API_SECRET "$SECRET"
    unset SECRET
    NEW_SECRET=1
fi
has_setting COSIGNER_LEDGER_PATH || FIRST_LEDGER=1
add_setting COSIGNER_LEDGER_PATH /app/data/cosigned.sqlite3
add_setting NETWORK "${NETWORK:-mainnet}"
for name in "${EXPORTED[@]}"; do
    add_setting "$name" "${!name:-}"
done

echo ""
if (( ${#ADDED[@]} )); then
    mv "$ENV_NEXT" "$ENV_FILE"
    echo "Added to .env.cosigner (mode 600): ${ADDED[*]}"
else
    echo ".env.cosigner already has every setting."
fi
if (( NEW_SECRET )); then
    echo "It holds a generated API secret. Copy COSIGNER_API_SECRET from it into"
    echo "Server A's environment file without displaying it, e.g. over SSH into a"
    echo "mode-600 file."
fi
mkdir -p -m 700 "$SCRIPT_DIR/data"

# --- Step 4: Build and start ---
echo ""
echo "Building Docker container..."
cd "$SCRIPT_DIR"
docker compose -f docker-compose.cosigner.yml build

# The ledger of approvals is created once, for a signer that has never signed.
# The service refuses to start without it; a replacement would forget every
# approval and with them the caps.
LEDGER="$SCRIPT_DIR/data/cosigned.sqlite3"
if [[ ! -f "$LEDGER" ]]; then
    if (( ! FIRST_LEDGER )); then
        echo "ERROR: $LEDGER is missing, but this co-signer has kept a ledger." >&2
        echo "Restore the ledger it has been using; do not start it on a new one." >&2
        exit 1
    fi
    docker compose -f docker-compose.cosigner.yml run --rm --no-deps cosigner \
        python -m services.redemption_ledger init /app/data/cosigned.sqlite3
fi

echo ""
read -p "Start the co-signer service now? [y/N] " START
if [[ "$START" =~ ^[Yy]$ ]]; then
    docker compose -f docker-compose.cosigner.yml up -d
    echo ""
    echo "Service starting... checking health in 5s..."
    sleep 5
    curl -sf http://localhost:8421/health && echo "" || echo "Health check failed — check logs: docker compose -f docker-compose.cosigner.yml logs"
fi

echo ""
echo "============================================"
echo "  SETUP COMPLETE"
echo "============================================"
echo ""
echo "  Next steps:"
echo "    1. On Server A, set these env vars:"
echo "       COSIGNER_URL=http://<this-machine-ip>:8421"
echo "       COSIGNER_API_SECRET=<the value in .env.cosigner>"
echo "       COSIGNER_PKH=$PKH"
echo ""
echo "    2. Configure firewall to only allow Server A's IP on port 8421"
echo "       ufw allow from <server-a-ip> to any port 8421"
echo "       ufw deny 8421"
echo ""
echo "    3. (Recommended) Set up WireGuard VPN between Server A and B"
echo "       so co-signer traffic stays encrypted and off the public internet"
echo ""
echo "============================================"
