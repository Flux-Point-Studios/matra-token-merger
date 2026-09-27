"""What a signer confirms with the chain itself before it records an approval.

services.cosign_policy judges a transaction by the bytes it is handed. What
those bytes cannot show comes from the signer's own view of the chain, a
Blockfrost project queried from the signer's own host:

  * no surrendered unit has more supply on chain than it had at the pin. The
    two collection policies with a single key and no time lock can still mint
    another edition of a pinned name, and editions of one name are
    indistinguishable, so while a later edition exists the unit is not
    redeemed at all: otherwise its minter could be paid ahead of the holder of
    the pinned edition.

A view that cannot answer raises :class:`ChainUnavailable`, and nothing is
signed.
"""

from __future__ import annotations

from typing import Any, Callable

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


def confirm_on_chain(approval: Approval, cfg: CosignConfig, chain: Any) -> None:
    """Raise :class:`CosignRejected` if the chain contradicts ``approval``,
    or :class:`ChainUnavailable` if ``chain`` (a BlockfrostClient) cannot say."""
    for unit in sorted(approval.units):
        info = _ask(chain.get_asset_info, unit)
        if info is None:
            raise ChainUnavailable(f"{unit} is unknown to the chain view")
        current, pinned = int(info["quantity"]), cfg.pinned_supply.get(unit, 0)
        if current > pinned:
            raise CosignRejected(
                "supply_grown", f"{unit}: {current} on chain, {pinned} at the pin",
            )
