"""Stale-while-revalidate arm of ``MoversSnapshotCache`` (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001, G1).

The opt-in ``stale_while_revalidate`` ctor flag: PAST the #1250 adaptive floor
a flagged cache serves the last-known entry IMMEDIATELY while a pull is active
(or a revalidator is already in flight) and refreshes in AT MOST ONE tokenless
background revalidator per key. This suite pins:

* the FAILS-BEFORE blocking-waiter defect (a stale-entry reader with a foreign
  in-flight compute parked on ``wait_event.wait()``) — non-blocking when
  flagged, STILL blocking on the default (the byte-identity behaviour pin);
* single-revalidator under an 8-thread fan-in; the floor arm stays
  spawn-free WITHIN the floor; ``force_fresh`` bypasses floor AND SWR;
* idle + no in-flight ⇒ the untouched inline recompute; "or revalidating"
  serves stale at the pull-end boundary;
* the NEWEST-WINS store guard (the judge-found warm-reseed regression window);
* spawn-failure (#1522 fold) + revalidate-failure (throttled WARN) honesty;
* the ``stale_extended`` escalation (once per episode + the 300 s re-WARN);
* the revalidator-stored entry's ``compute_duration`` is the REAL compute
  span feeding the #920 adaptive floor (V2 honesty — the round-5 review's
  zeroing-mutant pin: a fabricated 0 would under-throttle the background
  walk cadence).

Pure (no lake / FastAPI) — plain closures, injectable clock + swr_runner,
zero real sleeps except the one bounded real-thread default-runner smoke.
"""

# Whitebox pins read module-private constants/state by design (DEC-P0-017).
# pyright: reportPrivateUsage=none
from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from brutex.dashboard.movers_cache import (
    _SWR_FAILURE_RELOG_S,
    _SWR_STALE_EXTENDED_REWARN_S,
    MoversSnapshotCache,
)
from structlog.testing import capture_logs


def _events(logs: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == event]


def _flagged(
    *,
    clock: Any,
    runner: Any = None,
    pull_active: Any = None,
    **kwargs: Any,
) -> MoversSnapshotCache:
    """A flagged cache with the floor armed (the production adopter shape)."""
    return MoversSnapshotCache(
        pull_active_probe=pull_active if pull_active is not None else (lambda: True),
        adaptive_floor=True,
        stale_while_revalidate=True,
        swr_runner=runner,
        clock=clock,
        **kwargs,
    )


class TestServesStaleNonBlocking:
    """The blocked-waiter defect (movers_cache single-flight wait) is closed."""

    def test_swr_serves_stale_past_floor_nonblocking(self) -> None:
        """FAILS-BEFORE: a stale-entry reader with a FOREIGN in-flight compute
        blocked on ``wait_event.wait()`` for the whole walk (pinning a lake
        token). Flagged: it returns the stale value immediately while the
        foreign compute stays parked."""
        clock = {"t": 0.0}
        spawned: list[Any] = []
        cache = _flagged(clock=lambda: clock["t"], runner=spawned.append)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0  # past the 2 s base floor
        # Park a FOREIGN compute in-flight on the same key (a slow walk).
        parked = threading.Event()
        release = threading.Event()

        def _foreign() -> None:
            def _slow() -> str:
                parked.set()
                assert release.wait(timeout=10)
                return "v-foreign"

            # force_fresh routes THIS thread into the inline single-flight (the
            # warm-reseed path), registering the key in-flight while it parks.
            cache.get_or_compute("k", generation=2, compute=_slow, force_fresh=True)

        foreign = threading.Thread(target=_foreign, daemon=True)
        foreign.start()
        assert parked.wait(timeout=10)
        # The flagged reader must NOT join the single-flight wait: pre-fix this
        # call blocked until `release` — the done-event bounds the pin.
        done = threading.Event()
        result: list[str] = []

        def _reader() -> None:
            result.append(cache.get_or_compute("k", generation=2, compute=lambda: "v-new"))
            done.set()

        threading.Thread(target=_reader, daemon=True).start()
        assert done.wait(timeout=2.0), "flagged reader blocked behind the in-flight compute"
        assert result == ["v1"]  # the last-known serve
        release.set()
        foreign.join(timeout=10)
        assert not foreign.is_alive()

    def test_swr_default_flag_blocks(self) -> None:
        """The byte-identity behaviour pin: the DEFAULT cache still waits on
        the single-flight for a stale entry with a foreign in-flight compute
        (every existing consumer's semantics, including the blocking wait)."""
        clock = {"t": 0.0}
        cache = MoversSnapshotCache(
            pull_active_probe=lambda: True, adaptive_floor=True, clock=lambda: clock["t"]
        )
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0  # past the floor — the default recomputes/waits
        parked = threading.Event()
        release = threading.Event()

        def _foreign() -> None:
            def _slow() -> str:
                parked.set()
                assert release.wait(timeout=10)
                return "v-foreign"

            cache.get_or_compute("k", generation=2, compute=_slow)

        foreign = threading.Thread(target=_foreign, daemon=True)
        foreign.start()
        assert parked.wait(timeout=10)
        done = threading.Event()
        result: list[str] = []

        def _reader() -> None:
            result.append(cache.get_or_compute("k", generation=2, compute=lambda: "v-new"))
            done.set()

        threading.Thread(target=_reader, daemon=True).start()
        # Still parked after a generous beat — the default DOES wait.
        assert not done.wait(timeout=0.3)
        release.set()
        assert done.wait(timeout=10)
        assert result == ["v-foreign"]  # woken by the foreign store — a clean hit

    def test_swr_miss_still_blocks_cold(self) -> None:
        """NO entry (cold first-ever open) keeps the blocking single-flight —
        the disclosed residual: there is nothing last-known to serve."""
        cache = _flagged(clock=lambda: 0.0, runner=lambda job: job())
        calls = {"n": 0}

        def _compute() -> str:
            calls["n"] += 1
            return "cold"

        assert cache.get_or_compute("k", generation=1, compute=_compute) == "cold"
        assert calls["n"] == 1  # computed inline — no revalidator involved


class TestArmConditions:
    """The semantics matrix rows: floor / force_fresh / idle / boundary."""

    def test_swr_within_floor_no_spawn(self) -> None:
        """WITHIN the floor the floor arm serves — no revalidator spawned
        (the floor IS the background cadence throttle)."""
        clock = {"t": 0.0}
        spawned: list[Any] = []
        cache = _flagged(clock=lambda: clock["t"], runner=spawned.append)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 1.0  # inside the 2 s base floor
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v1"
        assert spawned == []  # the floor arm — never the SWR arm
        (served,) = _events(logs, "movers_cache.stale_served")
        assert "revalidate" not in served

    def test_swr_past_floor_serves_and_spawns(self) -> None:
        """PAST the floor: serve stale + spawn exactly one revalidator, with
        the ``revalidate="spawned"`` log field; the (inline) revalidator's
        store makes the next read a fresh hit."""
        clock = {"t": 0.0}
        cache = _flagged(clock=lambda: clock["t"], runner=lambda job: job())
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v1"
        (served,) = _events(logs, "movers_cache.stale_served")
        assert served["revalidate"] == "spawned"
        assert _events(logs, "movers_cache.swr_revalidate_stored")
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v3") == "v2"

    def test_swr_force_fresh_bypasses(self) -> None:
        """``force_fresh`` (the warm reseed) bypasses floor AND SWR — the
        compute runs inline and stores fresh."""
        clock = {"t": 0.0}
        spawned: list[Any] = []
        cache = _flagged(clock=lambda: clock["t"], runner=spawned.append)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        got = cache.get_or_compute("k", generation=2, compute=lambda: "v2", force_fresh=True)
        assert got == "v2"
        assert spawned == []

    def test_swr_idle_no_inflight_recomputes_inline(self) -> None:
        """Pull idle + no revalidator in flight ⇒ the untouched inline
        recompute (an idle stamp is stable — the cheap manifest-first read)."""
        clock = {"t": 0.0}
        spawned: list[Any] = []
        cache = _flagged(clock=lambda: clock["t"], runner=spawned.append, pull_active=lambda: False)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v2"
        assert spawned == []

    def test_swr_serves_while_revalidating_pull_idle(self) -> None:
        """The "or revalidating" graft: the pull just ended but a revalidator
        is still in flight — a reader serves stale instead of blocking
        behind it (logged ``revalidate="in_flight"``)."""
        clock = {"t": 0.0}
        pull = {"active": True}
        captured: list[Any] = []
        cache = _flagged(
            clock=lambda: clock["t"], runner=captured.append, pull_active=lambda: pull["active"]
        )
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        # Spawn (captured, NOT run) while the pull is active…
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v1"
        assert len(captured) == 1
        # …then the pull ENDS with the revalidator still in flight.
        pull["active"] = False
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=lambda: "v3") == "v1"
        (served,) = _events(logs, "movers_cache.stale_served")
        assert served["revalidate"] == "in_flight"
        captured[0]()  # the revalidator completes → the fresh entry lands
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v4") == "v2"

    def test_swr_single_revalidator_under_fanin(self) -> None:
        """8 concurrent past-floor readers ⇒ exactly ONE runner invocation;
        every reader serves the stale value immediately."""
        clock = {"t": 0.0}
        captured: list[Any] = []
        lock = threading.Lock()

        def _capturing(job: Any) -> None:
            with lock:
                captured.append(job)

        cache = _flagged(clock=lambda: clock["t"], runner=_capturing)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        results: list[str] = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    cache.get_or_compute("k", generation=2, compute=lambda: "v2")
                ),
                daemon=True,
            )
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert results == ["v1"] * 8
        assert len(captured) == 1  # AT MOST ONE revalidator per key


class TestStoreGuardNewestWins:
    """The judge-found defect: a slow revalidator must never regress a
    fresher entry (the pull-end warm-reseed window)."""

    def test_swr_store_guard_newest_generation_wins(self) -> None:
        """A revalidator finishing AFTER a warm reseed stored a DIFFERENT
        (newer) generation must SKIP its store — dropped guard ⇒ this fails
        with the stale value clobbering the warm's fresh one."""
        clock = {"t": 0.0}
        captured: list[Any] = []
        cache = _flagged(clock=lambda: clock["t"], runner=captured.append)
        cache.get_or_compute("k", generation=1, compute=lambda: "old")
        clock["t"] = 5.0
        # Spawn the revalidator at generation 2 (captured — still "running").
        assert cache.get_or_compute("k", generation=2, compute=lambda: "reval-late") == "old"
        # The pull-end warm reseed lands at generation 3 while it runs.
        cache.get_or_compute("k", generation=3, compute=lambda: "warm-fresh", force_fresh=True)
        with capture_logs() as logs:
            captured[0]()  # the slow revalidator NOW finishes
        assert _events(logs, "movers_cache.swr_revalidate_superseded")
        assert not _events(logs, "movers_cache.swr_revalidate_stored")
        # The warm's entry survives — a clean hit at generation 3.
        assert cache.get_or_compute("k", generation=3, compute=lambda: "boom") == "warm-fresh"

    def test_swr_store_lands_when_slot_unchanged(self) -> None:
        """The complement: the slot still holds the spawn-observed generation
        ⇒ the store lands (the normal case)."""
        clock = {"t": 0.0}
        captured: list[Any] = []
        cache = _flagged(clock=lambda: clock["t"], runner=captured.append)
        cache.get_or_compute("k", generation=1, compute=lambda: "old")
        clock["t"] = 5.0
        cache.get_or_compute("k", generation=2, compute=lambda: "fresh")
        captured[0]()
        assert cache.get_or_compute("k", generation=2, compute=lambda: "boom") == "fresh"

    def test_swr_store_lands_when_entry_evicted(self) -> None:
        """An entry evicted (LRU churn) during the flight ⇒ the store lands —
        the revalidated value is better than nothing."""
        clock = {"t": 0.0}
        captured: list[Any] = []
        cache = _flagged(clock=lambda: clock["t"], runner=captured.append, capacity=1)
        cache.get_or_compute("k", generation=1, compute=lambda: "old")
        clock["t"] = 5.0
        cache.get_or_compute("k", generation=2, compute=lambda: "fresh")
        cache.get_or_compute("other", generation=2, compute=lambda: "evictor")  # evicts k
        captured[0]()
        assert cache.get_or_compute("k", generation=2, compute=lambda: "boom") == "fresh"

    def test_swr_equal_generation_store_skipped(self) -> None:
        """A store already landed AT the spawn generation ⇒ skip (the existing
        entry is at least as fresh — idempotent, never a downgrade)."""
        clock = {"t": 0.0}
        captured: list[Any] = []
        cache = _flagged(clock=lambda: clock["t"], runner=captured.append)
        cache.get_or_compute("k", generation=1, compute=lambda: "old")
        clock["t"] = 5.0
        cache.get_or_compute("k", generation=2, compute=lambda: "reval")
        # An inline force_fresh store lands AT generation 2 during the flight.
        cache.get_or_compute("k", generation=2, compute=lambda: "landed", force_fresh=True)
        with capture_logs() as logs:
            captured[0]()
        assert _events(logs, "movers_cache.swr_revalidate_superseded")
        assert cache.get_or_compute("k", generation=2, compute=lambda: "boom") == "landed"


class TestFailureHonesty:
    """Spawn/compute failures are loud, bounded, and never break a read."""

    def test_swr_spawn_failure_serves_stale_warns(self) -> None:
        """A raising runner (Thread.start under exhaustion): marker released,
        ONE ``swr_spawn_failed`` WARN per key episode, stale served — and the
        NEXT read retries the spawn (the #1522 fold)."""
        clock = {"t": 0.0}
        attempts = {"n": 0}

        def _failing(job: Any) -> None:
            attempts["n"] += 1
            raise RuntimeError("can't start new thread")

        cache = _flagged(clock=lambda: clock["t"], runner=_failing)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v1"
        (served,) = _events(logs, "movers_cache.stale_served")
        assert served["revalidate"] == "spawn_failed"
        assert len(_events(logs, "movers_cache.swr_spawn_failed")) == 1
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v1"
        assert attempts["n"] == 2  # the next read retried the spawn…
        assert _events(logs, "movers_cache.swr_spawn_failed") == []  # …silently (episode)

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
    def test_swr_spawn_interrupt_propagates_marker_released(
        self, exc_type: type[BaseException]
    ) -> None:
        """KeyboardInterrupt/SystemExit from the runner PROPAGATE — with the
        in-flight marker released so the key is never wedged."""
        clock = {"t": 0.0}

        def _interrupting(job: Any) -> None:
            raise exc_type()

        cache = _flagged(clock=lambda: clock["t"], runner=_interrupting)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        with pytest.raises(exc_type):
            cache.get_or_compute("k", generation=2, compute=lambda: "v2")
        assert "k" not in cache._revalidating  # marker released — no wedge

    def test_swr_revalidate_failure_logged_flag_cleared_throttled(self) -> None:
        """A raising revalidate compute: WARN with the running failure count,
        re-log throttled to once per 60 s per key, marker cleared so the next
        past-floor read respawns; the episode resets on a successful store."""
        clock = {"t": 0.0}
        cache = _flagged(clock=lambda: clock["t"], runner=lambda job: job())
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")

        def _boom() -> str:
            raise ValueError("walk failed")

        clock["t"] = 5.0
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=_boom) == "v1"
        (failed,) = _events(logs, "movers_cache.swr_revalidate_failed")
        assert failed["failures"] == 1
        assert "walk failed" in str(failed["error"])
        # A second failure INSIDE the 60 s throttle window: counted, not logged.
        clock["t"] = 6.0
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=_boom) == "v1"
        assert _events(logs, "movers_cache.swr_revalidate_failed") == []
        # Past the throttle window: logged again with the TRUE running count.
        clock["t"] = 5.0 + _SWR_FAILURE_RELOG_S + 1.0
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=_boom) == "v1"
        (failed3,) = _events(logs, "movers_cache.swr_revalidate_failed")
        assert failed3["failures"] == 3
        # A SUCCESSFUL revalidate resets the episode…
        cache.get_or_compute("k", generation=2, compute=lambda: "v2")
        assert cache.get_or_compute("k", generation=2, compute=lambda: "x") == "v2"
        # …so the next failing episode logs from count 1 again.
        clock["t"] += 10.0
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=3, compute=_boom) == "v2"
        (failed_new,) = _events(logs, "movers_cache.swr_revalidate_failed")
        assert failed_new["failures"] == 1


class TestStaleExtendedEscalation:
    """Serving past the floor cap is LOUD — never silent-forever."""

    def test_swr_stale_extended_warn_once_per_episode(self) -> None:
        """Age > the cap WARNs once; repeats inside the 300 s re-WARN window
        stay silent; past it the episode re-WARNs; a landed store ends the
        episode."""
        clock = {"t": 0.0}
        captured: list[Any] = []
        cache = _flagged(
            clock=lambda: clock["t"], runner=captured.append, adaptive_floor_cap_s=120.0
        )
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 121.0  # past the 120 s cap
        with capture_logs() as logs:
            cache.get_or_compute("k", generation=2, compute=lambda: "v2")
        (warned,) = _events(logs, "movers_cache.stale_extended")
        assert warned["cap_s"] == 120.0
        clock["t"] = 200.0  # inside the 300 s re-WARN window
        with capture_logs() as logs:
            cache.get_or_compute("k", generation=2, compute=lambda: "v2")
        assert _events(logs, "movers_cache.stale_extended") == []
        clock["t"] = 121.0 + _SWR_STALE_EXTENDED_REWARN_S + 1.0
        with capture_logs() as logs:
            cache.get_or_compute("k", generation=2, compute=lambda: "v2")
        assert len(_events(logs, "movers_cache.stale_extended")) == 1
        # A landed store ends the episode: run the (single) captured
        # revalidator → fresh entry → later staleness is a NEW episode.
        captured[0]()
        clock["t"] += 121.0
        with capture_logs() as logs:
            cache.get_or_compute("k", generation=3, compute=lambda: "v3")
        assert len(_events(logs, "movers_cache.stale_extended")) == 1  # new episode WARNs

    def test_swr_at_cap_boundary_no_escalation(self) -> None:
        """Exactly AT the cap does not escalate (strict ``>`` — the floor
        family's boundary law; the ``>``→``>=`` mutant dies here)."""
        clock = {"t": 0.0}
        cache = _flagged(
            clock=lambda: clock["t"], runner=lambda job: None, adaptive_floor_cap_s=120.0
        )
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 120.0
        with capture_logs() as logs:
            cache.get_or_compute("k", generation=2, compute=lambda: "v2")
        assert _events(logs, "movers_cache.stale_extended") == []


class TestRevalidatorFloorHonesty:
    """V2 (round-5 review): the revalidator-stored ``compute_duration`` is the
    REAL compute span, so the #920 adaptive floor throttles the background
    revalidate cadence honestly — a zeroed/unmeasured duration would
    under-throttle (up to cap/base_floor× extra background walks)."""

    def test_revalidator_compute_duration_feeds_adaptive_floor(self) -> None:
        """The revalidator's stored ``compute_duration`` is its REAL compute
        span, and the NEXT past-base read throttles to that MEASURED span:
        no respawn inside it (kills the ``_store_revalidated``
        ``compute_duration=0.0`` mutant — a zeroed floor respawns at the 2 s
        base), one respawn past it (kills the ``finished_at`` absolute-time
        mutant — a 30 s floor would still throttle at age 21); the direct
        value assert kills the ``started_at`` remainder-swap mutant."""
        clock = {"t": 0.0}
        captured: list[Any] = []
        cache = _flagged(clock=lambda: clock["t"], runner=captured.append)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")  # duration 0 @ t=0

        def _slow_v2() -> str:
            clock["t"] += 20.0  # the revalidator's real compute span
            return "v2"

        clock["t"] = 5.0  # past the 2 s base floor -> the SWR arm spawns
        assert cache.get_or_compute("k", generation=2, compute=_slow_v2) == "v1"
        assert len(captured) == 1
        clock["t"] = 10.0  # the revalidator STARTS here (started_at=10)
        captured[0]()  # compute -> t=30: duration = 20, computed_at = 30
        entry = cache._entries["k"]
        assert entry.compute_duration == pytest.approx(20.0)  # the real compute span
        assert entry.computed_at == pytest.approx(30.0)
        # Behavioral teeth: age 15 is past the 2 s base but INSIDE the honest
        # 20 s effective floor -> the floor arm serves, NO second revalidator.
        clock["t"] = 45.0
        assert cache.get_or_compute("k", generation=3, compute=lambda: "x") == "v2"
        assert len(captured) == 1  # a zeroed duration would have respawned here
        # Past the measured floor the SWR arm respawns — throttled TO the
        # measured span, never forever (the absolute-time mutant's kill).
        clock["t"] = 51.0  # age 21 >= the 20 s effective floor
        assert cache.get_or_compute("k", generation=3, compute=lambda: "x") == "v2"
        assert len(captured) == 2


class TestBoundedAuxState:
    """The three per-key SWR state maps stay LRU-bounded at ``capacity``."""

    def test_fail_state_map_bounded(self) -> None:
        clock = {"t": 0.0}
        cache = _flagged(clock=lambda: clock["t"], runner=lambda job: job(), capacity=1)

        def _boom() -> str:
            raise ValueError("x")

        cache.get_or_compute("a", generation=1, compute=lambda: "va")
        clock["t"] = 5.0
        cache.get_or_compute("a", generation=2, compute=_boom)
        cache.get_or_compute("b", generation=2, compute=lambda: "vb")  # evicts a (capacity 1)
        clock["t"] = 10.0
        cache.get_or_compute("b", generation=3, compute=_boom)
        assert len(cache._swr_fail_state) == 1  # bounded — the oldest key popped

    def test_spawn_warned_map_bounded(self) -> None:
        clock = {"t": 0.0}

        def _failing(job: Any) -> None:
            raise RuntimeError("no threads")

        cache = _flagged(clock=lambda: clock["t"], runner=_failing, capacity=1)
        cache.get_or_compute("a", generation=1, compute=lambda: "va")
        clock["t"] = 5.0
        cache.get_or_compute("a", generation=2, compute=lambda: "x")
        cache.get_or_compute("b", generation=2, compute=lambda: "vb", force_fresh=True)
        clock["t"] = 10.0
        cache.get_or_compute("b", generation=3, compute=lambda: "x")
        assert len(cache._swr_spawn_warned) == 1

    def test_stale_warned_map_bounded(self) -> None:
        clock = {"t": 0.0}
        cache = _flagged(
            clock=lambda: clock["t"],
            runner=lambda job: None,
            capacity=1,
            adaptive_floor_cap_s=10.0,
        )
        cache.get_or_compute("a", generation=1, compute=lambda: "va")
        clock["t"] = 11.0  # past the cap → escalation stamp for a
        cache.get_or_compute("a", generation=2, compute=lambda: "x")
        cache.get_or_compute("b", generation=2, compute=lambda: "vb", force_fresh=True)
        clock["t"] = 23.0  # past the cap for b too
        cache.get_or_compute("b", generation=3, compute=lambda: "x")
        assert len(cache._swr_stale_warned_at) == 1


class TestRevalidatorStoreEdges:
    """The remaining revalidator-store arms: evicted-serve + outage-latch."""

    def test_swr_serve_with_entry_evicted_midway(self) -> None:
        """The MRU-touch guard: the served key can be EVICTED between the arm
        decision and the MRU touch (a hostile runner churns the capacity-1
        cache before returning) — the serve still returns the held value."""
        clock = {"t": 0.0}
        cache_ref: list[MoversSnapshotCache] = []

        def _churning(job: Any) -> None:
            # Evict "k" while the SWR arm is mid-serve (capacity 1).
            cache_ref[0].get_or_compute("evictor", generation=99, compute=lambda: "e")

        cache = _flagged(clock=lambda: clock["t"], runner=_churning, capacity=1)
        cache_ref.append(cache)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v1"

    def test_swr_serve_clears_outage_latch_logs_recovered(self) -> None:
        """A key latched by a prior ``serve_last_known`` outage serve logs the
        one recovery INFO when the SWR arm serves it (the #1386 latch contract
        holds through the new arm)."""
        clock = {"t": 0.0}
        cache = _flagged(clock=lambda: clock["t"], runner=lambda job: None)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        value, served = cache.serve_last_known("k", compute=lambda: "seed")
        assert (value, served) == ("v1", True)  # the outage latch is now armed
        clock["t"] = 5.0
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=lambda: "v2") == "v1"
        assert _events(logs, "movers_cache.revision_probe_recovered")


class TestDefaultRunnerSmoke:
    """The production default: ONE real daemon thread per revalidate."""

    def test_default_runner_revalidates_on_a_real_daemon_thread(self) -> None:
        clock = {"t": 0.0}
        cache = _flagged(clock=lambda: clock["t"])  # runner=None → real thread
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        stored = threading.Event()
        names: list[str] = []

        def _fresh() -> str:
            names.append(threading.current_thread().name)
            stored.set()
            return "v2"

        assert cache.get_or_compute("k", generation=2, compute=_fresh) == "v1"
        assert stored.wait(timeout=10)
        assert names == ["bx-cache-revalidate"]
        # Bounded settle: the daemon's store lands after the compute returns.
        deadline = threading.Event()
        for _ in range(200):
            if cache.get_or_compute("k", generation=2, compute=lambda: "x") == "v2":
                break
            deadline.wait(0.01)
        assert cache.get_or_compute("k", generation=2, compute=lambda: "x") == "v2"


class TestRacedCleanStoreServedAsHit:
    """Round-3 review: a CLEAN entry landing between the fast-path miss and
    the stale serve is served AS the value — never a one-store-older snapshot,
    never a ``stale_served`` log for a stale serve that did not happen (the
    stale_served law). Both stale-serving arms re-check under the serve lock;
    the race is injected deterministically through the ``pull_active`` probe
    (called between the fast-path miss and either serve, outside the lock)."""

    @staticmethod
    def _racing_pull_active(
        cache_ref: list[MoversSnapshotCache],
        *,
        store_value: str,
        relatch: bool = False,
    ) -> Any:
        """One-shot probe: the FIRST call stores a clean gen-2 entry for "k"."""
        state = {"raced": False}

        def _probe() -> bool:
            if not state["raced"]:
                state["raced"] = True
                # force_fresh bypasses floor AND SWR — the inline store lands
                # the clean entry exactly inside the reader's race window.
                cache_ref[0].get_or_compute(
                    "k", generation=2, compute=lambda: store_value, force_fresh=True
                )
                if relatch:
                    # Re-arm the outage latch AFTER the inner store cleared it,
                    # so the raced-clean hit itself performs the recovery flip.
                    cache_ref[0].serve_last_known("k", compute=lambda: "unused")
            return True

        return _probe

    def test_floor_arm_raced_clean_served_fresh_no_stale_log(self) -> None:
        """FLOOR arm (age < floor): the raced-in clean value is served and the
        stale_served log is suppressed — without the re-read this served the
        stale snapshot AND logged a stale serve."""
        clock = {"t": 0.0}
        cache_ref: list[MoversSnapshotCache] = []
        spawned: list[Any] = []
        cache = _flagged(
            clock=lambda: clock["t"],
            runner=spawned.append,
            pull_active=self._racing_pull_active(cache_ref, store_value="fresh-raced"),
        )
        cache_ref.append(cache)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 1.0  # inside the 2 s base floor — the floor arm serves
        with capture_logs() as logs:
            got = cache.get_or_compute("k", generation=2, compute=lambda: "must-not-run")
        assert got == "fresh-raced"  # the clean raced-in value, not "v1"
        assert _events(logs, "movers_cache.stale_served") == []
        assert spawned == []  # the floor arm never spawns

    def test_swr_arm_raced_clean_served_fresh_no_spawn_no_stale_log(self) -> None:
        """SWR arm (past floor): the raced-in clean value is a plain hit — no
        revalidator spawned, no stale_served / stale_extended logged."""
        clock = {"t": 0.0}
        cache_ref: list[MoversSnapshotCache] = []
        spawned: list[Any] = []
        cache = _flagged(
            clock=lambda: clock["t"],
            runner=spawned.append,
            pull_active=self._racing_pull_active(cache_ref, store_value="fresh-raced"),
        )
        cache_ref.append(cache)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0  # past the 2 s base floor — the SWR arm is reached
        with capture_logs() as logs:
            got = cache.get_or_compute("k", generation=2, compute=lambda: "must-not-run")
        assert got == "fresh-raced"
        assert _events(logs, "movers_cache.stale_served") == []
        assert _events(logs, "movers_cache.stale_extended") == []
        assert spawned == []  # served as a hit BEFORE the spawn decision

    def test_swr_arm_raced_clean_clears_outage_latch_logs_recovered(self) -> None:
        """The raced-clean hit is a REAL resolution: it clears an armed outage
        latch and logs the one ``revision_probe_recovered`` INFO (the #1386
        latch contract holds through the re-check path)."""
        clock = {"t": 0.0}
        cache_ref: list[MoversSnapshotCache] = []
        cache = _flagged(
            clock=lambda: clock["t"],
            runner=lambda job: None,
            pull_active=self._racing_pull_active(
                cache_ref, store_value="fresh-raced", relatch=True
            ),
        )
        cache_ref.append(cache)
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")
        clock["t"] = 5.0
        with capture_logs() as logs:
            got = cache.get_or_compute("k", generation=2, compute=lambda: "must-not-run")
        assert got == "fresh-raced"
        assert _events(logs, "movers_cache.revision_probe_recovered")
        assert _events(logs, "movers_cache.stale_served") == []


class TestStaleExtendedOnFailingRevalidator:
    """Round-3 review (V3): the persistently-FAILING revalidator path is
    NEVER a silent forever-stale illusion — every past-cap serve escalates
    ``stale_extended`` on the episode cadence even while the revalidate
    compute raises on every spawn (the WEDGED/in-flight path was already
    pinned; this pins the failing-runner path explicitly)."""

    def test_stale_extended_fires_while_revalidator_keeps_failing(self) -> None:
        clock = {"t": 0.0}
        cache = _flagged(
            clock=lambda: clock["t"],
            runner=lambda job: job(),  # inline: the failure happens each read
            adaptive_floor_cap_s=120.0,
        )
        cache.get_or_compute("k", generation=1, compute=lambda: "v1")

        def _boom() -> str:
            raise ValueError("dead volume")

        clock["t"] = 121.0  # past the 120 s cap
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=_boom) == "v1"
        (warned,) = _events(logs, "movers_cache.stale_extended")
        assert warned["cap_s"] == 120.0
        (failed,) = _events(logs, "movers_cache.swr_revalidate_failed")
        assert failed["failures"] == 1
        (served,) = _events(logs, "movers_cache.stale_served")
        assert served["revalidate"] == "spawned"  # the serve stays disclosed
        # STILL failing past the 300 s re-WARN window: the escalation
        # re-fires (never once-and-silent while the failure persists) and the
        # failure WARN re-logs with the TRUE running count.
        clock["t"] = 121.0 + _SWR_STALE_EXTENDED_REWARN_S + 1.0
        with capture_logs() as logs:
            assert cache.get_or_compute("k", generation=2, compute=_boom) == "v1"
        assert len(_events(logs, "movers_cache.stale_extended")) == 1
        (failed2,) = _events(logs, "movers_cache.swr_revalidate_failed")
        assert failed2["failures"] == 2
