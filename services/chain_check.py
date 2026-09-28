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
    ledger holds approvals to.

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

from services.cosign_policy import Approval, CosignConfig, CosignRejected
from tools.api_clients import BlockfrostUnavailable


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


def confirm_on_chain(
    approval: Approval, cfg: CosignConfig, chain: Any, recorded: Callable[[bytes], bool],
) -> None:
    """Raise :class:`CosignRejected` if the chain contradicts ``approval``,
    or :class:`ChainUnavailable` if ``chain`` (a BlockfrostClient) cannot say.
    ``recorded(tx_hash)``: this signer recorded an approval of ``tx_hash``."""
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
        if recorded(tx_id):
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
