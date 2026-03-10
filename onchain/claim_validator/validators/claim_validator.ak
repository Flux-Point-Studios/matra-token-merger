/// Surrender Pool Validator (Parameterized)
///
/// Location: onchain/claim_validator/validators/claim_validator.ak
///
/// A surrender-and-redeem pool validator for the cMATRA merger. Pool UTxOs at
/// this script address hold cMATRA tokens. The admin processes user surrender
/// requests during a 6-month window, then reclaims any remaining pool balance
/// after the deadline expires.
///
/// This file replaces the previous per-claimant claim validator. Instead of
/// per-user datums, pool UTxOs use Void (unit) datums — the pool is fungible,
/// and the admin orchestrates all spending.
///
/// Parameters (applied at compile time via `aiken blueprint apply`):
///   - admin_pkh:  VerificationKeyHash of the admin wallet
///   - deadline:   POSIX milliseconds after which admin may withdraw remaining pool
///
/// Datum:  Void (unit) — no per-UTxO data needed
///
/// Redeemer:
///   ProcessSurrender — Admin fulfills a user surrender during the window
///   AdminWithdraw    — Admin reclaims remaining pool after deadline
///
/// Spending paths:
///   1. **ProcessSurrender** — admin signs + tx validity entirely before deadline
///   2. **AdminWithdraw**    — admin signs + tx validity entirely after deadline
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
  /// Admin processes a user's surrender request during the open window.
  ProcessSurrender
  /// Admin withdraws remaining pool balance after the deadline.
  AdminWithdraw
}

/// Parameterized spend validator: admin processes surrenders or withdraws after deadline.
validator surrender_pool(admin_pkh: VerificationKeyHash, deadline: Int) {
  spend(
    datum: Option<Void>,
    redeemer: SurrenderAction,
    _own_ref: OutputReference,
    tx: Transaction,
  ) {
    // Datum is required — fail if missing (even though it is Void).
    expect Some(_) = datum

    // Admin must always sign regardless of path.
    let admin_signed = list.has(tx.extra_signatories, admin_pkh)

    when redeemer is {
      // Path 1: ProcessSurrender — admin signs and window is still open.
      // The tx validity range must be entirely before the deadline.
      ProcessSurrender ->
        admin_signed && is_entirely_before(tx.validity_range, deadline)

      // Path 2: AdminWithdraw — admin signs and window has closed.
      // The tx validity range must be entirely after the deadline.
      AdminWithdraw ->
        admin_signed && is_entirely_after(tx.validity_range, deadline)
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
/// For "entirely before deadline" tests we need a finite upper bound.
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
/// This is useful for "entirely after" checks.
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
/// This is useful for "entirely before" checks.
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

const test_admin_pkh: VerificationKeyHash =
  #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaabb"

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

// ----- ProcessSurrender tests -----

test admin_can_process_surrender() {
  // Admin signs, upper bound is before deadline -> passes
  let datum = Some(Void)
  let tx = test_tx_open_start([test_admin_pkh], test_deadline - 1)
  surrender_pool.spend(
    test_admin_pkh,
    test_deadline,
    datum,
    ProcessSurrender,
    test_own_ref(),
    tx,
  )
}

test process_surrender_after_deadline_fails() fail {
  // Admin signs, but validity range is after deadline -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_end([test_admin_pkh], test_deadline + 1)
  surrender_pool.spend(
    test_admin_pkh,
    test_deadline,
    datum,
    ProcessSurrender,
    test_own_ref(),
    tx,
  )
}

// ----- AdminWithdraw tests -----

test admin_withdraw_before_deadline_fails() fail {
  // Admin signs, but validity range is before deadline -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_start([test_admin_pkh], test_deadline - 1)
  surrender_pool.spend(
    test_admin_pkh,
    test_deadline,
    datum,
    AdminWithdraw,
    test_own_ref(),
    tx,
  )
}

test admin_withdraw_after_deadline_passes() {
  // Admin signs, lower bound after deadline -> passes
  let datum = Some(Void)
  let tx = test_tx_open_end([test_admin_pkh], test_deadline + 1)
  surrender_pool.spend(
    test_admin_pkh,
    test_deadline,
    datum,
    AdminWithdraw,
    test_own_ref(),
    tx,
  )
}

// ----- Authorization tests -----

test random_signer_process_fails() fail {
  // Non-admin tries ProcessSurrender during window -> must fail
  let datum = Some(Void)
  let tx = test_tx_open_start([test_random_pkh], test_deadline - 1)
  surrender_pool.spend(
    test_admin_pkh,
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
    test_admin_pkh,
    test_deadline,
    datum,
    AdminWithdraw,
    test_own_ref(),
    tx,
  )
}

// ----- Datum robustness test -----

test no_datum_fails() fail {
  // Missing datum (None) should fail even with valid admin + timing
  let datum: Option<Void> = None
  let tx = test_tx_open_start([test_admin_pkh], test_deadline - 1)
  surrender_pool.spend(
    test_admin_pkh,
    test_deadline,
    datum,
    ProcessSurrender,
    test_own_ref(),
    tx,
  )
}
