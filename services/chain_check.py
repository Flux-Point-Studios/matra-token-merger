"""What a signer confirms with the chain itself before it records an approval.

services.cosign_policy judges a transaction by the bytes it is handed. What
those bytes cannot show comes from the signer's own view of the chain, a
Blockfrost project queried from the signer's own host:

  * no surrendered unit has more supply on chain than it had at the pin. Two
    collection policies can still mint, and editions of one name are
    indistinguishable, so a signer cannot tell a pinned edition from a later
    one; while a later edition exists the unit is not redeemed at all;
  * no surrendered NFT unit has been minted or burned since the pin's supply
    slot, by its own mint history. That is a separate query, made after the
    input lookups, so a supply answer that lags a mint, or a mint later
    offset by a burn, does not pass;
  * every input and collateral input is an unspent output on chain, or an
    output of a surrender this signer has recorded (a chained surrender
    spends outputs still in the mempool). A producing body proves what an
    output would hold, not that it exists; a transaction spending an output
    that does not exist can never land, and must not use up the limits the
    ledger holds approvals to;
  * of each unit surrendered, the claimant's inputs held what is
    surrendered at its payment key COOLING_OFF_SLOTS before the check. An
    input made inside that window is followed back through the transaction
    that made it, through those of its inputs that carry the unit and are the
    claimant's own (any of its addresses). Whatever of the unit entered such
    a transaction from another key, or was minted in it, arrived inside the
    window, and the inputs must hold what is surrendered on top of all of it:
    units of one kind cannot be told apart, so what arrived counts in full
    wherever it went afterwards, once per transaction, and a burn does not
    offset it. A walk through more than MAX_PROVENANCE_TXS transactions
    inside the window refuses the surrender. Change of a surrender this
    signer recorded is followed back through that surrender's own inputs, as
    the signer recorded them, unless the chain places the surrender before
    the window: a surrender checks only the units it quarantines, so its
    change may carry others that arrived inside the window. Stolen units are
    surrendered within hours of being taken; a holder waits at most the
    window, and only for what arrived inside it, so units sent to a holder
    hold back nothing it held before.

A view that cannot answer raises :class:`ChainUnavailable`, and nothing is
signed. That includes an answer of the wrong shape: a page of a history that
is not a list, an entry that does not name a transaction hash, a slot that is
not a whole number.

The view itself is trusted, as the signer's own provider. A history page that
is an empty or short list ends the history, and a view that ends it early, or
leaves an entry out, cannot be told from a complete one.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterator

import requests
from pycardano import Address
from pycardano.exception import DecodingException, DeserializeException

from services.cosign_policy import Approval, CosignConfig, CosignRejected
from tools.api_clients import BlockfrostUnavailable

COOLING_OFF_SLOTS = 72 * 3600
MAX_PROVENANCE_TXS = 32

Recorded = Callable[[bytes], tuple[tuple[bytes, int], ...] | None]


class ChainUnavailable(RuntimeError):
    """The chain view gave no usable answer."""


def _ask(call: Callable[[str], Any], key: str) -> Any:
    """``call(key)``, or None when the chain view has no such thing (404)."""
    try:
        return call(key)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise ChainUnavailable(f"{key}: {exc}") from exc
    except (requests.RequestException, BlockfrostUnavailable) as exc:
        raise ChainUnavailable(f"{key}: {exc}") from exc


def _unspent_outputs(answer: dict) -> set[int]:
    """Indexes of the outputs a Blockfrost /txs/{hash}/utxos answer lists as
    created and not yet spent. A valid transaction's collateral return is
    listed too, although the ledger never created it."""
    try:
        return {
            out["output_index"] for out in answer["outputs"]
            if out["collateral"] is False and out["consumed_by_tx"] is None
        }
    except (KeyError, TypeError) as exc:
        raise ChainUnavailable(f"unexpected answer from the chain view: {exc!r}") from exc


_TX_HASH = re.compile(r"[0-9a-f]{64}")


def _history_tx(unit: str, entry: Any) -> str:
    """The transaction an /assets/{unit}/history entry names. It goes into a
    request path, so it must be a transaction hash and nothing else."""
    tx_hash = entry.get("tx_hash") if isinstance(entry, dict) else None
    if not (isinstance(tx_hash, str) and _TX_HASH.fullmatch(tx_hash)):
        raise ChainUnavailable(f"{unit}: unexpected history entry from the chain view: {entry!r:.200}")
    return tx_hash


def _slot(tx_hash: str, answer: Any) -> int:
    """The slot a /txs/{hash} answer, or None for an unknown hash, places
    the transaction in."""
    slot = answer.get("slot") if isinstance(answer, dict) else None
    if type(slot) is not int:
        raise ChainUnavailable(f"{tx_hash}: the chain view gives no slot for it")
    return slot


def placed_at(tx_hash: str, chain: Any) -> int | None:
    """The slot ``chain`` places ``tx_hash`` in, or None if it has not seen it."""
    answer = _ask(chain.get_tx, tx_hash)
    return None if answer is None else _slot(tx_hash, answer)


def _amounts(entry: Any, tx_hash: str) -> dict[str, int]:
    """How much of each unit, not lovelace, a /txs/{hash}/utxos entry holds."""
    amounts: dict[str, int] = {}
    try:
        for a in entry["amount"]:
            if a["unit"] != "lovelace" and int(a["quantity"]) > 0:
                amounts[a["unit"]] = amounts.get(a["unit"], 0) + int(a["quantity"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ChainUnavailable(f"{tx_hash}: unexpected amounts from the chain view: {exc!r}") from exc
    return amounts


def _made_outputs(answer: Any, tx_hash: str) -> list:
    """The outputs a /txs/{hash}/utxos answer lists the transaction as
    making: not a valid transaction's collateral return, which the ledger
    never creates."""
    try:
        return [out for out in answer["outputs"] if out["collateral"] is False]
    except (KeyError, TypeError) as exc:
        raise ChainUnavailable(f"{tx_hash}: unexpected outputs from the chain view: {exc!r}") from exc


def _held_by(answer: Any, index: int, tx_hash: str) -> dict[str, int]:
    """What output ``index`` holds, by its transaction's listing."""
    for out in _made_outputs(answer, tx_hash):
        if out.get("output_index") == index:
            return _amounts(out, tx_hash)
    raise ChainUnavailable(f"{tx_hash}#{index}: the chain view lists no such output")


def _spent_inputs(answer: Any, tx_hash: str) -> list:
    """The inputs a /txs/{hash}/utxos answer lists the transaction as
    consuming: neither collateral, which a valid transaction keeps, nor
    reference inputs, which it only reads."""
    try:
        return [
            entry for entry in answer["inputs"]
            if entry["collateral"] is False and entry["reference"] is False
        ]
    except (KeyError, TypeError) as exc:
        raise ChainUnavailable(f"{tx_hash}: unexpected inputs from the chain view: {exc!r}") from exc


def _input_ref(entry: Any, tx_hash: str) -> tuple[bytes, int]:
    """The output an input entry spends. Its hash goes into a request path,
    so it must be a transaction hash and nothing else."""
    ref_tx, index = entry.get("tx_hash"), entry.get("output_index")
    if not (isinstance(ref_tx, str) and _TX_HASH.fullmatch(ref_tx) and type(index) is int):
        raise ChainUnavailable(f"{tx_hash}: unexpected input entry from the chain view: {entry!r:.200}")
    return bytes.fromhex(ref_tx), index


def _payment_key(entry: Any, tx_hash: str) -> bytes | None:
    """The payment credential of the address an input entry names; None for
    an address that has none (Byron)."""
    try:
        payment = Address.from_primitive(entry["address"]).payment_part
    except (DecodingException, DeserializeException):
        return None
    except KeyError as exc:
        raise ChainUnavailable(f"{tx_hash}: an input entry names no address") from exc
    return None if payment is None else payment.payload


def refuse_inside_cooling_off(approval: Approval, chain: Any, recorded: Recorded) -> None:
    """Raise :class:`CosignRejected` unless, of each unit ``approval``
    surrenders, its inputs held what it surrenders at the claimant's payment
    key COOLING_OFF_SLOTS or more before it was checked. Units of one kind
    cannot be told apart, so every unit that reached the claimant from
    another key, or was minted, inside the window counts against what the
    inputs hold, once, wherever it went afterwards."""
    cutoff = approval.checked_at - COOLING_OFF_SLOTS
    owner = approval.claimant[1:29]
    arrived = dict.fromkeys(approval.units, 0)
    latest: dict[str, tuple[int, str]] = {}
    pending, seen, inside, counted = list(approval.unit_inputs), set(), set(), set()
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        tx_id, index = ref
        tx_hash = tx_id.hex()
        spends, slot = recorded(tx_id), placed_at(tx_hash, chain)
        if slot is None and spends is None:
            raise ChainUnavailable(f"{tx_hash}: the chain view gives no slot for it")
        if slot is not None and slot <= cutoff:
            continue
        if spends is not None:
            # A recorded surrender, possibly still in the mempool. The policy
            # held all its inputs but the pool's to the claimant, so its
            # change carries only what those inputs brought.
            pending.extend(spends)
            continue
        inside.add(tx_id)
        if len(inside) > MAX_PROVENANCE_TXS:
            raise CosignRejected(
                "cooling_off", f"the surrendered units moved through more than {MAX_PROVENANCE_TXS}"
                f" transactions in the {COOLING_OFF_SLOTS} slots before the surrender",
            )
        answer = _ask(chain.get_tx_utxos, tx_hash)
        held = _held_by(answer, index, tx_hash)
        inputs = [(entry, _amounts(entry, tx_hash)) for entry in _spent_inputs(answer, tx_hash)]
        made = [_amounts(out, tx_hash) for out in _made_outputs(answer, tx_hash)]
        for unit in sorted(arrived.keys() & held.keys()):
            if (tx_id, unit) in counted:
                continue
            counted.add((tx_id, unit))
            own, entered = [], 0
            for entry, amounts in inputs:
                if unit in amounts:
                    if _payment_key(entry, tx_hash) == owner:
                        own.append(entry)
                    else:
                        entered += amounts[unit]
            # What the outputs hold beyond what the inputs brought was minted;
            # a burn does not offset what arrived.
            minted = sum(out.get(unit, 0) for out in made) - sum(amounts.get(unit, 0) for _, amounts in inputs)
            entered += max(0, minted)
            if entered:
                arrived[unit] += entered
                latest[unit] = max(latest.get(unit, (slot, tx_hash)), (slot, tx_hash))
            pending.extend(_input_ref(entry, tx_hash) for entry in own)

    for unit, quantity in sorted(approval.units.items()):
        ready = approval.held[unit] - arrived[unit]
        if quantity > ready:
            slot, tx_hash = latest[unit]
            raise CosignRejected(
                "cooling_off", f"{unit} reached the claimant in {tx_hash} at slot {slot}; {max(0, ready)} of"
                f" the {quantity} surrendered can be surrendered now, all from slot {slot + COOLING_OFF_SLOTS}",
            )


def mints_and_burns_after(unit: str, slot: int, chain: Any) -> Iterator[tuple[str, str, int]]:
    """Each mint or burn of ``unit`` after ``slot`` by the unit's own history
    on ``chain`` (a BlockfrostClient), as (action, transaction hash, its
    slot). One transaction lookup per entry, made only as far as the caller
    reads, so a history inflated with changes after the first costs a signer
    nothing more. Raises :class:`ChainUnavailable` once an entry up to that
    point, or the history itself, cannot be read."""
    history = _ask(chain.get_asset_history, unit)
    if not (isinstance(history, list) and history):
        raise ChainUnavailable(f"{unit}: the chain view gives no mint history")
    for event in history:
        tx_hash = _history_tx(unit, event)
        tx_slot = _slot(tx_hash, _ask(chain.get_tx, tx_hash))
        if tx_slot > slot:
            yield event.get("action"), tx_hash, tx_slot


def confirm_on_chain(approval: Approval, cfg: CosignConfig, chain: Any, recorded: Recorded) -> None:
    """Raise :class:`CosignRejected` if the chain contradicts ``approval``,
    or :class:`ChainUnavailable` if ``chain`` (a BlockfrostClient) cannot say.
    ``recorded(tx_hash)``: the outputs besides the pool's that a surrender
    this signer recorded spends, or None if it recorded no approval of
    ``tx_hash`` (RedemptionLedger.claimant_spends)."""
    for unit in sorted(approval.units):
        info = _ask(chain.get_asset_info, unit)
        if info is None:
            raise ChainUnavailable(f"{unit} is unknown to the chain view")
        current, pinned = int(info["quantity"]), cfg.pinned_supply.get(unit, 0)
        if current > pinned:
            raise CosignRejected(
                "supply_grown", f"{unit}: {current} on chain, {pinned} at the pin",
            )

    for tx_id in sorted({tx_id for tx_id, _ in approval.inputs}):
        if recorded(tx_id) is not None:
            continue
        answer = _ask(chain.get_tx_utxos, tx_id.hex())
        if answer is None:
            raise CosignRejected("input_unknown", f"{tx_id.hex()} is neither on chain nor recorded")
        unspent = _unspent_outputs(answer)
        for ref_tx, index in approval.inputs:
            if ref_tx == tx_id and index not in unspent:
                raise CosignRejected("input_spent", f"{tx_id.hex()}#{index} is not an unspent output")

    # A query of its own, after the inputs: the supply answer read first may
    # lag a mint, and a mint offset by a burn leaves the supply as pinned.
    for unit in sorted(cfg.redeemable_nfts.intersection(approval.units)):
        change = next(mints_and_burns_after(unit, cfg.supply_slot, chain), None)
        if change is not None:
            action, tx_hash, slot = change
            raise CosignRejected(
                "minted_after_pin",
                f"{unit}: {action} in {tx_hash} at slot {slot}, after the pin at slot {cfg.supply_slot}",
            )

    refuse_inside_cooling_off(approval, chain, recorded)
