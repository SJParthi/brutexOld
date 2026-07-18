"""Unit tests for the Stage-3 out-of-sample validator (DEC-OOS-VALIDATION-001).

Covers the IST day-boundary split (``_day_split``: range/min-days guards, boundary
correctness, clamping) and ``validate_out_of_sample`` (empty guard, the train/test
split + ``held_up`` wiring, the no-trade combo, and determinism) for the strict
100% ``brutex.engine`` package gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from brutex.data.models import CandleBar
from brutex.engine.validation import (
    OOSValidation,
    _day_split,
    _drawdown_held,
    _sharpe_held,
    validate_out_of_sample,
)
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import passes_gates
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))

#: All-52-bits mask is self-contradictory (bullish AND bearish) → never fires.
_IMPOSSIBLE_MASK = (1 << 52) - 1


def _bar(day: int, minute: int, base: int) -> CandleBar:
    """One IST-session bar on day ``2026-05-(13+day)`` at ``09:16+minute``."""
    ts = datetime(2026, 5, 13 + day, 9, 16 + minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(f"{base}.00"),
        high=Decimal(f"{base + 1}.00"),
        low=Decimal(f"{base - 1}.00"),
        close=Decimal(f"{base}.50"),
        volume=100,
        open_interest=None,
    )


def _multiday(n_days: int, bars_per_day: int = 4) -> list[CandleBar]:
    """``n_days`` distinct IST days, each with ``bars_per_day`` bars (slight drift)."""
    return [_bar(d, m, 100 + d) for d in range(n_days) for m in range(bars_per_day)]


def _m(**overrides: Any) -> ResultMetrics:
    """A ResultMetrics with neutral defaults; override the fields under test."""
    base: dict[str, Any] = {
        "net_pnl_paisa": 0,
        "total_return_pct": 0.0,
        "cagr_pct": 0.0,
        "gross_profit_paisa": 0,
        "gross_loss_paisa": 0,
        "sharpe_ratio": 1.0,
        "sortino_ratio": 1.0,
        "calmar_ratio": 1.0,
        "mar_ratio": 0.0,
        "information_ratio": 0.0,
        "omega_ratio": 0.0,
        "max_drawdown_pct": 10.0,
        "max_drawdown_paisa": 0,
        "max_drawdown_duration_bars": 0,
        "recovery_factor": 1.0,
        "ulcer_index": 0.0,
        "largest_losing_trade_paisa": 0,
        "max_consecutive_losses": 2,
        "max_consecutive_wins": 0,
        "total_trades": 50,
        "winning_trades": 30,
        "losing_trades": 20,
        "win_rate": 0.6,
        "profit_factor": 1.5,
        "avg_winning_trade_paisa": 0,
        "avg_losing_trade_paisa": 0,
        "win_loss_ratio": 1.5,
        "expectancy_paisa": 0,
        "avg_mfe_paisa": 0,
        "avg_mae_paisa": 0,
        "avg_holding_time_bars": 0.0,
        "max_holding_time_bars": 0,
        "time_in_market_pct": 0.0,
        "tail_ratio": 0.0,
        "skewness": 0.0,
        "kurtosis": 0.0,
        "returns_std_dev": 0.0,
        # The loss/growth trio (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
        # DEC-METRICS-KELLY-GROWTH-001): loss_rate consistent with the
        # counts above (losing 20 / total 50); the Kelly pair defaults
        # None (undefined) — override per test where the value matters.
        "loss_rate": 0.4,
        "kelly_fraction": None,
        "log_growth_at_half_kelly": None,
    }
    base.update(overrides)
    return ResultMetrics(**base)


class TestDaySplit:
    def test_rejects_train_frac_out_of_range(self) -> None:
        bars = _multiday(3)
        for bad in (0.0, 1.0, -0.1, 1.5):
            with pytest.raises(ValueError, match="train_frac must be in"):
                _day_split(bars, bad)

    def test_rejects_fewer_than_two_days(self) -> None:
        with pytest.raises(ValueError, match="need >= 2 distinct trading days"):
            _day_split(_multiday(1), 0.7)

    def test_splits_at_day_boundary(self) -> None:
        bars = _multiday(4, bars_per_day=5)  # 20 bars over 4 days; day_starts 0,5,10,15
        split, train_days, test_days = _day_split(bars, 0.7)  # round(2.8)=3 train days
        assert (train_days, test_days) == (3, 1)
        assert split == 15
        # The split bar opens a FRESH IST day vs the bar before it.
        before = bars[split - 1].timestamp.astimezone(_IST).date()
        at = bars[split].timestamp.astimezone(_IST).date()
        assert at != before

    def test_clamps_to_at_least_one_day_each(self) -> None:
        bars = _multiday(2, bars_per_day=3)
        # 0.9*2 → round 2 → clamped to n_days-1=1 train day (test keeps a whole day).
        assert _day_split(bars, 0.9) == (3, 1, 1)
        # tiny frac → round 0 → clamped UP to 1 train day.
        assert _day_split(_multiday(3, bars_per_day=2), 0.1) == (2, 1, 2)


class TestValidateOOS:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            validate_out_of_sample([], 0, direction=Side.BUY, tf_minutes=1)

    def test_runs_split_and_wires_held_up(self) -> None:
        bars = _multiday(4, bars_per_day=5)
        result = validate_out_of_sample(bars, 0, direction=Side.BUY, tf_minutes=1)
        assert isinstance(result, OOSValidation)
        assert isinstance(result.train_metrics, ResultMetrics)
        assert isinstance(result.test_metrics, ResultMetrics)
        assert result.train_days + result.test_days == 4
        # gates_held is the test-period hard-gate verdict; held_up ANDs all three.
        assert result.gates_held == passes_gates(result.test_metrics)
        assert result.held_up == (result.gates_held and result.sharpe_held and result.drawdown_held)

    def test_no_trade_combo_does_not_hold_up(self) -> None:
        bars = _multiday(4, bars_per_day=5)
        result = validate_out_of_sample(bars, _IMPOSSIBLE_MASK, direction=Side.BUY, tf_minutes=1)
        # The self-contradictory mask never fires → 0 test trades → fails the gates.
        assert result.test_metrics.total_trades == 0
        assert result.gates_held is False
        assert result.held_up is False

    def test_deterministic(self) -> None:
        # Same inputs → bit-identical result (idempotence, Golden Rule #7).
        bars = _multiday(3, bars_per_day=5)
        a = validate_out_of_sample(bars, 0, direction=Side.BUY, tf_minutes=1, train_frac=0.6)
        b = validate_out_of_sample(bars, 0, direction=Side.BUY, tf_minutes=1, train_frac=0.6)
        assert a == b


class TestSharpeHeld:
    def test_none_train_sharpe_fails(self) -> None:
        # No in-sample Sharpe to hold up → cannot validate.
        assert _sharpe_held(_m(sharpe_ratio=None), _m(sharpe_ratio=2.0)) is False

    def test_nonpositive_train_sharpe_fails(self) -> None:
        assert _sharpe_held(_m(sharpe_ratio=0.0), _m(sharpe_ratio=2.0)) is False

    def test_none_test_sharpe_fails(self) -> None:
        assert _sharpe_held(_m(sharpe_ratio=1.0), _m(sharpe_ratio=None)) is False

    def test_within_tolerance_holds(self) -> None:
        # train 1.0 → floor 0.70; test 0.75 >= 0.70 → holds.
        assert _sharpe_held(_m(sharpe_ratio=1.0), _m(sharpe_ratio=0.75)) is True

    def test_below_tolerance_fails(self) -> None:
        # test 0.69 < 0.70 floor → degraded too far.
        assert _sharpe_held(_m(sharpe_ratio=1.0), _m(sharpe_ratio=0.69)) is False


class TestDrawdownHeld:
    def test_within_tolerance_holds(self) -> None:
        # train 10% → ceiling 15%; test 14% <= 15% → holds.
        assert _drawdown_held(_m(max_drawdown_pct=10.0), _m(max_drawdown_pct=14.0)) is True

    def test_worse_than_tolerance_fails(self) -> None:
        # test 16% > 15% ceiling → blew the drawdown budget.
        assert _drawdown_held(_m(max_drawdown_pct=10.0), _m(max_drawdown_pct=16.0)) is False
