"""Property invariants for the walk-forward fold schedule (DEC-WALK-FORWARD-OOS-001).

Proves, for *every* valid window configuration over a synthetic multi-day stream,
the properties the engine walk-forward mode relies on:

1. **No leak (causality).** Each fold's training window ends no later than its
   held-out test window begins — at both the trading-day and the bar level
   (``train_end_day <= test_start_day`` and ``train_end_bar <= test_start_bar``).
   This is the core no-look-ahead guarantee; it is *also* re-proved at runtime by
   the P-06 :class:`~brutex.feed.lookahead.LookAheadBarrier` via
   :func:`~brutex.engine.sweep_driver.verify_walk_forward_no_lookahead`.
2. **Exact window sizes.** Every fold's training window spans exactly
   ``train_days`` trading days and its test window exactly ``test_days``.
3. **Chronological, evenly-stepped folds.** Successive folds shift their test
   window start by exactly ``step_days`` trading days.
4. **Clean partition when ``step == test``.** The held-out test windows tile the
   covered range with no overlap and no gap (the seasonal-coverage guarantee —
   audit Gap #4).
5. **Disjoint test windows when ``step >= test``.** No bar belongs to two folds'
   test windows.
6. **In-bounds, non-empty slices.** Every bar boundary lies in ``[0, n_bars]`` and
   every train/test slice is non-empty.
7. **Hash determinism / sensitivity.** Rebuilding an identical stream + windows
   yields an identical ``schedule_hash``; a schedule with different boundaries
   (almost surely) yields a different hash, and the no-look-ahead re-check passes
   for every generated schedule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from itertools import pairwise

import pytest
from brutex.data.models import CandleBar
from brutex.engine.stage1a import build_walk_forward_schedule, trading_day_starts
from brutex.engine.sweep_driver import (
    evaluate_combo_walk_forward,
    verify_walk_forward_no_lookahead,
)
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: int, minute: int) -> CandleBar:
    """One IST bar on trading day ``day`` (distinct calendar dates, contiguous)."""
    ts = datetime(2026, 1, 5, 9, 16 + minute, tzinfo=_IST).astimezone(UTC) + timedelta(days=day)
    base = 100 + (day % 11)
    return CandleBar(
        timestamp=ts,
        open=Decimal(f"{base}.00"),
        high=Decimal(f"{base + 1}.00"),
        low=Decimal(f"{base - 1}.00"),
        close=Decimal(f"{base}.50"),
        volume=10,
        open_interest=None,
    )


def _make_bars(n_days: int, bars_per_day: int) -> list[CandleBar]:
    return [_bar(d, m) for d in range(n_days) for m in range(bars_per_day)]


# Window parameters bounded small so the property suite stays fast; the windows
# are sized in trading days and mapped onto a synthetic day grid.
_days = st.integers(min_value=2, max_value=40)
_bpd = st.integers(min_value=1, max_value=4)
_train = st.integers(min_value=1, max_value=20)
_test = st.integers(min_value=1, max_value=10)
_step = st.integers(min_value=1, max_value=10)


@settings(max_examples=250, deadline=None)
@given(n_days=_days, bpd=_bpd, train=_train, test=_test, step=_step)
def test_schedule_invariants(n_days: int, bpd: int, train: int, test: int, step: int) -> None:
    bars = _make_bars(n_days, bpd)
    day_starts = trading_day_starts(bars)
    assert day_starts == sorted(set(day_starts))  # strictly increasing
    assert len(day_starts) == n_days

    if n_days < train + test:
        # Too short for even one fold → the builder refuses (honest envelope).
        with pytest.raises(ValueError):
            build_walk_forward_schedule(bars, train_days=train, test_days=test, step_days=step)
        return

    schedule = build_walk_forward_schedule(bars, train_days=train, test_days=test, step_days=step)
    assert schedule.n_folds >= 1
    assert schedule.n_trading_days == n_days
    assert schedule.n_bars == len(bars)

    test_day_ranges: list[tuple[int, int]] = []
    for i, fold in enumerate(schedule.folds):
        assert fold.fold_index == i
        # (2) exact window sizes (in trading days)
        assert fold.train_end_day - fold.train_start_day == train
        assert fold.test_end_day - fold.test_start_day == test
        # (1) no leak: train ends exactly at test start (contiguous, no embargo)
        assert fold.train_end_day == fold.test_start_day
        assert fold.train_end_bar == fold.test_start_bar
        assert fold.train_end_bar <= fold.test_start_bar
        # (6) in-bounds, non-empty slices
        assert 0 <= fold.train_start_bar < fold.train_end_bar <= schedule.n_bars
        assert 0 <= fold.test_start_bar < fold.test_end_bar <= schedule.n_bars
        # day boundaries map to the recorded day-start indices
        assert fold.train_start_bar == day_starts[fold.train_start_day]
        assert fold.test_start_bar == day_starts[fold.test_start_day]
        test_day_ranges.append((fold.test_start_day, fold.test_end_day))

    # (3) evenly-stepped, chronological
    for prev, nxt in pairwise(schedule.folds):
        assert nxt.test_start_day - prev.test_start_day == step
        assert nxt.test_start_bar >= prev.test_start_bar

    # (5) disjoint test windows when step >= test
    if step >= test:
        covered: set[int] = set()
        for fold in schedule.folds:
            window = set(range(fold.test_start_bar, fold.test_end_bar))
            assert covered.isdisjoint(window)
            covered |= window

    # (5b) overlap regime: when step < test, consecutive test windows MUST overlap
    # by exactly (test - step) trading days (not merely "did not crash").
    if step < test and schedule.n_folds >= 2:
        for prev, nxt in pairwise(schedule.folds):
            overlap_days = prev.test_end_day - nxt.test_start_day
            assert overlap_days == test - step
            assert overlap_days > 0

    # (4) clean partition (no overlap, no gap) when step == test
    if step == test:
        for (_, prev_end), (next_start, _) in pairwise(test_day_ranges):
            assert prev_end == next_start
        first_start = test_day_ranges[0][0]
        last_end = test_day_ranges[-1][1]
        assert first_start == train  # first test starts right after the first train block
        # contiguous cover of [first_start, last_end)
        covered_days = set()
        for start, end in test_day_ranges:
            covered_days |= set(range(start, end))
        assert covered_days == set(range(first_start, last_end))


@settings(max_examples=150, deadline=None)
@given(n_days=_days, bpd=_bpd, train=_train, test=_test, step=_step)
def test_hash_determinism_and_lookahead(
    n_days: int, bpd: int, train: int, test: int, step: int
) -> None:
    bars = _make_bars(n_days, bpd)
    if n_days < train + test:
        return
    a = build_walk_forward_schedule(bars, train_days=train, test_days=test, step_days=step)
    b = build_walk_forward_schedule(bars, train_days=train, test_days=test, step_days=step)
    # determinism: identical inputs → identical hash + folds
    assert a.schedule_hash == b.schedule_hash
    assert a.folds == b.folds
    assert len(a.schedule_hash) == 64
    # the P-06 barrier re-check passes for every well-formed schedule
    assert verify_walk_forward_no_lookahead(a).passed is True


@settings(max_examples=80, deadline=None)
@given(n_days=st.integers(min_value=12, max_value=40), bpd=_bpd)
def test_hash_sensitive_to_window_change(n_days: int, bpd: int) -> None:
    bars = _make_bars(n_days, bpd)
    base = build_walk_forward_schedule(bars, train_days=5, test_days=3, step_days=3)
    other = build_walk_forward_schedule(bars, train_days=6, test_days=3, step_days=3)
    # Different train window ⇒ different fold boundaries ⇒ different hash.
    if base.folds != other.folds:
        assert base.schedule_hash != other.schedule_hash


def _perturb_tail(bars: list[CandleBar], from_index: int) -> list[CandleBar]:
    """Return a copy of ``bars`` with every bar at index >= ``from_index`` shifted.

    Bumps the price of the tail bars by a large constant (preserving OHLC ordering
    + IST dates so the schedule is identical), to test that NO earlier fold's
    metrics depend on those future bars.
    """
    bump = Decimal("17.00")
    out: list[CandleBar] = []
    for i, bar in enumerate(bars):
        if i < from_index:
            out.append(bar)
            continue
        out.append(
            CandleBar(
                timestamp=bar.timestamp,
                open=bar.open + bump,
                high=bar.high + bump,
                low=bar.low + bump,
                close=bar.close + bump,
                volume=bar.volume,
                open_interest=bar.open_interest,
            )
        )
    return out


@settings(max_examples=40, deadline=None)
@given(
    n_days=st.integers(min_value=10, max_value=24),
    bpd=st.integers(min_value=2, max_value=4),
    train=st.integers(min_value=2, max_value=6),
    test=st.integers(min_value=2, max_value=4),
)
def test_no_lookahead_counterfactual(n_days: int, bpd: int, train: int, test: int) -> None:
    """The REAL no-look-ahead proof: perturbing bars AFTER a fold's ``test_end_bar``
    must leave that fold's train + test metrics byte-identical.

    This is the substantive causality guarantee (module note pillar 1) — stronger
    than the structural P-06 barrier re-check: a centred window, a "next-day-open"
    anchor, or any forward peek in the bit-matrix would change an earlier fold's
    metrics when the future is mutated, and this would catch it.
    """
    step = test  # clean partition keeps folds independent for the comparison
    bars = _make_bars(n_days, bpd)
    if n_days < train + test:
        return
    baseline = evaluate_combo_walk_forward(
        bars,
        0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        train_days=train,
        test_days=test,
        step_days=step,
    )
    # Perturb everything strictly after the FIRST fold's test window.
    cut = baseline.schedule.folds[0].test_end_bar
    if cut >= len(bars):
        return  # only one fold spanning the whole stream; nothing "after" to perturb
    perturbed_bars = _perturb_tail(bars, cut)
    perturbed = evaluate_combo_walk_forward(
        perturbed_bars,
        0,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=perturbed_bars,
        train_days=train,
        test_days=test,
        step_days=step,
    )
    # The schedule (bar boundaries) is unchanged by a price-only perturbation.
    assert perturbed.schedule.folds == baseline.schedule.folds
    # Fold 0 lies entirely before the cut → its metrics must be identical.
    assert perturbed.fold_results[0] == baseline.fold_results[0]
