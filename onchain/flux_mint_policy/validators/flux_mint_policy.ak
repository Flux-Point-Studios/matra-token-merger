/// cMATRA Mint Policy (One-Shot, Dual-Admin, Supply-Capped, Burn-Permissive)
///
/// Location: onchain/flux_mint_policy/validators/flux_mint_policy.ak
///
/// The mint policy for cMATRA — Cardano's representation of the MATRA token.
/// It governs the *entire* 1B cMATRA token supply. Once burned out, the policy
/// is permanently inert; once minted, it is permanently capped.
///
/// SECURITY INVARIANTS (all enforced in `mint` below — see referenced lines):
///
///   I1. One-shot mint: any mint path requires consuming `seed_utxo` (an
///       `OutputReference` parameterized at compile time). Because UTxOs
///       are globally unique and can only be consumed once across all of
///       Cardano's history, a second initial mint is structurally impossible
///       — the seed UTxO no longer exists after the first mint tx settles.
///       Enforced via `seed_consumed` and the mint-path conjunction below.
///
///   I2. Exact supply cap: the *only* permitted mint quantity is exactly
///       `total_supply_base = 1_000_000_000_000_000` (1 billion × 10^6 for
///       6-decimal cMATRA). Any other positive net mint of cMATRA under
///       this policy is rejected. Enforced via `qty == total_supply_base`
///       in the mint path.
///
///   I3. Single asset name: the policy may only ever touch one asset name
///       — `#"634d41545241"` ("cMATRA"). If the policy's contribution to
///       `tx.mint` contains any other asset name (including in addition to
///       cMATRA), the tx is rejected. Enforced via `single_asset_only`
///       at the top of the mint handler.
///
///   I4. Dual-admin signatures: every initial-mint tx must be signed by
///       both `admin_pkh_1` AND `admin_pkh_2`. Same dual-signer model as
///       the surrender pool validator: compromising one server does not
///       grant minting authority. Enforced via `both_signed` in the
///       mint path.
///
///   I5. Permissionless burn: anyone holding cMATRA can burn it (negative
///       net mint). No signature, no seed, no quantity constraints other
///       than "still only cMATRA under this policy". This is the safety
///       valve — mint policies that block burns get tokens permanently
///       stuck. Enforced via the `qty < 0` branch.
///
/// Parameters (applied at compile time via `aiken blueprint apply`):
///   - seed_utxo:    OutputReference that must be consumed for the initial mint
///   - admin_pkh_1:  VerificationKeyHash of the primary admin key (Server A)
///   - admin_pkh_2:  VerificationKeyHash of the co-signer key (Server B)
///
/// Redeemer: Void (unit) — no per-mint data needed; the rules are structural.
///
/// Used by: off-chain Python tooling in tools/ and scripts/ to build the
/// single initial-mint transaction (post-mint, the policy is referenced
/// only for burns by token holders, which require no admin involvement).

use aiken/collection/dict
use aiken/collection/list
use aiken/crypto.{VerificationKeyHash}
use cardano/address.{Address, VerificationKey}
use cardano/assets.{PolicyId, Value, from_asset, merge, quantity_of, tokens,
  zero}
use cardano/transaction.{Input, NoDatum, Output, OutputReference, Transaction,
  placeholder}

// =========================================================================
// Constants (audit-stable — NOT parameters, baked into the script hash)
// =========================================================================

/// Asset name hex for "cMATRA". 6 ASCII bytes: 0x63 0x4d 0x41 0x54 0x52 0x41.
/// Must match `MERGE_TOKEN_TICKER` in tools/config.py encoded as ASCII.
const cmatra_asset_name: ByteArray = #"634d41545241"

/// Total supply in base units: 1,000,000,000 × 10^6 (cMATRA is 6-decimal).
/// Must match `MERGE_TOKEN_SUPPLY_BASE` in tools/config.py.
const total_supply_base: Int = 1_000_000_000_000_000

// =========================================================================
// Validator
// =========================================================================

/// Parameterized mint policy. The redeemer is unused — all rules are derived
/// from the structural shape of the transaction.
validator flux_mint_policy(
  seed_utxo: OutputReference,
  admin_pkh_1: VerificationKeyHash,
  admin_pkh_2: VerificationKeyHash,
) {
  mint(_redeemer: Data, policy_id: PolicyId, tx: Transaction) {
    // ----- INVARIANT I3: single asset name under this policy -----
    // The policy's contribution to tx.mint must touch *only* cMATRA. We
    // pull the per-asset dict for this policy and require exactly one
    // entry, keyed by `cmatra_asset_name`. Any other asset name (even
    // alongside cMATRA) is fatal.
    let policy_tokens = tokens(tx.mint, policy_id)
    let single_asset_only =
      dict.size(policy_tokens) == 1 && dict.has_key(
        policy_tokens,
        cmatra_asset_name,
      )

    // The signed quantity of cMATRA in this tx: positive = mint, negative =
    // burn, zero = no-op (rejected — see below). Value invariants guarantee
    // this is the *net* across any mint/burn pairs in the same tx.
    let qty = quantity_of(tx.mint, policy_id, cmatra_asset_name)

    // ----- Dual-admin signature check (reused on the mint path) -----
    // INVARIANT I4
    let admin_1_signed = list.has(tx.extra_signatories, admin_pkh_1)
    let admin_2_signed = list.has(tx.extra_signatories, admin_pkh_2)
    let both_signed = admin_1_signed && admin_2_signed

    // ----- INVARIANT I1: seed UTxO must be consumed -----
    // Look for the parameterized OutputReference among tx.inputs.
    let seed_consumed =
      list.any(tx.inputs, fn(i) { i.output_reference == seed_utxo })

    // ----- Path selection by sign of net quantity -----
    when single_asset_only is {
      False -> False
      True ->
        if qty > 0 {
          // MINT PATH — all of: dual-sig, seed-consumed, exact-cap.
          // INVARIANT I2 + I4 + I1.
          both_signed && seed_consumed && qty == total_supply_base
        } else if qty < 0 {
          // BURN PATH — permissionless. INVARIANT I5.
          // Single-asset constraint already enforced above.
          True
        } else {
          // qty == 0: a single-asset-keyed entry with quantity zero is
          // impossible under Value invariants (zero-qty entries are
          // pruned at construction). Belt-and-braces reject for clarity.
          False
        }
    }
  }

  else(_) {
    fail
  }
}

// =========================================================================
// Tests
// =========================================================================
//
// All tests target the structural rules above. Each test name encodes the
// invariant it exercises; failure paths use the `test ... fail { ... }`
// form (Aiken expects the body to fail).

// ----- Test constants -----

const test_admin_1_pkh: VerificationKeyHash =
  #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaabb"

const test_admin_2_pkh: VerificationKeyHash =
  #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbcc"

const test_random_pkh: VerificationKeyHash =
  #"ccccccccccccccccccccccccccccccccccccccccccccccccccccccdd"

// 28-byte zero policy_id stand-in. The runtime fills this in for real txs.
const test_policy_id: PolicyId =
  #"11111111111111111111111111111111111111111111111111111111"

const wrong_asset_name: ByteArray = #"deadbeef"

fn test_seed_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"abababababababababababababababababababababababababababababababab",
    output_index: 7,
  }
}

fn other_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
    output_index: 0,
  }
}

/// Build a minimal input that carries the given OutputReference; the resolved
/// output is filler — only the reference matters for the seed-consumption check.
fn input_for(out_ref: OutputReference) -> Input {
  Input {
    output_reference: out_ref,
    output: Output {
      address: Address {
        payment_credential: VerificationKey(test_admin_1_pkh),
        stake_credential: None,
      },
      value: zero,
      datum: NoDatum,
      reference_script: None,
    },
  }
}

/// Build a test tx with the given signers, inputs, and net mint Value.
fn test_tx(
  signers: List<VerificationKeyHash>,
  inputs: List<Input>,
  mint_value: Value,
) -> Transaction {
  Transaction {
    ..placeholder,
    extra_signatories: signers,
    inputs: inputs,
    mint: mint_value,
  }
}

fn both_admins() -> List<VerificationKeyHash> {
  [test_admin_1_pkh, test_admin_2_pkh]
}

// =========================================================================
// HAPPY PATHS
// =========================================================================

/// I1+I2+I3+I4: a fully-correct initial mint passes.
test happy_mint_passes() {
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, total_supply_base)
  let tx = test_tx(both_admins(), [input_for(test_seed_ref())], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

/// I5: permissionless burn of part of the supply.
test burn_partial_supply_passes() {
  // Negative quantity = burn. No admin signatures, no seed consumption.
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, -123_456_789)
  let tx = test_tx([], [], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

/// I5: permissionless burn of the entire supply.
test burn_full_supply_passes() {
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, -total_supply_base)
  let tx = test_tx([test_random_pkh], [], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

// =========================================================================
// FAILURE PATHS — INVARIANT I2 (exact supply cap)
// =========================================================================

/// I2: minting 1 less than the cap is rejected.
test mint_below_cap_fails() fail {
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, total_supply_base - 1)
  let tx = test_tx(both_admins(), [input_for(test_seed_ref())], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

/// I2: minting 1 more than the cap is rejected.
test mint_above_cap_fails() fail {
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, total_supply_base + 1)
  let tx = test_tx(both_admins(), [input_for(test_seed_ref())], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

// =========================================================================
// FAILURE PATHS — INVARIANT I3 (single asset name)
// =========================================================================

/// I3: minting under the wrong asset name (even at the exact cap qty) fails.
test mint_wrong_asset_name_fails() fail {
  let mint_value = from_asset(test_policy_id, wrong_asset_name, total_supply_base)
  let tx = test_tx(both_admins(), [input_for(test_seed_ref())], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

/// I3: minting cMATRA AND a second asset under the same policy fails.
/// Even though the cMATRA quantity is correct, the policy must only ever
/// touch one asset name.
test mint_cmatra_plus_extra_asset_fails() fail {
  let cmatra_value =
    from_asset(test_policy_id, cmatra_asset_name, total_supply_base)
  let extra_value = from_asset(test_policy_id, wrong_asset_name, 1)
  let mint_value = merge(cmatra_value, extra_value)
  let tx = test_tx(both_admins(), [input_for(test_seed_ref())], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

// =========================================================================
// FAILURE PATHS — INVARIANT I4 (dual-admin signatures)
// =========================================================================

/// I4: only admin_1 signs the mint — must fail.
test mint_missing_admin_2_sig_fails() fail {
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, total_supply_base)
  let tx =
    test_tx([test_admin_1_pkh], [input_for(test_seed_ref())], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

/// I4: only admin_2 signs the mint — must fail.
test mint_missing_admin_1_sig_fails() fail {
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, total_supply_base)
  let tx =
    test_tx([test_admin_2_pkh], [input_for(test_seed_ref())], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

/// I4: a random non-admin signer attempts the mint — must fail.
test mint_random_signer_fails() fail {
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, total_supply_base)
  let tx =
    test_tx([test_random_pkh], [input_for(test_seed_ref())], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

// =========================================================================
// FAILURE PATHS — INVARIANT I1 (one-shot via seed UTxO)
// =========================================================================

/// I1: seed UTxO is NOT among inputs — mint fails even with dual sigs and
/// the correct quantity. This is the structural one-shot guard.
test mint_without_seed_utxo_fails() fail {
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, total_supply_base)
  // Only a different (non-seed) input is present.
  let tx = test_tx(both_admins(), [input_for(other_ref())], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

/// I1: simulates the "mint again after seed already consumed" scenario.
/// In production this can't happen because the UTxO is gone — but the
/// validator's defense-in-depth: no seed in inputs (because it no longer
/// exists) → fail. Equivalent to mint_without_seed_utxo_fails by
/// construction; named for traceability to the explicit security goal.
test mint_after_seed_consumed_fails() fail {
  let mint_value =
    from_asset(test_policy_id, cmatra_asset_name, total_supply_base)
  // No inputs at all — seed is gone forever.
  let tx = test_tx(both_admins(), [], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}

// =========================================================================
// FAILURE PATHS — combined / belt-and-braces
// =========================================================================

/// Defense-in-depth: even a burn that touches a non-cMATRA asset name is
/// rejected. This keeps the policy invariant "this script only ever
/// touches cMATRA" total.
test burn_wrong_asset_name_fails() fail {
  let mint_value = from_asset(test_policy_id, wrong_asset_name, -1)
  let tx = test_tx([], [], mint_value)
  flux_mint_policy.mint(
    test_seed_ref(),
    test_admin_1_pkh,
    test_admin_2_pkh,
    Void,
    test_policy_id,
    tx,
  )
}
