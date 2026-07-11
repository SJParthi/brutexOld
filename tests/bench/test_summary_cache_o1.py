"""O(1) benchmark for the dashboard summary cache (DEC-DASHBOARD-FASTREAD-001).

The guarantee we prove: a **cache-served** ``SummaryCache.get_or_refresh`` (the
per-poll hot path the coverage page hits every 4 s) has latency **independent of
how many datasets the lake holds**. A clean hit returns the cached payload via a
single O(1) dict handoff under one lock — it must NOT re-scan, so serving a
summary of 1000 datasets is as fast as serving one of 10.

This is the read-side counterpart to the honest envelope in
``brutex/dashboard/summary_cache.py``: the per-poll READ is O(1); the amortised
refresh is O(files) and is NOT what this bench measures (the cache is primed
first, then every timed call is a hit).

Strategy (mirrors the historical ``tests/bench/test_store_o1.py``, deleted
with ``orchestrator/store.py`` — MIGRATION.md #1727b): warm up, then time many
clean-hit ``get_or_refresh`` calls at 10 vs 1000 datasets and assert the latency
ratio stays under ``ratio_ceiling(5.0)`` — 5× on the shared GHA runner, tightened
to the strict 1.1× M4-Pro tier via ``tests/bench/_o1_tiers.py``. A regression
that re-scanned on every poll (the bug this PR fixes) would make the ratio scale
with dataset count and blow past the ceiling.
"""

from __future__ import annotations

import time
from typing import Any

from brutex.dashboard.summary_cache import SPOT, SummaryCache

from tests.bench._o1_tiers import ratio_ceiling

pytest_plugins: list[str] = []


def _payload(n: int) -> list[dict[str, Any]]:
    """Build an *n*-dataset summary payload (the shape summarize_* returns)."""
    return [
        {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": f"SYM{i}",
            "interval": "1minute",
            "bars": i,
            "first_date": "2021-01-01",
            "last_date": "2026-06-03",
        }
        for i in range(n)
    ]


def _primed_cache(n_datasets: int) -> SummaryCache:
    """Return a SummaryCache primed with an *n_datasets* payload (clean entry)."""
    cache = SummaryCache()  # default 2 s TTL floor; never marked dirty here
    payload = _payload(n_datasets)
    cache.get_or_refresh(SPOT, recompute=lambda: payload)  # one scan → cached
    return cache


def _hit_recompute_should_not_run() -> list[dict[str, Any]]:
    """A recompute that must NEVER be called on a clean-hit path."""
    raise AssertionError("clean-hit get_or_refresh must not recompute")


class TestCachedGetIsO1:
    """A cache-served get is O(1) in the dataset count (no rescan)."""

    def test_cached_get_at_10_datasets(self, benchmark: Any) -> None:
        cache = _primed_cache(10)

        def _get() -> list[dict[str, Any]]:
            return cache.get_or_refresh(SPOT, recompute=_hit_recompute_should_not_run)

        result = benchmark(_get)
        assert len(result) == 10

    def test_cached_get_at_1000_datasets(self, benchmark: Any) -> None:
        cache = _primed_cache(1000)

        def _get() -> list[dict[str, Any]]:
            return cache.get_or_refresh(SPOT, recompute=_hit_recompute_should_not_run)

        result = benchmark(_get)
        assert len(result) == 1000

    def test_cached_get_ratio_below_ceiling(self) -> None:
        """Empirical latency ratio at 1000 / 10 datasets must be < the ceiling.

        The ceiling is ``ratio_ceiling(5.0)`` — 5× on the shared GHA runner,
        tightened to the strict 1.1× M4-Pro tier (DEC-O1-STRICT-TIER-001). A
        regression that rescanned on every poll would scale with the dataset
        count and blow past this bound — the explicit O(1) cached-read gate.
        """
        iterations = 50_000

        def _time_hits(n_datasets: int) -> float:
            cache = _primed_cache(n_datasets)
            # Warm up the hot path (and confirm the no-recompute invariant).
            for _ in range(1_000):
                cache.get_or_refresh(SPOT, recompute=_hit_recompute_should_not_run)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                cache.get_or_refresh(SPOT, recompute=_hit_recompute_should_not_run)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_10 = _time_hits(10)
        t_1000 = _time_hits(1000)
        ratio = t_1000 / max(t_10, 1.0)

        assert ratio < ratio_ceiling(5.0), (
            f"cached get latency ratio {ratio:.2f}× (1000 vs 10 datasets) exceeds "
            f"the 5× CI ceiling — the summary cache may have regressed to a "
            f"per-poll rescan. t_10={t_10:.0f}ns, t_1000={t_1000:.0f}ns."
        )


class TestCounterSnapshotIsO1:
    """The incremental coverage-counter snapshot is O(1)-ish in label count.

    A snapshot is one dict copy — its cost grows with the number of distinct
    labels (it copies them), but the live ``add`` (the write-path hot call) is a
    single int bump, so a re-pull never re-scans the lake. We bench the snapshot
    copy at 10 vs 1000 labels to confirm it stays cheap + linear, never quadratic.
    """

    def test_counter_snapshot_at_10_labels(self, benchmark: Any) -> None:
        from brutex.dashboard.summary_cache import CoverageCounters

        counters = CoverageCounters()
        for i in range(10):
            counters.add(f"L{i}", i + 1)

        result = benchmark(counters.snapshot)
        assert result.total_bars == sum(range(1, 11))

    def test_counter_snapshot_at_1000_labels(self, benchmark: Any) -> None:
        from brutex.dashboard.summary_cache import CoverageCounters

        counters = CoverageCounters()
        for i in range(1000):
            counters.add(f"L{i}", i + 1)

        result = benchmark(counters.snapshot)
        assert result.total_bars == sum(range(1, 1001))
