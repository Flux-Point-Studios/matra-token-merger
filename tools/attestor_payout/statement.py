"""A period's attestor payout statement, and the checker that recomputes it.

Credits: each signer of a receipt certified by threshold inside the period
earns one credit. The signer set is replayed from ``AttestationRecorded``
events the way the pallet keeps it: before each attest, signers no longer on
the committee are dropped, so a signer who left before the threshold earns
nothing. Every attest carries the chain's own signer count. Events missed
before the scan start can only leave the replayed set short of the chain's,
never over it, so where the threshold attest's count equals the replayed set
the two sets are equal; a credited certification where they differ is
refused, which is how a lookback too short to see a receipt's first attests
is caught. A certification Root sets directly credits no one.

Pricing: a credit is worth ``usd_micros_per_credit`` micro-USD, paid in
cMATRA at ``usd_micros_per_cmatra``. Each attestor is held to
``cap_per_attestor``; if the period still exceeds ``cap_total`` every line is
scaled down by the same fraction, rounding down. FPS seats in ``excluded``
are listed with their credits and paid nothing.

The checker builds the statement again from a second node and refuses the
presented one on any difference, naming each differing field.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pycardano import Address, Network

from tools.attestor_payout.materios_events import ChainSlice, Event
from tools.attestor_payout.merkle import leaf_hash, merkle_root
from tools.attestor_payout.registration import parse_attestor
from tools.config import ATTESTOR_EMISSIONS_BASE, MERGE_TOKEN_DECIMALS

SCHEMA = "materios-attestor-payout/1"
ANCHOR_PROTOCOL = "materios-payout"
_HEX32 = re.compile(r"[0-9a-f]{64}")
_HEX28 = re.compile(r"[0-9a-f]{56}")
_RATE_KEYS = {"usd_micros_per_credit", "usd_micros_per_cmatra", "price_source", "cap_per_attestor", "cap_total"}


class ConfigError(ValueError):
    """The period config is not of this shape."""


class StatementError(ValueError):
    """A statement cannot be built from these inputs."""


class HistoryIncomplete(StatementError):
    """The events read do not explain a signer count the chain recorded."""


class StatementMismatch(StatementError):
    """The presented statement is not the one the checker's source gives."""


@dataclass(frozen=True)
class PeriodConfig:
    network: str
    chain: str
    period: int
    first_block: int
    last_block: int
    scan_from: int
    rates: dict
    excluded: frozenset[bytes]
    cmatra: dict
    float_keys: tuple[bytes, bytes]

    @property
    def cardano_network(self) -> Network:
        return Network.MAINNET if self.network == "mainnet" else Network.TESTNET

    @property
    def cap_per_attestor(self) -> int:
        return self.rates["cap_per_attestor"]

    @property
    def cap_total(self) -> int:
        return self.rates["cap_total"]


def _keys(value: Any, keys: set[str], where: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ConfigError(f"{where} must have exactly the keys {sorted(keys)}")
    return value


def _count(value: Any, where: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{where} must be an integer of at least {minimum}, not {value!r}")
    return value


def load_period_config(path: str | Path) -> PeriodConfig:
    try:
        doc = json.loads(Path(path).read_text())
    except ValueError as exc:
        raise ConfigError(f"{path}: not JSON: {exc}") from None
    doc = _keys(doc, {"version", "network", "chain", "period", "blocks", "rates", "excluded", "cmatra", "float_keys"},
                "the period config")
    if type(doc["version"]) is not int or doc["version"] != 1:
        raise ConfigError("version must be the integer 1")
    if doc["network"] not in ("mainnet", "preprod"):
        raise ConfigError(f"network must be mainnet or preprod, not {doc['network']!r}")
    if not (isinstance(doc["chain"], str) and _HEX32.fullmatch(doc["chain"])):
        raise ConfigError("chain must be the Materios genesis hash as 64 lowercase hex digits")
    period = doc["period"]
    if type(period) is not int or not (200001 <= period <= 999912 and 1 <= period % 100 <= 12):
        raise ConfigError(f"period must be a month as the integer YYYYMM, not {period!r}")

    blocks = _keys(doc["blocks"], {"first", "last", "lookback"}, "blocks")
    first = _count(blocks["first"], "blocks.first")
    last = _count(blocks["last"], "blocks.last", first)
    scan_from = first - _count(blocks["lookback"], "blocks.lookback", 0)
    if scan_from < 1:
        raise ConfigError(f"blocks: the lookback reaches before block 1 (scan from {scan_from})")

    rates = _keys(doc["rates"], _RATE_KEYS, "rates")
    for key in ("usd_micros_per_credit", "usd_micros_per_cmatra"):
        _count(rates[key], f"rates.{key}")
    if not isinstance(rates["price_source"], str) or not rates["price_source"].strip():
        raise ConfigError("rates.price_source must name where the cMATRA price came from")
    cap_total = _count(rates["cap_total"], "rates.cap_total")
    if cap_total > ATTESTOR_EMISSIONS_BASE:
        raise ConfigError(f"rates.cap_total {cap_total} exceeds the attestor bucket {ATTESTOR_EMISSIONS_BASE}")
    if _count(rates["cap_per_attestor"], "rates.cap_per_attestor") > cap_total:
        raise ConfigError("rates.cap_per_attestor exceeds rates.cap_total")

    if not isinstance(doc["excluded"], list):
        raise ConfigError("excluded must be a list")
    try:
        excluded = [parse_attestor(a) for a in doc["excluded"]]
    except ValueError as exc:
        raise ConfigError(f"excluded: {exc}") from None
    if len(set(excluded)) != len(excluded):
        raise ConfigError("excluded lists an attestor twice")

    cmatra = _keys(doc["cmatra"], {"policy", "asset"}, "cmatra")
    if not (isinstance(cmatra["policy"], str) and _HEX28.fullmatch(cmatra["policy"])
            and isinstance(cmatra["asset"], str) and re.fullmatch(r"(?:[0-9a-f]{2}){1,32}", cmatra["asset"])):
        raise ConfigError("cmatra must name a 28-byte policy and an asset name of 1-32 bytes, in lowercase hex")
    keys = doc["float_keys"]
    if not (isinstance(keys, list) and len(keys) == 2 and len(set(keys)) == 2
            and all(isinstance(k, str) and _HEX28.fullmatch(k) for k in keys)):
        raise ConfigError("float_keys must be two distinct 28-byte key hashes in lowercase hex")
    return PeriodConfig(
        network=doc["network"], chain=doc["chain"], period=period,
        first_block=first, last_block=last, scan_from=scan_from,
        rates=dict(rates), excluded=frozenset(excluded), cmatra=dict(cmatra),
        float_keys=(bytes.fromhex(keys[0]), bytes.fromhex(keys[1])),
    )


def certification_credits(events: Iterable[Event], first: int) -> tuple[dict[bytes, int], int]:
    """(credits per attestor, receipts certified by threshold) for the
    certifications at or after block ``first``."""
    member: dict[str, bool] = {}
    signers: dict[str, set[str]] = {}
    last_attest: dict[str, tuple[int, int | None, int, int, int]] = {}
    credits: Counter[str] = Counter()
    certified = 0
    for event in events:
        fields = event.fields
        if event.name == "AttestationRecorded":
            receipt, who = fields["receipt_id"], fields["attester"]
            current = {s for s in signers.get(receipt, ()) if member[s]} | {who}
            member[who] = True
            signers[receipt] = current
            last_attest[receipt] = (event.block, event.extrinsic, fields["count"], fields["threshold"], len(current))
        elif event.name == "AvailabilityCertified":
            receipt = fields["receipt_id"]
            attest = last_attest.pop(receipt, None)
            current = signers.pop(receipt, set())
            by_threshold = attest is not None and attest[:2] == (event.block, event.extrinsic) and attest[2] >= attest[3]
            if not (by_threshold and event.block >= first):
                continue
            if attest[4] != attest[2]:
                raise HistoryIncomplete(
                    f"receipt {receipt} certified in block {event.block}: the chain counts {attest[2]} signers and "
                    f"the events read show {attest[4]}; read from an earlier block (raise the lookback)")
            credits.update(current)
            certified += 1
        elif event.name == "CommitteeMemberJoined":
            member[fields["who"]] = True
        elif event.name in ("CommitteeMemberLeft", "AutoEjected"):
            member[fields["who"]] = False
        elif event.name == "CommitteeUpdated":
            members = set(fields["members"])
            member = {who: who in members for who in member} | {who: True for who in members}
    return {parse_attestor(who): n for who, n in credits.items()}, certified


def _priced(config: PeriodConfig, credits: int) -> int:
    rates = config.rates
    return credits * rates["usd_micros_per_credit"] * 10 ** MERGE_TOKEN_DECIMALS // rates["usd_micros_per_cmatra"]


def statement_leaves(statement: dict) -> list[bytes]:
    """The leaf of each line paid a nonzero amount, in line order."""
    return [
        leaf_hash(statement["period"], parse_attestor(line["attestor"]),
                  bytes(Address.from_primitive(line["address"])), line["amount"])
        for line in statement["lines"] if line["amount"] > 0
    ]


def build_statement(config: PeriodConfig, chain: ChainSlice, registry: dict[bytes, Address]) -> dict:
    if chain.genesis.removeprefix("0x").lower() != config.chain:
        raise StatementError(f"the node's genesis {chain.genesis} is not the config's chain {config.chain}")
    if (chain.scan_from, chain.first, chain.last) != (config.scan_from, config.first_block, config.last_block):
        raise StatementError(
            f"the events read cover range {chain.scan_from}/{chain.first}..{chain.last}, not the config's "
            f"{config.scan_from}/{config.first_block}..{config.last_block}")
    credits, certified = certification_credits(chain.events, config.first_block)

    paid = sorted(a for a in credits if a not in config.excluded)
    missing = [f"0x{a.hex()}" for a in paid if a not in registry]
    if missing:
        raise StatementError(f"credited attestors with no registered payout address: {', '.join(missing)}")
    computed = {a: _priced(config, credits[a]) for a in paid}
    capped = {a: min(computed[a], config.cap_per_attestor) for a in paid}
    over = sum(capped.values())
    amounts = capped if over <= config.cap_total else {a: capped[a] * config.cap_total // over for a in paid}

    statement = {
        "schema": SCHEMA,
        "network": config.network,
        "chain": config.chain,
        "period": config.period,
        "blocks": {"scan_from": chain.scan_from, "first": chain.first, "first_hash": chain.first_hash,
                   "last": chain.last, "last_hash": chain.last_hash},
        "rates": dict(config.rates),
        "cmatra": dict(config.cmatra),
        "receipts_certified": certified,
        "lines": [{"attestor": f"0x{a.hex()}", "address": registry[a].encode(), "credits": credits[a],
                   "computed": computed[a], "amount": amounts[a]} for a in paid],
        "excluded": [{"attestor": f"0x{a.hex()}", "credits": credits[a]}
                     for a in sorted(a for a in credits if a in config.excluded)],
        "total": sum(amounts.values()),
    }
    leaves = statement_leaves(statement)
    statement["merkle"] = {"root": merkle_root(leaves).hex(), "leaves": len(leaves)}
    return statement


def statement_digest(statement: dict) -> str:
    canonical = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.blake2b(canonical, digest_size=32).hexdigest()


def anchor_metadata(statement: dict) -> dict:
    """The label-8746 record of the statement. It is an audit record: no
    script reads it."""
    return {
        "p": ANCHOR_PROTOCOL,
        "v": 1,
        "chain": statement["chain"],
        "period": statement["period"],
        "blocks": [statement["blocks"]["first"], statement["blocks"]["last"]],
        "leaves": statement["merkle"]["leaves"],
        "total": statement["total"],
        "root": statement["merkle"]["root"],
        "statement": statement_digest(statement),
    }


def _differences(presented: Any, expected: Any, path: str) -> list[str]:
    if isinstance(presented, dict) and isinstance(expected, dict):
        return [d for key in sorted(presented.keys() | expected.keys())
                for d in (_differences(presented[key], expected[key], f"{path}.{key}" if path else key)
                          if key in presented and key in expected else [f"{path}.{key}" if path else key])]
    if isinstance(presented, list) and isinstance(expected, list):
        if len(presented) != len(expected):
            return [f"{path} has {len(presented)} entries, not {len(expected)}"]
        return [d for n, (p, e) in enumerate(zip(presented, expected)) for d in _differences(p, e, f"{path}[{n}]")]
    return [] if type(presented) is type(expected) and presented == expected else [path]


def check_statement(statement: dict, config: PeriodConfig, chain: ChainSlice, registry: dict[bytes, Address]) -> None:
    """Refuses ``statement`` unless the checker's own source builds it exactly."""
    differences = _differences(statement, build_statement(config, chain, registry), "")
    if differences:
        raise StatementMismatch(f"the statement differs from the checker's at: {', '.join(differences)}")
