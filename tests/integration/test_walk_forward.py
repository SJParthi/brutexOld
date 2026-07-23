"""Integration test for engine walk-forward mode on fake bars (DEC-WALK-FORWARD-OOS-001).

Drives the full vertical slice — schedule build → P-06 look-ahead re-check →
per-fold ``bar_bits`` → trade sim → metrics → aggregate — over a deterministic
multi-day fake 1-minute stream, exercising :func:`evaluate_combo_walk_forward`
and :func:`walk_forward_sweep` end-to-end (no mocks; the real engine bricks).

Asserts the contract the walk-forward mode promises:

* multiple held-out folds are scored (a single OOS split would be one block);
* each fold carries directly-comparable train + test metrics;
* the aggregate pass-rate is a probability and the Sharpe summary is consistent;
* the run-hash is a 64-hex digest that **changes when the fold schedule changes**
  (reproducibility binds a result to its exact layout, Golden Rule #4);
* the schedule passes the P-06 :class:`LookAheadBarrier` re-check;
* ``walk_forward_sweep`` over many masks reuses one schedule + one ``bar_bits``
  build and agrees with the single-combo evaluator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.sweep_driver import (
    evaluate_combo_walk_forward,
    verify_walk_forward_no_lookahead,
    walk_forward_sweep,
)
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))

#: A self-contradictory mask (all 52 bits) never fires → deterministic no-trade.
_IMPOSSIBLE_MASK = (1 << 52) - 1


def _bar(day: int, minute: int, base: int) -> CandleBar:
    ts = datetime(2026, 1, 5, 9, 16 + minute, tzinfo=_IST).astimezone(UTC) + timedelta(days=day)
    # A gentle intraday zig-zag so trades open and close with non-trivial P&L.
    drift = Decimal(minute) * Decimal("0.10")
    return CandleBar(
        timestamp=ts,
        open=Decimal(f"{base}") + drift,
        high=Decimal(f"{base}") + drift + Decimal("1.00"),
        low=Decimal(f"{base}") + drift - Decimal("1.00"),
        close=Decimal(f"{base}") + drift + Decimal("0.50"),
        volume=100,
        open_interest=None,
    )


def _fake_stream(n_days: int = 30, bars_per_day: int = 6) -> list[CandleBar]:
    """``n_days`` distinct IST sessions, each ``bars_per_day`` one-minute bars."""
    return [_bar(d, m, 100 + (d % 5)) for d in range(n_days) for m in range(bars_per_day)]


def test_evaluate_combo_walk_forward_scores_every_fold() -> None:
    bars = _fake_stream(n_days=30)
    result = evaluate_combo_walk_forward(
        bars,
        required_mask=0,  # enter every bar
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
    )
    # 30 days, train=10/test=5/step=5 → test starts at 10,15,20,25 → 4 folds.
    assert result.n_folds == 4
    assert len(result.fold_results) == 4
    assert result.schedule.n_trading_days == 30

    for i, fr in enumerate(result.fold_results):
        assert fr.fold_index == i
        # both windows produced a metrics row from the identical core
        assert fr.train_metrics.total_trades >= 0
        assert fr.test_metrics.total_trades >= 0
        assert isinstance(fr.test_gates_held, bool)

    # aggregate consistency
    assert 0.0 <= result.pass_rate <= 1.0
    assert result.folds_passed == sum(fr.test_gates_held for fr in result.fold_results)
    assert result.pass_rate == result.folds_passed / result.n_folds
    assert result.held_up == (result.pass_rate >= 0.6)
    if result.mean_test_sharpe is not None:
        assert result.min_test_sharpe is not None
        assert result.min_test_sharpe <= result.mean_test_sharpe
    assert len(result.run_hash) == 64


def test_schedule_passes_lookahead_barrier() -> None:
    bars = _fake_stream(n_days=25)
    result = evaluate_combo_walk_forward(
        bars,
        required_mask=0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
    )
    report = verify_walk_forward_no_lookahead(result.schedule)
    assert report.passed is True
    assert report.violation is None
    # the barrier touched one training index per fold
    assert report.indices_checked == result.n_folds


def test_run_hash_changes_with_schedule_but_is_otherwise_stable() -> None:
    bars = _fake_stream(n_days=30)
    a = evaluate_combo_walk_forward(
        bars,
        required_mask=0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
    )
    a_again = evaluate_combo_walk_forward(
        bars,
        required_mask=0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
    )
    # reproducible: identical bars + params + schedule → identical run-hash
    assert a.run_hash == a_again.run_hash
    assert a.schedule.schedule_hash == a_again.schedule.schedule_hash

    # change ONLY the fold schedule (train window) → run-hash must change
    b = evaluate_combo_walk_forward(
        bars,
        required_mask=0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=12,
        test_days=5,
        step_days=5,
    )
    assert b.schedule.schedule_hash != a.schedule.schedule_hash
    assert b.run_hash != a.run_hash


def test_no_trade_mask_holds_up_false_but_well_formed() -> None:
    bars = _fake_stream(n_days=25)
    result = evaluate_combo_walk_forward(
        bars,
        required_mask=_IMPOSSIBLE_MASK,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
    )
    # no trades anywhere → no fold clears the trade-count gate
    assert result.folds_passed == 0
    assert result.pass_rate == 0.0
    assert result.held_up is False
    for fr in result.fold_results:
        assert fr.test_metrics.total_trades == 0


def test_walk_forward_sweep_matches_single_combo() -> None:
    bars = _fake_stream(n_days=30)
    masks = [0, _IMPOSSIBLE_MASK]
    swept = walk_forward_sweep(
        bars,
        masks,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=10,
        test_days=5,
        step_days=5,
    )
    assert [m for m, _ in swept] == masks
    for mask, swept_result in swept:
        single = evaluate_combo_walk_forward(
            bars,
            required_mask=mask,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            train_days=10,
            test_days=5,
            step_days=5,
        )
        # the sweep path and the single-combo path agree bit-for-bit
        assert swept_result.run_hash == single.run_hash
        assert swept_result.pass_rate == single.pass_rate
        assert swept_result.fold_results == single.fold_results


def test_too_few_days_raises() -> None:
    bars = _fake_stream(n_days=8)  # < train(10) + test(5)
    with pytest.raises(ValueError, match="too short"):
        evaluate_combo_walk_forward(
            bars,
            required_mask=0,
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            train_days=10,
            test_days=5,
            step_days=5,
        )


def test_empty_bars_raise() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        evaluate_combo_walk_forward(
            [], required_mask=0, direction=Side.BUY, tf_minutes=1, exec_bars_1m=[]
        )
    with pytest.raises(ValueError, match="non-empty"):
        walk_forward_sweep([], [0], direction=Side.BUY, tf_minutes=1, exec_bars_1m=[])
