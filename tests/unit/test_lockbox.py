"""Unit tests for :mod:`brutex.metrics.lockbox` — Stage-4 filter #8.

Targets 100% line + branch coverage. Exercises the three statuses
(PASS / FAIL / INSUFFICIENT_N), the default-vs-extended window branches,
every gate combination, the validation guards, and the private
``_max_drawdown`` edge cases.
"""
# Whitebox: imports the private ``_max_drawdown`` to cover its empty-array
# branch (unreachable via the public API). Silence the pyright friend-access
# warning file-wide (DEC-P0-017 whitebox-test convention).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import numpy as np
import pytest
from brutex.metrics.lockbox import (
    _DEFAULT_MIN_TRADES,
    _DEFAULT_WINDOW_DAYS,
    LOCKBOX_DEFAULT_MIN_TRADES,
    LOCKBOX_DEFAULT_WINDOW_DAYS,
    LockboxResult,
    LockboxStatus,
    LockboxWindow,
    _max_drawdown,
    lockbox_evaluation,
    resolve_lockbox_window,
)


def test_public_window_constant_is_the_default_window() -> None:
    # DEC-STAGE3-WIRE-001 (review F1): the Stage-3 train-slice helper imports
    # THIS constant — pin it to the private default and the spec's ~6 months
    # so the shared boundary can never silently drift.
    assert LOCKBOX_DEFAULT_WINDOW_DAYS == _DEFAULT_WINDOW_DAYS
    assert LOCKBOX_DEFAULT_WINDOW_DAYS == 182
    # And the evaluation's default window IS this constant: the window start
    # lands exactly at max_day - LOCKBOX_DEFAULT_WINDOW_DAYS + 1.
    days = np.arange(1000, 1000 + 400)
    r = lockbox_evaluation(
        np.full(400, 0.01),
        days,
        training_sharpe=0.0,
        training_max_drawdown=0.0,
        min_trades=1,
    )
    assert r.window_start_day == int(days[-1]) - LOCKBOX_DEFAULT_WINDOW_DAYS + 1


def test_pass_default_window() -> None:
    rets = np.array([0.10, 0.20, 0.15, 0.05, 0.12])
    days = np.array([10, 11, 12, 13, 14])
    r = lockbox_evaluation(
        rets,
        days,
        training_sharpe=0.1,
        training_max_drawdown=10.0,
        min_trades=5,
        default_window_days=5,
    )
    assert isinstance(r, LockboxResult)
    assert r.status is LockboxStatus.PASS
    assert r.passed is True
    assert r.n_trades == 5
    assert r.extended is False
    assert r.count_gate_passed and r.sharpe_gate_passed and r.mdd_gate_passed
    assert r.lockbox_sharpe is not None and r.lockbox_sharpe > 0


def test_fail_on_sharpe_gate() -> None:
    # Mean ~ 0 → low lockbox Sharpe; high training Sharpe → floor not met.
    rets = np.array([0.1, -0.1, 0.1, -0.1, 0.1, -0.1])
    days = np.arange(6)
    r = lockbox_evaluation(
        rets,
        days,
        training_sharpe=2.0,
        training_max_drawdown=100.0,
        min_trades=6,
        default_window_days=10,
    )
    assert r.status is LockboxStatus.FAIL
    assert r.passed is False
    assert r.count_gate_passed is True
    assert r.sharpe_gate_passed is False
    assert r.mdd_gate_passed is True  # generous training MDD


def test_fail_on_mdd_gate() -> None:
    # Positive trend (Sharpe gate passes) but a dip creates MDD > 0; training
    # MDD of 0 makes the ceiling 0 → MDD gate fails.
    rets = np.array([0.1, 0.1, -0.05, 0.1, 0.1])
    days = np.arange(5)
    r = lockbox_evaluation(
        rets,
        days,
        training_sharpe=0.05,
        training_max_drawdown=0.0,
        min_trades=5,
        default_window_days=10,
    )
    assert r.status is LockboxStatus.FAIL
    assert r.sharpe_gate_passed is True
    assert r.mdd_gate_passed is False
    assert r.lockbox_max_drawdown > 0.0


def test_insufficient_n() -> None:
    rets = np.array([0.1, 0.2, 0.1, 0.05, 0.1])  # only 5 trades
    days = np.arange(5)
    r = lockbox_evaluation(
        rets, days, training_sharpe=0.1, training_max_drawdown=10.0, min_trades=500
    )
    assert r.status is LockboxStatus.INSUFFICIENT_N
    assert r.passed is False
    assert r.count_gate_passed is False
    assert r.extended is True  # tried to extend across all history
    assert r.n_trades == 5


def test_extended_window() -> None:
    # Default 2-day window holds only 2 trades (< min 4); extend back to reach 4.
    rets = np.array([0.1, 0.2, 0.15, 0.1, 0.12])
    days = np.array([1, 1, 1, 5, 6])  # last 2 days (5,6) hold only 2 trades
    r = lockbox_evaluation(
        rets,
        days,
        training_sharpe=0.1,
        training_max_drawdown=10.0,
        min_trades=4,
        default_window_days=2,
    )
    assert r.extended is True
    assert r.n_trades >= 4
    assert r.window_start_day == 1  # extended back to the earliest needed day
    assert r.count_gate_passed is True


def test_sharpe_none_single_trade() -> None:
    # A 1-trade window → Sharpe undefined (None) → Sharpe gate fails.
    r = lockbox_evaluation(
        np.array([0.1]),
        np.array([7]),
        training_sharpe=0.0,
        training_max_drawdown=10.0,
        min_trades=1,
        default_window_days=1,
    )
    assert r.lockbox_sharpe is None
    assert r.sharpe_gate_passed is False
    assert r.status is LockboxStatus.FAIL
    assert r.count_gate_passed is True


@pytest.mark.parametrize(
    ("returns", "days", "match"),
    [
        (np.zeros((2, 2)), np.array([1, 2]), "must both be 1-D"),
        (np.array([0.1, 0.2]), np.zeros((2, 1)), "must both be 1-D"),
        (np.array([0.1, 0.2]), np.array([1]), "equal length"),
        (np.array([]), np.array([]), "non-empty"),
        (np.array([0.1, np.nan]), np.array([1, 2]), "must be finite"),
        (np.array([0.1, np.inf]), np.array([1, 2]), "must be finite"),
        (np.array([0.1, 0.2]), np.array([5, 1]), "non-decreasing"),
    ],
)
def test_input_validation(returns: np.ndarray, days: np.ndarray, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        lockbox_evaluation(returns, days, training_sharpe=0.1, training_max_drawdown=1.0)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"default_window_days": 0}, "default_window_days must be >= 1"),
        ({"min_trades": 0}, "min_trades must be >= 1"),
        ({"sharpe_tolerance": -0.1}, "sharpe_tolerance must be >= 0"),
        ({"mdd_tolerance": -0.1}, "mdd_tolerance must be >= 0"),
        ({"training_max_drawdown": -1.0}, "training_max_drawdown must be a finite"),
        ({"training_max_drawdown": float("inf")}, "training_max_drawdown must be a finite"),
        ({"training_max_drawdown": float("nan")}, "training_max_drawdown must be a finite"),
        ({"training_sharpe": float("inf")}, "training_sharpe must be finite"),
        ({"training_sharpe": float("nan")}, "training_sharpe must be finite"),
    ],
)
def test_config_validation(kwargs: dict[str, float], match: str) -> None:
    base = {"training_sharpe": 0.1, "training_max_drawdown": 1.0}
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        lockbox_evaluation(np.array([0.1, 0.2, 0.3]), np.array([1, 2, 3]), **base)  # type: ignore[arg-type]


def test_max_drawdown_helper() -> None:
    assert _max_drawdown(np.array([])) == 0.0
    # Monotonically non-decreasing equity → no drawdown.
    assert _max_drawdown(np.array([0.1, 0.2, 0.3])) == 0.0
    # Peak at +0.3 then down to -0.2 cumulative → drawdown 0.5.
    assert _max_drawdown(np.array([0.3, -0.2, -0.3])) == pytest.approx(0.5)


# ── resolve_lockbox_window — the ONE shared boundary resolver ────────────────
# (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F1/F2)


def test_public_min_trades_constant_is_the_default_floor() -> None:
    # The twin of the LOCKBOX_DEFAULT_WINDOW_DAYS pin: downstream configs
    # (CascadeConfig) import THIS constant — pin it to the private default
    # and the DEC-LOCK-001 500-trade floor so it can never silently drift.
    assert LOCKBOX_DEFAULT_MIN_TRADES == _DEFAULT_MIN_TRADES
    assert LOCKBOX_DEFAULT_MIN_TRADES == 500


@pytest.mark.parametrize(
    ("returns", "days", "kwargs"),
    [
        # Default window suffices (extended False).
        (
            np.array([0.10, 0.20, 0.15, 0.05, 0.12]),
            np.array([10, 11, 12, 13, 14]),
            {"min_trades": 5, "default_window_days": 5},
        ),
        # Backward extension to the min_trades-th most-recent trade.
        (
            np.array([0.1, 0.2, 0.15, 0.1, 0.12]),
            np.array([1, 1, 1, 5, 6]),
            {"min_trades": 4, "default_window_days": 2},
        ),
        # Insufficient even over all history.
        (np.array([0.1, 0.2, 0.1, 0.05, 0.1]), np.arange(5), {"min_trades": 500}),
        # The golden fixture shape (boundary ties on the extension day).
        (
            np.array([0.3, -0.1, 0.2, 0.05, -0.15, 0.25, 0.1, 0.0]),
            np.array([1, 1, 2, 3, 5, 5, 6, 8]),
            {"min_trades": 4, "default_window_days": 4},
        ),
    ],
)
def test_resolver_matches_legacy_evaluation_fields(
    returns: np.ndarray, days: np.ndarray, kwargs: dict[str, int]
) -> None:
    # With evidence_days=None the resolver must be BYTE-IDENTICAL to the
    # legacy inline window logic lockbox_evaluation carried (R2-SR-F1).
    window = resolve_lockbox_window(
        days,
        default_window_days=int(kwargs.get("default_window_days", _DEFAULT_WINDOW_DAYS)),
        min_trades=int(kwargs["min_trades"]),
    )
    r = lockbox_evaluation(returns, days, training_sharpe=0.0, training_max_drawdown=10.0, **kwargs)
    assert isinstance(window, LockboxWindow)
    assert window.window_start == r.window_start_day
    assert window.extended == r.extended
    assert window.n_evidence_in_window == r.n_trades


def test_resolver_evidence_default_window_suffices() -> None:
    # 10 bar-days; 3 trades all inside the 5-day default window ⇒ default kept.
    window = resolve_lockbox_window(
        np.arange(1, 11),
        evidence_days=np.array([7, 9, 10]),
        default_window_days=5,
        min_trades=3,
    )
    assert window == LockboxWindow(window_start=6, extended=False, n_evidence_in_window=3)


def test_resolver_evidence_drives_backward_extension() -> None:
    # Only 1 of 3 trades in the 2-day default window ⇒ extend back to the day
    # of the 3rd-most-recent TRADE (day 2), not to any bar-count boundary.
    window = resolve_lockbox_window(
        np.arange(1, 11),
        evidence_days=np.array([2, 5, 10]),
        default_window_days=2,
        min_trades=3,
    )
    assert window == LockboxWindow(window_start=2, extended=True, n_evidence_in_window=3)


def test_resolver_evidence_boundary_day_ties_exceed_floor() -> None:
    # Several trades share the extension boundary day ⇒ the in-window count
    # may EXCEED min_trades (>= floor is allowed, never trimmed).
    window = resolve_lockbox_window(
        np.arange(1, 11),
        evidence_days=np.array([2, 2, 2, 5, 10]),
        default_window_days=2,
        min_trades=3,
    )
    assert window.window_start == 2
    assert window.extended is True
    assert window.n_evidence_in_window == 5


def test_resolver_evidence_insufficient_spans_all_days() -> None:
    window = resolve_lockbox_window(
        np.arange(1, 11), evidence_days=np.array([5]), default_window_days=2, min_trades=3
    )
    assert window == LockboxWindow(window_start=1, extended=True, n_evidence_in_window=1)


def test_resolver_empty_evidence_is_legal() -> None:
    # A zero-trade candidate is legal (review A2): the resolver falls to the
    # spans-everything arm with zero in-window evidence, never a ValueError.
    window = resolve_lockbox_window(
        np.arange(1, 11),
        evidence_days=np.array([], dtype=np.int64),
        default_window_days=5,
        min_trades=3,
    )
    assert window == LockboxWindow(window_start=1, extended=True, n_evidence_in_window=0)


def test_resolver_max_day_anchors_on_bar_days_not_evidence() -> None:
    # max_day is ALWAYS days[-1]: evidence ending earlier must not move the
    # default window start.
    window = resolve_lockbox_window(
        np.arange(1, 101), evidence_days=np.array([97, 98]), default_window_days=5, min_trades=2
    )
    assert window.window_start == 96  # 100 - 5 + 1, anchored on the BAR axis
    assert window.extended is False


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"default_window_days": 0, "min_trades": 1}, "default_window_days must be >= 1"),
        ({"default_window_days": 1, "min_trades": 0}, "min_trades must be >= 1"),
    ],
)
def test_resolver_param_validation(kwargs: dict[str, int], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        resolve_lockbox_window(np.arange(5), **kwargs)


@pytest.mark.parametrize(
    ("days", "match"),
    [
        (np.zeros((2, 2)), "days must be 1-D"),
        (np.array([], dtype=np.int64), "days must be non-empty"),
        (np.array([5, 1]), "days must be non-decreasing"),
    ],
)
def test_resolver_days_validation(days: np.ndarray, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        resolve_lockbox_window(days, default_window_days=5, min_trades=1)


@pytest.mark.parametrize(
    ("evidence", "match"),
    [
        (np.zeros((2, 2), dtype=np.int64), "evidence_days must be 1-D"),
        (np.array([5, 1]), "evidence_days must be non-decreasing"),
        (np.array([0, 5]), "must lie within the days span"),  # below days[0]
        (np.array([5, 11]), "must lie within the days span"),  # above days[-1]
    ],
)
def test_resolver_evidence_validation(evidence: np.ndarray, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        resolve_lockbox_window(
            np.arange(1, 11), evidence_days=evidence, default_window_days=5, min_trades=1
        )


# ── lockbox_evaluation with trade evidence (R2-SR-F2) ────────────────────────


def test_evidence_count_gate_counts_trades_not_bars() -> None:
    # THE R2-SR-F2 regression: 40 per-bar observations clear min_trades=4 by
    # BAR count (the old behaviour — count gate passes), but only 2 TRUE
    # trades exist ⇒ with evidence the verdict is INSUFFICIENT_N.
    returns = np.full(40, 0.01)
    days = np.arange(40)
    legacy = lockbox_evaluation(
        returns,
        days,
        training_sharpe=0.0,
        training_max_drawdown=10.0,
        min_trades=4,
        default_window_days=5,
    )
    assert legacy.count_gate_passed is True  # bars counted — the old illusion
    with_evidence = lockbox_evaluation(
        returns,
        days,
        training_sharpe=0.0,
        training_max_drawdown=10.0,
        min_trades=4,
        default_window_days=5,
        evidence_days=np.array([36, 38]),
    )
    assert with_evidence.status is LockboxStatus.INSUFFICIENT_N
    assert with_evidence.n_trades == 2
    assert with_evidence.count_gate_passed is False


def test_evidence_empty_yields_insufficient_n_zero_trades() -> None:
    # Review A2: a zero-trade candidate reaches filter #8 legally — empty
    # evidence must yield INSUFFICIENT_N with n_trades=0, never a ValueError.
    r = lockbox_evaluation(
        np.array([0.1, 0.2, 0.3]),
        np.array([1, 2, 3]),
        training_sharpe=0.0,
        training_max_drawdown=10.0,
        min_trades=1,
        default_window_days=2,
        evidence_days=(),
    )
    assert r.status is LockboxStatus.INSUFFICIENT_N
    assert r.n_trades == 0
    assert r.extended is True
    assert r.window_start_day == 1


def test_evidence_extension_driven_by_trade_days() -> None:
    # The window extends back to the day of the min_trades-th most-recent
    # TRADE while the Sharpe/MDD stay measured over the (bar) returns series
    # inside that window.
    returns = np.array([0.5, 0.1, 0.2, 0.15, 0.05])
    days = np.array([1, 2, 3, 4, 5])
    r = lockbox_evaluation(
        returns,
        days,
        training_sharpe=0.0,
        training_max_drawdown=10.0,
        min_trades=2,
        default_window_days=1,
        evidence_days=np.array([2, 5]),
    )
    assert r.extended is True
    assert r.window_start_day == 2
    assert r.n_trades == 2  # the TWO trades, not the four bars in-window
    assert r.count_gate_passed is True


def test_evidence_out_of_span_raises() -> None:
    with pytest.raises(ValueError, match="must lie within the days span"):
        lockbox_evaluation(
            np.array([0.1, 0.2]),
            np.array([5, 6]),
            training_sharpe=0.0,
            training_max_drawdown=1.0,
            min_trades=1,
            evidence_days=np.array([9]),
        )


def test_evidence_none_is_byte_identical_legacy() -> None:
    kw = {
        "training_sharpe": 0.2,
        "training_max_drawdown": 0.5,
        "min_trades": 4,
        "default_window_days": 4,
    }
    returns = np.array([0.3, -0.1, 0.2, 0.05, -0.15, 0.25, 0.1, 0.0])
    days = np.array([1, 1, 2, 3, 5, 5, 6, 8])
    assert lockbox_evaluation(returns, days, **kw) == lockbox_evaluation(  # type: ignore[arg-type]
        returns,
        days,
        evidence_days=None,
        **kw,  # type: ignore[arg-type]
    )


def test_metadata_echoed() -> None:
    r = lockbox_evaluation(
        np.array([0.1, 0.2, 0.3, 0.1, 0.2]),
        np.arange(5),
        training_sharpe=0.3,
        training_max_drawdown=2.0,
        min_trades=5,
        default_window_days=5,
    )
    assert r.training_sharpe == 0.3
    assert r.training_max_drawdown == 2.0
    assert r.min_trades == 5
