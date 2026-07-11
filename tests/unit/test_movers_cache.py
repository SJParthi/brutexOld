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

# The sizer-fallback pin reads a module-private constant deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import threading
from typing import Any

import pytest
from brutex.dashboard.movers_cache import (
    _DEEP_SIZE_FALLBACK_BYTES,
    DEFAULT_ADAPTIVE_BASE_FLOOR_S,
    DEFAULT_MOVERS_CACHE_CAPACITY,
    MAX_ADAPTIVE_FLOOR_CAP_S,
    MoversSnapshotCache,
    candidate_key,
    deep_size_bytes,
    revision_generation,
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


class TestRevisionGeneration:
    """The token→generation digest bridge (DEC-LEDGER-DELTA-O-PAGE-001).

    Standalone twin of the wired pins in
    ``test_dashboard_db_view_ledger_read_cache.py`` so this module's OWN
    stem-matched suite proves the bridge (the discovery-gate contract).
    Homed HERE — beside the :class:`MoversSnapshotCache` whose generations it
    feeds — not in ``lake_revision`` (a ``db_view → lake_revision`` import
    edge closes the CodeQL ``py/cyclic-import`` cycle, alert 690).
    """

    def test_deterministic_64bit_and_token_sensitive(self) -> None:
        """Same token → same generation; any token change moves it; fits u64."""
        first = revision_generation("base:1|deltas:a.parquet")
        assert first == revision_generation("base:1|deltas:a.parquet")
        assert 0 <= first < 2**64
        assert first != revision_generation("base:1|deltas:b.parquet")


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
            # Under DEC-SCAN-SINGLE-FLIGHT-001 exactly ONE thread computes and
            # the other serves the stored result — either way the value matches
            # (the scan is a deterministic function of the coordinate).
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
        assert cache.size() == 1  # exactly one resident entry


class TestSingleFlight:
    """Per-key single-flight (DEC-SCAN-SINGLE-FLIGHT-001).

    The 2026-07-05 restart log measured TEN concurrent cold ``/movers``
    coordinates EACH paying the full O(files) discovery walk (~29.7 min per
    call). Concurrent misses of one key must now share exactly ONE compute.
    """

    def test_same_key_concurrent_misses_compute_once(self) -> None:
        cache = MoversSnapshotCache()
        compute_started = threading.Event()
        release = threading.Event()
        calls = {"n": 0}

        def _slow_compute() -> str:
            calls["n"] += 1
            compute_started.set()
            assert release.wait(timeout=10)
            return "candidates"

        results: list[str] = []

        def _worker() -> None:
            results.append(cache.get_or_compute("k", generation=0, compute=_slow_compute))

        lead = threading.Thread(target=_worker)
        lead.start()
        assert compute_started.wait(timeout=10)  # the leader is registered + scanning
        waiters = [threading.Thread(target=_worker) for _ in range(9)]
        for t in waiters:
            t.start()
        release.set()
        lead.join(timeout=10)
        for t in waiters:
            t.join(timeout=10)
        assert not lead.is_alive() and not any(t.is_alive() for t in waiters)
        assert calls["n"] == 1  # ONE scan — never ten
        assert results == ["candidates"] * 10

    def test_different_keys_still_compute_in_parallel(self) -> None:
        """Single-flight is PER KEY — two coordinates never serialise each other."""
        cache = MoversSnapshotCache()
        both_running = threading.Barrier(2, timeout=10)

        def _compute_a() -> str:
            both_running.wait()  # deadlocks unless B's compute runs CONCURRENTLY
            return "a"

        def _compute_b() -> str:
            both_running.wait()
            return "b"

        results: dict[str, str] = {}
        ta = threading.Thread(
            target=lambda: results.__setitem__(
                "a", cache.get_or_compute("ka", generation=0, compute=_compute_a)
            )
        )
        tb = threading.Thread(
            target=lambda: results.__setitem__(
                "b", cache.get_or_compute("kb", generation=0, compute=_compute_b)
            )
        )
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)
        assert results == {"a": "a", "b": "b"}

    def test_waiter_takes_over_after_compute_exception(self) -> None:
        """A raised in-flight compute wakes the waiter, which computes fresh."""
        cache = MoversSnapshotCache()
        compute_started = threading.Event()
        release = threading.Event()

        class _Boom(RuntimeError):
            pass

        def _failing() -> str:
            compute_started.set()
            assert release.wait(timeout=10)
            raise _Boom

        errors: list[BaseException] = []
        results: list[str] = []

        def _leader() -> None:
            try:
                cache.get_or_compute("k", generation=0, compute=_failing)
            except _Boom as exc:
                errors.append(exc)

        def _waiter() -> None:
            results.append(cache.get_or_compute("k", generation=0, compute=lambda: "recovered"))

        lead = threading.Thread(target=_leader)
        lead.start()
        assert compute_started.wait(timeout=10)
        wait_thread = threading.Thread(target=_waiter)
        wait_thread.start()
        release.set()
        lead.join(timeout=10)
        wait_thread.join(timeout=10)
        assert not lead.is_alive() and not wait_thread.is_alive()
        assert len(errors) == 1  # the exception propagated untouched (nothing cached)
        assert results == ["recovered"]  # the waiter took over

    def test_newer_generation_waiter_recomputes(self) -> None:
        """A waiter at a NEWER generation never serves the in-flight stale value."""
        cache = MoversSnapshotCache()
        compute_started = threading.Event()
        release = threading.Event()

        def _old_gen_compute() -> str:
            compute_started.set()
            assert release.wait(timeout=10)
            return "old"

        results: dict[str, str] = {}

        def _leader() -> None:
            results["old"] = cache.get_or_compute("k", generation=0, compute=_old_gen_compute)

        def _new_gen_reader() -> None:
            results["new"] = cache.get_or_compute("k", generation=1, compute=lambda: "fresh")

        lead = threading.Thread(target=_leader)
        lead.start()
        assert compute_started.wait(timeout=10)
        reader = threading.Thread(target=_new_gen_reader)
        reader.start()
        release.set()
        lead.join(timeout=10)
        reader.join(timeout=10)
        assert results == {"old": "old", "new": "fresh"}  # never a stale cross-serve
        # The newest store wins the resident slot for the key.
        assert cache.get_or_compute("k", generation=1, compute=lambda: "unused") == "fresh"

    def test_reentrant_miss_computes_without_deadlock(self) -> None:
        """A compute that RE-ENTERS the same key on the same thread never hangs."""
        cache = MoversSnapshotCache()
        inner_value: list[str] = []

        def _outer() -> str:
            inner_value.append(cache.get_or_compute("k", generation=0, compute=lambda: "inner"))
            return "outer"

        done: list[str] = []

        def _run() -> None:
            done.append(cache.get_or_compute("k", generation=0, compute=_outer))

        worker = threading.Thread(target=_run)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()  # no self-deadlock
        assert inner_value == ["inner"]
        assert done == ["outer"]


# ── deep_size_bytes + the opt-in byte watermark (DEC-MEMORY-FOOTPRINT-HARDENING-001) ─


class TestDeepSizeBytes:
    """The best-effort deep sizer — an approximation, cycle/depth/fault-safe."""

    def test_containers_count_deeper_than_shallow(self) -> None:
        import sys as _sys

        payload = {"rows": [bytes(1000) for _ in range(10)], "note": "x" * 500}
        deep = deep_size_bytes(payload)
        assert deep > _sys.getsizeof(payload)  # the dict shell alone is tiny
        assert deep > 10 * 1000 + 500  # the real content is counted

    def test_scalars_and_none_count_shallow(self) -> None:
        import sys as _sys

        assert deep_size_bytes(None) == _sys.getsizeof(None)
        assert deep_size_bytes(True) == _sys.getsizeof(True)
        assert deep_size_bytes(42) == _sys.getsizeof(42)
        assert deep_size_bytes(1.5) == _sys.getsizeof(1.5)
        assert deep_size_bytes("abc" * 100) == _sys.getsizeof("abc" * 100)
        assert deep_size_bytes(b"xyz" * 100) == _sys.getsizeof(b"xyz" * 100)

    def test_tuple_set_and_frozenset_recurse(self) -> None:
        import sys as _sys

        blob = bytes(4096)
        for container in ((blob,), {blob}, frozenset({blob})):
            assert deep_size_bytes(container) >= _sys.getsizeof(container) + _sys.getsizeof(blob)

    def test_cycle_is_guarded(self) -> None:
        loop: list[object] = []
        loop.append(loop)  # self-referencing list
        assert deep_size_bytes(loop) > 0  # terminates, never RecursionError

    def test_shared_subobject_counts_once_per_call(self) -> None:
        shared = bytes(8192)
        single = deep_size_bytes([shared])
        double = deep_size_bytes([shared, shared])
        # The second reference adds only pointer-level bytes, not another 8 KiB.
        assert double - single < 4096

    def test_depth_cap_terminates_a_pathological_nest(self) -> None:
        deep: object = "leaf"
        for _ in range(200):
            deep = [deep]
        assert deep_size_bytes(deep) > 0  # bounded walk, never a raise

    def test_dataclass_like_dict_and_slots_attributes_counted(self) -> None:
        import sys as _sys
        from dataclasses import dataclass

        @dataclass(frozen=True, slots=True)
        class _SlotsRow:
            blob: bytes

        class _DictObj:
            def __init__(self) -> None:
                self.blob = bytes(2048)

        assert deep_size_bytes(_SlotsRow(blob=bytes(2048))) > 2048
        assert deep_size_bytes(_DictObj()) > 2048

        # A bare-STRING __slots__ is one attribute name, not iterated char-wise.
        class _StrSlot:
            __slots__ = "blob"  # noqa: PLC0205 — the bare-string-slots ARM is the test subject

            def __init__(self) -> None:
                self.blob = bytes(2048)

        assert deep_size_bytes(_StrSlot()) > 2048

        # An UNSET slot holds nothing to size (the AttributeError-continue arm).
        class _UnsetSlot:
            __slots__ = ("never_set",)

        assert deep_size_bytes(_UnsetSlot()) >= _sys.getsizeof(_UnsetSlot())

    def test_raising_sizeof_degrades_to_the_constant_fallback(self) -> None:
        class _Weird:
            __slots__ = ()

            def __sizeof__(self) -> int:
                raise RuntimeError("no size for you")

        # _shallow_size degrades to the documented constant — never a raise.
        assert deep_size_bytes(_Weird()) == _DEEP_SIZE_FALLBACK_BYTES

    def test_walk_fault_degrades_to_shallow_size_of_the_whole_value(self) -> None:
        import sys as _sys

        class _RaisingItems(dict):  # type: ignore[type-arg]  # test double
            def items(self) -> Any:
                raise RuntimeError("mid-walk mutation stand-in")

        poisoned = _RaisingItems(a=1)
        # The outer guard catches the walk fault → shallow size of the value.
        assert deep_size_bytes(poisoned) == _sys.getsizeof(poisoned)


class TestByteWatermark:
    """The opt-in ``max_bytes`` watermark: LRU-first byte eviction, never the newest."""

    @staticmethod
    def _payload(n: int) -> bytes:
        return bytes(n)

    def test_rejects_non_positive_max_bytes(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            MoversSnapshotCache(max_bytes=0)
        with pytest.raises(ValueError, match="max_bytes"):
            MoversSnapshotCache(max_bytes=-1)

    def test_default_none_never_sizes_and_never_evicts_for_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The parity pin: without a watermark the sizer is NEVER called and no
        byte-watermark event can fire — behaviour byte-identical to before."""
        import brutex.dashboard.movers_cache as mod

        def _must_not_size(_value: object) -> int:
            raise AssertionError("deep_size_bytes must not run without a watermark")

        monkeypatch.setattr(mod, "deep_size_bytes", _must_not_size)
        cache = MoversSnapshotCache(capacity=4)
        with capture_logs() as logs:
            for i in range(8):  # over capacity → count-LRU eviction still works
                assert (
                    cache.get_or_compute(
                        f"k{i}", generation=0, compute=lambda i=i: self._payload(10_000)
                    )
                    is not None
                )
            assert cache.get_or_compute(
                "k7", generation=0, compute=lambda: b"MISS"
            ) == self._payload(10_000)
        assert cache.total_bytes() == 0
        assert cache.max_bytes is None
        assert not [e for e in logs if e["event"].startswith("movers_cache.byte_watermark")]

    def test_lru_first_eviction_keeps_total_at_or_under_watermark(self) -> None:
        """The eviction proof: known ~10 KB payloads under a 25 KB watermark —
        LRU evicted first, the tracked total never ends an insert over budget."""
        payload = self._payload(10_000)
        per_entry = deep_size_bytes(payload)
        cache = MoversSnapshotCache(capacity=100, max_bytes=25_000)
        with capture_logs() as logs:
            for name in ("a", "b", "c", "d", "e"):
                cache.get_or_compute(name, generation=0, compute=lambda: payload)
        assert cache.total_bytes() <= 25_000
        assert cache.total_bytes() == 2 * per_entry  # exactly two ~10 KB entries fit
        assert cache.size() == 2
        # LRU-first: the two NEWEST keys survive; the older three recompute.
        calls = {"n": 0}

        def _recompute() -> bytes:
            calls["n"] += 1
            return payload

        for name in ("d", "e"):
            cache.get_or_compute(name, generation=0, compute=_recompute)
        assert calls["n"] == 0  # both are hits — the just-inserted side was never evicted
        cache.get_or_compute("a", generation=0, compute=_recompute)
        assert calls["n"] == 1  # the LRU entry was evicted → honest recompute
        # ONE eviction-burst INFO per evicting insert, with a count — not per entry.
        bursts = [e for e in logs if e["event"] == "movers_cache.byte_watermark_evicted"]
        assert len(bursts) == 3  # inserts c, d, e each evicted exactly one LRU entry
        assert all(e["evicted"] == 1 for e in bursts)

    def test_single_over_budget_entry_is_kept_alone_with_one_warning_per_episode(self) -> None:
        huge = self._payload(100_000)
        cache = MoversSnapshotCache(capacity=100, max_bytes=25_000)
        with capture_logs() as logs:
            # Breach episode: the over-budget entry is KEPT (never evict the
            # just-inserted entry) and serves as a normal hit afterwards.
            assert cache.get_or_compute("huge", generation=0, compute=lambda: huge) == huge
            assert cache.get_or_compute("huge", generation=0, compute=lambda: b"MISS") == huge
            assert cache.size() == 1
            # A SECOND over-budget insert in the same episode: the prior entry is
            # evicted (LRU, len > 1) and the episode continues — NO second WARN.
            assert cache.get_or_compute("huge2", generation=0, compute=lambda: huge) == huge
            assert cache.size() == 1
            # The episode ENDS when an insert lands the total back under…
            small = self._payload(100)
            cache.get_or_compute("huge2", generation=1, compute=lambda: small)
            assert cache.total_bytes() <= 25_000
            # …so the NEXT breach warns again.
            cache.get_or_compute("huge3", generation=0, compute=lambda: huge)
        warns = [e for e in logs if e["event"] == "movers_cache.byte_watermark_single_entry_over"]
        assert len(warns) == 2  # one per breach EPISODE, not per over-budget insert
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["key"] == "huge"
        assert warns[1]["key"] == "huge3"

    def test_same_key_replace_retires_the_prior_entry_bytes(self) -> None:
        cache = MoversSnapshotCache(capacity=8, max_bytes=1_000_000)
        big = self._payload(50_000)
        small = self._payload(100)
        cache.get_or_compute("k", generation=0, compute=lambda: big)
        big_total = cache.total_bytes()
        assert big_total >= 50_000
        # A generation bump re-inserts the SAME key: the old bytes are retired.
        cache.get_or_compute("k", generation=1, compute=lambda: small)
        assert cache.total_bytes() == deep_size_bytes(small)

    def test_capacity_eviction_also_retires_bytes(self) -> None:
        payload = self._payload(10_000)
        per_entry = deep_size_bytes(payload)
        cache = MoversSnapshotCache(capacity=2, max_bytes=1_000_000)
        for name in ("a", "b", "c"):
            cache.get_or_compute(name, generation=0, compute=lambda: payload)
        assert cache.size() == 2
        assert cache.total_bytes() == 2 * per_entry  # the count-evicted entry retired its bytes

    def test_watermark_cache_single_flight_unchanged(self) -> None:
        """The 8-thread single-flight law holds under a watermark instance."""
        cache = MoversSnapshotCache(capacity=8, max_bytes=1_000_000)
        barrier = threading.Barrier(8, timeout=10)
        calls = {"n": 0}
        lock = threading.Lock()

        def _compute() -> bytes:
            with lock:
                calls["n"] += 1
            return self._payload(10_000)

        results: list[bytes] = []

        def _worker() -> None:
            barrier.wait()
            results.append(cache.get_or_compute("k", generation=0, compute=_compute))

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in threads)
        assert calls["n"] == 1  # exactly ONE compute across 8 concurrent cold misses
        assert results == [self._payload(10_000)] * 8


class TestSizingHint:
    """The opt-in ``size_of`` sizing-hint seam (hostile-review M1 on
    DEC-MEMORY-FOOTPRINT-HARDENING-001): consulted FIRST under a watermark, a
    non-``None`` return replaces the O(payload) deep walk; ``None`` / a raising
    hint fall back to ``deep_size_bytes``; without a watermark NEITHER runs."""

    def test_hint_short_circuits_the_deep_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-None hint is the entry's byte size — deep_size_bytes never runs.

        Fails before the M1 fix: the constructor has no ``size_of`` parameter
        (TypeError), and behaviourally every watermarked insert paid the deep
        walk (measured 5.28 s at the 841k-row production ledger scale).
        """
        import brutex.dashboard.movers_cache as mod

        def _must_not_deep_size(_value: object) -> int:
            raise AssertionError("the hint must short-circuit deep_size_bytes")

        monkeypatch.setattr(mod, "deep_size_bytes", _must_not_deep_size)
        cache = MoversSnapshotCache(capacity=4, max_bytes=1_000_000, size_of=lambda _v: 12_345)
        assert cache.get_or_compute("k", generation=0, compute=lambda: b"payload") == b"payload"
        assert cache.total_bytes() == 12_345  # the hint IS the tracked figure

    def test_hint_none_falls_back_to_the_deep_walk(self) -> None:
        """An unrecognized shape (hint → None) still gets the accurate walk."""
        payload = bytes(10_000)
        cache = MoversSnapshotCache(capacity=4, max_bytes=1_000_000, size_of=lambda _v: None)
        cache.get_or_compute("k", generation=0, compute=lambda: payload)
        assert cache.total_bytes() == deep_size_bytes(payload)

    def test_raising_hint_falls_back_never_raises(self) -> None:
        """A hint fault must never break an insert — deep-size fallback."""
        payload = bytes(10_000)

        def _boom(_value: object) -> int:
            raise RuntimeError("hint blew up")

        cache = MoversSnapshotCache(capacity=4, max_bytes=1_000_000, size_of=_boom)
        assert cache.get_or_compute("k", generation=0, compute=lambda: payload) == payload
        assert cache.total_bytes() == deep_size_bytes(payload)

    def test_negative_hint_clamps_to_zero(self) -> None:
        """A buggy negative hint can never drive the running total negative."""
        cache = MoversSnapshotCache(capacity=4, max_bytes=1_000_000, size_of=lambda _v: -99)
        cache.get_or_compute("k", generation=0, compute=lambda: b"payload")
        assert cache.total_bytes() == 0

    def test_no_watermark_never_consults_the_hint(self) -> None:
        """Without ``max_bytes`` the hint is ignored entirely — the parity law."""

        def _must_not_hint(_value: object) -> int:
            raise AssertionError("size_of must not run without a watermark")

        cache = MoversSnapshotCache(capacity=4, size_of=_must_not_hint)
        assert cache.get_or_compute("k", generation=0, compute=lambda: "v") == "v"
        assert cache.total_bytes() == 0

    def test_compute_duration_includes_sizing_cost(self) -> None:
        """Honest floor accounting (M1): ``compute_duration`` — the #920
        adaptive floor's input — covers compute PLUS sizing.

        Fails before the M1 fix: ``finished_at`` was captured BEFORE the sizer
        ran, so a 1 s compute + 5 s sizing recorded ``compute_duration=1.0``
        and the floor under-throttled the genuinely ~6 s cold cycle to its
        2 s base (the reviewer's ~2 s floor for a ~7 s real cycle).
        """
        clock = _TickClock()

        def _slow_size(_value: object) -> int:
            clock.advance(5.0)  # the deliberately slow sizing step
            return 100

        cache = MoversSnapshotCache(
            max_bytes=1_000_000,
            size_of=_slow_size,
            pull_active_probe=lambda: True,
            adaptive_floor=True,
            clock=clock,
        )
        cache.get_or_compute("k", generation=1, compute=lambda: _slow_compute(clock, 1.0, "v1"))
        entry = cache._entries["k"]
        assert entry.compute_duration == pytest.approx(6.0)  # compute (1 s) + sizing (5 s)
        assert entry.computed_at == pytest.approx(6.0)  # stamped AFTER sizing
        # Behavioural teeth: at age 4 s the floor (max(2, 6) = 6 s) serves
        # stale under the pull — the pre-fix duration=1.0 floor (2 s) would
        # have recomputed here.
        clock.advance(4.0)

        def _must_not_run() -> str:
            raise AssertionError("within the compute+sizing floor — must serve stale")

        assert cache.get_or_compute("k", generation=2, compute=_must_not_run) == "v1"

    def test_lock_wait_stays_excluded_from_the_duration(self) -> None:
        """The duration covers compute + sizing, NEVER lock-wait: the clock is
        captured before the insert lock is taken (structural — the capture
        precedes the ``with self._lock`` block), so a contended lock cannot
        inflate the floor. Pinned via source order."""
        import inspect

        import brutex.dashboard.movers_cache as mod

        src = inspect.getsource(mod.MoversSnapshotCache.get_or_compute)
        sizing_at = src.index("entry_bytes = self._size_payload(value)")
        finished_at = src.index("finished_at = self._clock()")
        lock_at = src.index("with self._lock:", finished_at)
        assert sizing_at < finished_at < lock_at
