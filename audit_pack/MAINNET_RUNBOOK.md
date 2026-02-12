# FLUX Merger — Mainnet Go/No-Go Runbook

This checklist must be completed sequentially before the mainnet merge executes.

---

## Pre-deployment

- [ ] Aiken build + check passes (`aiken build && aiken check` in `onchain/claim_validator/`)
- [ ] All on-chain tests pass (claimant claim, admin reclaim before/after deadline, wrong key, third party)
- [ ] Parameters applied via `aiken blueprint apply`:
  - `admin_pkh`: ________________________________________ (56 hex chars)
  - `deadline`:  ________________________________________ (POSIX ms)
  - Deadline in UTC: ____________________________________
- [ ] Script hash pinned: __________________________________________
- [ ] Script address derived: ______________________________________
- [ ] Verify script hash matches blueprint `hash` field
- [ ] Funding calculator run (`python -m tools.funding_calculator --allocations-csv ...`)
  - Grand total ADA: __________
  - Admin wallet funded with sufficient ADA

## Cross-validation

- [ ] Koios cross-check passes for AGENT (`python -m tools.cross_check_holders`)
- [ ] Koios cross-check passes for SHARDS
- [ ] Holder counts match between Blockfrost and Koios
- [ ] Total supply matches for both tokens

## Preflight

- [ ] `evaluate_tx` preflight passes for a sample distribution batch
  - `python -m tools.build_claim_utxos_flux --preflight ...`
- [ ] `evaluate_tx` preflight passes for a sample claim tx
  - `python -m tools.claim_flux_indexed --preflight ...`

## Token registry

- [ ] `audit_pack/token_registry/FLUX.json` updated with final policy ID
- [ ] JSON submitted to Cardano Token Registry (PR opened)

## Preprod re-rehearsal

- [ ] Preprod stages 5-9 re-pass with parameterized validator
  - `NETWORK=preprod python -m scripts.preprod_harness --skip-to-stage 5`
- [ ] 5/5 happy-path claims succeed
- [ ] All red-team tests pass:
  - [ ] Wrong signer (rejected)
  - [ ] Double claim (rejected)
  - [ ] Wrong redeemer (rejected/accepted by design)
  - [ ] Datum swap (rejected)
  - [ ] Index poisoning (rejected)
  - [ ] Franken address (rejected)
  - [ ] Admin reclaim before deadline (rejected)
  - [ ] Admin reclaim after deadline (accepted)

## Python test suite

- [ ] `pytest` passes — all tests green
- [ ] Test count: _____ (target: 160+)

## Final confirmation

- [ ] Deadline timestamp confirmed:
  - POSIX ms: ________________
  - UTC: _____________________
  - Human-readable: ___________
  - Verify: `1.7T < value < 2.0T` (reasonable range check)
- [ ] Admin PKH matches actual signing key:
  - PKH from env: ____________
  - PKH from skey: ___________
  - Match: [ ] Yes
- [ ] FLUX mint policy ID finalized: __________________________________
- [ ] Allocations CSV hash (SHA-256): _________________________________
- [ ] Git tag created for release commit

---

## Execution order

1. Mint FLUX (Phase 5)
2. Build claim UTxOs in batches (Phase 6) — with `--preflight`
3. Build claim index (Phase 7)
4. Announce claim window opening
5. Monitor claims
6. After deadline: run admin reclaim (`python -m tools.admin_reclaim --submit`)

---

## Emergency procedures

### Claim window extension
If the deadline needs extending, a new validator must be deployed with a later deadline.
The old script address UTxOs remain spendable by claimants (the claimant path has no time check).
Admin would need to re-deploy unclaimed UTxOs to the new script address.

### Admin key compromise
The admin key can only reclaim AFTER the deadline. Before the deadline, the admin key has no special power over claim UTxOs. If compromised before deadline, rotate the key and redeploy with a new admin_pkh.

### Blockfrost outage
Fall back to Koios for read operations. Submission can use any Cardano submit API endpoint.
