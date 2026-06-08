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
    evaluate_combo_walk_forward,
    verify_walk_forward_no_lookahead,
    walk_forward_sweep,
)
from brutex.feed.errors import LookAheadError
from brutex.feed.lookahead import LookAheadReport
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


def _leaky_schedule() -> WalkForwardSchedule:
    """A hand-built schedule whose training window reaches past its test start.

    ``train_end_bar = 10 > test_start_bar = 8`` — exactly the look-ahead a
    well-formed schedule can never produce, used to prove the P-06 barrier bites.
    """
    fold = WalkForwardFold(
        fold_index=0,
        train_start_day=0,
        train_end_day=5,
        test_start_day=4,
        test_end_day=9,
        train_start_bar=0,
        train_end_bar=10,
        test_start_bar=8,
        test_end_bar=18,
    )
    return WalkForwardSchedule(
        folds=(fold,),
        train_days=5,
        test_days=5,
        step_days=5,
        n_trading_days=9,
        n_bars=18,
        schedule_hash="0" * 64,
    )


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
