"""O(1)/candidate benchmark for the alpha-vs-buy-&-hold metric.

DEC-ALPHA-BUYHOLD-METRIC-001. The alpha computation is a stateless scalar
(four i64s in, one f64 out — seven IEEE ops, kernel-routed with the Python
oracle fallback), so its per-candidate latency must be:

* independent of how many candidates were scored before it (no accumulating
  state — a top100 build scores O(pool) candidates in one pass), and
* independent of the input MAGNITUDE (a ±2**60-paisa extreme costs the same
  seven ops as a production-scale value).

Mirrors the ``_o1_tiers`` two-tier pattern via :func:`ratio_ceiling` —
robust ``< 5×`` on shared CI. Honest tier note: this file is NOT in
bench-m4pro.yml's curated strict list, so the decreed 1.1× M4-Pro strict
ceiling is DORMANT here unless the file joins that list; the shared robust
ceiling is the operative gate. HONEST envelope: this pins the SCALAR's
flatness — the top100 build's alpha loop is O(pool) by construction (one
scalar call per candidate), stated, never claimed O(1)-total.
"""

from __future__ import annotations

import time
from typing import Final

from brutex.engine.alpha_metric import alpha_vs_buyhold_pct

from tests.bench._o1_tiers import ratio_ceiling

#: Per-call latency must not grow between these two call-batch sizes.
_SMALL: Final[int] = 1_000
_LARGE: Final[int] = 50_000


def _score_once(i: int) -> float:
    """Score one candidate (vary the P&L so no accidental memoization hides)."""
    return alpha_vs_buyhold_pct(10_000_000 + i, 100_000_000, 2_500_000, 2_600_000)


class TestAlphaIsO1PerCandidate:
    """``alpha_vs_buyhold_pct`` is O(1) per candidate — flat over a build."""

    def test_scalar_value_sanity(self) -> None:
        assert _score_once(0) == 6.0

    def test_alpha_at_small_scale(self, benchmark) -> None:
        assert benchmark(lambda: _score_once(7)) > 0.0

    def test_alpha_at_large_scale(self, benchmark) -> None:
        assert benchmark(lambda: _score_once(123_457)) > 0.0

    def test_per_candidate_ratio_flat(self) -> None:
        """Per-call latency over a 50k batch must match a 1k batch."""

        def _time(n: int) -> float:
            for i in range(50):  # warm (resolves the kernel memo once)
                _ = _score_once(i)
            t0 = time.perf_counter_ns()
            for i in range(n):
                _ = _score_once(i)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"alpha per-candidate cost grew with batch size: "
            f"{t_small:.1f} ns → {t_large:.1f} ns ({ratio:.2f}x)"
        )

    def test_magnitude_invariance(self) -> None:
        """A ±2**60 extreme costs the same as a production-scale value."""

        def _time(pnl: int, equity: int, anchor: int, close: int) -> float:
            for _ in range(50):
                _ = alpha_vs_buyhold_pct(pnl, equity, anchor, close)
            t0 = time.perf_counter_ns()
            for _ in range(_SMALL):
                _ = alpha_vs_buyhold_pct(pnl, equity, anchor, close)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / _SMALL

        t_production = _time(10_000_000, 100_000_000, 2_500_000, 2_600_000)
        t_extreme = _time(-(2**60), 2**60 - 1, 2**60 - 3, 2**60 - 5)
        ratio = t_extreme / max(t_production, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"alpha cost grew with input magnitude: "
            f"{t_production:.1f} ns → {t_extreme:.1f} ns ({ratio:.2f}x)"
        )
