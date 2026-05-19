# cMATRA Token Merger

Cardano-native consolidation of seven legacy Flux Point Studios assets into a
single new token — **cMATRA** — via a dual-admin surrender pool and a
parameterized one-shot mint policy.

| In (legacy) | Policy ID | Type |
|---|---|---|
| AGENT (Talos) | `97bbb7db…174bec` | fungible (0-dec) |
| SHARDS | `ea153b5d…15b243a` | fungible (6-dec) |
| FLUX_PASS | `0889a2d5…64f683a` | NFT (401 ct) |
| SE_BRAWLERS | `25c75bbf…dde7eafc` | NFT (242 ct) |
| BRAWL_PASS_ETD | `d3a197c4…529a02d2` | NFT (44 ct) |
| T1_ADAM_PASS | `b4689145…a20332f` | NFT (43 ct) |
| T2_ADAM_PASS | `06a64965…fb1164b9` | NFT (25 ct) |

| Out (new) | Decimals | Supply cap | On-chain symbol |
|---|---|---|---|
| **cMATRA** | 6 | 1,000,000,000 (1 × 10¹⁵ base units) | `cMATRA` (hex `634d41545241`) |

cMATRA stays Cardano-side in v0. The Materios partner-chain `MATRA` token
(same total cap, Substrate-side) is a separate track — see
[`Flux-Point-Studios/materios`](https://github.com/Flux-Point-Studios/materios).

---

## Status

| Component | Status |
|---|---|
| `claim_validator` (surrender pool, Aiken Plutus V3) | Audited, preprod-rehearsed |
| `flux_mint_policy` (one-shot mint, Aiken Plutus V3) | Audited, unit-tested, **mainnet mint pending** |
| `services/surrender_api.py` (Server A) | Containerized, preprod-tested |
| `services/cosigner_api.py` (Server B) | Containerized, preprod-tested |
| `tools/*` (off-chain pipeline) | ~160 pytest tests passing |
| Preprod rehearsal | 9 / 9 stages + 8 / 8 red-team tests pass |
| Mainnet deploy | Pending: admin keys, deadline, deploy ceremony |

**Pre-mainnet readiness:** see [`audit_pack/MAINNET_RUNBOOK.md`](audit_pack/MAINNET_RUNBOOK.md).

---

## Architecture

The merger has **two on-chain pieces** and **two operator services**.

### On-chain components

```
┌─────────────────────────────┐      ┌─────────────────────────────┐
│   onchain/flux_mint_policy  │      │   onchain/claim_validator   │
│   ─────────────────────────  │      │   ─────────────────────────  │
│   one-shot 1 B cMATRA mint  │      │   dual-admin surrender pool │
│   parameterized by:         │      │   parameterized by:         │
│     • seed_utxo             │      │     • admin_pkh_1           │
│     • admin_pkh_1           │      │     • admin_pkh_2           │
│     • admin_pkh_2           │      │                             │
│                             │      │   pool UTxOs hold cMATRA;   │
│   one transaction ever,     │      │   each surrender tx is      │
│   exact supply cap          │      │   admin-co-signed and       │
│                             │      │   atomically pays out at    │
│                             │      │   fixed rate-table prices   │
└─────────────────────────────┘      └─────────────────────────────┘
                │                                  ▲
                └──────── 1 B cMATRA ──────────────┘
                         minted into pool
```

**`flux_mint_policy`** enforces (audited at `audit_pack/2026-04-14/`):

1. **One-shot mint** — parameterized by an `OutputReference` (`seed_utxo`).
   The mint tx must consume that UTxO; once spent, no second mint is possible.
2. **Exact supply cap** — net mint of cMATRA must equal exactly
   `1_000_000_000 × 10⁶ = 1 × 10¹⁵` base units.
3. **Single asset name** — only `cMATRA` (hex `634d41545241`); any other
   asset name under this policy is rejected.
4. **Dual-admin signatures** — both `admin_pkh_1` AND `admin_pkh_2` must sign.
5. **Permissionless burn** — any holder can burn cMATRA (negative net mint)
   with no signature.

**`claim_validator` (surrender pool)** enforces:

1. **Dual-admin signatures** — both admin keys must sign every surrender or
   admin sweep.
2. **No claimant-side keys** — the user wallet signs the surrender of legacy
   assets; the pool pays out cMATRA at the rate-table-locked price.
3. **Admin sweep after deadline** — once the 6-month window closes, remaining
   pool cMATRA can be reclaimed by both admins co-signing.

### Off-chain services

```
                      flux1 website (Next.js)
                              │
                              │ HTTPS + X-API-Secret
                              ▼
                  ┌─────────────────────────┐
                  │  surrender_api.py       │
                  │  (Server A)             │
                  │  • holds admin_1.skey   │
                  │  • builds surrender tx  │
                  └───────────┬─────────────┘
                              │ HTTPS (LAN only)
                              │ X-Cosigner-Secret
                              ▼
                  ┌─────────────────────────┐
                  │  cosigner_api.py        │
                  │  (Server B — separate   │
                  │  physical host)         │
                  │  • holds admin_2.skey   │
                  │  • signs only when tx   │
                  │    matches expected     │
                  │    surrender pattern    │
                  └─────────────────────────┘
```

Server A and Server B are deployed on **separate physical machines** so that
compromising one does not yield minting or pool-drain authority. The
co-signer service refuses to sign any transaction that doesn't match the
expected surrender / mint / admin-sweep shape.

---

## Quick start

### Prerequisites

- Python 3.10+ (3.11 recommended)
- [Aiken](https://aiken-lang.org/) `v1.1.21` (pin matches the audited version)
- Docker + Docker Compose (for running `surrender_api` / `cosigner_api`)

### Setup

```bash
git clone https://github.com/Flux-Point-Studios/matra-token-merger.git
cd matra-token-merger

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example env.local
# Edit env.local with your Blockfrost / TapTools API keys
```

### Run tests

```bash
# Python pipeline (~160 tests)
pytest

# On-chain Plutus V3 validators (Aiken)
( cd onchain/claim_validator && aiken check && aiken build )
( cd onchain/flux_mint_policy && aiken check && aiken build )
```

### Verify reproducible builds

The on-chain compiled blueprints (`plutus.json`) are deterministic given the
pinned Aiken version. Each `onchain/*/` directory has a `verify_build.sh`
script that recompiles and reports the unapplied script hash for independent
verification.

```bash
bash onchain/flux_mint_policy/verify_build.sh
bash onchain/claim_validator/verify_build.sh
```

### Run preprod rehearsal

The preprod harness exercises all 9 deployment stages plus 8 red-team
attacks against a parameterized validator with synthetic test wallets.

```bash
NETWORK=preprod python -m scripts.preprod_harness
# Resume from a specific stage:
NETWORK=preprod python -m scripts.preprod_harness --skip-to-stage 6
# Run the red-team suite:
NETWORK=preprod python -m scripts.red_team
```

---

## Deployment (mainnet)

The mainnet deploy is a single ceremony, run once. The high-level path:

1. **Generate admin keys** — two separate physical machines, one
   `admin_1.skey` on Server A, one `admin_2.skey` on Server B. See
   [`services/deploy/setup-cosigner.sh`](services/deploy/setup-cosigner.sh)
   for the Server B half.
2. **Pick a seed UTxO** controlled by `admin_pkh_1` — this becomes the
   one-shot-mint anchor.
3. **`aiken blueprint apply`** parameters to each validator:
   - `flux_mint_policy`: three params in order — `seed_utxo`,
     `admin_pkh_1`, `admin_pkh_2`
   - `claim_validator`: three params in order — `admin_pkh_1`,
     `admin_pkh_2`, `deadline` (POSIX milliseconds, the cutoff after
     which surrender transactions are rejected on-chain and admin
     withdraw becomes valid)

   Result: `plutus.json` blueprints with the applied `cmatra_policy_id`
   (mint policy) and `surrender_pool_address` (script address).
4. **Mint 1 B cMATRA** in one transaction that consumes `seed_utxo` and
   pays the full supply into the surrender pool address (both admins
   co-sign). After this tx, the policy can never mint again.
5. **Deploy `surrender_api`** + `cosigner_api` containers, point flux1
   front-end at the public surrender_api URL, set `WINDOW_OPEN = true`.
6. **Six-month surrender window** — users submit legacy assets via flux1;
   each surrender is admin co-signed.
7. **After deadline** — both admins co-sign a sweep tx to retire any
   unclaimed cMATRA from the pool.

Full step-by-step go/no-go checklist:
[`audit_pack/MAINNET_RUNBOOK.md`](audit_pack/MAINNET_RUNBOOK.md).

---

## Security model

The threat model is **dual-admin compromise**:

- Compromising Server A (admin_1) without Server B grants no minting and no
  pool draining. Surrender transactions require both signatures.
- Compromising Server B (admin_2) without Server A grants no minting and
  no pool draining for the same reason. The cosigner service additionally
  refuses to sign anything that doesn't match the expected transaction
  pattern (this is defense-in-depth on top of the dual-signature requirement).
- Compromising the flux1 front-end can at worst block surrenders (DoS); it
  cannot mint or drain. The front-end never holds either admin key.
- Compromising user wallets is out of scope — that's a per-user problem,
  not a protocol-level one.

Once the surrender deadline passes, the only on-chain operation the admins
can perform is the sweep of unclaimed cMATRA. They cannot mint more, they
cannot recover surrendered legacy assets (those moved to a quarantine
address at surrender time), and they cannot impose new surrender terms.

**Audit + adversarial testing:**

| Test | Attack vector | Result |
|---|---|---|
| Wrong signer (single-admin) | Only one of the two admins signs | PASS (rejected) |
| No signers | Surrender with no admin sigs | PASS (rejected) |
| Wrong redeemer | Garbage CBOR redeemer data | PASS (rejected) |
| Datum swap | Modify pool datum to redirect cMATRA | PASS (rejected) |
| Mint policy bypass | Mint cMATRA with bogus seed_utxo | PASS (rejected) |
| Mint over cap | Mint > 1 B in the parameterized tx | PASS (rejected) |
| Mint wrong asset name | Mint under policy with different name | PASS (rejected) |
| `ProcessSurrender` after deadline | Both admins try to surrender past deadline | PASS (rejected) |
| `AdminWithdraw` before deadline | Both admins try to sweep early | PASS (rejected) |

The deadline check on `ProcessSurrender` is enforced on chain via
`is_entirely_before(tx.validity_range, deadline)` (see
`onchain/claim_validator/validators/claim_validator.ak:69-70`). The mirror
check `is_entirely_after` is enforced on `AdminWithdraw`. The deadline
is a compile-time parameter of the validator and is baked into the
script hash — it cannot be changed without redeploying at a new
address.

Full audit at [`audit_pack/2026-04-14/smart_contract_audit.html`](audit_pack/2026-04-14/smart_contract_audit.html).

---

## Repository structure

```
matra-token-merger/
  onchain/
    claim_validator/                # surrender pool (Aiken Plutus V3)
      validators/claim_validator.ak
      plutus.json                   # compiled blueprint
      verify_build.sh
    flux_mint_policy/               # one-shot mint policy (Aiken Plutus V3)
      validators/flux_mint_policy.ak
      plutus.json
      verify_build.sh
      README.md                     # parameter spec + ceremony
  services/
    surrender_api.py                # Server A FastAPI (holds admin_1)
    cosigner_api.py                 # Server B FastAPI (holds admin_2)
    deploy/
      setup-cosigner.sh             # Server B bootstrap script
      docker-compose.cosigner.yml   # Server B container
      Dockerfile.cosigner
      .env.cosigner.example
  tools/                            # off-chain pipeline (~160 pytest tests)
    twap_snapshot_pools.py          # multi-pool TWAP
    flux_merge_valuation_int.py     # integer-only valuation
    snapshot_allocate_flux.py       # holder snapshot + allocation
    build_surrender_pool.py         # pool initialization
    process_surrender.py            # per-tx surrender flow
    admin_reclaim.py                # post-deadline sweep
    cardano_utils.py                # address / datum / param helpers
    api_clients.py                  # Blockfrost + TapTools + Koios
    config.py                       # env loading
  scripts/
    preprod_harness.py              # 9-stage rehearsal
    red_team.py                     # adversarial test suite
  tests/                            # ~160 pytest tests
  audit_pack/
    2026-04-14/                     # smart contract audit
    2026-04-19/                     # canonical mainnet rate table
    preprod/                        # preprod rehearsal state + test wallets
    MAINNET_RUNBOOK.md              # go/no-go checklist
  .env.example
  pyproject.toml
```

---

## Contributing

PRs welcome. We require:

- All Python tests passing (`pytest`)
- Aiken validators pass `aiken check` for both `claim_validator/` and
  `flux_mint_policy/`
- Signed commits (`git config --global commit.gpgsign true` + SSH signing
  key registered with GitHub — see materios-intent-settlement's
  [`docs/onboarding-signing.md`](https://github.com/Flux-Point-Studios/materios-intent-settlement/blob/main/docs/onboarding-signing.md)
  for the recipe; the same setup applies to this repo)
- Security-sensitive changes (validators, surrender / mint / sweep paths,
  admin-key handling) go through `/security-review` before merge

See [`SECURITY.md`](SECURITY.md) for vulnerability disclosure.

---

## Legal & disclosures

The cMATRA Merger Portal is operated by **Flux Point Studios, Inc.**, a
Delaware corporation. The authoritative legal materials live in three
places — please review them before surrendering legacy assets:

- **Portal disclosures** — the always-visible "cMATRA Merger Disclosures"
  block on the live Portal at
  [fluxpointstudios.com/matra-merger](https://fluxpointstudios.com/matra-merger).
- **Docs** — the "Legal and Disclosures" section in the cMATRA Token
  Merger docs:
  [github.com/Flux-Point-Studios/docs](https://github.com/Flux-Point-Studios/docs/blob/main/materios/cmatra-token-merger/README.md#legal-and-disclosures)
  (with companion FAQ entries 41b–41h in the same docs folder).
- **Site-wide terms** — the FPS
  [Terms of Service](https://fluxpointstudios.com/tos) and
  [Privacy Policy](https://fluxpointstudios.com/privacy-policy), each of
  which is incorporated by reference into the Portal disclosures.

Use of the Portal is governed by all three documents together. Among
other things, surrender transactions are **final and irreversible**,
cMATRA is a **utility token** (no equity, dividend, revenue, or
investment rights, and no future-value guarantee), the on-chain code is
open source but **not** third-party audited, and you are solely
responsible for tax consequences in your jurisdiction. The Terms of
Service controls in the event of any conflict.

This repository is provided for transparency and independent inspection
under its dual Apache-2.0 / MIT license (see below). Nothing in this
repository — including this README — is legal, tax, or investment advice.

---

## License

Dual-licensed at your option:

- [Apache License 2.0](LICENSE-APACHE)
- [MIT License](LICENSE-MIT)
