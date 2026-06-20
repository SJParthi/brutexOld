"""O(1) benchmark for the movers snapshot cache (DEC-MOVERS-O1-LATENCY-001).

The guarantee we prove: a **cache-served** ``MoversSnapshotCache.get_or_compute``
(the per-interaction hot path after the first view of a coordinate — every
sub-filter switch, ``top_n`` nudge, or re-selection) has latency **independent of
how many instruments the candidate set holds**. A clean hit returns the cached
candidate set via a single O(1) dict handoff under one lock — it must NOT re-scan,
so serving a snapshot of 1000 instruments is as fast as one of 10.

This is the read-side counterpart to the honest envelope in
``brutex/dashboard/movers_cache.py``: the per-interaction HIT is O(1); the first
computation of a coordinate is O(instruments × bars-read) and is NOT what this
bench measures (the cache is primed first, then every timed call is a hit). A
regression that re-scanned the lake on every interaction (the lag this PR fixes)
would make the latency scale with the candidate count and blow past the ceiling.

Strategy mirrors ``tests/bench/test_summary_cache_o1.py``: warm up, then time many
clean-hit ``get_or_compute`` calls at 10 vs 1000 candidates and assert the latency
ratio stays under ``ratio_ceiling(5.0)`` — 5× on the shared GHA runner, tightened
to the strict 1.1× M4-Pro tier via ``tests/bench/_o1_tiers.py``.
"""

from __future__ import annotations

from typing import Any

from brutex.dashboard.movers_cache import MoversSnapshotCache, candidate_key

from tests.bench._o1_tiers import ratio_ceiling


def _candidates(n: int) -> tuple[dict[str, Any], ...]:
    """An *n*-instrument candidate set (a stand-in for the MoverSnapshot tuple)."""
    return tuple({"symbol": f"SYM{i}", "pct_change": i, "volume": i} for i in range(n))


def _primed_cache(n_instruments: int) -> tuple[MoversSnapshotCache, str]:
    """Return a cache primed with an *n_instruments* candidate set + its key (clean entry)."""
    cache = MoversSnapshotCache()
    key = candidate_key(asset="spot", exchange="NSE", interval="1day", date="2024-06-13", time=None)
    payload = _candidates(n_instruments)
    cache.get_or_compute(key, generation=0, compute=lambda: payload)  # one scan → cached
    return cache, key


def _hit_compute_should_not_run() -> tuple[dict[str, Any], ...]:
    """A compute that must NEVER be called on a clean-hit path."""
    raise AssertionError("clean-hit get_or_compute must not recompute")


class TestCachedHitIsO1:
    """A cache-served hit is O(1) in the candidate count (no rescan)."""

    def test_cached_hit_at_10(self, benchmark: Any) -> None:
        cache, key = _primed_cache(10)

        def _get() -> tuple[dict[str, Any], ...]:
            return cache.get_or_compute(key, generation=0, compute=_hit_compute_should_not_run)

        result = benchmark(_get)
        assert len(result) == 10

    def test_cached_hit_at_1000(self, benchmark: Any) -> None:
        cache, key = _primed_cache(1000)

        def _get() -> tuple[dict[str, Any], ...]:
            return cache.get_or_compute(key, generation=0, compute=_hit_compute_should_not_run)

        result = benchmark(_get)
        assert len(result) == 1000

    def test_cached_hit_ratio_below_ceiling(self) -> None:
        """Empirical latency ratio at 1000 / 10 candidates must be < the ceiling.

        The ceiling is ``ratio_ceiling(5.0)`` — 5× on the shared GHA runner,
        tightened to the strict 1.1× M4-Pro tier (DEC-O1-STRICT-TIER-001). A
        regression that re-scanned on every interaction would scale with the
        candidate count and blow past this bound — the explicit O(1) hot-path gate.
        """
        import time

        cache_small, key_small = _primed_cache(10)
        cache_big, key_big = _primed_cache(1000)

        def _time(cache: MoversSnapshotCache, key: str, iterations: int) -> float:
            start = time.perf_counter()
            for _ in range(iterations):
                cache.get_or_compute(key, generation=0, compute=_hit_compute_should_not_run)
            return time.perf_counter() - start

        iterations = 20_000
        _time(cache_small, key_small, 2_000)  # warm up
        _time(cache_big, key_big, 2_000)
        small = _time(cache_small, key_small, iterations)
        big = _time(cache_big, key_big, iterations)
        # Guard against a zero-division on an absurdly fast machine.
        ratio = big / small if small > 0 else 1.0
        assert ratio < ratio_ceiling(5.0), f"cached-hit latency ratio {ratio:.2f} scaled with size"
