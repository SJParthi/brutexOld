"""Unit tests for the keyed movers snapshot cache (DEC-MOVERS-O1-LATENCY-001).

Proves :class:`brutex.dashboard.movers_cache.MoversSnapshotCache` +
:func:`~brutex.dashboard.movers_cache.candidate_key`: a clean hit never recomputes,
a generation bump (a pull) invalidates, the LRU bound is honoured, two concurrent
cold misses both resolve idempotently to the same value, and a ``compute`` that
raises propagates without poisoning the cache. Plus the OPT-IN pull-time
adaptive floor (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001 — the ported #920 pattern:
stale-serve under pull within ``min(max(base, duration), cap)``, force_fresh
bypass, probe-error swallow, default byte-identical). Pure (no lake / FastAPI)
— the cache is the generic speed core, fully exercised with plain closures +
threads.
"""

from __future__ import annotations

import threading

import pytest
from brutex.dashboard.movers_cache import (
    DEFAULT_ADAPTIVE_BASE_FLOOR_S,
    DEFAULT_MOVERS_CACHE_CAPACITY,
    MAX_ADAPTIVE_FLOOR_CAP_S,
    MoversSnapshotCache,
    candidate_key,
)
from structlog.testing import capture_logs


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

    def test_default_capacity_holds_the_warm_grid(self) -> None:
        # DEC-MOVERS-WARM-GRID-001: the post-pull warmer warms the whole default-day grid
        # (options 3×3×3=27 + futures 3×3=9 + spot 3 = 39 keys); the default capacity must
        # exceed that PLUS headroom for the operator's live coordinates, or a warmed entry
        # would be evicted before its first /movers open (defeating the warm).
        from brutex.dashboard.movers_warm import (
            futures_warm_grid,
            options_warm_grid,
            spot_warm_grid,
        )

        warm_grid = len(options_warm_grid()) + len(futures_warm_grid()) + len(spot_warm_grid())
        assert warm_grid == 39
        assert warm_grid < DEFAULT_MOVERS_CACHE_CAPACITY

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


class _TickClock:
    """A manually-advanced monotonic clock for deterministic floor tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _slow_compute(clock: _TickClock, duration: float, value: str) -> str:
    """Simulate a walk that takes *duration* seconds on the injected clock."""
    clock.advance(duration)
    return value


class TestAdaptiveFloorOptIn:
    """The OPT-IN pull-time stale-serve floor (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001)."""

    def test_stale_served_under_pull_within_floor(self) -> None:
        """REGRESSION (fail-before proven): on origin/main the constructor has no
        ``adaptive_floor``/``pull_active_probe`` parameters (this test errors with
        ``TypeError``) and behaviourally EVERY generation mismatch recomputed
        immediately — the discovery walk re-fired back-to-back during a pull.
        After the fix, a mismatch within the floor while the pull runs serves the
        STALE entry with ONE ``movers_cache.stale_served`` log (stale=True)."""
        clock = _TickClock()
        cache = MoversSnapshotCache(
            pull_active_probe=lambda: True, adaptive_floor=True, clock=clock
        )
        # Seed: a 30 s walk at generation 1 → floor = max(2, 30) = 30 s.
        assert (
            cache.get_or_compute(
                "k", generation=1, compute=lambda: _slow_compute(clock, 30.0, "v1")
            )
            == "v1"
        )
        clock.advance(10.0)  # 10 s < the 30 s floor

        def _must_not_run() -> str:
            raise AssertionError("stale-under-pull within floor must not recompute")

        with capture_logs() as logs:
            served = cache.get_or_compute("k", generation=2, compute=_must_not_run)
        assert served == "v1"  # the STALE entry, honestly served under the pull
        events = [e for e in logs if e["event"] == "movers_cache.stale_served"]
        assert len(events) == 1
        assert events[0]["stale"] is True
        assert events[0]["key"] == "k"
        assert events[0]["entry_generation"] == 1
        assert events[0]["generation"] == 2

    def test_recomputes_past_floor_even_under_pull(self) -> None:
        clock = _TickClock()
        cache = MoversSnapshotCache(
            pull_active_probe=lambda: True, adaptive_floor=True, clock=clock
        )
        cache.get_or_compute("k", generation=1, compute=lambda: _slow_compute(clock, 30.0, "v1"))
        clock.advance(31.0)  # past the 30 s floor → the walk re-runs even mid-pull
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v2"

    def test_no_pull_recomputes_immediately(self) -> None:
        # Probe False ⇒ today's behaviour byte-identically (the floor never applies).
        clock = _TickClock()
        cache = MoversSnapshotCache(
            pull_active_probe=lambda: False, adaptive_floor=True, clock=clock
        )
        cache.get_or_compute("k", generation=1, compute=lambda: _slow_compute(clock, 30.0, "v1"))
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v2"

    def test_default_cache_never_serves_stale(self) -> None:
        # The DEFAULT (no opt-in) pin: byte-identical pre-floor behaviour even
        # with a probe wired — adaptive_floor=False keeps every mismatch fresh.
        clock = _TickClock()
        cache = MoversSnapshotCache(pull_active_probe=lambda: True, clock=clock)
        cache.get_or_compute("k", generation=1, compute=lambda: _slow_compute(clock, 30.0, "v1"))
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v2"

    def test_opt_in_without_probe_never_serves_stale(self) -> None:
        # adaptive_floor=True but NO probe ⇒ pull state is unknowable ⇒ fresh.
        clock = _TickClock()
        cache = MoversSnapshotCache(adaptive_floor=True, clock=clock)
        cache.get_or_compute("k", generation=1, compute=lambda: _slow_compute(clock, 30.0, "v1"))
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v2"

    def test_probe_exception_swallowed_to_false(self) -> None:
        def _boom() -> bool:
            raise RuntimeError("probe blew up")

        clock = _TickClock()
        cache = MoversSnapshotCache(pull_active_probe=_boom, adaptive_floor=True, clock=clock)
        cache.get_or_compute("k", generation=1, compute=lambda: _slow_compute(clock, 30.0, "v1"))
        # The raising probe reads as "no pull" → recompute, never a crash.
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v2"

    def test_force_fresh_bypasses_floor(self) -> None:
        # The post-pipeline discovery warm runs while the job is still `running`,
        # so it MUST bypass the floor to land its fresh seed.
        clock = _TickClock()
        cache = MoversSnapshotCache(
            pull_active_probe=lambda: True, adaptive_floor=True, clock=clock
        )
        cache.get_or_compute("k", generation=1, compute=lambda: _slow_compute(clock, 30.0, "v1"))
        clock.advance(1.0)  # deep inside the floor
        assert (
            cache.get_or_compute("k", generation=2, compute=lambda: "fresh", force_fresh=True)
            == "fresh"
        )
        # And the seed is now the clean hit at generation 2.
        assert cache.get_or_compute("k", generation=2, compute=lambda: "later") == "fresh"

    def test_force_fresh_clean_hit_still_serves_cache(self) -> None:
        # force_fresh bypasses the STALE floor, never the clean-hit fast path.
        cache = MoversSnapshotCache(
            pull_active_probe=lambda: True, adaptive_floor=True, clock=_TickClock()
        )
        cache.get_or_compute("k", generation=1, compute=lambda: "seed")

        def _must_not_run() -> str:
            raise AssertionError("clean hit must not recompute even with force_fresh")

        assert (
            cache.get_or_compute("k", generation=1, compute=_must_not_run, force_fresh=True)
            == "seed"
        )

    def test_fast_walk_keeps_base_floor(self) -> None:
        # duration ≈ 0 ⇒ floor = base (2 s): within 2 s stale-served, past it fresh.
        clock = _TickClock()
        cache = MoversSnapshotCache(
            pull_active_probe=lambda: True, adaptive_floor=True, clock=clock
        )
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock.advance(DEFAULT_ADAPTIVE_BASE_FLOOR_S - 0.5)
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v1"
        clock.advance(1.0)  # now past the 2 s base floor
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v2"

    def test_floor_capped_at_max(self) -> None:
        # A pathological 10 000 s walk is capped at 600 s — bounded staleness.
        clock = _TickClock()
        cache = MoversSnapshotCache(
            pull_active_probe=lambda: True, adaptive_floor=True, clock=clock
        )
        cache.get_or_compute(
            "k", generation=1, compute=lambda: _slow_compute(clock, 10_000.0, "v1")
        )
        clock.advance(MAX_ADAPTIVE_FLOOR_CAP_S + 1.0)  # past the cap (< the raw 10 000)
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v2"

    def test_stale_serve_keeps_key_mru(self) -> None:
        # A stale-served key is marked most-recently-used so the LRU never
        # evicts the very entry the pull-time floor is protecting.
        clock = _TickClock()
        cache = MoversSnapshotCache(
            capacity=2, pull_active_probe=lambda: True, adaptive_floor=True, clock=clock
        )
        cache.get_or_compute("a", generation=1, compute=lambda: _slow_compute(clock, 30.0, "A"))
        cache.get_or_compute("b", generation=1, compute=lambda: "B")
        clock.advance(1.0)
        assert cache.get_or_compute("a", generation=2, compute=lambda: "A2") == "A"  # stale-served
        cache.get_or_compute("c", generation=2, compute=lambda: "C")  # evicts LRU = "b"
        assert cache.size() == 2
        recomputed: list[str] = []
        cache.get_or_compute("b", generation=1, compute=lambda: recomputed.append("b") or "B2")  # type: ignore[func-returns-value]
        assert recomputed == ["b"]  # "b" was the evictee — "a" survived its stale serve

    def test_stale_serve_survives_concurrent_eviction(self) -> None:
        """The stale-serve MRU touch tolerates a CONCURRENT eviction.

        The pull-active probe runs OUTSIDE the cache lock (it takes the pull
        job's own lock), so another thread can evict the stale entry between
        the stale decision and the MRU re-touch. The read must still serve the
        snapshot it captured under the first lock — never a ``KeyError`` on the
        vanished key, never a recompute (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001).
        Deterministically driven: the probe itself plays the concurrent writer,
        inserting a second key into a capacity-1 cache so ``"a"`` is evicted
        before the stale-serve re-acquires the lock.
        """
        clock = _TickClock()
        holder: dict[str, MoversSnapshotCache] = {}
        fired: list[bool] = []

        def _evicting_probe() -> bool:
            if not fired:  # fire exactly once — on the stale-decision probe.
                fired.append(True)
                holder["cache"].get_or_compute("b", generation=5, compute=lambda: "B")
            return True

        cache = MoversSnapshotCache(
            capacity=1, pull_active_probe=_evicting_probe, adaptive_floor=True, clock=clock
        )
        holder["cache"] = cache
        cache.get_or_compute("a", generation=1, compute=lambda: _slow_compute(clock, 30.0, "A"))
        clock.advance(1.0)  # deep inside the 30 s floor

        def _must_not_run() -> str:
            raise AssertionError("stale-under-pull within floor must not recompute")

        with capture_logs() as logs:
            assert cache.get_or_compute("a", generation=2, compute=_must_not_run) == "A"
        assert [e["key"] for e in logs if e["event"] == "movers_cache.stale_served"] == ["a"]
        # The concurrent insert won: "b" is the sole resident entry ("a" gone).
        assert cache.size() == 1
        assert cache.get_or_compute("b", generation=5, compute=_must_not_run) == "B"


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
