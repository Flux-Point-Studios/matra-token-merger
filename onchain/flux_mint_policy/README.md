# cMATRA Mint Policy

Aiken (Plutus V3) minting policy that governs the entire 1B cMATRA token supply.
Locked-down by construction: one-shot mint, dual-admin signatures, exact supply
cap, single asset name; permissionless burn.

## Approach: Seed-UTxO + Dual-Admin Plutus V3 Validator

We deliberately moved off the older native-script (`{ all: [ sig, before_slot ] }`)
approach for the mainnet launch. The native script gave us a timelock but not a
hard supply cap, not a hard asset-name binding, and not the same operational
shape as the surrender pool validator that already requires both admin keys to
co-sign. Reusing the same dual-signer infrastructure for both the pool and the
mint keeps the security boundary uniform.

The policy enforces, in order:

1. **One-shot mint** — parameterized by a specific `OutputReference`
   (`seed_utxo`). The mint tx must consume that UTxO. Once spent, the UTxO is
   gone from Cardano's UTxO set forever, and so is the ability to re-mint.
2. **Exact supply cap** — the mint tx's *net* mint of cMATRA under this policy
   must equal exactly `1_000_000_000 × 10^6 = 1_000_000_000_000_000` base units.
   Any other positive quantity is rejected.
3. **Single asset name** — the policy may only ever touch `cMATRA`
   (hex `634d41545241`). Any other asset name under this policy, alone or in
   combination, is rejected.
4. **Dual-admin signatures** — both `admin_pkh_1` and `admin_pkh_2` must sign.
   Same model as `onchain/claim_validator`: Server A builds + signs, Server B
   co-signs via an authenticated API call. Compromising one server does not
   grant minting authority.
5. **Permissionless burn** — any holder can burn cMATRA (negative net mint),
   with no signature, no seed, and no quantity constraint other than the
   single-asset-name rule. This is the safety valve.

See `validators/flux_mint_policy.ak` for the validator source and the embedded
test suite (13 tests covering all five invariants).

## Parameterization

`aiken blueprint apply` expects, in order:

| # | Name          | Type                  | Notes                                                         |
|---|---------------|-----------------------|---------------------------------------------------------------|
| 1 | `seed_utxo`   | `OutputReference`     | Constructor `OutputReference { transaction_id, output_index}` |
| 2 | `admin_pkh_1` | `VerificationKeyHash` | 28-byte hex                                                   |
| 3 | `admin_pkh_2` | `VerificationKeyHash` | 28-byte hex                                                   |

The script hash printed by `aiken build` is the **unapplied** hash (before any
parameters are applied). The applied policy ID — the one used as the on-chain
`PolicyId` for cMATRA — is produced by `aiken blueprint apply` against the
three parameters.

## Build + verify

```bash
# Audited compiler version. Different versions produce different hashes.
aiken --version   # must print v1.1.21

# Build
aiken build       # writes plutus.json

# Tests
aiken check       # all 13 tests must pass

# Reproducibility check
bash verify_build.sh
```

The verify script prints the unapplied script hash and the parameter shape so
any third party can independently confirm the build matches the audited source.

## Usage (high-level)

The mint side is a single ceremony, run once:

1. Choose a `seed_utxo` controlled by `admin_pkh_1` (any of their UTxOs).
2. `aiken blueprint apply` the three parameters → produces the applied
   `plutus.json` with the final `PolicyId`.
3. Build the mint tx in Python tooling (`tools/`) with:
   - `--mint "1000000000000000 <policy_id>.634d41545241"`
   - The `seed_utxo` as an input
   - Both admin signatures (Server A signs, Server B co-signs)
4. Submit. After it lands, the policy is permanently inert for mints (seed is
   spent), and remains live forever for burns.

The asset-name hex `634d41545241` decodes to ASCII `cMATRA` (6 bytes) and
matches `MERGE_TOKEN_TICKER` in `tools/config.py`. The supply quantity
`1_000_000_000_000_000` matches `MERGE_TOKEN_SUPPLY_BASE`.

## Metadata

Off-chain CIP-26 metadata for the asset lives in `metadata/token-registry.json`.
Replace `POLICY_ID_HERE` in the `subject` field with the applied policy ID after
deployment.
