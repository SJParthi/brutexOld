"""Unit tests for walk-forward mode edges (DEC-WALK-FORWARD-OOS-001).

Covers the guard rails that the property/golden/integration suites do not reach by
construction: the empty-bars + invalid-window refusals of
:func:`~brutex.engine.stage1a.build_walk_forward_schedule`, the
:class:`~brutex.feed.lookahead.LookAheadBarrier` actually *tripping* on a
deliberately leaky hand-built schedule (proving the guard bites, not just decorates),
and the defensive ``LookAheadError`` re-raise in the two sweep-driver entry points.
These close the strict 100% line+branch ``brutex.engine`` gate for the new code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine import sweep_driver
from brutex.engine.stage1a import (
    WalkForwardFold,
    WalkForwardSchedule,
    build_walk_forward_schedule,
)
from brutex.engine.sweep_driver import (
    FoldResult,
    evaluate_combo_walk_forward,
    verify_walk_forward_no_lookahead,
    walk_forward_sweep,
)
from brutex.feed.errors import LookAheadError
from brutex.feed.lookahead import LookAheadReport
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: int, minute: int) -> CandleBar:
    ts = datetime(2026, 1, 5, 9, 16 + minute, tzinfo=_IST).astimezone(UTC) + timedelta(days=day)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


def _stream(n_days: int, bars_per_day: int = 2) -> list[CandleBar]:
    return [_bar(d, m) for d in range(n_days) for m in range(bars_per_day)]


def _schedule_of(*folds: WalkForwardFold, n_bars: int = 64) -> WalkForwardSchedule:
    """Wrap hand-built folds in a minimal schedule (window params are immaterial)."""
    return WalkForwardSchedule(
        folds=folds,
        train_days=5,
        test_days=5,
        step_days=5,
        n_trading_days=max(1, len(folds)),
        n_bars=n_bars,
        schedule_hash="0" * 64,
    )


def _fold(*, train_end_bar: int, test_start_bar: int, idx: int = 0) -> WalkForwardFold:
    """A fold pinning only the two bar bounds the look-ahead guard reads."""
    return WalkForwardFold(
        fold_index=idx,
        train_start_day=0,
        train_end_day=5,
        test_start_day=4,
        test_end_day=9,
        train_start_bar=0,
        train_end_bar=train_end_bar,
        test_start_bar=test_start_bar,
        test_end_bar=test_start_bar + 10,
    )


def _leaky_schedule() -> WalkForwardSchedule:
    """A hand-built schedule that leaks by EXACTLY ONE bar.

    ``train_end_bar = 9 > test_start_bar = 8`` — a 1-bar leak, the tightest case a
    well-formed schedule can never produce. The 1-bar gap (not 2) is deliberate:
    it pins the barrier cursor boundary so a ``test_start_bar - 1`` → ``test_start_bar``
    off-by-one mutant in the guard is killed (a 2-bar leak would let it survive).
    """
    return _schedule_of(_fold(train_end_bar=9, test_start_bar=8), n_bars=18)


# ── build_walk_forward_schedule guards ──


def test_empty_bars_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        build_walk_forward_schedule([])


def test_invalid_window_raises() -> None:
    # train_days < 1 is rejected by the shared window validator.
    with pytest.raises(ValueError, match="train_size"):
        build_walk_forward_schedule(_stream(20), train_days=0, test_days=5, step_days=5)


def test_too_few_days_raises() -> None:
    with pytest.raises(ValueError, match="too short"):
        build_walk_forward_schedule(_stream(6), train_days=10, test_days=5, step_days=5)


# ── the barrier actually trips on a leak ──


def test_verify_detects_leaky_schedule() -> None:
    report = verify_walk_forward_no_lookahead(_leaky_schedule())
    assert report.passed is False
    assert report.violation is not None
    assert "look-ahead" in report.violation


# ── defensive re-raise in the two entry points ──


def test_evaluate_combo_reraises_on_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = LookAheadReport(
        passed=False,
        indices_checked=1,
        max_index_touched=9,
        final_cursor=7,
        violation="boom",
    )
    monkeypatch.setattr(sweep_driver, "verify_walk_forward_no_lookahead", lambda schedule: failing)
    with pytest.raises(LookAheadError, match="P-06 re-check"):
        evaluate_combo_walk_forward(
            _stream(20),
            0,
            direction=Side.BUY,
            tf_minutes=1,
            train_days=10,
            test_days=5,
            step_days=5,
        )


def test_walk_forward_sweep_reraises_on_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = LookAheadReport(
        passed=False,
        indices_checked=1,
        max_index_touched=9,
        final_cursor=7,
        violation="boom",
    )
    monkeypatch.setattr(sweep_driver, "verify_walk_forward_no_lookahead", lambda schedule: failing)
    with pytest.raises(LookAheadError, match="P-06 re-check"):
        walk_forward_sweep(
            _stream(20),
            [0],
            direction=Side.BUY,
            tf_minutes=1,
            train_days=10,
            test_days=5,
            step_days=5,
        )


# ── malformed schedules → passed=False, NOT an uncaught ValueError ──


def test_verify_reports_negative_test_start_as_violation() -> None:
    # test_start_bar == 0 → cursor would be -1; the guard maps it to passed=False
    # rather than letting LookAheadBarrier.advance_to raise a bare ValueError.
    bad = _schedule_of(_fold(train_end_bar=0, test_start_bar=0))
    report = verify_walk_forward_no_lookahead(bad)
    assert report.passed is False
    assert report.violation is not None
    assert "malformed" in report.violation


def test_verify_reports_backward_folds_as_violation() -> None:
    # Two folds whose test_start_bar decreases (a backward cursor move) is
    # malformed → passed=False, not a ValueError traceback.
    bad = _schedule_of(
        _fold(train_end_bar=20, test_start_bar=20, idx=0),
        _fold(train_end_bar=10, test_start_bar=10, idx=1),
    )
    report = verify_walk_forward_no_lookahead(bad)
    assert report.passed is False
    assert report.violation is not None
    assert "malformed" in report.violation


def test_verify_passes_well_formed_contiguous_schedule() -> None:
    good = _schedule_of(
        _fold(train_end_bar=10, test_start_bar=10, idx=0),
        _fold(train_end_bar=20, test_start_bar=20, idx=1),
    )
    report = verify_walk_forward_no_lookahead(good)
    assert report.passed is True
    assert report.violation is None
    assert report.indices_checked == 2


# ── the P-06 trade-confinement guard (pillar 2b) ──


def _trade(entry: datetime, exit_: datetime) -> ClosedTrade:
    return ClosedTrade(
        pnl_paisa=50,
        mfe_paisa=50,
        mae_paisa=0,
        holding_bars=1,
        entry_ts=entry,
        exit_ts=exit_,
        direction=Side.BUY,
    )


def test_trades_confined_happy_path() -> None:
    bars = _stream(1, bars_per_day=4)  # 4 bars on one IST day
    # one trade entering bar 0, exiting bar 2 — fully inside the window
    trades = [_trade(bars[0].timestamp, bars[2].timestamp)]
    sweep_driver._assert_trades_confined(bars, trades)  # no raise


def test_trades_confined_rejects_out_of_window_trade() -> None:
    bars = _stream(1, bars_per_day=4)
    outside = _bar(99, 0)  # a bar whose timestamp is not in `bars`
    trades = [_trade(bars[0].timestamp, outside.timestamp)]
    with pytest.raises(LookAheadError, match="escapes its test window"):
        sweep_driver._assert_trades_confined(bars, trades)


def test_trades_confined_rejects_unordered_trades() -> None:
    bars = _stream(1, bars_per_day=6)
    # second trade exits BEFORE the first → not chronologically ordered by exit
    trades = [
        _trade(bars[0].timestamp, bars[4].timestamp),
        _trade(bars[1].timestamp, bars[2].timestamp),
    ]
    with pytest.raises(LookAheadError, match="not chronologically ordered"):
        sweep_driver._assert_trades_confined(bars, trades)


# ── _aggregate_folds: the aggregation layer (pass-rate + Sharpe summary) ──


def _metrics(sharpe: float | None) -> ResultMetrics:
    """A neutral ResultMetrics double varying only ``sharpe_ratio``."""
    return ResultMetrics(
        net_pnl_paisa=0,
        total_return_pct=0.0,
        cagr_pct=0.0,
        gross_profit_paisa=0,
        gross_loss_paisa=0,
        sharpe_ratio=sharpe,
        sortino_ratio=None,
        calmar_ratio=None,
        mar_ratio=None,
        information_ratio=None,
        omega_ratio=None,
        max_drawdown_pct=0.0,
        max_drawdown_paisa=0,
        max_drawdown_duration_bars=0,
        recovery_factor=None,
        ulcer_index=0.0,
        largest_losing_trade_paisa=0,
        max_consecutive_losses=0,
        max_consecutive_wins=0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=0.0,
        profit_factor=None,
        avg_winning_trade_paisa=0,
        avg_losing_trade_paisa=0,
        win_loss_ratio=None,
        expectancy_paisa=0,
        avg_mfe_paisa=0,
        avg_mae_paisa=0,
        avg_holding_time_bars=0.0,
        max_holding_time_bars=0,
        time_in_market_pct=0.0,
        tail_ratio=None,
        skewness=None,
        kurtosis=None,
        returns_std_dev=None,
        deflated_sharpe_ratio=None,
        pbo_probability=None,
    )


def _fold_result(idx: int, *, sharpe: float | None, passed: bool) -> FoldResult:
    return FoldResult(
        fold_index=idx,
        train_metrics=_metrics(0.0),
        test_metrics=_metrics(sharpe),
        test_gates_held=passed,
    )


def _n_fold_schedule(n: int) -> WalkForwardSchedule:
    return _schedule_of(*[_fold(train_end_bar=10, test_start_bar=10, idx=i) for i in range(n)])


def test_aggregate_pass_rate_and_distinct_sharpe_summary() -> None:
    # 5 folds, 3 pass → pass_rate 0.6 exactly; distinct Sharpes pin min != mean.
    folds = [
        _fold_result(0, sharpe=2.0, passed=True),
        _fold_result(1, sharpe=1.0, passed=True),
        _fold_result(2, sharpe=-1.0, passed=False),
        _fold_result(3, sharpe=4.0, passed=True),
        _fold_result(4, sharpe=3.0, passed=False),
    ]
    result = sweep_driver._aggregate_folds(_n_fold_schedule(5), folds, "h" * 64)
    assert result.folds_passed == 3
    assert result.pass_rate == 0.6
    # mean = (2+1-1+4+3)/5 = 1.8 (NOT max 4.0 → kills sum→max); min = -1.0 (NOT max)
    assert result.mean_test_sharpe == 1.8
    assert result.min_test_sharpe == -1.0
    assert result.held_up is True  # 0.6 >= 0.6 threshold (boundary)


def test_aggregate_held_up_false_just_below_threshold() -> None:
    # 2 of 5 pass → pass_rate 0.4 < 0.6 → held_up False (kills >= → > only if
    # paired with the boundary test above; together they pin the comparator).
    folds = [
        _fold_result(0, sharpe=1.0, passed=True),
        _fold_result(1, sharpe=1.0, passed=True),
        _fold_result(2, sharpe=1.0, passed=False),
        _fold_result(3, sharpe=1.0, passed=False),
        _fold_result(4, sharpe=1.0, passed=False),
    ]
    result = sweep_driver._aggregate_folds(_n_fold_schedule(5), folds, "h" * 64)
    assert result.pass_rate == 0.4
    assert result.held_up is False
    assert result.pass_rate < sweep_driver.WALK_FORWARD_PASS_RATE_THRESHOLD


def test_aggregate_all_none_sharpe_yields_none_summary() -> None:
    folds = [_fold_result(i, sharpe=None, passed=False) for i in range(3)]
    result = sweep_driver._aggregate_folds(_n_fold_schedule(3), folds, "h" * 64)
    assert result.mean_test_sharpe is None
    assert result.min_test_sharpe is None
    assert result.pass_rate == 0.0
    assert result.held_up is False
