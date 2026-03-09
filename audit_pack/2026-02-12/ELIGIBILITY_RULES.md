# cMATRA Merger -- Eligibility Rules

**Version:** 2.1
**Date:** 2026-03-09
**Applies to:** All reports in `audit_pack/2026-02-12/` with `cmatra` prefix

---

## 1. Eligible Assets

The cMATRA merger consolidates 7 assets into a single cMATRA allocation:

### Fungible Tokens

| Token | Policy ID | Asset Name (hex) | Decimals |
|-------|-----------|-------------------|----------|
| AGENT | `97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec` | `54616c6f73` | 0 |
| SHARDS | `ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a` | `0014df10536861726473` | 6 |

### NFT Collections

| Collection | Policy ID |
|------------|-----------|
| Flux Point Team Pass | `0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a` |
| SE Brawlers | `25c75bbf105310685d51cd3adbdd50b72fdbd99be2cc3757dde7eafc` |
| Brawl Pass: Enter the Dragon | `d3a197c4814054623432c882c60e6a81e8f3b94158033432529a02d2` |
| T1 ADAM Launch Pass | `b46891456b77dbc77c16090fd92a37f087f9a68e953c56b00a20332f` |
| T2 ADAM Launch Pass | `06a64965c0ac1144a72a6ddfcb23aa9d4d7742a5b20ddd5cfb1164b9` |

---

## 2. Three-Bucket Allocation Model

The total cMATRA supply is divided into three buckets:

### Bucket 1: Normal Claimants

Resolvable holders at snapshot time receive regular claim UTxOs. They may
claim at any time during the 6-month claim window by signing with their
payment key.

### Bucket 2: Team Treasury (immediate)

AGENT and SHARDS holdings at the following Team-controlled addresses are
carved out as Team allocation. These are not distributed to other holders
and are not subject to the claim window.

| Address | Label |
|---------|-------|
| `addr1w9u9mw864yszpqk7374wtwtwludpa0rc9dmante78c7c9sqqdlyy9` | FPS DAO Treasury |
| `addr1wx84ytuumke8gxex0l8par4852ey7l4eq6h325rnez0yluc56x0dj` | $TALOS Treasury Wallet |

The Team's share is computed as:

    team_share = (team_balance / total_on_chain_supply) * bucket

This amount is subtracted from the bucket BEFORE distribution to eligible
holders, ensuring it is not redistributed.

### Bucket 3: Conditional Reserve (script-held NFTs)

> For unresolved assets held in script or marketplace custody at snapshot
> time, a per-asset cMATRA reserve allocation is recorded but not immediately
> distributed. During the 6-month claim window, the holder of the corresponding
> asset may claim that reserved allocation by moving the asset to a personal
> wallet and proving current control of that wallet. After the claim deadline,
> any unclaimed reserve allocations associated with unresolved script-held
> assets are sweepable by the Team and become Team allocation.

The reserve amount per NFT is:

    per_nft_reserve = collection_bucket / total_nft_supply

The claim right attaches to the **asset**, not to an assumed owner. Whoever
moves that exact asset into a normal PKH wallet before the deadline, and
signs from that wallet, can claim that asset's reserved cMATRA amount.

---

## 3. NFT Eligibility Rule

> For NFT collections, only assets under the included policy with on-chain
> quantity exactly `1` are treated as NFTs for merger eligibility. Any asset
> under the same policy with quantity greater than `1` is treated as a
> fungible token and is **excluded** from NFT allocation math.

### Rationale

Several NFT policies contain both true 1-of-1 NFTs and semi-fungible or
fungible tokens minted under the same policy. Treating fungible tokens
(qty > 1) as NFTs would inflate supply counts, dilute per-NFT allocations,
and misattribute value.

### CIP-68 Handling

Three collections (FLUX_PASS, SE_BRAWLERS, BRAWL_PASS_ETD) use the CIP-68
standard, where each NFT has both a user token (`000de140` prefix, label 222)
and a reference token (`000643b0` prefix, label 100), each with qty=1. Only
the user token counts for eligibility; reference tokens are excluded.

### Impact of this filter

| Collection | Total Assets | CIP-68 User Tokens | True NFTs (qty=1) | Excluded |
|------------|-------------|--------------------|--------------------|----------|
| FLUX_PASS | 804 | 401 | 401 | 403 (ref tokens + fungible) |
| SE_BRAWLERS | 484 | 242 | 242 | 242 (ref tokens) |
| BRAWL_PASS_ETD | 90 | 44 | 44 | 46 (ref tokens + fungible) |
| T1_ADAM_PASS | 79 | n/a (non-CIP-68) | 43 | 36 (fungible) |
| T2_ADAM_PASS | 119 | n/a (non-CIP-68) | 95 | 24 (fungible) |

---

## 4. Script Address / Marketplace Resolution

> NFTs held in script/marketplace custody are placed in the conditional
> reserve (Bucket 3) unless beneficial ownership can be deterministically
> resolved from on-chain data at snapshot time.

### Resolution strategies (applied in order)

1. **Datum parsing**: Parse the UTxO's inline datum (CBOR) for a 28-byte
   field matching a payment key hash. Most Cardano marketplaces (JPG Store,
   etc.) encode the seller's PKH in the top-level datum structure.

2. **Deposit transaction tracing**: If datum parsing fails, trace the
   transaction that deposited the NFT to the script address. If the sending
   input came from a public-key address, that address's PKH is used as the
   beneficial owner.

3. **Conditional reserve**: If neither strategy resolves to a public-key
   hash, the NFT's allocation enters the conditional reserve ledger.

### Unresolvable NFTs (at time of snapshot)

| Collection | Unresolvable Count |
|------------|--------------------|
| FLUX_PASS | 8 |
| SE_BRAWLERS | 14 |
| BRAWL_PASS_ETD | 0 |
| T1_ADAM_PASS | 0 |
| T2_ADAM_PASS | 1 |
| **Total** | **23** |

---

## 5. Fungible Token Holder Rules

- All addresses holding AGENT or SHARDS are eligible, EXCEPT:
  - **Team treasury addresses** (Bucket 2) -- carved out as Team allocation
  - **Other script addresses** (DEX pools, lending protocols) -- excluded
    and redistributed proportionally among eligible PKH holders
- There is no script-resolution fallback for fungible tokens, because
  fungible tokens at script addresses typically represent DEX liquidity,
  lending positions, or other protocol-managed holdings with no single
  beneficial owner.

---

## 6. Allocation Parameters

| Parameter | Value |
|-----------|-------|
| Output token | cMATRA |
| Decimals | 12 |
| Total supply (base units) | 1,000,000,000,000,000,000,000 (1e21) |
| Total supply (display) | 1,000,000,000 (1 billion) |
| Denominator mode | `eligible` (sum of eligible holder balances, after reserve carve-out) |
| Min allocation threshold | 1 base unit |
| Claim window | 6 months from deployment |
| Dust handling | Retained as remainder (not swept) |

---

## 7. Claim Window and Expiry

- **Normal claimants** (Bucket 1): May claim at any time during the 6-month
  window by signing a transaction with their payment key.

- **Conditional reserve holders** (Bucket 3): May claim by:
  1. Moving the exact NFT asset from the script/marketplace address to a
     personal (PKH) wallet
  2. Proving current control of that wallet
  3. The service verifies: asset unit matches a reserve ledger entry, asset
     is now at a PKH address, claimant signs with that wallet, deadline has
     not passed, entry has not already been claimed

- **After the deadline**: The admin may sweep all unclaimed claim UTxOs and
  any unclaimed conditional reserve allocations. These become Team allocation.
  The claim validator enforces this on-chain: admin signature required AND
  transaction validity range must be entirely after the deadline.

---

## 8. Pricing

Each asset's share of the total cMATRA supply is weighted by its TWAP-based
USD valuation:

- **Fungible tokens**: 7-day TWAP from top-3 DEX pools (by TVL) via TapTools
- **NFT collections**: 7-day TWAP of floor price via TapTools NFT OHLCV data
- **Combination**: Median across pool/window TWAPs for fungible; arithmetic
  mean of close prices for NFTs

---

## 9. Appeals

If a holder believes their NFT was incorrectly excluded (e.g., listed on a
marketplace whose datum format is not recognized), they may request manual
review during the 6-month claim window. The deterministic filter described
above is the default; any exception requires explicit governance approval
and must be documented in a supplementary addendum to this file.

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-03-05 | Initial eligibility rules |
| 2.0 | 2026-03-09 | Added 3-bucket model (Team treasury carve-out + conditional NFT reserve), 6-month claim window, reserve ledger |
| 2.1 | 2026-03-09 | Updated NFT counts for CIP-68 correction (ref tokens excluded), updated unresolvable counts (712→23) |
