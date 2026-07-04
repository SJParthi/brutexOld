"""OFF-mode cost gate for the opt-in sweep look-ahead barrier.

DEC-SWEEP-LOOKAHEAD-GUARD-001's hard constraint: with the guard OFF (the
default), the sweep hot path must be byte-identical AND cost-unchanged —
no per-bar flag check may exist anywhere. Three complementary proofs:

1. **Structural (the load-bearing one):** ``maybe_guard_bars`` OFF returns
   the caller's bars object ITSELF (``result is bars``). No wrapper exists,
   so every downstream per-bar loop runs over the identical unwrapped
   sequence — the OFF hot path is the pre-change hot path by construction.
2. **O(1)-in-N seam cost:** the ONE flag resolution per sweep run costs the
   same for a 1k-bar and a 100k-bar window (it never touches the bars), so
   the setup seam adds zero per-bar work. Ratio routed through the COMMON
   two-tier ceiling (:func:`tests.bench._o1_tiers.ratio_ceiling`).
3. **A/B wall-clock:** the OFF-mode seam call (env resolved, identity
   returned) is nil next to even ONE per-bar operation over the window —
   the median seam cost must be under the median cost of a bare
   ``for bar in bars: pass`` walk, so it cannot hide a per-bar loop.

The existing sweep-path O(1) gates (``tests/bench/test_engine_sweep_run_o1.py``
etc.) remain the regression net for the sweep's own hot paths — this file
only pins the NEW seam's OFF-mode cost.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.sweep_lookahead import SWEEP_LOOKAHEAD_GUARD_ENV, maybe_guard_bars

from tests.bench._o1_tiers import ratio_ceiling

_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

_SMALL = 1_000
_LARGE = 100_000


def _bars(n: int) -> list[CandleBar]:
    price = Decimal(100)
    return [
        CandleBar(
            timestamp=_OPEN_UTC + timedelta(minutes=i),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0,
            open_interest=None,
        )
        for i in range(n)
    ]


def _median_seam_seconds(bars: list[CandleBar], *, rounds: int = 200) -> float:
    """Median wall-clock of ONE OFF-mode ``maybe_guard_bars`` seam call."""
    samples: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        result = maybe_guard_bars(bars)
        samples.append(time.perf_counter() - t0)
        assert result is bars  # identity on every sample — never a wrapper
    samples.sort()
    return samples[len(samples) // 2]


class TestOffModeIsFree:
    def test_off_returns_identical_object_small_and_large(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The structural zero-cost proof: OFF is the identity function."""
        monkeypatch.delenv(SWEEP_LOOKAHEAD_GUARD_ENV, raising=False)
        for n in (_SMALL, 10_000):
            bars = _bars(n)
            assert maybe_guard_bars(bars) is bars

    def test_off_seam_cost_is_independent_of_bar_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The OFF seam never touches the bars: cost(100k) ≈ cost(1k)."""
        monkeypatch.delenv(SWEEP_LOOKAHEAD_GUARD_ENV, raising=False)
        small = _bars(_SMALL)
        large = _bars(_LARGE)
        _median_seam_seconds(small, rounds=50)  # warm-up
        t_small = _median_seam_seconds(small)
        t_large = _median_seam_seconds(large)
        # Sub-microsecond medians divide noisily; floor both at 100 ns so the
        # ratio compares real work, not timer granularity (house convention).
        floor = 1e-7
        ratio = max(t_large, floor) / max(t_small, floor)
        assert ratio <= ratio_ceiling(5.0), (
            f"OFF-mode seam cost scaled with bar count: {t_small * 1e9:.0f} ns @ {_SMALL} vs "
            f"{t_large * 1e9:.0f} ns @ {_LARGE} (ratio {ratio:.2f})"
        )

    def test_off_seam_cost_is_below_one_bare_walk_over_the_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A/B: the whole OFF seam costs less than ONE no-op per-bar walk.

        If the seam secretly did ANY per-bar work (the forbidden per-bar flag
        check), its cost would be at least comparable to a bare
        ``for bar in bars: pass`` loop over the same window. Requiring the
        seam's median to sit UNDER the bare walk's median (with a generous 2×
        head-room for shared-CI timer noise) pins "resolved ONCE at setup"
        mechanically.
        """
        monkeypatch.delenv(SWEEP_LOOKAHEAD_GUARD_ENV, raising=False)
        bars = _bars(_LARGE)
        _median_seam_seconds(bars, rounds=50)  # warm-up
        seam = _median_seam_seconds(bars)

        walk_samples: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            for _bar in bars:
                pass
            walk_samples.append(time.perf_counter() - t0)
        walk_samples.sort()
        walk = walk_samples[len(walk_samples) // 2]

        assert seam < walk * 2.0, (
            f"OFF-mode seam ({seam * 1e6:.1f} µs) is not nil next to a bare "
            f"{_LARGE}-bar walk ({walk * 1e6:.1f} µs) — a per-bar cost is hiding in the seam"
        )
