"""O(1)-per-bar bench for the Stage-1A matrix builder (DEC-ENGINE-DRIVER-001).

The Stage-1A builder constructs one :class:`IndicatorSnapshot` per bar and calls
the O(1) ``evaluate_atoms`` (CLAUDE.md §5 / Golden Rule #5). The per-bar build
cost must therefore be **independent of how many bars precede it** — processing
bar 1,000,000 must cost the same as bar 1,000, because every running state
(EMA, day extremes, prior-bar history, the day-OHLC accumulator) is O(1) per
update and the daily anchors are computed once per day and broadcast.

## Strategy

We measure mean per-bar ``build`` latency at two tiers (``N_small`` vs
``N_big``) holding the intraday density constant (same bars-per-day, so the
day-rollover frequency per bar is identical across tiers — a fair O(1) probe).
The ratio ``per_bar(N_big) / per_bar(N_small)`` must stay under the CI ceiling —
``5×`` on the shared GHA runner, tightened to the decreed strict ``1.1×`` on the
self-hosted M4-Pro tier via ``tests/bench/_o1_tiers.py::ratio_ceiling``
(DEC-O1-STRICT-TIER-001). A regression to super-linear per-bar work (e.g. an
accidental rescan of history) blows the ratio past the bound and fails CI.

## De-flake: best-of-N timing

Each tier is timed as the **minimum of N repeats** — the noise-robust estimator
of true cost (least perturbed by upward-only OS jitter; never inflated).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.stage1a import Stage1AMatrixBuilder

from tests.bench._o1_tiers import ratio_ceiling

#: Two problem sizes (bar counts). The big tier is 100× the small tier.
_N_SMALL: int = 1_000
_N_BIG: int = 100_000
#: Intraday density held constant across tiers (NSE 1-minute session length).
_BARS_PER_DAY: int = 375
#: 09:15:00 IST == 03:45 UTC.
_OPEN_UTC = datetime(2024, 1, 1, 3, 45, tzinfo=UTC)
_REPEATS: int = 5  # best-of-N: the min is the honest true-cost estimate


def _make_bars(n: int) -> list[CandleBar]:
    """Deterministic ``n``-bar stream at a fixed intraday density.

    Prices oscillate inside a fixed band so OHLC is always valid and the
    indicator paths (EMA, day extremes, near-checks) all stay live.
    """
    bars: list[CandleBar] = []
    for i in range(n):
        day, minute = divmod(i, _BARS_PER_DAY)
        ts = _OPEN_UTC + timedelta(days=day, minutes=minute)
        close = 1000 + (i % 7)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(1000),
                high=Decimal(1010),
                low=Decimal(990),
                close=Decimal(close),
                volume=0,
                open_interest=None,
            )
        )
    return bars


def _per_bar_seconds(bars: list[CandleBar]) -> float:
    """Best-of-N minimum mean per-bar ``build`` latency (seconds)."""
    n = len(bars)
    best = float("inf")
    for _ in range(_REPEATS):
        t0 = time.perf_counter()
        Stage1AMatrixBuilder().build(bars)
        best = min(best, (time.perf_counter() - t0) / n)
    return best


@pytest.mark.slow
@pytest.mark.bench
def test_stage1a_per_bar_cost_is_flat() -> None:
    """Per-bar build cost at ``N_big`` must be within the CI ceiling of ``N_small``."""
    small = _make_bars(_N_SMALL)
    big = _make_bars(_N_BIG)

    # Warm caches / allocator on each size before timing.
    Stage1AMatrixBuilder().build(small)
    Stage1AMatrixBuilder().build(big)

    per_bar_small = _per_bar_seconds(small)
    per_bar_big = _per_bar_seconds(big)
    ratio = per_bar_big / max(per_bar_small, 1e-12)

    ceiling = ratio_ceiling(5.0)
    print(
        f"\nStage-1A per-bar O(1) bench (DEC-ENGINE-DRIVER-001) "
        f"— best of {_REPEATS} runs:\n"
        f"  N_small = {_N_SMALL:,} bars | {per_bar_small * 1e9:.1f} ns/bar\n"
        f"  N_big   = {_N_BIG:,} bars | {per_bar_big * 1e9:.1f} ns/bar\n"
        f"  ratio   = {ratio:.2f}× (ceiling {ceiling}×)\n"
    )
    assert ratio < ceiling, (
        f"Stage-1A per-bar build cost ratio {ratio:.2f}× (N_big vs N_small) "
        f"exceeds the {ceiling}× CI ceiling — per-bar work may have regressed "
        f"to super-linear. per_bar_small={per_bar_small * 1e9:.1f}ns, "
        f"per_bar_big={per_bar_big * 1e9:.1f}ns."
    )
