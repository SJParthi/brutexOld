"""O(1) benchmarks for the strategy-mask evaluation hot path (`combo_mask`).

DEC-ENGINE-DEDUP-NAN-CANON-001 (audit O(1) coverage). The strategy-mask
AND-evaluation (:func:`brutex.engine.combo_mask.evaluate_mask`) is THE inner
operation of a full sweep — one ``u64 AND`` + one ``CMP``, run ~33B times
(CLAUDE.md §2 Rule 5 / §12). This bench pins that the **per-mask** evaluation
cost is independent of how many masks the sweep ranges over, so the hot path
stays a constant two-instruction sequence rather than degrading to anything
super-constant.

* :func:`evaluate_mask` — one mask test must be latency-independent of the total
  number of masks evaluated (a single bitwise AND, not a function of the grid
  size).
* :func:`mask_to_entry_signals` — the per-bar projection of one mask over a bar
  series must be O(1) per bar (the per-bar cost is flat whether the series is 1k
  or 10k bars long; the total is O(n) bars, the per-bar amortised cost is O(1)).

Mirrors ``tests/bench/test_engine_results_store_o1.py``: an empirical ratio at
10× scale with CI-jitter headroom (5× on shared CI). Both ratios route their
M4-Pro strict ceiling through :func:`tests.bench._o1_tiers.cache_tier_ceiling`
(the DEC-O1-CACHE-TIER-001 measured 3× cache allowance) rather than the raw
1.1×: the AND/CMP op itself is a constant two-instruction sequence, but the
MEASURED ratio is dominated by iterating the ``N_LARGE`` (10k) mask / bar list
vs the ``N_SMALL`` (1k) one — a larger working set that crosses a CPU cache
tier into DRAM (~1.0–1.12× measured, hovering AT the 1.1× boundary under
shared-VM jitter — a cache-cliff, not an algorithmic regression). This is the
SAME allowance the throughput canary routes its 1M-elem vectorised mask-AND
through (``cache_tier_ceiling(8.0)``; DEC-O1-BENCH-GATE-COMPLETE-001). A genuine
super-constant per-mask cost (an O(n) wrapper) still blows well past 3× and
fails. Shared CI is byte-unchanged (``cache_tier_ceiling`` returns the caller's
5× default when the strict env is unset).
"""

from __future__ import annotations

import time

from brutex.engine.combo_mask import evaluate_mask, mask_to_entry_signals

from tests.bench._o1_tiers import cache_tier_ceiling

_SMALL = 1_000
_LARGE = 10_000

#: A dense 52-bit truth-vector + a sub-mask of it (every required bit set, so the
#: AND/CMP exercises the full-width path).
_BAR_BITS = (1 << 52) - 1
_MASK = 0b1010_1100_1111_0001


def _masks(n: int) -> list[int]:
    """Build *n* distinct sub-masks of the dense bar (all satisfiable)."""
    return [(_MASK ^ (i & 0xFFFF)) | _MASK for i in range(n)]


class TestEvaluateMaskIsO1:
    def test_evaluate_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        result = benchmark(lambda: evaluate_mask(_BAR_BITS, _MASK))
        assert result is True

    def test_evaluate_ratio_is_o1(self) -> None:
        """Per-mask evaluate latency is flat at 10k masks vs 1k masks (O(1))."""

        def _time(n: int) -> float:
            masks = _masks(n)
            for m in masks[:50]:  # warm
                evaluate_mask(_BAR_BITS, m)
            t0 = time.perf_counter_ns()
            for m in masks:
                evaluate_mask(_BAR_BITS, m)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < cache_tier_ceiling(5.0), (
            f"evaluate_mask per-mask ratio {ratio:.2f}× (10k vs 1k masks) exceeds "
            f"the ceiling — the AND/CMP hot path may have regressed from O(1). "
            f"t_1k={t_small:.1f}ns, t_10k={t_large:.1f}ns."
        )


class TestMaskProjectionIsO1:
    def test_project_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        bars = [_BAR_BITS] * _SMALL
        result = benchmark(lambda: mask_to_entry_signals(bars, _MASK))
        assert len(result) == _SMALL
        assert all(result)

    def test_project_per_bar_ratio_is_o1(self) -> None:
        """Per-bar projection cost is flat at 10k bars vs 1k bars (amortised O(1))."""

        def _time(n: int) -> float:
            bars = [_BAR_BITS] * n
            mask_to_entry_signals(bars[:50], _MASK)  # warm
            t0 = time.perf_counter_ns()
            mask_to_entry_signals(bars, _MASK)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < cache_tier_ceiling(5.0), (
            f"mask_to_entry_signals per-bar ratio {ratio:.2f}× (10k vs 1k bars) "
            f"exceeds the ceiling — the per-bar projection may have regressed "
            f"from O(1). t_1k={t_small:.1f}ns, t_10k={t_large:.1f}ns."
        )
