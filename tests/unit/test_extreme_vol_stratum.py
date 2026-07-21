"""Unit tests for :mod:`brutex.metrics.extreme_vol_stratum`
(DEC-REGIME-MUST-SURVIVE-STRATUM-001).

Boundary-exact session classification (the 2.00% threshold + the
STRATUM_START cut) and every :func:`evaluate_stratum` decision branch
(unproven / insufficient / negative-expectancy fail / variance-ratio fail /
abstaining variance arm / ok), targeting 100% line + branch coverage.
"""

from __future__ import annotations

import math
from datetime import date

from brutex.metrics.extreme_vol_stratum import (
    EXTREME_DAILY_MOVE_PCT,
    MIN_STRATUM_TRADES,
    STRATUM_START,
    VARIANCE_RATIO_MAX,
    VIX_EXTREME,
    StratumVerdict,
    classify_extreme_sessions,
    evaluate_stratum,
)

# ────── classify_extreme_sessions ──────


def test_classify_empty_and_single_entry_are_empty() -> None:
    assert classify_extreme_sessions({}) == frozenset()
    assert classify_extreme_sessions({date(2024, 1, 2): 100.0}) == frozenset()


def test_classify_boundary_exact_move_percent() -> None:
    # Consecutive sessions 100.00 → X: 1.99% is calm, 2.00% and 2.01% are
    # extreme. HONEST: the exact 2% edge is float-unreachable via close
    # ratios (102.0/100.0 == 2.0000000000000018%, not exactly 2.0), so this
    # pins behavior NEAR the boundary rather than the >= edge itself; the
    # variance arm (exact 4.0) carries the true strict-boundary pin.
    d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
    assert classify_extreme_sessions({d1: 100.0, d2: 101.99}) == frozenset()
    assert classify_extreme_sessions({d1: 100.0, d2: 102.0}) == frozenset({d2})
    assert classify_extreme_sessions({d1: 100.0, d2: 102.01}) == frozenset({d2})


def test_classify_downward_move_counts_absolute() -> None:
    d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
    assert classify_extreme_sessions({d1: 100.0, d2: 97.9}) == frozenset({d2})
    assert classify_extreme_sessions({d1: 100.0, d2: 98.5}) == frozenset()


def test_classify_pre_stratum_start_never_extreme() -> None:
    # A huge 2023 move is OUTSIDE the stratum; the same move on/after
    # 2024-01-01 classifies (the prev session may itself predate the start).
    closes = {
        date(2023, 12, 28): 100.0,
        date(2023, 12, 29): 110.0,  # +10% but pre-2024 → never extreme
        date(2024, 1, 1): 121.0,  # +10% vs 2023-12-29 → extreme (>= start)
        date(2024, 1, 2): 121.5,  # +0.41% → calm
    }
    assert classify_extreme_sessions(closes) == frozenset({date(2024, 1, 1)})
    assert date(2024, 1, 1) == STRATUM_START


def test_classify_first_session_has_no_predecessor() -> None:
    # The first key can never classify (no previous close to move from).
    closes = {date(2024, 1, 2): 500.0, date(2024, 1, 3): 500.1}
    assert classify_extreme_sessions(closes) == frozenset()


def test_classify_unordered_mapping_uses_date_order() -> None:
    # Insertion order must NOT matter — the predecessor is the previous KEY
    # in date order.
    closes = {
        date(2024, 1, 4): 103.0,  # +3% vs the 1/3 close → extreme
        date(2024, 1, 2): 99.0,
        date(2024, 1, 3): 100.0,  # +1.01% vs 1/2 → calm
    }
    assert classify_extreme_sessions(closes) == frozenset({date(2024, 1, 4)})


def test_classify_zero_or_nonfinite_prev_close_abstains() -> None:
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    # Zero previous close → no ratio → that session abstains (never a crash).
    assert classify_extreme_sessions({d1: 0.0, d2: 100.0}) == frozenset()
    # Non-finite closes abstain the affected sessions.
    assert classify_extreme_sessions({d1: math.nan, d2: 100.0, d3: 200.0}) == frozenset({d3})
    assert classify_extreme_sessions({d1: 100.0, d2: math.inf}) == frozenset()


def test_followup_vix_constant_is_defined_but_unconsumed() -> None:
    # The VIX arm is a NAMED FOLLOW-UP: the constant exists (documented) but
    # nothing in the v1 classification consumes it — pin the value so a
    # silent recalibration is loud.
    assert VIX_EXTREME == 25.0


# ────── evaluate_stratum ──────


def test_zero_stratum_trades_is_unproven() -> None:
    v = evaluate_stratum([], [100, -50, 25])
    assert v.status == "unproven_current_regime"
    assert v.stratum_trades == 0
    assert v.calm_trades == 3
    assert v.stratum_expectancy_paisa is None
    assert v.stratum_variance is None
    assert v.calm_expectancy_paisa == 25.0
    # The ratio needs BOTH variances; the stratum side is empty → None.
    assert v.variance_ratio is None


def test_insufficient_below_min_is_abstain() -> None:
    v = evaluate_stratum([100] * (MIN_STRATUM_TRADES - 1), [50] * 10)
    assert v.status == "insufficient_stratum_trades"
    assert v.stratum_trades == MIN_STRATUM_TRADES - 1
    assert "29" in v.detail


def test_exactly_min_trades_evaluates() -> None:
    v = evaluate_stratum([100] * MIN_STRATUM_TRADES, [50] * 10)
    assert v.status == "ok"
    assert v.stratum_trades == MIN_STRATUM_TRADES == 30


def test_negative_expectancy_fails() -> None:
    v = evaluate_stratum([-100] * MIN_STRATUM_TRADES, [50, 150])
    assert v.status == "fail"
    assert v.stratum_expectancy_paisa == -100.0
    assert "expectancy" in v.detail


def test_variance_ratio_boundary() -> None:
    # calm = [800, 1200] → pvariance 40_000. Stratum mean 1000 (>= 0) with
    # 15×(1000+d) + 15×(1000−d) → pvariance d². Strict `>` law:
    #   d=199 → ratio 0.990… of the max → ok
    #   d=400 → ratio exactly 4.0 → NOT > max → ok
    #   d=402 → ratio 4.0401 > 4.0 → fail
    calm = [800, 1200]

    def stratum(d: int) -> list[int]:
        return [1000 + d] * 15 + [1000 - d] * 15

    below = evaluate_stratum(stratum(199), calm)
    assert below.status == "ok"
    assert below.variance_ratio is not None and below.variance_ratio < VARIANCE_RATIO_MAX

    at = evaluate_stratum(stratum(400), calm)
    assert at.status == "ok"
    assert at.variance_ratio == VARIANCE_RATIO_MAX  # exactly at the ratio passes

    above = evaluate_stratum(stratum(402), calm)
    assert above.status == "fail"
    assert above.variance_ratio is not None and above.variance_ratio > VARIANCE_RATIO_MAX
    assert "variance ratio" in above.detail


def test_calm_empty_abstains_variance_arm_expectancy_still_applies() -> None:
    # No calm trades → no denominator → variance arm abstains; the
    # expectancy arm still decides (positive → ok, negative → fail).
    ok = evaluate_stratum([100] * MIN_STRATUM_TRADES, [])
    assert ok.status == "ok"
    assert ok.calm_trades == 0
    assert ok.calm_variance is None
    assert ok.variance_ratio is None
    assert "abstained" in ok.detail

    fail = evaluate_stratum([-1] * MIN_STRATUM_TRADES, [])
    assert fail.status == "fail"


def test_zero_calm_variance_abstains_variance_arm() -> None:
    # A constant calm series has pvariance 0.0 — no meaningful denominator,
    # so the ratio abstains instead of fabricating an infinite ratio.
    v = evaluate_stratum([900, 1100] * 15, [500] * 8)
    assert v.status == "ok"
    assert v.calm_variance == 0.0
    assert v.variance_ratio is None


def test_zero_expectancy_is_not_a_fail() -> None:
    # Expectancy exactly 0 is NOT < 0 (the strict law).
    v = evaluate_stratum([100, -100] * 15, [50] * 4)
    assert v.status == "ok"
    assert v.stratum_expectancy_paisa == 0.0


def test_verdict_carries_measured_numbers() -> None:
    v = evaluate_stratum([100, 300] * 15, [150, 250])
    assert isinstance(v, StratumVerdict)
    assert v.stratum_trades == 30
    assert v.calm_trades == 2
    assert v.stratum_expectancy_paisa == 200.0
    assert v.calm_expectancy_paisa == 200.0
    assert v.stratum_variance == 10_000.0
    assert v.calm_variance == 2_500.0
    assert v.variance_ratio == 4.0
    assert EXTREME_DAILY_MOVE_PCT == 2.0
