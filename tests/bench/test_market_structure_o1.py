"""O(1) bench for the BOS/CHoCH per-bar update — Golden Rule #5 hot path (bits 56-59).

DEC-O1-BENCH-GATE-COMPLETE-001 (M2). The market-structure detector
:meth:`brutex.indicators.market_structure.MarketStructureState.update` is a
per-bar hot path added by the vocabulary expansion 52 -> 60 conditions
(DEC-NIFTY-BOS-CHOCH-001, ``docs/MIGRATION.md`` #1012): bits 56-59
(BOS-bullish / BOS-bearish / CHoCH-bullish / CHoCH-bearish) are evaluated once
per bar per (instrument, timeframe), so ``update`` sits inside the same
per-evaluated-bar loop the rest of the 60-condition vocabulary does. It had NO
dedicated O(1) ratio gate until this file (the sibling bar-lookup /
indicator-lookup / mask-eval / dedup / result-append paths each already have
one); the guarantee audit flagged the omission.

Why it is O(1) per bar (Golden Rule #10 honest envelope): ``update`` pushes the
bar into a ``deque(maxlen=2k+1)`` ring, confirms the ring's CENTRE bar as a
swing over the ``2k`` fixed-size neighbours, then tests the close against the two
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

from brutex.indicators.market_structure import MarketStructureState

from tests.bench._o1_tiers import ratio_ceiling

#: Bars already consumed by the session before the timed steady-state phase.
#: A truly O(1)-per-bar ``update`` costs the same at 100K deep as at 1K deep.
N_SMALL = 1_000
N_BIG = 100_000

#: Timed ``update`` calls per tier.
ITERATIONS = 200_000

#: Warm-up ``update`` calls before each timed phase.
_WARMUP = 2_000

#: A representative OHLC-ish bar in int64 paisa (the update's argument unit).
_BASE_HIGH = 2_000_000
_BASE_LOW = 1_990_000
_BASE_CLOSE = 1_995_000


def _bar(seq: int) -> tuple[int, int, int]:
    """A deterministic, size-independent (high, low, close) that oscillates.

    The close swings above/below the confirmed swing levels often enough to
    exercise BOTH the BOS/CHoCH resolution branches AND the swing-confirmation
    branch — so the timed op is the REAL mixed hot path, not a no-op fast path.
    """
    high = _BASE_HIGH + (seq % 1_000)
    low = _BASE_LOW - (seq % 1_000)
    close = _BASE_CLOSE + ((seq * 7) % 1_500)
    return high, low, close


def _time_update(prefilled_bars: int) -> float:
    """Mean ns per ``update`` AFTER the session already consumed ``prefilled_bars``.

    Proves the per-bar cost is independent of session depth: the fixed-size ring
    is fully primed and ``count`` carries the whole history, yet the steady-state
    ``update`` cost must match a shallow session's.
    """
    state = MarketStructureState()  # k=2 default (production 5-bar fractal)
    for i in range(prefilled_bars):
        state.update(*_bar(i))
    base = prefilled_bars
    for k in range(_WARMUP):  # warm up branch predictor + attribute caches
        state.update(*_bar(base + k))
    start = base + _WARMUP
    t0 = time.perf_counter_ns()
    for k in range(ITERATIONS):
        state.update(*_bar(start + k))
    t1 = time.perf_counter_ns()
    return (t1 - t0) / ITERATIONS


def test_market_structure_update_is_o1_in_session_bars() -> None:
    """``update`` latency with a 100K-bar-deep session must hold the ceiling vs 1K.

    The BOS/CHoCH detector's fixed-size fractal ring makes each bar's cost a
    constant (confirm the centre over ``2k`` neighbours + two swing-level compares);
    it must NOT scale with the number of bars already fed (bits 56-59,
    DEC-NIFTY-BOS-CHOCH-001). A regression to a session-history re-scan blows this.
    """
    t_small = _time_update(N_SMALL)
    t_big = _time_update(N_BIG)
    ratio = t_big / max(t_small, 1.0)

    assert ratio < ratio_ceiling(5.0), (
        f"MarketStructureState.update latency ratio {ratio:.2f}× ({N_BIG} vs "
        f"{N_SMALL} session bars) exceeds the CI ceiling — the BOS/CHoCH per-bar "
        f"path may have regressed away from O(1) (e.g. an unbounded window or a "
        f"session-history re-scan). t_small={t_small:.1f}ns, t_big={t_big:.1f}ns."
    )
