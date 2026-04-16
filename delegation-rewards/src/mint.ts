import type { RewardAllocation } from './ledger';

/**
 * Build a cardano-cli conway transaction that mints cMATRA tokens
 * and distributes them to recipients in a single TX.
 *
 * Uses a native script policy (time-locked or multisig) — NOT a Plutus contract.
 */

export function buildMintAmount(
  allocations: RewardAllocation[],
  policyId: string,
  tokenNameHex: string,
): string {
  let total = 0n;
  for (const a of allocations) {
    total += a.cMatraReward;
  }
  return `${total} ${policyId}.${tokenNameHex}`;
}

interface MintTxArgs {
  txIns: string[];
  allocations: RewardAllocation[];
  policyId: string;
  policyScriptPath: string;
  tokenNameHex: string;
  minUtxoLovelace: bigint;
  changeAddress: string;
  networkFlag: string;
  outFile: string;
  invalidHereafter?: number;
}

export function buildMintDistributeCommand(args: MintTxArgs): string[] {
  const cmd: string[] = ['conway', 'transaction', 'build'];

  for (const txIn of args.txIns) {
    cmd.push('--tx-in', txIn);
  }

  // Mint clause
  const mintAmount = buildMintAmount(args.allocations, args.policyId, args.tokenNameHex);
  cmd.push('--mint', mintAmount);
  cmd.push('--mint-script-file', args.policyScriptPath);

  // Outputs to recipients
  for (const a of args.allocations) {
    cmd.push(
      '--tx-out',
      `${a.paymentAddress}+${args.minUtxoLovelace}+${a.cMatraReward} ${args.policyId}.${args.tokenNameHex}`,
    );
  }

  cmd.push('--change-address', args.changeAddress);
  cmd.push(args.networkFlag);

  if (args.invalidHereafter !== undefined) {
    cmd.push('--invalid-hereafter', String(args.invalidHereafter));
  }

  cmd.push('--out-file', args.outFile);

  return cmd;
}

export function buildMintSignCommand(
  unsignedPath: string,
  signedPath: string,
  paymentSKeyPath: string,
  policySKeyPath: string,
  networkFlag: string,
): string[] {
  return [
    'conway', 'transaction', 'sign',
    '--signing-key-file', paymentSKeyPath,
    '--signing-key-file', policySKeyPath,
    networkFlag,
    '--tx-body-file', unsignedPath,
    '--out-file', signedPath,
  ];
}
