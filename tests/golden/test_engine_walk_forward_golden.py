"""Golden byte-stability oracle for the walk-forward fold schedule.

A fixed, hand-derived synthetic stream (25 distinct IST trading days, 2 one-minute
bars each = 50 bars) under the small ``train=10 / test=5 / step=5`` windows pins
the **exact** rolling fold boundaries and the **exact** BLAKE3 schedule hash. Any
change to the day-segmentation, the day→bar mapping, the rolling-window stepping,
or the schedule-hash canonical encoding (DEC-WALK-FORWARD-OOS-001) breaks this
test — that is its job (Golden Rule #4 reproducibility / NFR-DET-001).

Hand-derived fold layout (``train=10 / test=5 / step=5`` over 25 trading days,
2 bars/day so day ``d`` spans bars ``[2d, 2d+2)``):

* **fold 0** — train days ``[0, 10)`` → bars ``[0, 20)``; test days ``[10, 15)``
  → bars ``[20, 30)``.
* **fold 1** — train days ``[5, 15)`` → bars ``[10, 30)``; test days ``[15, 20)``
  → bars ``[30, 40)``.
* **fold 2** — train days ``[10, 20)`` → bars ``[20, 40)``; test days ``[20, 25)``
  → bars ``[40, 50)``.

The three held-out test windows tile days ``[10, 25)`` (bars ``[20, 50)``) with no
overlap and no gap — the ``step == test`` clean partition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.stage1a import build_walk_forward_schedule

_IST = timezone(timedelta(hours=5, minutes=30))

#: The locked schedule hash for the fixture below. Regenerating it requires an
#: intentional change to the boundaries or the canonical hash encoding.
_GOLDEN_SCHEDULE_HASH = "f7763dd3941443a0b85b51d535207c574028cd9301e55d35817ad3159e38a7ec"

#: (train_start_bar, train_end_bar, test_start_bar, test_end_bar) per fold — the
#: hand-derived oracle.
_GOLDEN_FOLD_BARS = (
    (0, 20, 20, 30),
    (10, 30, 30, 40),
    (20, 40, 40, 50),
)

#: (train_start_day, train_end_day, test_start_day, test_end_day) per fold.
_GOLDEN_FOLD_DAYS = (
    (0, 10, 10, 15),
    (5, 15, 15, 20),
    (10, 20, 20, 25),
)


def _bar(day: int, minute: int, base: int) -> CandleBar:
    """One IST-session bar on trading day ``day`` (calendar 2026-01-05 + day)."""
    ts = datetime(2026, 1, 5, 9, 16 + minute, tzinfo=_IST).astimezone(UTC) + timedelta(days=day)
    return CandleBar(
        timestamp=ts,
        open=Decimal(f"{base}.00"),
        high=Decimal(f"{base + 1}.00"),
        low=Decimal(f"{base - 1}.00"),
        close=Decimal(f"{base}.50"),
        volume=100,
        open_interest=None,
    )


def _fixture_bars() -> list[CandleBar]:
    """25 distinct IST days × 2 one-minute bars = 50 bars (deterministic prices)."""
    return [_bar(d, m, 100 + (d % 7)) for d in range(25) for m in range(2)]


def test_fold_boundaries_are_byte_stable() -> None:
    schedule = build_walk_forward_schedule(_fixture_bars(), train_days=10, test_days=5, step_days=5)
    assert schedule.n_trading_days == 25
    assert schedule.n_bars == 50
    assert schedule.n_folds == 3

    got_bars = tuple(
        (f.train_start_bar, f.train_end_bar, f.test_start_bar, f.test_end_bar)
        for f in schedule.folds
    )
    got_days = tuple(
        (f.train_start_day, f.train_end_day, f.test_start_day, f.test_end_day)
        for f in schedule.folds
    )
    assert got_bars == _GOLDEN_FOLD_BARS
    assert got_days == _GOLDEN_FOLD_DAYS
    # fold_index is the chronological position.
    assert tuple(f.fold_index for f in schedule.folds) == (0, 1, 2)


def test_schedule_hash_is_locked() -> None:
    schedule = build_walk_forward_schedule(_fixture_bars(), train_days=10, test_days=5, step_days=5)
    assert schedule.schedule_hash == _GOLDEN_SCHEDULE_HASH
    assert len(schedule.schedule_hash) == 64


def test_rebuild_is_bit_identical() -> None:
    # Two independent builds of the same fixture produce identical folds + hash.
    a = build_walk_forward_schedule(_fixture_bars(), train_days=10, test_days=5, step_days=5)
    b = build_walk_forward_schedule(_fixture_bars(), train_days=10, test_days=5, step_days=5)
    assert a == b
    assert a.schedule_hash == b.schedule_hash
