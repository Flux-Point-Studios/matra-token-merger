/// FLUX Claim Validator (Parameterized)
///
/// A signature-gated lockbox with admin reclaim.  Each UTxO at this script
/// address contains an inline datum encoding a claimant's payment key hash.
///
/// Spending paths:
///   1. **Claimant claim** — the claimant (identified by `extra_signatories`)
///      can spend their UTxO at any time.
///   2. **Admin reclaim** — after the deadline, the admin can sweep unclaimed
///      UTxOs (requires admin signature + validity range entirely after deadline).
///
/// Parameters (applied at compile time via `aiken blueprint apply`):
///   - admin_pkh:  VerificationKeyHash of the admin wallet
///   - deadline:   POSIX milliseconds after which admin may reclaim
///
/// Datum:  Constr 0 [ VerificationKeyHash ]   (28-byte payment key hash)
/// Redeemer: ignored (unit / Void)

use aiken/collection/list
use aiken/crypto.{VerificationKeyHash}
use aiken/interval.{
  Interval, IntervalBound, Finite, PositiveInfinity, is_entirely_after,
}
use cardano/transaction.{OutputReference, Transaction, placeholder}

/// The inline datum stored in every claim UTxO.
pub type ClaimDatum {
  /// Payment verification-key hash of the authorised claimant.
  claimant_pkh: VerificationKeyHash,
}

/// Parameterized spend validator: claimant claims or admin reclaims after deadline.
validator claim_validator(admin_pkh: VerificationKeyHash, deadline: Int) {
  spend(
    datum: Option<ClaimDatum>,
    _redeemer: Data,
    _own_ref: OutputReference,
    tx: Transaction,
  ) {
    // Datum is required — fail if missing.
    expect Some(d) = datum

    // Path 1: Claimant claims — must sign with their payment key.
    let claimant_claims = list.has(tx.extra_signatories, d.claimant_pkh)

    // Path 2: Admin reclaims — must sign AND tx validity range is entirely
    // after the deadline (i.e. invalid_before > deadline).
    let admin_reclaims =
      list.has(tx.extra_signatories, admin_pkh) && is_entirely_after(
        tx.validity_range,
        deadline,
      )

    claimant_claims || admin_reclaims
  }

  else(_) {
    fail
  }
}

// =========================================================================
// Tests
// =========================================================================

fn test_tx(
  signers: List<VerificationKeyHash>,
  lower_bound: Int,
) -> Transaction {
  Transaction {
    ..placeholder,
    extra_signatories: signers,
    validity_range: Interval {
      lower_bound: IntervalBound {
        bound_type: Finite(lower_bound),
        is_inclusive: True,
      },
      upper_bound: IntervalBound {
        bound_type: PositiveInfinity,
        is_inclusive: True,
      },
    },
  }
}

const test_admin_pkh: VerificationKeyHash =
  #"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaabb"

const test_claimant_pkh: VerificationKeyHash =
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

test claimant_can_spend() {
  let datum = Some(ClaimDatum { claimant_pkh: test_claimant_pkh })
  let tx = test_tx([test_claimant_pkh], 0)
  claim_validator.spend(test_admin_pkh, test_deadline, datum, Void, test_own_ref(), tx)
}

test admin_reclaim_before_deadline_fails() fail {
  let datum = Some(ClaimDatum { claimant_pkh: test_claimant_pkh })
  // lower_bound = deadline - 1 → NOT entirely after deadline
  let tx = test_tx([test_admin_pkh], test_deadline - 1)
  claim_validator.spend(test_admin_pkh, test_deadline, datum, Void, test_own_ref(), tx)
}

test admin_reclaim_at_deadline_fails() fail {
  let datum = Some(ClaimDatum { claimant_pkh: test_claimant_pkh })
  // lower_bound = deadline exactly → NOT entirely after (is_entirely_after
  // with inclusive bound checks low > point, which is false when equal)
  let tx = test_tx([test_admin_pkh], test_deadline)
  claim_validator.spend(test_admin_pkh, test_deadline, datum, Void, test_own_ref(), tx)
}

test admin_reclaim_after_deadline_passes() {
  let datum = Some(ClaimDatum { claimant_pkh: test_claimant_pkh })
  // lower_bound = deadline + 1 → entirely after deadline
  let tx = test_tx([test_admin_pkh], test_deadline + 1)
  claim_validator.spend(test_admin_pkh, test_deadline, datum, Void, test_own_ref(), tx)
}

test admin_reclaim_wrong_key_fails() fail {
  let datum = Some(ClaimDatum { claimant_pkh: test_claimant_pkh })
  // random key signs, even though after deadline
  let tx = test_tx([test_random_pkh], test_deadline + 1)
  claim_validator.spend(test_admin_pkh, test_deadline, datum, Void, test_own_ref(), tx)
}

test random_third_party_fails() fail {
  let datum = Some(ClaimDatum { claimant_pkh: test_claimant_pkh })
  // random key, before deadline
  let tx = test_tx([test_random_pkh], 0)
  claim_validator.spend(test_admin_pkh, test_deadline, datum, Void, test_own_ref(), tx)
}
