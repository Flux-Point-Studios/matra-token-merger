"""Mutation test for the surrender API's check of the co-signer's answer and
the order it merges the admins' witnesses in.

Each mutant rewrites one condition in a copy of one function, and every test
of the answer then runs against it on a world of its own. A mutant that turns
no test red is a condition nothing notices, and this test names it."""

from __future__ import annotations

import inspect
from typing import Callable

import pytest

import services.surrender_api as api
from tests.test_surrender_api_cosign import (
    WRONG_ANSWERS,
    World,
    test_a_cosigner_answer_that_is_not_its_signature_of_the_body_is_refused as refuses,
    test_no_pair_from_the_cosigner_step_displaces_admin_1s_own_witness as keeps_admin_1s_witness,
)

TESTS: dict[str, Callable[[World], None]] = {
    **{f"refuses_{wrong}": (lambda world, wrong=wrong: refuses(world, wrong)) for wrong in WRONG_ANSWERS},
    "keeps_admin_1s_witness": lambda world: keeps_admin_1s_witness(world, world.monkeypatch),
}

MUTANTS = {
    "key_not_checked": (
        "_get_cosigner_witness",
        "hashlib.blake2b(vkey, digest_size=28).digest() == state.cosigner_pkh.payload\n            and ",
        "",
    ),
    "signature_not_verified": (
        "_get_cosigner_witness", " and signature_verifies(vkey, body_hash, signature)", "",
    ),
    "signature_length_not_checked": ("_get_cosigner_witness", "len(signature) == 64 and ", ""),
    "a_long_signature_passed_to_the_verifier": (
        "_get_cosigner_witness", "len(signature) == 64", "len(signature) >= 64",
    ),
    "body_hash_taken_from_the_answer": (
        "_get_cosigner_witness",
        "hashlib.blake2b(split_tx(tx_cbor)[0], digest_size=32).digest()",
        'bytes.fromhex(data["tx_hash"])',
    ),
    "cosigner_witness_merged_first": ("_signed_by_admins", "[own, *cosigner]", "[*cosigner, own]"),
}


def _mutant(function: str, original: str, changed: str) -> Callable:
    source = inspect.getsource(getattr(api, function))
    assert source.count(original) == 1, f"{original!r} is no longer in {function}"
    namespace: dict = {}
    exec(compile(source.replace(original, changed), api.__file__, "exec"), vars(api), namespace)
    return namespace[function]


def _red(tmp_path, replace: tuple[str, Callable] | None) -> list[str]:
    """The tests that fail with ``replace`` (a function's name and its
    stand-in) in place, each on a world of its own."""
    red = []
    for name, run in TESTS.items():
        (tmp_path / name).mkdir()
        with pytest.MonkeyPatch.context() as mp:
            world = World(tmp_path / name, mp)
            if replace is not None:
                mp.setattr(api, *replace)
            try:
                run(world)
            except (Exception, pytest.fail.Exception):
                red.append(name)
            finally:
                world.close()
    return red


def test_the_unmutated_functions_turn_no_test_red(tmp_path):
    assert _red(tmp_path, None) == []


@pytest.mark.parametrize("name", MUTANTS)
def test_every_mutant_turns_a_test_red(tmp_path, name):
    function, original, changed = MUTANTS[name]
    assert _red(tmp_path, (function, _mutant(function, original, changed))), f"{name} turns no test red"
