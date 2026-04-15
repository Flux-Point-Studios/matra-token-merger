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
if [[ ! -f "$SCRIPT_DIR/.env.cosigner" ]]; then
    SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    cat > "$SCRIPT_DIR/.env.cosigner" <<EOF
COSIGNER_SKEY_PATH=/app/keys/admin_2.skey
COSIGNER_API_SECRET=$SECRET
COSIGNER_API_PORT=8421
EOF
    chmod 600 "$SCRIPT_DIR/.env.cosigner"
    echo ""
    echo "Created .env.cosigner with generated API secret."
    echo ""
    echo "  API Secret: $SECRET"
    echo ""
    echo "  Set this SAME value on Server A:"
    echo "    COSIGNER_API_SECRET=$SECRET"
    echo ""
else
    echo ".env.cosigner already exists — skipping."
    SECRET=$(grep COSIGNER_API_SECRET "$SCRIPT_DIR/.env.cosigner" | cut -d= -f2)
fi

# --- Step 4: Build and start ---
echo ""
echo "Building Docker container..."
cd "$SCRIPT_DIR"
docker compose -f docker-compose.cosigner.yml build

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
echo "       COSIGNER_API_SECRET=$SECRET"
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
