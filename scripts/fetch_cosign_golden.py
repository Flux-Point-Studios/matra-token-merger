#!/usr/bin/env python3
"""Rebuild the co-signer policy's mainnet fixtures (read-only).

With no argument, tests/fixtures/cosign_golden.json.gz: every transaction that
ever touched the surrender pool address, with the bodies of all transactions
that produced its inputs, collateral and reference inputs, plus the PlutusV3
cost-model encoding in force in its epoch. Surrenders (a pool input spent and
legacy assets paid to quarantine) are the corpus the co-signer policy must
accept; the rest are the mint and pool rotation ceremonies it must refuse.

With --recent, tests/fixtures/recent_claims.json.gz: every surrender after
the golden set's, up to the stop on 2026-11-02, in the same form: the 19 the
attacker made with FLUX_PASS taken from W1 (kind "forfeited") and the
legitimate ones beside them. Each claim's cooling-off walk
(services.chain_check.refuse_inside_cooling_off, at the claim's validity
start, nothing recorded) runs against the provider, and the fixture also
holds every slot it asks and, for each listing it asks, the bodies a chain
view derives that listing from: what a signer asks about where the
surrendered units came from, and no more.

Needs NETWORK=mainnet and the chain provider settings in the environment.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import cbor2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pycardano import Address  # noqa: E402

from dataclasses import replace  # noqa: E402

from services.chain_check import refuse_inside_cooling_off  # noqa: E402
from services.cosign_policy import (  # noqa: E402
    CosignRejected,
    blake2b_256,
    decode,
    evaluate_surrender,
    load_config,
    parse_output,
    split_tx,
)
from tools.api_clients import BlockfrostClient  # noqa: E402
from tools.credential_denylist import DenyList  # noqa: E402

POOL_ADDRESS = "addr1w8s6rqdjlzm5he27v9s202p8vjumza8qfsmufm2f6dy68hg9mn27a"
QUARANTINE_ADDRESS = "addr1wy5gl6nh5rm8f3sgp2ka3mfu5skdt2fqhu0spsxnucesdeqatlhxl"
FIXTURES = Path(__file__).resolve().parent.parent / "tests/fixtures"

SHELLEY_FIRST_SLOT, SHELLEY_FIRST_EPOCH, EPOCH_SLOTS = 4_492_800, 208, 432_000

# The forfeit ledger: every surrender paid to the attacker's claim address.
FORFEITED_CLAIMS = [
    "a08b8b1739017285d853b84c8e71f52bc92c18bf6116ac7da7587df468d7382a",
    "e4c9205e90e169cd69ed68531bde651b7fd5c90be17196fc59805447243c6dd9",
    "63a858c2f02943fc9dcbea8e76c7c05210e7dddd7120293290d1ad285ead7941",
    "d42fa0ae1bc1d633d8ffe89e83688fc19bad31172740e5c89be396b6a6160f26",
    "605ef183695957119861ebf4d4588fdb66cb61d2802baa31d7b956d577c7e373",
    "c7d331184dc5a1c5ef7be4f0e57a1da7d46dcb9c8f52f77b87dd04f67cbad78a",
    "4386a6e6fd2f5ed6cb27767f140856015f795a1f540f4cbbb341dcec4802e40d",
    "272aaec2771bcd5e7c2ce99f37aeb681f78166f0aae7e9a414b4e605c5586a56",
    "f8d2c4aa9749a62302f507b05eaf408a3bc610a5bf974f55f7be4374df2b6041",
    "9999ba215c7787e8beb3c58a66afb458b57d842ecbbe860e7a87032fa3d13170",
    "a15a648c0a0abd9048790e533e2c7c3bc2ce44b7087419d2870c73f47dfd9996",
    "d9d7b79360316c567907c6ec575a5e28610d92f17ef41eaeee4b76f90c8b0f5b",
    "b4fe17762243007197300aaadb7a58bc8499c5a69326f078a6f3a4967c1a0eb8",
    "18b80545e43ffd2cf481774cf9028dd90f94508ef72e554e0eeb1945b6506e4f",
    "c9291b849906e00ab24280442ec2cd78bb0135a59bb7c0d6c2d0e6028dcd3c95",
    "8474949d4c628d52a1c8ec0b9f6cebc49aeeec6456004311d3891aa2feb1e48c",
    "5bb694e86a9b300a986ed8321b4d12f90e9d4068803763aabdd7a651fb830a6b",
    "dce4e0b8f5a4510fa9db69d40ce9ae566b9860ab65ba5ee99cd5b3acd16ae068",
    "d3a1e632424794bed36d7062b4c09c5fe547fa7a1addbd2d45f9b84df773c733",
]


def _refs(body: dict) -> list:
    refs = []
    for key in (0, 13, 18):
        value = body.get(key, [])
        refs.extend(value.value if hasattr(value, "tag") else value)
    return refs


class Fetcher:
    """Transactions, producing bodies and language views, each checked
    against its hash and fetched once."""

    def __init__(self, bf: BlockfrostClient) -> None:
        self.bf = bf
        self.parents: dict[str, str] = {}
        self.views: list[str] = []
        self.views_by_epoch: dict[int, int] = {}
        self.slots: dict[str, int] = {}

    def body(self, tx_hash: str) -> dict:
        """The decoded body of ``tx_hash``, kept among the producing bodies."""
        if tx_hash not in self.parents:
            body, _, _ = split_tx(self.bf.get_tx_cbor(tx_hash))
            assert blake2b_256(body).hex() == tx_hash, tx_hash
            self.parents[tx_hash] = body.hex()
        return decode(bytes.fromhex(self.parents[tx_hash]))

    def slot(self, tx_hash: str) -> int:
        if tx_hash not in self.slots:
            self.slots[tx_hash] = self.bf.get_tx(tx_hash)["slot"]
        return self.slots[tx_hash]

    def views_at(self, slot: int) -> int:
        epoch = SHELLEY_FIRST_EPOCH + (slot - SHELLEY_FIRST_SLOT) // EPOCH_SLOTS
        if epoch not in self.views_by_epoch:
            cost_model = self.bf.get_epoch_parameters(epoch)["cost_models_raw"]["PlutusV3"]
            encoded = cbor2.dumps({2: cost_model}).hex()
            if encoded not in self.views:
                self.views.append(encoded)
            self.views_by_epoch[epoch] = self.views.index(encoded)
        return self.views_by_epoch[epoch]

    def transaction(self, tx_hash: str, depth: int = 1) -> tuple[dict, dict]:
        """(the fixture row of ``tx_hash``, its decoded body), with the bodies
        that produced its inputs ``depth`` transactions deep."""
        tx_cbor = self.bf.get_tx_cbor(tx_hash)
        body_raw, _, _ = split_tx(tx_cbor)
        assert blake2b_256(body_raw).hex() == tx_hash, tx_hash
        body = decode(body_raw)
        level = [body]
        for _ in range(depth):
            level = [self.body(txid.hex()) for produced in level for txid, _index in _refs(produced)]
        slot = self.slot(tx_hash)
        row = {"tx_hash": tx_hash, "slot": slot, "language_views": self.views_at(slot), "tx_cbor": tx_cbor.hex()}
        return row, body

    def fixture(self, transactions: list[dict], **extra) -> dict:
        return {
            "network": "mainnet",
            "pool_address": POOL_ADDRESS,
            "quarantine_address": QUARANTINE_ADDRESS,
            "language_views": self.views,
            "parents": self.parents,
            "transactions": transactions,
            **extra,
        }


def golden(bf: BlockfrostClient) -> dict:
    fetch = Fetcher(bf)
    pool = Address.from_primitive(POOL_ADDRESS).to_primitive()
    quarantine = Address.from_primitive(QUARANTINE_ADDRESS).to_primitive()
    transactions = []
    for entry in bf.get_address_transactions(POOL_ADDRESS):
        row, body = fetch.transaction(entry["tx_hash"])
        spends_pool = any(
            parse_output(decode(bytes.fromhex(fetch.parents[txid.hex()]))[1][index]).address == pool
            for txid, index in (body[0].value if hasattr(body[0], "tag") else body[0])
        )
        pays_quarantine = any(parse_output(o).address == quarantine for o in body[1])
        transactions.append({**row, "kind": "surrender" if spends_pool and pays_quarantine else "ceremony"})
        print(f"{len(transactions):4d} {row['tx_hash']} {transactions[-1]['kind']}", flush=True)
    return fetch.fixture(transactions)


class WalkRecorder:
    """The provider as the cooling-off walk reads it, keeping the slot of
    every transaction it places and the bodies behind every listing."""

    def __init__(self, fetch: Fetcher) -> None:
        self.fetch = fetch

    def get_tx(self, tx_hash: str) -> dict:
        return {"hash": tx_hash, "slot": self.fetch.slot(tx_hash)}

    def get_tx_utxos(self, tx_hash: str) -> dict:
        for txid, _index in _refs(self.fetch.body(tx_hash)):
            self.fetch.body(txid.hex())
        return self.fetch.bf.get_tx_utxos(tx_hash)


def _policy() -> object:
    """The mainnet pool's policy with no deny-list and no payout cap, under
    which every surrender the pool paid is approved."""
    cfg = load_config({
        "NETWORK": "mainnet", "SURRENDER_SCRIPT_ADDRESS": POOL_ADDRESS, "QUARANTINE_ADDRESS": QUARANTINE_ADDRESS,
        "CMATRA_POLICY_HEX": "7ff33a5565393dc47b48ac47becc12d92c9952e724e8446dfb6adc66",
        "CMATRA_ASSET_HEX": "634d41545241", "SURRENDER_DEADLINE_POSIX_MS": "1795910400000",
        "MAX_CMATRA_PER_TX": str(10**15),
    }, [bytes.fromhex("f1eee828ae800a5bad6f3913603e20531162cdf44c5e0ffa49b6a936"),
        bytes.fromhex("77fdb621ad5f926257ce1a2f845fc946ae3d5a683ac89128bad3410d")])
    return replace(cfg, denylist=DenyList(labels={}, allowed=frozenset(), sha256="none"))


def recent(bf: BlockfrostClient) -> dict:
    with gzip.open(FIXTURES / "cosign_golden.json.gz", "rt") as fh:
        known = {t["tx_hash"] for t in json.load(fh)["transactions"]}
    fetch, cfg = Fetcher(bf), _policy()
    transactions = []
    for entry in bf.get_address_transactions(POOL_ADDRESS):
        tx_hash = entry["tx_hash"]
        if tx_hash in known:
            continue
        row, body = fetch.transaction(tx_hash)
        tx = bytes.fromhex(row["tx_cbor"])
        parents = [bytes.fromhex(fetch.parents[txid.hex()]) for txid, _index in _refs(body)]
        views = bytes.fromhex(fetch.views[row["language_views"]])
        approval = evaluate_surrender(tx, views, parents, body.get(8, row["slot"]), cfg)
        try:
            refuse_inside_cooling_off(approval, WalkRecorder(fetch), lambda _tx_id: False)
            walk = "held"
        except CosignRejected as exc:
            walk = exc.code
        kind = "forfeited" if tx_hash in FORFEITED_CLAIMS else "surrender"
        transactions.append({**row, "kind": kind})
        print(f"{len(transactions):4d} {tx_hash} {kind} {walk}", flush=True)
    assert {t["tx_hash"] for t in transactions if t["kind"] == "forfeited"} == set(FORFEITED_CLAIMS)
    return fetch.fixture(transactions, slots=fetch.slots)


def write(fixture: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", compresslevel=9) as fh:
        json.dump(fixture, fh, sort_keys=True)
    kinds = [t["kind"] for t in fixture["transactions"]]
    print(f"wrote {out}: {kinds.count('surrender')} surrenders, {kinds.count('forfeited')} forfeited, "
          f"{kinds.count('ceremony')} ceremonies, {len(fixture['parents'])} producing bodies")


def main(argv: list[str], bf: BlockfrostClient | None = None) -> None:
    bf = bf or BlockfrostClient()
    if argv == ["--recent"]:
        write(recent(bf), FIXTURES / "recent_claims.json.gz")
    elif not argv:
        write(golden(bf), FIXTURES / "cosign_golden.json.gz")
    else:
        sys.exit("usage: python scripts/fetch_cosign_golden.py [--recent]")


if __name__ == "__main__":
    main(sys.argv[1:])
