"""Tests for hung / stalled dashboard-pull detection (DEC-PULL-STALL-DETECTION-001).

Two MAJOR stall-detection gaps the failure hunt found, both proven closed here:

* **GAP 1** — :class:`~brutex.dashboard.pull_stall.PullWatchdog` +
  :class:`~brutex.dashboard.pull_jobs.BackgroundPullJob`: a HUNG worker (which never
  reaches the worker's ``finally`` to clear ``_running``) is now detected by a background
  heartbeat timer that fires a CRITICAL pull-halt alert AND clears ``_running`` so future
  pulls are not silently refused. A slow-but-PROGRESSING pull beats the heartbeat and is
  never falsely declared hung; a watchdog failure can never break the pull.
* **GAP 2** — :func:`~brutex.dashboard.pull_controller.make_window_observer`: the
  :class:`~brutex.observability.pull_health.PullHealthMonitor` ``observe`` is now fed
  PER WINDOW (not only at pull boundaries), so the dwell-gated ``pull.stalled`` rule
  accumulates and a mid-pull stall actually fires.

100% line+branch on the touched files (``pull_stall.py`` + the new ``pull_jobs.py`` paths
+ the new ``pull_controller.make_window_observer``).
"""

# Whitebox tests of module-private state (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook, WatchdogFactory
from brutex.dashboard.pull_progress import STATE_DONE, STATE_ERROR, PullProgress
from brutex.dashboard.pull_stall import (
    DEFAULT_STALL_TIMEOUT_SECONDS,
    PULL_STALL_ENV,
    PULL_WORKER_NAME,
    PullWatchdog,
    resolve_stall_timeout_seconds,
)
from brutex.observability.watchdog import WorkerEvent, WorkerState


class _FakeClock:
    """Manually-advanced monotonic clock (no sleeping)."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _capturing_spawn() -> tuple[list[Callable[[], None]], Callable[[Callable[[], None]], None]]:
    """A spawner that CAPTURES the timer loop body instead of running it (deterministic)."""
    captured: list[Callable[[], None]] = []
    return captured, captured.append


def _sync_spawn(fn: Callable[[], None]) -> None:
    """Run the worker inline so the test is deterministic."""
    fn()


# ════════════════════════════════════════════════════════════════════════
# resolve_stall_timeout_seconds — env override + fail-safe
# ════════════════════════════════════════════════════════════════════════


class TestResolveStallTimeout:
    def test_default_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PULL_STALL_ENV, raising=False)
        assert resolve_stall_timeout_seconds() == DEFAULT_STALL_TIMEOUT_SECONDS

    def test_blank_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_STALL_ENV, "   ")
        assert resolve_stall_timeout_seconds() == DEFAULT_STALL_TIMEOUT_SECONDS

    def test_valid_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_STALL_ENV, "120")
        assert resolve_stall_timeout_seconds() == 120.0

    def test_non_numeric_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_STALL_ENV, "not-a-number")
        assert resolve_stall_timeout_seconds() == DEFAULT_STALL_TIMEOUT_SECONDS

    def test_non_positive_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_STALL_ENV, "0")
        assert resolve_stall_timeout_seconds() == DEFAULT_STALL_TIMEOUT_SECONDS
        monkeypatch.setenv(PULL_STALL_ENV, "-5")
        assert resolve_stall_timeout_seconds() == DEFAULT_STALL_TIMEOUT_SECONDS


# ════════════════════════════════════════════════════════════════════════
# PullWatchdog — heartbeat + hung detection (GAP 1 primitive)
# ════════════════════════════════════════════════════════════════════════


class TestPullWatchdog:
    def test_timeout_defaults_to_resolver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_STALL_ENV, "90")
        wd = PullWatchdog(on_hung=lambda _e: None, spawn=lambda _fn: None)
        assert wd.timeout_seconds == 90.0

    def test_explicit_timeout_overrides_resolver(self) -> None:
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=42.0, spawn=lambda _fn: None)
        assert wd.timeout_seconds == 42.0

    def test_start_registers_worker_and_spawns_timer(self) -> None:
        captured, spawn = _capturing_spawn()
        clock = _FakeClock()
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=30.0, spawn=spawn, clock=clock)
        wd.start()
        assert len(captured) == 1  # the timer loop body was spawned
        assert wd.state() == WorkerState.HEALTHY  # registered HEALTHY up front

    def test_hung_fires_on_dwell_timeout(self) -> None:
        _, spawn = _capturing_spawn()
        clock = _FakeClock()
        events: list[WorkerEvent] = []
        wd = PullWatchdog(on_hung=events.append, timeout_seconds=30.0, spawn=spawn, clock=clock)
        wd.start()
        # No beat; advance past the timeout → check_once declares HUNG.
        clock.advance(31.0)
        fired = wd.check_once()
        assert len(fired) == 1
        assert fired[0].new_state == WorkerState.HUNG
        assert fired[0].worker_name == PULL_WORKER_NAME
        assert events and events[0].new_state == WorkerState.HUNG
        assert wd.state() == WorkerState.HUNG

    def test_beat_keeps_worker_healthy(self) -> None:
        _, spawn = _capturing_spawn()
        clock = _FakeClock()
        events: list[WorkerEvent] = []
        wd = PullWatchdog(on_hung=events.append, timeout_seconds=30.0, spawn=spawn, clock=clock)
        wd.start()
        # Beat just before each timeout — a slow-but-progressing pull never hangs.
        for _ in range(3):
            clock.advance(20.0)
            wd.beat()
            assert wd.check_once() == ()  # never an edge
        assert wd.state() == WorkerState.HEALTHY
        assert events == []  # no hung alert for a progressing pull

    def test_recovery_edge_is_logged_only(self) -> None:
        _, spawn = _capturing_spawn()
        clock = _FakeClock()
        events: list[WorkerEvent] = []
        wd = PullWatchdog(on_hung=events.append, timeout_seconds=30.0, spawn=spawn, clock=clock)
        wd.start()
        clock.advance(31.0)
        wd.check_once()  # HUNG
        assert wd.state() == WorkerState.HUNG
        # A late beat + check recovers the worker (HUNG→HEALTHY edge) — informational,
        # the on_hung callback is NOT re-fired on recovery.
        wd.beat()
        recovered = wd.check_once()
        assert len(recovered) == 1
        assert recovered[0].new_state == WorkerState.HEALTHY
        assert len(events) == 1  # still only the original hung event (no recovery alert)

    def test_drive_loop_polls_until_stopped(self) -> None:
        """The real timer-loop body polls check_once each tick until stop() fires."""
        clock = _FakeClock()
        polls: list[int] = []
        wd = PullWatchdog(
            on_hung=lambda _e: None,
            timeout_seconds=30.0,
            spawn=lambda _fn: None,  # we drive _drive ourselves below
            clock=clock,
        )

        # Stub check_once to count polls and stop the loop after 2 ticks.
        def _counting_check() -> tuple[WorkerEvent, ...]:
            polls.append(1)
            if len(polls) >= 2:
                wd.stop()
            return ()

        wd.check_once = _counting_check  # type: ignore[method-assign]
        wd.start(poll_seconds=0.001)
        wd._drive(0.001)
        assert len(polls) == 2  # polled twice, then stop() ended the loop

    def test_stop_before_any_poll_exits_immediately(self) -> None:
        """stop() before the loop starts → the wait returns True on the first iteration."""
        clock = _FakeClock()
        polls: list[int] = []

        wd = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=30.0, spawn=lambda _fn: None, clock=clock
        )

        def _counting_check() -> tuple[WorkerEvent, ...]:
            polls.append(1)
            return ()

        wd.check_once = _counting_check  # type: ignore[method-assign]
        wd.start(poll_seconds=10.0)
        wd.stop()
        wd._drive(10.0)
        assert polls == []  # stopped before the first poll

    def test_default_poll_seconds_is_timeout_over_three(self) -> None:
        """start() with no poll_seconds uses timeout/3 as the cadence."""
        clock = _FakeClock()
        captured: list[float] = []

        def _capture_interval(interval: float) -> None:
            captured.append(interval)

        # A spawner that RUNS the loop body so the default timeout/3 interval is captured.
        def _running_spawn(fn: Callable[[], None]) -> None:
            fn()

        wd = PullWatchdog(
            on_hung=lambda _e: None,
            timeout_seconds=30.0,
            spawn=_running_spawn,
            clock=clock,
        )
        wd._drive = _capture_interval  # type: ignore[method-assign]
        wd.start()
        assert captured == [10.0]  # 30 / 3


# ════════════════════════════════════════════════════════════════════════
# BackgroundPullJob — watchdog wiring (GAP 1 integration)
# ════════════════════════════════════════════════════════════════════════


class _SpyWatchdog:
    """A capturing PullWatchdog double recording its lifecycle + exposing the on_hung hook."""

    def __init__(self, on_hung: Callable[[WorkerEvent], None]) -> None:
        self.on_hung = on_hung
        self.started = False
        self.stopped = False
        self.beats = 0

    def start(self, *, poll_seconds: float | None = None) -> None:
        self.started = True

    def beat(self) -> None:
        self.beats += 1

    def stop(self) -> None:
        self.stopped = True

    def trigger_hung(self, *, last_beat_seconds_ago: float = 700.0, timeout: float = 600.0) -> None:
        """Simulate the timer thread declaring the worker HUNG."""
        self.on_hung(
            WorkerEvent(
                worker_name=PULL_WORKER_NAME,
                new_state=WorkerState.HUNG,
                last_beat_seconds_ago=last_beat_seconds_ago,
                timeout_seconds=timeout,
            )
        )


def _spy_factory() -> tuple[list[_SpyWatchdog], WatchdogFactory]:
    """A watchdog factory that records every built spy for inspection."""
    built: list[_SpyWatchdog] = []

    def factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
        spy = _SpyWatchdog(on_hung)
        built.append(spy)
        return spy  # type: ignore[return-value]

    return built, factory


def _job(factory: WatchdogFactory) -> BackgroundPullJob:
    return BackgroundPullJob(progress=PullProgress(), spawn=_sync_spawn, watchdog_factory=factory)


class TestBackgroundPullJobWatchdog:
    def test_watchdog_started_and_beaten_then_stopped(self) -> None:
        built, factory = _spy_factory()
        job = _job(factory)

        def work(on_progress: ProgressHook) -> None:
            on_progress("NIFTY 1minute", 100, False, False)
            on_progress("BANKNIFTY 1minute", 50, False, False)

        assert job.start(work, total=2) is True
        assert len(built) == 1
        spy = built[0]
        assert spy.started is True
        assert spy.beats == 2  # one beat per real-progress window
        assert spy.stopped is True  # stopped in the worker's finally
        assert job.snapshot().state == STATE_DONE
        assert job.running is False

    def test_ledger_sync_beats_the_watchdog(self) -> None:
        built, factory = _spy_factory()
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=_sync_spawn, watchdog_factory=factory
        )

        def work(_on_progress: ProgressHook) -> None:
            job.sync_from_ledger(completed=5, total=10, bars=500)

        job.start(work, total=10, ledger_synced=True)
        assert built[0].beats == 1  # the per-window ledger sync beat the watchdog

    def test_hung_edge_alerts_and_clears_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A HUNG worker → CRITICAL pull-halt alert + progress error + _running cleared."""
        halts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)

        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        # A capturing WORKER spawner so the worker never finishes (simulates a hang).
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        assert job.start(lambda _h: None, total=1) is True
        assert job.running is True  # worker captured, still "running"

        # The timer thread declares the worker HUNG.
        built[0].trigger_hung()

        assert len(halts) == 1  # one CRITICAL pull-halt alert fired
        assert isinstance(halts[0], TimeoutError)
        snap = job.snapshot()
        assert snap.state == STATE_ERROR
        assert "stalled" in snap.detail
        assert job.running is False  # _running CLEARED so future pulls are not refused

    def test_future_pull_accepted_after_hung(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After a hung→stalled transition, a NEW pull starts (single-flight released)."""
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)
        built[0].trigger_hung()
        assert job.running is False
        # A second pull is now ACCEPTED (would have been silently refused before the fix).
        accepted = job.start(lambda _h: None, total=1)
        assert accepted is True
        assert len(built) == 2  # a fresh watchdog for the new run

    def test_stale_worker_finally_does_not_clobber_new_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hung worker that LATER unwedges must not reset a freshly-started new run."""
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)
        built[0].trigger_hung()  # declared hung; _declared_hung latched, _running cleared
        # The stale worker finally unwedges and runs its captured body → its finally fires.
        captured[0]()
        # Because _declared_hung latched, the stale finally did NOT touch _running.
        assert job.running is False
        # And a new pull can still start cleanly.
        assert job.start(lambda _h: None, total=1) is True

    def test_beat_is_a_noop_before_a_run_starts(self) -> None:
        """``_beat`` (via sync_from_ledger / _on_progress) is a no-op when no run is active.

        The ``_watchdog is None`` guard: before the first :meth:`start` the watchdog is
        unbuilt, so a stray public ``sync_from_ledger`` / progress tick must not crash.
        """
        job = BackgroundPullJob(progress=PullProgress(), spawn=_sync_spawn)
        assert job._watchdog is None
        job.sync_from_ledger(completed=1, total=2, bars=10)  # hits _beat() with None watchdog
        job._on_progress("X 1minute", 5, False, False)  # also a no-op beat
        # No exception → the None-guard held; nothing started.
        assert job.running is False

    def test_default_watchdog_factory_builds_real_watchdog(self) -> None:
        """The default factory (no injection) builds a real daemon-timer PullWatchdog."""
        done = threading.Event()

        def spawn(fn: Callable[[], None]) -> None:
            fn()
            done.set()

        # No watchdog_factory → the production _default_watchdog_factory builds the real
        # PullWatchdog (daemon timer). The worker runs+returns synchronously, so the
        # watchdog is started + stopped cleanly with no hang.
        job = BackgroundPullJob(progress=PullProgress(), spawn=spawn)
        job.start(lambda h: h("X 1minute", 1, False, False), total=1)
        assert done.is_set()
        assert job.snapshot().state == STATE_DONE
        assert job.running is False


# ════════════════════════════════════════════════════════════════════════
# make_window_observer — per-window pull-health observe (GAP 2)
# ════════════════════════════════════════════════════════════════════════


class _FakeMeter:
    def __init__(self, snap: dict[str, object]) -> None:
        self._snap = snap

    def snapshot(self) -> dict[str, object]:
        return self._snap


class _FakeClient:
    def __init__(self, meter_snap: dict[str, object]) -> None:
        self.meter = _FakeMeter(meter_snap)


class _FakeAdapter:
    def __init__(self, meter_snap: dict[str, object]) -> None:
        self.client = _FakeClient(meter_snap)


class TestMakeWindowObserver:
    """The per-window observe wrapper that lets the pull.stalled dwell accumulate."""

    def test_no_observer_no_monitor_returns_inner_unwrapped(self) -> None:
        from brutex.dashboard.pull_controller import make_window_observer

        def inner() -> None:
            pass

        wrapped = make_window_observer(inner, None, None, _FakeAdapter({}), None)  # type: ignore[arg-type]
        assert wrapped is inner  # zero overhead when there is nothing to observe

    def test_inner_runs_then_observes_each_window(self) -> None:
        from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_CEILING
        from brutex.adapters.rate_common import RateController
        from brutex.dashboard.pull_controller import make_window_observer
        from brutex.observability.metrics import MetricRegistry
        from brutex.observability.pull_health import PullHealthMonitor

        order: list[str] = []
        registry = MetricRegistry()
        monitor = PullHealthMonitor(registry=registry, on_degraded=lambda *_: None, pool_size=4)
        controller = RateController(ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING, ceiling_per_min=500)
        adapter = _FakeAdapter({"effective_rps": 4.0, "latency_ms_p95": 100.0, "in_flight": 4})

        def inner() -> None:
            order.append("inner")

        wrapped = make_window_observer(inner, None, controller, adapter, monitor)  # type: ignore[arg-type]
        wrapped()
        # The inner live-sync runs FIRST, then the observe mirrors a gauge (proving observe ran).
        assert order == ["inner"]
        assert registry.gauge_value("brutex_pull_latency_ms_p95") == 100.0

    def test_per_window_observe_accumulates_stalled_dwell(self) -> None:
        """GAP 2: the dwell-gated pull.stalled rule fires once enough WINDOWS observe a stall."""
        from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_CEILING
        from brutex.adapters.rate_common import RateController
        from brutex.dashboard.pull_controller import make_window_observer
        from brutex.observability.metrics import MetricRegistry
        from brutex.observability.pull_health import PullHealthMonitor
        from brutex.observability.sla import SLAState

        degraded: list[str] = []
        registry = MetricRegistry()
        monitor = PullHealthMonitor(
            registry=registry,
            on_degraded=lambda rule, *_: degraded.append(rule),
            pool_size=4,
            stalled_dwell=3,
        )
        controller = RateController(ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING, ceiling_per_min=500)
        # A near-zero-throughput snapshot — the stall signature.
        stalled_adapter = _FakeAdapter(
            {"effective_rps": 0.1, "latency_ms_p95": 100.0, "in_flight": 4}
        )
        wrapped = make_window_observer(lambda: None, None, controller, stalled_adapter, monitor)  # type: ignore[arg-type]

        # First two windows: dwell not yet satisfied → pull.stalled still OK.
        wrapped()
        wrapped()
        assert monitor.state_of("pull.stalled") == SLAState.OK
        # Third window: dwell satisfied → CRITICAL pull.stalled BREACH fires the alert.
        wrapped()
        assert monitor.state_of("pull.stalled") == SLAState.BREACH
        assert "pull.stalled" in degraded
