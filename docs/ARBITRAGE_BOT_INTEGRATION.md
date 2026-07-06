# Surrender API — cMATRA Arbitrage Bot Integration

The internal cMATRA arbitrage bot (see
[`saturnswap-filler/docs/ARBITRAGE_BOT.md`](https://github.com/Flux-Point-Studios/saturnswap-filler/blob/main/docs/ARBITRAGE_BOT.md))
uses this repo's surrender API as its **merge leg**: it buys AGENT/SHARDS on the
SaturnSwap order book when the cMATRA book trades rich, surrenders them here at the
rate-table-locked prices, and sells the minted cMATRA back into the book — pulling the
bonding-curve price in line with the merger-implied value.

This document is for the **surrender-service operator**: what the bot calls, what it
needs, and what it will refuse.

## Endpoints the bot drives

All three mutating calls carry `X-API-Secret` (set `SURRENDER_API_SECRET` on the bot):

1. `GET /pool-status` — startup check (warns when `window_open` is false).
2. `POST /build-surrender` — `{user_address: <bot address>, assets: [{asset_key:
   "AGENT"|"SHARDS", quantity_base}]}`. The bot only surrenders the fungible legs.
3. `POST /submit-surrender` — `{tx_cbor_hex: <partial witness set>, tx_hash}`. The bot
   signs the built tx hash with its own payment key (CIP-30-style partial witness set,
   the same shape `signTx(..., partialSign=true)` returns) within the sign-and-return
   window and submits immediately. 503 `POOL_SETTLING` responses are retried with
   exponential backoff (4 retries).

## Operator prerequisites

- **Allowlist the bot's address** (or open the window): `ALLOWED_USER_ADDRESSES` must
  accept the bot's `user_address`, else every build returns 403.
- **Canonical rate table**: the service must run with
  `RATE_TABLE_PATH=audit_pack/2026-04-19/rate_table_cmatra.json`. The code default
  points at the deprecated 2026-03-11 table — the bot cross-checks every quote against
  the canonical rational rates (`(quantity_base × rate_numerator) // rate_denominator`)
  and **refuses to merge** when the quote is short.

## What the bot refuses to sign

The bot never blind-signs. Before witnessing a built surrender it decodes the tx body
and enforces (`saturnswap-filler/src/arb/surrenderGuard.ts`):

1. exactly the requested legacy quantities leave its holdings;
2. at least the quoted cMATRA returns to its address;
3. its net ADA cost stays under a small cap (fee + min-UTxO shuffling, ~5 ADA);
4. every other asset it holds that enters the tx comes back in full.

Service-side changes that violate any of these (e.g. sourcing extra fee ADA from the
user's inputs, routing change elsewhere, batching unrelated user UTxOs) will make the
bot reject the build — coordinate before changing the surrender tx shape.
