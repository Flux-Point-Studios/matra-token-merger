"""Tests for tools.pool_migration — moving the surrender pool to fresh admin keys."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cbor2
import pytest
from cbor2 import CBORTag
from nacl.signing import SigningKey

from services.cosign_policy import VOID_INLINE_DATUM, _witness_fields, decode, signature_verifies, split_tx
from tools.pool_migration import (
    MigrationRejected,
    _key_witness,
    assemble,
    build_migration_tx,
    check_migration_tx,
    encode_tx,
    language_views,
    load_plan,
    load_snapshot,
    main,
    migration_parts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pool_migration"
POOL_TX = bytes.fromhex((FIXTURES / "pool_parent_d3a1e632.tx.hex").read_text().strip())
POOL_REF = "d3a1e632424794bed36d7062b4c09c5fe547fa7a1addbd2d45f9b84df773c733#1"
POOL_CMATRA = 301_731_979_205_950
POOL_LOVELACE = 1_500_000
OLD_SCRIPT_HEX = (FIXTURES / "pool_e1a181b2.script.hex").read_text().strip()
LIVE_SCRIPT = bytes.fromhex(OLD_SCRIPT_HEX)
PARAMS = json.loads((FIXTURES / "protocol_parameters_epoch665.json").read_text())
CMATRA_UNIT = "7ff33a5565393dc47b48ac47becc12d92c9952e724e8446dfb6adc66634d41545241"
OLD_ADMINS = ["f1eee828ae800a5bad6f3913603e20531162cdf44c5e0ffa49b6a936",
              "77fdb621ad5f926257ce1a2f845fc946ae3d5a683ac89128bad3410d"]
OLD_DEADLINE_MS = 1_795_910_400_000
OLD_DEADLINE_SLOT = 204_344_109
NEW_DEADLINE_MS = 1_798_502_400_000
NEW_ADMINS = ["737003392591a2c07d68c0e86b42fc31fc02f7780ca292e92bf9e122",
              "8364ef054c467b32ac627c73acac7acce616c3f02eb5915332751ac7"]
OTHER_ADMINS = ["bcfa44669f99b0d61f5c19ed9d9d6476db94e1cd009e49637895607a",
                "28db8ac484c5ae547fa58358a0cb77a6aebb353468e4110c773d322d"]
# onchain/claim_validator/apply_params.sh under aiken v1.1.21, which gives
# e1a181b2 for OLD_ADMINS and OLD_DEADLINE_MS
NEW_SCRIPT_HASH = "21b85d9b9b2aa12acb4fac95c1b36d76557a8ae4b2e4f7fe26a23bf1"
NEW_POOL_ADDRESS = "addr1wysmshvmnv42z2ktf7kftsdnd4m92752ujewfal7y63rhug0mc9d7"
SWAPPED_HASH = "abbb243e710e8f1f83d9e9fb800cdedacd11c05f97140285b0a70829"
SWAPPED_ADDRESS = "addr1wx4mkfp7wy8g78urm85lhqqvmmdv6ywqt7t3gq59kznss2g4usdjl"
DAY_LATER_HASH = "f26ee4c7d62bdd8d61013d0ebe20c389b7a946d51a67e9b165697c4e"
DAY_LATER_ADDRESS = "addr1w8exaex86c4amrtpqy7sa03qcwym022x65dx06d3v45hcnswz9mpg"
OTHER_ADMINS_HASH = "d521774fef7418624b5833d55d893459f1578ce915a5d77f8c1cfff0"
OTHER_ADMINS_ADDRESS = "addr1w82jza60aa6pscjttqea2hvfx3vlz4uvay26t4ml3sw0luqwpe93x"
DAY_MS = 86_400_000
TIP_SLOT = 202_188_020
FEE_LOVELACE = 10_000_000


def blake2b_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def blake2b_224(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=28).digest()


def parent_tx(outputs: list, seed: bytes = b"\x11") -> bytes:
    """A producing transaction with these outputs."""
    return cbor2.dumps([{0: [[seed * 32, 0]], 1: outputs, 2: 200_000}, {}, True, None])


def ref_of(tx: bytes, index: int) -> str:
    return f"{blake2b_256(split_tx(tx)[0]).hex()}#{index}"


def fee_parent(pkh: str, coin: int = FEE_LOVELACE, seed: bytes = b"\x11") -> bytes:
    return parent_tx([{0: b"\x61" + bytes.fromhex(pkh), 1: coin}], seed)


def snapshot_doc(pool_tx: bytes = POOL_TX, pool_ref: str = POOL_REF,
                 fee_tx: bytes | None = None, fee_index: int = 0) -> dict:
    fee_tx = fee_tx if fee_tx is not None else fee_parent(NEW_ADMINS[0])
    return {
        "network": "mainnet",
        "tip": {"slot": TIP_SLOT},
        "inputs": {
            "pool": {"ref": pool_ref, "producing_tx": pool_tx.hex(), "consumed_by": None},
            "fee": {"ref": ref_of(fee_tx, fee_index), "producing_tx": fee_tx.hex(), "consumed_by": None},
        },
        "protocol_parameters": PARAMS,
    }


def plan_doc(snap: dict, **overrides) -> dict:
    doc = {
        "network": "mainnet",
        "pool_ref": snap["inputs"]["pool"]["ref"],
        "fee_ref": snap["inputs"]["fee"]["ref"],
        "cmatra_unit": CMATRA_UNIT,
        "old_script_hex": OLD_SCRIPT_HEX,
        "old_admins": OLD_ADMINS,
        "old_deadline_ms": OLD_DEADLINE_MS,
        "new_admins": NEW_ADMINS,
        "new_deadline_ms": NEW_DEADLINE_MS,
        "new_script_hash": NEW_SCRIPT_HASH,
        "move": "all",
        "validity_start": TIP_SLOT - 60,
        "ttl": TIP_SLOT + 3 * 3600,
        "ex_units": [2_000_000, 900_000_000],
    }
    doc.update(overrides)
    return doc


def world(**overrides):
    snap_doc = snapshot_doc()
    return load_snapshot(snap_doc), load_plan(plan_doc(snap_doc, **overrides))


def rejected(code: str):
    return pytest.raises(MigrationRejected, match=rf"^{code}\b")


# ---------------------------------------------------------------------------
# A synthetic pool whose admin keys the tests hold, for signing
# ---------------------------------------------------------------------------


class Keys:
    def __init__(self, n: int) -> None:
        self.signing = [SigningKey.generate() for _ in range(n)]

    def pkh(self, i: int) -> str:
        return blake2b_224(bytes(self.signing[i].verify_key)).hex()

    def witness(self, i: int, txid: bytes) -> bytes:
        """What cardano-cli 11's `transaction witness` writes: [vkey, signature]."""
        key = self.signing[i]
        return cbor2.dumps([bytes(key.verify_key), key.sign(txid).signature])


def applied(admins: list[str], deadline_ms: int) -> bytes:
    """The live pool script with its three parameter constants swapped for
    these, written apart from the tool's own derivation so each checks the
    other; TestDestination pins the tool's to aiken."""
    script = LIVE_SCRIPT
    for old, new in zip(OLD_ADMINS, admins, strict=True):
        script = script.replace(bytes.fromhex("581c" + old), bytes.fromhex("581c" + new))
    return script.replace(b"\x1b" + OLD_DEADLINE_MS.to_bytes(8, "big"), b"\x1b" + deadline_ms.to_bytes(8, "big"))


def pool_docs(old_script: bytes, fee_pkh: str = NEW_ADMINS[0], **overrides) -> tuple[dict, dict]:
    """A pool of POOL_CMATRA locked by ``old_script``, and a plan to move it."""
    pool_tx = parent_tx([
        {0: b"\x71" + blake2b_224(b"\x03" + old_script),
         1: [POOL_LOVELACE, {bytes.fromhex(CMATRA_UNIT[:56]): {bytes.fromhex(CMATRA_UNIT[56:]): POOL_CMATRA}}],
         2: [1, CBORTag(24, bytes.fromhex("d87980"))]},
    ], seed=b"\x22")
    snap_doc = snapshot_doc(pool_tx=pool_tx, pool_ref=ref_of(pool_tx, 0), fee_tx=fee_parent(fee_pkh))
    return snap_doc, plan_doc(snap_doc, old_script_hex=old_script.hex(), **overrides)


def signing_docs(move="all"):
    """A pool of the claim validator applied to old admins 0-1, moving to it
    applied to new admins 2-3; new admin 2's address pays the fee."""
    keys = Keys(4)
    old, new = [keys.pkh(0), keys.pkh(1)], [keys.pkh(2), keys.pkh(3)]
    new_hash = blake2b_224(b"\x03" + applied(new, NEW_DEADLINE_MS)).hex()
    snap_doc, plan = pool_docs(applied(old, OLD_DEADLINE_MS), fee_pkh=keys.pkh(2), old_admins=old, new_admins=new,
                               new_script_hash=new_hash, move=move)
    return keys, snap_doc, plan


def signing_world(move="all"):
    keys, snap_doc, plan = signing_docs(move)
    return keys, load_snapshot(snap_doc), load_plan(plan)


def outputs_of(tx: bytes) -> list:
    return decode(tx)[0][1]


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


class TestBuild:
    def test_full_move_passes_its_check(self):
        snap, plan = world()
        tx = build_migration_tx(snap, plan)
        report = check_migration_tx(tx, snap, plan)
        assert report["moved_cmatra"] == POOL_CMATRA
        assert report["left_at_old_pool"] == 0
        assert report["new_pool_address"] == NEW_POOL_ADDRESS
        assert report["txid"] == blake2b_256(split_tx(tx)[0]).hex()
        assert 0 < report["fee"] < 1_000_000

    def test_new_pool_holds_the_pool_value_under_the_void_inline_datum(self):
        snap, plan = world()
        new_pool = outputs_of(build_migration_tx(snap, plan))[0]
        assert new_pool[0] == b"\x71" + bytes.fromhex(NEW_SCRIPT_HASH)
        assert new_pool[1] == [POOL_LOVELACE, {bytes.fromhex(CMATRA_UNIT[:56]):
                                               {bytes.fromhex(CMATRA_UNIT[56:]): POOL_CMATRA}}]
        assert new_pool[2] == VOID_INLINE_DATUM

    def test_build_is_deterministic_from_its_documents(self):
        snap_doc = snapshot_doc()
        plan = plan_doc(snap_doc)
        first = build_migration_tx(load_snapshot(snap_doc), load_plan(plan))
        again = build_migration_tx(load_snapshot(json.loads(json.dumps(snap_doc))),
                                   load_plan(json.loads(json.dumps(plan))))
        assert first == again

    def test_slice_leaves_the_rest_under_the_old_pool(self):
        snap, plan = world(move=1_000_000)
        tx = build_migration_tx(snap, plan)
        report = check_migration_tx(tx, snap, plan)
        assert report["moved_cmatra"] == 1_000_000
        assert report["left_at_old_pool"] == POOL_CMATRA - 1_000_000
        new_pool, old_pool, change = outputs_of(tx)
        assert new_pool[2] == VOID_INLINE_DATUM and old_pool[2] == VOID_INLINE_DATUM
        assert old_pool[1][0] == POOL_LOVELACE
        assert change[0] == b"\x61" + bytes.fromhex(NEW_ADMINS[0])

    @pytest.mark.parametrize("seed", [b"\x00", b"\xff", b"\x5a"])
    def test_redeemer_points_at_the_pool_input_in_ledger_order(self, seed):
        snap_doc = snapshot_doc(fee_tx=fee_parent(NEW_ADMINS[0], seed=seed))
        snap, plan = load_snapshot(snap_doc), load_plan(plan_doc(snap_doc))
        tx = build_migration_tx(snap, plan)
        body = decode(tx)[0]
        inputs = [tuple(i) for i in body[0].value]
        assert inputs == sorted(inputs)
        (tag, index), = decode(_witness_fields(split_tx(tx)[1])[5]).keys()
        assert (tag, inputs[index]) == (0, (bytes.fromhex(POOL_REF[:64]), 1))
        check_migration_tx(tx, snap, plan)

    def test_language_views_reproduce_a_mainnet_script_data_hash(self):
        body, witnesses, _ = split_tx(POOL_TX)
        redeemers = _witness_fields(witnesses)[5]
        cost_model = PARAMS["cost_models_raw"]["PlutusV3"]
        assert blake2b_256(redeemers + language_views(cost_model)) == decode(body)[11]

    def test_required_signers_are_both_old_and_both_new_admins(self):
        snap, plan = world()
        signers = decode(build_migration_tx(snap, plan))[0][14].value
        assert sorted(s.hex() for s in signers) == sorted(OLD_ADMINS + NEW_ADMINS)

    def test_the_report_names_what_each_signer_compares(self):
        snap, plan = world()
        report = check_migration_tx(build_migration_tx(snap, plan), snap, plan)
        assert report["old_admins"] == OLD_ADMINS
        assert report["new_admins"] == NEW_ADMINS
        assert report["new_deadline_ms"] == NEW_DEADLINE_MS
        assert report["new_deadline_utc"] == "2026-12-29T00:00:00+00:00"


# ---------------------------------------------------------------------------
# The destination: the old script applied to the new admins and deadline
# ---------------------------------------------------------------------------


OTHER_SCRIPT_HASH = blake2b_224(b"\x03" + bytes.fromhex("450101002499")).hex()


class TestDestination:
    @pytest.mark.parametrize("admins, deadline, script_hash, address", [
        (NEW_ADMINS, NEW_DEADLINE_MS, NEW_SCRIPT_HASH, NEW_POOL_ADDRESS),
        (NEW_ADMINS[::-1], NEW_DEADLINE_MS, SWAPPED_HASH, SWAPPED_ADDRESS),
        (NEW_ADMINS, NEW_DEADLINE_MS + DAY_MS, DAY_LATER_HASH, DAY_LATER_ADDRESS),
        (OTHER_ADMINS, NEW_DEADLINE_MS, OTHER_ADMINS_HASH, OTHER_ADMINS_ADDRESS),
    ], ids=["new_admins", "swapped", "day_later", "other_admins"])
    def test_is_what_apply_params_gives_for_the_new_admins_and_deadline(self, admins, deadline, script_hash,
                                                                         address):
        snap, plan = world(new_admins=admins, new_deadline_ms=deadline, new_script_hash=script_hash)
        tx = build_migration_tx(snap, plan)
        report = check_migration_tx(tx, snap, plan)
        assert (report["new_script_hash"], report["new_pool_address"]) == (script_hash, address)
        assert outputs_of(tx)[0][0] == b"\x71" + bytes.fromhex(script_hash)

    @pytest.mark.parametrize("admins, deadline, script_hash", [
        (NEW_ADMINS, NEW_DEADLINE_MS, OTHER_SCRIPT_HASH),        # another script altogether
        (NEW_ADMINS, NEW_DEADLINE_MS, OTHER_ADMINS_HASH),        # the validator for other admins
        (NEW_ADMINS, NEW_DEADLINE_MS, SWAPPED_HASH),             # the new admins in the other order
        (NEW_ADMINS, NEW_DEADLINE_MS, DAY_LATER_HASH),           # another deadline
        (OTHER_ADMINS, NEW_DEADLINE_MS, NEW_SCRIPT_HASH),        # signers the destination does not name
        (NEW_ADMINS, NEW_DEADLINE_MS + DAY_MS, NEW_SCRIPT_HASH),  # a deadline it does not carry
    ], ids=["other_script", "other_admins", "swapped", "day_later", "signers_not_named", "deadline_not_carried"])
    def test_refuses_a_script_hash_that_is_not_the_old_script_reapplied(self, admins, deadline, script_hash):
        snap, plan = world(new_admins=admins, new_deadline_ms=deadline, new_script_hash=script_hash)
        with rejected("new_script"):
            build_migration_tx(snap, plan)

    def test_check_refuses_a_transaction_paying_such_a_destination(self):
        snap, plan = world()
        tampered = load_plan(plan_doc(snapshot_doc(), new_script_hash=OTHER_ADMINS_HASH))

        def mutate(body, _):
            body[1][0][0] = b"\x71" + bytes.fromhex(OTHER_ADMINS_HASH)
        with rejected("new_script"):
            check_migration_tx(mutated(snap, plan, mutate), snap, tampered)

    @pytest.mark.parametrize("old_deadline", [OLD_DEADLINE_MS - DAY_MS, OLD_DEADLINE_MS + 1])
    def test_refuses_an_old_deadline_the_old_script_does_not_carry(self, old_deadline):
        snap, plan = world(old_deadline_ms=old_deadline)
        with rejected("old_deadline"):
            build_migration_tx(snap, plan)

    @pytest.mark.parametrize("repeated, code", [
        (bytes.fromhex("581c" + OLD_ADMINS[0]), "old_admins"),
        (bytes.fromhex("581c" + OLD_ADMINS[1]), "old_admins"),
        (b"\x1b" + OLD_DEADLINE_MS.to_bytes(8, "big"), "old_deadline"),
    ], ids=["admin_1", "admin_2", "deadline"])
    def test_refuses_an_old_parameter_that_occurs_twice_in_the_old_script(self, repeated, code):
        snap_doc, plan = pool_docs(LIVE_SCRIPT + repeated)
        with rejected(code):
            build_migration_tx(load_snapshot(snap_doc), load_plan(plan))

    @pytest.mark.parametrize("constant, other_header, code", [
        (bytes.fromhex("581c" + OLD_ADMINS[0]), b"\x58\x1d", "old_admins"),
        (b"\x1b" + OLD_DEADLINE_MS.to_bytes(8, "big"), b"\x1a", "old_deadline"),
    ], ids=["admin", "deadline"])
    def test_an_old_value_under_another_data_header_is_not_a_parameter(self, constant, other_header, code):
        snap_doc, plan = pool_docs(LIVE_SCRIPT.replace(constant, other_header + constant[len(other_header):]))
        with rejected(code):
            build_migration_tx(load_snapshot(snap_doc), load_plan(plan))

    @pytest.mark.parametrize("field, value", [
        ("new_deadline_ms", 10**13),
        ("new_deadline_ms", 2**64),
        ("old_deadline_ms", 10**12 - 1),
    ])
    def test_refuses_a_deadline_that_apply_params_would(self, field, value):
        with rejected(field.removesuffix("_ms")):
            load_plan(plan_doc(snapshot_doc(), **{field: value}))


# ---------------------------------------------------------------------------
# The check every admin runs before witnessing
# ---------------------------------------------------------------------------


def mutated(snap, plan, mutate) -> bytes:
    body, witnesses = migration_parts(snap, plan)
    tail = mutate(body, witnesses)
    return encode_tx(body, witnesses, *(tail or ()))


def change_index(body) -> int:
    return len(body[1]) - 1


class TestCheckRefuses:
    def test_another_destination_script(self):
        snap, plan = world()

        def mutate(body, _):
            body[1][0][0] = b"\x71" + b"\xab" * 28
        with rejected("output_address"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_the_old_pool_as_destination_of_a_full_move(self):
        snap, plan = world()

        def mutate(body, _):
            body[1][0][0] = b"\x71" + bytes.fromhex("e1a181b2f8b74be55e6160a7a82764b9b174e04c37c4ed49d349a3dd")
        with rejected("new_pool_output"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    @pytest.mark.parametrize("datum", [None, [0, b"\x00" * 32], [1, CBORTag(24, b"\x00")]])
    def test_a_new_pool_output_without_the_void_inline_datum(self, datum):
        snap, plan = world()

        def mutate(body, _):
            if datum is None:
                del body[1][0][2]
            else:
                body[1][0][2] = datum
        with rejected("pool_datum"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_an_old_pool_continuation_without_the_datum(self):
        snap, plan = world(move=1_000_000)

        def mutate(body, _):
            del body[1][1][2]
        with rejected("pool_datum"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_cmatra_diverted_to_the_change(self):
        snap, plan = world()

        def mutate(body, _):
            policy, name = bytes.fromhex(CMATRA_UNIT[:56]), bytes.fromhex(CMATRA_UNIT[56:])
            body[1][0][1][1][policy][name] -= 1
            change = body[1][change_index(body)]
            change[1] = [change[1], {policy: {name: 1}}]
        with rejected("change_output"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_cmatra_short_of_the_plan(self):
        snap, plan = world(move=1_000_000)

        def mutate(body, _):
            policy, name = bytes.fromhex(CMATRA_UNIT[:56]), bytes.fromhex(CMATRA_UNIT[56:])
            body[1][0][1][1][policy][name] -= 1
            body[1][1][1][1][policy][name] += 1
        with rejected("moved_cmatra"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_an_output_to_anyone_else(self):
        snap, plan = world()

        def mutate(body, _):
            change = body[1][change_index(body)]
            change[1] -= 2_000_000
            body[1].append({0: b"\x61" + b"\xcd" * 28, 1: 2_000_000})
        with rejected("output_address"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_fee_of_one_ada(self):
        snap, plan = world()

        def mutate(body, _):
            body[1][change_index(body)][1] -= 1_000_000 - body[2]
            body[2] = 1_000_000
        with rejected("fee"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_fee_below_the_minimum(self):
        snap, plan = world()

        def mutate(body, _):
            body[2] -= 20_000
            body[1][change_index(body)][1] += 20_000
        with rejected("fee_below_min"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_unbalanced_value(self):
        snap, plan = world()

        def mutate(body, _):
            body[1][change_index(body)][1] += 1
        with rejected("unbalanced"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    @pytest.mark.parametrize("key, value", [
        (9, {b"\x01" * 28: {b"": 1}}),          # mint
        (4, [[0, [0, b"\x02" * 28]]]),          # certificates
        (5, {b"\xe1" + b"\x03" * 28: 0}),        # withdrawals
        (18, [[b"\x04" * 32, 0]]),               # reference inputs
        (7, b"\x05" * 32),                       # auxiliary data hash
        (15, 1),                                 # network id
    ])
    def test_any_other_body_field(self, key, value):
        snap, plan = world()

        def mutate(body, _):
            body[key] = value
        with rejected("forbidden_body_field"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    @pytest.mark.parametrize("tail", [(False, None), (True, {0: {1: 2}})])
    def test_invalid_flag_or_auxiliary_data(self, tail):
        snap, plan = world()
        with rejected("tx_tail"):
            check_migration_tx(mutated(snap, plan, lambda body, _: tail), snap, plan)

    @pytest.mark.parametrize("signers", [
        OLD_ADMINS + NEW_ADMINS[:1],
        OLD_ADMINS[1:] + NEW_ADMINS,
        OLD_ADMINS + NEW_ADMINS + ["ee" * 28],
    ])
    def test_required_signers_other_than_the_four_admins(self, signers):
        snap, plan = world()

        def mutate(body, _):
            body[14] = CBORTag(258, [bytes.fromhex(s) for s in signers])
        with rejected("required_signers"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_ttl_past_the_old_deadline(self):
        snap, plan = world()

        def mutate(body, _):
            body[3] = OLD_DEADLINE_SLOT + 1
        with rejected("after_deadline"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_validity_window_longer_than_six_hours(self):
        snap, plan = world()

        def mutate(body, _):
            body[8] = body[3] - 6 * 3600 - 1
        with rejected("validity_window"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_an_admin_withdraw_redeemer(self):
        snap, plan = world()

        def mutate(_, witnesses):
            key = next(iter(witnesses[5]))
            witnesses[5][key][0] = CBORTag(122, [])
        with rejected("redeemer"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_redeemer_on_the_fee_input(self):
        snap, plan = world()

        def mutate(_, witnesses):
            (tag, index), = witnesses[5].keys()
            witnesses[5] = {(tag, 1 - index): witnesses[5][(tag, index)]}
        with rejected("redeemer"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_redeemer_budget_unlike_the_plan(self):
        snap, plan = world()

        def mutate(_, witnesses):
            key = next(iter(witnesses[5]))
            witnesses[5][key][1] = [1, 1]
        with rejected("redeemer"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_another_script_in_the_witness_set(self):
        snap, plan = world()

        def mutate(_, witnesses):
            witnesses[7] = CBORTag(258, [cbor2.dumps(b"\x00" * 40)])
        with rejected("script"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_another_witness_field(self):
        snap, plan = world()

        def mutate(_, witnesses):
            witnesses[4] = CBORTag(258, [CBORTag(121, [])])
        with rejected("witness_fields"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_script_data_hash_that_does_not_cover_the_redeemers(self):
        snap, plan = world()

        def mutate(body, _):
            body[11] = b"\x00" * 32
        with rejected("script_data_hash"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_third_input(self):
        snap, plan = world()

        def mutate(body, _):
            body[0] = CBORTag(258, sorted(body[0].value + [[b"\x06" * 32, 0]]))
        with rejected("inputs"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_collateral_other_than_the_fee_input(self):
        snap, plan = world()

        def mutate(body, _):
            body[13] = CBORTag(258, [[bytes.fromhex(POOL_REF[:64]), 1]])
        with rejected("collateral"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_collateral_below_the_protocol_share_of_the_fee(self):
        snap, plan = world()

        def mutate(body, _):
            body[16][1] += body[17] - body[2]
            body[17] = body[2]
        with rejected("collateral"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_collateral_return_below_its_minimum_ada(self):
        snap_doc = snapshot_doc(fee_tx=fee_parent(NEW_ADMINS[0], coin=5_500_000))
        snap, plan = load_snapshot(snap_doc), load_plan(plan_doc(snap_doc))

        def mutate(body, _):
            body[17] = 4_700_000
            body[16][1] = 800_000
        with rejected("collateral"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_collateral_return_to_anyone_else(self):
        snap, plan = world()

        def mutate(body, _):
            body[16][0] = b"\x61" + b"\xcd" * 28
        with rejected("collateral"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_missing_body_field(self):
        snap, plan = world()

        def mutate(body, _):
            del body[16]
        with rejected("body_shape"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_validity_interval_other_than_the_plans(self):
        snap, plan = world()

        def mutate(body, _):
            body[8] += 1
            body[3] += 1
        with rejected("validity_window"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_ttl_that_is_not_a_slot(self):
        snap, plan = world()

        def mutate(body, _):
            body[3] = b"\x01"
        with rejected("validity_window"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_a_reference_script_on_the_new_pool(self):
        snap, plan = world()

        def mutate(body, _):
            body[1][0][3] = CBORTag(24, cbor2.dumps([3, b"\x00"]))
        with rejected("output_script_ref"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_another_token_in_the_new_pool(self):
        snap, plan = world()

        def mutate(body, _):
            body[1][0][1][1][b"\x09" * 28] = {b"x": 1}
        with rejected("pool_assets"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_pool_lovelace_moved_to_the_change(self):
        snap, plan = world()

        def mutate(body, _):
            body[1][0][1][0] -= 1
            body[1][change_index(body)][1] += 1
        with rejected("pool_lovelace"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)

    def test_an_output_array_of_indefinite_length(self):
        snap, plan = world()
        body, witnesses = migration_parts(snap, plan)
        tx = encode_tx(body, witnesses)
        body_raw = split_tx(tx)[0]
        outputs_raw = cbor2.dumps(body[1])
        indefinite = b"\x9f" + outputs_raw[1:] + b"\xff"
        patched = tx.replace(body_raw, body_raw.replace(outputs_raw, indefinite, 1), 1)
        with rejected("cbor"):
            check_migration_tx(patched, snap, plan)

    @pytest.mark.parametrize("tx", [b"\xff", b"\x84\x01", b"\x83\x01\x02\x03"],
                             ids=["not_cbor", "truncated", "three_elements"])
    def test_bytes_that_are_not_a_transaction(self, tx):
        snap, plan = world()
        with rejected("cbor"):
            check_migration_tx(tx, snap, plan)

    def test_a_new_pool_output_below_its_minimum_ada(self):
        snap, plan = world(move=1_000_000)

        def mutate(body, _):
            short = body[1][0][1][0] - 500_000
            body[1][change_index(body)][1] += body[1][0][1][0] - short
            body[1][0][1][0] = short
        with rejected("min_ada"):
            check_migration_tx(mutated(snap, plan, mutate), snap, plan)


# ---------------------------------------------------------------------------
# Plans and snapshots
# ---------------------------------------------------------------------------


class TestPlanRefuses:
    @pytest.mark.parametrize("start, ttl", [
        (TIP_SLOT, TIP_SLOT),
        (TIP_SLOT, TIP_SLOT - 1),
        (TIP_SLOT, TIP_SLOT + 6 * 3600 + 1),
        (-1, TIP_SLOT),
    ])
    def test_an_empty_or_long_validity_window(self, start, ttl):
        snap_doc = snapshot_doc()
        with rejected("validity_window"):
            load_plan(plan_doc(snap_doc, validity_start=start, ttl=ttl))

    def test_the_last_ttl_is_one_slot_before_the_deadline(self):
        snap_doc = snapshot_doc()
        last = load_plan(plan_doc(snap_doc, validity_start=OLD_DEADLINE_SLOT - 3600, ttl=OLD_DEADLINE_SLOT - 1))
        build_migration_tx(load_snapshot(snap_doc), last)
        with rejected("after_deadline"):
            load_plan(plan_doc(snap_doc, validity_start=OLD_DEADLINE_SLOT - 3600, ttl=OLD_DEADLINE_SLOT))

    def test_an_unknown_network(self):
        snap_doc = snapshot_doc()
        with rejected("plan"):
            load_plan(plan_doc(snap_doc, network="testnet"))

    @pytest.mark.parametrize("move", ["half", True, 1.5])
    def test_a_move_that_is_not_base_units(self, move):
        snap_doc = snapshot_doc()
        with rejected("move"):
            load_plan(plan_doc(snap_doc, move=move))

    def test_an_old_script_that_is_not_the_pools(self):
        snap, plan = world(old_script_hex=cbor2.dumps(b"\x00" + bytes.fromhex("".join(OLD_ADMINS))).hex())
        with rejected("old_script"):
            build_migration_tx(snap, plan)

    def test_a_budget_above_the_transaction_limit(self):
        snap, plan = world(ex_units=[16_500_001, 900_000_000])
        with rejected("ex_units"):
            build_migration_tx(snap, plan)

    def test_a_new_admin_that_is_an_old_admin(self):
        snap_doc = snapshot_doc()
        with rejected("key_reuse"):
            load_plan(plan_doc(snap_doc, new_admins=[OLD_ADMINS[1], NEW_ADMINS[1]]))

    def test_two_equal_new_admins(self):
        snap_doc = snapshot_doc()
        with rejected("new_admins"):
            load_plan(plan_doc(snap_doc, new_admins=[NEW_ADMINS[0], NEW_ADMINS[0]]))

    def test_the_old_script_as_new_script(self):
        snap_doc = snapshot_doc()
        with rejected("new_script"):
            load_plan(plan_doc(snap_doc, new_script_hash="e1a181b2f8b74be55e6160a7a82764b9b174e04c37c4ed49d349a3dd"))

    def test_an_earlier_new_deadline(self):
        snap_doc = snapshot_doc()
        with rejected("new_deadline"):
            load_plan(plan_doc(snap_doc, new_deadline_ms=OLD_DEADLINE_MS - 1))

    def test_a_ttl_past_the_old_deadline(self):
        snap_doc = snapshot_doc()
        with rejected("after_deadline"):
            load_plan(plan_doc(snap_doc, ttl=OLD_DEADLINE_SLOT + 1))

    def test_old_admins_absent_from_the_old_script(self):
        snap, plan = world(old_admins=[NEW_ADMINS[0], OLD_ADMINS[1]],
                           new_admins=["aa" * 28, NEW_ADMINS[1]])
        with rejected("old_admins"):
            build_migration_tx(snap, plan)

    @pytest.mark.parametrize("move", [0, POOL_CMATRA + 1, -5])
    def test_a_move_outside_the_pool(self, move):
        snap, plan = world(move=move)
        with rejected("move"):
            build_migration_tx(snap, plan)

    def test_a_plan_for_another_pool_input(self):
        snap_doc = snapshot_doc()
        plan = load_plan(plan_doc(snap_doc, pool_ref=POOL_REF[:-1] + "0"))
        with rejected("snapshot"):
            build_migration_tx(load_snapshot(snap_doc), plan)


class TestBuildRefuses:
    @pytest.mark.parametrize("coin", [
        1_000_000,   # the change falls below its minimum ADA
        1_300_000,   # the change suffices, the collateral return does not
    ])
    def test_a_fee_input_too_small_to_pay(self, coin):
        snap_doc = snapshot_doc(fee_tx=fee_parent(NEW_ADMINS[0], coin=coin))
        with rejected("funds"):
            build_migration_tx(load_snapshot(snap_doc), load_plan(plan_doc(snap_doc)))

    def test_a_pool_holding_another_token(self):
        pool_tx = parent_tx([{0: b"\x71" + bytes.fromhex("e1a181b2f8b74be55e6160a7a82764b9b174e04c37c4ed49d349a3dd"),
                              1: [POOL_LOVELACE, {bytes.fromhex(CMATRA_UNIT[:56]): {bytes.fromhex(CMATRA_UNIT[56:]): 5},
                                                  b"\x09" * 28: {b"x": 1}}],
                              2: [1, CBORTag(24, bytes.fromhex("d87980"))]}])
        snap_doc = snapshot_doc(pool_tx=pool_tx, pool_ref=ref_of(pool_tx, 0))
        with rejected("pool_input"):
            build_migration_tx(load_snapshot(snap_doc), load_plan(plan_doc(snap_doc)))

    def test_a_transaction_above_the_size_limit(self):
        snap_doc = snapshot_doc()
        snap_doc["protocol_parameters"] = {**PARAMS, "max_tx_size": 1_000}
        with rejected("size"):
            build_migration_tx(load_snapshot(snap_doc), load_plan(plan_doc(snap_doc)))

    def test_a_fee_that_never_settles(self, monkeypatch):
        snap, plan = world()
        fees = iter(range(200_000, 10**9, 1_000))
        monkeypatch.setattr("tools.pool_migration.min_fee", lambda *a: next(fees))
        with rejected("fee"):
            build_migration_tx(snap, plan)


class TestSnapshotRefuses:
    @pytest.mark.parametrize("ref", ["d3a1e632", POOL_REF.replace("#", "@"), POOL_REF[:-1] + "x"])
    def test_a_malformed_reference(self, ref):
        doc = snapshot_doc()
        doc["inputs"]["pool"]["ref"] = ref
        with rejected("snapshot"):
            load_snapshot(doc)

    def test_a_reference_past_the_producing_outputs(self):
        doc = snapshot_doc()
        doc["inputs"]["pool"]["ref"] = POOL_REF[:-1] + "9"
        with rejected("snapshot"):
            load_snapshot(doc)

    def test_an_unknown_network(self):
        doc = snapshot_doc()
        doc["network"] = "testnet"
        with rejected("snapshot"):
            load_snapshot(doc)

    def test_a_producing_transaction_that_does_not_hash_to_the_reference(self):
        doc = snapshot_doc()
        doc["inputs"]["pool"]["ref"] = "ab" * 32 + "#1"
        with rejected("snapshot"):
            load_snapshot(doc)

    def test_a_producing_transaction_that_does_not_decode(self):
        doc = snapshot_doc()
        doc["inputs"]["pool"]["producing_tx"] = "84ff"
        with rejected("snapshot"):
            load_snapshot(doc)

    def test_a_spent_input(self):
        doc = snapshot_doc()
        doc["inputs"]["pool"]["consumed_by"] = "cd" * 32
        with rejected("spent"):
            load_snapshot(doc)

    def test_a_pool_input_without_the_void_datum(self):
        pool_tx = parent_tx([{0: b"\x71" + b"\x01" * 28, 1: [2_000_000, {
            bytes.fromhex(CMATRA_UNIT[:56]): {bytes.fromhex(CMATRA_UNIT[56:]): 5}}]}])
        with rejected("pool_input"):
            load_snapshot(snapshot_doc(pool_tx=pool_tx, pool_ref=ref_of(pool_tx, 0)))

    @pytest.mark.parametrize("output", [
        {0: b"\x71" + b"\x07" * 28, 1: FEE_LOVELACE},
        {0: b"\x61" + b"\x07" * 28, 1: [FEE_LOVELACE, {b"\x08" * 28: {b"x": 1}}]},
    ])
    def test_a_fee_input_that_is_not_pure_ada_at_a_key(self, output):
        with rejected("fee_input"):
            load_snapshot(snapshot_doc(fee_tx=parent_tx([output])))


# ---------------------------------------------------------------------------
# Witnesses
# ---------------------------------------------------------------------------


class TestAssemble:
    def test_reads_what_cardano_cli_11_writes(self):
        envelope = json.loads((FIXTURES / "cardano-cli-11.0.0.0.witness.json").read_text())
        txid = bytes.fromhex((FIXTURES / "cardano-cli-11.0.0.0.witness.txid").read_text().strip())
        vkey, signature = _key_witness(bytes.fromhex(envelope["cborHex"]))
        assert envelope["type"] == "TxWitness ConwayEra"
        assert len(vkey) == 32 and signature_verifies(vkey, txid, signature)

    def test_signed_transaction_keeps_every_signed_byte(self):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        signed = assemble(unsigned, [keys.witness(i, txid) for i in range(4)], snap, plan)
        assert split_tx(signed)[0] == split_tx(unsigned)[0]
        before, after = _witness_fields(split_tx(unsigned)[1]), _witness_fields(split_tx(signed)[1])
        assert (after[5], after[7]) == (before[5], before[7])
        assert sorted(after) == [0, 5, 7]
        report = check_migration_tx(signed, snap, plan)
        assert report["signed_size"] == len(signed)
        assert report["fee"] >= report["min_fee"]

    def test_slice_signs_the_same_way(self):
        keys, snap, plan = signing_world(move=1_000_000)
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        assemble(unsigned, [keys.witness(i, txid) for i in range(4)], snap, plan)

    def test_a_missing_admin_witness(self):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        with rejected("witnesses"):
            assemble(unsigned, [keys.witness(i, txid) for i in range(3)], snap, plan)

    def test_a_witness_over_another_body(self):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        witnesses = [keys.witness(i, txid) for i in range(3)] + [keys.witness(3, b"\x00" * 32)]
        with rejected("witnesses"):
            assemble(unsigned, witnesses, snap, plan)

    def test_a_witness_from_anyone_else(self):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        stranger = Keys(1)
        witnesses = [keys.witness(i, txid) for i in range(4)] + [stranger.witness(0, txid)]
        with rejected("witnesses"):
            assemble(unsigned, witnesses, snap, plan)

    def test_a_repeated_witness(self):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        witnesses = [keys.witness(i, txid) for i in range(4)] + [keys.witness(0, txid)]
        with rejected("witnesses"):
            assemble(unsigned, witnesses, snap, plan)

    @pytest.mark.parametrize("witness", [
        cbor2.dumps([b"\x00" * 32, b"\x00" * 64, b"", b""]),   # bootstrap witness
        cbor2.dumps([b"\x00" * 31, b"\x00" * 64]),             # short key
    ])
    def test_a_malformed_witness(self, witness):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        with rejected("witnesses"):
            assemble(unsigned, [keys.witness(i, txid) for i in range(4)] + [witness], snap, plan)

    def test_an_undecodable_witness(self):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        with pytest.raises(MigrationRejected, match=r"^witnesses: undecodable witness"):
            assemble(unsigned, [keys.witness(i, txid) for i in range(4)] + [b"\xff"], snap, plan)

    def test_an_admin_witness_with_a_short_signature(self):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        key = keys.signing[3]
        short = cbor2.dumps([bytes(key.verify_key), key.sign(txid).signature[:63]])
        with rejected("witnesses"):
            assemble(unsigned, [keys.witness(i, txid) for i in range(3)] + [short], snap, plan)

    def test_an_admin_signature_framed_as_another_witness_kind(self):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        key = keys.signing[3]
        reframed = cbor2.dumps([0, [bytes(key.verify_key), key.sign(txid).signature]])
        with rejected("witnesses"):
            assemble(unsigned, [keys.witness(i, txid) for i in range(3)] + [reframed], snap, plan)

    def test_a_transaction_already_signed(self):
        keys, snap, plan = signing_world()
        unsigned = build_migration_tx(snap, plan)
        txid = blake2b_256(split_tx(unsigned)[0])
        witnesses = [keys.witness(i, txid) for i in range(4)]
        signed = assemble(unsigned, witnesses, snap, plan)
        with rejected("witnesses"):
            assemble(signed, witnesses, snap, plan)

    def test_a_transaction_that_fails_its_check(self):
        keys, snap, plan = signing_world()

        def mutate(body, _):
            body[1][0][0] = b"\x71" + b"\xab" * 28
        unsigned = mutated(snap, plan, mutate)
        txid = blake2b_256(split_tx(unsigned)[0])
        with rejected("output_address"):
            assemble(unsigned, [keys.witness(i, txid) for i in range(4)], snap, plan)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


class FakeChain:
    """The chain client calls `snapshot` makes, answered from fixtures."""

    def __init__(self, txs: dict[str, bytes], spent: dict[str, str]) -> None:
        self.txs, self.spent = txs, spent
        self.base_url = "http://127.0.0.1:1/api/v0"

    def get_tx_cbor(self, tx_hash: str) -> bytes:
        return self.txs[tx_hash]

    def get_tx_utxos(self, tx_hash: str) -> dict:
        outputs = decode(split_tx(self.txs[tx_hash])[0])[1]
        rows = [{"output_index": i, "consumed_by_tx": self.spent.get(f"{tx_hash}#{i}")}
                for i in range(len(outputs))]
        return {"hash": tx_hash, "inputs": [], "outputs": rows + [
            {"output_index": len(outputs), "collateral": True, "consumed_by_tx": "ff" * 32}]}

    def get_protocol_parameters(self) -> dict:
        return PARAMS

    def get_latest_block(self) -> dict:
        return {"slot": TIP_SLOT, "height": 14_154_879, "hash": "aa" * 32, "time": 1_793_754_311}


class TestCli:
    def test_snapshot_records_what_build_needs(self, tmp_path, monkeypatch):
        fee_tx = fee_parent(NEW_ADMINS[0])
        fee_ref = ref_of(fee_tx, 0)
        chain = FakeChain({POOL_REF[:64]: POOL_TX, fee_ref[:64]: fee_tx}, {})
        monkeypatch.setattr("tools.pool_migration.BlockfrostClient", lambda: chain)
        out = tmp_path / "snapshot.json"
        assert main(["snapshot", "--pool-ref", POOL_REF, "--fee-ref", fee_ref, "--out", str(out)]) == 0
        doc = json.loads(out.read_text())
        assert doc["tip"]["slot"] == TIP_SLOT
        assert doc["inputs"]["pool"]["consumed_by"] is None
        snap = load_snapshot(doc)
        build_migration_tx(snap, load_plan(plan_doc(doc)))

    def test_snapshot_records_a_spent_input(self, tmp_path, monkeypatch):
        fee_tx = fee_parent(NEW_ADMINS[0])
        fee_ref = ref_of(fee_tx, 0)
        chain = FakeChain({POOL_REF[:64]: POOL_TX, fee_ref[:64]: fee_tx}, {POOL_REF: "cd" * 32})
        monkeypatch.setattr("tools.pool_migration.BlockfrostClient", lambda: chain)
        out = tmp_path / "snapshot.json"
        assert main(["snapshot", "--pool-ref", POOL_REF, "--fee-ref", fee_ref, "--out", str(out)]) == 0
        with rejected("spent"):
            load_snapshot(json.loads(out.read_text()))

    def test_snapshot_refuses_an_output_the_chain_does_not_list(self, tmp_path, monkeypatch, capsys):
        fee_tx = fee_parent(NEW_ADMINS[0])
        chain = FakeChain({POOL_REF[:64]: POOL_TX, ref_of(fee_tx, 0)[:64]: fee_tx}, {})
        monkeypatch.setattr("tools.pool_migration.BlockfrostClient", lambda: chain)
        out = tmp_path / "snapshot.json"
        assert main(["snapshot", "--pool-ref", POOL_REF[:-1] + "4", "--fee-ref", ref_of(fee_tx, 0),
                     "--out", str(out)]) == 1
        assert "REFUSED snapshot" in capsys.readouterr().err
        assert not out.exists()

    def test_build_check_assemble(self, tmp_path, capsys):
        keys, snap_doc, plan = signing_docs()
        plan_path, snap_path = tmp_path / "plan.json", tmp_path / "snapshot.json"
        snap_path.write_text(json.dumps(snap_doc))
        plan_path.write_text(json.dumps(plan))
        unsigned_path = tmp_path / "migration.unsigned.tx"
        common = ["--snapshot", str(snap_path), "--plan", str(plan_path)]

        assert main(["build", *common, "--out", str(unsigned_path)]) == 0
        envelope = json.loads(unsigned_path.read_text())
        assert envelope["type"] == "Tx ConwayEra"
        unsigned = bytes.fromhex(envelope["cborHex"])

        capsys.readouterr()
        assert main(["check", *common, "--tx", str(unsigned_path)]) == 0
        report = json.loads(capsys.readouterr().out)
        txid = blake2b_256(split_tx(unsigned)[0])
        assert report["txid"] == txid.hex()

        witness_paths = []
        for i in range(4):
            path = tmp_path / f"w{i}.witness"
            path.write_text(json.dumps({"type": "TxWitness ConwayEra", "description": "Key Witness ShelleyEra",
                                        "cborHex": keys.witness(i, txid).hex()}))
            witness_paths += ["--witness", str(path)]
        signed_path = tmp_path / "migration.signed.tx"
        assert main(["assemble", *common, "--tx", str(unsigned_path), *witness_paths,
                     "--out", str(signed_path)]) == 0
        signed = bytes.fromhex(json.loads(signed_path.read_text())["cborHex"])
        assert split_tx(signed)[0] == split_tx(unsigned)[0]

    def test_check_exits_nonzero_on_a_refusal(self, tmp_path, capsys):
        snap_doc = snapshot_doc()
        snap, plan = load_snapshot(snap_doc), load_plan(plan_doc(snap_doc))
        tx = mutated(snap, plan, lambda body, _: body.__setitem__(2, 1_000_000))
        tx_path, snap_path, plan_path = tmp_path / "tx", tmp_path / "s.json", tmp_path / "p.json"
        tx_path.write_text(json.dumps({"type": "Tx ConwayEra", "description": "", "cborHex": tx.hex()}))
        snap_path.write_text(json.dumps(snap_doc))
        plan_path.write_text(json.dumps(plan_doc(snap_doc)))
        assert main(["check", "--snapshot", str(snap_path), "--plan", str(plan_path),
                     "--tx", str(tx_path)]) == 1
        assert "REFUSED" in capsys.readouterr().err
