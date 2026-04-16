import dotenv from 'dotenv';
dotenv.config();

export interface Config {
  poolId: string;
  blockfrostApiKey: string;
  blockfrostBaseUrl: string;
  rewardRate: bigint;          // cMATRA base units per 1000 ADA per epoch
  tokenName: string;
  tokenNameHex: string;        // hex of "cMATRA" = "634d41545241"
  policyScriptPath: string;
  policyId: string;
  paymentSKeyPath: string;
  policySKeyPath: string;
  cardanoCliPath: string;
  networkFlag: string;
  dbPath: string;
  maxOutputsPerTx: number;
  minUtxoLovelace: bigint;
  changeAddress: string;
}

export function loadConfig(overrides?: Partial<Config>): Config {
  const config: Config = {
    poolId: process.env.POOL_ID || 'pool1qqhk0m3ew3jn4njkftxuxxpt5qfnf0pqvnzn2y52nc7yq8rlpq6',
    blockfrostApiKey: process.env.BLOCKFROST_API_KEY || '',
    blockfrostBaseUrl: process.env.BLOCKFROST_BASE_URL || 'https://cardano-mainnet.blockfrost.io/api/v0',
    rewardRate: BigInt(process.env.REWARD_RATE || '100'),  // 100 cMATRA per 1000 ADA
    tokenName: 'cMATRA',
    tokenNameHex: '634d41545241',
    policyScriptPath: process.env.POLICY_SCRIPT_PATH || '../onchain/flux_mint_policy/mint_policy.json',
    policyId: process.env.POLICY_ID || '',
    paymentSKeyPath: process.env.PAYMENT_SKEY_PATH || './keys/payment.skey',
    policySKeyPath: process.env.POLICY_SKEY_PATH || '../onchain/flux_mint_policy/policy.skey',
    cardanoCliPath: process.env.CARDANO_CLI_PATH || 'cardano-cli',
    networkFlag: process.env.NETWORK === 'testnet' ? '--testnet-magic 1' : '--mainnet',
    dbPath: process.env.DB_PATH || './ledger.sqlite',
    maxOutputsPerTx: parseInt(process.env.MAX_OUTPUTS_PER_TX || '50'),
    minUtxoLovelace: BigInt(process.env.MIN_UTXO_LOVELACE || '1500000'),
    changeAddress: process.env.CHANGE_ADDRESS || '',
    ...overrides,
  };

  if (!config.blockfrostApiKey) throw new Error('BLOCKFROST_API_KEY is required');
  if (!config.changeAddress && !overrides?.changeAddress) {
    // Not required for snapshot-only operations
  }

  return config;
}
