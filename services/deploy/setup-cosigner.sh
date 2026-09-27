#!/usr/bin/env bash
# ============================================================================
# Co-Signer Setup Script (Server B — GMKTec Ultra6)
# ============================================================================
#
# Run this ON the GMKTec machine. It will:
#   1. Generate admin key pair 2 (the key NEVER leaves this machine)
#   2. Create the .env.cosigner file
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

# --- Step 3: Create .env.cosigner ---
# The co-signer refuses to start without its policy. Export these before
# running this script (values for the deployed pool are in the ceremony
# record): COSIGNER_PRIMARY_ADMIN_PKH, SURRENDER_SCRIPT_ADDRESS,
# QUARANTINE_ADDRESS, CMATRA_POLICY_HEX, CMATRA_ASSET_HEX,
# SURRENDER_DEADLINE_POSIX_MS, MAX_CMATRA_PER_TX, MAX_CMATRA_PER_DAY.
if [[ ! -f "$SCRIPT_DIR/.env.cosigner" ]]; then
    : "${COSIGNER_PRIMARY_ADMIN_PKH:?export the admin_1 key hash}"
    : "${SURRENDER_SCRIPT_ADDRESS:?export the pool script address}"
    : "${QUARANTINE_ADDRESS:?export the quarantine address}"
    : "${CMATRA_POLICY_HEX:?export the cMATRA policy id}"
    : "${CMATRA_ASSET_HEX:?export the cMATRA asset name hex}"
    : "${SURRENDER_DEADLINE_POSIX_MS:?export the pool deadline}"
    : "${MAX_CMATRA_PER_TX:?export the per-transaction cap in base units}"
    : "${MAX_CMATRA_PER_DAY:?export the 24-hour cap in base units}"
    SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    (
        umask 177
        cat > "$SCRIPT_DIR/.env.cosigner" <<ENVFILE
COSIGNER_SKEY_PATH=/app/keys/admin_2.skey
COSIGNER_API_SECRET=$SECRET
COSIGNER_LEDGER_PATH=/app/data/cosigned.sqlite3
COSIGNER_PRIMARY_ADMIN_PKH=$COSIGNER_PRIMARY_ADMIN_PKH
NETWORK=${NETWORK:-mainnet}
SURRENDER_SCRIPT_ADDRESS=$SURRENDER_SCRIPT_ADDRESS
QUARANTINE_ADDRESS=$QUARANTINE_ADDRESS
CMATRA_POLICY_HEX=$CMATRA_POLICY_HEX
CMATRA_ASSET_HEX=$CMATRA_ASSET_HEX
SURRENDER_DEADLINE_POSIX_MS=$SURRENDER_DEADLINE_POSIX_MS
MAX_CMATRA_PER_TX=$MAX_CMATRA_PER_TX
MAX_CMATRA_PER_DAY=$MAX_CMATRA_PER_DAY
ENVFILE
    )
    unset SECRET
    mkdir -p -m 700 "$SCRIPT_DIR/data"
    NEW_SIGNER=1
    echo ""
    echo "Created .env.cosigner (mode 600) with a generated API secret."
    echo "Copy COSIGNER_API_SECRET from it into Server A's environment file"
    echo "without displaying it, e.g. over SSH into a mode-600 file."
    echo ""
else
    echo ".env.cosigner already exists — skipping."
fi

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
    if [[ "${NEW_SIGNER:-0}" != 1 ]]; then
        echo "ERROR: $LEDGER is missing, but this co-signer was set up before." >&2
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
