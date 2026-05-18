# Security Policy

## Reporting a Vulnerability

If you discover a security issue in `matra-token-merger`, please report it
privately rather than via a public GitHub issue.

**Preferred channels:**

1. **GitHub Security Advisory** (private):
   <https://github.com/Flux-Point-Studios/matra-token-merger/security/advisories/new>
2. **Email:** `security@fluxpointstudios.com`

Please include:

- A clear description of the issue and its impact
- Steps to reproduce, ideally with a minimal proof-of-concept
- The component(s) affected (validator / mint policy / surrender API / cosigner API / off-chain tooling)
- Your name or handle if you'd like to be credited

We aim to acknowledge reports within **3 business days** and to provide a
remediation plan within **10 business days**. Critical issues affecting the
deployed cMATRA mint policy or surrender pool on Cardano mainnet will be
triaged on an accelerated timeline — especially issues that could grant
unauthorized minting or allow draining the surrender pool before the deadline.

Standard disclosure window is **90 days** from initial report. We may request
an extension for complex issues; we'll communicate that proactively.

## Scope

This policy covers:

- `onchain/claim_validator/` (Aiken Plutus V3 surrender pool validator)
- `onchain/flux_mint_policy/` (Aiken Plutus V3 one-shot mint policy for cMATRA)
- `services/surrender_api.py` (FastAPI surrender flow, Server A)
- `services/cosigner_api.py` (FastAPI co-signer service, Server B)
- `tools/` (off-chain CLIs: TWAP, snapshot, allocation, claim-vault builder,
  admin reclaim, process_surrender)
- `scripts/preprod_harness.py` + `scripts/red_team.py` (preprod rehearsal +
  adversarial suite)

The cMATRA token itself, once minted, lives entirely on Cardano L1 in
**v0**. The Materios partner-chain side (where `MATRA` on Substrate
will eventually mirror the Cardano `cMATRA`) is tracked separately in the
`Flux-Point-Studios/materios` repository.

## Status

This codebase has been **internally audited** with a full mainnet readiness
review at `audit_pack/2026-04-14/smart_contract_audit.html`. The PR #3 mint
policy ships under the same dual-admin model audited there. The companion
preprod rehearsal at `audit_pack/preprod/rehearsal_state.json` exercises 9
happy-path stages plus 8 red-team attacks (all rejected as expected) before
mainnet deploy.

Until launch, this project is **alpha** for the mint-policy path. The
surrender-pool path has live preprod proofs; the mint-policy path has unit
tests + audit only (no mainnet mint yet).

## Cryptographic surfaces

The validators and tooling use:

- `ed25519` for Cardano payment-key signatures (both admin keys, both
  cosigners, all claim signers)
- `blake2b_224` for Plutus V3 script-hash and verification-key-hash
  derivation
- Inline datums are the authoritative on-chain encoding for both validators
  (no Merkle proofs, no off-chain attestation)

## Out of scope

- Vulnerabilities in upstream dependencies that are already disclosed and
  tracked publicly (see `pip-audit` and `aiken check` output in CI).
- Issues that require physical access to either admin operator's signing-key
  material (Server A or Server B).
- Issues in `audit_pack/preprod/keys/*.json` — these are public test wallet
  metadata, never funded with mainnet ADA. The corresponding `.skey` files
  are gitignored and were generated for the preprod rehearsal only.
- Vulnerabilities in the Cardano node, Blockfrost, Koios, or TapTools APIs
  themselves. We rely on these as data oracles for off-chain pricing; report
  upstream.

## Hall of Fame

Disclosed issues will be credited here once a remediation has shipped, unless
the reporter prefers anonymity.

_(none yet — this repo is pre-public-release as of 2026-05-18.)_
