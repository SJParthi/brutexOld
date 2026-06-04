"""Stage-3 out-of-sample validation — does a winning combo hold up on unseen data?

The sweep + ranking FIND a top combo on the history. But a combo that looks great
in-sample can be a data-mining fluke (the #1 risk). The honest test: split the
history chronologically into a TRAIN period (the past) and a TEST period (the
future the combo never saw), measure the SAME combo on both, and ask — does it
still clear the hard rejection gates out-of-sample?

This is the first slice of Stage-3 (the real-vs-illusion decider). It reuses the
proven engine core (``build_bar_bits`` → the shared ``metrics_for_mask``) and the
ranking hard gates (``passes_gates``); it does NOT re-fit anything.

No look-ahead, no anchor artifact (the careful part — Golden Rule #2/#5)
-----------------------------------------------------------------------
``bar_bits`` is built ONCE over the FULL stream, so every bar's prior-day anchors
(pivots / Fib / PDH-PDL) come from its real prior IST day — including the test
period's first day, whose prior day is the last train day. The split is then
applied to the *trade simulation*, at an IST DAY BOUNDARY, so the test period
begins at a fresh trading day — matching the per-day running-extreme resets inside
``build_bar_bits``. Building ``bar_bits`` per-segment instead would CLEAR the test
period's first-day anchors (a subtle correctness leak); building once and slicing
avoids it.

Honest envelope (Golden Rule #10)
---------------------------------
* The combo is GIVEN (already chosen by the caller); this measures it on train vs
  test — it does not itself do the selection. The OOS *validity* comes from the
  caller selecting the combo on the train period only; the full train/test
  orchestration is the cascade's job — this is the holdout primitive it builds on.
* ``held_up`` is the basic OOS gate: the combo still clears the 5 hard rejection
  gates on the test period. The fuller lockbox tolerance (test Sharpe within 30%
  of train, drawdown within 50% — :mod:`brutex.metrics.lockbox`) + CPCV / DSR are
  the next refinements.
* A position still open at the train-period boundary is force-closed at that
  period's end (each segment is a self-contained walk) — standard for a split.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import DEFAULT_START_EQUITY_PAISA, metrics_for_mask
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import passes_gates
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

#: A train/test split needs at least this many distinct IST trading days (one
#: whole day for each side).
_MIN_SPLIT_DAYS: Final[int] = 2


@dataclass(frozen=True, slots=True)
class OOSValidation:
    """A combo's in-sample vs out-of-sample result + the holdout verdict.

    Attributes
    ----------
    train_metrics, test_metrics
        The combo's metrics on the train (earlier days) and test (later, unseen
        days) periods.
    train_days, test_days
        Distinct IST trading-day counts in each period.
    held_up
        ``True`` iff the combo still clears the hard rejection gates on the test
        period — the basic "not a fluke" out-of-sample check.
    """

    train_metrics: ResultMetrics
    test_metrics: ResultMetrics
    train_days: int
    test_days: int
    held_up: bool


def _day_split(bars: Sequence[CandleBar], train_frac: float) -> tuple[int, int, int]:
    """Return ``(split_index, train_days, test_days)`` at an IST day boundary.

    Splits by whole IST trading days, so train is strictly before test and the
    test period begins at a fresh day. ``split_index`` is the first TEST bar.

    Raises
    ------
    ValueError
        If ``train_frac`` is not in the open interval ``(0, 1)``, or there are
        fewer than 2 distinct trading days to split.
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError(f"train_frac must be in (0, 1), got {train_frac}")
    day_starts: list[int] = []
    prev_date = None
    for i, bar in enumerate(bars):
        ist_date = bar.timestamp.astimezone(IST).date()
        if ist_date != prev_date:
            day_starts.append(i)
            prev_date = ist_date
    n_days = len(day_starts)
    if n_days < _MIN_SPLIT_DAYS:
        raise ValueError(f"need >= {_MIN_SPLIT_DAYS} distinct trading days to split, got {n_days}")
    # Clamp so BOTH periods have at least one whole day.
    train_days = max(1, min(n_days - 1, round(train_frac * n_days)))
    return day_starts[train_days], train_days, n_days - train_days


def validate_out_of_sample(
    bars: Sequence[CandleBar],
    required_mask: int,
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    train_frac: float = 0.7,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
) -> OOSValidation:
    """Measure one combo on a train period vs the unseen test period that follows.

    Builds ``bar_bits`` ONCE over the full stream (correct prior-day anchors
    everywhere), splits at an IST day boundary, and runs the SAME mask through the
    shared per-mask metrics core on each half. ``held_up`` is ``True`` iff the test
    period still clears the hard rejection gates.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)`` spanning
        ``>= 2`` distinct IST trading days (non-empty).
    required_mask
        The u64 strategy combo to validate (``0`` enters on every bar).
    direction, tf_minutes, stop_loss_paisa, take_profit_paisa, start_equity_paisa, proximity, prior_n
        As :func:`brutex.engine.sweep_driver.evaluate_combo`.
    train_frac
        Fraction of distinct trading days in the train period (``0 < train_frac <
        1``); the remainder is the held-out test period. Defaults to 0.7.

    Returns
    -------
    OOSValidation
        Train + test metrics, the day split, and the ``held_up`` verdict.

    Raises
    ------
    ValueError
        If ``bars`` is empty, ``train_frac`` ∉ ``(0, 1)``, or there are fewer than
        2 distinct trading days.
    """
    if not bars:
        raise ValueError("validate_out_of_sample: bars must be non-empty")
    split_index, train_days, test_days = _day_split(bars, train_frac)
    # Build once over the FULL stream → the test period's first-day anchors are its
    # real prior (train) day, not cleared. Then slice the trade-sim at the boundary.
    bar_bits = build_bar_bits(bars, proximity=proximity, prior_n=prior_n)
    train_metrics = metrics_for_mask(
        bars[:split_index],
        bar_bits[:split_index],
        required_mask,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
    )
    test_metrics = metrics_for_mask(
        bars[split_index:],
        bar_bits[split_index:],
        required_mask,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
    )
    return OOSValidation(
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        train_days=train_days,
        test_days=test_days,
        held_up=passes_gates(test_metrics),
    )


__all__ = [
    "OOSValidation",
    "validate_out_of_sample",
]
