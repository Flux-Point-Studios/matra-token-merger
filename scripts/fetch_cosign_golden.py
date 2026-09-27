#!/usr/bin/env python3
"""Rebuild tests/fixtures/cosign_golden.json.gz from mainnet (read-only).

Every transaction that ever touched the surrender pool address is fetched with
the bodies of all transactions that produced its inputs, collateral and
reference inputs, plus the PlutusV3 cost-model encoding in force in its epoch.
Surrenders (a pool input spent and legacy assets paid to quarantine) are the
corpus the co-signer policy must accept; the rest are the mint and pool
rotation ceremonies it must refuse.

Needs NETWORK=mainnet and BLOCKFROST_PROJECT_ID in the environment.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import cbor2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pycardano import Address  # noqa: E402

from services.cosign_policy import blake2b_256, decode, parse_output, split_tx  # noqa: E402
from tools.api_clients import BlockfrostClient  # noqa: E402

POOL_ADDRESS = "addr1w8s6rqdjlzm5he27v9s202p8vjumza8qfsmufm2f6dy68hg9mn27a"
QUARANTINE_ADDRESS = "addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl"
OUT = Path(__file__).resolve().parent.parent / "tests/fixtures/cosign_golden.json.gz"

SHELLEY_FIRST_SLOT, SHELLEY_FIRST_EPOCH, EPOCH_SLOTS = 4_492_800, 208, 432_000


def _refs(body: dict) -> list:
    refs = []
    for key in (0, 13, 18):
        value = body.get(key, [])
        refs.extend(value.value if hasattr(value, "tag") else value)
    return refs


def main() -> None:
    bf = BlockfrostClient()
    pool = Address.from_primitive(POOL_ADDRESS).to_primitive()
    quarantine = Address.from_primitive(QUARANTINE_ADDRESS).to_primitive()

    parents: dict[str, str] = {}
    views: list[str] = []
    views_by_epoch: dict[int, int] = {}
    transactions = []
    for row in bf.get_address_transactions(POOL_ADDRESS):
        tx_hash = row["tx_hash"]
        tx_cbor = bf.get_tx_cbor(tx_hash)
        body_raw, _, _ = split_tx(tx_cbor)
        assert blake2b_256(body_raw).hex() == tx_hash, tx_hash
        body = decode(body_raw)
        for txid, _index in _refs(body):
            if txid.hex() not in parents:
                parent_body, _, _ = split_tx(bf.get_tx_cbor(txid.hex()))
                assert blake2b_256(parent_body) == txid, txid.hex()
                parents[txid.hex()] = parent_body.hex()

        spends_pool = any(
            parse_output(decode(bytes.fromhex(parents[txid.hex()]))[1][index]).address == pool
            for txid, index in (body[0].value if hasattr(body[0], "tag") else body[0])
        )
        pays_quarantine = any(parse_output(o).address == quarantine for o in body[1])

        slot = bf.get_tx(tx_hash)["slot"]
        epoch = SHELLEY_FIRST_EPOCH + (slot - SHELLEY_FIRST_SLOT) // EPOCH_SLOTS
        if epoch not in views_by_epoch:
            cost_model = bf.get_epoch_parameters(epoch)["cost_models_raw"]["PlutusV3"]
            encoded = cbor2.dumps({2: cost_model}).hex()
            if encoded not in views:
                views.append(encoded)
            views_by_epoch[epoch] = views.index(encoded)

        transactions.append({
            "tx_hash": tx_hash,
            "kind": "surrender" if spends_pool and pays_quarantine else "ceremony",
            "slot": slot,
            "language_views": views_by_epoch[epoch],
            "tx_cbor": tx_cbor.hex(),
        })
        print(f"{len(transactions):4d} {tx_hash} {transactions[-1]['kind']}", flush=True)

    fixture = {
        "network": "mainnet",
        "pool_address": POOL_ADDRESS,
        "quarantine_address": QUARANTINE_ADDRESS,
        "language_views": views,
        "parents": parents,
        "transactions": transactions,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", compresslevel=9) as fh:
        json.dump(fixture, fh, sort_keys=True)
    kinds = [t["kind"] for t in transactions]
    print(f"wrote {OUT}: {kinds.count('surrender')} surrenders, "
          f"{kinds.count('ceremony')} ceremonies, {len(parents)} producing bodies")


if __name__ == "__main__":
    main()
