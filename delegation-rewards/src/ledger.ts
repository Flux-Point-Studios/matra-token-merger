import Database from 'better-sqlite3';

export interface RewardAllocation {
  stakeAddress: string;
  paymentAddress: string;
  lovelaceStaked: bigint;
  cMatraReward: bigint;
}

export interface EpochSummary {
  epoch: number;
  totalDelegators: number;
  totalLovelaceStaked: string;
  totalCmatraDistributed: string;
  rewardRate: string;
  status: string;
  completedAt: string | null;
}

export class Ledger {
  private db: Database.Database;

  constructor(dbPath: string) {
    this.db = new Database(dbPath);
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('busy_timeout = 5000');
  }

  init(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS distributions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        epoch INTEGER NOT NULL,
        stake_address TEXT NOT NULL,
        payment_address TEXT NOT NULL,
        lovelace_staked TEXT NOT NULL,
        cmatra_reward TEXT NOT NULL,
        tx_hash TEXT,
        batch_index INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        confirmed_at TEXT
      );
      CREATE UNIQUE INDEX IF NOT EXISTS idx_dist_epoch_stake
        ON distributions(epoch, stake_address);

      CREATE TABLE IF NOT EXISTS epochs (
        epoch INTEGER PRIMARY KEY,
        total_delegators INTEGER NOT NULL,
        total_lovelace_staked TEXT NOT NULL,
        total_cmatra_distributed TEXT NOT NULL,
        reward_rate TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        started_at TEXT DEFAULT (datetime('now')),
        completed_at TEXT
      );

      CREATE TABLE IF NOT EXISTS address_cache (
        stake_address TEXT PRIMARY KEY,
        payment_address TEXT NOT NULL,
        last_updated TEXT NOT NULL DEFAULT (datetime('now'))
      );
    `);
  }

  isEpochDistributed(epoch: number): boolean {
    const row = this.db.prepare('SELECT status FROM epochs WHERE epoch = ?').get(epoch) as { status: string } | undefined;
    return row?.status === 'completed';
  }

  isEpochInProgress(epoch: number): boolean {
    const row = this.db.prepare('SELECT status FROM epochs WHERE epoch = ?').get(epoch) as { status: string } | undefined;
    return row?.status === 'in_progress' || row?.status === 'pending';
  }

  startEpoch(epoch: number, totalDelegators: number, totalLovelace: string, totalCmatra: string, rate: string): void {
    this.db.prepare(
      'INSERT OR REPLACE INTO epochs (epoch, total_delegators, total_lovelace_staked, total_cmatra_distributed, reward_rate, status) VALUES (?, ?, ?, ?, ?, ?)'
    ).run(epoch, totalDelegators, totalLovelace, totalCmatra, rate, 'in_progress');
  }

  saveAllocations(epoch: number, allocations: RewardAllocation[]): void {
    const insert = this.db.prepare(
      'INSERT INTO distributions (epoch, stake_address, payment_address, lovelace_staked, cmatra_reward) VALUES (?, ?, ?, ?, ?)'
    );
    const tx = this.db.transaction(() => {
      for (const a of allocations) {
        insert.run(epoch, a.stakeAddress, a.paymentAddress, a.lovelaceStaked.toString(), a.cMatraReward.toString());
      }
    });
    tx();
  }

  getPendingAllocations(epoch: number): RewardAllocation[] {
    const rows = this.db.prepare(
      "SELECT stake_address, payment_address, lovelace_staked, cmatra_reward FROM distributions WHERE epoch = ? AND status = 'pending'"
    ).all(epoch) as any[];
    return rows.map(r => ({
      stakeAddress: r.stake_address,
      paymentAddress: r.payment_address,
      lovelaceStaked: BigInt(r.lovelace_staked),
      cMatraReward: BigInt(r.cmatra_reward),
    }));
  }

  markBatchSubmitted(epoch: number, batchIndex: number, txHash: string): void {
    this.db.prepare(
      "UPDATE distributions SET status = 'submitted', tx_hash = ?, batch_index = ? WHERE epoch = ? AND status = 'pending'"
    ).run(txHash, batchIndex, epoch);
  }

  markEpochCompleted(epoch: number): void {
    this.db.prepare(
      "UPDATE epochs SET status = 'completed', completed_at = datetime('now') WHERE epoch = ?"
    ).run(epoch);
  }

  getCachedAddress(stakeAddress: string): string | null {
    const row = this.db.prepare('SELECT payment_address FROM address_cache WHERE stake_address = ?').get(stakeAddress) as { payment_address: string } | undefined;
    return row?.payment_address ?? null;
  }

  cacheAddress(stakeAddress: string, paymentAddress: string): void {
    this.db.prepare(
      'INSERT OR REPLACE INTO address_cache (stake_address, payment_address, last_updated) VALUES (?, ?, datetime(\'now\'))'
    ).run(stakeAddress, paymentAddress);
  }

  getDistributionHistory(): EpochSummary[] {
    return this.db.prepare('SELECT * FROM epochs ORDER BY epoch ASC').all() as any[];
  }

  close(): void {
    this.db.close();
  }
}
