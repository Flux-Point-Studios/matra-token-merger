import { describe, it, expect } from 'vitest';
import { calculateRewards, filterDustRewards, summarize } from '../src/calculate';

describe('Calculate', () => {
  it('calculates proportional rewards correctly', () => {
    const delegators = [
      { stakeAddress: 's1', paymentAddress: 'a1', lovelaceAmount: 10000_000000n },  // 10K ADA
      { stakeAddress: 's2', paymentAddress: 'a2', lovelaceAmount: 50000_000000n },  // 50K ADA
      { stakeAddress: 's3', paymentAddress: 'a3', lovelaceAmount: 100000_000000n }, // 100K ADA
    ];
    const rate = 100n; // 100 cMATRA per 1000 ADA
    const rewards = calculateRewards(delegators, rate);
    expect(rewards[0].cMatraReward).toBe(1000n);
    expect(rewards[1].cMatraReward).toBe(5000n);
    expect(rewards[2].cMatraReward).toBe(10000n);
  });

  it('handles zero-ADA delegator', () => {
    const delegators = [
      { stakeAddress: 's1', paymentAddress: 'a1', lovelaceAmount: 0n },
    ];
    const rewards = calculateRewards(delegators, 100n);
    expect(rewards[0].cMatraReward).toBe(0n);
  });

  it('filters dust rewards below minimum', () => {
    const allocations = [
      { stakeAddress: 's1', paymentAddress: 'a1', lovelaceStaked: 5_000000n, cMatraReward: 0n },
      { stakeAddress: 's2', paymentAddress: 'a2', lovelaceStaked: 50000_000000n, cMatraReward: 5000n },
    ];
    const filtered = filterDustRewards(allocations, 1n);
    expect(filtered.length).toBe(1);
    expect(filtered[0].stakeAddress).toBe('s2');
  });

  it('integer division truncation is consistent', () => {
    const delegators = [
      { stakeAddress: 's1', paymentAddress: 'a1', lovelaceAmount: 1500_000000n },  // 1500 ADA
    ];
    const rewards = calculateRewards(delegators, 100n);
    expect(rewards[0].cMatraReward).toBe(150n);  // 1500/1000 * 100 = 150
  });

  it('large delegation values do not overflow', () => {
    const delegators = [
      { stakeAddress: 's1', paymentAddress: 'a1', lovelaceAmount: 50000000_000000n }, // 50M ADA
    ];
    const rewards = calculateRewards(delegators, 100n);
    expect(rewards[0].cMatraReward).toBe(5000000n);
  });

  it('empty delegator list returns empty allocations', () => {
    const rewards = calculateRewards([], 100n);
    expect(rewards.length).toBe(0);
  });

  it('summary totals are correct', () => {
    const allocations = [
      { stakeAddress: 's1', paymentAddress: 'a1', lovelaceStaked: 10000_000000n, cMatraReward: 1000n },
      { stakeAddress: 's2', paymentAddress: 'a2', lovelaceStaked: 50000_000000n, cMatraReward: 5000n },
    ];
    const summary = summarize(allocations);
    expect(summary.totalCmatra).toBe(6000n);
    expect(summary.recipientCount).toBe(2);
    expect(summary.totalAdaDelegated).toBe(60000_000000n);
  });
});
