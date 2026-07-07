"""O(1) bench for the BOS/CHoCH per-bar update — Golden Rule #5 hot path (bits 56-59).

DEC-O1-BENCH-GATE-COMPLETE-001 (M2). The market-structure detector
``trading_core.MarketStructureState.update`` (the PRODUCTION pyclass
``brutex/engine/stage1a.py`` drives — the single source since
DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 deleted the hand-synced Python twin
this bench previously timed) is a per-bar hot path added by the vocabulary
expansion 52 -> 60 conditions (DEC-NIFTY-BOS-CHOCH-001, ``docs/MIGRATION.md``
#1012): bits 56-59 (BOS-bullish / BOS-bearish / CHoCH-bullish / CHoCH-bearish)
are evaluated once per bar per (instrument, timeframe), so ``update`` sits
inside the same per-evaluated-bar loop the rest of the 60-condition vocabulary
does. It had NO dedicated O(1) ratio gate until this file (the sibling
bar-lookup / indicator-lookup / mask-eval / dedup / result-append paths each
already have one); the guarantee audit flagged the omission.

Why it is O(1) per bar (Golden Rule #10 honest envelope): ``update`` pushes the
bar into a fixed ``2k+1`` ring, confirms the ring's CENTRE bar as a swing over
the ``2k`` fixed-size neighbours, then tests the close against the two
confirmed swing levels. Every step touches a FIXED-SIZE window (``k`` is a
constant, default 2 -> a 5-bar fractal), NEVER a structure that grows with the
number of bars already fed. So the per-bar latency must be independent of how
deep the session is — a regression that made ``update`` re-scan the whole
session history (or hold an unbounded window) would make a deep session slower
than a shallow one and blow the ratio.

The gate is the standard ratio ``mean_ns(deep) / mean_ns(shallow) <= ceiling``
routed through the single-source-of-truth
:func:`tests.bench._o1_tiers.ratio_ceiling` (5x robust on the shared GHA runner,
tightened to the strict 1.1x M4-Pro tier). The ring is fixed-size (it does NOT
cross a CPU cache tier as the session deepens — measured ~1.0x at strict), so
this path holds the raw 1.1x, unlike the 100K-object cross-cache-tier lookups
that route through :func:`cache_tier_ceiling`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import trading_core

from tests.bench._o1_tiers import ratio_ceiling

#: Bars already consumed by the session before the timed steady-state phase.
#: A truly O(1)-per-bar ``update`` costs the same at 100K deep as at 1K deep.
N_SMALL = 1_000
N_BIG = 100_000

#: Timed ``update`` calls per tier.
ITERATIONS = 200_000

#: Warm-up ``update`` calls before each timed phase.
_WARMUP = 2_000

#: Mid price in int64 paisa the oscillation is anchored to (the update's unit).
_BASE_CLOSE = 1_995_000
#: Half-spread of each bar's high/low around its close (paisa).
_HALF_SPREAD = 300
#: Bars per spike period: a single strict-peak bar + a single strict-trough bar.
_PERIOD = 12
#: Cycles per drift ramp before it saws back to zero (so later peaks/troughs
#: re-cross earlier CONFIRMED swings → the BOS/CHoCH resolution branches fire).
_DRIFT_CYCLES = 8
#: Paisa the peak/trough level climbs per cycle inside a drift ramp.
_DRIFT_STEP = 800
#: Paisa a spike bar juts above (peak) / below (trough) the drifted mid.
_SPIKE = 4_000
#: Gentle per-phase slope on the non-spike bars (keeps every level distinct so
#: a spike bar is a STRICT local extremum over its fixed 2k neighbours).
_PHASE_SLOPE = 100


def _bar(seq: int) -> tuple[int, int, int]:
    """A deterministic, size-independent (high, low, close) that fires BOS/CHoCH.

    The series is a strictly periodic train of isolated single-bar spikes: within
    each fixed ``_PERIOD``-bar cycle exactly one bar juts up to a strict local
    maximum (a confirmable swing HIGH) and exactly one juts down to a strict local
    minimum (a confirmable swing LOW), while a sawtooth ``drift`` walks the whole
    train up over ``_DRIFT_CYCLES`` cycles then resets. That drift makes each new
    peak/trough re-cross the PREVIOUSLY confirmed swing high/low, so the close
    actually breaks the confirmed levels — exercising the real BOS/CHoCH
    resolution branches (``bos_*`` / ``choch_*`` = ``True``) AND the per-bar
    ``_confirm_centre_swing`` scan, not just the no-op warm-up fast path. The
    train is fully periodic (a pure function of ``seq``, no RNG, no growing
    state), so the timed op is byte-for-byte identical at 1K deep and 100K deep —
    which is exactly what makes the O(1) ratio meaningful. (The generator's
    drift makes closes re-cross previously CONFIRMED levels, exercising the
    real BOS/CHoCH resolution branches — not just the warm-up no-op path.)
    """
    phase = seq % _PERIOD
    cycle = seq // _PERIOD
    drift = (cycle % _DRIFT_CYCLES) * _DRIFT_STEP
    mid = _BASE_CLOSE + drift
    if phase == 3:  # single strict PEAK bar → confirmable swing high
        close = mid + _SPIKE
    elif phase == 9:  # single strict TROUGH bar → confirmable swing low
        close = mid - _SPIKE
    else:  # distinct sloped mid levels so the spike stays a strict extremum
        close = mid + (phase - 6) * _PHASE_SLOPE
    return close + _HALF_SPREAD, close - _HALF_SPREAD, close


@dataclass(frozen=True)
class _TimedPhase:
    """Result of one timed steady-state phase: mean latency + fired-signal tally."""

    #: Mean ns per ``update`` in the timed steady-state region.
    mean_ns: float
    #: Total BOS ``True`` signals (bullish + bearish) fired in the timed region.
    bos_fired: int
    #: Total CHoCH ``True`` signals (bullish + bearish) fired in the timed region.
    choch_fired: int


def _time_update(prefilled_bars: int) -> _TimedPhase:
    """Time ``update`` AFTER the session already consumed ``prefilled_bars``.

    Proves the per-bar cost is independent of session depth: the fixed-size ring
    is fully primed and ``count`` carries the whole history, yet the steady-state
    ``update`` cost must match a shallow session's. Also tallies the BOS/CHoCH
    ``True`` signals fired in the timed region so the caller can assert the timed
    op really is the mixed signal-firing hot path (not a warm-up no-op).
    """
    state: Any = trading_core.MarketStructureState()  # k=2 default (5-bar fractal)
    for i in range(prefilled_bars):
        state.update(*_bar(i))
    base = prefilled_bars
    for k in range(_WARMUP):  # warm up branch predictor + attribute caches
        state.update(*_bar(base + k))
    start = base + _WARMUP
    bos_fired = 0
    choch_fired = 0
    t0 = time.perf_counter_ns()
    for k in range(ITERATIONS):
        # The FFI wire order: (bos_bullish, bos_bearish, choch_bullish, choch_bearish).
        bos_bull, bos_bear, choch_bull, choch_bear = state.update(*_bar(start + k))
        bos_fired += bos_bull + bos_bear
        choch_fired += choch_bull + choch_bear
    t1 = time.perf_counter_ns()
    return _TimedPhase(
        mean_ns=(t1 - t0) / ITERATIONS,
        bos_fired=bos_fired,
        choch_fired=choch_fired,
    )


def test_market_structure_update_is_o1_in_session_bars() -> None:
    """``update`` latency with a 100K-bar-deep session must hold the ceiling vs 1K.

    The BOS/CHoCH detector's fixed-size fractal ring makes each bar's cost a
    constant (confirm the centre over ``2k`` neighbours + two swing-level compares);
    it must NOT scale with the number of bars already fed (bits 56-59,
    DEC-NIFTY-BOS-CHOCH-001). A regression to a session-history re-scan — on
    either side of the PyO3 boundary — blows this.
    """
    small = _time_update(N_SMALL)
    big = _time_update(N_BIG)

    # The timed op must be the REAL mixed hot path: the price train breaks the
    # confirmed swing levels, so both a BOS and a CHoCH ``True`` branch actually
    # fire in the timed region (not just the warm-up no-op fast path). Enforcing
    # it here keeps the ``_bar`` docstring honest (Golden Rule #10) — a generator
    # that stopped firing signals would silently downgrade this to a no-op bench.
    for phase, label in ((small, "shallow"), (big, "deep")):
        assert phase.bos_fired > 0, (
            f"{label} timed phase fired ZERO BOS True signals — the bench would be "
            f"a no-op fast path, not the mixed BOS/CHoCH hot path it claims."
        )
        assert phase.choch_fired > 0, (
            f"{label} timed phase fired ZERO CHoCH True signals — the bench would "
            f"be a no-op fast path, not the mixed BOS/CHoCH hot path it claims."
        )

    ratio = big.mean_ns / max(small.mean_ns, 1.0)

    assert ratio < ratio_ceiling(5.0), (
        f"MarketStructureState.update latency ratio {ratio:.2f}× ({N_BIG} vs "
        f"{N_SMALL} session bars) exceeds the CI ceiling — the BOS/CHoCH per-bar "
        f"path may have regressed away from O(1) (e.g. an unbounded window or a "
        f"session-history re-scan). t_small={small.mean_ns:.1f}ns, "
        f"t_big={big.mean_ns:.1f}ns."
    )
