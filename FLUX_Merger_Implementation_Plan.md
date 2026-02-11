# FLUX Token Merger (AGENT + SHARDS → FLUX) — Implementation Plan

> Purpose: a clean, auditable token consolidation on Cardano that avoids “sale” framing and minimizes manipulation/ops risk.

This document refactors the original brainstorm into a single, non-repetitive implementation plan you can hand to an implementation agent (Claude Code). fileciteturn0file0

---

## 0) Scope + non-goals

### In scope
- Merge two legacy Cardano native assets into a new fungible token **FLUX**:
  - **AGENT (Talos)** unit: `97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec54616c6f73`  
    Decimals: **0**
  - **SHARDS** unit: `ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a0014df10536861726473`  
    Decimals: **6**
- New token:
  - Name/Ticker: **FLUX**
  - Max supply: **1,000,000,000 FLUX**
  - Decimals: **6** (display convention; enforced via registry/metadata, not ledger rules)
  - Base units supply: `1_000_000_000 * 10^6 = 1_000_000_000_000_000` (1e15)

- Produce a reproducible “audit pack”:
  - price/TWAP report + pool selection evidence
  - snapshot anchor (block hash/height/time)
  - allocation CSV (address/keyhash → FLUX units)
  - claim-vault manifest (txids + claimant list)
  - claim index (keyhash → UTxO refs)

- Distribute FLUX via a **claim vault**:
  - Distributor creates many script UTxOs containing FLUX + min ADA
  - Each UTxO has an inline datum encoding claimant payment key hash
  - Claimant spends only their UTxO(s) with their signature

### Explicit non-goals (for this iteration)
- “Portal” UI / website
- On-chain TWAP oracle or on-chain pricing enforcement
- Automatic LP-holder attribution unless you choose to build it (covered as an optional module)

---

## 1) High-level architecture (boring on purpose)

### Data sources
- **TapTools API**: price candles (OHLCV), pool discovery, ADA/USD quote
- **Blockfrost API**: on-chain holder sets and tx UTxOs, snapshot anchor (latest block), tx submit (via pycardano)

### Off-chain pipeline (deterministic artifacts)
1) **TWAP & valuation snapshot**
2) **Supply normalization** (exact base-unit supplies)
3) **Merge weights → FLUX buckets** (integer bucket allocation across tokens)
4) **Holder snapshot** (addresses + quantities per asset)
5) **Allocation** (address/keyhash → FLUX base units) using **pure integer math**
6) **Claim-vault tx builder** (batched script outputs with inline datum)
7) **Index builder** (manifest → keyhash→UTxO refs)
8) **Claimer client** (uses index; no full script-address scan)

### On-chain components
- FLUX minting policy (enforce one-time mint / max supply)
- Claim validator (Aiken): “only the claimant’s key can spend this UTxO”

---

## 2) Critical decisions (commit these before you run anything)

### 2.1 Snapshot definition
- Choose a **UTC timestamp T** and publish it before execution.
- Operationally, you “snapshot” at a **block**:
  - `snapshot_block = first block with block.timestamp >= T`
  - With Blockfrost you typically capture “latest” at time of run; record the returned block hash/height/time in artifacts.

**Constraint:** Blockfrost does not provide historical “asset holder set at block X” queries. Treat “run time + anchored latest block” as the snapshot.

### 2.2 TWAP window + anti-manipulation
Recommended default:
- **Primary TWAP window:** **7 days** ending at snapshot time
- Candle interval: **1h** (168 candles)
- Use multiple pools and combine using a manipulation-resistant aggregator:
  - Select **top N pools by TVL** (default N=3)
  - Ignore pools below **min TVL** threshold (default **10,000 ADA**)
  - Combine per-pool TWAPs via **median** (default)

Also compute and publish:
- 24h TWAP (15m candles) and 30d TWAP (4h candles) as sanity checks (not used for allocation unless pre-committed).

### 2.3 Valuation model
Use a **value-based merge** (not a vibes-based ratio):
- For each token *i*:
  - `P_i` = primary-window TWAP price (quoted in USD preferred)
  - `S_i` = eligible supply (base units → display units using known decimals)
  - `V_i = P_i * S_i` (valuation)
- Total valuation: `V_total = Σ V_i`
- Weight: `w_i = V_i / V_total`

### 2.4 Integer-only distribution rule (no floating point in allocations)
Once you’ve fixed token buckets, everything becomes deterministic integers:

- Let:
  - `B_i` = FLUX bucket for token *i* in **FLUX base units** (integer)
  - `S_i` = token *i* eligible supply in **token base units** (integer)
  - `b_i(addr)` = address balance for token *i* in base units (integer)

Allocation from token *i*:
- `alloc_i(addr) = floor( b_i(addr) * B_i / S_i )`

Total allocation:
- `alloc(addr) = Σ alloc_i(addr)`

**Dust:** floor rounding creates leftover units. Decide up front:
- Sweep dust to treasury (`--dust-to`) **or**
- Leave undistributed and publish leftover totals

### 2.5 LP handling (choose one; don’t half-do it)
This is where merges go to die socially.

**Option A (recommended, more work): include LP holders by underlying**
- Compute each LP holder’s underlying AGENT/SHARDS exposure at snapshot
- Treat underlying balances like normal balances and allocate accordingly

**Option B (cheap, risky): exclude LP by forcing unwind**
- Publicly announce: “LP positions must be unwound before T to be counted”
- Expect volatility and complaints; TWAP may get weird if liquidity vanishes

If you exclude script addresses in the snapshot (recommended for safety), **LP is implicitly excluded** unless you implement Option A.

---

## 3) On-chain contracts

### 3.1 Claim validator (Aiken) — spec
**Datum**
- `ClaimDatum = Constr 0 [ bytes(payment_key_hash) ]`
  - bytes length: 28 (a payment key hash)

**Redeemer**
- Unit (ignored)

**Validation rules (minimum viable, safe)**
- Transaction must include the claimant key hash in `tx.extra_signatories`
  - i.e., the claimant must sign and the tx must declare required signer

That’s it. Keep it minimal; it’s a lockbox keyed by a signature.

**Optional hardening (nice-to-have)**
- Require that at least one output pays to a pubkey address with the same key hash (prevents weird “burn-by-claimant” mistakes).
- This is not required for security (claimant is the only spender anyway), but it improves UX correctness.

### 3.2 FLUX minting policy — spec
Goal: enforce max supply **1e15 base units** and prevent re-mint.

Cleanest approach on Cardano:
- A one-time mint policy requiring a specific signing key and/or time-lock,
- Mint exactly 1e15 units in a single tx,
- Policy rejects any mint after that (or requires a time window already expired).

Also prepare off-chain metadata:
- Token name/ticker/decimals/logo (Cardano Token Registry style)
- Decimals are a display convention; wallets/explorers rely on metadata/registry.

---

## 4) Repository/file layout (recommended)

```
/onchain
  /claim_validator
    claim_validator.ak
    plutus.json              # build artifact
    README.md                # datum/redeemer format + address derivation
  /flux_mint_policy
    mint_policy.ak (or native-script json if you prefer)
    metadata/
      token-registry.json    # decimals=6, ticker=FLUX, etc.

/tools
  twap_snapshot_pools.py
  flux_merge_valuation_int.py
  snapshot_allocate_flux.py
  build_claim_utxos_flux.py
  build_flux_claim_index.py
  claim_flux_indexed.py

/audit_pack/<YYYY-MM-DD>/
  twap_report.json
  merge_report.json
  allocations_flux.csv
  allocations_summary.json
  claim_manifest.json
  claim_index_min.json
  claim_index_full.json
```

---

## 5) Implementation steps (phased runbook)

### Phase 0 — Preconditions + rule freeze
Deliverables:
- Written “Methodology” section with:
  - snapshot timestamp T (UTC)
  - TWAP window + pool selection rules (min TVL, top N, combine median)
  - valuation formula and integer allocation rule
  - LP handling decision
  - dust handling decision
- Confirm old token supplies are not changing unexpectedly (mint/burn/treasury moves).

### Phase 1 — TWAP & pool selection report
Implement `twap_snapshot_pools.py`:
- Inputs:
  - token units (AGENT, SHARDS)
  - interval/numIntervals per window (24h, 7d, 30d)
  - pool filter (min TVL ADA)
  - combine mode (median/deepest/liquidity_weighted)
  - quote currency (USD recommended)
- Outputs:
  - JSON report:
    - selected pools (onchain ids), their TVL
    - per-pool candle window start/end
    - per-pool TWAP + close
    - combined TWAP per window
  - optional CSV exports of candles

Acceptance checks:
- Report includes ≥1 eligible pool per token (or explicitly flags fallback).
- “Deepest” vs “median” combined TWAPs aren’t wildly divergent (if they are, liquidity fragmentation is a red flag).

### Phase 2 — Merge weights → integer token buckets
Implement `flux_merge_valuation_int.py`:
- Inputs:
  - TWAP config (same as Phase 1; or consume Phase 1 output)
  - known decimals: AGENT=0, SHARDS=6
  - total FLUX base units: 1e15
- Computes:
  - exact supplies in base units via Blockfrost `/assets/{asset}` quantity (preferred)
  - valuations `V_i`
  - weights `w_i`
  - bucket sizes `B_i` (FLUX base units), floor for all but last token; remainder to last token to preserve total

Outputs:
- `merge_report.json` containing:
  - `weights_primary`
  - `flux_buckets_units` (B_i)
  - per-token supplies used
  - warning list

Acceptance checks:
- `Σ B_i == 1e15`
- Supplies are present and reasonable
- Warnings are empty or explicitly acknowledged

### Phase 3 — Holder snapshot
Implement `snapshot_allocate_flux.py` snapshot portion:
- Fetch holders via Blockfrost `/assets/{unit}/addresses` (paged).
- Capture snapshot anchor via `/blocks/latest` and store it.

Important decisions:
- `--exclude-script-addresses`:
  - Recommended to prevent pools/contracts from claiming.
- `--exclude-addresses` and `--script-whitelist`:
  - For known custodial/bridge addresses, or exceptions.

Outputs:
- raw holder maps per asset (optional, but great for auditing)
- `allocations_summary.json` includes:
  - total supply seen from holder list
  - eligible supply after exclusions
  - snapshot block hash/height/time

### Phase 4 — Allocation generation (integer math)
Implement `snapshot_allocate_flux.py` allocation portion:
- Inputs:
  - `merge_report.json` (B_i, optionally denominators)
  - eligible balances per token
  - denominator mode:
    - **eligible** (recommended): denom = sum(eligible balances) → ensures entire bucket distributed among included holders
    - report: denom = total supply baseline (will leave bucket partially undistributed if exclusions remove balances)
  - `--dust-to` treasury address (optional but recommended to make sums exact)

Outputs:
- `allocations_flux.csv` with at least:
  - address
  - payment_key_hash_hex (derived from bech32 payment credential when possible)
  - per-token balances (base + display)
  - per-token FLUX units contribution
  - total FLUX units + display
- Summary includes leftover per bucket (should be 0 if dust sweep enabled)

Acceptance checks:
- `Σ flux_total_units == 1e15` (if dust sweep enabled)
- rows with missing keyhash are understood (these cannot claim under the “signer” scheme)

### Phase 5 — Mint FLUX + prepare funding wallet
- Mint 1e15 base units under the FLUX policy to a funding address.
- Ensure funding wallet has enough ADA for:
  - min-UTxO ADA for each claim output
  - fees for batching txs
  - script collateral requirements

Acceptance checks:
- Funding address holds:
  - FLUX total supply
  - sufficient ADA headroom (measure by doing a pilot run of claim builder to estimate min ADA/output)

### Phase 6 — Build claim-vault UTxOs (distribution txs)
Implement `build_claim_utxos_flux.py`:
- Input: `allocations_flux.csv`
- Group by `payment_key_hash_hex` (so multiple addresses owned by same key hash become one claim)
- Create outputs at claim script address:
  - `Value = minAda + FLUX(qty)`
  - inline datum = `ClaimDatum(keyhash_bytes)`
- Batch with adaptive size (shrink on failure; grow on success)
- Output:
  - CBOR tx files per batch
  - `manifest.json` with txids + claimant list + quantities

Acceptance checks:
- Number of outputs equals number of claimants (after grouping + min threshold)
- Total FLUX locked at script == intended distributed total
- manifest is complete and deterministic

### Phase 7 — Build claim index (no-scan claims)
Implement `build_flux_claim_index.py`:
- Input: `manifest.json`
- For each txid, call Blockfrost `/txs/{hash}/utxos`
- Match script outputs by inline datum → claimant keyhash
- Output:
  - `claim_index_min.json` (keyhash → list of [tx_hash, output_index, flux_units])
  - `claim_index_full.json` (includes warnings/unmatched/missing)
  - `claim_index.csv`

Acceptance checks:
- `missing_keyhashes` empty
- `mismatches` empty
- `unmatched_outputs` empty or explained

### Phase 8 — Claim client (indexed)
Implement `claim_flux_indexed.py`:
- Input:
  - claimant `payment.skey`
  - `claim_index_min.json`
  - `plutus.json` blueprint
- Steps:
  - compute claimant pkh hex from skey
  - look up refs in index
  - fetch each tx’s outputs via Blockfrost `/txs/{hash}/utxos`
  - verify:
    - output address == claim script address
    - inline datum == expected datum CBOR hex
    - output contains FLUX asset
    - skip if spent (if `consumed_by_tx` available) or missing
  - build tx spending claim UTxOs:
    - include `required_signers = [pkh]` (critical)
    - pay FLUX to claimant address
- Output:
  - signed tx CBOR
  - optional submit

Acceptance checks:
- “check-only” mode reports correct totals for a known claimant
- full claim succeeds on mainnet for pilot claim
- client fails safely if index is tampered (datum/address mismatch)

### Phase 9 — Publish audit pack + operational comms
Publish (public or at least internally):
- snapshot block hash/height/time
- TWAP report and pool selection logic
- merge report (weights + buckets)
- allocation CSV (or hashed dataset + reproducibility scripts)
- claim index + claim instructions

---

## 6) Operational notes + sharp edges

### Blockfrost rate limits
- All holder scans are paginated and can be large.
- Implement backoff + retry on 429/5xx.
- Consider caching raw responses to disk during snapshot.

### Script addresses and “LP fairness”
- Excluding script addresses prevents pool contracts from claiming, but it also excludes LP-owned underlying unless you attribute LP properly.
- If you choose Option A (LP underlying attribution), implement it as a separate step:
  - snapshot LP token holders
  - compute underlying balances from pool reserves
  - add those balances into the eligible balance totals before allocation

### CEX/custodial addresses
- Decide policy:
  - treat as one claimant (exchange distributes internally), or
  - coordinate with exchange pre-snapshot
- If left as one claimant, they’ll receive one claim UTxO based on the payment key hash for that address (often not feasible). Better to coordinate.

### Decimals reality check
- Cardano ledger does not enforce “decimals”.
- Enforce display semantics through:
  - token registry metadata
  - consistent UI and docs
- All internal math uses **base units**.

### Rounding/dust policy
- Publish the rule and enforce it in scripts.
- Recommended: sweep dust to a treasury address and disclose amount.

---

## 7) Acceptance checklist (definition of done)

### Correctness
- [ ] `merge_report.json` exists and `sum(bucket_units) == 1e15`
- [ ] `allocations_flux.csv` exists and sums to 1e15 (with dust sweep)
- [ ] claim vault outputs exist on-chain, each:
  - has inline datum = claimant key hash CBOR
  - contains correct FLUX quantity
  - includes min ADA
- [ ] `claim_index_min.json` contains every claimant key hash exactly once (or clearly documented if multiple UTxOs per claimant)
- [ ] indexed claim client successfully claims for pilot wallet(s)

### Auditability
- [ ] Snapshot block recorded and published
- [ ] Pool selection and TWAP math reproducible from scripts
- [ ] Allocation deterministically reproducible from artifacts
- [ ] Scripts produce identical outputs when rerun with same inputs (excluding live API timestamps)

### Safety
- [ ] Claim validator only allows spending by designated key hash
- [ ] Claimer client verifies address + datum before spending
- [ ] Distributor wallet keys never leave secure environment; CBOR outputs are reviewed before submit

---

## 8) What Claude Code should implement (task list)

1) `onchain/claim_validator/claim_validator.ak`  
   - Implement minimal signature-gated spend based on datum keyhash.
   - Produce `plutus.json` blueprint and derived script address.

2) `onchain/flux_mint_policy/`  
   - Implement mint policy to mint exactly 1e15 once, then lock.
   - Provide token metadata docs and registry JSON.

3) `tools/twap_snapshot_pools.py`  
   - Pool discovery + TVL filtering + per-pool OHLCV + combined TWAPs.
   - JSON + optional CSV exports.

4) `tools/flux_merge_valuation_int.py`  
   - Supply fetch + weights + integer buckets.
   - Output merge report.

5) `tools/snapshot_allocate_flux.py`  
   - Holder scan + exclusions + bech32 payment credential extraction.
   - Integer allocation + dust sweep.
   - Output CSV + summary JSON.

6) `tools/build_claim_utxos_flux.py`  
   - Read allocation CSV → batch txs locking claims at script address.
   - Output manifest + CBOR files; optional submit.

7) `tools/build_flux_claim_index.py`  
   - Read manifest → build claim index mapping; validations.

8) `tools/claim_flux_indexed.py`  
   - Index-based claim client; check-only + submit modes.

9) `docs/operations_runbook.md`  
   - One-page “how to run the pipeline” with exact commands and outputs.

---

## 9) Minimal “run commands” cheat sheet (for the runbook)

Environment:
```bash
export TAPTOOLS_API_KEY="..."
export BLOCKFROST_PROJECT_ID="..."
```

1) TWAP report:
```bash
python tools/twap_snapshot_pools.py --interval 1h --num-intervals 168 --top-pools 3 --min-tvl-ada 10000 --combine median --quote USD --out audit_pack/.../twap_report.json
```

2) Merge report (weights + buckets):
```bash
python tools/flux_merge_valuation_int.py --primary-window 7d --quote USD --out-json audit_pack/.../merge_report.json
```

3) Snapshot + allocations:
```bash
python tools/snapshot_allocate_flux.py --merge-report audit_pack/.../merge_report.json --exclude-script-addresses --denominator-mode eligible --dust-to <treasury_addr> --out audit_pack/.../allocations_flux.csv --out-summary audit_pack/.../allocations_summary.json
```

4) Build claim vault:
```bash
python tools/build_claim_utxos_flux.py --allocations-csv audit_pack/.../allocations_flux.csv --funding-address <addr> --funding-skey payment.skey --script-address <claim_script_addr> --flux-policy <policyhex> --flux-asset-hex 464c5558 --batch-size 40 --out-dir audit_pack/.../claim_tx_out
```

5) Build claim index:
```bash
python tools/build_flux_claim_index.py --manifest audit_pack/.../claim_tx_out/manifest.json --out-min audit_pack/.../claim_index_min.json --out-full audit_pack/.../claim_index_full.json
```

6) Claim (indexed client):
```bash
python tools/claim_flux_indexed.py --index-file audit_pack/.../claim_index_min.json --blueprint onchain/claim_validator/plutus.json --payment-skey payment.skey --submit
```

---

End.
