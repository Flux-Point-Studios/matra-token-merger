#!/usr/bin/env tsx
import { Command } from 'commander';
import { loadConfig } from './config.js';
import { Ledger } from './ledger.js';
import { fetchPoolDelegators, resolveAllPaymentAddresses, getCurrentEpoch } from './snapshot.js';
import { calculateRewards, filterDustRewards, summarize } from './calculate.js';
import { batchAllocations, buildDistributionCommand, buildSignCommand, buildSubmitCommand, buildTxIdCommand } from './distribute.js';
import { buildMintDistributeCommand, buildMintSignCommand } from './mint.js';
import { execSync } from 'child_process';

const program = new Command();

program
  .name('delegation-rewards')
  .description('Cardano SPO delegation rewards distribution tool for Materios')
  .version('1.0.0');

// ─── snapshot ────────────────────────────────────────────────────────────────

program
  .command('snapshot')
  .description('Snapshot delegators and calculate rewards for an epoch')
  .option('-e, --epoch <number>', 'epoch number (defaults to current - 2)')
  .option('--dry-run', 'print summary without saving to ledger')
  .action(async (opts) => {
    const config = loadConfig({ blockfrostApiKey: process.env.BLOCKFROST_API_KEY || '' });
    const ledger = new Ledger(config.dbPath);
    ledger.init();

    try {
      let epoch = opts.epoch ? parseInt(opts.epoch) : undefined;
      if (!epoch) {
        const current = await getCurrentEpoch(config.blockfrostApiKey);
        epoch = current - 2; // use completed epoch (2 behind for safety)
        console.log(`Current epoch: ${current}, snapshotting epoch ${epoch}`);
      }

      if (ledger.isEpochDistributed(epoch)) {
        console.log(`Epoch ${epoch} already distributed. Skipping.`);
        return;
      }

      console.log(`Fetching delegators for pool ${config.poolId} at epoch ${epoch}...`);
      const delegators = await fetchPoolDelegators(epoch, config.poolId, config.blockfrostApiKey);
      console.log(`Found ${delegators.length} delegators`);

      if (delegators.length === 0) {
        console.log('No delegators found. Nothing to do.');
        return;
      }

      console.log('Resolving payment addresses...');
      const cache = {
        get: (s: string) => ledger.getCachedAddress(s),
        set: (s: string, a: string) => ledger.cacheAddress(s, a),
      };
      const resolved = await resolveAllPaymentAddresses(delegators, config.blockfrostApiKey, cache);

      console.log('Calculating rewards...');
      const allocations = calculateRewards(resolved, config.rewardRate);
      const filtered = filterDustRewards(allocations, 1n);
      const summary = summarize(filtered);

      console.log(`\n── Epoch ${epoch} Summary ──`);
      console.log(`  Delegators:    ${summary.recipientCount}`);
      console.log(`  Total staked:  ${(summary.totalAdaDelegated / 1_000_000n).toLocaleString()} ADA`);
      console.log(`  Total rewards: ${summary.totalCmatra.toLocaleString()} cMATRA`);
      console.log(`  Rate:          ${config.rewardRate.toString()} cMATRA per 1000 ADA\n`);

      if (opts.dryRun) {
        console.log('Dry run — not saving to ledger.');
        for (const a of filtered) {
          console.log(`  ${a.paymentAddress}  ${a.cMatraReward} cMATRA  (${(a.lovelaceStaked / 1_000_000n)} ADA)`);
        }
        return;
      }

      ledger.startEpoch(
        epoch,
        summary.recipientCount,
        summary.totalAdaDelegated.toString(),
        summary.totalCmatra.toString(),
        config.rewardRate.toString(),
      );
      ledger.saveAllocations(epoch, filtered.map(a => ({
        stakeAddress: a.stakeAddress,
        paymentAddress: a.paymentAddress,
        lovelaceStaked: a.lovelaceStaked,
        cMatraReward: a.cMatraReward,
      })));
      console.log(`Saved ${filtered.length} allocations to ledger.`);
    } finally {
      ledger.close();
    }
  });

// ─── distribute ──────────────────────────────────────────────────────────────

program
  .command('distribute')
  .description('Build, sign, and submit distribution TXs for a pending epoch')
  .option('-e, --epoch <number>', 'epoch number')
  .option('--mint', 'mint tokens in the same TX (requires policy script + skey)')
  .option('--utxo <utxo>', 'UTxO to use as input (txhash#index)')
  .option('--build-only', 'build TX files but do not sign/submit')
  .action(async (opts) => {
    const config = loadConfig({
      blockfrostApiKey: process.env.BLOCKFROST_API_KEY || '',
      changeAddress: process.env.CHANGE_ADDRESS || '',
    });

    if (!config.changeAddress) {
      console.error('CHANGE_ADDRESS is required for distribution.');
      process.exit(1);
    }

    const ledger = new Ledger(config.dbPath);
    ledger.init();

    try {
      const epoch = opts.epoch ? parseInt(opts.epoch) : undefined;
      if (!epoch) {
        console.error('--epoch is required for distribute.');
        process.exit(1);
      }

      const pending = ledger.getPendingAllocations(epoch);
      if (pending.length === 0) {
        console.log(`No pending allocations for epoch ${epoch}.`);
        return;
      }

      console.log(`Distributing ${pending.length} allocations for epoch ${epoch}...`);
      const batches = batchAllocations(pending, config.maxOutputsPerTx);
      console.log(`Split into ${batches.length} batches of max ${config.maxOutputsPerTx}`);

      const txIns = opts.utxo ? [opts.utxo] : [];
      if (txIns.length === 0) {
        console.error('--utxo is required. Query UTxOs with: cardano-cli conway query utxo --address <addr> --mainnet');
        process.exit(1);
      }

      for (let i = 0; i < batches.length; i++) {
        const batch = batches[i];
        const unsignedPath = `/tmp/delegation-epoch${epoch}-batch${i}.unsigned`;
        const signedPath = `/tmp/delegation-epoch${epoch}-batch${i}.signed`;

        let cmd: string[];
        if (opts.mint) {
          cmd = buildMintDistributeCommand({
            txIns,
            allocations: batch,
            policyId: config.policyId,
            policyScriptPath: config.policyScriptPath,
            tokenNameHex: config.tokenNameHex,
            minUtxoLovelace: config.minUtxoLovelace,
            changeAddress: config.changeAddress,
            networkFlag: config.networkFlag,
            outFile: unsignedPath,
          });
        } else {
          cmd = buildDistributionCommand({
            txIns,
            allocations: batch,
            policyId: config.policyId,
            tokenNameHex: config.tokenNameHex,
            minUtxoLovelace: config.minUtxoLovelace,
            changeAddress: config.changeAddress,
            networkFlag: config.networkFlag,
            outFile: unsignedPath,
          });
        }

        console.log(`\nBatch ${i}: Building TX for ${batch.length} recipients...`);
        console.log(`  ${config.cardanoCliPath} ${cmd.join(' ')}`);

        if (opts.buildOnly) {
          console.log(`  → Written to ${unsignedPath}`);
          continue;
        }

        // Build
        execSync(`${config.cardanoCliPath} ${cmd.join(' ')}`, { stdio: 'inherit' });

        // Sign
        let signCmd: string[];
        if (opts.mint) {
          signCmd = buildMintSignCommand(
            unsignedPath,
            signedPath,
            config.paymentSKeyPath,
            config.policySKeyPath,
            config.networkFlag,
          );
        } else {
          signCmd = buildSignCommand(unsignedPath, signedPath, config.paymentSKeyPath, config.networkFlag);
        }
        execSync(`${config.cardanoCliPath} ${signCmd.join(' ')}`, { stdio: 'inherit' });

        // Submit
        const submitCmd = buildSubmitCommand(signedPath, config.networkFlag);
        execSync(`${config.cardanoCliPath} ${submitCmd.join(' ')}`, { stdio: 'inherit' });

        // Get TX hash
        const txIdCmd = buildTxIdCommand(signedPath);
        const txHash = execSync(`${config.cardanoCliPath} ${txIdCmd.join(' ')}`).toString().trim();
        console.log(`  → TX submitted: ${txHash}`);

        ledger.markBatchSubmitted(epoch, i, txHash);
      }

      ledger.markEpochCompleted(epoch);
      console.log(`\nEpoch ${epoch} distribution complete.`);
    } finally {
      ledger.close();
    }
  });

// ─── status ──────────────────────────────────────────────────────────────────

program
  .command('status')
  .description('Show distribution history and pending epochs')
  .action(() => {
    const config = loadConfig({ blockfrostApiKey: 'dummy' });
    const ledger = new Ledger(config.dbPath);
    ledger.init();

    try {
      const history = ledger.getDistributionHistory();
      if (history.length === 0) {
        console.log('No distribution history found.');
        return;
      }

      console.log('── Distribution History ──\n');
      for (const e of history) {
        const status = e.status === 'completed' ? '✓' : e.status === 'in_progress' ? '⏳' : '⏸';
        console.log(`  ${status} Epoch ${e.epoch}`);
        console.log(`    Delegators: ${e.totalDelegators}`);
        console.log(`    Staked:     ${BigInt(e.totalLovelaceStaked) / 1_000_000n} ADA`);
        console.log(`    Rewards:    ${e.totalCmatraDistributed} cMATRA`);
        console.log(`    Rate:       ${e.rewardRate} per 1000 ADA`);
        console.log(`    Status:     ${e.status}`);
        if (e.completedAt) console.log(`    Completed:  ${e.completedAt}`);
        console.log();
      }
    } finally {
      ledger.close();
    }
  });

program.parse();
