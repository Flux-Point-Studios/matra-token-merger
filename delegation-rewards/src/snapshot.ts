export interface Delegator {
  stakeAddress: string;
  lovelaceAmount: bigint;
}

export interface ResolvedDelegator extends Delegator {
  paymentAddress: string;
}

const BLOCKFROST_BASE = 'https://cardano-mainnet.blockfrost.io/api/v0';

export async function getCurrentEpoch(apiKey: string): Promise<number> {
  const res = await fetch(`${BLOCKFROST_BASE}/epochs/latest`, {
    headers: { project_id: apiKey },
  });
  if (!res.ok) throw new Error(`Blockfrost error: ${res.status}`);
  const data = await res.json();
  return data.epoch;
}

export async function fetchPoolDelegators(
  epoch: number,
  poolId: string,
  apiKey: string,
): Promise<Delegator[]> {
  const delegators: Delegator[] = [];
  let page = 1;

  while (true) {
    const res = await fetch(
      `${BLOCKFROST_BASE}/epochs/${epoch}/stakes/${poolId}?count=100&page=${page}`,
      { headers: { project_id: apiKey } },
    );
    if (!res.ok) {
      throw new Error(`Blockfrost error ${res.status}: ${JSON.stringify(await res.json())}`);
    }
    const data = await res.json();
    if (!Array.isArray(data) || data.length === 0) break;

    for (const entry of data) {
      delegators.push({
        stakeAddress: entry.stake_address,
        lovelaceAmount: BigInt(entry.amount),
      });
    }

    if (data.length < 100) break;
    page++;
  }

  return delegators;
}

export async function resolvePaymentAddress(
  stakeAddress: string,
  apiKey: string,
): Promise<string> {
  const res = await fetch(
    `${BLOCKFROST_BASE}/accounts/${stakeAddress}/addresses`,
    { headers: { project_id: apiKey } },
  );
  if (!res.ok) throw new Error(`Failed to resolve ${stakeAddress}: ${res.status}`);
  const data = await res.json();
  if (!Array.isArray(data) || data.length === 0) {
    throw new Error(`No payment address found for ${stakeAddress}`);
  }
  return data[0].address;
}

export async function resolveAllPaymentAddresses(
  delegators: Delegator[],
  apiKey: string,
  cache?: { get: (s: string) => string | null; set: (s: string, a: string) => void },
): Promise<ResolvedDelegator[]> {
  const resolved: ResolvedDelegator[] = [];
  for (const d of delegators) {
    const cached = cache?.get(d.stakeAddress);
    if (cached) {
      resolved.push({ ...d, paymentAddress: cached });
      continue;
    }
    const addr = await resolvePaymentAddress(d.stakeAddress, apiKey);
    cache?.set(d.stakeAddress, addr);
    resolved.push({ ...d, paymentAddress: addr });
  }
  return resolved;
}
