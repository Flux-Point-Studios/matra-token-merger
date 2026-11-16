"""Read-only Materios JSON-RPC reader for the OrinqReceipts events a payout
is computed from.

Each block's ``System.Events`` is read with ``state_getStorage`` and decoded
with the metadata of the runtime that wrote it, which is the runtime of the
block's parent. A node keeps the state of only its last 256 blocks unless it
runs with ``--state-pruning archive``, so a period's blocks are read from an
archive node; a pruned block is refused, never read as a block without events.
Only blocks up to the node's finalized head are read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping

import requests
from scalecodec.base import RuntimeConfigurationObject, ScaleBytes
from scalecodec.type_registry import load_type_registry_preset

EVENTS_KEY = "0x26aa394eea5630e07c48ae0c9558cef780d41e5e16056765bc8461851072c9d7"
PALLET = "OrinqReceipts"
TRACKED = frozenset({
    "AttestationRecorded",
    "AvailabilityCertified",
    "CommitteeMemberJoined",
    "CommitteeMemberLeft",
    "CommitteeUpdated",
    "AutoEjected",
})
BATCH = 64


class MateriosRpcError(RuntimeError):
    """The node refused a call or answered something unreadable."""


@dataclass(frozen=True)
class Event:
    block: int
    index: int
    extrinsic: int | None
    name: str
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class ChainSlice:
    """The tracked events of blocks ``scan_from``..``last``, read from one
    node, with the hashes that pin the period's bounds."""

    genesis: str
    scan_from: int
    first: int
    last: int
    first_hash: str
    last_hash: str
    events: tuple[Event, ...]


class _Rpc:
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()

    def batch(self, calls: list[tuple[str, list]]) -> list[Any]:
        body = [{"jsonrpc": "2.0", "id": n, "method": m, "params": p} for n, (m, p) in enumerate(calls)]
        response = self.session.post(self.url, json=body, timeout=120)
        response.raise_for_status()
        answers = response.json()
        if not isinstance(answers, list):
            raise MateriosRpcError(f"a batch of {len(calls)} calls was answered with {type(answers).__name__}")
        by_id = {a.get("id"): a for a in answers if isinstance(a, dict)}
        results = []
        for n, (method, params) in enumerate(calls):
            answer = by_id.get(n)
            if answer is None:
                raise MateriosRpcError(f"no answer to {method}{params}")
            if "error" in answer:
                raise MateriosRpcError(f"{method}{params}: {answer['error']}")
            results.append(answer.get("result"))
        return results

    def call(self, method: str, params: list) -> Any:
        return self.batch([(method, params)])[0]


class _Decoders:
    """One scale-codec runtime per spec version, built from its metadata."""

    def __init__(self, rpc: _Rpc):
        self.rpc = rpc
        self.by_spec: dict[int, tuple[RuntimeConfigurationObject, Any, str]] = {}

    def events(self, spec: int, at_hash: str, raw: str) -> list[dict]:
        if spec not in self.by_spec:
            runtime = RuntimeConfigurationObject()
            runtime.update_type_registry(load_type_registry_preset("core"))
            runtime.update_type_registry(load_type_registry_preset("legacy"))
            metadata = runtime.create_scale_object("MetadataVersioned", data=ScaleBytes(self.rpc.call("state_getMetadata", [at_hash])))
            metadata.decode()
            runtime.add_portable_registry(metadata)
            events_type = metadata.get_metadata_pallet("System").get_storage_function("Events").get_value_type_string()
            self.by_spec[spec] = (runtime, metadata, events_type)
        runtime, metadata, events_type = self.by_spec[spec]
        decoded = runtime.create_scale_object(events_type, data=ScaleBytes(raw), metadata=metadata)
        decoded.decode()
        return decoded.value


def _spec(version: Any) -> int:
    if not isinstance(version, dict) or not isinstance(version.get("specVersion"), int):
        raise MateriosRpcError(f"unreadable runtime version {version!r}")
    return version["specVersion"]


def _block_events(rpc: _Rpc, decoders: _Decoders, start: int, end: int) -> Iterator[Event]:
    """The tracked events of blocks start..end, each decoded with its parent's runtime."""
    hashes = dict(zip(range(start - 1, end + 1),
                      rpc.batch([("chain_getBlockHash", [n]) for n in range(start - 1, end + 1)])))
    if not all(isinstance(h, str) for h in hashes.values()):
        raise MateriosRpcError(f"the node has no hash for a block in {start - 1}..{end}")
    lo, hi = (_spec(v) for v in rpc.batch([("state_getRuntimeVersion", [hashes[start - 1]]),
                                           ("state_getRuntimeVersion", [hashes[end - 1]])]))
    if lo == hi:
        parent_spec = {n: lo for n in range(start - 1, end)}
    else:
        versions = rpc.batch([("state_getRuntimeVersion", [hashes[n]]) for n in range(start - 1, end)])
        parent_spec = {n: _spec(v) for n, v in zip(range(start - 1, end), versions)}
    stored = rpc.batch([("state_getStorage", [EVENTS_KEY, hashes[n]]) for n in range(start, end + 1)])
    for n, raw in zip(range(start, end + 1), stored):
        if not isinstance(raw, str):
            raise MateriosRpcError(f"block {n} has no events stored")
        for index, record in enumerate(decoders.events(parent_spec[n - 1], hashes[n - 1], raw)):
            if record["module_id"] == PALLET and record["event_id"] in TRACKED:
                yield Event(n, index, record.get("extrinsic_idx"), record["event_id"], record["attributes"])


def read_chain(url: str, scan_from: int, first: int, last: int) -> ChainSlice:
    """The tracked events of blocks ``scan_from``..``last`` from the node at
    ``url`` (http or https), with the hashes of ``first`` and ``last``."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"the Materios RPC URL must be http or https, not {url!r}")
    if not 1 <= scan_from <= first <= last:
        raise ValueError(f"blocks must satisfy 1 <= scan_from <= first <= last: {scan_from}, {first}, {last}")
    rpc = _Rpc(url)
    genesis, finalized = rpc.batch([("chain_getBlockHash", [0]), ("chain_getFinalizedHead", [])])
    header = rpc.call("chain_getHeader", [finalized])
    head = int(header["number"], 16)
    if last > head:
        raise MateriosRpcError(f"block {last} is past the node's finalized head {head}")
    decoders = _Decoders(rpc)
    events: list[Event] = []
    for start in range(scan_from, last + 1, BATCH):
        events.extend(_block_events(rpc, decoders, start, min(start + BATCH - 1, last)))
    first_hash, last_hash = rpc.batch([("chain_getBlockHash", [first]), ("chain_getBlockHash", [last])])
    return ChainSlice(genesis, scan_from, first, last, first_hash, last_hash, tuple(events))
