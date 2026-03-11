# cMATRA Merger -- Eligibility & Redemption Rules

**Version:** 3.0  
**Date:** 2026-03-10  
**Status:** Public / governance draft  
**Applies to:** Final cMATRA launch package and redemption window  
**Supersedes:** Earlier snapshot-only claimant interpretations for fungible assets

---

## 1. Included Assets

The cMATRA transition consolidates seven legacy Cardano assets into a single public redemption program.

### Fungible Tokens

| Token | Policy ID | Asset Name (hex) | Decimals |
|---|---|---|---:|
| AGENT | `97bbb7db0baef89caefce61b8107ac74c7a7340166b39d906f174bec` | `54616c6f73` | 0 |
| SHARDS | `ea153b5d4864af15a1079a94a0e2486d6376fa28aafad272d15b243a` | `0014df10536861726473` | 6 |

### NFT Collections

| Collection | Policy ID |
|---|---|
| Flux Point Team Pass | `0889a2d542897f0c7eefed47d2d809bd8d8ec78881bd4ff9464f683a` |
| SE Brawlers | `25c75bbf105310685d51cd3adbdd50b72fdbd99be2cc3757dde7eafc` |
| Brawl Pass: Enter the Dragon | `d3a197c4814054623432c882c60e6a81e8f3b94158033432529a02d2` |
| T1 ADAM Launch Pass | `b46891456b77dbc77c16090fd92a37f087f9a68e953c56b00a20332f` |
| T2 ADAM Launch Pass | `06a64965c0ac1144a72a6ddfcb23aa9d4d7742a5b20ddd5cfb1164b9` |

---

## 2. Supply Model

The merger now uses a fixed supply model with a validator reserve carved out at genesis.

| Parameter | Value |
|---|---:|
| Output token | cMATRA / MATRA |
| Decimals | 6 |
| Max supply | 1,000,000,000 |
| Validator reserve | 150,000,000 (15%) |
| Public redemption pool | 850,000,000 (85%) |
| Public window | 6 months from launch |

### Key policy implications

- The **15% validator reserve** is non-circulating at launch and exists to fund Materios validators over time.
- The **85% public redemption pool** is the only pool used for ordinary public redemptions.
- **Team treasury softening** is handled through explicit waiver and disclosure, not by pretending the reserve does not exist.

---

## 3. Public Redemption Model

This merger now uses a **surrender-and-redeem model** for normal public holders.

### Core rule

A public holder redeems by **actually controlling and surrendering an eligible legacy asset during the redemption window**.

For normal public holders:

- redemption follows **present control plus surrender**, not snapshot ownership alone;
- a holder who acquires an eligible legacy asset before the deadline may redeem it;
- a holder who sells an eligible legacy asset before redeeming no longer controls the redeemable unit; and
- the public process is intended to feel like a real trade-in, not a snapshot-only entitlement airdrop.

### Window mechanics

- The standard public window is **6 months** from the published launch date.
- The launch package is expected to publish a **fixed rate table before the window opens**.
- The rate table is intended to remain fixed for the full window unless a documented governance-approved correction is required for a launch bug or reconciliation error.
- Holders should be able to redeem in **multiple transactions** during the window unless the final launch instructions state otherwise.

---

## 4. What the Reference Snapshot Still Does

A published reference snapshot still exists, but its role has changed.

### The reference snapshot is still used for:

- auditability and public reconciliation;
- publication of Team treasury waivers and other disclosed non-public balances;
- legacy reward reconciliation before launch;
- NFT collection inventory review and CIP-68 filtering; and
- sanity checks for the final launch package.

### The reference snapshot is **not** the normal fungible entitlement rule

For AGENT and SHARDS, the reference snapshot is **not** what gives an ordinary public holder their redemption right by itself.

Earlier snapshot-era logic that redistributed fungible balances from DEX, lending, or other script addresses among snapshot claimants is **superseded** by this redemption model.

---

## 5. Fungible Asset Rules

### 5.1 Redeemable fungible units

The redeemable fungible assets are:

- AGENT;
- SHARDS; and
- any valid legacy rewards that Materios explicitly honors and **materializes into actual redeemable AGENT or SHARDS units before launch**.

### 5.2 Current control matters

For ordinary public redemption, redeemability follows **current control of the fungible unit at redemption time**, subject to the explicit exclusions below.

Materios is **not** relying on provenance tracking of ordinary fungible units once they are circulating, because fungible tokens do not carry a reliable public on-chain history label that distinguishes one unit from another in normal use.

### 5.3 Explicit exclusions and waivers

The following categories are not intended to compete with the public redemption pool:

1. **Disclosed Team treasury balances** that are explicitly waived from public redemption.
2. **Balances already burned, quarantined, or permanently immobilized** under the published launch controls.
3. **Any other specifically disclosed non-public balances** that the final launch package excludes by explicit publication.

#### Current disclosed Team treasury addresses

| Address | Label |
|---|---|
| `addr1w9u9mw864yszpqk7374wtwtwludpa0rc9dmante78c7c9sqqdlyy9` | FPS DAO Treasury |
| `addr1wx84ytuumke8gxex0l8par4852ey7l4eq6h325rnez0yluc56x0dj` | $TALOS Treasury Wallet |

The launch reconciliation package should publish the exact waived balances used for rate setting.

#### Why treasury balances are waived

Since the creation of both on-chain treasuries, there has been a complete lack of independent DAO proposals — the only proposals submitted were those proposed by the FPS Team itself. Given this, it makes the most sense for these funds to be returned to the team where they can be put to better use with more flexibility, rather than sitting idle in governance structures that have seen no community participation. The same rationale applies to the $TALOS treasury.

All waived balances are published transparently so any holder can verify them on-chain.

### 5.4 LP farms, DEX positions, and other DeFi custody

If AGENT or SHARDS are currently in:

- a Minswap or WingRiders liquidity pool,
- a farm,
- a lending protocol,
- another script-controlled DeFi position,

then the holder must first **unfarm / withdraw / remove liquidity** so the underlying AGENT and/or SHARDS return to a wallet they control.

#### Important rules

- **LP tokens do not redeem.** They are receipts for positions, not merger assets.
- The redeemable assets are the **underlying AGENT and/or SHARDS** once those assets are back in a wallet the holder controls.
- If impermanent loss, fees, or trading activity changed the amount of underlying tokens in the position, the holder redeems **what they actually withdraw and control**, not what they once deposited.
- Materios may try to coordinate with major venues such as Minswap and WingRiders to make the process easier, but holders remain responsible for withdrawing their assets before the deadline.

### 5.5 Custodial and exchange balances

Assets held on a centralized exchange or by another custodian are not directly redeemable through the normal public path unless the holder can move them into self-custody during the window.

In practice, the standard assumption is:

- **self-custody = redeemable once surrendered**;
- **custodial balance = not safely assumed redeemable until withdrawn to self-custody**.

---

## 6. NFT Rules

Eligible NFTs redeem on an asset-by-asset basis.

### 6.1 NFT eligibility filter

Only assets under the included policy with **on-chain quantity exactly `1`** count as NFTs for merger eligibility.

Any asset under the same policy with quantity greater than `1` is treated as fungible or semi-fungible and is **excluded** from NFT redemption math.

### 6.2 CIP-68 handling

For CIP-68 collections, only the **user token** counts. The **reference token** does not redeem.

### 6.3 Current control rule for NFTs

The redeeming holder must control the **exact NFT asset** during the redemption window.

That means:

- if the NFT is in a normal wallet, it may be surrendered for redemption;
- if the NFT is listed on a marketplace or held by a script, it must first be withdrawn back to a wallet the holder controls; and
- if the NFT cannot be recovered from third-party custody before the deadline, it cannot use the normal public redemption path.

### 6.4 Current reference inventory after filtering

| Collection | True redeemable NFT count (reference inventory) |
|---|---:|
| Flux Point Team Pass | 401 |
| SE Brawlers | 242 |
| Brawl Pass: Enter the Dragon | 44 |
| T1 ADAM Launch Pass | 43 |
| T2 ADAM Launch Pass | 95 |

These counts are a **reference inventory baseline** used for launch reconciliation and rate setting. The final launch package should disclose any further explicit waivers or exclusions.

---

## 7. Legacy Reward Materialization

Materios is explicitly choosing the cleaner path for legacy rewards.

If a user has valid legacy staking rewards or dashboard-tracked rewards that Materios commits to honor, those rewards are intended to be:

1. reconciled before launch;
2. credited or minted into actual redeemable AGENT or SHARDS units before the window opens; and then
3. redeemed through the same public surrender path as any other eligible unit of that asset.

### Intended consequence

- no separate spreadsheet side-claim should be necessary for ordinary honored legacy rewards;
- the public rate table can be set against the supply that actually exists for redemption; and
- all ordinary honored holders follow the same public path.

---

## 8. Rate Methodology

### 8.1 Asset weighting

Each asset receives a weighted share of the **850,000,000 public redemption pool** using the published valuation methodology:

- **fungible tokens:** 7-day TWAP from top-pool market data;
- **NFT collections:** 7-day TWAP of collection floor prices; and
- **final allocation:** deterministic weighted split of the public redemption pool.

### 8.2 Final rate formulas

For fungible assets:

```text
asset_bucket = asset_weight × 850,000,000
fungible_redemption_rate = asset_bucket / final_redeemable_supply_for_asset
```

For NFTs:

```text
asset_bucket = asset_weight × 850,000,000
per_nft_redemption = asset_bucket / final_redeemable_nft_count_for_collection
```

### 8.3 Final rate publication conditions

Final fixed redemption rates are intended to be published only after:

- legacy reward materialization is complete;
- final Team treasury waivers are published; and
- the launch reconciliation package is signed off.

---

## 9. Post-Surrender Treatment of Legacy Assets

Surrendered legacy assets are **not** intended to remain in free public circulation.

Where possible they should be:

- burned;
- permanently locked;
- quarantined; or
- otherwise removed from circulation under the published merger controls.

The final launch package should document the operational path used for each asset type.

---

## 10. Deadline and After-Window Handling

- The ordinary public window closes **6 months** after launch.
- After the deadline, the normal public surrender path is closed unless an explicit extension or exceptional remedy is formally announced.
- Any unreleased or unredeemed portion of the public pool must follow the **published post-deadline policy** in the final launch package and should not be left vague.

---

## 11. Authoritative Launch Materials

If earlier reports, Discord posts, or snapshot-era drafts conflict with this document, the authoritative launch package is intended to be:

1. the final litepaper;
2. the final FAQ;
3. the final fixed rate table;
4. the legacy reward reconciliation / materialization package;
5. the final Team waiver publication; and
6. the final redemption instructions.

This document is intended to align those materials around the **redemption model**, not the old snapshot-only claimant model.

---

## Changelog

| Version | Date | Changes |
|---|---|---|
| 1.0 | 2026-03-05 | Initial eligibility rules |
| 2.0 | 2026-03-09 | Added snapshot-era team carve-out, claim window, and reserve-ledger logic |
| 2.1 | 2026-03-09 | Corrected NFT counts after CIP-68 filtering |
| 3.0 | 2026-03-10 | Rewrote policy around live surrender-and-redeem, removed fungible script redistribution as authoritative rule, added LP / custody / legacy reward materialization guidance, aligned with 15% validator reserve model |
