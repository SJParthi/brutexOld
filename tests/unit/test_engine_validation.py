"""Unit tests for the Stage-3 out-of-sample validator (DEC-OOS-VALIDATION-001).

Covers the IST day-boundary split (``_day_split``: range/min-days guards, boundary
correctness, clamping) and ``validate_out_of_sample`` (empty guard, the train/test
split + ``held_up`` wiring, the no-trade combo, and determinism) for the strict
100% ``brutex.engine`` package gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.validation import (
    OOSValidation,
    _day_split,
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
        # held_up is EXACTLY the test-period hard-gate verdict (the wiring contract).
        assert result.held_up == passes_gates(result.test_metrics)

    def test_no_trade_combo_does_not_hold_up(self) -> None:
        bars = _multiday(4, bars_per_day=5)
        result = validate_out_of_sample(bars, _IMPOSSIBLE_MASK, direction=Side.BUY, tf_minutes=1)
        # The self-contradictory mask never fires → 0 test trades → fails the gates.
        assert result.test_metrics.total_trades == 0
        assert result.held_up is False

    def test_deterministic(self) -> None:
        # Same inputs → bit-identical result (idempotence, Golden Rule #7).
        bars = _multiday(3, bars_per_day=5)
        a = validate_out_of_sample(bars, 0, direction=Side.BUY, tf_minutes=1, train_frac=0.6)
        b = validate_out_of_sample(bars, 0, direction=Side.BUY, tf_minutes=1, train_frac=0.6)
        assert a == b
