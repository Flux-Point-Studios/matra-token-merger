# cMATRA Merger — Mainnet Go/No-Go Runbook

This checklist must be completed sequentially before the mainnet cMATRA
mint and surrender pool are deployed. Every blank below must be filled
in by an operator. Once any step fails, halt and remediate before
proceeding.

The merger has **two on-chain pieces** and **one ceremony to mint them
into existence**:

1. `onchain/flux_mint_policy` — the 1 B cMATRA one-shot mint (parameterized
   by `seed_utxo`, `admin_pkh_1`, `admin_pkh_2`).
2. `onchain/claim_validator` — the surrender pool that pays out cMATRA in
   exchange for legacy assets (parameterized by `admin_pkh_1`, `admin_pkh_2`).

Both validators share the same two admin signers. Every mint, surrender,
and admin sweep requires **both** signatures.

---

## Phase A — Pre-deployment

### A.1 Admin keys exist on two separate machines

- [ ] `admin_1.skey` exists on Server A only — file mode `0400`, never copied
- [ ] `admin_2.skey` exists on Server B only — file mode `0400`, never copied
- [ ] `admin_pkh_1`: ________________________________________ (56 hex chars)
- [ ] `admin_pkh_2`: ________________________________________ (56 hex chars)
- [ ] Server A is on a different physical machine than Server B
- [ ] Both `.skey` files have an off-machine, gpg-encrypted backup stored
      separately from the operator's daily-driver laptop

### A.2 Aiken validators build + test green

- [ ] `aiken --version` reports `v1.1.21` (must match the audited version)
- [ ] `onchain/claim_validator/` — `aiken check` + `aiken build` clean
- [ ] `onchain/flux_mint_policy/` — `aiken check` + `aiken build` clean
- [ ] Both `verify_build.sh` scripts run clean (deterministic compile)

### A.3 Seed UTxO selected for mint policy

The mint policy is one-shot. The `seed_utxo` parameter pins the mint to a
specific (txhash, output_index) that must be consumed in the mint tx.
Pick a UTxO controlled by `admin_pkh_1`.

- [ ] `seed_utxo` txhash: ___________________________________________
- [ ] `seed_utxo` output_index: _____
- [ ] UTxO confirmed in `admin_pkh_1` address via Blockfrost or `cardano-cli`
- [ ] UTxO has at least 5 ADA (well above min-UTxO + tx fee)

### A.4 Parameters applied to both validators

- [ ] `flux_mint_policy` — `aiken blueprint apply` with three params
      in order (`seed_utxo`, `admin_pkh_1`, `admin_pkh_2`)
- [ ] `claim_validator` — `aiken blueprint apply` with three params
      in order (`admin_pkh_1`, `admin_pkh_2`, `deadline`).
      `deadline` is a `POSIX milliseconds Int`, sourced from
      `CLAIM_DEADLINE_POSIX_MS` in env. **This is baked into the script
      hash — changing it requires re-deploying the validator at a new
      script address.**
- [ ] `cmatra_policy_id` (mint policy applied hash): _______________________
- [ ] `surrender_pool_script_hash` (claim validator applied hash): _________
- [ ] `surrender_pool_address` (bech32): __________________________________

### A.5 Pool target value

- [ ] Public-pool target supply: **722,500,000 cMATRA** (722.5M × 10⁶ base units)
- [ ] Total mint: **1,000,000,000 cMATRA** (1 × 10¹⁵ base units)
- [ ] Difference (277.5M) is the Network Incentives Reserve, retained by
      admin_1 wallet post-mint for separate distribution
- [ ] Rate table: `audit_pack/2026-04-19/rate_table_cmatra.json` (canonical)

### A.6 Operator services deployed

- [ ] `services/cosigner_api.py` running on Server B
      (`docker compose -f services/deploy/docker-compose.cosigner.yml up`)
- [ ] `services/cosigner_api.py` `/health` returns `{"ok": true}` from
      Server A (LAN-only firewall confirmed: only Server A's IP allowed)
- [ ] `services/surrender_api.py` running on Server A
- [ ] `services/surrender_api.py` `/health` returns `{"ok": true}`
- [ ] Server A's `ADMIN_SKEY_PATH` points at `admin_1.skey`
- [ ] Server A's `COSIGNER_PKH` env matches `admin_pkh_2` exactly
- [ ] Server A's `COSIGNER_URL` resolves to Server B's LAN IP only

### A.7 Quarantine destination

When users surrender legacy assets, those assets move to a quarantine
address (not destroyed on-chain — Cardano native assets cannot be burned
unless the original policy supports it). The quarantine address holds
them indefinitely.

- [ ] `QUARANTINE_ADDRESS` (bech32): ______________________________________
- [ ] Quarantine wallet documented as inert (no spending key in production
      operator hands)

---

## Phase B — Cross-validation

- [ ] Koios cross-check passes for AGENT (`python -m tools.cross_check_holders`)
- [ ] Koios cross-check passes for SHARDS
- [ ] Holder counts match between Blockfrost and Koios for both
- [ ] Total on-chain supply matches `tools/config.py` constants for both

---

## Phase C — Preflight

- [ ] `evaluate_tx` preflight passes for a sample surrender tx
      (`python -m tools.process_surrender --preflight ...`)
- [ ] `evaluate_tx` preflight passes for the mint ceremony tx
      (built via `tools/build_surrender_pool.py --preflight`)

---

## Phase D — Token registry

- [ ] `audit_pack/token_registry/cMATRA.json` updated with final `cmatra_policy_id`
- [ ] CIP-26 token registry PR opened against the Cardano-foundation repo
- [ ] CIP-26 token registry PR merged (or queued for merge)

---

## Phase E — Preprod re-rehearsal (final)

Even after all the preprod rehearsals on `audit_pack/preprod/`, re-run the
full surrender-pool flow on preprod against the actual parameter set that
will go to mainnet (i.e. the test admin keys mirror the mainnet 2-of-2
shape, even if the PKHs differ).

- [ ] Mint policy preflight on preprod (with test admin keys)
- [ ] Pool initialization on preprod
- [ ] 5 surrender txs on preprod (admin co-signed) — all settle within 2 blocks
- [ ] All red-team tests pass on preprod:
  - [ ] Single-admin surrender attempt (rejected)
  - [ ] No-admin surrender attempt (rejected)
  - [ ] Wrong redeemer (rejected)
  - [ ] Datum swap (rejected)
  - [ ] Mint with wrong seed_utxo (rejected)
  - [ ] Mint over the supply cap (rejected)
  - [ ] Mint a different asset name (rejected)
  - [ ] `ProcessSurrender` **before** deadline + both admins (accepted)
  - [ ] `ProcessSurrender` **after** deadline + both admins (rejected —
        the validator enforces `is_entirely_before(tx.validity_range, deadline)`
        on this path, mirrored by `claim_validator.ak` unit test
        `process_surrender_after_deadline_fails`)
  - [ ] `AdminWithdraw` **before** deadline + both admins (rejected —
        the symmetric `is_entirely_after` check)
  - [ ] `AdminWithdraw` **after** deadline + both admins (accepted)

---

## Phase F — Python test suite

- [ ] `pytest -q` passes — all tests green
- [ ] Test count: _____ (expected: 160+)
- [ ] `aiken check` passes for both validators

---

## Phase G — Final confirmation

- [ ] Claim deadline timestamp confirmed:
  - POSIX ms: ________________
  - UTC: _____________________
  - Human-readable: ___________
  - Sanity: deadline is 6 months ± 2 weeks from mint date
- [ ] Admin PKHs match the actual signing keys:
  - `admin_pkh_1` from env: __________ (matches `admin_1.skey` derivation on Server A)
  - `admin_pkh_2` from env: __________ (matches `admin_2.skey` derivation on Server B)
- [ ] `cmatra_policy_id` finalized + frozen: ______________________________
- [ ] `surrender_pool_address` finalized + frozen: ________________________
- [ ] Git tag `mainnet-v1.0.0` created on the release commit

---

## Execution order

1. **Mint ceremony** — Server A builds the mint tx, signs with `admin_1`,
   sends to Server B which signs with `admin_2`, Server A submits.
   This tx mints 1 B cMATRA, pays 722.5M into the surrender pool address,
   and retains 277.5M at `admin_1` for the reserve.
2. **Open the surrender window** — flip the
   [flux1](https://github.com/realdecimalist/flux1) front-end's
   `WINDOW_OPEN` flag to `true` and deploy. Surrender API begins accepting
   user txs.
3. **Monitor surrenders** — watch `surrender_api.py` logs + the surrender
   pool address for incoming legacy assets and outgoing cMATRA.
4. **After deadline** — both admins co-sign a sweep tx pulling remaining
   pool cMATRA back to the reserve (`python -m tools.admin_reclaim --submit`).

---

## Emergency procedures

### Deadline extension

**The deadline is a compile-time parameter of `claim_validator` and is
baked into the script hash.** `ProcessSurrender` enforces
`is_entirely_before(tx.validity_range, deadline)` on chain, and
`AdminWithdraw` enforces the symmetric `is_entirely_after` check. After
the on-chain deadline passes, surrender transactions are **rejected at
submission** — there is no soft / operational extension.

Extending the window requires:

1. Deploying a new `claim_validator` instance with a later `deadline`
   parameter (new script address).
2. Both admins co-signing an `AdminWithdraw` against the old pool to
   recover the remaining cMATRA.
3. Both admins co-signing a fresh pool-initialization tx that pays
   the recovered cMATRA into the new script address.
4. Updating `surrender_api.py` `SURRENDER_SCRIPT_ADDRESS` env var and
   redeploying the service.

Plan accordingly — operators who need extension capability should
either pick a generous initial deadline or build the new-validator
ceremony into their launch playbook.

### Admin key compromise — pre-deadline

Both admins must sign to mint or to drain the pool. A single-key
compromise gives the attacker no on-chain authority. If both keys are
compromised, halt all surrender API traffic immediately (flip flux1
`WINDOW_OPEN` to `false`) and rotate both keys before resuming. The
remaining pool cMATRA is recoverable to a new admin pair only by both
current admins co-signing a sweep — there is no on-chain recovery if
both keys are simultaneously lost.

### Admin key loss — post-deadline

If both keys are lost after the deadline, any unswept pool cMATRA is
permanently locked at the surrender-pool script address. There is no
script-side recovery. Mitigation: ensure both `.skey` files have
off-machine gpg-encrypted backups before the mint ceremony.

### Blockfrost outage

Fall back to Koios for read operations. Submission can use any Cardano
submit API endpoint, including a local cardano-node.

### Surrender API DoS

If `surrender_api.py` is overwhelmed or compromised, flip flux1
`WINDOW_OPEN` to `false`. The front-end will refuse to construct new
surrender txs. Already-built txs in flight that have not yet been
submitted are inert — they can only spend a pool UTxO with both admin
signatures, and the cosigner refuses to sign a tx it didn't authorize.
