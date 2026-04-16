import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fetchPoolDelegators, getCurrentEpoch, resolvePaymentAddress } from '../src/snapshot';

// Mock fetch globally
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

describe('Snapshot', () => {
  beforeEach(() => {
    mockFetch.mockReset();
  });

  it('getCurrentEpoch returns latest epoch number', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ epoch: 520 }),
    });
    const epoch = await getCurrentEpoch('test-key');
    expect(epoch).toBe(520);
  });

  it('fetches delegators for a valid epoch', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => [
        { stake_address: 'stake1u_a', amount: '50000000000' },
        { stake_address: 'stake1u_b', amount: '100000000000' },
      ],
    });
    // Empty second page
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => [],
    });
    const delegators = await fetchPoolDelegators(520, 'pool1test', 'test-key');
    expect(delegators.length).toBe(2);
    expect(delegators[0].stakeAddress).toBe('stake1u_a');
    expect(delegators[0].lovelaceAmount).toBe(50000000000n);
  });

  it('paginates through multiple pages', async () => {
    // Page 1: 100 results
    const page1 = Array.from({ length: 100 }, (_, i) => ({
      stake_address: `stake1u_${i}`,
      amount: `${(i + 1) * 1000000}`,
    }));
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => page1 });
    // Page 2: 50 results
    const page2 = Array.from({ length: 50 }, (_, i) => ({
      stake_address: `stake1u_${i + 100}`,
      amount: `${(i + 101) * 1000000}`,
    }));
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => page2 });
    // Page 3: empty
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });

    const delegators = await fetchPoolDelegators(520, 'pool1test', 'test-key');
    expect(delegators.length).toBe(150);
  });

  it('handles empty pool (no delegators)', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    const delegators = await fetchPoolDelegators(520, 'pool1test', 'test-key');
    expect(delegators.length).toBe(0);
  });

  it('resolves stake address to payment address', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => [{ address: 'addr1q_test_payment' }],
    });
    const addr = await resolvePaymentAddress('stake1u_a', 'test-key');
    expect(addr).toBe('addr1q_test_payment');
  });

  it('handles Blockfrost 404 (pool not found)', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({ error: 'Not Found' }),
    });
    await expect(fetchPoolDelegators(520, 'pool1bad', 'test-key')).rejects.toThrow();
  });
});
