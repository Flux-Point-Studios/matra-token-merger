import type { RewardAllocation } from './ledger';

export function batchAllocations(
  allocations: RewardAllocation[],
  batchSize: number,
): RewardAllocation[][] {
  if (allocations.length === 0) return [];
  const batches: RewardAllocation[][] = [];
  for (let i = 0; i < allocations.length; i += batchSize) {
    batches.push(allocations.slice(i, i + batchSize));
  }
  return batches;
}

export function buildTxOutArgs(
  allocations: RewardAllocation[],
  policyId: string,
  tokenNameHex: string,
  minUtxoLovelace: bigint,
): string[] {
  const args: string[] = [];
  for (const a of allocations) {
    args.push('--tx-out');
    args.push(`${a.paymentAddress}+${minUtxoLovelace}+${a.cMatraReward} ${policyId}.${tokenNameHex}`);
  }
  return args;
}

interface BuildCommandArgs {
  txIns: string[];
  allocations: RewardAllocation[];
  policyId: string;
  tokenNameHex: string;
  minUtxoLovelace: bigint;
  changeAddress: string;
  networkFlag: string;
  outFile: string;
}

export function buildDistributionCommand(args: BuildCommandArgs): string[] {
  const cmd: string[] = ['conway', 'transaction', 'build'];

  for (const txIn of args.txIns) {
    cmd.push('--tx-in', txIn);
  }

  const txOutArgs = buildTxOutArgs(
    args.allocations,
    args.policyId,
    args.tokenNameHex,
    args.minUtxoLovelace,
  );
  cmd.push(...txOutArgs);

  cmd.push('--change-address', args.changeAddress);
  cmd.push(args.networkFlag);
  cmd.push('--out-file', args.outFile);

  return cmd;
}

export function buildSignCommand(
  unsignedPath: string,
  signedPath: string,
  paymentSKeyPath: string,
  networkFlag: string,
): string[] {
  return [
    'conway', 'transaction', 'sign',
    '--signing-key-file', paymentSKeyPath,
    networkFlag,
    '--tx-body-file', unsignedPath,
    '--out-file', signedPath,
  ];
}

export function buildSubmitCommand(
  signedPath: string,
  networkFlag: string,
): string[] {
  return [
    'conway', 'transaction', 'submit',
    '--tx-file', signedPath,
    networkFlag,
  ];
}

export function buildTxIdCommand(signedPath: string): string[] {
  return [
    'conway', 'transaction', 'txid',
    '--tx-file', signedPath,
  ];
}
