import { describe, it, expect } from 'vitest';
import { buildMintAmount, buildMintDistributeCommand, buildMintSignCommand } from '../src/mint';

describe('Mint', () => {
  const policyId = 'abc123def456';
  const tokenNameHex = '634d41545241';

  const allocs = [
    { stakeAddress: 's1', paymentAddress: 'addr1q_alice', lovelaceStaked: 10000_000000n, cMatraReward: 1000n },
    { stakeAddress: 's2', paymentAddress: 'addr1q_bob', lovelaceStaked: 50000_000000n, cMatraReward: 5000n },
  ];

  it('calculates total mint amount', () => {
    const result = buildMintAmount(allocs, policyId, tokenNameHex);
    expect(result).toBe(`6000 ${policyId}.${tokenNameHex}`);
  });

  it('returns 0 mint amount for empty allocations', () => {
    const result = buildMintAmount([], policyId, tokenNameHex);
    expect(result).toBe(`0 ${policyId}.${tokenNameHex}`);
  });

  it('builds full mint+distribute command', () => {
    const cmd = buildMintDistributeCommand({
      txIns: ['utxo1#0'],
      allocations: allocs,
      policyId,
      policyScriptPath: './policy/policy.script',
      tokenNameHex,
      minUtxoLovelace: 1500000n,
      changeAddress: 'addr1q_treasury',
      networkFlag: '--mainnet',
      outFile: '/tmp/mint-batch.unsigned',
    });

    expect(cmd[0]).toBe('conway');
    expect(cmd[1]).toBe('transaction');
    expect(cmd[2]).toBe('build');
    expect(cmd).toContain('--tx-in');
    expect(cmd).toContain('utxo1#0');
    expect(cmd).toContain('--mint');
    expect(cmd).toContain(`6000 ${policyId}.${tokenNameHex}`);
    expect(cmd).toContain('--mint-script-file');
    expect(cmd).toContain('./policy/policy.script');

    // Should have 2 tx-outs for 2 recipients
    const txOutIndices = cmd.reduce((acc: number[], val, i) => val === '--tx-out' ? [...acc, i] : acc, []);
    expect(txOutIndices.length).toBe(2);

    // First output to Alice
    expect(cmd[txOutIndices[0] + 1]).toContain('addr1q_alice');
    expect(cmd[txOutIndices[0] + 1]).toContain('+1500000+');
    expect(cmd[txOutIndices[0] + 1]).toContain(`+1000 ${policyId}.${tokenNameHex}`);

    // Second output to Bob
    expect(cmd[txOutIndices[1] + 1]).toContain('addr1q_bob');
    expect(cmd[txOutIndices[1] + 1]).toContain(`+5000 ${policyId}.${tokenNameHex}`);

    expect(cmd).toContain('--change-address');
    expect(cmd).toContain('addr1q_treasury');
    expect(cmd).toContain('--mainnet');
    expect(cmd).toContain('--out-file');
    expect(cmd).toContain('/tmp/mint-batch.unsigned');
  });

  it('includes invalid-hereafter when provided', () => {
    const cmd = buildMintDistributeCommand({
      txIns: ['utxo1#0'],
      allocations: allocs,
      policyId,
      policyScriptPath: './policy/policy.script',
      tokenNameHex,
      minUtxoLovelace: 1500000n,
      changeAddress: 'addr1q_treasury',
      networkFlag: '--mainnet',
      outFile: '/tmp/mint.unsigned',
      invalidHereafter: 999999,
    });

    expect(cmd).toContain('--invalid-hereafter');
    expect(cmd).toContain('999999');
  });

  it('omits invalid-hereafter when not provided', () => {
    const cmd = buildMintDistributeCommand({
      txIns: ['utxo1#0'],
      allocations: allocs,
      policyId,
      policyScriptPath: './policy/policy.script',
      tokenNameHex,
      minUtxoLovelace: 1500000n,
      changeAddress: 'addr1q_treasury',
      networkFlag: '--mainnet',
      outFile: '/tmp/mint.unsigned',
    });

    expect(cmd).not.toContain('--invalid-hereafter');
  });

  it('builds sign command with both payment and policy skeys', () => {
    const cmd = buildMintSignCommand(
      '/tmp/mint.unsigned',
      '/tmp/mint.signed',
      './keys/payment.skey',
      './policy/policy.skey',
      '--mainnet',
    );

    expect(cmd[0]).toBe('conway');
    expect(cmd[1]).toBe('transaction');
    expect(cmd[2]).toBe('sign');

    // Two signing key files
    const skeyIndices = cmd.reduce((acc: number[], val, i) => val === '--signing-key-file' ? [...acc, i] : acc, []);
    expect(skeyIndices.length).toBe(2);
    expect(cmd[skeyIndices[0] + 1]).toBe('./keys/payment.skey');
    expect(cmd[skeyIndices[1] + 1]).toBe('./policy/policy.skey');

    expect(cmd).toContain('--mainnet');
    expect(cmd).toContain('--tx-body-file');
    expect(cmd).toContain('/tmp/mint.unsigned');
    expect(cmd).toContain('--out-file');
    expect(cmd).toContain('/tmp/mint.signed');
  });
});
