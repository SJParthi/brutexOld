"""P1 — roll-segmentation equivalence: THE anti-leak oracle for the B2 roll reset.

DEC-STAGE1A-ROLL-RESET-001 (ADR-0012 D11): a stitched multi-contract stream
built through ONE ``Stage1AMatrixBuilder`` with a ``roll_schedule`` must be
**bit-identical to per-segment fresh builders** —

    ``build_bar_bits(stitched, roll_schedule=S)
        == concat(build_bar_bits(seg.bars) for seg in segments)``

Validity of the oracle: a fresh builder's constant default session key is
output-invariant (the VWAP reset predicate keys on key CHANGE, and within one
segment the schedule key is one constant — pinned by the constant-key
invariance test in ``tests/unit/test_engine_stage1a.py``), and the
``_roll_to_new_contract`` TOTAL hard reset restores the exact fresh-builder
state. Any partial-reset mutant (an omitted member in the reset body — EMA,
market structure, prior history, day extremes, daily history, anchors) breaks
the equivalence on a crafted stream IN THIS FILE, so this property is the
permanent CI teeth behind "every session/level indicator recomputes
per-contract" (``brutex/data/futures_roll.py`` roll policy). One member needs
a dedicated stream shape (review round-3 finding 1): ``_daily_history`` is
only POPULATED under a non-PREV_DAY fib anchor (``stage1a.py`` records
completed days for the multi-day anchors only), so the default-anchor streams
are structurally BLIND to its reset — the deterministic rolling-weekly /
weekly-static cases below (>= 5 warm sessions per segment, post-roll prices
inside the pre-roll band) are the killers for that member, with a dedicated
mutant-teeth test proving their discriminating power.

The hypothesis stream deliberately includes a **muhurat-style evening-dated
segment** (bars at 18:15+ IST — legitimately in FNO lakes since #1294) and a
**degenerate 1-bar segment**, both also pinned by explicit deterministic
cases below so they can never silently stop being drawn.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.stage1a import Stage1AMatrixBuilder, build_bar_bits
from brutex.strategies.fib_definitions import FibAnchorKind
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001): the Hypothesis `@st.composite` strategies
# below are invoked with no args (the decorator injects `draw` at runtime);
# pyright does not model the `@composite` signature rewrite. Scoped to this
# file only — same convention as tests/property/test_sweep_suffix_independence.py.

_IST = timezone(timedelta(hours=5, minutes=30))

#: A weekday base so the synthetic calendar walks ordinary IST dates.
_BASE_DAY = date(2025, 6, 2)

#: Integer-rupee prices INCLUDING the extreme edges with extra weight.
_price = st.one_of(
    st.just(1),
    st.just(1_000_000),
    st.integers(min_value=1, max_value=1_000_000),
)


def _seg_bar(day: date, minute: int, open_i: int, close_i: int, *, evening: bool) -> CandleBar:
    """One volume-bearing bar on *day* (09:16 IST regular / 18:15 IST muhurat-style)."""
    hour, base_minute = (18, 15) if evening else (9, 16)
    ts = datetime(day.year, day.month, day.day, hour, base_minute, tzinfo=_IST) + timedelta(
        minutes=minute
    )
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=Decimal(open_i),
        high=Decimal(max(open_i, close_i)),
        low=Decimal(min(open_i, close_i)),
        close=Decimal(close_i),
        volume=100,
        open_interest=None,
    )


@st.composite
def _multi_contract_stream(
    draw: st.DrawFn,
) -> tuple[list[list[CandleBar]], tuple[int, ...]]:
    """Draw (per-contract segments, roll_schedule) over consecutive IST dates.

    Each segment owns 1-3 consecutive dates with 1-6 volume-bearing bars per
    date (a 1-day 1-bar segment is the degenerate case); each segment's
    owning expiry is its LAST date, so the schedule partitions the stream
    exactly at the segment boundaries (the resolver's ``(E_{k-1}, E_k]`` law).
    A segment may be muhurat-style evening-dated.
    """
    n_segments = draw(st.integers(min_value=2, max_value=3))
    day_cursor = 0
    segments: list[list[CandleBar]] = []
    schedule: list[int] = []
    for _ in range(n_segments):
        n_days = draw(st.integers(min_value=1, max_value=3))
        evening = draw(st.booleans())
        bars: list[CandleBar] = []
        for _ in range(n_days):
            day = _BASE_DAY + timedelta(days=day_cursor)
            day_cursor += 1
            n_bars = draw(st.integers(min_value=1, max_value=6))
            for minute in range(n_bars):
                open_i = draw(_price)
                close_i = draw(_price)
                bars.append(_seg_bar(day, minute, open_i, close_i, evening=evening))
        segments.append(bars)
        schedule.append((_BASE_DAY + timedelta(days=day_cursor - 1)).toordinal())
    return segments, tuple(schedule)


def _assert_roll_equivalence(
    segments: list[list[CandleBar]],
    schedule: tuple[int, ...],
    fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY,
) -> None:
    """The P1 oracle body — stitched == concatenated fresh builds, bit for bit."""
    stitched_bars = [bar for seg in segments for bar in seg]
    stitched = build_bar_bits(stitched_bars, fib_anchor=fib_anchor, roll_schedule=schedule)
    fresh: list[int] = []
    for seg in segments:
        fresh.extend(build_bar_bits(seg, fib_anchor=fib_anchor))
    assert stitched == fresh


@given(stream=_multi_contract_stream())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_stitched_build_equals_per_segment_fresh_builders(
    stream: tuple[list[list[CandleBar]], tuple[int, ...]],
) -> None:
    """P1: the roll reset makes the stitched build == per-segment fresh builds."""
    segments, schedule = stream
    _assert_roll_equivalence(segments, schedule)


def test_degenerate_one_bar_segment_equivalence() -> None:
    """The 1-bar-segment edge, pinned deterministically (never left to the draw)."""
    seg_a = [_seg_bar(_BASE_DAY, m, 100 + m, 101 + m, evening=False) for m in range(5)]
    seg_b = [_seg_bar(_BASE_DAY + timedelta(days=1), 0, 140, 141, evening=False)]
    seg_c = [
        _seg_bar(_BASE_DAY + timedelta(days=2), m, 180 + m, 181 + m, evening=False)
        for m in range(3)
    ]
    schedule = (
        _BASE_DAY.toordinal(),
        (_BASE_DAY + timedelta(days=1)).toordinal(),
        (_BASE_DAY + timedelta(days=2)).toordinal(),
    )
    _assert_roll_equivalence([seg_a, seg_b, seg_c], schedule)


def test_muhurat_evening_segment_equivalence() -> None:
    """A muhurat-style evening-dated segment (18:15+ IST bars), pinned explicitly.

    Evening bars are owned by their OWN IST date (the loader's IST-date cut,
    #1294 calendar-aware ingest keeps them), so the roll machinery must treat
    an evening-session segment exactly like a regular one.
    """
    seg_a = [_seg_bar(_BASE_DAY, m, 25_000 + m, 25_001 + m, evening=False) for m in range(4)]
    seg_muhurat = [
        _seg_bar(_BASE_DAY + timedelta(days=1), m, 25_040 + m, 25_041 + m, evening=True)
        for m in range(3)
    ]
    schedule = (
        _BASE_DAY.toordinal(),
        (_BASE_DAY + timedelta(days=1)).toordinal(),
    )
    _assert_roll_equivalence([seg_a, seg_muhurat], schedule)


def _trading_weekdays(start: date, count: int) -> list[date]:
    """The first *count* Mon–Fri dates from *start* (June-2025 holds no NSE holiday)."""
    days: list[date] = []
    d = start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _multi_day_anchor_segments() -> tuple[list[CandleBar], list[CandleBar], tuple[int, int]]:
    """Two 8-trading-day segments; post-roll prices INSIDE the pre-roll band.

    The crafted stream of review round-3 finding 1: >= 5 completed sessions
    per segment keep the 5-session rolling-weekly anchor (and the
    completed-ISO-week weekly-static anchor) WARM before AND after the roll,
    and the overlapping price band makes the STALE pre-roll fib levels
    condition-relevant post-roll — so an omitted ``_daily_history.clear()``
    in the roll reset genuinely moves the post-roll Group-4 bits. Real 2025
    NSE trading days (the multi-day anchors walk the REAL calendar).
    """
    days = _trading_weekdays(_BASE_DAY, 16)
    seg_a_days, seg_b_days = days[:8], days[8:]
    seg_a = [
        _seg_bar(dd, m, 100 + i * 3 + m, 102 + i * 3 + m, evening=False)
        for i, dd in enumerate(seg_a_days)
        for m in range(4)
    ]
    seg_b = [
        _seg_bar(dd, m, 110 + i + m, 112 + i + m, evening=False)
        for i, dd in enumerate(seg_b_days)
        for m in range(4)
    ]
    return seg_a, seg_b, (seg_a_days[-1].toordinal(), seg_b_days[-1].toordinal())


def test_rolling_weekly_fib_anchor_roll_equivalence() -> None:
    """The `_daily_history` killer (round-3 finding 1), ROLLING_WEEKLY arm.

    ``_daily_history`` is populated ONLY under a non-PREV_DAY fib anchor, so
    every PREV_DAY stream in this file is blind to its reset — a mutant that
    omits ``self._daily_history.clear()`` in ``_roll_to_new_contract`` would
    evaluate post-roll Group-4 fib levels from PRE-roll daily candles across
    the roll basis gap (the exact D11 violation class) with zero test signal.
    This warm-anchor case makes the P1 oracle see that member.
    """
    seg_a, seg_b, schedule = _multi_day_anchor_segments()
    _assert_roll_equivalence([seg_a, seg_b], schedule, fib_anchor=FibAnchorKind.ROLLING_WEEKLY)


def test_weekly_static_fib_anchor_roll_equivalence() -> None:
    """The `_daily_history` killer, WEEKLY_STATIC arm (same crafted stream)."""
    seg_a, seg_b, schedule = _multi_day_anchor_segments()
    _assert_roll_equivalence([seg_a, seg_b], schedule, fib_anchor=FibAnchorKind.WEEKLY_STATIC)


class _DailyHistoryLeakMutant(Stage1AMatrixBuilder):
    """The round-3 finding-1 mutant: the roll reset 'forgets' `_daily_history.clear()`."""

    def _roll_to_new_contract(self) -> None:
        saved = dict(self._daily_history)
        super()._roll_to_new_contract()
        self._daily_history.update(saved)  # simulate the omitted clear


def test_daily_history_leak_mutant_is_killed_by_the_multi_day_anchor_cases() -> None:
    """Teeth: omitting ONLY the `_daily_history` clear breaks the new cases.

    Non-vacuity for the rolling-weekly/weekly-static equivalence tests above:
    the SAME stream under a builder whose roll reset preserves
    ``_daily_history`` diverges from the per-segment fresh concat — so the
    mutant those cases exist to kill is genuinely killable BY them (it is
    provably invisible to every PREV_DAY stream in this file).
    """
    seg_a, seg_b, schedule = _multi_day_anchor_segments()
    for anchor in (FibAnchorKind.ROLLING_WEEKLY, FibAnchorKind.WEEKLY_STATIC):
        mutant = _DailyHistoryLeakMutant(fib_anchor=anchor, roll_schedule=schedule)
        stitched_mutant = mutant.build(seg_a + seg_b)
        fresh = build_bar_bits(seg_a, fib_anchor=anchor) + build_bar_bits(seg_b, fib_anchor=anchor)
        assert stitched_mutant != fresh


def test_partial_reset_would_break_the_oracle_teeth() -> None:
    """Non-vacuity teeth: WITHOUT the reset the equivalence genuinely fails.

    The continuous (schedule-less) build over the same two-segment stream
    differs from the per-segment fresh concat — the prior-day anchors span the
    boundary — so the P1 property has real discriminating power (a mutant that
    skips ``_roll_to_new_contract`` entirely is killed by the cases above).
    """
    seg_a = [
        _seg_bar(_BASE_DAY + timedelta(days=d), m, 100 + m, 102 + m, evening=False)
        for d in range(2)
        for m in range(4)
    ]
    seg_b = [
        _seg_bar(_BASE_DAY + timedelta(days=2), m, 140 + m, 142 + m, evening=False)
        for m in range(4)
    ]
    continuous = build_bar_bits(seg_a + seg_b)  # NO schedule — state spans the roll
    fresh = build_bar_bits(seg_a) + build_bar_bits(seg_b)
    assert continuous != fresh
