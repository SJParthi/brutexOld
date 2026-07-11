"""Unit tests for the CPCV robustness wiring (DEC-CPCV-ROBUSTNESS-001).

Covers ``cpcv_robustness``: the too-short → ``None`` guard (never a crash), a robust
all-positive series (passes most folds), a fail-everywhere all-negative series, and a
custom ``n_groups`` that lowers the fold-count threshold — for the strict 100%
``brutex.engine`` package gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.robustness import combo_returns, cpcv_robustness
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(minute: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """One IST-session 1-minute bar (stored UTC, Decimal rupee prices)."""
    ts = datetime(2026, 5, 13, 9, minute, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=100,
        open_interest=None,
    )


def _session_bars() -> list[CandleBar]:
    """A rising-then-falling 5-bar session (entry bar1 open 100.50 → exit 99.20)."""
    return [
        _bar(16, "100.00", "101.00", "99.50", "100.50"),
        _bar(17, "100.50", "102.00", "100.00", "101.50"),
        _bar(18, "101.50", "101.80", "100.20", "100.40"),
        _bar(19, "100.40", "100.90", "99.80", "100.10"),
        _bar(20, "100.10", "100.30", "99.00", "99.20"),
    ]


class TestCpcvRobustness:
    def test_too_few_returns_is_undefined(self) -> None:
        # Fewer than n_groups (16) returns → can't form the folds → None, not a crash.
        assert cpcv_robustness([0.01] * 5) is None

    def test_all_positive_returns_pass_most_folds(self) -> None:
        # 20 varied POSITIVE returns → every fold's held-out mean is positive (with
        # nonzero variance) → a high pass-rate in (0, 1].
        returns = [float(i % 5 + 1) for i in range(20)]
        rate = cpcv_robustness(returns)
        assert rate is not None
        assert 0.0 < rate <= 1.0

    def test_all_negative_returns_fail_every_fold(self) -> None:
        # 20 varied NEGATIVE returns → every fold's held-out Sharpe <= 0 → 0.0.
        returns = [-float(i % 5 + 1) for i in range(20)]
        assert cpcv_robustness(returns) == 0.0

    def test_custom_groups_lowers_the_threshold(self) -> None:
        # With n_groups=4 a 5-return series is now long enough → defined (not None).
        rate = cpcv_robustness([1.0, -1.0, 2.0, -2.0, 3.0], n_groups=4, n_test_groups=2)
        assert rate is not None
        assert 0.0 <= rate <= 1.0


class TestComboReturns:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            combo_returns([], 0, direction=Side.BUY, tf_minutes=1)

    def test_buy_on_falling_tail_loses(self) -> None:
        # mask=0 → one long held to end-of-data; the falling tail (entry 100.50 →
        # exit 99.20) yields a single NEGATIVE per-trade return.
        rets = combo_returns(
            _session_bars(), 0, direction=Side.BUY, tf_minutes=1, start_equity_paisa=1_000
        )
        assert len(rets) == 1
        assert rets[0] < 0.0

    def test_sell_on_falling_tail_profits(self) -> None:
        # The mirror short profits as price falls → a single POSITIVE return.
        rets = combo_returns(
            _session_bars(), 0, direction=Side.SELL, tf_minutes=1, start_equity_paisa=1_000
        )
        assert len(rets) == 1
        assert rets[0] > 0.0

    def test_roll_schedule_reaches_the_stage1a_build(self) -> None:
        # ADR-0012 pass-through (review round-2 finding 2): pre-fix
        # combo_returns accepted NO roll_schedule — schedule-blind bar_bits
        # were the only possible CPCV input for roll-stitched futures bars.
        # A schedule ending before the bars now provably reaches Stage-1A.
        bars = _session_bars()
        stale = (bars[0].timestamp.astimezone(_IST).date().toordinal() - 1,)
        with pytest.raises(ValueError, match="beyond the last roll_schedule expiry"):
            combo_returns(bars, 0, direction=Side.BUY, tf_minutes=1, roll_schedule=stale)

    def test_covering_schedule_and_defaults_stay_the_spot_path(self) -> None:
        bars = _session_bars()
        covering = (bars[-1].timestamp.astimezone(_IST).date().toordinal(),)
        base = combo_returns(bars, 0, direction=Side.BUY, tf_minutes=1)
        explicit = combo_returns(
            bars, 0, direction=Side.BUY, tf_minutes=1, roll_schedule=None, apply_1day_mask=False
        )
        scheduled = combo_returns(bars, 0, direction=Side.BUY, tf_minutes=1, roll_schedule=covering)
        assert explicit == base  # the defaults ARE the spot path
        # A single covering contract has no roll boundary → same trades.
        assert scheduled == base
