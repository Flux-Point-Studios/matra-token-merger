//// Copyright (C) Midnight Foundation
//// Copyright (C) Flux Point Studios
//// SPDX-License-Identifier: Apache-2.0
////
//// Licensed under the Apache License, Version 2.0 (the "License"); you may
//// not use this file except in compliance with the License. A copy is in
//// LICENSE-APACHE at the root of this repository.
////
//// A modified copy of validators/cnight_generates_dust.ak from
//// github.com/midnightntwrk/midnight-reserve-contracts at commit
//// 6749c82a509442010bbd0252c00491c7e55ae1c5. Flux Point Studios changed it:
//// the datum binds a stake credential to a 32-byte Materios AccountId, the
//// auth NFT is named after that credential, the one-shot input check is
//// dropped, and the cMATRA policy id and a Materios chain id are anchored in
//// the script hash where the original anchors the cNIGHT policy id and the
//// candidate nonce.

use aiken/collection/dict
use aiken/collection/list
use aiken/crypto.{VerificationKeyHash}
use aiken/primitive/bytearray
use cardano/address.{Address, Credential, Script, VerificationKey}
use cardano/assets.{AssetName, PolicyId, Value}
use cardano/certificate.{RegisterCredential, UnregisterCredential}
use cardano/script_context.{
  Minting, Publishing, ScriptContext, Spending, Withdrawing,
}
use cardano/transaction.{
  DatumHash, InlineDatum, Input, Output, OutputReference, Transaction,
}
use config

/// Binds a Cardano stake credential to the Materios account its cMATRA
/// generates MOTRA for.
pub type RegistrationDatum {
  stake_credential: Credential,
  materios_account: ByteArray,
}

pub type RegistrationAction {
  Create
  Burn
}

/// The auth NFT's name: 0x00 then a key hash, or 0x01 then a script hash.
pub fn registration_name(credential: Credential) -> AssetName {
  when credential is {
    VerificationKey(hash) -> bytearray.push(hash, 0)
    Script(hash) -> bytearray.push(hash, 1)
  }
}

/// A key credential must sign; a script credential must be withdrawn from.
fn check_auth(
  credential: Credential,
  extra_signatories: List<VerificationKeyHash>,
  withdrawals: Pairs<Credential, Int>,
) -> Bool {
  when credential is {
    VerificationKey(key) -> list.has(extra_signatories, key)
    Script(_) ->
      list.any(withdrawals, fn(withdraw) { withdraw.1st == credential })
  }
}

/// The auth NFT name a registration datum calls for, once its account is 32
/// bytes and its credential authorizes the transaction. Callers must use the
/// name: the compiler drops an unused binding, and these checks with it.
fn authorized_name(
  data: Data,
  extra_signatories: List<VerificationKeyHash>,
  withdrawals: Pairs<Credential, Int>,
) -> AssetName {
  expect registration: RegistrationDatum = data
  expect bytearray.length(registration.materios_account) == 32
  expect
    check_auth(registration.stake_credential, extra_signatories, withdrawals)
  registration_name(registration.stake_credential)
}

/// The value is lovelace plus exactly one `policy`.`name` and nothing else.
fn is_singleton(value: Value, policy: PolicyId, name: AssetName) -> Bool {
  expect [_, ..nft] = value |> assets.to_dict |> dict.to_pairs
  as_data(nft) == as_data([Pair(policy, [Pair(name, 1)])])
}

/// Mints, holds and burns the auth NFTs that register stake credentials.
validator cmatra_generates_motra(materios_chain_id: ByteArray) {
  else(ctx: ScriptContext) {
    let ScriptContext { redeemer, info, transaction } = ctx

    let Transaction {
      inputs,
      outputs,
      mint,
      extra_signatories,
      withdrawals,
      ..
    } = transaction

    when info is {
      Minting(own_policy) -> {
        let own_credential = Script(own_policy)
        let own_tokens = mint |> assets.tokens(own_policy) |> dict.to_pairs
        and {
          // Anchor both constants in the script hash; no policy can equal them.
          own_policy != config.cmatra_policy,
          own_policy != materios_chain_id,
          if redeemer == as_data(Create) {
            expect Some(Output { value, datum, .. }) =
              list.find(
                outputs,
                fn(output) {
                  output.address.payment_credential == own_credential
                },
              )
            expect InlineDatum(data) = datum
            let name = authorized_name(data, extra_signatories, withdrawals)
            and {
              own_tokens == [Pair(name, 1)],
              is_singleton(value, own_policy, name),
            }
          } else {
            let spent =
              list.count(
                inputs,
                fn(input) {
                  input.output.address.payment_credential == own_credential
                },
              )
            and {
              list.all(own_tokens, fn(token) { token.2nd < 0 }),
              list.foldl(own_tokens, spent, fn(token, sum) { sum + token.2nd }) == 0,
            }
          },
        }
      }

      Withdrawing(own_credential) -> {
        expect Script(own_hash) = own_credential
        expect _ =
          list.foldl(
            inputs,
            outputs,
            fn(input, remaining) {
              if input.output.address.payment_credential == own_credential {
                expect [Output { address, value, datum, .. }, ..rest] =
                  remaining
                expect address.payment_credential == own_credential
                expect InlineDatum(data) = datum
                expect
                  is_singleton(
                    value,
                    own_hash,
                    authorized_name(data, extra_signatories, withdrawals),
                  )
                rest
              } else {
                remaining
              }
            },
          )
        True
      }

      Spending { datum, output } -> {
        expect Some(data) = datum
        expect RegistrationDatum { stake_credential, .. }: RegistrationDatum =
          data
        expect Some(Input { output: Output { address, .. }, .. }) =
          list.find(inputs, fn(input) { input.output_reference == output })
        expect Script(own_hash) = address.payment_credential
        and {
          check_auth(stake_credential, extra_signatories, withdrawals),
          or {
            assets.quantity_of(
              mint,
              own_hash,
              registration_name(stake_credential),
            ) < 0,
            and {
              list.any(
                withdrawals,
                fn(withdraw) { withdraw.1st == address.payment_credential },
              ),
              dict.is_empty(assets.tokens(mint, own_hash)),
            },
          },
        }
      }

      Publishing { certificate: RegisterCredential { .. }, .. } -> True

      _ -> fail
    }
  }
}

// =========================================================================
// Tests
// =========================================================================

const chain_id = "materios-mainnet"

const own_policy: PolicyId =
  #"5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed5eed"

const holder_key: VerificationKeyHash =
  #"a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"

const other_key: VerificationKeyHash =
  #"b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"

const holder_script: ByteArray =
  #"c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"

const account: ByteArray =
  #"d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4"

const new_account: ByteArray =
  #"e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5"

const holder = VerificationKey(holder_key)

const script_holder = Script(holder_script)

const own_address =
  Address { payment_credential: Script(own_policy), stake_credential: None }

const wallet_address =
  Address {
    payment_credential: VerificationKey(holder_key),
    stake_credential: None,
  }

fn run(redeemer: RegistrationAction, info, transaction: Transaction) -> Bool {
  cmatra_generates_motra.else(
    chain_id,
    ScriptContext { transaction, redeemer: as_data(redeemer), info },
  )
}

fn nft_value(policy: PolicyId, name: AssetName, quantity: Int) {
  assets.from_lovelace(2_000_000) |> assets.add(policy, name, quantity)
}

fn registration_output(credential: Credential, materios_account: ByteArray) {
  Output {
    address: own_address,
    value: nft_value(own_policy, registration_name(credential), 1),
    datum: InlineDatum(
      RegistrationDatum { stake_credential: credential, materios_account },
    ),
    reference_script: None,
  }
}

fn out_ref(index: Int) {
  OutputReference { transaction_id: #"0f0f", output_index: index }
}

fn registration_input(index: Int, credential: Credential) {
  Input {
    output_reference: out_ref(index),
    output: registration_output(credential, account),
  }
}

fn wallet_input() {
  Input {
    output_reference: out_ref(99),
    output: Output {
      address: wallet_address,
      value: assets.from_lovelace(10_000_000),
      datum: transaction.NoDatum,
      reference_script: None,
    },
  }
}

fn mint_one(credential: Credential) {
  assets.from_asset(own_policy, registration_name(credential), 1)
}

fn create_tx(output: Output, mint, signers, withdrawals) {
  Transaction {
    ..transaction.placeholder,
    inputs: [wallet_input()],
    outputs: [output],
    mint,
    extra_signatories: signers,
    withdrawals,
  }
}

fn key_create(output: Output, mint) {
  run(Create, Minting(own_policy), create_tx(output, mint, [holder_key], []))
}

fn burn_tx(inputs: List<Input>, mint, signers, withdrawals) {
  Transaction {
    ..transaction.placeholder,
    inputs,
    outputs: [],
    mint,
    extra_signatories: signers,
    withdrawals,
  }
}

fn spend(index: Int, credential: Credential, transaction: Transaction) -> Bool {
  run(
    Burn,
    Spending {
      output: out_ref(index),
      datum: Some(
        as_data(
          RegistrationDatum {
            stake_credential: credential,
            materios_account: account,
          },
        ),
      ),
    },
    transaction,
  )
}

fn own_withdrawal() -> Pairs<Credential, Int> {
  [Pair(Script(own_policy), 0)]
}

fn update_tx(output: Output, signers) {
  Transaction {
    ..transaction.placeholder,
    inputs: [registration_input(0, holder), wallet_input()],
    outputs: [output],
    mint: assets.zero,
    extra_signatories: signers,
    withdrawals: own_withdrawal(),
  }
}

// --- naming -------------------------------------------------------------

test name_tags_the_credential_kind() {
  and {
    registration_name(holder) == #"00a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1",
    registration_name(script_holder) == #"01c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3",
  }
}

// --- registration -------------------------------------------------------

test key_holder_registers() {
  key_create(registration_output(holder, account), mint_one(holder))
}

test script_holder_registers_with_a_zero_withdrawal() {
  run(
    Create,
    Minting(own_policy),
    create_tx(
      registration_output(script_holder, account),
      mint_one(script_holder),
      [],
      [Pair(script_holder, 0)],
    ),
  )
}

test registration_without_the_key_signature_fails() fail {
  run(
    Create,
    Minting(own_policy),
    create_tx(registration_output(holder, account), mint_one(holder), [], []),
  )
}

test registration_signed_by_another_key_fails() fail {
  run(
    Create,
    Minting(own_policy),
    create_tx(
      registration_output(holder, account),
      mint_one(holder),
      [other_key],
      [],
    ),
  )
}

test script_registration_without_the_withdrawal_fails() fail {
  run(
    Create,
    Minting(own_policy),
    create_tx(
      registration_output(script_holder, account),
      mint_one(script_holder),
      [holder_key],
      [Pair(holder, 0)],
    ),
  )
}

test account_of_31_bytes_fails() fail {
  key_create(
    registration_output(holder, bytearray.drop(account, 1)),
    mint_one(holder),
  )
}

test account_of_33_bytes_fails() fail {
  key_create(
    registration_output(holder, bytearray.push(account, 0)),
    mint_one(holder),
  )
}

test datum_that_is_not_a_registration_fails() fail {
  key_create(
    Output {
      ..registration_output(holder, account),
      datum: InlineDatum(as_data((holder, account))),
    },
    mint_one(holder),
  )
}

type PaddedRegistration {
  stake_credential: Credential,
  materios_account: ByteArray,
  padding: Int,
}

test datum_with_an_extra_field_fails() fail {
  key_create(
    Output {
      ..registration_output(holder, account),
      datum: InlineDatum(
        PaddedRegistration {
          stake_credential: holder,
          materios_account: account,
          padding: 0,
        },
      ),
    },
    mint_one(holder),
  )
}

test datum_by_hash_fails() fail {
  key_create(
    Output { ..registration_output(holder, account), datum: DatumHash(#"00") },
    mint_one(holder),
  )
}

test double_mint_of_one_name_fails() fail {
  key_create(
    Output {
      ..registration_output(holder, account),
      value: nft_value(own_policy, registration_name(holder), 2),
    },
    assets.from_asset(own_policy, registration_name(holder), 2),
  )
}

test two_registrations_in_one_tx_fail() fail {
  run(
    Create,
    Minting(own_policy),
    Transaction {
      ..create_tx(
        registration_output(holder, account),
        mint_one(holder) |> assets.merge(mint_one(script_holder)),
        [holder_key],
        [Pair(script_holder, 0)],
      ),
      outputs: [
        registration_output(holder, account),
        registration_output(script_holder, account),
      ],
    },
  )
}

test nft_named_after_another_credential_fails() fail {
  key_create(
    Output {
      ..registration_output(holder, account),
      value: nft_value(
        own_policy,
        registration_name(VerificationKey(other_key)),
        1,
      ),
    },
    assets.from_asset(
      own_policy,
      registration_name(VerificationKey(other_key)),
      1,
    ),
  )
}

/// A key holder's registration validated as if `policy` were this script.
fn create_as(policy: PolicyId) -> Bool {
  let name = registration_name(holder)
  run(
    Create,
    Minting(policy),
    create_tx(
      Output {
        ..registration_output(holder, account),
        address: Address {
          payment_credential: Script(policy),
          stake_credential: None,
        },
        value: nft_value(policy, name, 1),
      },
      assets.from_asset(policy, name, 1),
      [holder_key],
      [],
    ),
  )
}

test create_as_this_script_passes() {
  create_as(own_policy)
}

test minting_as_the_cmatra_policy_fails() fail {
  create_as(config.cmatra_policy)
}

test minting_as_the_chain_id_fails() fail {
  create_as(chain_id)
}

test registration_output_holding_cmatra_fails() fail {
  key_create(
    Output {
      ..registration_output(holder, account),
      value: nft_value(own_policy, registration_name(holder), 1)
        |> assets.add(config.cmatra_policy, "cMATRA", 1),
    },
    mint_one(holder),
  )
}

test nft_paid_to_a_wallet_fails() fail {
  key_create(
    Output { ..registration_output(holder, account), address: wallet_address },
    mint_one(holder),
  )
}

// --- deregistration -----------------------------------------------------

fn burn_one(credential: Credential) {
  assets.from_asset(own_policy, registration_name(credential), -1)
}

test owner_deregisters() {
  let tx =
    burn_tx(
      [registration_input(0, holder), wallet_input()],
      burn_one(holder),
      [holder_key],
      [],
    )
  and {
    spend(0, holder, tx),
    run(Burn, Minting(own_policy), tx),
  }
}

test script_owner_deregisters_with_a_zero_withdrawal() {
  let tx =
    burn_tx(
      [registration_input(0, script_holder)],
      burn_one(script_holder),
      [],
      [Pair(script_holder, 0)],
    )
  and {
    spend(0, script_holder, tx),
    run(Burn, Minting(own_policy), tx),
  }
}

test burn_by_a_non_owner_fails() fail {
  spend(
    0,
    holder,
    burn_tx(
      [registration_input(0, holder), wallet_input()],
      burn_one(holder),
      [other_key],
      [],
    ),
  )
}

test script_burn_without_the_withdrawal_fails() fail {
  spend(
    0,
    script_holder,
    burn_tx(
      [registration_input(0, script_holder)],
      burn_one(script_holder),
      [],
      [],
    ),
  )
}

test burning_fewer_nfts_than_registrations_spent_fails() fail {
  run(
    Burn,
    Minting(own_policy),
    burn_tx(
      [registration_input(0, holder), registration_input(1, holder)],
      burn_one(holder),
      [holder_key],
      [],
    ),
  )
}

test burn_that_also_mints_fails() fail {
  run(
    Burn,
    Minting(own_policy),
    burn_tx(
      [registration_input(0, holder)],
      assets.from_asset(own_policy, registration_name(holder), -2)
        |> assets.merge(mint_one(script_holder)),
      [holder_key],
      [],
    ),
  )
}

/// A key holder's deregistration validated as if `policy` were this script.
fn burn_as(policy: PolicyId) -> Bool {
  let name = registration_name(holder)
  run(
    Burn,
    Minting(policy),
    burn_tx(
      [
        Input {
          output_reference: out_ref(0),
          output: Output {
            ..registration_output(holder, account),
            address: Address {
              payment_credential: Script(policy),
              stake_credential: None,
            },
            value: nft_value(policy, name, 1),
          },
        },
      ],
      assets.from_asset(policy, name, -1),
      [holder_key],
      [],
    ),
  )
}

test burn_as_this_script_passes() {
  burn_as(own_policy)
}

test burning_as_the_cmatra_policy_fails() fail {
  burn_as(config.cmatra_policy)
}

test burning_as_the_chain_id_fails() fail {
  burn_as(chain_id)
}

test spend_with_a_foreign_withdrawal_fails() fail {
  spend(
    0,
    holder,
    burn_tx(
      [registration_input(0, holder), wallet_input()],
      assets.zero,
      [holder_key],
      [Pair(script_holder, 0)],
    ),
  )
}

test spend_that_neither_burns_nor_withdraws_fails() fail {
  spend(
    0,
    holder,
    burn_tx(
      [registration_input(0, holder), wallet_input()],
      assets.zero,
      [holder_key],
      [],
    ),
  )
}

// --- update -------------------------------------------------------------

test owner_moves_registration_to_a_new_account() {
  let tx = update_tx(registration_output(holder, new_account), [holder_key])
  and {
    spend(0, holder, tx),
    run(Burn, Withdrawing(Script(own_policy)), tx),
  }
}

test update_by_a_non_owner_fails() fail {
  spend(
    0,
    holder,
    update_tx(registration_output(holder, new_account), [other_key]),
  )
}

test update_to_another_credential_fails() fail {
  run(
    Burn,
    Withdrawing(Script(own_policy)),
    update_tx(
      Output {
        ..registration_output(holder, new_account),
        datum: InlineDatum(
          RegistrationDatum {
            stake_credential: VerificationKey(other_key),
            materios_account: new_account,
          },
        ),
      },
      [holder_key, other_key],
    ),
  )
}

test update_moving_the_nft_to_a_wallet_fails() fail {
  run(
    Burn,
    Withdrawing(Script(own_policy)),
    update_tx(
      Output {
        ..registration_output(holder, new_account),
        address: wallet_address,
      },
      [holder_key],
    ),
  )
}

test update_to_a_short_account_fails() fail {
  run(
    Burn,
    Withdrawing(Script(own_policy)),
    update_tx(registration_output(holder, #"d4"), [holder_key]),
  )
}

test update_that_mints_fails() fail {
  spend(
    0,
    holder,
    Transaction {
      ..update_tx(registration_output(holder, new_account), [holder_key]),
      mint: mint_one(script_holder),
    },
  )
}

// --- publishing ---------------------------------------------------------

test registering_the_script_stake_credential_passes() {
  run(
    Burn,
    Publishing {
      at: 0,
      certificate: RegisterCredential {
        credential: Script(own_policy),
        deposit: Never,
      },
    },
    transaction.placeholder,
  )
}

test unregistering_the_script_stake_credential_fails() fail {
  run(
    Burn,
    Publishing {
      at: 0,
      certificate: UnregisterCredential {
        credential: Script(own_policy),
        refund: Never,
      },
    },
    transaction.placeholder,
  )
}
