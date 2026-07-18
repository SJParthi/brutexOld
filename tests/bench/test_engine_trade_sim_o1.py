"""O(1)-per-bar bench for the Stage-2 trade-sim walker (DEC-TRADESIM-001).

Each :meth:`TradeSimWalker.push` does a fixed amount of work — a constant set of
paisa comparisons plus at most one position open/close — so the per-bar cost must
be **independent of how many bars precede it** (CLAUDE.md §5 / Golden Rule #5):
processing bar 1,000,000 must cost the same as bar 1,000. There is no scan back
over history and no growing per-bar state (the trade *list* grows with the number
of trades, which is inherent output — the O(1) claim is per bar / per operation,
never for the whole walk; §9 finding F-5/M3).

## Strategy

Measure mean per-bar :meth:`run` latency at two tiers (``N_small`` vs ``N_big``)
holding the intraday density AND the signal density constant, so the
open/close/forced-exit frequency per bar is identical across tiers (a fair O(1)
probe). The ratio ``per_bar(N_big) / per_bar(N_small)`` must stay under the CI
ceiling — ``5×`` on the shared GHA runner, tightened to the decreed strict
``1.1×`` on the self-hosted M4-Pro tier via
``tests/bench/_o1_tiers.py::ratio_ceiling`` (DEC-O1-STRICT-TIER-001).

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
from brutex.engine.trade_sim import TradeSimWalker
from brutex.strategies.types import Side

from tests.bench._o1_tiers import ratio_ceiling

#: Two problem sizes (bar counts). The big tier is 100× the small tier.
_N_SMALL: int = 1_000
_N_BIG: int = 100_000
#: Intraday density held constant across tiers (NSE 1-minute session length).
_BARS_PER_DAY: int = 375
#: Signal cadence held constant — one entry attempt every 30 bars.
_SIGNAL_EVERY: int = 30
#: 09:15:00 IST == 03:45 UTC.
_OPEN_UTC = datetime(2024, 1, 1, 3, 45, tzinfo=UTC)
_REPEATS: int = 5  # best-of-N: the min is the honest true-cost estimate


def _make_bars(n: int) -> list[CandleBar]:
    """Deterministic ``n``-bar stream at a fixed intraday density.

    Prices oscillate inside a fixed band so OHLC is always valid and the
    walker's stop / target / excursion paths all stay live.
    """
    bars: list[CandleBar] = []
    for i in range(n):
        day, minute = divmod(i, _BARS_PER_DAY)
        ts = _OPEN_UTC + timedelta(days=day, minutes=minute)
        # Close oscillates inside the fixed [997, 1003] band so every bar is
        # valid and the stop / target / excursion paths all stay live.
        close = Decimal(998 + (i % 5))
        bars.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(1000),
                high=Decimal(1003),
                low=Decimal(997),
                close=close,
                volume=0,
                open_interest=None,
            )
        )
    return bars


def _make_signals(n: int) -> list[bool]:
    """One entry signal every :data:`_SIGNAL_EVERY` bars (constant density)."""
    return [i % _SIGNAL_EVERY == 0 for i in range(n)]


def _per_bar_seconds(bars: list[CandleBar], signals: list[bool]) -> float:
    """Best-of-N minimum mean per-bar ``run`` latency (seconds)."""
    n = len(bars)
    best = float("inf")
    for _ in range(_REPEATS):
        walker = TradeSimWalker(
            direction=Side.BUY, tf_minutes=1, stop_loss_paisa=20, take_profit_paisa=40
        )
        t0 = time.perf_counter()
        walker.run(bars, bars, signals)
        best = min(best, (time.perf_counter() - t0) / n)
    return best


@pytest.mark.slow
@pytest.mark.bench
def test_trade_sim_per_bar_cost_is_flat() -> None:
    """Per-bar walk cost at ``N_big`` must be within the CI ceiling of ``N_small``."""
    small_bars, small_sig = _make_bars(_N_SMALL), _make_signals(_N_SMALL)
    big_bars, big_sig = _make_bars(_N_BIG), _make_signals(_N_BIG)

    # Warm caches / allocator on each size before timing.
    TradeSimWalker(direction=Side.BUY, tf_minutes=1, stop_loss_paisa=20, take_profit_paisa=40).run(
        small_bars, small_bars, small_sig
    )
    TradeSimWalker(direction=Side.BUY, tf_minutes=1, stop_loss_paisa=20, take_profit_paisa=40).run(
        big_bars, big_bars, big_sig
    )

    per_bar_small = _per_bar_seconds(small_bars, small_sig)
    per_bar_big = _per_bar_seconds(big_bars, big_sig)
    ratio = per_bar_big / max(per_bar_small, 1e-12)

    ceiling = ratio_ceiling(5.0)
    print(
        f"\nStage-2 trade-sim per-bar O(1) bench (DEC-TRADESIM-001) "
        f"— best of {_REPEATS} runs:\n"
        f"  N_small = {_N_SMALL:,} bars | {per_bar_small * 1e9:.1f} ns/bar\n"
        f"  N_big   = {_N_BIG:,} bars | {per_bar_big * 1e9:.1f} ns/bar\n"
        f"  ratio   = {ratio:.2f}× (ceiling {ceiling}×)\n"
    )
    assert ratio < ceiling, (
        f"Stage-2 trade-sim per-bar walk cost ratio {ratio:.2f}× (N_big vs N_small) "
        f"exceeds the {ceiling}× CI ceiling — per-bar work may have regressed to "
        f"super-linear. per_bar_small={per_bar_small * 1e9:.1f}ns, "
        f"per_bar_big={per_bar_big * 1e9:.1f}ns."
    )
