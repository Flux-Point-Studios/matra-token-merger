"""Tests for tools.sweep_helpers — the AdminWithdraw sweep ceremony helpers."""

from __future__ import annotations

import json
import pytest

from tools.sweep_helpers import (
    MAINNET_SHELLEY_START_SLOT,
    MAINNET_SHELLEY_START_TIME,
    ValidityWindow,
    compute_sweep_change,
    derive_sweep_validity_window,
    encode_admin_withdraw_redeemer,
    encode_some_void_datum,
    koios_get_pool_utxo,
    koios_get_tip,
    posix_ms_to_slot,
    slot_to_posix_ms,
    validate_ex_units,
    write_plutus_data_json_constr,
)


class FakeFetch:
    """Records calls + returns a queued response. The _fetch test seam contract
    is fetch(method, url, body) -> parsed_json."""

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method, url, body):
        self.calls.append((method, url, body))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


# Canonical on-chain values pinned at mint (2026-05-18)
DEADLINE_POSIX_MS = 1_795_910_400_000  # 2026-11-29 00:00:00 UTC
DEADLINE_SLOT_MAINNET = 203_912_109     # = 4492800 + (1795910400 - 1596491091)

POOL_ADDR = "addr1w8s6rqdjlzm5he27v9s202p8vjumza8qfsmufm2f6dy68hg9mn27a"
CMATRA_POLICY = "98c61f406f7c8df11ccff49ca1631d8bca9663894537c6c5ee5ed418"
CMATRA_ASSET = "634d41545241"  # "cMATRA"
CMATRA_UNIT = f"{CMATRA_POLICY}.{CMATRA_ASSET}"
KOIOS = "https://api.koios.rest/api/v1"


# ============================================================================
# Slot / time conversion
# ============================================================================


class TestPosixMsToSlot:
    def test_mainnet_shelley_start_round_trips(self):
        # POSIX 1596491091 → slot 4492800
        assert posix_ms_to_slot(MAINNET_SHELLEY_START_TIME * 1000, "mainnet") == \
            MAINNET_SHELLEY_START_SLOT

    def test_mainnet_deadline_canonical(self):
        # The pinned mainnet deadline must map to 203_912_109
        assert posix_ms_to_slot(DEADLINE_POSIX_MS, "mainnet") == DEADLINE_SLOT_MAINNET

    def test_mainnet_offset_1000s(self):
        slot = posix_ms_to_slot((MAINNET_SHELLEY_START_TIME + 1000) * 1000, "mainnet")
        assert slot == MAINNET_SHELLEY_START_SLOT + 1000

    def test_preprod_genesis(self):
        # Preprod: shelley start at posix 1655683200 → slot 0
        assert posix_ms_to_slot(1655683200000, "preprod") == 0

    def test_preprod_offset(self):
        assert posix_ms_to_slot(1655684200000, "preprod") == 1000

    def test_slot_to_posix_round_trip(self):
        # round-trip the deadline
        ms = slot_to_posix_ms(DEADLINE_SLOT_MAINNET, "mainnet")
        assert ms == DEADLINE_POSIX_MS

    def test_slot_to_posix_rejects_negative(self):
        with pytest.raises(ValueError):
            slot_to_posix_ms(-1, "mainnet")


# ============================================================================
# Validity-window derivation (the central correctness property)
# ============================================================================


class TestDeriveSweepValidityWindow:
    def test_lower_strictly_after_deadline(self):
        # The validator demands `lower > deadline_ms` (is_entirely_after). The
        # buffer must push us strictly past, in slots.
        win = derive_sweep_validity_window(
            DEADLINE_POSIX_MS,
            buffer_slots=60,
            duration_hours=4,
            network="mainnet",
        )
        assert win.invalid_before == DEADLINE_SLOT_MAINNET + 60

    def test_duration_in_slots(self):
        # 4 hours = 14400 slots
        win = derive_sweep_validity_window(
            DEADLINE_POSIX_MS, buffer_slots=60, duration_hours=4, network="mainnet",
        )
        assert win.invalid_hereafter - win.invalid_before == 4 * 3600

    def test_zero_buffer_rejected(self):
        # buffer must be >= 1 — the validator's is_entirely_after is strict
        with pytest.raises(ValueError, match="buffer_slots must be >= 1"):
            derive_sweep_validity_window(
                DEADLINE_POSIX_MS, buffer_slots=0, duration_hours=4,
            )

    def test_negative_buffer_rejected(self):
        with pytest.raises(ValueError):
            derive_sweep_validity_window(
                DEADLINE_POSIX_MS, buffer_slots=-1, duration_hours=4,
            )

    def test_zero_duration_rejected(self):
        with pytest.raises(ValueError, match="duration_hours must be >= 1"):
            derive_sweep_validity_window(
                DEADLINE_POSIX_MS, buffer_slots=60, duration_hours=0,
            )

    def test_covers_slot_inside_range(self):
        win = derive_sweep_validity_window(
            DEADLINE_POSIX_MS, buffer_slots=60, duration_hours=4,
        )
        assert win.covers_slot(win.invalid_before)
        assert win.covers_slot(win.invalid_before + 100)
        assert win.covers_slot(win.invalid_hereafter - 1)

    def test_covers_slot_outside_range(self):
        win = derive_sweep_validity_window(
            DEADLINE_POSIX_MS, buffer_slots=60, duration_hours=4,
        )
        assert not win.covers_slot(win.invalid_before - 1)
        assert not win.covers_slot(win.invalid_hereafter)
        assert not win.covers_slot(win.invalid_hereafter + 1)
        # Deadline itself must NOT be covered (sanity)
        assert not win.covers_slot(DEADLINE_SLOT_MAINNET)


# ============================================================================
# CBOR / Plutus Data shaping
# ============================================================================


class TestCborEncoding:
    def test_some_void_datum_is_d87980(self):
        # Constr 121 [] -> CBOR D8 79 80
        cbor = encode_some_void_datum()
        assert cbor.hex().lower() == "d87980"

    def test_admin_withdraw_redeemer_is_d87a80(self):
        # Constr 122 [] -> CBOR D8 7A 80
        cbor = encode_admin_withdraw_redeemer()
        assert cbor.hex().lower() == "d87a80"

    def test_datum_redeemer_are_distinct(self):
        assert encode_some_void_datum() != encode_admin_withdraw_redeemer()

    def test_write_constructor_0_json(self, tmp_path):
        p = tmp_path / "datum.json"
        write_plutus_data_json_constr(str(p), 0)
        with open(p) as f:
            data = json.load(f)
        assert data == {"constructor": 0, "fields": []}

    def test_write_constructor_1_json(self, tmp_path):
        p = tmp_path / "redeemer.json"
        write_plutus_data_json_constr(str(p), 1)
        with open(p) as f:
            data = json.load(f)
        assert data == {"constructor": 1, "fields": []}

    def test_write_rejects_negative_constructor(self, tmp_path):
        with pytest.raises(ValueError):
            write_plutus_data_json_constr(str(tmp_path / "x.json"), -1)


# ============================================================================
# Koios fetches (mocked)
# ============================================================================


def _make_koios_pool_response(
    tx_hash: str = "9a68849fd788fb3622bc5823d892b61c2e94ff57416b190300ee49f3dc7cb6a2",
    tx_index: int = 0,
    ada: int = 2_000_000,
    cmatra_qty: int = 722_500_000_000_000,
    datum_hash: str | None = None,
    inline_datum: str | None = None,
) -> list[dict]:
    return [{
        "tx_hash": tx_hash,
        "tx_index": tx_index,
        "address": POOL_ADDR,
        "value": str(ada),
        "stake_address": None,
        "payment_cred": "e1a181b2f8b74be55e6160a7a82764b9b174e04c37c4ed49d349a3dd",
        "epoch_no": 631,
        "block_height": 13435752,
        "block_time": 1779141644,
        "datum_hash": datum_hash,
        "inline_datum": inline_datum,
        "reference_script": None,
        "asset_list": [
            {
                "decimals": 0,
                "quantity": str(cmatra_qty),
                "policy_id": CMATRA_POLICY,
                "asset_name": CMATRA_ASSET,
                "fingerprint": "asset1za7s4h0upcr54mrxc3xtt8vuptjvw6cugfthzw",
            },
        ],
        "is_spent": False,
    }]


class TestKoiosGetPoolUtxo:
    def test_single_pool_utxo_parses(self):
        f = FakeFetch(_make_koios_pool_response())
        u = koios_get_pool_utxo(KOIOS, POOL_ADDR, CMATRA_UNIT, _fetch=f)
        assert u["tx_hash"] == "9a68849fd788fb3622bc5823d892b61c2e94ff57416b190300ee49f3dc7cb6a2"
        assert u["tx_index"] == 0
        assert u["ada_lovelace"] == 2_000_000
        assert u["cmatra_units"] == 722_500_000_000_000
        # contract: POST to /address_utxos, extended=True, single address
        assert f.calls == [(
            "POST",
            f"{KOIOS}/address_utxos",
            {"_addresses": [POOL_ADDR], "_extended": True},
        )]

    def test_empty_pool_raises(self):
        with pytest.raises(RuntimeError, match="No UTxOs at pool"):
            koios_get_pool_utxo(KOIOS, POOL_ADDR, CMATRA_UNIT, _fetch=FakeFetch([]))

    def test_multiple_utxos_raises(self):
        # The pool invariant is one UTxO; multiple = manual review
        resp = _make_koios_pool_response() + _make_koios_pool_response(tx_hash="bb" * 32)
        with pytest.raises(RuntimeError, match="Expected exactly one UTxO"):
            koios_get_pool_utxo(KOIOS, POOL_ADDR, CMATRA_UNIT, _fetch=FakeFetch(resp))

    def test_unknown_unit_yields_zero_cmatra(self):
        u = koios_get_pool_utxo(
            KOIOS, POOL_ADDR,
            "00" * 28 + ".decafbad",  # different policy
            _fetch=FakeFetch(_make_koios_pool_response()),
        )
        assert u["cmatra_units"] == 0
        assert u["ada_lovelace"] == 2_000_000

    def test_malformed_unit_raises(self):
        with pytest.raises(ValueError, match="cmatra_unit must be"):
            koios_get_pool_utxo(
                KOIOS, POOL_ADDR, "no_dot_here",
                _fetch=FakeFetch(_make_koios_pool_response()),
            )

    def test_non_list_response_raises(self):
        with pytest.raises(ValueError, match="non-list"):
            koios_get_pool_utxo(
                KOIOS, POOL_ADDR, CMATRA_UNIT,
                _fetch=FakeFetch({"unexpected": "shape"}),
            )

    def test_inline_datum_passes_through(self):
        resp = _make_koios_pool_response(inline_datum="d87980")
        u = koios_get_pool_utxo(KOIOS, POOL_ADDR, CMATRA_UNIT, _fetch=FakeFetch(resp))
        assert u["inline_datum"] == "d87980"


class TestKoiosGetTip:
    def test_returns_abs_slot_and_block_time(self):
        resp = [{"abs_slot": 205_000_000, "block_time": 1797000000, "epoch_no": 632}]
        tip = koios_get_tip(KOIOS, _fetch=FakeFetch(resp))
        assert tip == {"abs_slot": 205_000_000, "block_time": 1797000000}

    def test_handles_dict_response(self):
        # Koios sometimes wraps singletons in {}, sometimes []. Both should work.
        resp = {"abs_slot": 205_000_000, "block_time": 1797000000}
        tip = koios_get_tip(KOIOS, _fetch=FakeFetch(resp))
        assert tip == {"abs_slot": 205_000_000, "block_time": 1797000000}

    def test_uses_get_method(self):
        f = FakeFetch([{"abs_slot": 1, "block_time": 2}])
        koios_get_tip(KOIOS, _fetch=f)
        assert f.calls == [("GET", f"{KOIOS}/tip", None)]


# ============================================================================
# Ex-units validation
# ============================================================================


def _make_pparams(max_mem: int = 14_000_000, max_cpu: int = 10_000_000_000) -> dict:
    return {
        "maxTxExecutionUnits": {"memory": max_mem, "steps": max_cpu},
    }


class TestValidateExUnits:
    def test_within_50pct_passes(self):
        pp = _make_pparams()
        # ~14% of mem, ~6% of cpu — well within 50% headroom
        validate_ex_units(2_000_000, 600_000_000, pp, headroom_fraction=0.5)

    def test_exceeds_50pct_mem_rejected(self):
        pp = _make_pparams(max_mem=10_000_000)
        with pytest.raises(ValueError, match="mem"):
            validate_ex_units(8_000_000, 100_000_000, pp, headroom_fraction=0.5)

    def test_exceeds_50pct_cpu_rejected(self):
        pp = _make_pparams(max_cpu=1_000_000_000)
        with pytest.raises(ValueError, match="cpu"):
            validate_ex_units(1_000_000, 800_000_000, pp, headroom_fraction=0.5)

    def test_zero_ex_units_rejected(self):
        pp = _make_pparams()
        with pytest.raises(ValueError, match="positive"):
            validate_ex_units(0, 1_000_000, pp)

    def test_missing_pparams_field_raises(self):
        with pytest.raises(ValueError, match="maxTxExecutionUnits"):
            validate_ex_units(1_000_000, 600_000_000, {"foo": "bar"})

    def test_invalid_headroom_fraction(self):
        pp = _make_pparams()
        with pytest.raises(ValueError, match="headroom_fraction"):
            validate_ex_units(1_000_000, 1, pp, headroom_fraction=0)
        with pytest.raises(ValueError, match="headroom_fraction"):
            validate_ex_units(1_000_000, 1, pp, headroom_fraction=1.5)

    def test_full_headroom_allows_max(self):
        pp = _make_pparams(max_mem=2_000_000, max_cpu=1_000_000_000)
        # headroom=1.0 → exact equality should be allowed
        validate_ex_units(2_000_000, 1_000_000_000, pp, headroom_fraction=1.0)


# ============================================================================
# Sweep change arithmetic
# ============================================================================


class TestComputeSweepChange:
    def test_normal_case(self):
        # pool_ada=2M, fee=200k → 1.8M remains for dest
        assert compute_sweep_change(2_000_000, 200_000, 1_000_000) == 1_800_000

    def test_change_below_min_utxo_raises(self):
        # If pool_ada - fee < min, the script must NOT silently fall through
        with pytest.raises(RuntimeError, match="min-utxo"):
            compute_sweep_change(2_000_000, 1_900_000, 1_000_000)

    def test_negative_inputs_rejected(self):
        with pytest.raises(ValueError):
            compute_sweep_change(-1, 100, 1_000_000)
        with pytest.raises(ValueError):
            compute_sweep_change(1_000_000, -1, 1_000_000)
        with pytest.raises(ValueError):
            compute_sweep_change(1_000_000, 100, -1)

    def test_exact_min_utxo_allowed(self):
        # 1M pool - 0 fee = 1M = min → boundary case (allowed)
        assert compute_sweep_change(1_000_000, 0, 1_000_000) == 1_000_000


# ============================================================================
# ValidityWindow dataclass behavior
# ============================================================================


class TestValidityWindow:
    def test_construct_and_access(self):
        w = ValidityWindow(invalid_before=100, invalid_hereafter=200)
        assert w.invalid_before == 100
        assert w.invalid_hereafter == 200

    def test_covers_slot_boundary(self):
        w = ValidityWindow(invalid_before=100, invalid_hereafter=200)
        assert w.covers_slot(100)         # inclusive lower
        assert w.covers_slot(199)         # inclusive upper - 1
        assert not w.covers_slot(99)
        assert not w.covers_slot(200)     # exclusive upper
