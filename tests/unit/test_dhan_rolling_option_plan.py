"""Tests for the Dhan rolling-ATM option plan (the pure 1008-key cartesian).

DEC-DHAN-ROLLING-OPTIONS-001.
"""

from __future__ import annotations

import pytest
from brutex.adapters.dhan.rolling_option_plan import (
    DHAN_ROLLING_PLAN_KEY_COUNT,
    RollingOptionKey,
    enumerate_rolling_option_keys,
    strike_offset_labels,
)


def test_strike_offset_labels_band_one_covers_all_three_arms() -> None:
    assert strike_offset_labels(1) == ("ATM-1", "ATM", "ATM+1")


def test_strike_offset_labels_band_zero_is_atm_only() -> None:
    assert strike_offset_labels(0) == ("ATM",)


def test_strike_offset_labels_default_band_ten() -> None:
    labels = strike_offset_labels(10)
    assert len(labels) == 21
    assert labels[0] == "ATM-10"
    assert labels[10] == "ATM"
    assert labels[-1] == "ATM+10"


def test_strike_offset_labels_negative_band_fails_loud() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        strike_offset_labels(-1)


def test_enumerate_yields_exactly_1008_keys() -> None:
    keys = enumerate_rolling_option_keys()
    assert len(keys) == DHAN_ROLLING_PLAN_KEY_COUNT == 1008
    # 3 underlyings × 2 flags × 4 codes × 21 offsets × 2 option types.
    assert len(keys) == 3 * 2 * 4 * 21 * 2


def test_enumerate_band_zero_shrinks_the_grid() -> None:
    keys = enumerate_rolling_option_keys(band=0)
    assert len(keys) == 3 * 2 * 4 * 1 * 2  # one offset (ATM) per selector


def test_each_key_is_a_full_partition_identity() -> None:
    keys = enumerate_rolling_option_keys()
    # Every key is unique (the cartesian has no duplicate identities).
    assert len({keys}) == 1  # tuple hashable
    identities = {
        (
            k.underlying,
            k.exchange,
            k.exchange_segment,
            k.security_id,
            k.expiry_flag,
            k.expiry_code,
            k.strike_offset,
            k.option_type,
        )
        for k in keys
    }
    assert len(identities) == len(keys)


def test_sensex_key_addresses_bse_fno() -> None:
    keys = enumerate_rolling_option_keys()
    sensex = [k for k in keys if k.underlying == "SENSEX"]
    assert sensex, "SENSEX keys must be enumerated (even if WEEK is honest-empty)"
    for k in sensex:
        assert k.exchange == "BSE"
        assert k.exchange_segment == "BSE_FNO"
        assert k.security_id == 51


def test_nifty_key_addresses_nse_fno_with_official_security_id() -> None:
    keys = enumerate_rolling_option_keys()
    nifty = next(k for k in keys if k.underlying == "NIFTY")
    assert nifty.exchange == "NSE"
    assert nifty.exchange_segment == "NSE_FNO"
    assert nifty.security_id == 13  # SDK-official


def test_key_is_frozen() -> None:
    key = enumerate_rolling_option_keys(band=0)[0]
    assert isinstance(key, RollingOptionKey)
    with pytest.raises((AttributeError, TypeError)):
        key.underlying = "X"  # type: ignore[misc]


def test_plan_strike_offset_law_atm_pm10_and_full_cartesian() -> None:
    """The ATM±10 plan pin (DEC-DHAN-ROLLING-OPTIONS-001): the default plan's
    strike-offset law is EXACTLY offsets −10..+10 (21 values, ATM inclusive), and
    the full cartesian is 3 underlyings × {WEEK, MONTH} × expiry_code {0,1,2,3} ×
    21 offsets × {CALL, PUT} = 1008 keys — each combination exactly once. This is
    also the SOLE derivative-history plan surface of the Dhan feed
    (DEC-DHAN-NO-FUTURES-001, two distinct claims split honestly): the SCOPE
    decision — no futures leg — implements DEC-SCOPE-MINIMAL-CORE-001 (the
    operator-authorized scope decision); the VENDOR-capability claim — Dhan
    serves no expired index futures history — is documented-as-unsupported,
    probes unrun."""
    keys = enumerate_rolling_option_keys()
    # The offset law: exactly ATM−10..ATM+10, ATM inclusive — 21 labels.
    expected_offsets = (
        {"ATM"} | {f"ATM+{i}" for i in range(1, 11)} | {f"ATM-{i}" for i in range(1, 11)}
    )
    offsets = {k.strike_offset for k in keys}
    assert offsets == expected_offsets == set(strike_offset_labels(10))
    assert len(offsets) == 21
    # The per-dimension value sets, pinned exactly.
    assert {k.underlying for k in keys} == {"NIFTY", "BANKNIFTY", "SENSEX"}
    assert {k.expiry_flag for k in keys} == {"WEEK", "MONTH"}
    assert {k.expiry_code for k in keys} == {0, 1, 2, 3}
    assert {k.option_type for k in keys} == {"CALL", "PUT"}
    # The full cartesian: 3 × 2 × 4 × 21 × 2 = 1008, every combination ONCE.
    assert len(keys) == 3 * 2 * 4 * 21 * 2 == 1008 == DHAN_ROLLING_PLAN_KEY_COUNT
    combos = {
        (k.underlying, k.expiry_flag, k.expiry_code, k.strike_offset, k.option_type) for k in keys
    }
    assert len(combos) == DHAN_ROLLING_PLAN_KEY_COUNT
