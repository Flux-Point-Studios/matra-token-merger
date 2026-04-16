import type { ResolvedDelegator } from './snapshot';

export interface RewardAllocation {
  stakeAddress: string;
  paymentAddress: string;
  lovelaceStaked: bigint;
  cMatraReward: bigint;
}

export function calculateRewards(
  delegators: ResolvedDelegator[],
  ratePerThousandAda: bigint,
): RewardAllocation[] {
  return delegators.map(d => {
    const adaAmount = d.lovelaceAmount / 1_000_000n; // lovelace to ADA
    const reward = (adaAmount * ratePerThousandAda) / 1000n;
    return {
      stakeAddress: d.stakeAddress,
      paymentAddress: d.paymentAddress,
      lovelaceStaked: d.lovelaceAmount,
      cMatraReward: reward,
    };
  });
}

export function filterDustRewards(
  allocations: RewardAllocation[],
  minReward: bigint,
): RewardAllocation[] {
  return allocations.filter(a => a.cMatraReward >= minReward);
}

export function summarize(allocations: RewardAllocation[]): {
  totalCmatra: bigint;
  recipientCount: number;
  totalAdaDelegated: bigint;
} {
  let totalCmatra = 0n;
  let totalAdaDelegated = 0n;
  for (const a of allocations) {
    totalCmatra += a.cMatraReward;
    totalAdaDelegated += a.lovelaceStaked;
  }
  return { totalCmatra, recipientCount: allocations.length, totalAdaDelegated };
}
