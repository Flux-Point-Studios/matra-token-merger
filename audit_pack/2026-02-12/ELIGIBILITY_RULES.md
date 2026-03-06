# cMATRA Merger — Eligibility Rules

**Version:** 1.0
**Date:** 2026-03-05
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

## 2. NFT Eligibility Rule

> For NFT collections, only assets under the included policy with on-chain quantity
> exactly `1` are treated as NFTs for merger eligibility. Any asset under the same
> policy with quantity greater than `1` is treated as a fungible token and is
> **excluded** from NFT allocation math.

### Rationale

Several NFT policies contain both true 1-of-1 NFTs and semi-fungible or fungible
tokens minted under the same policy. Treating fungible tokens (qty > 1) as NFTs
would inflate supply counts, dilute per-NFT allocations, and misattribute value.

### Impact of this filter

| Collection | Total Assets Under Policy | True NFTs (qty=1) | Excluded (qty>1) |
|------------|--------------------------|-------------------|-------------------|
| FLUX_PASS | 804 | 802 | 2 |
| SE_BRAWLERS | 484 | 484 | 0 |
| BRAWL_PASS_ETD | 90 | 90 | 0 |
| T1_ADAM_PASS | 79 | 43 | 36 |
| T2_ADAM_PASS | 119 | 95 | 24 |

---

## 3. Script Address / Marketplace Rule

> NFTs held in script/marketplace custody are excluded from the allocation unless
> beneficial ownership can be deterministically resolved from on-chain data.

### Resolution strategies (applied in order)

1. **Datum parsing**: Parse the UTxO's inline datum (CBOR) for a 28-byte field
   matching a payment key hash. Most Cardano marketplaces (JPG Store, etc.)
   encode the seller's PKH in the top-level datum structure.

2. **Deposit transaction tracing**: If datum parsing fails, trace the transaction
   that deposited the NFT to the script address. If the sending input came from
   a public-key address, that address's PKH is used as the beneficial owner.

3. **Exclusion**: If neither strategy resolves to a public-key hash, the NFT is
   excluded from the allocation. The holder cannot be deterministically identified.

### Unresolvable NFTs (at time of snapshot)

| Collection | Unresolvable Count |
|------------|--------------------|
| FLUX_PASS | 419 |
| SE_BRAWLERS | 260 |
| BRAWL_PASS_ETD | 47 |
| T1_ADAM_PASS | 0 |
| T2_ADAM_PASS | 2 |
| **Total** | **728** |

These NFTs are excluded from the allocation. Their share of the bucket is
redistributed proportionally among resolved (eligible) holders within the
same collection.

---

## 4. Fungible Token Holder Rules

- All addresses holding AGENT or SHARDS are eligible.
- **Script addresses** (Plutus/native scripts) are **excluded** — only public-key
  addresses participate.
- There is no script-resolution fallback for fungible tokens (unlike NFTs), because
  fungible tokens at script addresses typically represent DEX liquidity, lending
  positions, or other protocol-managed holdings with no single beneficial owner.

---

## 5. Allocation Parameters

| Parameter | Value |
|-----------|-------|
| Output token | cMATRA |
| Decimals | 12 |
| Total supply (base units) | 1,000,000,000,000,000,000,000 (1e21) |
| Total supply (display) | 1,000,000,000 (1 billion) |
| Denominator mode | `eligible` (sum of eligible holder balances) |
| Min allocation threshold | 1 base unit |
| Dust handling | Retained as remainder (not swept) |

---

## 6. Pricing

Each asset's share of the total cMATRA supply is weighted by its TWAP-based USD
valuation:

- **Fungible tokens**: 7-day TWAP from top-3 DEX pools (by TVL) via TapTools
- **NFT collections**: 7-day TWAP of floor price via TapTools NFT OHLCV data
- **Combination**: Median across pool/window TWAPs for fungible; arithmetic mean
  of close prices for NFTs

---

## 7. Appeals

If a holder believes their NFT was incorrectly excluded (e.g., listed on a
marketplace whose datum format is not recognized), they may request manual review.
The deterministic filter described above is the default; any exception requires
explicit governance approval and must be documented in a supplementary addendum
to this file.
