"""O(1) bench for the engine's per-bar lookup — Golden Rule #5 hot path #1.

DEC-O1-BENCH-GAPS-002 (closes follow-up F-2 of DEC-WORKSPACE-VERIFY-002,
``docs/MIGRATION.md`` #609): "bar lookup" is named as an O(1) hot path by
CLAUDE.md §2 rule 5, but until this file it had no DEDICATED isolated ratio
gate — the throughput canary gates a generic dict lookup and the resident-bar
benches gate marshalling, not the indexed access itself. The surface gated
here is the real access path the sweep pays per evaluated bar:

1. indexed access on the engine's bar container (``list[CandleBar]`` — the
   exact shape ``brutex/engine/rust_trade_sim.py`` and ``sweep_driver.py``
   index into), and
2. the same access wrapped by the ``LookAheadBarrier`` guard — the P-06
   instrumented accessor active on every sweep eval (CLAUDE.md §4,
   DEC-LOOKAHEAD-BARRIER-001).

Both must stay latency-independent of how many bars are loaded. The access
pattern measured is the engine's REAL one — a FORWARD sequential replay
(``evaluate(0..N)``), exactly like the sibling ``test_lookahead_o1.py`` —
not a random-access sweep. The gate is the standard ratio
``mean_ns(N_big) / mean_ns(N_small) ≤ ceiling`` routed through
:func:`tests.bench._o1_tiers.cache_tier_ceiling` — 5× robust on the shared
GHA runner, tightened on the M4-Pro strict tier to the DEC-O1-CACHE-TIER-001
measured cache allowance (3×) rather than the raw 1.1×, because the ``N_BIG``
container (100K ``CandleBar`` heap objects) SPILLS the CPU cache into DRAM: the
per-access cost is bounded by a CONSTANT memory-tier factor (~1.1–1.2× measured
on the quiet M4-Pro; a cache-cliff, not an algorithmic regression), exactly the
allowance the throughput canary already routes its 1M-elem mask-AND / dict
lookups through (DEC-O1-BENCH-GATE-COMPLETE-001). Same-footprint hot paths keep
the raw 1.1× via :func:`ratio_ceiling`; this cross-tier bench routes through
:func:`cache_tier_ceiling` so the strict tier stays HONEST (Golden Rule #10) —
a genuine O(n) regression still blows well past 3×.

Honest envelope (Golden Rule #10): a CPython ``list.__getitem__`` is O(1) by
construction — this gate exists so a future REGRESSION (e.g. swapping the
container for a timestamp scan, or making the barrier retain + scan state)
fails CI mechanically, not to re-prove CPython semantics. A RANDOM-access
pattern at 100K heap objects is cache-miss bound (~9× measured here) — that
is memory-hierarchy physics on a pattern the sweep does not use, so it is
deliberately NOT the gated surface (the dict-lookup throughput canary already
covers the scattered-access shape at its own documented ceiling).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.feed.lookahead import LookAheadBarrier

from tests.bench._o1_tiers import cache_tier_ceiling

#: Container sizes for the small vs big tiers of the ratio gate.
N_SMALL = 1_000
N_BIG = 100_000

#: Warm-up accesses before each timed replay.
_WARMUP = 64


def _make_bars(n: int) -> list[CandleBar]:
    """Build *n* valid 1-minute bars (the engine's container shape)."""
    start = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
    one_minute = timedelta(minutes=1)
    open_ = Decimal("20300.00")
    high = Decimal("20350.00")
    low = Decimal("20250.00")
    close = Decimal("20325.00")
    return [
        CandleBar(
            timestamp=start + i * one_minute,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=0,
            open_interest=None,
        )
        for i in range(n)
    ]


def _time_indexed_access(bars: list[CandleBar]) -> float:
    """Mean ns per ``bars[i]`` access over a forward sequential replay."""
    n = len(bars)
    for index in range(min(_WARMUP, n)):  # warm up
        _ = bars[index]
    t0 = time.perf_counter_ns()
    for index in range(n):
        _ = bars[index]
    t1 = time.perf_counter_ns()
    return (t1 - t0) / n


def _time_guarded_replay(bars: list[CandleBar]) -> float:
    """Mean ns per ``advance_to + guard(bars[i])`` over a forward replay."""
    n = len(bars)
    barrier = LookAheadBarrier(record=False)
    for index in range(min(_WARMUP, n)):  # warm up
        barrier.advance_to(index)
        barrier.guard(index, bars[index])
    barrier.reset()
    t0 = time.perf_counter_ns()
    for index in range(n):
        barrier.advance_to(index)
        barrier.guard(index, bars[index])
    t1 = time.perf_counter_ns()
    return (t1 - t0) / n


def test_indexed_bar_access_is_o1_in_bar_count() -> None:
    """Per-access latency at 100K bars must hold the ceiling vs 1K bars.

    A regression that made bar access scale with the loaded history (e.g.
    replacing the list index with a timestamp scan) blows this ratio.
    """
    t_small = _time_indexed_access(_make_bars(N_SMALL))
    t_big = _time_indexed_access(_make_bars(N_BIG))
    ratio = t_big / max(t_small, 1.0)

    assert ratio < cache_tier_ceiling(5.0), (
        f"bar indexed-access latency ratio {ratio:.2f}× ({N_BIG} vs {N_SMALL} "
        f"bars) exceeds the CI ceiling — the bar container may have regressed "
        f"away from O(1). t_small={t_small:.1f}ns, t_big={t_big:.1f}ns."
    )


def test_guarded_bar_access_is_o1_in_bar_count() -> None:
    """The full eval-time path (barrier + index) must hold the same ceiling.

    This composes the two O(1) pieces the sweep actually pays per bar —
    ``LookAheadBarrier.advance_to/guard`` plus the container index — so a
    regression in EITHER (or in how they compose) fails one gate.
    """
    t_small = _time_guarded_replay(_make_bars(N_SMALL))
    t_big = _time_guarded_replay(_make_bars(N_BIG))
    ratio = t_big / max(t_small, 1.0)

    assert ratio < cache_tier_ceiling(5.0), (
        f"guarded bar-access latency ratio {ratio:.2f}× ({N_BIG} vs {N_SMALL} "
        f"bars) exceeds the CI ceiling — the eval-time per-bar path may have "
        f"regressed away from O(1). t_small={t_small:.1f}ns, t_big={t_big:.1f}ns."
    )
