import { describe, it, expect, beforeEach } from 'vitest';
import { Ledger } from '../src/ledger';

describe('Ledger', () => {
  let ledger: Ledger;

  beforeEach(() => {
    ledger = new Ledger(':memory:');
  });

  it('initDb creates tables without error', () => {
    expect(() => ledger.init()).not.toThrow();
  });

  it('isEpochDistributed returns false for new epoch', () => {
    ledger.init();
    expect(ledger.isEpochDistributed(520)).toBe(false);
  });

  it('isEpochDistributed returns true for completed epoch', () => {
    ledger.init();
    ledger.startEpoch(520, 10, '1000000000', '50000', '100');
    ledger.markEpochCompleted(520);
    expect(ledger.isEpochDistributed(520)).toBe(true);
  });

  it('saveAllocations inserts correct rows', () => {
    ledger.init();
    const allocs = [
      { stakeAddress: 'stake1u_a', paymentAddress: 'addr1_a', lovelaceStaked: 50000000000n, cMatraReward: 5000n },
      { stakeAddress: 'stake1u_b', paymentAddress: 'addr1_b', lovelaceStaked: 100000000000n, cMatraReward: 10000n },
    ];
    ledger.startEpoch(520, 2, '150000000000', '15000', '100');
    ledger.saveAllocations(520, allocs);
    const pending = ledger.getPendingAllocations(520);
    expect(pending.length).toBe(2);
    expect(pending[0].stakeAddress).toBe('stake1u_a');
  });

  it('duplicate allocation for same epoch+stake_address is rejected', () => {
    ledger.init();
    const alloc = { stakeAddress: 'stake1u_a', paymentAddress: 'addr1_a', lovelaceStaked: 50000000000n, cMatraReward: 5000n };
    ledger.startEpoch(520, 1, '50000000000', '5000', '100');
    ledger.saveAllocations(520, [alloc]);
    expect(() => ledger.saveAllocations(520, [alloc])).toThrow();
  });

  it('markBatchSubmitted updates tx_hash and status', () => {
    ledger.init();
    const allocs = [
      { stakeAddress: 'stake1u_a', paymentAddress: 'addr1_a', lovelaceStaked: 50000000000n, cMatraReward: 5000n },
    ];
    ledger.startEpoch(520, 1, '50000000000', '5000', '100');
    ledger.saveAllocations(520, allocs);
    ledger.markBatchSubmitted(520, 0, 'txhash123');
    const pending = ledger.getPendingAllocations(520);
    expect(pending.length).toBe(0);
  });

  it('markEpochCompleted sets status and timestamp', () => {
    ledger.init();
    ledger.startEpoch(520, 1, '50000000000', '5000', '100');
    ledger.markEpochCompleted(520);
    expect(ledger.isEpochDistributed(520)).toBe(true);
  });

  it('address cache stores and retrieves correctly', () => {
    ledger.init();
    expect(ledger.getCachedAddress('stake1u_a')).toBeNull();
    ledger.cacheAddress('stake1u_a', 'addr1_a');
    expect(ledger.getCachedAddress('stake1u_a')).toBe('addr1_a');
  });

  it('getDistributionHistory returns completed epochs', () => {
    ledger.init();
    ledger.startEpoch(518, 5, '500000000000', '50000', '100');
    ledger.markEpochCompleted(518);
    ledger.startEpoch(519, 8, '800000000000', '80000', '100');
    ledger.markEpochCompleted(519);
    const history = ledger.getDistributionHistory();
    expect(history.length).toBe(2);
    expect(history[0].epoch).toBe(518);
  });

  it('isEpochInProgress returns correct state', () => {
    ledger.init();
    ledger.startEpoch(520, 1, '50000000000', '5000', '100');
    expect(ledger.isEpochInProgress(520)).toBe(true);
    expect(ledger.isEpochDistributed(520)).toBe(false);
  });
});
