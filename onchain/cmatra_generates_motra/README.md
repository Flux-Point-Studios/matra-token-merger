# cMATRA registration validator (`cmatra_generates_motra`)

A Plutus V3 validator that lets a cMATRA holder register a Cardano stake
credential to a Materios account. Registration mints an auth NFT that sits at
the validator's own address with an inline datum binding the credential to a
32-byte Materios `AccountId`. A Materios observer reads these registrations and
credits MOTRA to the account in proportion to the cMATRA held under the
credential.

It is a fork of Midnight's `cnight_generates_dust.ak`
([midnight-reserve-contracts](https://github.com/midnightntwrk/midnight-reserve-contracts)
at commit `6749c82a509442010bbd0252c00491c7e55ae1c5`, Apache-2.0, Copyright (C)
Midnight Foundation), which does the same for cNIGHT and DUST. The validator
file carries the license notice and lists what changed.

**Status: not deployed.** It needs an external review before any deployment.

## Datum and NFT

```aiken
pub type RegistrationDatum {
  stake_credential: Credential,   // VerificationKey(hash) or Script(hash)
  materios_account: ByteArray,    // exactly 32 bytes: the Materios AccountId
}
```

The NFT's policy id is the script hash. Its asset name is derived from the
credential, so one credential always gets the same name:

| Credential | Asset name (29 bytes) |
|---|---|
| `VerificationKey(h)` | `0x00 ‖ h` |
| `Script(h)` | `0x01 ‖ h` |

## Paths

The validator has one `else` handler covering every script purpose. The
`MG` ids map to Midnight's `DG` ids in its `docs/governance/validators.md`.

**Register** (mint, redeemer `Create`)
- MG-1: the first output paid to the script's payment credential holds an
  inline datum that decodes to `RegistrationDatum` exactly (DG-2).
- MG-2: `materios_account` is exactly 32 bytes (DG-3 allowed up to 33).
- MG-3: a key credential signs the transaction; a script credential is
  withdrawn from, normally with a zero withdrawal (DG-4, DG-5).
- MG-4: the transaction mints exactly one token under this policy, named for
  the datum's credential, and that output holds only lovelace and that token.

**Deregister** (mint, any other redeemer, and spend)
- MG-5: every token of this policy in the mint is burned, and the number burned
  equals the number of inputs spent from the script (DG-11; upstream also
  allowed only a single asset name).
- MG-6: each spent registration is authorized by its own datum's credential and
  burns the NFT named for that credential (DG-13 to DG-15).

**Update the account** (spend plus a zero withdrawal from the script)
- MG-7: each spent registration has a matching output, in input order, at the
  head of the outputs: same payment credential, inline datum, a 32-byte account,
  authorized by the new datum's credential, holding only lovelace and the NFT
  named for that credential (DG-6 to DG-10). No token of this policy may be
  minted or burned, so an update can change the account but never the
  credential.

**Publish**
- Registering the script's stake credential is allowed, so the zero
  withdrawal can run. Every other certificate fails.

**Anchors**
- MG-8: the minting path compares its own policy id with the cMATRA policy id
  and with the Materios chain id. No policy id can equal them; the comparison
  keeps both constants in the script hash, as Midnight's DG-12 does for the
  cNIGHT policy id and the candidate nonce.

Midnight's DG-1 (the transaction must spend `inputs[0]`) is dropped: every
transaction spends its own first input, so it never constrained anything.

## Parameters

| Value | Where | Why |
|---|---|---|
| cMATRA policy id | `aiken.toml` config: `default` is mainnet `7ff33a55…dc66`, `preprod` is the rehearsal policy | Ties the registration script to the token that generates MOTRA. `tests/test_cmatra_generates_motra_config.py` holds both equal to the policies the merger minted. |
| `materios_chain_id` | validator parameter, applied at deployment | Gives each Materios network its own script address, so a registration for one network never counts on another. |

`materios_chain_id` must be a fixed name for the network, such as
`materios-mainnet`, chosen once and never changed. Do not use the genesis hash
or the chain spec id: both change when a network is relaunched, and a new value
means a new script address, which would void every existing registration.

Applying it (CBOR byte string: header `0x40 + length` for up to 23 bytes):

```bash
aiken build
aiken blueprint apply -o plutus.mainnet.json "50$(printf materios-mainnet | xxd -p)"
aiken blueprint hash -i plutus.mainnet.json   # the policy id and script hash
```

## Observer rules

The observer enforces one registration per stake credential; the chain cannot
see other registrations. For a deployment with script hash `H`:

1. Watch UTxOs whose payment credential is `Script(H)`. Ignore any that do not
   hold exactly one token under policy `H`; anyone can pay garbage to the
   address, but it carries no NFT and can never be spent.
2. The NFT's name identifies the credential (`0x00`/`0x01` then the hash). The
   validator only ever lets that NFT sit beside a datum for the same
   credential, with a 32-byte account.
3. A credential is registered while exactly one live UTxO holds an NFT with its
   name, and its MOTRA goes to that UTxO's `materios_account`. With two or more
   it generates nothing until the extras are burned, which is Midnight's rule.
   Only the credential can mint or burn its own name, so nobody else can add a
   duplicate to void a registration, or move one.
4. Holdings are the cMATRA (`7ff33a55…dc66` / `634d41545241`) at base addresses
   whose stake part is the registered credential.

## Build and verify

```bash
aiken --version        # v1.1.21: other versions give other hashes
aiken check            # adversarial tests included
aiken build            # writes plutus.json (committed; CI diffs it)
./verify_build.sh      # pins the unapplied hash; checks the chain id moves it
```

`aiken build --env preprod` builds against the preprod rehearsal policy.
