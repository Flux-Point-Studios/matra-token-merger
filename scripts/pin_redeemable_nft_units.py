#!/usr/bin/env python3
"""Pin the NFT units each merge collection can redeem (read-only, mainnet).

Two collection policies are single-key scripts with no time lock, so their
key holder can still mint. Surrender pricing therefore accepts only the units
that exist when the pin is taken, never "anything under the policy".

    NETWORK=mainnet BLOCKFROST_PROJECT_ID=... \\
        python -m scripts.pin_redeemable_nft_units audit_pack/<date>/redeemable_nft_units.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tools.api_clients import BlockfrostClient
from tools.config import (
    CIP68_REFERENCE_TOKEN_PREFIX,
    CIP68_USER_TOKEN_PREFIX,
    NFT_COLLECTIONS,
)


def redeemable_names(policy_assets: list[dict]) -> list[str]:
    """Asset-name hex of every unit still in circulation (supply > 0). A CIP-68
    collection redeems only its user tokens; any other collection redeems every
    live name except CIP-68 reference tokens, editions with supply > 1 included."""
    live = sorted(
        row["asset"][56:] for row in policy_assets if int(row["quantity"]) > 0
    )
    user_tokens = [n for n in live if n.startswith(CIP68_USER_TOKEN_PREFIX)]
    if user_tokens:
        return user_tokens
    return [n for n in live if not n.startswith(CIP68_REFERENCE_TOKEN_PREFIX)]


def main(out: Path) -> None:
    bf = BlockfrostClient()
    tip = bf.get_latest_block()
    doc = {
        "pinned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pinned_at_slot": tip["slot"],
        "collections": {
            nft.name: {
                "policy_id": nft.policy_id,
                "asset_names": redeemable_names(bf.get_policy_assets(nft.policy_id)),
            }
            for nft in NFT_COLLECTIONS
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    for name, entry in doc["collections"].items():
        print(f"{name}: {len(entry['asset_names'])} units")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
