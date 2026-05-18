# cMATRA Quarantine Validator

A Plutus V3 spend validator with **no spending path**. Any transaction that
tries to spend a UTxO at this script address is rejected by the validator
itself, regardless of who signs, what redeemer is supplied, what datum is
attached, or what time window the tx claims.

This is the destination for surrendered legacy assets in the cMATRA
merger. When a user surrenders AGENT / SHARDS / FLUX_PASS / SE_BRAWLERS /
BRAWL_PASS_ETD / T1_ADAM_PASS / T2_ADAM_PASS, those assets move into a
UTxO at this address. Because the validator has no spending path, the
assets are permanently locked.

**No admin recovery.** Not Server A, not Server B, not the deployer, not
anyone. There is no key — the lock is enforced by Cardano consensus.

This is intentionally stronger than the "admin holds the quarantine key
and promises not to spend" alternative. The doctrine principle: avoid
trust assumptions that aren't enforced by code. Surrendered assets must
go somewhere unspendable, and the only way to make that claim
cryptographically verifiable is to use a validator that rejects every
spend.

## Properties

- **Unparameterized.** Identical script hash on mainnet, preprod, and
  preview networks. Same bech32 prefix per network, different network ID
  byte.
- **No datum requirement.** Surrender txs pay arbitrary datum here (or
  none), since the validator ignores it.
- **No redeemer requirement.** Hypothetical spend attempts can pass any
  redeemer; all are rejected.
- **No time window.** No deadline, no admin-recovery path. Forever.

## Build + verify

```bash
# Audited compiler version. Different versions produce different hashes.
aiken --version   # must print v1.1.21

aiken build       # writes plutus.json
aiken check       # all 3 tests must pass

bash verify_build.sh  # rebuilds, prints script hash for cross-check
```

## Deployed addresses

| Network | Script address | Source |
|---|---|---|
| Mainnet | `addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl` | [`quarantine.mainnet.addr`](quarantine.mainnet.addr) |
| Preprod | `addr_test1wq5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqxrttf6` | [`quarantine.preprod.addr`](quarantine.preprod.addr) |
| Script hash | `288fea77a0f674c6080aadd8ed3ca42cd5a920bf1f00c0d3e63306e4` | [`quarantine.script_hash`](quarantine.script_hash) |

These are committed to the repo as artifacts of `aiken build` + `aiken
blueprint address` at the audited Aiken version (`v1.1.21`). Anyone can
re-derive them from source:

```bash
cd onchain/quarantine_validator
aiken build                          # produces plutus.json (committed for convenience)
aiken blueprint hash                 # → 288fea77…3306e4
aiken blueprint address --mainnet    # → addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl
aiken blueprint address              # preprod/testnet form
```

Or use `cardano-cli` against the included `quarantine.plutus` envelope:

```bash
cardano-cli latest address build --payment-script-file quarantine.plutus --mainnet
```

The bech32 address is what `surrender_api.py` reads from
`QUARANTINE_ADDRESS` in env, and what users see as the destination of
their surrendered assets on a block explorer.

## Why this is auditable

Any third party can:

1. Check out this repo at the deployed commit.
2. Run `aiken --version` to confirm `v1.1.21`.
3. Run `aiken build`.
4. Run `verify_build.sh` to print the script hash.
5. Run `cardano-cli latest address build --payment-script-file plutus.json --mainnet`.
6. Compare the resulting bech32 address against the `QUARANTINE_ADDRESS`
   in this repo's `.env.example` (or whichever live env it points at) and
   against the address visible on a block explorer for surrender txs.

If those match, the surrendered assets are demonstrably unrecoverable.
