"""Unit tests for the keyed movers snapshot cache (DEC-MOVERS-O1-LATENCY-001).

Proves :class:`brutex.dashboard.movers_cache.MoversSnapshotCache` +
:func:`~brutex.dashboard.movers_cache.candidate_key`: a clean hit never recomputes,
a generation bump (a pull) invalidates, the LRU bound is honoured, two concurrent
cold misses both resolve idempotently to the same value, and a ``compute`` that
raises propagates without poisoning the cache. Pure (no lake / FastAPI) — the
cache is the generic speed core, fully exercised with plain closures + threads.
"""

from __future__ import annotations

import threading

import pytest
from brutex.dashboard.movers_cache import (
    DEFAULT_MOVERS_CACHE_CAPACITY,
    MoversSnapshotCache,
    candidate_key,
)


class TestCandidateKey:
    """``candidate_key`` builds a stable, collision-free key from the coordinates."""

    def test_spot_key_omits_options_dims(self) -> None:
        key = candidate_key(
            asset="spot", exchange="NSE", interval="1day", date="2024-06-13", time=None
        )
        assert key == "spot|NSE|1day|2024-06-13|||"

    def test_none_time_and_empty_time_key_identically(self) -> None:
        a = candidate_key(
            asset="spot", exchange=None, interval="5minute", date="2024-06-13", time=None
        )
        b = candidate_key(
            asset="spot", exchange=None, interval="5minute", date="2024-06-13", time=""
        )
        assert a == b == "spot||5minute|2024-06-13|||"

    def test_options_key_includes_tier_and_track(self) -> None:
        key = candidate_key(
            asset="options",
            exchange="BSE",
            interval="1day",
            date="2024-06-13",
            time="09:20",
            expiry_tier="near",
            expiry_track="monthly",
        )
        assert key == "options|BSE|1day|2024-06-13|09:20|near|monthly"

    def test_spot_and_options_keys_distinct_for_same_coordinate(self) -> None:
        spot = candidate_key(
            asset="spot", exchange="NSE", interval="1day", date="2024-06-13", time=None
        )
        opt = candidate_key(
            asset="options",
            exchange="NSE",
            interval="1day",
            date="2024-06-13",
            time=None,
            expiry_tier="near",
            expiry_track="all",
        )
        assert spot != opt


class TestHitAndMiss:
    """A miss computes once; a clean hit returns the cached value with no recompute."""

    def test_miss_computes_then_hit_does_not(self) -> None:
        cache = MoversSnapshotCache()
        calls = {"n": 0}

        def _compute() -> str:
            calls["n"] += 1
            return f"v{calls['n']}"

        first = cache.get_or_compute("k", generation=0, compute=_compute)
        second = cache.get_or_compute("k", generation=0, compute=_compute)
        assert first == "v1"
        assert second == "v1"  # served from cache — NOT recomputed
        assert calls["n"] == 1

    def test_hit_never_calls_compute(self) -> None:
        cache = MoversSnapshotCache()
        cache.get_or_compute("k", generation=3, compute=lambda: "seed")

        def _must_not_run() -> str:
            raise AssertionError("clean hit must not recompute")

        assert cache.get_or_compute("k", generation=3, compute=_must_not_run) == "seed"

    def test_distinct_keys_each_compute(self) -> None:
        cache = MoversSnapshotCache()
        a = cache.get_or_compute("a", generation=0, compute=lambda: "A")
        b = cache.get_or_compute("b", generation=0, compute=lambda: "B")
        assert (a, b) == ("A", "B")
        assert cache.size() == 2


class TestGenerationInvalidation:
    """A newer generation (a pull/materialize) makes the entry dirty → recompute."""

    def test_generation_bump_recomputes(self) -> None:
        cache = MoversSnapshotCache()
        calls = {"n": 0}

        def _compute() -> int:
            calls["n"] += 1
            return calls["n"]

        assert cache.get_or_compute("k", generation=0, compute=_compute) == 1
        assert cache.get_or_compute("k", generation=0, compute=_compute) == 1  # hit
        assert cache.get_or_compute("k", generation=1, compute=_compute) == 2  # dirty → recompute
        assert cache.get_or_compute("k", generation=1, compute=_compute) == 2  # hit at new gen


class TestLruEviction:
    """The cache is bounded — the least-recently-used entry is evicted over capacity."""

    def test_evicts_least_recently_used(self) -> None:
        cache = MoversSnapshotCache(capacity=2)
        cache.get_or_compute("a", generation=0, compute=lambda: "A")
        cache.get_or_compute("b", generation=0, compute=lambda: "B")
        # Touch "a" so "b" becomes the LRU, then insert "c" → "b" is evicted.
        cache.get_or_compute("a", generation=0, compute=lambda: "A2")  # hit (no recompute)
        cache.get_or_compute("c", generation=0, compute=lambda: "C")
        assert cache.size() == 2

        recomputed: list[str] = []

        def _track(label: str) -> str:
            recomputed.append(label)
            return label

        # "a" + "c" are still cached (no recompute); "b" was evicted (recomputes).
        cache.get_or_compute("a", generation=0, compute=lambda: _track("a"))
        cache.get_or_compute("c", generation=0, compute=lambda: _track("c"))
        cache.get_or_compute("b", generation=0, compute=lambda: _track("b"))
        assert recomputed == ["b"]

    def test_capacity_and_size_accessors(self) -> None:
        cache = MoversSnapshotCache(capacity=5)
        assert cache.capacity == 5
        assert cache.size() == 0

    def test_default_capacity(self) -> None:
        assert MoversSnapshotCache().capacity == DEFAULT_MOVERS_CACHE_CAPACITY

    def test_rejects_non_positive_capacity(self) -> None:
        with pytest.raises(ValueError, match="capacity must be >= 1"):
            MoversSnapshotCache(capacity=0)


class TestComputeRaises:
    """A ``compute`` that raises propagates, caches nothing, and clears in-flight."""

    def test_exception_propagates_and_nothing_cached(self) -> None:
        cache = MoversSnapshotCache()

        def _boom() -> str:
            raise ValueError("bad time")

        with pytest.raises(ValueError, match="bad time"):
            cache.get_or_compute("k", generation=0, compute=_boom)
        assert cache.size() == 0
        # A subsequent good compute on the SAME key works (in-flight was cleared).
        assert cache.get_or_compute("k", generation=0, compute=lambda: "ok") == "ok"


class TestConcurrentMissIsIdempotent:
    """Two concurrent cold misses both succeed with the same value (idempotent)."""

    def test_concurrent_misses_return_same_value(self) -> None:
        cache = MoversSnapshotCache()
        release = threading.Event()
        results: dict[str, str] = {}

        def _compute() -> str:
            # Both threads may compute (no cross-thread wait by design); the scan
            # is a deterministic function of the coordinate, so the value matches.
            release.wait(timeout=5)
            return "value"

        def _worker(name: str) -> None:
            results[name] = cache.get_or_compute("k", generation=0, compute=_compute)

        threads = [threading.Thread(target=_worker, args=(n,)) for n in ("t1", "t2")]
        for t in threads:
            t.start()
        release.set()
        for t in threads:
            t.join(timeout=5)
        assert results == {"t1": "value", "t2": "value"}
        assert cache.size() == 1  # last store wins → exactly one resident entry
