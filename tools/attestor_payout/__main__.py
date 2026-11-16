"""Stage A attestor payouts, run in this order each month:

    message   the text an attestor signs with CIP-30 signData at admission
    registry  verify every registration in the registry
    build     the period's statement, from an archive Materios node
    check     the statement rebuilt from a second archive node FPS does not
              run; writes the check record only if every field matches
    tx        the unsigned payout transaction from the float, read through
              Koios, for a statement whose check record matches it

Every output file is created, never overwritten. Nothing here signs or
submits; the two float keys sign the transaction elsewhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pycardano import Address

from tools.api_clients import BlockfrostUnavailable, KoiosClient
from tools.attestor_payout.materios_events import MateriosRpcError, read_chain
from tools.attestor_payout.payout_tx import (
    PayoutTxError,
    build_payout_tx,
    fee_params,
    float_address,
    float_utxos,
)
from tools.attestor_payout.registration import (
    RegistrationError,
    load_registry,
    parse_attestor,
    registration_message,
)
from tools.attestor_payout.statement import (
    ConfigError,
    PeriodConfig,
    StatementError,
    anchor_metadata,
    build_statement,
    check_statement,
    load_period_config,
    statement_digest,
)
from tools.config import KOIOS_BASE_URLS
from tools.credential_denylist import DEFAULT_DENYLIST_PATH, DenyListError, load_denylist


def _write_new(path: str, doc: dict) -> None:
    with open(path, "x") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")


def _registry(config: PeriodConfig, path: str) -> dict:
    return load_registry(path, config.chain, config.cardano_network, load_denylist(DEFAULT_DENYLIST_PATH))


def _chain(config: PeriodConfig, rpc: str):
    return read_chain(rpc, config.scan_from, config.first_block, config.last_block)


def _message(config: PeriodConfig, args) -> None:
    message = registration_message(config.chain, parse_attestor(args.attestor), Address.from_primitive(args.address).encode())
    print(message.decode())
    print(f"signData payload (hex): {message.hex()}")


def _list_registry(config: PeriodConfig, args) -> None:
    for attestor, address in sorted(_registry(config, args.registry).items()):
        print(f"0x{attestor.hex()} {address.encode()}")


def _build(config: PeriodConfig, args) -> None:
    statement = build_statement(config, _chain(config, args.rpc), _registry(config, args.registry))
    _write_new(args.out, statement)
    print(f"statement {statement_digest(statement)}: {len(statement['lines'])} lines, "
          f"{statement['total']} cMATRA base units, root {statement['merkle']['root']}")


def _check(config: PeriodConfig, args) -> None:
    statement = json.loads(Path(args.statement).read_text())
    chain = _chain(config, args.rpc)
    check_statement(statement, config, chain, _registry(config, args.registry))
    _write_new(args.out, {"statement": statement_digest(statement), "genesis": chain.genesis,
                          "first_hash": chain.first_hash, "last_hash": chain.last_hash})
    print(f"statement {statement_digest(statement)} matches the second source")


def _tx(config: PeriodConfig, args) -> None:
    statement = json.loads(Path(args.statement).read_text())
    if json.loads(Path(args.check).read_text()).get("statement") != statement_digest(statement):
        raise StatementError(f"{args.check} is not a passed check of {args.statement}")
    koios = KoiosClient(base_url=KOIOS_BASE_URLS[config.network])
    address = float_address(config.float_keys, config.cardano_network)
    tx = build_payout_tx(
        statement, config, float_utxos(koios.get_address_utxos(address.encode()), address),
        fee_params(koios.get_cli_protocol_params()), load_denylist(DEFAULT_DENYLIST_PATH),
    )
    _write_new(args.out, {"type": "Tx ConwayEra",
                          "description": f"Unwitnessed attestor payout {config.period}",
                          "cborHex": tx.to_cbor_hex()})
    print(f"unsigned payout {tx.id}: {len(tx.transaction_body.outputs) - 1} payees, fee "
          f"{tx.transaction_body.fee} lovelace, label 8746 {json.dumps(anchor_metadata(statement), sort_keys=True)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.attestor_payout", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, options in (
        ("message", _message, ("attestor", "address")),
        ("registry", _list_registry, ("registry",)),
        ("build", _build, ("registry", "rpc", "out")),
        ("check", _check, ("registry", "rpc", "statement", "out")),
        ("tx", _tx, ("statement", "check", "out")),
    ):
        command = commands.add_parser(name)
        command.set_defaults(handler=handler)
        for option in ("config", *options):
            command.add_argument(f"--{option}", required=True)
    args = parser.parse_args(argv)
    try:
        if getattr(args, "out", None) and Path(args.out).exists():
            raise FileExistsError(f"{args.out} exists")
        args.handler(load_period_config(args.config), args)
    except (ConfigError, StatementError, RegistrationError, PayoutTxError, MateriosRpcError,
            BlockfrostUnavailable, DenyListError, OSError, json.JSONDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
