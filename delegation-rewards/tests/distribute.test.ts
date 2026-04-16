import { describe, it, expect, vi, beforeEach } from 'vitest';
import { batchAllocations, buildTxOutArgs, buildDistributionCommand } from '../src/distribute';

describe('Distribute', () => {
  it('batches 120 allocations into 3 batches of 50+50+20', () => {
    const allocs = Array.from({ length: 120 }, (_, i) => ({
      stakeAddress: `stake_${i}`,
      paymentAddress: `addr_${i}`,
      lovelaceStaked: 10000_000000n,
      cMatraReward: 1000n,
    }));
    const batches = batchAllocations(allocs, 50);
    expect(batches.length).toBe(3);
    expect(batches[0].length).toBe(50);
    expect(batches[1].length).toBe(50);
    expect(batches[2].length).toBe(20);
  });

  it('batches exactly 50 into 1 batch', () => {
    const allocs = Array.from({ length: 50 }, (_, i) => ({
      stakeAddress: `stake_${i}`,
      paymentAddress: `addr_${i}`,
      lovelaceStaked: 10000_000000n,
      cMatraReward: 1000n,
    }));
    const batches = batchAllocations(allocs, 50);
    expect(batches.length).toBe(1);
    expect(batches[0].length).toBe(50);
  });

  it('single allocation becomes 1 batch', () => {
    const allocs = [{
      stakeAddress: 'stake_0',
      paymentAddress: 'addr_0',
      lovelaceStaked: 10000_000000n,
      cMatraReward: 1000n,
    }];
    const batches = batchAllocations(allocs, 50);
    expect(batches.length).toBe(1);
    expect(batches[0].length).toBe(1);
  });

  it('empty allocations returns empty batches', () => {
    const batches = batchAllocations([], 50);
    expect(batches.length).toBe(0);
  });

  it('builds correct tx-out args', () => {
    const allocs = [
      { stakeAddress: 's1', paymentAddress: 'addr1qtest1', lovelaceStaked: 10000_000000n, cMatraReward: 1000n },
      { stakeAddress: 's2', paymentAddress: 'addr1qtest2', lovelaceStaked: 50000_000000n, cMatraReward: 5000n },
    ];
    const policyId = 'abc123';
    const tokenNameHex = '634d41545241';
    const minUtxo = 1500000n;
    const args = buildTxOutArgs(allocs, policyId, tokenNameHex, minUtxo);
    expect(args.length).toBe(4); // 2 allocs * 2 args each (--tx-out, value)
    expect(args[0]).toBe('--tx-out');
    expect(args[1]).toContain('addr1qtest1');
    expect(args[1]).toContain('+1500000');
    expect(args[1]).toContain(`+1000 ${policyId}.${tokenNameHex}`);
    expect(args[2]).toBe('--tx-out');
    expect(args[3]).toContain('addr1qtest2');
    expect(args[3]).toContain(`+5000 ${policyId}.${tokenNameHex}`);
  });

  it('each tx-out includes min-UTXO lovelace', () => {
    const allocs = [
      { stakeAddress: 's1', paymentAddress: 'addr1q_a', lovelaceStaked: 10000_000000n, cMatraReward: 100n },
    ];
    const args = buildTxOutArgs(allocs, 'pid', 'tnhex', 1500000n);
    expect(args[1]).toContain('+1500000+');
  });

  it('builds full cardano-cli distribution command', () => {
    const allocs = [
      { stakeAddress: 's1', paymentAddress: 'addr1q_a', lovelaceStaked: 10000_000000n, cMatraReward: 100n },
    ];
    const cmd = buildDistributionCommand({
      txIns: ['utxo1#0', 'utxo2#1'],
      allocations: allocs,
      policyId: 'abc123',
      tokenNameHex: '634d41545241',
      minUtxoLovelace: 1500000n,
      changeAddress: 'addr1q_change',
      networkFlag: '--mainnet',
      outFile: '/tmp/batch-0.unsigned',
    });
    expect(cmd[0]).toBe('conway');
    expect(cmd[1]).toBe('transaction');
    expect(cmd[2]).toBe('build');
    expect(cmd).toContain('--tx-in');
    expect(cmd).toContain('utxo1#0');
    expect(cmd).toContain('--change-address');
    expect(cmd).toContain('addr1q_change');
    expect(cmd).toContain('--mainnet');
    expect(cmd).toContain('--out-file');
    expect(cmd).toContain('/tmp/batch-0.unsigned');
  });
});
