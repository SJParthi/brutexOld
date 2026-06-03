"""O(1)-per-bar benchmark for the Stage-2 trade-sim walker (`brutex.engine.trade_sim`).

The walker is a single forward pass over the session bars (one scan per open
position), so the **per-bar** cost must be independent of the session length
(CLAUDE.md §2 r5). A regression to per-bar slicing / re-scanning would make it
super-linear and blow the ratio. Routed through the single-source
:func:`tests.bench._o1_tiers.ratio_ceiling` (5× shared-CI / strict 1.1× on the
M4-Pro runner), mirroring ``tests/bench/test_engine_stage1a_o1.py``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar
from brutex.engine.trade_sim import simulate_session_mode_a
from brutex.strategies.types import Side

from tests.bench._o1_tiers import ratio_ceiling

_IST = ZoneInfo("Asia/Kolkata")
_OPEN = datetime(2024, 1, 2, 9, 15, tzinfo=_IST)
_SMALL = 1_000
_LARGE = 10_000


def _session(n: int) -> list[CandleBar]:
    """``n`` one-second-spaced bars (single IST session, all before 15:20).

    A single open position rides the whole session (no SL/TP breach, no forced
    bar) so the walk scans every bar — the O(N)-total / O(1)-per-bar hot path.
    """
    base = Decimal("100.00")
    bars: list[CandleBar] = []
    for i in range(n):
        ts = (_OPEN + timedelta(seconds=i)).astimezone(UTC)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=base,
                high=base + Decimal("0.05"),
                low=base - Decimal("0.05"),
                close=base,
                volume=0,
                open_interest=None,
            )
        )
    return bars


def _per_bar_ns(n: int, *, iterations: int = 30) -> float:
    bars = _session(n)
    for _ in range(3):  # warm
        simulate_session_mode_a(
            bars, signal_bars=[0], direction=Side.BUY, sl_paisa=10**9, tp_paisa=10**9
        )
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        simulate_session_mode_a(
            bars, signal_bars=[0], direction=Side.BUY, sl_paisa=10**9, tp_paisa=10**9
        )
    t1 = time.perf_counter_ns()
    return (t1 - t0) / iterations / n  # per-bar nanoseconds


class TestTradeSimIsO1PerBar:
    def test_small_scale_runs(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        bars = _session(_SMALL)
        result = benchmark(
            lambda: simulate_session_mode_a(
                bars, signal_bars=[0], direction=Side.BUY, sl_paisa=10**9, tp_paisa=10**9
            )
        )
        assert result == []  # rides to end, no forced bar -> drop-incomplete

    def test_per_bar_ratio_is_o1(self) -> None:
        """Per-bar walk cost at 10k / 1k bars must stay O(1) (< ceiling)."""
        small = _per_bar_ns(_SMALL)
        large = _per_bar_ns(_LARGE)
        ratio = large / max(small, 1e-3)
        assert ratio < ratio_ceiling(5.0), (
            f"trade_sim per-bar ratio {ratio:.2f}× (10k vs 1k bars) exceeds 5× — "
            f"the walk may have regressed from O(1)-per-bar. "
            f"small={small:.1f}ns/bar, large={large:.1f}ns/bar."
        )
