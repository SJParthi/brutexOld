"""Tests for `brutex.dashboard.summary_cache` (DEC-DASHBOARD-FASTREAD-001).

100% line + branch via a fake recompute + fake clock — deterministic, no real
threads, no lake:

* :class:`SummaryCache` — cold miss recomputes once; a clean hit serves the
  cache with ZERO recomputes; a dirty read past the 2 s TTL floor recomputes
  exactly once; a dirty read WITHIN the floor is throttled (stale served, no
  scan); ``ttl_floor_s <= 0`` disables the floor (every dirty read recomputes);
  ``generation`` reflects ``mark_dirty``; a concurrent in-flight refresh of the
  same kind serves the current entry (no double-scan); the ``recompute``-raises
  path still clears the in-flight guard. A barrier-synced concurrency test proves
  ``mark_dirty`` / ``get_or_refresh`` are thread-safe under contention.
* :class:`CoverageCounters` — ``add`` accumulates per label + total; ``bars == 0``
  AND ``bars < 0`` are no-ops (idempotent re-pull); ``snapshot`` is a consistent
  immutable copy; ``to_dict`` is JSON-friendly; a barrier-synced storm proves the
  tally is torn-state-free.
"""

# The default-clock test imports the private `_monotonic` helper.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import threading

from brutex.dashboard.summary_cache import (
    DEFAULT_TTL_FLOOR_S,
    LAKE_UNIQUENESS,
    OPTIONS,
    SPOT,
    CoverageCounters,
    CoverageCountersSnapshot,
    SummaryCache,
    _monotonic,
)


class _FakeClock:
    """A manually-advanced monotonic clock for deterministic TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, by: float) -> None:
        self._t += by


class _CountingRecompute:
    """A recompute closure that counts its calls and returns a stamped payload."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> list[dict[str, object]]:
        self.calls += 1
        return [{"call": self.calls}]


# ────────────────────────────────────────────────────────────────────────
# SummaryCache — hit / miss / dirty / TTL floor
# ────────────────────────────────────────────────────────────────────────


def test_default_monotonic_clock_returns_float() -> None:
    # The default (non-injected) clock is the real monotonic wall clock.
    t = _monotonic()
    assert isinstance(t, float)


def test_default_constructed_cache_uses_real_clock() -> None:
    # No injected clock → the default _monotonic is wired and a read works.
    cache = SummaryCache()
    rc = _CountingRecompute()
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 1}]
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 1}]  # clean hit
    assert rc.calls == 1


def test_cold_miss_recomputes_once() -> None:
    cache = SummaryCache(clock=_FakeClock())
    rc = _CountingRecompute()
    payload = cache.get_or_refresh(SPOT, recompute=rc)
    assert payload == [{"call": 1}]
    assert rc.calls == 1


def test_clean_hit_serves_cache_without_recompute() -> None:
    cache = SummaryCache(clock=_FakeClock())
    rc = _CountingRecompute()
    first = cache.get_or_refresh(SPOT, recompute=rc)
    # Second read with NO intervening mark_dirty → clean hit, no recompute.
    second = cache.get_or_refresh(SPOT, recompute=rc)
    assert first == second == [{"call": 1}]
    assert rc.calls == 1  # served from cache the second time


def test_dirty_past_ttl_floor_recomputes_once() -> None:
    clock = _FakeClock()
    cache = SummaryCache(clock=clock, ttl_floor_s=2.0)
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)  # call 1
    cache.mark_dirty()
    clock.advance(2.0)  # exactly at the floor (>= → eligible)
    payload = cache.get_or_refresh(SPOT, recompute=rc)
    assert payload == [{"call": 2}]
    assert rc.calls == 2


def test_dirty_within_ttl_floor_is_throttled() -> None:
    clock = _FakeClock()
    cache = SummaryCache(clock=clock, ttl_floor_s=2.0)
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)  # call 1
    cache.mark_dirty()
    clock.advance(1.9)  # still inside the 2 s floor
    payload = cache.get_or_refresh(SPOT, recompute=rc)
    # Stale entry served; the write storm does NOT trigger a rescan.
    assert payload == [{"call": 1}]
    assert rc.calls == 1


def test_many_mark_dirty_inside_floor_triggers_at_most_one_rescan() -> None:
    """A 200-symbol fan-out (hundreds of mark_dirty) → ONE rescan per window."""
    clock = _FakeClock()
    cache = SummaryCache(clock=clock, ttl_floor_s=2.0)
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)  # call 1 (cold)
    for _ in range(200):
        cache.mark_dirty()
        cache.get_or_refresh(SPOT, recompute=rc)  # all throttled within the floor
    assert rc.calls == 1  # the floor collapsed 200 dirty reads into zero rescans
    clock.advance(2.0)
    cache.get_or_refresh(SPOT, recompute=rc)  # now past the floor → exactly one
    assert rc.calls == 2


def test_zero_ttl_floor_recomputes_on_every_dirty_read() -> None:
    clock = _FakeClock()
    cache = SummaryCache(clock=clock, ttl_floor_s=0.0)
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)  # call 1
    cache.mark_dirty()
    cache.get_or_refresh(SPOT, recompute=rc)  # call 2 (no floor, same instant)
    cache.mark_dirty()
    cache.get_or_refresh(SPOT, recompute=rc)  # call 3
    assert rc.calls == 3


def test_distinct_kinds_are_namespaced() -> None:
    cache = SummaryCache(clock=_FakeClock())
    spot_rc = _CountingRecompute()
    opt_rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=spot_rc)
    cache.get_or_refresh(OPTIONS, recompute=opt_rc)
    # Each kind computed once; a second spot read hits cache, opt untouched.
    cache.get_or_refresh(SPOT, recompute=spot_rc)
    assert spot_rc.calls == 1
    assert opt_rc.calls == 1


def test_generation_reflects_mark_dirty() -> None:
    cache = SummaryCache(clock=_FakeClock())
    assert cache.generation == 0
    cache.mark_dirty()
    cache.mark_dirty()
    assert cache.generation == 2


def test_default_ttl_floor_constant() -> None:
    # The default floor is the documented 2 s (no surprise zero).
    cache = SummaryCache(clock=_FakeClock())
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    # 0 < DEFAULT_TTL_FLOOR_S elapsed → throttled.
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 1
    assert DEFAULT_TTL_FLOOR_S == 2.0


# ────────────────────────────────────────────────────────────────────────
# SummaryCache — ADAPTIVE TTL floor (DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001)
# ────────────────────────────────────────────────────────────────────────


class _SlowRecompute:
    """A recompute that ADVANCES the injected clock by *duration* (simulates a slow scan)."""

    def __init__(self, clock: _FakeClock, duration: float) -> None:
        self._clock = clock
        self._duration = duration
        self.calls = 0

    def __call__(self) -> list[dict[str, object]]:
        self.calls += 1
        self._clock.advance(self._duration)
        return [{"call": self.calls}]


def test_slow_scan_self_throttles_beyond_base_floor() -> None:
    # A 30 s scan makes the adaptive floor 30 s: a dirty read 10 s later is throttled
    # (the base 2 s floor alone would have rescanned), and one 35 s later rescans.
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)  # base 2 s floor
    rc = _SlowRecompute(clock, duration=30.0)
    cache.get_or_refresh(SPOT, recompute=rc)  # cold → scans, clock +30, duration=30
    assert rc.calls == 1
    cache.mark_dirty()
    clock.advance(10.0)  # 10 s < adaptive 30 s floor
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 1  # THROTTLED by the adaptive floor (base 2 s would have rescanned)
    clock.advance(25.0)  # now 35 s since the scan finished > 30 s
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2  # past the adaptive floor → rescans


def test_adaptive_floor_capped_at_max() -> None:
    # A pathologically slow 200 s scan is capped at MAX_ADAPTIVE_FLOOR_S (120 s), so a
    # dirty read 130 s later rescans (without the cap, a 200 s floor would throttle it).
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    rc = _SlowRecompute(clock, duration=200.0)
    cache.get_or_refresh(SPOT, recompute=rc)  # clock +200, duration=200 → capped to 120
    cache.mark_dirty()
    clock.advance(130.0)  # 130 s > 120 s cap (but < the uncapped 200 s)
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2  # the cap let it rescan; an uncapped 200 s floor would not have


def test_zero_base_floor_still_bounds_a_slow_scan() -> None:
    # ttl_floor_s=0 disables the BASE floor, but the adaptive component still throttles a
    # genuinely slow scan (so "no floor" never devolves into back-to-back 30 s scans).
    clock = _FakeClock()
    cache = SummaryCache(clock=clock, ttl_floor_s=0.0)
    rc = _SlowRecompute(clock, duration=30.0)
    cache.get_or_refresh(SPOT, recompute=rc)  # clock +30, duration=30
    cache.mark_dirty()
    clock.advance(5.0)  # 5 s < adaptive 30 s
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 1  # throttled by the adaptive floor despite ttl_floor_s=0


def test_nonmonotonic_clock_falls_back_to_base_floor() -> None:
    # A clock that goes BACKWARDS during recompute yields a NEGATIVE scan-duration delta.
    # The adaptive floor's `max(base, duration)` harmlessly neutralizes it — the effective
    # floor falls back to the base 2 s, never negative/zero (so no explicit clamp is
    # needed; the outer max is load-bearing, not a separate clamp).
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    rc = _SlowRecompute(clock, duration=-5.0)  # finishes BEFORE it started → negative delta
    cache.get_or_refresh(SPOT, recompute=rc)  # stored duration is -5; max(2, -5) = 2
    cache.mark_dirty()
    clock.advance(1.0)  # 1 s < base 2 s floor
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 1  # base 2 s floor in effect (negative duration neutralized by max)
    clock.advance(2.0)  # 3 s elapsed since the (negative-delta) scan → past the base floor
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2  # base floor elapsed → rescans


def test_in_flight_refresh_serves_current_entry_no_double_scan() -> None:
    """A concurrent dirty read of the SAME kind serves the stale entry, no rescan.

    Drives the ``kind in self._refreshing`` branch deterministically: the
    recompute, while running, re-enters ``get_or_refresh`` for the same kind. The
    re-entrant call must see the in-flight guard and return the current cached
    entry WITHOUT a second recompute (single-flight per kind).
    """
    clock = _FakeClock()
    cache = SummaryCache(clock=clock, ttl_floor_s=2.0)
    seed = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=seed)  # seed the cache (call 1)
    cache.mark_dirty()
    clock.advance(2.0)  # make the next read eligible

    reentrant_result: list[list[dict[str, object]]] = []
    outer_calls = {"n": 0}

    def reentrant_recompute() -> list[dict[str, object]]:
        outer_calls["n"] += 1
        # While THIS refresh is in flight, a second reader asks for the same kind.
        reentrant_result.append(cache.get_or_refresh(SPOT, recompute=seed))
        return [{"call": "fresh"}]

    final = cache.get_or_refresh(SPOT, recompute=reentrant_recompute)
    assert outer_calls["n"] == 1  # the outer refresh ran exactly once
    assert seed.calls == 1  # the re-entrant reader did NOT trigger a new scan
    assert reentrant_result == [[{"call": 1}]]  # it got the stale seeded entry
    assert final == [{"call": "fresh"}]


def test_cold_concurrent_refresh_recomputes_instead_of_keyerror() -> None:
    """A concurrent read on a COLD cache (no entry yet) recomputes — never KeyError.

    DEC-COMPLETENESS-O1-LATENCY-001 (hostile-review MAJOR): when reader A is
    mid-recompute on a kind that has NEVER been populated, reader B used to hit the
    ``kind in self._refreshing`` branch, return ``None``, and then dereference the
    absent ``self._entries[kind]`` → ``KeyError`` → a 500. Sharing the SPOT entry
    across two concurrently-polled routes widened that cold-start exposure. With the
    cold-cache fall-through, B recomputes too (a transient, idempotent double-scan),
    so no ``KeyError`` can occur. Driven deterministically: the FIRST-EVER refresh
    re-enters ``get_or_refresh`` for the SAME kind while the cache is still cold (NO
    prior seed), so the re-entrant ``self._refreshing`` membership is hit with
    ``entry is None``.
    """
    cache = SummaryCache(ttl_floor_s=0.0)
    reentrant_result: list[list[dict[str, object]]] = []
    inner = _CountingRecompute()

    def outer_recompute() -> list[dict[str, object]]:
        # While THIS first-ever refresh is in flight (cache still cold), a second
        # reader asks for the same kind. It must recompute, NOT KeyError.
        reentrant_result.append(cache.get_or_refresh(SPOT, recompute=inner))
        return [{"call": "outer"}]

    final = cache.get_or_refresh(SPOT, recompute=outer_recompute)
    assert inner.calls == 1  # the cold re-entrant reader recomputed (no KeyError)
    assert reentrant_result == [[{"call": 1}]]  # it got its OWN fresh scan, not a 500
    assert final == [{"call": "outer"}]


def test_recompute_exception_clears_in_flight_guard() -> None:
    """If ``recompute`` raises, the in-flight guard is cleared so retries work."""
    clock = _FakeClock()
    cache = SummaryCache(clock=clock, ttl_floor_s=0.0)

    class _Boom(RuntimeError):
        pass

    def boom() -> list[dict[str, object]]:
        raise _Boom

    try:
        cache.get_or_refresh(SPOT, recompute=boom)
    except _Boom:
        pass
    else:  # pragma: no cover — the recompute must propagate
        raise AssertionError("expected the recompute exception to propagate")

    # The guard was cleared in `finally`, so a subsequent good read recomputes.
    rc = _CountingRecompute()
    payload = cache.get_or_refresh(SPOT, recompute=rc)
    assert payload == [{"call": 1}]
    assert rc.calls == 1


def test_concurrent_mark_dirty_and_get_are_thread_safe() -> None:
    """Barrier-synced storm: concurrent mark_dirty + get never corrupt state."""
    cache = SummaryCache(clock=_FakeClock(), ttl_floor_s=0.0)
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)  # seed
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            for _ in range(50):
                if i % 2 == 0:
                    cache.mark_dirty()
                else:
                    cache.get_or_refresh(SPOT, recompute=rc)
        except Exception as exc:  # pragma: no cover — any race is a failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # Generation advanced by exactly the 8 dirtying threads × 50 bumps.
    assert cache.generation == 8 * 50


# ────────────────────────────────────────────────────────────────────────
# revision_probe — cross-process freshness (DEC-HARDENING-LEFTOVERS-001)
# ────────────────────────────────────────────────────────────────────────


class _MutableProbe:
    """A revision probe whose token the test mutates between reads."""

    def __init__(self, token: str | None) -> None:
        self.token = token
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        return self.token


def test_probe_baseline_recorded_without_bump() -> None:
    """The FIRST non-None token is a baseline — recorded, generation NOT bumped."""
    probe = _MutableProbe("t1")
    cache = SummaryCache(clock=_FakeClock(), revision_probe=probe)
    assert cache.generation == 0
    assert probe.calls == 1


def test_probe_same_token_never_bumps() -> None:
    """Repeated reads with an unchanged token leave the generation alone."""
    probe = _MutableProbe("t1")
    cache = SummaryCache(clock=_FakeClock(), revision_probe=probe)
    assert cache.generation == 0
    assert cache.generation == 0
    assert cache.generation == 0
    assert probe.calls == 3


def test_probe_token_change_bumps_exactly_once() -> None:
    """A token CHANGE bumps the generation exactly once (then stays put)."""
    probe = _MutableProbe("t1")
    cache = SummaryCache(clock=_FakeClock(), revision_probe=probe)
    assert cache.generation == 0  # baseline
    probe.token = "t2"  # the CLI-process ledger checkpoint moved the token
    assert cache.generation == 1
    assert cache.generation == 1  # same token → no further bump


def test_probe_none_is_noop_then_baseline() -> None:
    """``None`` ("no signal") is a no-op; the first REAL token is the baseline."""
    probe = _MutableProbe(None)
    cache = SummaryCache(clock=_FakeClock(), revision_probe=probe)
    assert cache.generation == 0  # None → no-op, no baseline yet
    probe.token = "t1"
    assert cache.generation == 0  # first non-None → baseline, still no bump
    probe.token = "t2"
    assert cache.generation == 1  # change past the baseline → bump


def test_probe_interplay_with_mark_dirty() -> None:
    """mark_dirty is unchanged; probe bumps stack on top (double-bump is harmless)."""
    probe = _MutableProbe("t1")
    cache = SummaryCache(clock=_FakeClock(), revision_probe=probe)
    assert cache.generation == 0  # baseline
    cache.mark_dirty()  # the dashboard's own in-process write signal
    assert cache.generation == 1
    probe.token = "t2"  # ...and the ledger moved too (a dashboard pull does both)
    assert cache.generation == 2


def test_probe_reconciles_inside_get_or_refresh() -> None:
    """A token change dirties the SPOT/OPTIONS entries themselves — no
    ``generation`` read or ``mark_dirty`` needed (the CLI-write path)."""
    probe = _MutableProbe("t1")
    cache = SummaryCache(clock=_FakeClock(), ttl_floor_s=0.0, revision_probe=probe)
    rc = _CountingRecompute()
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 1}]  # seeds + baseline
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 1}]  # clean hit
    probe.token = "t2"  # a CLI checkpoint in another process
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 2}]  # dirty → rescanned
    assert rc.calls == 2


def test_stalled_probe_never_blocks_mark_dirty() -> None:
    """DEC-SURFACE-TRIAL-001 regression (MINOR-1): the probe stat runs OUTSIDE
    the cache lock, so a probe WEDGED on a stalled external volume can never
    block the pull's ``mark_dirty`` write funnel (or hold the lock at all).

    Before the fix the probe ran under ``self._lock`` inside ``generation``;
    a blocked probe held the lock indefinitely and this test deadlocked.
    """
    probe_entered = threading.Event()
    probe_release = threading.Event()

    def _stalled_probe() -> str | None:
        probe_entered.set()
        # Simulate a wedged os.stat on a stalled volume (bounded for safety).
        assert probe_release.wait(timeout=10.0), "test harness release never fired"
        return "t1"

    cache = SummaryCache(clock=_FakeClock(), revision_probe=_stalled_probe)
    reader = threading.Thread(target=lambda: cache.generation, daemon=True)
    reader.start()
    assert probe_entered.wait(timeout=5.0), "the generation read never reached the probe"

    # While the probe is STALLED, mark_dirty must complete promptly — it only
    # needs the lock, which the stalled probe must NOT be holding.
    dirtier = threading.Thread(target=cache.mark_dirty, daemon=True)
    dirtier.start()
    dirtier.join(timeout=2.0)
    stalled = dirtier.is_alive()
    # Unwedge the probe before asserting so a failure never leaks a live thread.
    probe_release.set()
    reader.join(timeout=5.0)
    assert not stalled, "mark_dirty blocked behind the stalled revision probe"
    # The stalled read's token lands as the baseline; the mark_dirty bump held.
    assert cache.generation == 1


def test_out_of_order_probe_tokens_over_invalidate_never_miss() -> None:
    """DEC-SURFACE-TRIAL-001 (MINOR-1 thread-safety envelope): tokens probed
    outside the lock may reconcile out of order — the documented worst case is
    a SPURIOUS extra bump (over-invalidation), never a missed change, and the
    generation stays monotonic. Deterministically replays the interleaving:
    baseline B → stale A applied late → B re-applied.
    """
    probe = _MutableProbe("B")
    cache = SummaryCache(clock=_FakeClock(), revision_probe=probe)
    seen = [cache.generation]  # B → baseline, no bump
    probe.token = "A"  # a STALE pre-write token applied after the fresh one
    seen.append(cache.generation)  # A ≠ B → bump (the spurious extra bump)
    probe.token = "B"  # the final on-disk state re-observed
    seen.append(cache.generation)  # B ≠ A → bump — the real change is NEVER missed
    assert seen == [0, 1, 2]
    assert seen == sorted(seen)  # monotonic — never decreases


# ────────────────────────────────────────────────────────────────────────
# cold-start-outage recovery bump (DEC-SUMMARY-CACHE-COLD-OUTAGE-BUMP-001)
# ────────────────────────────────────────────────────────────────────────


def test_cold_start_outage_recovery_bumps_once_and_recomputes() -> None:
    """The MINOR-2 envelope (DEC-SURFACE-TRIAL-001) MECHANISED: entries cached
    while a WIRED probe errors (pre-baseline ``None``) are invalidated ONCE on
    the first recovered token — a CLI write may have landed unseen during the
    outage — and the next read recomputes."""
    probe = _MutableProbe(None)  # the app starts mid-outage
    cache = SummaryCache(clock=_FakeClock(), ttl_floor_s=0.0, revision_probe=probe)
    rc = _CountingRecompute()
    # Populated DURING the outage — the caches the recovery must not trust.
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 1}]
    assert cache.generation == 0
    probe.token = "t1"  # the probe recovers
    assert cache.generation == 1  # recovery bump — exactly once
    assert cache.generation == 1  # same token → no further bump
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 2}]  # recomputed
    assert rc.calls == 2


def test_cold_start_outage_recovery_without_entries_is_plain_baseline() -> None:
    """An outage the app merely OBSERVED (nothing cached over it) records the
    recovered token as a plain no-bump baseline — the healthy-start contract."""
    probe = _MutableProbe(None)
    cache = SummaryCache(clock=_FakeClock(), revision_probe=probe)
    assert cache.generation == 0  # outage observed, no entries populated
    probe.token = "t1"
    assert cache.generation == 0  # recovery: baseline WITHOUT a bump


def test_cold_start_outage_bump_is_at_most_once_per_process() -> None:
    """After the recovery bump, a LATER outage + same-token recovery never
    bumps again — the baseline transition happens exactly once per process."""
    probe = _MutableProbe(None)
    cache = SummaryCache(clock=_FakeClock(), ttl_floor_s=0.0, revision_probe=probe)
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)  # outage-populated
    probe.token = "t1"
    assert cache.generation == 1  # THE one recovery bump
    probe.token = None  # a second, post-baseline outage...
    assert cache.generation == 1  # ...is a plain no-op (never re-arms)
    probe.token = "t1"  # ...and recovering to the SAME token
    assert cache.generation == 1  # never bumps again
    probe.token = "t2"  # a REAL change still bumps (the #1149 law intact)
    assert cache.generation == 2


def test_healthy_warm_start_with_entries_never_bumps() -> None:
    """A first probe call that ALREADY returns a token (no outage) records the
    baseline with NO bump even when an entry was populated before any
    ``generation`` read — a pre-first-probe-call population with a healthy
    probe must never look like an outage."""
    probe = _MutableProbe("t1")
    cache = SummaryCache(clock=_FakeClock(), ttl_floor_s=0.0, revision_probe=probe)
    rc = _CountingRecompute()
    # get_or_refresh probes BEFORE populating → baseline lands healthy, with
    # the entry stored under the un-bumped generation.
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 1}]
    assert cache.generation == 0
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 1}]  # clean hit
    assert rc.calls == 1


def test_unwired_probe_never_arms_the_outage_flag() -> None:
    """``revision_probe=None`` reconciles ``None`` on every read (the
    ``_probe_token`` no-probe path) — it must NEVER arm the outage flag or
    bump: the pre-probe byte-identical contract."""
    cache = SummaryCache(clock=_FakeClock(), ttl_floor_s=0.0)
    rc = _CountingRecompute()
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 1}]
    assert cache.generation == 0
    assert cache.generation == 0
    assert cache.get_or_refresh(SPOT, recompute=rc) == [{"call": 1}]  # clean hit
    assert rc.calls == 1


# ────────────────────────────────────────────────────────────────────────
# CoverageCounters — incremental, idempotent O(1) tally
# ────────────────────────────────────────────────────────────────────────


def test_counter_accumulates_per_label_and_total() -> None:
    counters = CoverageCounters()
    counters.add("NIFTY 1minute", 100)
    counters.add("NIFTY 1minute", 50)
    counters.add("BANKNIFTY 1minute", 7)
    snap = counters.snapshot()
    assert snap.by_label == {"NIFTY 1minute": 150, "BANKNIFTY 1minute": 7}
    assert snap.total_bars == 157
    assert snap.updates == 3


def test_counter_zero_bars_is_a_noop_idempotent_repull() -> None:
    counters = CoverageCounters()
    counters.add("NIFTY 1minute", 100)
    before = counters.snapshot()
    # A duplicate re-pull writes zero new bars post-dedup → no change at all.
    counters.add("NIFTY 1minute", 0)
    after = counters.snapshot()
    assert after.by_label == before.by_label == {"NIFTY 1minute": 100}
    assert after.total_bars == before.total_bars == 100
    assert after.updates == before.updates == 1  # the no-op did NOT advance updates


def test_counter_negative_bars_is_a_noop() -> None:
    counters = CoverageCounters()
    counters.add("X", -5)
    snap = counters.snapshot()
    assert snap.by_label == {}
    assert snap.total_bars == 0
    assert snap.updates == 0


def test_counter_snapshot_is_an_isolated_copy() -> None:
    counters = CoverageCounters()
    counters.add("X", 1)
    snap = counters.snapshot()
    # Mutating the snapshot's dict must not leak back into the live counter.
    snap.by_label["X"] = 999
    assert counters.snapshot().by_label == {"X": 1}


def test_counters_snapshot_to_dict_is_json_friendly() -> None:
    snap = CoverageCountersSnapshot(by_label={"A": 3}, total_bars=3, updates=1)
    assert snap.to_dict() == {"by_label": {"A": 3}, "total_bars": 3, "updates": 1}
    # to_dict copies the inner dict too (no shared mutable reference).
    snap.to_dict()["by_label"]["A"] = 99
    assert snap.by_label == {"A": 3}


def test_concurrent_counter_adds_are_thread_safe() -> None:
    """Barrier-synced storm of adds → the total is exact (no lost updates)."""
    counters = CoverageCounters()
    n_threads = 16
    per_thread = 100
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        barrier.wait()
        for _ in range(per_thread):
            counters.add("shared", 1)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = counters.snapshot()
    assert snap.total_bars == n_threads * per_thread
    assert snap.by_label == {"shared": n_threads * per_thread}
    assert snap.updates == n_threads * per_thread


# ────────────────────────────────────────────────────────────────────────
# PULL duty-degrade (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001)
# ────────────────────────────────────────────────────────────────────────


def test_pull_widens_floor_regression_27s_scan() -> None:
    """REGRESSION (fail-before proven): on origin/main ``SummaryCache`` has no
    ``set_pull_active_probe`` (this test errors with ``AttributeError``) and
    behaviourally a dirty read 30 s after a 27 s scan RECOMPUTED (the adaptive
    floor was exactly 27 s) — the measured p50 68 ms → 27.1 s scan re-fired on
    every poll during a pull. After the fix, with the pull probe True the
    effective floor widens to min(9 × 27, 600) = 243 s: the 30 s read serves
    the stale entry, and only a read past 243 s rescans (still bounded)."""
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    cache.set_pull_active_probe(lambda: True)
    rc = _SlowRecompute(clock, duration=27.0)
    cache.get_or_refresh(SPOT, recompute=rc)  # cold → scans, duration=27
    assert rc.calls == 1
    cache.mark_dirty()
    clock.advance(30.0)  # past today's 27 s adaptive floor — main RE-FIRED here
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 1  # widened pull floor (243 s) holds it back
    meta = cache.entry_meta(SPOT)
    assert meta is not None
    assert meta.duty_degraded is True  # the staleness is VISIBLE, never silent
    clock.advance(215.0)  # 245 s since the scan > the 243 s widened floor
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2  # bounded: even mid-pull the scan eventually re-fires
    assert cache.entry_meta(SPOT) is not None
    meta_after = cache.entry_meta(SPOT)
    assert meta_after is not None
    assert meta_after.duty_degraded is False  # fresh recompute clears the flag


def test_pull_widened_floor_capped_at_600() -> None:
    # 9 × 100 s = 900 s would exceed the cap → 600 s bounds the mid-pull staleness.
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    cache.set_pull_active_probe(lambda: True)
    rc = _SlowRecompute(clock, duration=100.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    clock.advance(610.0)  # past the 600 s cap (< the uncapped 900 s)
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2


def test_pull_floor_never_below_base_floor() -> None:
    # A fast scan (0.1 s) under pull: widened = 0.9 s < base 2 s → the base wins
    # (the widening can only ever RAISE the floor, never lower it).
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    cache.set_pull_active_probe(lambda: True)
    rc = _SlowRecompute(clock, duration=0.1)
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    clock.advance(1.0)  # inside the base 2 s floor
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 1  # still throttled by the BASE floor
    clock.advance(1.5)  # 2.5 s > base 2 s AND > widened 0.9 s
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2


def test_no_probe_is_byte_identical_to_today() -> None:
    # Default (no probe wired): the 27 s scan re-fires at 30 s exactly as today.
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    rc = _SlowRecompute(clock, duration=27.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    clock.advance(30.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2  # today's exact semantics preserved


def test_probe_false_is_byte_identical_to_today() -> None:
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    cache.set_pull_active_probe(lambda: False)
    rc = _SlowRecompute(clock, duration=27.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    clock.advance(30.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2


def test_probe_exception_swallowed_to_false() -> None:
    def _boom() -> bool:
        raise RuntimeError("probe blew up")

    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    cache.set_pull_active_probe(_boom)
    rc = _SlowRecompute(clock, duration=27.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    clock.advance(30.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2  # the raising probe reads "no pull" — never a crash


def test_probe_reset_to_none_disables_widening() -> None:
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    cache.set_pull_active_probe(lambda: True)
    cache.set_pull_active_probe(None)  # un-wire → today's semantics
    rc = _SlowRecompute(clock, duration=27.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    clock.advance(30.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 2


def test_entry_meta_none_before_first_compute() -> None:
    cache = SummaryCache(clock=_FakeClock())
    assert cache.entry_meta(SPOT) is None


def test_entry_meta_age_tracks_clock() -> None:
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)
    clock.advance(12.5)
    meta = cache.entry_meta(SPOT)
    assert meta is not None
    assert meta.age_s == 12.5
    assert meta.duty_degraded is False


def test_non_pull_throttle_is_not_duty_degraded() -> None:
    # A plain (no-pull) base-floor throttle serves stale but duty_degraded stays
    # False — the flag means "held back BY THE PULL", never a generic staleness bit.
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    clock.advance(1.0)  # inside the base 2 s floor, no pull
    cache.get_or_refresh(SPOT, recompute=rc)
    assert rc.calls == 1  # throttled
    meta = cache.entry_meta(SPOT)
    assert meta is not None
    assert meta.duty_degraded is False


def test_clean_hit_clears_duty_degraded() -> None:
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    cache.set_pull_active_probe(lambda: True)
    rc = _SlowRecompute(clock, duration=27.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    clock.advance(30.0)
    cache.get_or_refresh(SPOT, recompute=rc)  # stale-under-pull → degraded True
    meta = cache.entry_meta(SPOT)
    assert meta is not None and meta.duty_degraded is True
    # The pull ends; the next read is dirty + past the floor → fresh recompute…
    cache.set_pull_active_probe(lambda: False)
    cache.get_or_refresh(SPOT, recompute=rc)
    meta2 = cache.entry_meta(SPOT)
    assert meta2 is not None and meta2.duty_degraded is False
    # …and a subsequent CLEAN hit keeps it False (the clean-hit path clears too).
    cache.get_or_refresh(SPOT, recompute=rc)
    meta3 = cache.entry_meta(SPOT)
    assert meta3 is not None and meta3.duty_degraded is False


def test_stamp_freshness_with_entry_and_without() -> None:
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    # No entry yet: honest False / 0.0 (a degrade payload built fresh this request).
    empty: dict[str, object] = {"rows": []}
    stamped = cache.stamp_freshness(SPOT, empty)
    assert stamped is empty  # stamped in place + returned (route ergonomics)
    assert stamped["duty_degraded"] is False
    assert stamped["snapshot_age_s"] == 0.0
    # A fresh (NOT duty-degraded) entry: the age is deliberately 0.0 — an
    # always-ticking age would defeat the DEC-DASHBOARD-JS-PERF-001
    # exact-body-text skip-render pollers in the steady state; the honesty
    # surface is the PULL-time staleness (the sibling test below).
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)
    clock.advance(1.23456)
    stamped2 = cache.stamp_freshness(SPOT, {"rows": []})
    assert stamped2["snapshot_age_s"] == 0.0
    assert stamped2["duty_degraded"] is False


def test_stamp_freshness_reports_age_while_duty_degraded() -> None:
    # While a pull holds the entry stale, the stamp carries the REAL served age
    # (rounded to 3 dp) — visible, growing per poll, never silent.
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    cache.set_pull_active_probe(lambda: True)
    rc = _SlowRecompute(clock, duration=27.0)
    cache.get_or_refresh(SPOT, recompute=rc)
    cache.mark_dirty()
    clock.advance(30.12345)
    cache.get_or_refresh(SPOT, recompute=rc)  # stale-under-pull -> degraded
    stamped = cache.stamp_freshness(SPOT, {"rows": []})
    assert stamped["duty_degraded"] is True
    assert stamped["snapshot_age_s"] == 30.123


def test_entry_meta_age_clamped_nonnegative() -> None:
    # A backwards clock (injected skew) never yields a negative age.
    clock = _FakeClock()
    cache = SummaryCache(clock=clock)
    rc = _CountingRecompute()
    cache.get_or_refresh(SPOT, recompute=rc)
    clock.advance(-5.0)
    meta = cache.entry_meta(SPOT)
    assert meta is not None
    assert meta.age_s == 0.0


# ────────────────────────────────────────────────────────────────────────
# COLD single-flight (DEC-SCAN-SINGLE-FLIGHT-001)
# ────────────────────────────────────────────────────────────────────────


def test_cold_concurrent_cross_thread_single_flight() -> None:
    """N cold concurrent readers of one kind → exactly ONE scan, all served.

    DEC-SCAN-SINGLE-FLIGHT-001: the 2026-07-05 restart log measured SIX
    concurrent 58-68 min duplicate lake-uniqueness census scans — one per cold
    poller — because the pre-fix cold path deliberately fell through to a
    "transient double-scan". Now the waiters block on the in-flight event and
    serve the stored payload: the scan spy must count EXACTLY one call.
    """
    cache = SummaryCache(ttl_floor_s=2.0)
    scan_started = threading.Event()
    release_scan = threading.Event()
    calls = {"n": 0}

    def slow_scan() -> list[dict[str, object]]:
        calls["n"] += 1
        scan_started.set()
        assert release_scan.wait(timeout=10)
        return [{"census": "payload"}]

    results: list[list[dict[str, object]]] = []

    def leader() -> None:
        results.append(cache.get_or_refresh(LAKE_UNIQUENESS, recompute=slow_scan))

    def waiter() -> None:
        results.append(cache.get_or_refresh(LAKE_UNIQUENESS, recompute=slow_scan))

    lead = threading.Thread(target=leader)
    lead.start()
    assert scan_started.wait(timeout=10)  # the leader is REGISTERED + scanning
    waiters = [threading.Thread(target=waiter) for _ in range(5)]
    for t in waiters:
        t.start()
    release_scan.set()
    lead.join(timeout=10)
    for t in waiters:
        t.join(timeout=10)
    assert not lead.is_alive() and not any(t.is_alive() for t in waiters)
    assert calls["n"] == 1  # ONE scan total — never six
    assert results == [[{"census": "payload"}]] * 6


def test_cold_waiter_takes_over_after_scan_exception() -> None:
    """A raised in-flight cold scan wakes the waiters; exactly one takes over."""
    cache = SummaryCache(ttl_floor_s=0.0)
    scan_started = threading.Event()
    release_scan = threading.Event()

    class _Boom(RuntimeError):
        pass

    def failing_scan() -> list[dict[str, object]]:
        scan_started.set()
        assert release_scan.wait(timeout=10)
        raise _Boom

    good = _CountingRecompute()
    errors: list[BaseException] = []
    results: list[list[dict[str, object]]] = []

    def leader() -> None:
        try:
            cache.get_or_refresh(SPOT, recompute=failing_scan)
        except _Boom as exc:
            errors.append(exc)

    def waiter() -> None:
        results.append(cache.get_or_refresh(SPOT, recompute=good))

    lead = threading.Thread(target=leader)
    lead.start()
    assert scan_started.wait(timeout=10)
    wait_thread = threading.Thread(target=waiter)
    wait_thread.start()
    release_scan.set()
    lead.join(timeout=10)
    wait_thread.join(timeout=10)
    assert not lead.is_alive() and not wait_thread.is_alive()
    assert len(errors) == 1  # the leader's exception propagated untouched
    assert results == [[{"call": 1}]]  # the waiter took over with ONE fresh scan
    assert good.calls == 1
