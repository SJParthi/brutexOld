"""Unit tests for the session-edge gap-midpoint delegator (`brutex.indicators.gap_mid`).

DEC-GAP-MIDPOINT-CONDITIONS-001 — 100% line+branch of the thin delegator.
The math itself lives ONCE in the shared Rust ``trading_core`` crate
(``DEC-SHARED-CORE-001``; 9 Rust unit tests + 3 proptests own the kernel
truth table); these tests pin the Python-side contract: delegation (never a
re-implementation), the tuple/None shape, the direction law, the
round-half-even house rounding, and the boundary's clean ``ValueError``
refusals (never a panic).
"""

from __future__ import annotations

import pytest
from brutex.indicators.gap_mid import compute_gap_mid


def test_gap_up_day_uses_the_high_pair() -> None:
    # X1_hi 10200 + X2_hi 11200 → level 10700; open 11000 > pdc 10000 ⇒ up (+1).
    assert compute_gap_mid(10200, 9800, 11200, 10800, 11000, 10000) == (10700, 1)


def test_gap_down_day_uses_the_low_pair() -> None:
    # X1_lo 9800 + X2_lo 8800 → level 9300; open 9000 < pdc 10000 ⇒ down (−1).
    assert compute_gap_mid(10200, 9800, 9100, 8800, 9000, 10000) == (9300, -1)


def test_zero_gap_yields_no_level() -> None:
    # open_of_day == pdc exactly ⇒ no direction ⇒ no level (None all day).
    assert compute_gap_mid(10200, 9800, 11200, 10800, 10000, 10000) is None


def test_midpoint_rounds_half_even() -> None:
    # (10001 + 10002) / 2 = 10001.5 → HALF-EVEN → 10002 (the pivots/fib law).
    assert compute_gap_mid(10001, 9000, 10002, 9500, 10100, 10000) == (10002, 1)
    # (10003 + 10004) / 2 = 10003.5 → HALF-EVEN → 10004 (up to even again);
    # (10002 + 10004) / 2 = 10003 exact — no tie.
    assert compute_gap_mid(10002, 9000, 10004, 9500, 10100, 10000) == (10003, 1)


def test_one_paisa_gap_direction_is_exact_sign() -> None:
    # The no-threshold law: a 1-paisa gap already selects a direction.
    up = compute_gap_mid(10200, 9800, 11200, 10800, 10001, 10000)
    down = compute_gap_mid(10200, 9800, 11200, 10800, 9999, 10000)
    assert up is not None and up[1] == 1
    assert down is not None and down[1] == -1
    assert up[0] == 10700  # the hi-pair midpoint
    assert down[0] == (9800 + 10800) // 2  # the lo-pair midpoint (10300)


def test_corrupt_candle_raises_clean_value_error() -> None:
    # hi < lo on either candle is a data-corruption refusal at the boundary.
    with pytest.raises(ValueError, match="X1 candle high"):
        compute_gap_mid(9000, 10200, 11200, 10800, 11000, 10000)
    with pytest.raises(ValueError, match="X2 candle high"):
        compute_gap_mid(10200, 9800, 10800, 11200, 11000, 10000)


def test_out_of_envelope_magnitude_raises_clean_value_error() -> None:
    # Beyond the kernel's overflow-safe bar envelope → ValueError, no panic.
    with pytest.raises(ValueError, match="overflow-safe range"):
        compute_gap_mid(10**17, 9800, 11200, 10800, 11000, 10000)
