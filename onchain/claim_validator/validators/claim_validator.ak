/// FLUX Claim Validator
///
/// A minimal signature-gated lockbox.  Each UTxO at this script address
/// contains an inline datum encoding a claimant's payment key hash.
/// Only that claimant (identified by `extra_signatories`) may spend the UTxO.
///
/// Datum:  Constr 0 [ VerificationKeyHash ]   (28-byte payment key hash)
/// Redeemer: ignored (unit / Void)

use aiken/collection/list
use aiken/crypto.{VerificationKeyHash}
use cardano/transaction.{OutputReference, Transaction}

/// The inline datum stored in every claim UTxO.
pub type ClaimDatum {
  /// Payment verification-key hash of the authorised claimant.
  claimant_pkh: VerificationKeyHash,
}

/// Spend validator: only the owner of `claimant_pkh` may consume this UTxO.
validator claim_validator {
  spend(
    datum: Option<ClaimDatum>,
    _redeemer: Data,
    _own_ref: OutputReference,
    tx: Transaction,
  ) {
    // Datum is required — fail if missing.
    expect Some(d) = datum

    // Core rule: the claimant MUST appear in extra_signatories.
    list.has(tx.extra_signatories, d.claimant_pkh)
  }

  else(_) {
    fail
  }
}
