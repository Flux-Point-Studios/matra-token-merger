# cMATRA Redemption Frontend -- Architectural Specification

**Version**: 1.0
**Date**: 2026-03-11
**Project**: Flux Point Studios -- cMATRA Merger Redemption Portal
**Phase**: PACT Architect

---

## 1. Executive Summary

This document specifies the architecture for a web-based redemption frontend that enables holders of 7 legacy Cardano assets (AGENT, SHARDS, and 5 NFT collections) to surrender those assets in exchange for cMATRA tokens at fixed, published rates.

The system uses a **server-assisted transaction building** model: the frontend collects user intent and wallet UTxOs, sends them to a Next.js API route, which builds the Plutus transaction server-side using the admin signing key. The user then signs the returned transaction with their wallet (CIP-30) and the server submits it. This matches the existing pattern used in the Flux Point Studios auction system (`build-bid.ts`) and avoids exposing the compiled validator script or admin key to the client.

The frontend will be built as a new route group within the existing Flux Point Studios Next.js 14 website (`D:/fluxPoint/website/flux1`), reusing the existing MeshSDK wallet provider, Tailwind CSS styling, and Blockfrost infrastructure.

---

## 2. System Context

### 2.1 External Dependencies

| System | Role | Interface |
|--------|------|-----------|
| **Cardano Mainnet** | Blockchain ledger | Blockfrost API v0 |
| **Blockfrost** | Chain indexer, tx submission | REST API (project ID per network) |
| **Surrender Validator** | On-chain PlutusV3 script | CBOR via pycardano (server-side) |
| **CIP-30 Wallets** | User wallet connection | `window.cardano.*` browser API |
| **Rate Table JSON** | Fixed redemption rates | Static file or API endpoint |
| **Admin Signing Key** | Required signer for ProcessSurrender | Server-side `.skey` file (never exposed) |

### 2.2 Supported Wallets

Based on the established pattern in SaturnSwapWeb (`src/utils/cardano/wallet.ts`):

- Eternl (`window.cardano.eternl`)
- Nami (`window.cardano.nami`)
- Lace (`window.cardano.lace`)
- Vespr (`window.cardano.vespr`)
- NuFi (`window.cardano.nufi`)
- Begin (`window.cardano.begin`)
- Typhon (`window.cardano.typhoncip30`)
- Gero (`window.cardano.gerowallet`)
- Yoroi (`window.cardano.yoroi`)
- Tokeo (`window.cardano.tokeo`)

### 2.3 Legacy Assets

| Asset Key | Type | Policy ID | Decimals | Redeemable Supply |
|-----------|------|-----------|----------|-------------------|
| AGENT | Fungible | `97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec` | 0 | 970,355,344 |
| SHARDS | Fungible (CIP-68) | `ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a` | 6 | 2,999,983.0 |
| FLUX_PASS | NFT (401 total) | `0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a` | -- | 401 |
| SE_BRAWLERS | NFT (242 total) | `25c75bbf105310685d51cd3adbdd50b72fdbd99be2cc3757dde7eafc` | -- | 242 |
| BRAWL_PASS_ETD | NFT (44 total) | `d3a197c4814054623432c882c60e6a81e8f3b94158033432529a02d2` | -- | 44 |
| T1_ADAM_PASS | NFT (43 total) | `b46891456b77dbc77c16090fd92a37f087f9a68e953c56b00a20332f` | -- | 43 |
| T2_ADAM_PASS | NFT (95 total) | `06a64965c0ac1144a72a6ddfcb23aa9d4d7742a5b20ddd5cfb1164b9` | -- | 95 |

---

## 3. Component Architecture

### 3.1 High-Level Component Diagram

```
+---------------------------------------------------------------+
|  Browser (Client)                                             |
|                                                               |
|  +------------------+   +------------------------------+      |
|  | WalletConnector  |-->| RedemptionPage               |      |
|  | (MeshSDK CIP-30) |   |                              |      |
|  +------------------+   |  +-------------------------+  |      |
|                         |  | AssetInventory          |  |      |
|                         |  | (balance detection)     |  |      |
|                         |  +-------------------------+  |      |
|                         |  | RateTableDisplay        |  |      |
|                         |  | (fixed rates)           |  |      |
|                         |  +-------------------------+  |      |
|                         |  | SurrenderForm           |  |      |
|                         |  | (asset selection)       |  |      |
|                         |  +-------------------------+  |      |
|                         |  | RedemptionPreview       |  |      |
|                         |  | (cMATRA calculation)    |  |      |
|                         |  +-------------------------+  |      |
|                         |  | TransactionStatus       |  |      |
|                         |  | (sign/submit/confirm)   |  |      |
|                         |  +-------------------------+  |      |
|                         +------------------------------+      |
+---------------------------------------------------------------+
          |  CIP-30 signTx         |  HTTP POST
          v                        v
+---------------------------------------------------------------+
|  Next.js Server (API Routes)                                  |
|                                                               |
|  POST /api/redeem/build-surrender                             |
|    - Validates request                                        |
|    - Queries pool UTxOs via Blockfrost                        |
|    - Builds Plutus tx (pycardano / lucid-evolution)           |
|    - Signs with admin key (required signer)                   |
|    - Returns unsigned CBOR for user co-sign                   |
|                                                               |
|  POST /api/redeem/submit                                      |
|    - Receives user-signed CBOR                                |
|    - Assembles final tx (admin + user witnesses)              |
|    - Submits to Blockfrost                                    |
|    - Returns tx hash                                          |
|                                                               |
|  GET /api/redeem/rate-table                                   |
|    - Returns current rate table JSON                          |
|                                                               |
|  GET /api/redeem/pool-status                                  |
|    - Returns remaining cMATRA in pool                         |
+---------------------------------------------------------------+
          |                        |
          v                        v
+--------------------+   +------------------------+
| Blockfrost API     |   | Surrender Validator    |
| (chain queries,    |   | (PlutusV3 on-chain)    |
| tx submission)     |   | ProcessSurrender path  |
+--------------------+   +------------------------+
```

### 3.2 Component Responsibilities

#### 3.2.1 Client Components

| Component | Responsibility | State |
|-----------|---------------|-------|
| `WalletConnector` | CIP-30 wallet detection, enable, network check | Connected wallet type, address, API handle |
| `AssetInventory` | Scan wallet UTxOs for eligible legacy assets | Map of asset -> balance (base units) |
| `RateTableDisplay` | Fetch and render fixed redemption rates | Rate table JSON, loading state |
| `SurrenderForm` | Asset selection UI, quantity input (fungible) or NFT picker | Selected assets, quantities |
| `RedemptionPreview` | Client-side cMATRA calculation for display | Computed cMATRA amount per selection |
| `TransactionStatus` | Sign flow, submission, confirmation polling | Tx lifecycle state machine |

#### 3.2.2 Server Components (API Routes)

| Endpoint | Responsibility |
|----------|---------------|
| `POST /api/redeem/build-surrender` | Server-side tx construction with admin signing key |
| `POST /api/redeem/submit` | Assemble user witness + admin witness, submit to chain |
| `GET /api/redeem/rate-table` | Serve the current rate table (cacheable) |
| `GET /api/redeem/pool-status` | Query remaining pool balance at script address |

---

## 4. Data Architecture

### 4.1 Rate Table Schema

The rate table is produced by `tools/flux_merge_valuation_int.py` and stored at `audit_pack/<date>/rate_table_cmatra.json`. The frontend serves this via API or as a static JSON import.

```typescript
interface RateTable {
  report_type: "redemption_rate_table";
  generated_at: string;                    // ISO 8601
  public_pool_base: string;                // "850000000000000000000" (bigint as string)
  public_pool_display: number;             // 850000000.0
  tokens: Record<string, TokenRate>;
}

interface TokenRate {
  bucket_base: string;                     // Total cMATRA allocated to this asset
  bucket_display: number;
  on_chain_supply_base: number;
  redeemable_supply_base: number;
  redeemable_supply_display: number;
  rate_base_per_unit: string;              // cMATRA base units per 1 base unit of legacy asset
  rate_display: number;                    // Human-readable rate
  is_nft: boolean;
}
```

**Note**: All `_base` fields that may exceed `Number.MAX_SAFE_INTEGER` (2^53) must be transmitted as strings and handled as `BigInt` on the client. The rate table values routinely exceed 10^18.

### 4.2 Surrender Request Schema

```typescript
interface SurrenderRequest {
  user_address: string;                    // Bech32 address from CIP-30
  assets: SurrenderAsset[];                // One or more assets to surrender
  user_utxos_cbor: string[];               // CBOR hex of user UTxOs (for coin selection)
  change_address: string;                  // User's change address from CIP-30
}

interface SurrenderAsset {
  asset_name: string;                      // "AGENT", "SHARDS", "FLUX_PASS", etc.
  quantity_base: number | string;          // Fungible: base units. NFT: count.
  legacy_assets: LegacyAssetDetail[];      // Individual policy+asset for each token/NFT
}

interface LegacyAssetDetail {
  policy_hex: string;                      // 56-char hex policy ID
  asset_hex: string;                       // Hex-encoded asset name
  quantity: number;                        // 1 for NFTs, amount for fungibles
}
```

### 4.3 Transaction Response Schema

```typescript
interface BuildSurrenderResponse {
  unsigned_cbor: string;                   // Tx CBOR hex for user to sign
  cmatra_total_base: string;               // Total cMATRA the user will receive
  cmatra_total_display: number;
  fee_estimate_lovelace: number;
  expiry_slot: number;                     // Tx validity window
}

interface SubmitResponse {
  tx_hash: string;                         // On-chain transaction hash
  explorer_url: string;                    // Link to CardanoScan/CExplorer
}
```

### 4.4 Client State Model

```typescript
// Zustand or React Context — single page state
interface RedemptionState {
  // Wallet
  walletType: WalletType | null;
  walletAddress: string | null;
  walletApi: CIP30API | null;
  networkId: number | null;                // 1 = mainnet, 0 = testnet

  // Assets
  walletAssets: Map<string, bigint>;       // unit -> quantity (all wallet assets)
  eligibleAssets: EligibleAsset[];         // Filtered to merger-eligible only
  assetsLoading: boolean;

  // Rate table
  rateTable: RateTable | null;
  rateTableLoading: boolean;

  // Selection
  selectedAssets: Map<string, SelectionEntry>;  // asset_name -> qty selected
  previewCmatra: bigint;                  // Computed client-side

  // Transaction
  txPhase: "idle" | "building" | "signing" | "submitting" | "confirmed" | "error";
  txHash: string | null;
  txError: string | null;

  // Pool
  poolRemaining: bigint | null;
}

interface EligibleAsset {
  assetName: string;                       // "AGENT", "FLUX_PASS", etc.
  displayName: string;                     // "Flux Point Team Pass"
  policyId: string;
  isNft: boolean;
  balance: bigint;                         // Wallet balance (base units or NFT count)
  decimals: number;
  // For NFTs: individual token names for picker
  nftTokens?: { assetHex: string; displayName: string }[];
}
```

---

## 5. API Specifications

### 5.1 POST /api/redeem/build-surrender

Builds the Plutus transaction server-side. This is the critical security boundary.

**Request**:
```json
{
  "user_address": "addr1q...",
  "change_address": "addr1q...",
  "assets": [
    {
      "asset_name": "AGENT",
      "quantity_base": "500000000",
      "legacy_assets": [
        {
          "policy_hex": "97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec",
          "asset_hex": "54616c6f73",
          "quantity": 500000000
        }
      ]
    },
    {
      "asset_name": "FLUX_PASS",
      "quantity_base": "2",
      "legacy_assets": [
        {
          "policy_hex": "0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a",
          "asset_hex": "466c7578506f696e745465616d5061737323313032",
          "quantity": 1
        },
        {
          "policy_hex": "0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a",
          "asset_hex": "466c7578506f696e745465616d5061737323323535",
          "quantity": 1
        }
      ]
    }
  ],
  "user_utxos_cbor": ["82825820abcd...00", "82825820efgh...01"]
}
```

**Response (200)**:
```json
{
  "unsigned_cbor": "84a700...",
  "cmatra_total_base": "292254526383500000000",
  "cmatra_total_display": 292254526.3835,
  "fee_estimate_lovelace": 450000,
  "expiry_slot": 123456789
}
```

**Error Responses**:

| Status | Condition |
|--------|-----------|
| 400 | Invalid address, missing fields, unknown asset, zero quantity |
| 403 | Redemption window closed (deadline passed) |
| 409 | Pool UTxO contention (retry) |
| 422 | User wallet does not hold the claimed assets |
| 429 | Rate limited |
| 500 | Transaction build failure |
| 503 | Pool exhausted -- insufficient cMATRA remaining |

**Server-Side Validation Checklist** (NEVER trust client):
1. Re-query Blockfrost for the user's address to verify they hold the claimed assets
2. Re-query pool UTxOs to get current balances
3. Re-compute cMATRA amount from the canonical rate table (server copy)
4. Verify the redemption window has not expired
5. Rate limit per IP and per wallet address

### 5.2 POST /api/redeem/submit

Receives the user-signed transaction and submits it.

**Request**:
```json
{
  "signed_cbor": "84a700...",
  "expected_tx_hash": "abcdef1234..."
}
```

**Response (200)**:
```json
{
  "tx_hash": "abcdef1234...",
  "explorer_url": "https://cexplorer.io/tx/abcdef1234..."
}
```

**Server Actions**:
1. Deserialize the user-signed CBOR
2. Verify the tx hash matches the one built in the `build-surrender` step (prevents substitution)
3. Run `evaluate_tx` preflight via Blockfrost
4. Submit via Blockfrost `submit_tx`
5. Return tx hash

### 5.3 GET /api/redeem/rate-table

Returns the current rate table. Cacheable with `Cache-Control: public, max-age=3600`.

**Response**: The full `rate_table_cmatra.json` contents as specified in Section 4.1.

### 5.4 GET /api/redeem/pool-status

Returns the current state of the surrender pool.

**Response**:
```json
{
  "pool_utxo_count": 8,
  "total_cmatra_remaining_base": "750000000000000000000",
  "total_cmatra_remaining_display": 750000000.0,
  "total_cmatra_distributed_base": "100000000000000000000",
  "total_cmatra_distributed_display": 100000000.0,
  "window_open": true,
  "deadline_posix_ms": 1756684800000,
  "deadline_iso": "2025-09-01T00:00:00Z"
}
```

---

## 6. Technology Decisions

### 6.1 Stack Selection

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| **Framework** | Next.js 14 (Pages Router) | Existing website uses Next.js 14 with Pages Router; no migration needed |
| **Language** | TypeScript | Already in use across the existing site and SaturnSwapWeb |
| **Styling** | Tailwind CSS 3.x + Headless UI | Existing site stack; consistent look and feel |
| **Wallet SDK** | MeshSDK (`@meshsdk/react` + `@meshsdk/core`) | Already installed and wrapped in `<MeshProvider>` in `_app.tsx` |
| **Tx Building (server)** | `@lucid-evolution/lucid` or `@meshsdk/core` (Node) | Already a dependency; server-side tx construction with Plutus script |
| **CBOR** | `cbor-x` (already installed) | Encode/decode Plutus datums and redeemers |
| **State** | React Context + `@tanstack/react-query` | Already in use; TanStack Query for server state (rate table, pool status) |
| **Animations** | Framer Motion | Already installed; transaction status transitions |
| **Notifications** | react-toastify | Already configured in `_app.tsx` |

### 6.2 Why MeshSDK Over Raw Lucid

The existing Flux Point website already wraps the entire app in `<MeshProvider>` and has `@meshsdk/core`, `@meshsdk/react`, and `@meshsdk/wallet` installed. MeshSDK provides:

- `useWallet()` hook for CIP-30 connection state
- `BrowserWallet` class for UTxO fetching, signing, and submission
- Built-in wallet icon detection and multi-wallet support
- TypeScript types for Cardano primitives

SaturnSwapWeb uses raw `lucid-cardano` with a custom `CardanoWallet` class. This works but requires more boilerplate. Since the Flux Point site already has MeshSDK wired up, we use that and avoid introducing a competing wallet abstraction.

### 6.3 Why Server-Assisted Transaction Building

The on-chain surrender validator requires the **admin PKH as a required signer** on every `ProcessSurrender` transaction. This means:

1. The admin signing key must produce a witness on every surrender tx
2. The compiled validator script CBOR is needed to build the script input
3. Pool UTxO selection requires up-to-date chain state

None of these should be on the client. The established pattern (see `build-bid.ts` in the auction system) is:

- Client sends intent + wallet UTxOs to server
- Server builds the full tx, adds admin witness
- Server returns partially-signed CBOR
- Client adds user witness via `wallet.signTx(cbor, true)` (partial sign = true)
- Client sends fully-signed CBOR back to server for submission

This keeps the admin key, script CBOR, and pool UTxO selection entirely server-side.

---

## 7. Wallet Integration (CIP-30)

### 7.1 Connection Flow

```
User clicks "Connect Wallet"
  --> MeshSDK <CardanoWallet> component renders wallet list
  --> User selects wallet (e.g., Eternl)
  --> MeshSDK calls window.cardano.eternl.enable()
  --> Returns CIP-30 WalletApi handle
  --> MeshSDK stores in context, exposes via useWallet()
  --> Frontend checks networkId === 1 (mainnet)
  --> If wrong network: toast error, do not proceed
  --> If correct: fetch wallet address, proceed to asset scan
```

### 7.2 Asset Detection

After connection, the frontend queries the wallet for all UTxOs and filters for eligible assets:

```typescript
// Pseudocode using MeshSDK
const { wallet } = useWallet();
const utxos = await wallet.getUtxos();         // CIP-30 getUtxos()
const balance = await wallet.getBalance();     // CIP-30 getBalance()

// Filter for known policy IDs
const ELIGIBLE_POLICIES = new Map([
  ["97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec", { name: "AGENT", decimals: 0, isNft: false }],
  ["ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a", { name: "SHARDS", decimals: 6, isNft: false }],
  ["0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a", { name: "FLUX_PASS", decimals: 0, isNft: true }],
  ["25c75bbf105310685d51cd3adbdd50b72fdbd99be2cc3757dde7eafc", { name: "SE_BRAWLERS", decimals: 0, isNft: true }],
  ["d3a197c4814054623432c882c60e6a81e8f3b94158033432529a02d2", { name: "BRAWL_PASS_ETD", decimals: 0, isNft: true }],
  ["b46891456b77dbc77c16090fd92a37f087f9a68e953c56b00a20332f", { name: "T1_ADAM_PASS", decimals: 0, isNft: true }],
  ["06a64965c0ac1144a72a6ddfcb23aa9d4d7742a5b20ddd5cfb1164b9", { name: "T2_ADAM_PASS", decimals: 0, isNft: true }],
]);
```

### 7.3 CIP-68 NFT Filtering

NFT collections using CIP-68 mint two tokens per NFT: a user token (`000de140` prefix) and a reference token (`000643b0` prefix). The frontend must only count user tokens. This mirrors the `filter_nft_assets()` logic in `tools/config.py`:

```typescript
function isUserToken(assetNameHex: string): boolean {
  return assetNameHex.startsWith("000de140");
}

function isReferenceToken(assetNameHex: string): boolean {
  return assetNameHex.startsWith("000643b0");
}
```

### 7.4 Wallet Event Handling

Following the SaturnSwapWeb pattern, register for account and network change events:

- **accountChange**: Re-scan assets, clear selection state
- **networkChange**: Disconnect and prompt reconnection

---

## 8. Transaction Building Flow

### 8.1 Sequence Diagram

```
User                    Frontend              API Server              Blockfrost
 |                        |                      |                       |
 |  Select assets         |                      |                       |
 |  Click "Redeem"        |                      |                       |
 |----------------------->|                      |                       |
 |                        |  getUtxos() (CIP-30) |                       |
 |                        |---[wallet]----------->                       |
 |                        |<--[utxo cbor list]---                        |
 |                        |                      |                       |
 |                        |  POST /build-surrender                       |
 |                        |  {address, assets,   |                       |
 |                        |   utxos, change_addr} |                       |
 |                        |--------------------->|                       |
 |                        |                      |  GET /addresses/{addr}/utxos
 |                        |                      |  (verify user assets) |
 |                        |                      |---------------------->|
 |                        |                      |<---------------------|
 |                        |                      |                       |
 |                        |                      |  GET /addresses/{script}/utxos
 |                        |                      |  (find pool UTxO)     |
 |                        |                      |---------------------->|
 |                        |                      |<---------------------|
 |                        |                      |                       |
 |                        |                      |  Build Plutus tx      |
 |                        |                      |  + admin witness      |
 |                        |                      |                       |
 |                        |  {unsigned_cbor,      |                       |
 |                        |   cmatra_amount}      |                       |
 |                        |<---------------------|                       |
 |                        |                      |                       |
 |                        |  signTx(cbor, true)  |                       |
 |  Wallet popup ---------|---[wallet]----------->                       |
 |  User approves         |<--[signed_cbor]------                        |
 |                        |                      |                       |
 |                        |  POST /submit        |                       |
 |                        |  {signed_cbor}       |                       |
 |                        |--------------------->|                       |
 |                        |                      |  evaluate_tx (preflight)
 |                        |                      |---------------------->|
 |                        |                      |<---------------------|
 |                        |                      |  submit_tx            |
 |                        |                      |---------------------->|
 |                        |                      |<--[tx_hash]----------|
 |                        |  {tx_hash}           |                       |
 |                        |<---------------------|                       |
 |  Confirmation screen   |                      |                       |
 |<-----------------------|                      |                       |
```

### 8.2 Transaction Structure (On-Chain)

As defined in `process_surrender.py`, each surrender transaction has:

**Inputs**:
- Pool UTxO at script address (script input with `ProcessSurrender` redeemer)
- User UTxO(s) containing the legacy assets (regular inputs)
- Admin UTxO for collateral and fee funding

**Outputs**:
1. **cMATRA to user**: `user_address` receives computed cMATRA amount + min ADA
2. **Pool remainder to script**: Remaining pool balance returned to script address with Void datum
3. **Legacy assets to quarantine**: User's surrendered assets sent to quarantine address

**Required Signers**: Admin PKH (on-chain validator check)

**Validity Range**: Upper bound must be before the deadline POSIX ms (enforced by validator)

### 8.3 Server-Side Tx Building Implementation

The API route at `/api/redeem/build-surrender` implements the equivalent of `process_surrender.py`'s `build_surrender_tx()` in TypeScript using `@lucid-evolution/lucid` or `@meshsdk/core`:

```typescript
// Pseudocode for server-side tx build
async function buildSurrenderTransaction(req: SurrenderRequest) {
  // 1. Load admin signing key from env/file
  // 2. Load compiled script CBOR from blueprint
  // 3. Query pool UTxOs via Blockfrost
  // 4. Select pool UTxO with sufficient cMATRA
  // 5. Compute cMATRA amount from canonical rate table
  // 6. Build transaction:
  //    - Script input: pool UTxO + ProcessSurrender redeemer
  //    - Output: cMATRA to user
  //    - Output: pool remainder to script (Void datum)
  //    - Output: legacy assets to quarantine
  //    - Required signer: admin PKH
  //    - Validity: upper bound < deadline
  // 7. Add admin witness
  // 8. Return partially-signed CBOR
}
```

### 8.4 Multi-Asset Surrender (Single Transaction)

Users may surrender multiple asset types in a single transaction (e.g., AGENT + 3 NFTs). The server must:

1. Sum cMATRA across all assets
2. Consolidate all legacy assets into a single quarantine output
3. Ensure the pool UTxO has sufficient cMATRA for the total
4. Keep the transaction within Cardano's max tx size (~16KB)

If the total exceeds a single pool UTxO's balance, the server should split into multiple transactions or select multiple pool UTxOs (multiple script inputs with separate redeemers).

---

## 9. Frontend Component Hierarchy

### 9.1 Page Structure

```
pages/
  redeem/
    index.tsx              -- RedemptionPage (main entry)

components/
  redeem/
    RedemptionPage.tsx     -- Layout, orchestrates child components
    WalletSection.tsx      -- Connect button + address display
    RateTablePanel.tsx     -- Displays all 7 asset rates
    AssetInventory.tsx     -- Shows user's eligible holdings
    AssetCard.tsx          -- Individual asset row (balance + select)
    NftPicker.tsx          -- Grid of owned NFTs with checkboxes
    FungibleInput.tsx      -- Numeric input for AGENT/SHARDS quantity
    RedemptionPreview.tsx  -- Summary: assets in, cMATRA out
    SurrenderButton.tsx    -- Initiates build + sign flow
    TransactionModal.tsx   -- Sign/submit progress + confirmation
    PoolStatusBanner.tsx   -- Remaining pool + window countdown
    WindowCountdown.tsx    -- Time remaining until deadline

lib/
  redeem/
    constants.ts           -- Policy IDs, asset metadata, config
    types.ts               -- TypeScript interfaces
    rateCalculator.ts      -- Client-side cMATRA preview computation
    assetDetection.ts      -- Wallet UTxO scanning and filtering
    api.ts                 -- Fetch wrappers for /api/redeem/* endpoints
```

### 9.2 Page Layout

```
+--------------------------------------------------+
|  HEADER (site nav)                                |
+--------------------------------------------------+
|  PoolStatusBanner                                 |
|  "850M cMATRA pool | 742M remaining | 108 days"  |
+--------------------------------------------------+
|                                                   |
|  [WalletSection]                                  |
|  "Connect Wallet" or "addr1q...xyz | Eternl"     |
|                                                   |
+--------------------------------------------------+
|  Two-column layout (desktop) / stacked (mobile)   |
|                                                   |
|  LEFT: AssetInventory                             |
|  +--------------------------------------------+  |
|  | Your Eligible Assets                        |  |
|  |                                             |  |
|  | AGENT          970,355,344     [input] [v]  |  |
|  | SHARDS         2,999.983       [input] [v]  |  |
|  | Flux Pass      3 NFTs          [pick]  [v]  |  |
|  | SE Brawlers    1 NFT           [pick]  [v]  |  |
|  | T1 ADAM Pass   2 NFTs          [pick]  [v]  |  |
|  +--------------------------------------------+  |
|                                                   |
|  RIGHT: RateTablePanel + RedemptionPreview        |
|  +--------------------------------------------+  |
|  | Redemption Rates                            |  |
|  |                                             |  |
|  | AGENT:     0.5845 cMATRA per AGENT          |  |
|  | SHARDS:    33.85 cMATRA per 1M SHARDS       |  |
|  | Flux Pass: 64,802 cMATRA per NFT            |  |
|  | ...                                         |  |
|  +--------------------------------------------+  |
|  +--------------------------------------------+  |
|  | You Will Receive                            |  |
|  |                                             |  |
|  | 567,181,482 AGENT x 0.5845 = 331,618,xxx   |  |
|  | 2 Flux Pass x 64,802 =        129,604       |  |
|  | ----------------------------------------    |  |
|  | TOTAL: 331,748,xxx cMATRA                   |  |
|  |                                             |  |
|  |          [Surrender & Redeem]               |  |
|  +--------------------------------------------+  |
+--------------------------------------------------+
```

---

## 10. Security Architecture

### 10.1 Threat Model

| Threat | Mitigation |
|--------|-----------|
| **Client-fabricated asset claims** | Server re-queries Blockfrost to verify the user's wallet actually holds the claimed assets before building tx |
| **Rate manipulation** | Rate table is server-authoritative; client display is for UX only. Server recomputes cMATRA from its own copy |
| **Admin key exposure** | Admin `.skey` is only on the server, loaded from env/file. Never serialized to client |
| **Script CBOR exposure** | Compiled validator stays server-side. Client never sees or needs it |
| **Pool UTxO contention** | Server selects pool UTxO at build time; if contention detected at submit, return 409 for retry |
| **Replay attacks** | Each tx consumes specific UTxOs (pool + user), making replay impossible |
| **Double-spend user assets** | The tx includes user's legacy assets as inputs; if already spent, tx fails |
| **Exceeding pool balance** | Server checks pool UTxO balance before building; returns 503 if insufficient |
| **Transaction substitution** | Submit endpoint verifies tx hash matches the one from build step |
| **Rate limiting bypass** | Rate limit by both IP and wallet address; max 5 builds/min, 2 submits/min |
| **XSS / injection** | All Blockfrost data is typed; no raw HTML rendering of user input |
| **Network mismatch** | Frontend checks `networkId` from CIP-30; server validates address prefix (`addr1` for mainnet) |

### 10.2 Server-Side Validation Rules

The `/api/redeem/build-surrender` endpoint must enforce:

1. **Address format**: Must be valid Bech32, mainnet prefix `addr1`
2. **Asset existence**: Each claimed asset must exist at the user's address per Blockfrost
3. **Quantity bounds**: Fungible quantities must be > 0 and <= wallet balance; NFT count must match distinct tokens
4. **Rate recomputation**: cMATRA amount is always computed server-side from the canonical rate table
5. **Pool sufficiency**: Selected pool UTxO must have >= total cMATRA required
6. **Window check**: Current time must be before the surrender deadline
7. **Max assets per tx**: Cap at 20 individual NFTs or 5 asset types per transaction to stay within tx size limits

### 10.3 Environment Variables

```
# Server-side only (never exposed to client)
SURRENDER_ADMIN_SKEY_PATH=/path/to/admin.skey
SURRENDER_SCRIPT_CBOR_HEX=<compiled validator hex>
SURRENDER_SCRIPT_ADDRESS=addr1w...
SURRENDER_QUARANTINE_ADDRESS=addr1w...
CMATRA_POLICY_HEX=<56-char hex>
CMATRA_ASSET_HEX=<hex-encoded "cMATRA">
BLOCKFROST_PROJECT_ID_MAINNET=mainnet...
SURRENDER_RATE_TABLE_PATH=./audit_pack/2026-03-11/rate_table_cmatra.json
SURRENDER_DEADLINE_POSIX_MS=1756684800000

# Client-side (NEXT_PUBLIC_ prefix)
NEXT_PUBLIC_CARDANO_NETWORK=mainnet
NEXT_PUBLIC_SURRENDER_DEADLINE_ISO=2025-09-01T00:00:00Z
```

---

## 11. Error Handling and Edge Cases

### 11.1 Error States and User Messaging

| Scenario | Detection | User Message |
|----------|----------|-------------|
| No wallet extension | `window.cardano` undefined | "Please install a Cardano wallet extension (Eternl, Nami, Lace, etc.)" |
| Wrong network | `networkId !== 1` | "Please switch your wallet to Cardano Mainnet" |
| No eligible assets | Asset scan returns empty | "Your wallet does not contain any eligible legacy assets" |
| Pool exhausted | API returns 503 | "The redemption pool has been fully claimed. No more cMATRA is available." |
| Window closed | API returns 403 | "The redemption window has closed. Surrenders are no longer accepted." |
| UTxO contention | API returns 409 | "Another transaction is in progress. Please wait a moment and try again." |
| User rejects sign | CIP-30 signTx throws | "Transaction signing was cancelled. No assets were sent." |
| Tx submission fails | Submit API returns error | "Transaction failed to submit. Your assets are safe. Error: {detail}" |
| Wallet disconnects mid-flow | accountChange/networkChange event | Reset state, prompt reconnection |
| Rate table unavailable | API returns 500 | "Unable to load redemption rates. Please try again later." |

### 11.2 Transaction Size Limits

Cardano transactions are limited to approximately 16KB. A surrender transaction with many NFTs can approach this limit due to the number of distinct asset entries. The frontend should:

1. Count selected assets and warn if > 15 NFTs in a single transaction
2. If the user wants to surrender more, split into multiple transactions
3. Show a "batch mode" indicator: "Transaction 1 of 3"

### 11.3 Partial Surrender

Users are not required to surrender all of their holdings. The UI supports:

- Fungible: slider or numeric input for partial amounts (e.g., surrender 500M of 970M AGENT)
- NFTs: individual checkboxes per NFT in a picker grid

### 11.4 Concurrent Access

Multiple users may attempt to spend the same pool UTxO simultaneously. Mitigations:

1. The pool is split across ~10 UTxOs (see `build_surrender_pool.py`, `num_utxos=10`)
2. The server selects the smallest sufficient pool UTxO to minimize contention
3. On 409 (UTxO already spent), the frontend auto-retries once with a fresh pool query
4. A server-side in-memory lock (per pool UTxO) prevents building two txs against the same UTxO within a 60-second window

---

## 12. Non-Functional Requirements

### 12.1 Performance

| Metric | Target |
|--------|--------|
| Wallet asset scan | < 2 seconds |
| Rate table load | < 500ms (cached after first load) |
| Transaction build (server) | < 5 seconds |
| Transaction sign (user) | Depends on wallet UX |
| Transaction submit | < 10 seconds (including preflight) |
| Pool status refresh | Every 30 seconds (polling) |

### 12.2 Scalability

- The surrender pool has ~10 UTxOs, supporting ~10 concurrent transactions
- Rate table is static and cacheable (CDN-friendly)
- Blockfrost rate limits: 500 req/sec on paid plans; server should cache UTxO queries for 5 seconds
- No database required; all state is on-chain

### 12.3 Accessibility

- All interactive elements must be keyboard-navigable
- Wallet amounts must use `aria-label` with full numeric values
- Transaction status modal must trap focus
- Color contrast must meet WCAG AA for rate table and balance displays

### 12.4 Mobile Responsiveness

- Single-column layout on screens < 768px
- Touch-friendly NFT picker (minimum 44px tap targets)
- Wallet connection: mobile wallets (e.g., Vespr) use deep links; MeshSDK handles this

---

## 13. Deployment Architecture

### 13.1 Infrastructure

The redemption frontend deploys as part of the existing Flux Point Studios website:

```
Vercel (existing deployment)
  |
  +-- Next.js 14 (Pages Router)
  |     +-- pages/redeem/index.tsx (new)
  |     +-- pages/api/redeem/*.ts (new API routes)
  |
  +-- Environment Variables (Vercel dashboard)
  |     +-- SURRENDER_ADMIN_SKEY_PATH (serverless function file)
  |     +-- BLOCKFROST_PROJECT_ID_MAINNET
  |     +-- SURRENDER_SCRIPT_CBOR_HEX
  |     +-- ... (see Section 10.3)
  |
  +-- Static Assets
        +-- rate_table_cmatra.json (bundled or fetched)
```

### 13.2 Admin Key in Serverless

The admin `.skey` must be available to the serverless function. Options:

1. **Environment variable**: Store the signing key bytes as a base64-encoded env var. Decode at runtime. Simplest for Vercel.
2. **Vercel Blob Storage**: Upload the `.skey` file and fetch at cold start. More secure but adds latency.
3. **KMS**: Use a cloud KMS (AWS KMS, HashiCorp Vault) for signing. Most secure but adds complexity and cost.

**Recommendation**: Option 1 (env var) for initial deployment, with a plan to migrate to KMS before mainnet production if the admin key controls significant value.

### 13.3 Staging Environment

- Deploy to Vercel Preview for each PR
- Staging uses `NETWORK=preprod` with a preprod Blockfrost key
- Preprod pool UTxOs created via `build_surrender_pool.py` against preprod

---

## 14. Implementation Roadmap

### 14.1 Development Order

```
Phase 1: Foundation (Week 1)
  [1.1] Rate table API route + static serving
  [1.2] Wallet connection integration (reuse existing MeshProvider)
  [1.3] Asset detection and filtering logic
  [1.4] RedemptionPage layout shell

Phase 2: Core Redemption (Weeks 2-3)
  [2.1] Server-side transaction building (build-surrender API route)
  [2.2] SurrenderForm with fungible input + NFT picker
  [2.3] RedemptionPreview (client-side calculation)
  [2.4] Sign + submit flow (TransactionModal)
  [2.5] Pool status endpoint + banner

Phase 3: Hardening (Week 4)
  [3.1] Server-side validation (asset verification, rate recomputation)
  [3.2] Rate limiting (per IP + per wallet)
  [3.3] Error handling for all edge cases
  [3.4] UTxO contention handling + retry logic
  [3.5] Transaction size limit checks

Phase 4: Polish + Preprod Testing (Week 5)
  [4.1] Responsive design pass
  [4.2] Accessibility audit
  [4.3] End-to-end testing on preprod
  [4.4] Multi-wallet testing (Eternl, Nami, Lace, Vespr)
  [4.5] Load testing (concurrent surrenders)
```

### 14.2 Milestones

| Milestone | Deliverable | Acceptance Criteria |
|-----------|-------------|-------------------|
| M1 | Wallet connects and shows eligible assets | All 7 asset types detected from a test wallet on preprod |
| M2 | Rate table displayed correctly | All rates match `rate_table_cmatra.json` values; BigInt precision preserved |
| M3 | End-to-end surrender on preprod | Single AGENT surrender: tx builds, signs, submits, cMATRA received |
| M4 | Multi-asset surrender | Surrender AGENT + 2 NFTs in one tx on preprod |
| M5 | Production hardened | All error cases handled, rate limited, validated server-side |
| M6 | Mainnet deploy | Live at fluxpoint.studio/redeem |

### 14.3 Testing Strategy

**Unit Tests** (Jest/Vitest):
- `rateCalculator.ts`: Verify BigInt arithmetic matches Python `compute_redemption()` output
- `assetDetection.ts`: CIP-68 filtering, policy ID matching
- `constants.ts`: Policy IDs match `config.py` values

**Integration Tests** (API route tests):
- `build-surrender`: Mock Blockfrost, verify tx structure
- `submit`: Verify tx hash validation, error propagation
- Rate limiting enforcement

**End-to-End Tests** (Preprod):
- Connect Eternl -> detect test assets -> surrender -> verify cMATRA received
- Repeat with Nami, Lace
- Concurrent surrender from 2 wallets -> verify no pool contention crash
- Surrender with window expired -> verify 403

**Manual Testing Checklist**:
- [ ] All 7 asset types detected and displayed
- [ ] Fungible partial surrender (input half of balance)
- [ ] NFT picker: select 2 of 5 owned NFTs
- [ ] Multi-asset surrender in single tx
- [ ] Pool status updates after surrender
- [ ] Window countdown reaches zero -> UI blocks new surrenders
- [ ] Wrong network -> error shown
- [ ] No eligible assets -> appropriate message
- [ ] Mobile layout renders correctly

---

## 15. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Blockfrost rate limits hit during high traffic | Medium | High | Cache UTxO queries (5s TTL), use Koios as fallback |
| Admin key compromise via serverless env | Low | Critical | Migrate to KMS before mainnet; key only controls pool distribution, not minting |
| Pool UTxO contention under load | Medium | Medium | 10 pool UTxOs + smallest-first selection + auto-retry |
| Transaction too large for many NFTs | Low | Medium | Cap at 15 NFTs/tx, auto-split into batches |
| MeshSDK version incompatibility | Low | Low | Pin versions, test against target wallets |
| BigInt precision loss in rate display | Medium | Medium | Use `BigInt` throughout; never convert to `Number` for arithmetic |
| User surrenders wrong NFT | Low | High | Show NFT image + name in picker; require explicit confirmation |
| Vercel function timeout (10s default) | Medium | Medium | Optimize tx build; increase to 30s on Pro plan |
| Rate table becomes stale | Low | Low | Rate table is fixed for the entire window; no staleness concern |

---

## 16. Appendix: BigInt Handling

cMATRA uses 12 decimal places. A single T1 ADAM Pass redeems for `2,985,976,994,718,552,921` base units -- this exceeds `Number.MAX_SAFE_INTEGER` (9,007,199,254,740,991). All arithmetic involving base units must use `BigInt`.

**JSON serialization**: `BigInt` cannot be serialized to JSON natively. Use string representation in all API payloads:

```typescript
// Server response
{ "cmatra_total_base": "2985976994718552921" }

// Client parsing
const cmatraBase = BigInt(response.cmatra_total_base);

// Display formatting
function formatCmatra(base: bigint, decimals: number = 12): string {
  const whole = base / BigInt(10 ** decimals);
  const frac = base % BigInt(10 ** decimals);
  const fracStr = frac.toString().padStart(decimals, "0").slice(0, 2);
  return `${whole.toLocaleString()}.${fracStr}`;
}
```

---

## 17. Appendix: Asset Configuration Constants

These must be kept in sync with `tools/config.py`:

```typescript
// lib/redeem/constants.ts

export const CMATRA_DECIMALS = 12;
export const CMATRA_DISPLAY_SYMBOL = "cMATRA";

export const ASSET_CONFIG = {
  AGENT: {
    policyId: "97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec",
    assetNameHex: "54616c6f73",
    decimals: 0,
    isNft: false,
    displayName: "AGENT",
  },
  SHARDS: {
    policyId: "ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a",
    assetNameHex: "0014df10536861726473",
    decimals: 6,
    isNft: false,
    displayName: "SHARDS",
  },
  FLUX_PASS: {
    policyId: "0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a",
    decimals: 0,
    isNft: true,
    displayName: "Flux Point Team Pass",
  },
  SE_BRAWLERS: {
    policyId: "25c75bbf105310685d51cd3adbdd50b72fdbd99be2cc3757dde7eafc",
    decimals: 0,
    isNft: true,
    displayName: "SE Brawlers",
  },
  BRAWL_PASS_ETD: {
    policyId: "d3a197c4814054623432c882c60e6a81e8f3b94158033432529a02d2",
    decimals: 0,
    isNft: true,
    displayName: "Brawl Pass: Enter the Dragon",
  },
  T1_ADAM_PASS: {
    policyId: "b46891456b77dbc77c16090fd92a37f087f9a68e953c56b00a20332f",
    decimals: 0,
    isNft: true,
    displayName: "T1 ADAM Launch Pass",
  },
  T2_ADAM_PASS: {
    policyId: "06a64965c0ac1144a72a6ddfcb23aa9d4d7742a5b20ddd5cfb1164b9",
    decimals: 0,
    isNft: true,
    displayName: "T2 ADAM Launch Pass",
  },
} as const;

// CIP-68 prefixes
export const CIP68_USER_TOKEN_PREFIX = "000de140";
export const CIP68_REFERENCE_TOKEN_PREFIX = "000643b0";
```
