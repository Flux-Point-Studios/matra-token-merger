/// Surrender Pool Validator (Dual-Signer, Parameterized)
///
/// Location: onchain/claim_validator/validators/claim_validator.ak
///
/// A surrender-and-redeem pool validator for the cMATRA merger. Pool UTxOs at
/// this script address hold cMATRA tokens. The admin processes user surrender
/// requests during a 6-month window, then reclaims any remaining pool balance
/// after the deadline expires.
///
/// DUAL-SIGNER MODEL: Two separate admin keys must both sign every pool spend.
/// This eliminates the single-point-of-failure risk — compromising one server
/// does not grant the ability to drain the pool.  The keys are intended to
/// reside on separate infrastructure (Server A builds + signs, Server B
/// co-signs via authenticated API call).
///
/// Parameters (applied at compile time via `aiken blueprint apply`):
///   - admin_pkh_1:  VerificationKeyHash of the primary admin key (Server A)
///   - admin_pkh_2:  VerificationKeyHash of the co-signer key (Server B)
///   - deadline:     POSIX milliseconds after which admins may withdraw remaining pool
///
/// Datum:  Void (unit) — no per-UTxO data needed
///
/// Redeemer:
///   ProcessSurrender — Both admins sign + tx validity entirely before deadline
///   AdminWithdraw    — Both admins sign + tx validity entirely after deadline
///
/// Used by: off-chain Python tooling in tools/ and scripts/ for building
/// surrender transactions and post-deadline sweep transactions.

use aiken/collection/list
use aiken/crypto.{VerificationKeyHash}
use aiken/interval.{
  Interval, IntervalBound, Finite, NegativeInfinity, PositiveInfinity,
  is_entirely_after, is_entirely_before,
}
use cardano/transaction.{OutputReference, Transaction, placeholder}

/// Redeemer type: which spending path to take.
pub type SurrenderAction {
  /// Both admins process a user's surrender request during the open window.
  ProcessSurrender
  /// Both admins withdraw remaining pool balance after the deadline.
  AdminWithdraw
}

/// Parameterized spend validator: dual-signer admin processes surrenders or
/// withdraws after deadline.
validator surrender_pool(
  admin_pkh_1: VerificationKeyHash,
  admin_pkh_2: VerificationKeyHash,
  deadline: Int,
) {
  spend(
    datum: Option<Void>,
    redeemer: SurrenderAction,
    _own_ref: OutputReference,
    tx: Transaction,
  ) {
    // Datum is required — fail if missing (even though it is Void).
    expect Some(_) = datum

    // Both admin keys must sign regardless of path.
    let admin_1_signed = list.has(tx.extra_signatories, admin_pkh_1)
    let admin_2_signed = list.has(tx.extra_signatories, admin_pkh_2)
    let both_signed = admin_1_signed && admin_2_signed

    when redeemer is {
      // Path 1: ProcessSurrender — both admins sign and window is still open.
      ProcessSurrender ->
        both_signed && is_entirely_before(tx.validity_range, deadline)

      // Path 2: AdminWithdraw — both admins sign and window has closed.
      AdminWithdraw ->
        both_signed && is_entirely_after(tx.validity_range, deadline)
    }
  }

  else(_) {
    fail
  }
}

// =========================================================================
// Tests
// =========================================================================

/// Build a test transaction with given signers and a validity range
/// from lower_bound (inclusive) to upper_bound (exclusive-ish).
fn test_tx_bounded(
  signers: List<VerificationKeyHash>,
  lower_bound: Int,
  upper_bound_type: IntervalBound<Int>,
) -> Transaction {
  Transaction {
    ..placeholder,
    extra_signatories: signers,
    validity_range: Interval {
      lower_bound: IntervalBound {
        bound_type: Finite(lower_bound),
        is_inclusive: True,
      },
      upper_bound: upper_bound_type,
    },
  }
}

/// Build a test transaction with validity range [lower, +inf).
fn test_tx_open_end(
  signers: List<VerificationKeyHash>,
  lower_bound: Int,
) -> Transaction {
  test_tx_bounded(
    signers,
    lower_bound,
    IntervalBound { bound_type: PositiveInfinity, is_inclusive: True },
  )
}

/// Build a test transaction with validity range (-inf, upper].
fn test_tx_open_start(
  signers: List<VerificationKeyHash>,
  upper_bound: Int,
) -> Transaction {
  Transaction {
    ..placeholder,
    extra_signatories: signers,
    validity_range: Interval {
      lower_bound: IntervalBound {
        bound_type: NegativeInfinity,
        is_inclusive: True,
      },
      upper_bound: IntervalBound {
        bound_type: Finite(upper_bound),
        is_inclusive: True,
      },
    },
  }
}

const test_admin_1_pkh: VerificationKeyHash =
  #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaabb"

const test_admin_2_pkh: VerificationKeyHash =
  #"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbcc"

const test_random_pkh: VerificationKeyHash =
  #"ccccccccccccccccccccccccccccccccccccccccccccccccccccccdd"

// Deadline: 1_700_000_000_000 (POSIX ms)
const test_deadline: Int = 1_700_000_000_000

fn test_own_ref() -> OutputReference {
  OutputReference {
    transaction_id: #"0000000000000000000000000000000000000000000000000000000000000000",
    output_index: 0,
  }
}

/// Helper: both admins sign
fn both_admins() -> List<VerificationKeyHash> {
  [test_admin_1_pkh, test_admin_2_pkh]
}

// ----- ProcessSurrender tests -----

test both_admins_can_process_surrender() {
  // Both admins sign, upper bound is before deadline -> passes
  let datum = Some(Void)
  let tx = test_tx_open_start(both_admins(), test_deadline - 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    ProcessSurrender,
    test_own_ref(),
    tx,
  )
}

test process_surrender_after_deadline_fails() fail {
  // Both admins sign, but validity range is after deadline -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_end(both_admins(), test_deadline + 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    ProcessSurrender,
    test_own_ref(),
    tx,
  )
}

// ----- AdminWithdraw tests -----

test admin_withdraw_before_deadline_fails() fail {
  // Both admins sign, but validity range is before deadline -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_start(both_admins(), test_deadline - 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    AdminWithdraw,
    test_own_ref(),
    tx,
  )
}

test admin_withdraw_after_deadline_passes() {
  // Both admins sign, lower bound after deadline -> passes
  let datum = Some(Void)
  let tx = test_tx_open_end(both_admins(), test_deadline + 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    AdminWithdraw,
    test_own_ref(),
    tx,
  )
}

// ----- Single-signer tests (must fail — need BOTH) -----

test only_admin_1_process_fails() fail {
  // Only admin_1 signs — admin_2 missing -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_start([test_admin_1_pkh], test_deadline - 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    ProcessSurrender,
    test_own_ref(),
    tx,
  )
}

test only_admin_2_process_fails() fail {
  // Only admin_2 signs — admin_1 missing -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_start([test_admin_2_pkh], test_deadline - 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    ProcessSurrender,
    test_own_ref(),
    tx,
  )
}

test only_admin_1_withdraw_fails() fail {
  // Only admin_1 signs for withdraw -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_end([test_admin_1_pkh], test_deadline + 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    AdminWithdraw,
    test_own_ref(),
    tx,
  )
}

// ----- Non-admin tests -----

test random_signer_process_fails() fail {
  // Non-admin tries ProcessSurrender during window -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_start([test_random_pkh], test_deadline - 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    ProcessSurrender,
    test_own_ref(),
    tx,
  )
}

test random_signer_withdraw_fails() fail {
  // Non-admin tries AdminWithdraw after deadline -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_end([test_random_pkh], test_deadline + 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    AdminWithdraw,
    test_own_ref(),
    tx,
  )
}

// ----- Datum robustness test -----

test no_datum_fails() fail {
  // Missing datum (None) should fail even with valid both admins + timing
  let datum: Option<Void> = None
  let tx = test_tx_open_start(both_admins(), test_deadline - 1)
  surrender_pool.spend(
    test_admin_1_pkh,
    test_admin_2_pkh,
    test_deadline,
    datum,
    ProcessSurrender,
    test_own_ref(),
    tx,
  )
}
