# FLUX Token Merger

**AGENT + SHARDS -> FLUX** on Cardano.

A deterministic, auditable pipeline that consolidates two legacy Cardano native assets into a single new token using value-weighted allocation and a signature-gated claim vault.

| Token | Policy | Decimals |
|-------|--------|----------|
| AGENT (Talos) | `97bbb7db...174bec` | 0 |
| SHARDS | `ea153b5d...15b243a` | 6 |
| **FLUX** (new) | minted at merge | 6 |

**FLUX max supply:** 1,000,000,000 (1e15 base units)

---

## Architecture

```
  TapTools API          Blockfrost API
       |                      |
  TWAP prices           holder snapshots
       |                      |
       v                      v
  +----------+   +------------------------+
  | Phase 1  |   | Phase 3                |
  | TWAP &   |-->| Snapshot + Allocation  |
  | Pools    |   | (integer math)         |
  +----------+   +------------------------+
       |                      |
       v                      v
  +----------+   +------------------------+
  | Phase 2  |   | Phase 4                |
  | Merge    |   | (reserved)             |
  | Valuation|   |                        |
  +----------+   +------------------------+
                              |
                              v
               +--------------+-----------+
               |                          |
          +----------+             +----------+
          | Phase 5  |             | Phase 6  |
          | Mint     |             | Build    |
          | FLUX     |             | Claim    |
          +----------+             | UTxOs    |
                                   +----------+
                                        |
                                        v
                                   +----------+
                                   | Phase 7  |
                                   | Build    |
                                   | Index    |
                                   +----------+
                                        |
                                        v
                                   +----------+
                                   | Phase 8  |
                                   | Claim    |
                                   | Client   |
                                   +----------+
```

### On-chain components

- **Claim Validator** (Aiken, Plutus V3) -- minimal signature-gated lockbox. Datum encodes a `VerificationKeyHash`; only that signer can spend the UTxO.
- **FLUX Mint Policy** -- native script with admin signature + timelock to enforce one-time mint.

### Off-chain pipeline

Six deterministic tools (each a standalone CLI) produce a reproducible audit pack:

1. **TWAP & Pool Selection** -- multi-pool time-weighted average pricing
2. **Merge Valuation** -- value-based token weighting with integer bucket allocation
3. **Snapshot & Allocation** -- holder scan, script-address exclusion, floor-division allocation
4. **Claim UTxO Builder** -- batched transactions locking FLUX at the script address
5. **Claim Index Builder** -- maps payment-key-hash to UTxO references
6. **Claim Client** -- index-based claiming (no full script-address scan)

---

## Progress

### Mainnet pipeline (Phases 1-3)

| Phase | Tool | Status | Artifact |
|-------|------|--------|----------|
| 1 | TWAP & Pool Selection | Done | `audit_pack/2026-02-11/twap_report.json` |
| 2 | Merge Valuation | Done | `audit_pack/2026-02-11/merge_report.json` |
| 3 | Snapshot & Allocation | Done | `audit_pack/2026-02-11/allocations_flux.csv` |
| 4 | *(reserved)* | -- | -- |
| 5 | Mint FLUX | Blocked on governance approval | -- |
| 6 | Build Claim UTxOs | Blocked on Phase 5 | -- |
| 7 | Build Claim Index | Blocked on Phase 6 | -- |
| 8 | Claim Client | Blocked on Phase 7 | -- |

### Preprod rehearsal (Phases 5-9)

Full end-to-end deployment on Cardano preprod testnet with synthetic data.

| Stage | Description | Status |
|-------|-------------|--------|
| 1 | Wallet generation (admin + 20 test wallets) | Done |
| 2 | Mint AgentTest (1B) + ShardsTest (3T) | Done |
| 3 | Adversarial token distribution (20 wallets) | Done |
| 4 | Synthetic allocation CSV | Done |
| 5 | Mint FLUX_TEST (1Q) with timelock | Done |
| 6 | Deploy claim validator + build 20 claim UTxOs | Done |
| 7 | Build claim index (20 keyhashes) | Done |
| 8 | Happy-path claims (5/5 successful) | Done |
| 9 | Red-team adversarial tests (6/6 pass) | Done |

**Preprod validator:** `addr_test1wplwxwujdq6t6lvc8j5agv7wurxpjx8dt094779t09whq4chqhwe6`
Script hash: `7ee33b926834bd7d983ca9d433cee0cc1918ed5bcb5f78ab795d7057`

### Red-team results

| Test | Attack vector | Result |
|------|---------------|--------|
| Wrong signer | Attacker signs victim's UTxO | PASS (rejected) |
| Double claim | Re-spend consumed UTxO | PASS (rejected) |
| Wrong redeemer | Garbage CBOR redeemer data | PASS (rejected) |
| Datum swap | Attacker substitutes own pkh in datum | PASS (rejected) |
| Index poisoning | Fabricated UTxO reference | PASS (rejected) |

### Test suite

125 unit/integration tests covering all tools, utilities, and E2E pipeline flow.

```
tests/test_api_clients.py        13 tests
tests/test_build_claim_index.py   6 tests
tests/test_build_claim_utxos.py  12 tests
tests/test_cardano_utils.py      12 tests
tests/test_claim_flux_indexed.py 13 tests
tests/test_config.py             10 tests
tests/test_e2e_pipeline.py       15 tests
tests/test_flux_merge_valuation.py 11 tests
tests/test_snapshot_allocate.py  14 tests
tests/test_twap_snapshot_pools.py 19 tests
```

---

## Quick start

### Prerequisites

- Python 3.10+
- [Aiken](https://aiken-lang.org/) v1.1+ (for on-chain contract compilation)

### Setup

```bash
git clone https://github.com/Flux-Point-Studios/flux-merger.git
cd flux-merger

python -m venv .venv
source .venv/bin/activate    # or .venv\Scripts\activate on Windows

pip install -e ".[dev]"

cp .env.example env.local
# Edit env.local with your API keys
```

### Environment variables

```bash
NETWORK=mainnet                 # mainnet | preprod | preview
BLOCKFROST_PROJECT_ID=mainnet...
BLOCKFROST_PROJECT_ID_PREPROD=preprod...
TAP_TOOLS_API_KEY=...
```

### Run tests

```bash
pytest
```

### Run the pipeline

```bash
# Phase 1: TWAP report
python -m tools.twap_snapshot_pools \
  --interval 1h --num-intervals 168 \
  --top-pools 3 --min-tvl-ada 10000 \
  --combine median --quote USD \
  --out audit_pack/YYYY-MM-DD/twap_report.json

# Phase 2: Merge valuation
python -m tools.flux_merge_valuation_int \
  --primary-window 7d --quote USD \
  --out-json audit_pack/YYYY-MM-DD/merge_report.json

# Phase 3: Snapshot + allocation
python -m tools.snapshot_allocate_flux \
  --merge-report audit_pack/YYYY-MM-DD/merge_report.json \
  --exclude-script-addresses \
  --denominator-mode eligible \
  --out audit_pack/YYYY-MM-DD/allocations_flux.csv \
  --out-summary audit_pack/YYYY-MM-DD/allocations_summary.json

# Phase 6: Build claim vault
python -m tools.build_claim_utxos_flux \
  --allocations-csv audit_pack/YYYY-MM-DD/allocations_flux.csv \
  --funding-skey payment.skey \
  --script-address <claim_script_addr> \
  --flux-policy <policy_hex> \
  --batch-size 40

# Phase 7: Build claim index
python -m tools.build_flux_claim_index \
  --manifest audit_pack/YYYY-MM-DD/claim_tx_out/manifest.json

# Phase 8: Claim (per user)
python -m tools.claim_flux_indexed \
  --index-file audit_pack/YYYY-MM-DD/claim_index_min.json \
  --blueprint onchain/claim_validator/plutus.json \
  --payment-skey payment.skey \
  --submit
```

### Run preprod rehearsal

```bash
NETWORK=preprod python -m scripts.preprod_harness
# Resume from any stage:
NETWORK=preprod python -m scripts.preprod_harness --skip-to-stage 6
```

### Run red-team tests

```bash
NETWORK=preprod python -m scripts.red_team
```

---

## Key design decisions

### Value-based merge weights
Each legacy token's share of FLUX is proportional to its market value:
- `weight_i = (TWAP_price_i * supply_i) / total_valuation`
- Integer bucket: `B_i = floor(weight_i * 1e15)`, remainder to last token

### Integer-only allocation
No floating point in allocation math. For each address:
- `alloc_i(addr) = floor(balance_i * B_i / eligible_supply_i)`
- Dust from floor rounding is tracked and published

### Inline datum claim vault
Each claim UTxO has an inline datum encoding the claimant's payment verification key hash. The Plutus V3 validator checks `list.has(tx.extra_signatories, datum.claimant_pkh)` -- only the authorized keyholder can spend.

### Multi-pool TWAP
Price manipulation resistance via:
- Top N pools by TVL (default: 3)
- Minimum TVL threshold (default: 10,000 ADA)
- Median combination across pools
- 7-day window with 1h candles (168 data points)

### Burn adjustment
88,551 SHARDS + 4,002 AGENT permanently locked in a dead script wallet (`$burnsnek`) are subtracted from eligible supply before allocation.

---

## Repository structure

```
flux-merger/
  onchain/
    claim_validator/
      validators/claim_validator.ak    # Aiken Plutus V3 validator
      plutus.json                      # compiled blueprint
      aiken.toml
  tools/
    config.py                          # shared config + env loading
    api_clients.py                     # Blockfrost + TapTools clients
    cardano_utils.py                   # address parsing, datum encoding
    twap_snapshot_pools.py             # Phase 1: TWAP
    flux_merge_valuation_int.py        # Phase 2: valuation
    snapshot_allocate_flux.py          # Phase 3: snapshot + allocation
    build_claim_utxos_flux.py          # Phase 6: claim vault builder
    build_flux_claim_index.py          # Phase 7: index builder
    claim_flux_indexed.py              # Phase 8: claim client
  scripts/
    preprod_harness.py                 # 9-stage preprod rehearsal
    red_team.py                        # adversarial test suite
  tests/                               # 125 tests
  audit_pack/
    2026-02-11/                        # mainnet artifacts (Phases 1-3)
    preprod/                           # preprod rehearsal state + data
  .env.example
  pyproject.toml
```

---

## Appendix: Code reference

### On-chain contract

| File | Line | Description |
|------|------|-------------|
| [`onchain/claim_validator/validators/claim_validator.ak`](onchain/claim_validator/validators/claim_validator.ak) | 15-18 | `ClaimDatum` type definition (`claimant_pkh: VerificationKeyHash`) |
| [`onchain/claim_validator/validators/claim_validator.ak`](onchain/claim_validator/validators/claim_validator.ak) | 21-33 | `claim_validator.spend` -- core validation logic |
| [`onchain/claim_validator/plutus.json`](onchain/claim_validator/plutus.json) | -- | Compiled Plutus V3 blueprint (script hash: `7ee33b92...`) |

### Shared infrastructure

| File | Line | Description |
|------|------|-------------|
| [`tools/config.py`](tools/config.py) | 33-36 | Network selection (`NETWORK` env var) |
| [`tools/config.py`](tools/config.py) | 42-45 | Network-aware Blockfrost project ID resolution |
| [`tools/config.py`](tools/config.py) | 72-75 | FLUX supply constants (`1e15` base units) |
| [`tools/config.py`](tools/config.py) | 101-129 | `TokenInfo` dataclass + `AGENT`/`SHARDS` definitions |
| [`tools/api_clients.py`](tools/api_clients.py) | 33-80 | `_request_with_retry` -- backoff + retry for API calls |
| [`tools/api_clients.py`](tools/api_clients.py) | 82-181 | `BlockfrostClient` -- asset info, holders, UTxOs, tx submission |
| [`tools/api_clients.py`](tools/api_clients.py) | 183-254 | `TapToolsClient` -- pool discovery, OHLCV candles, ADA/USD |
| [`tools/cardano_utils.py`](tools/cardano_utils.py) | 29-50 | `address_to_payment_key_hash` -- bech32 address parsing |
| [`tools/cardano_utils.py`](tools/cardano_utils.py) | 51-58 | `is_script_address` -- script vs pubkey address detection |
| [`tools/cardano_utils.py`](tools/cardano_utils.py) | 73-88 | `encode_claim_datum` -- CBOR datum encoding |
| [`tools/cardano_utils.py`](tools/cardano_utils.py) | 107-116 | `derive_script_address` -- from script hash to bech32 |

### Phase 1: TWAP & Pool Selection

| File | Line | Description |
|------|------|-------------|
| [`tools/twap_snapshot_pools.py`](tools/twap_snapshot_pools.py) | 46-56 | `compute_twap` -- time-weighted average from candle array |
| [`tools/twap_snapshot_pools.py`](tools/twap_snapshot_pools.py) | 58-74 | `combine_twaps` -- median / deepest / liquidity-weighted modes |
| [`tools/twap_snapshot_pools.py`](tools/twap_snapshot_pools.py) | 95-111 | `discover_pools` -- TVL filtering + top-N selection |
| [`tools/twap_snapshot_pools.py`](tools/twap_snapshot_pools.py) | 152-239 | `build_twap_report` -- full report assembly |

### Phase 2: Merge Valuation

| File | Line | Description |
|------|------|-------------|
| [`tools/flux_merge_valuation_int.py`](tools/flux_merge_valuation_int.py) | 39-43 | `fetch_supply` -- on-chain supply via Blockfrost |
| [`tools/flux_merge_valuation_int.py`](tools/flux_merge_valuation_int.py) | 50-76 | `compute_valuations` -- `V_i = price * supply` per token |
| [`tools/flux_merge_valuation_int.py`](tools/flux_merge_valuation_int.py) | 83-109 | `compute_integer_buckets` -- floor division, remainder to last token |
| [`tools/flux_merge_valuation_int.py`](tools/flux_merge_valuation_int.py) | 116-211 | `build_merge_report` -- orchestrates valuation + bucket computation |

### Phase 3: Snapshot & Allocation

| File | Line | Description |
|------|------|-------------|
| [`tools/snapshot_allocate_flux.py`](tools/snapshot_allocate_flux.py) | 37-47 | `fetch_holders` -- paginated asset holder scan |
| [`tools/snapshot_allocate_flux.py`](tools/snapshot_allocate_flux.py) | 49-67 | `filter_holders` -- script address exclusion + burn adjustment |
| [`tools/snapshot_allocate_flux.py`](tools/snapshot_allocate_flux.py) | 74-83 | `capture_snapshot_anchor` -- block hash/height/time |
| [`tools/snapshot_allocate_flux.py`](tools/snapshot_allocate_flux.py) | 90-129 | `allocate_flux` -- integer floor-division allocation |
| [`tools/snapshot_allocate_flux.py`](tools/snapshot_allocate_flux.py) | 136-298 | `run_snapshot_and_allocate` -- full pipeline orchestrator |

### Phase 6: Claim UTxO Builder

| File | Line | Description |
|------|------|-------------|
| [`tools/build_claim_utxos_flux.py`](tools/build_claim_utxos_flux.py) | 52-67 | `load_allocations` -- CSV parsing with payment key hash extraction |
| [`tools/build_claim_utxos_flux.py`](tools/build_claim_utxos_flux.py) | 74-106 | `build_claim_outputs` -- datum construction + script output building |
| [`tools/build_claim_utxos_flux.py`](tools/build_claim_utxos_flux.py) | 119-191 | `build_batch_tx_cbor` -- batched transaction CBOR generation |
| [`tools/build_claim_utxos_flux.py`](tools/build_claim_utxos_flux.py) | 198-292 | `build_claim_vault` -- full vault orchestrator with manifest |

### Phase 7: Claim Index Builder

| File | Line | Description |
|------|------|-------------|
| [`tools/build_flux_claim_index.py`](tools/build_flux_claim_index.py) | 34-178 | `build_index_from_manifest` -- tx UTxO querying + datum matching |
| [`tools/build_flux_claim_index.py`](tools/build_flux_claim_index.py) | 185-193 | `write_index_csv` -- CSV export of index |

### Phase 8: Claim Client

| File | Line | Description |
|------|------|-------------|
| [`tools/claim_flux_indexed.py`](tools/claim_flux_indexed.py) | 56-122 | `verify_claim_utxo` -- address + datum + asset verification |
| [`tools/claim_flux_indexed.py`](tools/claim_flux_indexed.py) | 129-177 | `find_claimable_utxos` -- index lookup + on-chain verification |
| [`tools/claim_flux_indexed.py`](tools/claim_flux_indexed.py) | 179-259 | `build_claim_tx` -- Plutus V3 script input + redeemer construction |
| [`tools/claim_flux_indexed.py`](tools/claim_flux_indexed.py) | 274-292 | `load_script_from_blueprint` -- Aiken blueprint parsing |

### Preprod rehearsal

| File | Line | Description |
|------|------|-------------|
| [`scripts/preprod_harness.py`](scripts/preprod_harness.py) | 284-353 | `distribute_test_tokens` -- batch distribution with fresh context per batch |
| [`scripts/preprod_harness.py`](scripts/preprod_harness.py) | 460-464 | `mint_flux_test` -- timelock native script mint |
| [`scripts/preprod_harness.py`](scripts/preprod_harness.py) | 561-633 | `build_claim_utxos` -- preprod claim UTxO batching |
| [`scripts/preprod_harness.py`](scripts/preprod_harness.py) | 640-661 | `build_claim_index_from_batches` -- deterministic index from batch results |
| [`scripts/preprod_harness.py`](scripts/preprod_harness.py) | 673-730 | `claim_flux` -- claim transaction building with collateral |
| [`scripts/preprod_harness.py`](scripts/preprod_harness.py) | 732-786 | `red_team_wrong_signer` -- adversarial wrong-key test |
| [`scripts/preprod_harness.py`](scripts/preprod_harness.py) | 788-844 | `red_team_double_claim` -- adversarial double-spend test |

### Red-team suite

| File | Line | Description |
|------|------|-------------|
| [`scripts/red_team.py`](scripts/red_team.py) | 105-134 | `test_wrong_signer` -- wrong key claims victim's UTxO |
| [`scripts/red_team.py`](scripts/red_team.py) | 137-171 | `test_wrong_redeemer` -- garbage redeemer data |
| [`scripts/red_team.py`](scripts/red_team.py) | 174-204 | `test_datum_swap` -- attacker substitutes own pkh in datum |
| [`scripts/red_team.py`](scripts/red_team.py) | 207-236 | `test_index_poisoning` -- fabricated UTxO reference |

---

## Security model

The claim validator is intentionally minimal (38 lines of Aiken). The only validation rule:

```
list.has(tx.extra_signatories, datum.claimant_pkh)
```

**Why this is sufficient:**
- Each UTxO is keyed to exactly one payment key hash via inline datum
- Inline datums are authoritative on Cardano -- attackers cannot substitute their own
- The signer check prevents unauthorized spending
- Double-claims are prevented by the UTXO model itself (spent UTxOs cease to exist)
- No admin key, no backdoor, no governance -- pure lockbox

**Tested adversarial scenarios:**
- Wrong signer (rejected by validator)
- Datum swap (rejected -- inline datums are on-chain authoritative)
- Double claim (rejected -- UTxO already consumed)
- Index poisoning (rejected -- UTxO doesn't exist on chain)
- Garbage redeemer (rejected at CBOR deserialization)

---

## License

Private. (c) Flux Point Studios.
