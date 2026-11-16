"""The unsigned payout transaction: every UTxO of the 2-of-2 native-script
float in, one output per paid statement line, and the change back to the
float, with the statement's label-8746 record as its metadata.

The change output is built here. A builder's "add change if needed" folds a
leftover too small for an output into the fee, so a float short of ADA is
refused instead. The fee is priced for the transaction once both float keys
have added their witnesses. Nothing here signs or submits.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from types import SimpleNamespace

from pycardano import (
    Address,
    AlonzoMetadata,
    AuxiliaryData,
    Metadata,
    MultiAsset,
    Network,
    ScriptAll,
    ScriptPubkey,
    Transaction,
    TransactionBody,
    TransactionInput,
    TransactionOutput,
    TransactionWitnessSet,
    UTxO,
    Value,
    VerificationKeyHash,
    VerificationKeyWitness,
)
from pycardano.key import VerificationKey
from pycardano.utils import min_lovelace_post_alonzo

from tools.attestor_payout.merkle import merkle_root
from tools.attestor_payout.statement import PeriodConfig, anchor_metadata, statement_leaves
from tools.credential_denylist import DenyList

ANCHOR_LABEL = 8746


class PayoutTxError(ValueError):
    """The payout cannot be built as asked."""


@dataclass(frozen=True)
class FeeParams:
    fee_per_byte: int
    fee_fixed: int
    coins_per_utxo_byte: int
    max_tx_size: int


def fee_params(cli_protocol_params: dict) -> FeeParams:
    """From Koios ``/cli_protocol_params``."""
    keys = ("txFeePerByte", "txFeeFixed", "utxoCostPerByte", "maxTxSize")
    for key in keys:
        if type(cli_protocol_params.get(key)) is not int or cli_protocol_params[key] <= 0:
            raise PayoutTxError(f"protocol parameter {key} is missing or not a positive integer")
    return FeeParams(*(cli_protocol_params[key] for key in keys))


def float_script(keys: tuple[bytes, bytes]) -> ScriptAll:
    return ScriptAll([ScriptPubkey(VerificationKeyHash(key)) for key in keys])


def float_address(keys: tuple[bytes, bytes], network: Network) -> Address:
    return Address(payment_part=float_script(keys).hash(), network=network)


def float_utxos(koios_rows: list[dict], address: Address) -> list[UTxO]:
    """The float's UTxOs from Koios ``/address_utxos`` (extended) rows."""
    if not koios_rows:
        raise PayoutTxError(f"the float {address} holds no UTxO")
    utxos = []
    for row in koios_rows:
        if row.get("address") != address.encode():
            raise PayoutTxError(f"Koios answered a UTxO of address {row.get('address')}, not the float's")
        if row.get("is_spent") is not False:
            raise PayoutTxError(f"UTxO {row['tx_hash']}#{row['tx_index']} is spent")
        assets: dict[bytes, dict[bytes, int]] = {}
        for asset in row["asset_list"]:
            assets.setdefault(bytes.fromhex(asset["policy_id"]), {})[bytes.fromhex(asset["asset_name"])] = int(asset["quantity"])
        utxos.append(UTxO(
            TransactionInput.from_primitive([row["tx_hash"], row["tx_index"]]),
            TransactionOutput(address, Value(int(row["value"]), MultiAsset.from_primitive(assets))),
        ))
    return sorted(utxos, key=lambda u: (u.input.transaction_id.payload, u.input.index))


def _min_ada(output: TransactionOutput, params: FeeParams) -> int:
    context = SimpleNamespace(protocol_param=SimpleNamespace(coins_per_utxo_byte=params.coins_per_utxo_byte))
    return min_lovelace_post_alonzo(copy.deepcopy(output), context)


def _funded(address: Address, assets: dict[bytes, dict[bytes, int]], params: FeeParams) -> TransactionOutput:
    output = TransactionOutput(address, Value(0, MultiAsset.from_primitive(assets)))
    while (needed := _min_ada(output, params)) > output.amount.coin:
        output.amount.coin = needed
    return output


def _check_statement(statement: dict, config: PeriodConfig, denylist: DenyList) -> list[tuple[Address, int]]:
    """Each paid line's address and amount, once the statement is the config's
    and its own lines, total and root agree."""
    expected = {"network": config.network, "chain": config.chain, "period": config.period,
                "rates": config.rates, "cmatra": config.cmatra}
    for name, value in expected.items():
        if statement.get(name) != value:
            raise PayoutTxError(f"the statement's {name} is not the period config's")
    amounts = [line["amount"] for line in statement["lines"]]
    if any(type(a) is not int or not 0 <= a <= config.cap_per_attestor for a in amounts):
        raise PayoutTxError(f"a line is negative or over the per-attestor cap {config.cap_per_attestor}")
    if statement["total"] != sum(amounts):
        raise PayoutTxError(f"the statement's total {statement['total']} is not its lines' {sum(amounts)}")
    if statement["total"] > config.cap_total:
        raise PayoutTxError(f"the statement's total exceeds the period cap {config.cap_total}")
    leaves = statement_leaves(statement)
    if statement["merkle"] != {"root": merkle_root(leaves).hex(), "leaves": len(leaves)}:
        raise PayoutTxError("the statement's Merkle root is not its lines'")
    paid = []
    for line in statement["lines"]:
        if line["amount"] == 0:
            continue
        address = Address.from_primitive(line["address"])
        if address.network != config.cardano_network:
            raise PayoutTxError(f"{line['address']} is not on {config.network}")
        denied = denylist.denied(bytes(address))
        if denied:
            raise PayoutTxError(f"{line['address']} names a denied credential: {denied}")
        paid.append((address, line["amount"]))
    if not paid:
        raise PayoutTxError("the statement pays no line")
    return paid


def build_payout_tx(
    statement: dict, config: PeriodConfig, utxos: list[UTxO], params: FeeParams, denylist: DenyList
) -> Transaction:
    paid = _check_statement(statement, config, denylist)
    policy, asset = bytes.fromhex(config.cmatra["policy"]), bytes.fromhex(config.cmatra["asset"])
    held: dict[bytes, dict[bytes, int]] = {}
    for utxo in utxos:
        for pid, names in (utxo.output.amount.multi_asset or {}).items():
            for name, quantity in names.items():
                bucket = held.setdefault(pid.payload, {})
                bucket[name.payload] = bucket.get(name.payload, 0) + quantity
    in_coin = sum(u.output.amount.coin for u in utxos)
    total = statement["total"]
    if held.get(policy, {}).get(asset, 0) < total:
        raise PayoutTxError(f"the float holds {held.get(policy, {}).get(asset, 0)} cMATRA base units, "
                            f"the statement pays {total}")
    held[policy][asset] -= total
    change_assets = {pid: kept for pid, names in held.items()
                     if (kept := {name: q for name, q in names.items() if q})}

    payees = [_funded(address, {policy: {asset: amount}}, params) for address, amount in paid]
    pay_coin = sum(o.amount.coin for o in payees)
    float_addr = float_address(config.float_keys, config.cardano_network)
    script = float_script(config.float_keys)
    auxiliary = AuxiliaryData(AlonzoMetadata(metadata=Metadata({ANCHOR_LABEL: anchor_metadata(statement)})))
    inputs = [u.input for u in utxos]
    priced_witnesses = TransactionWitnessSet(
        vkey_witnesses=[VerificationKeyWitness(VerificationKey(bytes([n]) * 32), bytes(64)) for n in (1, 2)],
        native_scripts=[script],
    )

    fee = 0
    while True:
        change = TransactionOutput(float_addr, Value(in_coin - pay_coin - fee, MultiAsset.from_primitive(change_assets)))
        if change.amount.coin < 0:
            raise PayoutTxError(f"the float's {in_coin} lovelace cannot fund the outputs and the fee in ADA")
        body = TransactionBody(inputs=inputs, outputs=[*payees, change], fee=fee,
                               auxiliary_data_hash=auxiliary.hash())
        size = len(Transaction(body, priced_witnesses, auxiliary_data=auxiliary).to_cbor())
        needed = params.fee_per_byte * size + params.fee_fixed
        if needed <= fee:
            break
        fee = needed
    if change.amount.coin < _min_ada(change, params):
        raise PayoutTxError(f"the float's change of {change.amount.coin} lovelace is under its minimum ADA")
    if size > params.max_tx_size:
        raise PayoutTxError(f"the signed transaction would be {size} bytes, over the size limit {params.max_tx_size}")
    return Transaction(body, TransactionWitnessSet(native_scripts=[script]), auxiliary_data=auxiliary)
