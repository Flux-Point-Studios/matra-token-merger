/// Quarantine Validator (Always-Fails Lockbox)
///
/// Location: onchain/quarantine_validator/validators/quarantine_validator.ak
///
/// A Plutus V3 spend validator with no spending path. Any transaction
/// that attempts to spend a UTxO at this script address is rejected by
/// the script, regardless of signers, redeemer, datum, validity range,
/// or any other transaction property.
///
/// Surrendered legacy assets (AGENT, SHARDS, FLUX_PASS, SE_BRAWLERS,
/// BRAWL_PASS_ETD, T1_ADAM_PASS, T2_ADAM_PASS) move to this address
/// when users invoke the surrender pool. Because the validator has no
/// spending path, those assets are cryptographically locked forever.
/// No admin key — not Server A, not Server B, not the deployer — can
/// recover them. The lock is enforced by Cardano consensus, not by
/// any operator's policy.
///
/// This is intentionally stronger than the alternative "admin holds the
/// quarantine key and promises not to spend": there is no key.
///
/// The applied script address is fully derivable from the compiled
/// blueprint hash + a network identifier. Anyone can independently
/// build this validator (`aiken build` at the audited Aiken version),
/// re-derive the script hash, and verify that the quarantine address
/// used by `surrender_api.py` matches.
///
/// Parameters: NONE. This validator is intentionally unparameterized
/// so that there is exactly one quarantine address on each network and
/// no operator-chosen value can vary it.
///
/// Datum:    Any (Option<Data>) — irrelevant, ignored.
/// Redeemer: Any (Data) — irrelevant, ignored.

use cardano/transaction.{OutputReference, Transaction, placeholder}

/// Unparameterized spend validator that rejects every spend attempt.
validator quarantine {
  spend(
    _datum: Option<Data>,
    _redeemer: Data,
    _own_ref: OutputReference,
    _tx: Transaction,
  ) {
    False
  }

  else(_) {
    fail
  }
}

// =========================================================================
// Tests
// =========================================================================
//
// The validator's body is `False` — it rejects regardless of inputs. These
// tests exist to lock that behavior under CI so a future refactor can't
// silently introduce a spending path.

test rejects_with_none_datum_empty_redeemer() {
  let datum: Option<Data> = None
  let redeemer: Data = #""
  let own_ref =
    OutputReference {
      transaction_id: #"0000000000000000000000000000000000000000000000000000000000000000",
      output_index: 0,
    }
  let tx = placeholder
  !quarantine.spend(datum, redeemer, own_ref, tx)
}

test rejects_with_some_datum() {
  let inner: Data = #""
  let datum: Option<Data> = Some(inner)
  let redeemer: Data = #"deadbeef"
  let own_ref =
    OutputReference {
      transaction_id: #"1111111111111111111111111111111111111111111111111111111111111111",
      output_index: 7,
    }
  let tx = placeholder
  !quarantine.spend(datum, redeemer, own_ref, tx)
}

test rejects_with_nonzero_output_index() {
  let datum: Option<Data> = None
  let redeemer: Data = #"0102030405"
  let own_ref =
    OutputReference {
      transaction_id: #"abcdef00112233445566778899aabbccddeeff00112233445566778899aabbcc",
      output_index: 99,
    }
  let tx = placeholder
  !quarantine.spend(datum, redeemer, own_ref, tx)
}
