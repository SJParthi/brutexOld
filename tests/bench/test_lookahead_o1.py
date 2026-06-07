"""O(1) bench for the look-ahead barrier — DEC-LOOKAHEAD-BARRIER-001.

Proves the runtime look-ahead enforcer (:class:`brutex.feed.lookahead.LookAheadBarrier`)
adds only **O(1) work per bar access** — the cost of one ``advance_to`` + one
``access`` must be independent of how many bars have already streamed past.

## Honest envelope (CLAUDE.md Golden Rule #10)

This is the **Phase-2 strengthening** of the existing timestamp feed-boundary
guard, NOT a performance path. The barrier is two integer comparisons plus an
optional list append per access. The claim proven here is **O(1) per access**,
*not* "O(1) total": a full sweep that touches ``Σ`` bars is inherently ``O(Σ)``.
The gate is the standard **ratio** ``p99(N_big)/p99(N_small) ≤ ceiling`` (the
CLAUDE.md §2 r5 form), tightened to the strict 1.1× M4-Pro tier via
:func:`tests.bench._o1_tiers.ratio_ceiling`; absolute ns are noisy on shared GHA
runners and are not gated (DEC-P0-020 pattern).

``record=False`` is the always-on sweep mode (O(1) memory, no trail retained),
so it is the mode benched for the latency ratio.
"""

from __future__ import annotations

import time

from brutex.feed.lookahead import LookAheadBarrier

from tests.bench._o1_tiers import ratio_ceiling


def _time_access(n: int) -> float:
    """Mean ns per ``advance_to`` + ``access`` over a forward replay of *n* bars."""
    barrier = LookAheadBarrier(record=False)
    # Warm up the interpreter / branch predictor on a small run.
    for index in range(64):
        barrier.advance_to(index)
        barrier.access(index)
    barrier.reset()

    t0 = time.perf_counter_ns()
    for index in range(n):
        barrier.advance_to(index)
        barrier.access(index)
    t1 = time.perf_counter_ns()
    return (t1 - t0) / n


def test_access_is_o1_per_bar() -> None:
    """Per-access latency at 1M bars must not exceed the ceiling vs 1K bars.

    A regression that made the barrier scale with the number of bars already
    streamed (e.g. retaining + scanning the trail in ``record=False`` mode)
    would blow this ratio past the bound and fail the gate.
    """
    t_small = _time_access(1_000)
    t_big = _time_access(1_000_000)
    ratio = t_big / max(t_small, 1.0)

    # 5× robust shared-CI ceiling, tightened to the strict 1.1× M4-Pro tier.
    assert ratio < ratio_ceiling(5.0), (
        f"barrier per-access latency ratio {ratio:.2f}× (1M vs 1K) exceeds the "
        f"5× CI ceiling — the accessor may have regressed away from O(1). "
        f"t_1k={t_small:.1f}ns, t_1m={t_big:.1f}ns."
    )


def test_record_mode_trail_is_bounded_by_accesses() -> None:
    """record=True retains exactly one trail entry per access (bounded, not quadratic)."""
    barrier = LookAheadBarrier(record=True)
    n = 5_000
    for index in range(n):
        barrier.advance_to(index)
        barrier.access(index)
    # Memory footprint of the trail is O(accesses), never O(accesses²).
    assert len(barrier.touched_indices) == n
    assert barrier.max_touched == n - 1
