# FLUX Minting Policy

## Approach: Native Script with Time Lock

The FLUX minting policy uses a **native script** (no Plutus overhead) combining:

1. **RequireSignature** — only the designated minting key can authorize.
2. **RequireTimeBefore** — minting is only possible before a specific slot.

After the time-lock slot passes, the policy becomes permanently locked — no
further minting or burning is possible.

## Usage

1. Edit `mint_policy.json` to set:
   - `keyHash`: the payment key hash of your minting authority
   - `slot`: the deadline slot (choose ~1 hour after your planned mint tx)

2. Derive the policy ID:
   ```bash
   cardano-cli transaction policyid --script-file mint_policy.json
   ```

3. Mint in a single transaction:
   ```bash
   cardano-cli transaction build \
     --tx-in <funding-utxo> \
     --mint "1000000000000000 <policy-id>.464c5558" \
     --minting-script-file mint_policy.json \
     --invalid-hereafter <slot> \
     --change-address <funding-address> \
     --out-file mint_tx.raw
   ```

4. After the slot passes, the policy is permanently locked.

## Asset Name

- Hex: `464c5558` (ASCII "FLUX")
- Decimals: 6 (display convention only)
- Total supply: 1,000,000,000,000,000 base units (1e15)
