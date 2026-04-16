# Delegation Rewards Distribution Tool

Distributes cMATRA from the **150M validator reserve** (15% of total supply) to delegators of participating Cardano SPOs via cross-validation (Minotaur).

## How it works

1. **Snapshot** — queries Blockfrost for all delegators of a pool at a given epoch, resolves payment addresses, calculates proportional rewards
2. **Distribute** — builds batched `cardano-cli conway` transactions to mint (or send pre-minted) cMATRA to each delegator

The tool uses the repo's shared native mint policy at `../onchain/flux_mint_policy/mint_policy.json`.

## Setup

```bash
cd delegation-rewards
npm install
cp .env.example .env
# Edit .env with your Blockfrost API key, policy ID, etc.
```

## Usage

```bash
# Preview rewards for an epoch (read-only, no writes)
npx tsx src/cli.ts snapshot --epoch 530 --dry-run

# Snapshot and save to ledger
npx tsx src/cli.ts snapshot --epoch 530

# Build + sign + submit mint+distribute TXs
npx tsx src/cli.ts distribute --epoch 530 --mint --utxo <txhash#idx>

# Build TX files only (for offline signing)
npx tsx src/cli.ts distribute --epoch 530 --utxo <txhash#idx> --build-only

# Show distribution history
npx tsx src/cli.ts status
```

## Reward calculation

```
reward_per_delegator = (delegator_ada / 1000) * rate_per_1000_ada
```

- Rate is configurable via `REWARD_RATE` env var (cMATRA base units per 1000 ADA per epoch)
- All math uses bigint — no floating point
- Dust rewards (< 1 base unit) are filtered out

## Source: validator reserve

- **Pool**: 150,000,000 MATRA (15% of 1B total supply)
- **Rationale**: SPO delegators secure Materios via Minotaur cross-validation
- **Does NOT draw from** the 850M public redemption pool

## Tests

```bash
npx vitest run        # 36 tests across 5 suites
npx vitest --watch    # watch mode
```

## Architecture

| Module | Purpose |
|--------|---------|
| `config.ts` | Env-based configuration |
| `ledger.ts` | SQLite tracking (distributions, epochs, address cache) |
| `snapshot.ts` | Blockfrost delegator fetch + address resolution |
| `calculate.ts` | Proportional reward math (bigint) |
| `distribute.ts` | cardano-cli TX build/sign/submit |
| `mint.ts` | Native script mint + distribute in single TX |
| `cli.ts` | CLI entrypoint |
