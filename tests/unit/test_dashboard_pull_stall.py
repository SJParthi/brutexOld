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
+ the new ``pull_controller.make_window_observer``). The JOB-level wedge-escalation
tests (``TestBackgroundPullJobWedge`` — the ``pull_jobs.py`` ``_build_revision_probe`` +
``_on_wedged`` wiring) live in ``test_pull_jobs_wedge.py`` so the auto-discovery
coverage gate's stem mapping for ``pull_jobs.py`` measures them; the watchdog-SIDE
wedge tests stay here.
"""

# Whitebox tests of module-private state (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook, WatchdogFactory
from brutex.dashboard.pull_progress import STATE_DONE, STATE_RUNNING, PullProgress
from brutex.dashboard.pull_stall import (
    DEFAULT_STALL_TIMEOUT_SECONDS,
    DEFAULT_WEDGE_MULTIPLIER,
    PULL_STALL_ENV,
    PULL_WEDGE_ENV,
    PULL_WORKER_NAME,
    PullWatchdog,
    resolve_stall_timeout_seconds,
    resolve_wedge_multiplier,
)
from brutex.observability.watchdog import WorkerEvent, WorkerState
from structlog.typing import EventDict


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

    def test_recovery_edge_forwards_to_the_on_recovery_hook(self) -> None:
        """DEC-PIPELINE-STALL-HEARTBEAT-001: the post-construction hook fires on recovery."""
        _, spawn = _capturing_spawn()
        clock = _FakeClock()
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=30.0, spawn=spawn, clock=clock)
        recovered: list[WorkerEvent] = []
        wd.on_recovery = recovered.append
        wd.start()
        clock.advance(31.0)
        wd.check_once()  # HUNG
        wd.beat()
        wd.check_once()  # HUNG→HEALTHY — must reach the hook
        assert len(recovered) == 1
        assert recovered[0].new_state == WorkerState.HEALTHY

    def test_recovery_hook_raising_is_swallowed(self) -> None:
        """A raising on_recovery hook is swallowed + logged — the timer never crashes."""
        _, spawn = _capturing_spawn()
        clock = _FakeClock()
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=30.0, spawn=spawn, clock=clock)

        def _boom(_event: WorkerEvent) -> None:
            raise RuntimeError("recovery hook boom")

        wd.on_recovery = _boom
        wd.start()
        clock.advance(31.0)
        wd.check_once()  # HUNG
        wd.beat()
        recovered = wd.check_once()  # must NOT raise out of the recovery forward
        assert len(recovered) == 1

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
        #: The job assigns its recovery hook post-construction (DEC-PIPELINE-STALL-
        #: HEARTBEAT-001) — mirrored here so the spy can replay a HUNG→HEALTHY edge.
        self.on_recovery: Callable[[WorkerEvent], None] | None = None
        #: The job assigns the wedge hook + revision probe post-construction
        #: (DEC-PULL-WEDGE-ESCALATE-001) — mirrored so the spy can replay a WEDGED verdict.
        self.on_wedged: Callable[[WorkerEvent], None] | None = None
        self.revision_probe: Callable[[], str | None] | None = None
        self.wedge_multiplier = 6.0
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

    def trigger_wedged(
        self, *, last_beat_seconds_ago: float = 3600.0, timeout: float = 600.0
    ) -> None:
        """Simulate the timer thread confirming the DUAL wedge signal."""
        assert self.on_wedged is not None  # the job wires it in start()
        self.on_wedged(
            WorkerEvent(
                worker_name=PULL_WORKER_NAME,
                new_state=WorkerState.HUNG,
                last_beat_seconds_ago=last_beat_seconds_ago,
                timeout_seconds=timeout,
            )
        )

    def trigger_recovery(self, *, last_beat_seconds_ago: float = 1.0) -> None:
        """Simulate the timer thread observing a HUNG→HEALTHY recovery edge."""
        assert self.on_recovery is not None  # the job wires it in start()
        self.on_recovery(
            WorkerEvent(
                worker_name=PULL_WORKER_NAME,
                new_state=WorkerState.HEALTHY,
                last_beat_seconds_ago=last_beat_seconds_ago,
                timeout_seconds=600.0,
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
    @pytest.fixture(autouse=True)
    def _disable_account_singleflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Disable the account-wide cross-process pull lock for these watchdog tests.

        DEC-ACCOUNT-PULL-SINGLEFLIGHT-001 makes :class:`BackgroundPullJob` take a real
        ``fcntl.flock`` under the production lake root by default. These tests probe the
        stall watchdog, not the cross-process guard, and several capture-but-never-run the
        worker — which would leak a process-wide flock into a sibling test. Point the
        default resolver at ``None`` (guard disabled); the guard's own coverage lives in
        ``test_pull_jobs_singleflight.py``.
        """
        from brutex.dashboard import pull_jobs

        monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)

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
        assert not job.running

    def test_ledger_sync_beats_the_watchdog(self) -> None:
        built, factory = _spy_factory()
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=_sync_spawn, watchdog_factory=factory
        )

        def work(_on_progress: ProgressHook) -> None:
            job.sync_from_ledger(completed=5, total=10, bars=500)

        job.start(work, total=10, ledger_synced=True)
        assert built[0].beats == 1  # the per-window ledger sync beat the watchdog

    def test_progress_beat_seam_keeps_watchdog_alive_under_contention(self) -> None:
        """A slow-but-progressing pull (no COMPLETED window, only lock-wait/pace beats)
        still beats the watchdog via the process-global seam — DEC-PULL-CONTENTION-
        ANTISTALL-001 GAP #15. The worker emits NO ``on_progress`` (no finished window);
        it only fires ``emit_progress_beat`` (as the bounded lock-wait + the pacer do),
        and that must reach the watchdog because ``_run`` registered the job's beat.
        """
        from brutex.observability.progress_beat import (
            emit_progress_beat,
            unregister_progress_beat,
        )

        unregister_progress_beat()  # clean slate
        built, factory = _spy_factory()
        job = _job(factory)
        seen_running: list[bool] = []

        def work(_on_progress: ProgressHook) -> None:
            # Simulate three lock-wait polls / pace returns — NO completed window.
            for _ in range(3):
                emit_progress_beat()
            # While the run is live the beat is registered (and the job is running).
            seen_running.append(job.running)

        assert job.start(work, total=1) is True
        spy = built[0]
        # The seam beat reached the watchdog 3× even with zero on_progress windows —
        # a slow-but-progressing pull is never falsely declared hung.
        assert spy.beats == 3
        assert seen_running == [True]
        # After the run the beat is unregistered — a stray emit no longer reaches it.
        emit_progress_beat()
        assert spy.beats == 3

    def test_hung_edge_alerts_and_marks_stalled_suspect_without_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HUNG edge → CRITICAL alert + STALLED-SUSPECT marker; run NOT failed, flight HELD.

        DEC-PIPELINE-STALL-HEARTBEAT-001, the forensic regression (operator run 2026-07-02):
        last fetch beat 13:58:21 → watchdog fired at ~14:11:10 (769 s silent) during the
        post-fetch stages and the OLD handler FAILED the progress + cleared ``_running`` +
        released the single-flight while the worker went on to SUCCEED at 14:15:08. The
        honest edge now alerts + marks stalled-suspect and keeps the single-flight held.
        """
        halts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)

        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        # A capturing WORKER spawner so the worker never finishes (simulates the silence).
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        assert job.start(lambda _h: None, total=1) is True
        assert job.running  # worker captured, still "running"

        # The timer thread declares the worker HUNG (the forensic 769 s silence).
        built[0].trigger_hung(last_beat_seconds_ago=769.0, timeout=600.0)

        assert len(halts) == 1  # one CRITICAL pull-halt alert fired (still detected!)
        assert isinstance(halts[0], TimeoutError)
        assert "stalled-suspect" in str(halts[0])
        # The timer is NOT stopped — it keeps polling so a later heartbeat recovery
        # (the greeks/prebuild/movers seams) can clear the marker.
        assert built[0].stopped is False
        snap = job.snapshot()
        assert snap.state == STATE_RUNNING  # NOT failed — the worker may still succeed
        assert snap.stalled is True
        assert "769" in snap.stall_detail and "stalled-suspect" in snap.stall_detail
        # The single-flight is HELD: a second pull is REFUSED while the worker is alive
        # (the double-run race the old release-on-hung design admitted).
        assert job.running is True
        assert job.start(lambda _h: None, total=1) is False

    def test_worker_success_after_hung_edge_finishes_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The forensic shape end-to-end: hung edge fires, worker later SUCCEEDS → done.

        The run must finish ``done`` (not the old false FAILED), the stalled marker must
        clear, and the single-flight must release only THEN.
        """
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)
        built[0].trigger_hung()
        assert job.snapshot().stalled is True
        assert job.running is True

        captured[0]()  # the "zombie" worker finishes — and it SUCCEEDS

        snap = job.snapshot()
        assert snap.state == STATE_DONE  # honest headline (was falsely FAILED before)
        assert snap.stalled is False  # finish clears the suspect marker
        assert not job.running  # single-flight released by the worker's own finally
        assert built[0].stopped is True  # the timer stops with the worker
        assert job.start(lambda _h: None, total=1) is True  # next pull accepted

    def test_recovery_edge_clears_stalled_suspect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A heartbeat landing after the HUNG edge clears the marker (HUNG→HEALTHY)."""
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)
        built[0].trigger_hung()
        assert job.snapshot().stalled is True
        built[0].trigger_recovery()
        snap = job.snapshot()
        assert snap.stalled is False
        assert snap.stall_detail == ""
        assert snap.state == STATE_RUNNING  # still in flight, honestly healthy again

    def test_on_hung_swallows_a_raising_progress_mark(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raise inside _on_hung (e.g. progress.mark_stalled) is swallowed, timer survives."""
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        built, factory = _spy_factory()

        class _BoomProgress(PullProgress):
            def mark_stalled(self, detail: str) -> None:
                raise RuntimeError("progress.mark_stalled boom")

        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=_BoomProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)
        # The raising progress.mark_stalled must NOT propagate out of _on_hung (best-effort).
        built[0].trigger_hung()
        assert job.running is True  # the run is untouched

    def test_on_recovered_swallows_a_raising_progress_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raise inside _on_recovered (progress.clear_stalled) is swallowed (best-effort)."""
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        built, factory = _spy_factory()

        class _BoomProgress(PullProgress):
            def clear_stalled(self) -> None:
                raise RuntimeError("progress.clear_stalled boom")

        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=_BoomProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)
        built[0].trigger_recovery()  # must NOT raise out — swallowed + logged
        assert job.running is True

    def test_late_hung_edge_for_prior_run_does_not_mark_new_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identity gate (FALSE arm): a late HUNG edge from a finished run never marks run B."""
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)  # run A
        captured[0]()  # A finishes cleanly → single-flight released
        assert job.start(lambda _h: None, total=1) is True  # run B, now current
        assert len(built) == 2
        # A late duplicate HUNG edge for the SUPERSEDED run A: the identity gate
        # (self._watchdog is watchdog_A) is False → run B is never marked.
        built[0].trigger_hung()
        snap = job.snapshot()
        assert snap.stalled is False
        assert job.running is True

    def test_late_recovery_edge_for_prior_run_does_not_touch_new_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identity gate (FALSE arm) for _on_recovered: a stale recovery never touches run B."""
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)  # run A
        captured[0]()  # A finishes
        job.start(lambda _h: None, total=1)  # run B, now current
        built[1].trigger_hung()  # B is genuinely stalled-suspect
        assert job.snapshot().stalled is True
        built[0].trigger_recovery()  # stale recovery from run A — gated out
        assert job.snapshot().stalled is True  # B's honest marker survives

    def test_stale_finally_identity_gate_false_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_run's finally identity gate (FALSE arm): a superseded worker never clobbers.

        Under DEC-PIPELINE-STALL-HEARTBEAT-001 the watchdog no longer clears ``_running``,
        so the finally is the ONE release path and its gate is normally always True. The
        gate is kept as defence in depth — this white-box test supersedes the prior
        trigger_hung-driven interleavings (which relied on the removed release-on-hung)
        and pins that a worker whose run identity was superseded leaves the flag alone.
        """
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        _built, factory = _spy_factory()
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(), spawn=captured.append, watchdog_factory=factory
        )
        job.start(lambda _h: None, total=1)  # run A (captured, in flight)
        # Simulate supersession of A's run identity (white-box: the invariant guard).
        job._watchdog = _SpyWatchdog(lambda _e: None)  # type: ignore[assignment]
        captured[0]()  # A's finally: gate False → _running untouched
        assert job.running is True

    def test_normal_finish_clears_running_and_allows_next_pull(self) -> None:
        """The SAFE path: a clean worker finish clears _running (identity gate True)."""
        built, factory = _spy_factory()
        job = _job(factory)  # synchronous worker spawner → runs + returns inline
        assert job.start(lambda _h: None, total=1) is True
        assert not job.running  # cleared in _run.finally (this run is current)
        assert built[0].stopped is True  # watchdog stopped on the clean path too
        assert job.start(lambda _h: None, total=1) is True  # next pull accepted

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
        assert not job.running

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
        assert not job.running


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


# ── DEC-PULL-WEDGE-ESCALATE-001 — STAGE 1 wedged-worker escalation ──────────


class TestResolveWedgeMultiplier:
    def test_default_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PULL_WEDGE_ENV, raising=False)
        assert resolve_wedge_multiplier() == DEFAULT_WEDGE_MULTIPLIER

    def test_blank_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_WEDGE_ENV, "   ")
        assert resolve_wedge_multiplier() == DEFAULT_WEDGE_MULTIPLIER

    def test_valid_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_WEDGE_ENV, "12")
        assert resolve_wedge_multiplier() == 12.0

    def test_non_numeric_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_WEDGE_ENV, "banana")
        assert resolve_wedge_multiplier() == DEFAULT_WEDGE_MULTIPLIER

    def test_non_positive_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_WEDGE_ENV, "-1")
        assert resolve_wedge_multiplier() == DEFAULT_WEDGE_MULTIPLIER


class _TokenProbe:
    """A settable lake-revision probe double (None = no signal / abstain)."""

    def __init__(self, token: str | None) -> None:
        self.token = token
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        return self.token


def _wedge_watchdog(
    *, probe: _TokenProbe | None, timeout: float = 30.0, multiplier: float = 6.0
) -> tuple[PullWatchdog, _FakeClock, list[WorkerEvent], list[WorkerEvent]]:
    """Build a PullWatchdog wired for wedge tests: fake clock, capturing hooks."""
    _, spawn = _capturing_spawn()
    clock = _FakeClock()
    hungs: list[WorkerEvent] = []
    wd = PullWatchdog(
        on_hung=hungs.append,
        timeout_seconds=timeout,
        spawn=spawn,
        clock=clock,
        wedge_multiplier=multiplier,
    )
    wedges: list[WorkerEvent] = []
    wd.on_wedged = wedges.append
    if probe is not None:
        wd.revision_probe = probe
    wd.start()
    return wd, clock, hungs, wedges


class TestPullWatchdogWedgeEscalation:
    def test_wedge_multiplier_defaults_to_resolver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PULL_WEDGE_ENV, "9")
        wd = PullWatchdog(on_hung=lambda _e: None, spawn=lambda _fn: None)
        assert wd.wedge_multiplier == 9.0

    def test_explicit_wedge_multiplier_overrides_resolver(self) -> None:
        wd = PullWatchdog(on_hung=lambda _e: None, spawn=lambda _fn: None, wedge_multiplier=4.0)
        assert wd.wedge_multiplier == 4.0

    def test_dual_signal_fires_once_terminal(self) -> None:
        """Zero heartbeats for 6x AND an unmoved token → ONE wedge verdict (latched)."""
        probe = _TokenProbe("rev-1")
        wd, clock, hungs, wedges = _wedge_watchdog(probe=probe)
        clock.advance(31.0)
        wd.check_once()  # HUNG edge (captures the baseline token)
        assert len(hungs) == 1 and wedges == []
        clock.advance(31.0)
        wd.check_once()  # 62 s silent < 180 s — below the wedge window, no verdict
        assert wedges == []
        clock.advance(120.0)  # 182 s silent >= 6 x 30
        wd.check_once()
        assert len(wedges) == 1
        assert wedges[0].last_beat_seconds_ago >= 180.0
        # Terminal latch: further polls never re-fire.
        clock.advance(500.0)
        wd.check_once()
        assert len(wedges) == 1

    def test_token_movement_defers_the_verdict(self) -> None:
        """A moved revision token (the worker IS writing) defers + restarts the window."""
        probe = _TokenProbe("rev-1")
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=probe)
        clock.advance(31.0)
        wd.check_once()  # HUNG (baseline rev-1)
        probe.token = "rev-2"  # the ledger checkpointed after the HUNG edge
        clock.advance(160.0)  # 191 s silent >= 180
        wd.check_once()
        assert wedges == []  # movement observed — no verdict, window restarted
        # The token now freezes at rev-2: the verdict needs the FULL window again.
        clock.advance(100.0)
        wd.check_once()
        assert wedges == []  # only 100 s since the observed movement
        clock.advance(100.0)  # 200 s since the movement >= 180
        wd.check_once()
        assert len(wedges) == 1

    def test_probe_none_token_abstains(self) -> None:
        """A probe with no signal (None token) NEVER escalates — honest abstention."""
        probe = _TokenProbe(None)
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=probe)
        clock.advance(31.0)
        wd.check_once()  # HUNG (baseline None)
        clock.advance(500.0)
        wd.check_once()
        assert wedges == []

    def test_no_probe_wired_abstains(self) -> None:
        """A probe-less deployment (revision_probe=None) never reaches WEDGED."""
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=None)
        clock.advance(31.0)
        wd.check_once()
        clock.advance(500.0)
        wd.check_once()
        assert wedges == []

    def test_baseline_none_adopts_first_real_token_then_escalates(self) -> None:
        """Baseline None (probe erred at the HUNG edge) is REPLACED by the first real token.

        Review MINOR fold: the feature's own target scenario — a wedged volume — makes
        the HUNG-edge probe itself likely to error, so a permanently-None baseline used
        to disable WEDGED for the rest of the run even after the probe recovered. Now
        the first later REAL token is adopted as a fresh baseline (NEVER escalating on
        that poll — no verdict against a blind baseline) and the wedge window restarts;
        a further full window of silence with the token unmoved then escalates.
        """
        probe = _TokenProbe(None)
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=probe)
        clock.advance(31.0)
        wd.check_once()  # HUNG — baseline captured as None (probe abstained at the edge)
        probe.token = "rev-1"
        clock.advance(500.0)
        wd.check_once()
        # The adoption poll NEVER escalates — the window restarts from this observation.
        assert wedges == []
        clock.advance(100.0)
        wd.check_once()
        assert wedges == []  # only 100 s since the adopted baseline < 180 s window
        clock.advance(100.0)  # 200 s since the adoption >= 180, token unmoved
        wd.check_once()
        assert len(wedges) == 1

    def test_baseline_none_and_probe_still_blind_abstains_forever(self) -> None:
        """A probe that NEVER recovers keeps abstaining — no verdict on a blind probe."""
        probe = _TokenProbe(None)
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=probe)
        clock.advance(31.0)
        wd.check_once()  # HUNG — baseline None
        for _ in range(5):
            clock.advance(500.0)
            wd.check_once()
        assert wedges == []

    def test_probe_runs_outside_the_lock(self) -> None:
        """The revision probe is NEVER called while ``_lock`` is held (review MINOR fold).

        The probe ``os.stat``s a possibly-wedged volume — the exact volume this feature
        suspects — so a probe under the lock could wedge the timer thread holding it
        and block a recovering worker's ``beat`` (the stalled-probe-under-lock class
        DEC-SURFACE-TRIAL-001 MINOR-1 fixed in ``SummaryCache``). Pinned white-box: on
        EVERY probe invocation (the HUNG-edge baseline capture AND the escalation
        read) the watchdog lock must be immediately acquirable.
        """
        acquired: list[bool] = []
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=None)

        def _lock_free_probe() -> str | None:
            got = wd._lock.acquire(blocking=False)
            acquired.append(got)
            if got:
                wd._lock.release()
            return "rev-1"

        wd.revision_probe = _lock_free_probe
        clock.advance(31.0)
        wd.check_once()  # HUNG edge — the baseline capture probe (outside the lock)
        clock.advance(200.0)
        wd.check_once()  # the escalation probe (outside the lock) — fires the verdict
        assert len(wedges) == 1
        assert acquired == [True, True]

    def test_beat_during_probe_read_defers_the_verdict(self) -> None:
        """A heartbeat landing WHILE the probe reads (lock free) still defers the verdict.

        The exact race the under-the-second-lock guard re-check defends: the probe runs
        outside the lock, so a recovering worker can now ``beat`` mid-read — the
        escalation must observe that sign of life and abstain, never fire a verdict
        decided before the beat.
        """
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=None)
        calls = {"n": 0}

        def _beating_probe() -> str | None:
            calls["n"] += 1
            if calls["n"] == 2:  # the escalation read — the worker recovers mid-probe
                wd.beat()
            return "rev-1"

        wd.revision_probe = _beating_probe
        clock.advance(31.0)
        wd.check_once()  # HUNG edge (baseline capture, probe call 1)
        clock.advance(200.0)
        wd.check_once()  # escalation read (probe call 2) — a beat lands mid-read
        assert calls["n"] == 2
        assert wedges == []  # the re-checked guards saw the recovery and deferred

    def test_missing_hung_baseline_abstains(self) -> None:
        """DEFENSIVE: no HUNG-edge baseline capture → abstain, never a blind verdict.

        Unreachable through the public API today (the HUNG edge always captures a
        baseline before the escalation can run) — pinned white-box so the defensive
        abstain branch keeps teeth if the edge wiring ever regresses.
        """
        probe = _TokenProbe("rev-1")
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=probe)
        clock.advance(31.0)
        wd.check_once()  # HUNG edge (captures the baseline)
        wd._hung_baseline_captured = False  # simulate a bypassed edge (whitebox)
        clock.advance(500.0)
        wd.check_once()
        assert wedges == []

    def test_beat_after_hung_defers_wedge(self) -> None:
        """Any heartbeat resets the silence clock — a sign of life defers the verdict."""
        probe = _TokenProbe("rev-1")
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=probe)
        clock.advance(31.0)
        wd.check_once()  # HUNG
        clock.advance(100.0)
        wd.beat()  # life! (also recovers on the next poll)
        clock.advance(100.0)  # only 100 s since the beat
        wd.check_once()
        assert wedges == []

    def test_no_wedge_while_healthy(self) -> None:
        """The escalation is evaluated only while HUNG — a healthy worker never wedges."""
        probe = _TokenProbe("rev-1")
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=probe)
        for _ in range(10):
            clock.advance(20.0)
            wd.beat()
            wd.check_once()
        assert wedges == []
        assert probe.calls == 0  # the probe is never touched while healthy

    def test_no_hook_wired_is_noop(self) -> None:
        """With no on_wedged hook the escalation short-circuits (zero probe reads)."""
        probe = _TokenProbe("rev-1")
        _, spawn = _capturing_spawn()
        clock = _FakeClock()
        wd = PullWatchdog(
            on_hung=lambda _e: None,
            timeout_seconds=30.0,
            spawn=spawn,
            clock=clock,
            wedge_multiplier=6.0,
        )
        wd.revision_probe = probe
        wd.start()
        clock.advance(31.0)
        wd.check_once()
        hung_edge_reads = probe.calls  # the HUNG edge itself captures a baseline
        clock.advance(500.0)
        wd.check_once()
        assert probe.calls == hung_edge_reads  # no escalation-path probe read

    def test_raising_wedge_hook_is_swallowed(self) -> None:
        """A raising on_wedged hook is swallowed + logged — the timer never crashes."""
        probe = _TokenProbe("rev-1")
        wd, clock, _hungs, _wedges = _wedge_watchdog(probe=probe)

        def _boom(_event: WorkerEvent) -> None:
            raise RuntimeError("wedge hook boom")

        wd.on_wedged = _boom
        clock.advance(31.0)
        wd.check_once()
        clock.advance(200.0)
        wd.check_once()  # must NOT raise out of the escalation

    def test_raising_probe_abstains_never_crashes(self) -> None:
        """A probe that RAISES (a stalled volume mid-stat) abstains — never a false wedge."""
        wd, clock, _hungs, wedges = _wedge_watchdog(probe=None)

        def _boom() -> str | None:
            raise OSError("stat wedged")

        wd.revision_probe = _boom
        clock.advance(31.0)
        wd.check_once()  # HUNG (baseline capture swallows the probe error -> None)
        clock.advance(500.0)
        wd.check_once()
        assert wedges == []


# ════════════════════════════════════════════════════════════════════════
# pull_stall.watchdog_alive — the throttled happy-path proof of life
# (DEC-PULL-TELEMETRY-TRIAD-001; ≤ 18 lines/hour, no new thread).
# ════════════════════════════════════════════════════════════════════════


class TestWatchdogAliveBeat:
    """One throttled INFO per check interval, derived from the EXISTING cadence."""

    @staticmethod
    def _alive(logs: list[EventDict]) -> list[EventDict]:
        return [e for e in logs if e["event"] == "pull_stall.watchdog_alive"]

    def test_first_check_logs_then_throttles_within_interval(self) -> None:
        from structlog.testing import capture_logs

        clock = _FakeClock()
        _captured, spawn = _capturing_spawn()
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=600.0, spawn=spawn, clock=clock)
        wd.start()
        with capture_logs() as logs:
            wd.check_once()  # first check → an early proof of life
            clock.advance(50.0)
            wd.check_once()  # 50 s < 200 s → throttled
        alive = self._alive(logs)
        assert len(alive) == 1
        assert alive[0]["state"] == WorkerState.HEALTHY.value
        assert alive[0]["timeout_seconds"] == 600.0
        assert alive[0]["interval_seconds"] == 200.0  # timeout/3 at the audited default

    def test_default_cadence_is_bounded_at_18_lines_per_hour(self) -> None:
        """At the default 600 s timeout the check cadence is 200 s = EXACTLY 18
        checks/hour; simulating one hour of timer ticks yields exactly 18 lines —
        the documented bound falls out of the existing cadence, no new thread."""
        from structlog.testing import capture_logs

        clock = _FakeClock()
        _captured, spawn = _capturing_spawn()
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=600.0, spawn=spawn, clock=clock)
        wd.start()
        with capture_logs() as logs:
            for _ in range(18):  # one hour of 200 s timer ticks
                wd.beat()  # a progressing worker (never hung)
                wd.check_once()
                clock.advance(200.0)
        assert len(self._alive(logs)) == 18

    def test_shrunk_env_timeout_still_respects_the_floor(self) -> None:
        """A small timeout (env-shrunk) checks every 10 s, but the alive log floors at
        ALIVE_LOG_MIN_INTERVAL_S — an hour of 10 s checks still logs ≤ 18 lines."""
        from brutex.dashboard.pull_stall import ALIVE_LOG_MIN_INTERVAL_S
        from structlog.testing import capture_logs

        clock = _FakeClock()
        _captured, spawn = _capturing_spawn()
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=30.0, spawn=spawn, clock=clock)
        wd.start()  # cadence timeout/3 = 10 s → the throttle floors at 200 s
        with capture_logs() as logs:
            for _ in range(360):  # one hour of 10 s checks
                wd.beat()
                wd.check_once()
                clock.advance(10.0)
        alive = self._alive(logs)
        assert len(alive) <= 18
        assert alive[0]["interval_seconds"] == ALIVE_LOG_MIN_INTERVAL_S

    def test_explicit_poll_seconds_above_floor_sets_the_interval(self) -> None:
        """start(poll_seconds=300) → the alive interval tracks the ACTUAL cadence."""
        from structlog.testing import capture_logs

        clock = _FakeClock()
        _captured, spawn = _capturing_spawn()
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=600.0, spawn=spawn, clock=clock)
        wd.start(poll_seconds=300.0)
        with capture_logs() as logs:
            wd.check_once()
            clock.advance(250.0)
            wd.check_once()  # 250 < 300 → throttled (a 200 s floor alone would log)
            clock.advance(60.0)
            wd.check_once()  # 310 ≥ 300 → logs
        alive = self._alive(logs)
        assert len(alive) == 2
        assert alive[0]["interval_seconds"] == 300.0

    def test_alive_log_reports_hung_state(self) -> None:
        """The alive line carries the worker's CURRENT state — a hung worker reads
        ``hung`` (the happy-path line never masks a stall; the edge events are
        byte-identical, logging is the only delta)."""
        from structlog.testing import capture_logs

        clock = _FakeClock()
        _captured, spawn = _capturing_spawn()
        edges: list[WorkerEvent] = []
        wd = PullWatchdog(on_hung=edges.append, timeout_seconds=600.0, spawn=spawn, clock=clock)
        wd.start()
        clock.advance(601.0)  # past the timeout with no beat → HUNG on the next check
        with capture_logs() as logs:
            events = wd.check_once()
        assert len(events) == 1  # PARITY: the hung edge fired exactly as before
        assert len(edges) == 1
        alive = self._alive(logs)
        assert len(alive) == 1
        assert alive[0]["state"] == WorkerState.HUNG.value

    def test_bare_check_once_without_start_reads_unregistered(self) -> None:
        """A never-started wrapper logs ``state="unregistered"`` instead of raising."""
        from structlog.testing import capture_logs

        clock = _FakeClock()
        _captured, spawn = _capturing_spawn()
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=600.0, spawn=spawn, clock=clock)
        with capture_logs() as logs:
            assert wd.check_once() == ()  # PARITY: the empty-registry return is unchanged
        alive = self._alive(logs)
        assert len(alive) == 1
        assert alive[0]["state"] == "unregistered"

    def test_alive_log_carries_the_stamped_run_id(self) -> None:
        """DEC-PULL-BEAT-PERRUN-001: ``run_id`` defaults ``""``, is assignable
        POST-construction (the on_recovery pattern — the factory signature stays
        ``(on_hung)``), and rides the alive event as an ADDITIVE ``pull_id`` field.
        Event name + throttle behaviour byte-unchanged."""
        from structlog.testing import capture_logs

        clock = _FakeClock()
        _captured, spawn = _capturing_spawn()
        wd = PullWatchdog(on_hung=lambda _e: None, timeout_seconds=600.0, spawn=spawn, clock=clock)
        assert wd.run_id == ""  # the unstamped default
        wd.start()
        with capture_logs() as logs:
            wd.check_once()
        alive = self._alive(logs)
        assert len(alive) == 1
        assert alive[0]["pull_id"] == ""  # a bare/test watchdog reads honestly empty

        clock2 = _FakeClock()
        wd2 = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=600.0, spawn=spawn, clock=clock2
        )
        wd2.run_id = "abc123def456"  # the job's post-construction stamp
        wd2.start()
        with capture_logs() as logs:
            wd2.check_once()
        alive = self._alive(logs)
        assert len(alive) == 1
        assert alive[0]["pull_id"] == "abc123def456"


# ════════════════════════════════════════════════════════════════════════
# DEC-RUST-PULL-STATE-001 (flip phase) — the fail-closed Rust decision kernel
# ════════════════════════════════════════════════════════════════════════
#
# The wedge/alive decision plane is served by the ``brutex_pull_state``
# kernel — the ONLY decision path (the Python oracle was deleted with the
# flip).  These pins cover: the removed-flag refusal, kernel AUTHORITY per
# arm token (a deliberately-wrong fake kernel's verdict is SERVED, proving
# the consult), the fail-closed ``PullStateKernelError`` raise on every
# wheel fault (absent / stale / raising / poisoned — never a silent wrong
# verdict), and the FAIL-SAFE env resolvers (Python-only, never routed
# through the kernel).  Real-wheel law pins live in
# ``tests/property/test_pull_stall_rust_parity.py`` + the frozen golden.


class _RecordingBombModule:
    """A ``sys.modules`` plant that RECORDS every attribute access, then raises.

    Raises ``AttributeError`` (never ``AssertionError`` — CodeQL's
    ``__getattr__`` contract).  The facade's resolver reads the plant as a
    wheel LACKING the symbol (``getattr`` swallows the raise into its
    default) and refuses ``reason=wheel_stale`` — the fail-closed pins below
    assert BOTH the typed raise and the recorded touch.
    """

    def __init__(self) -> None:
        self.accesses: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.accesses.append(name)
        raise AttributeError(name)


def _install_stall_kernel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wedge: object = None,
    alive: object = None,
) -> None:
    """Plant a fake ``brutex_pull_state`` module carrying the given kernels."""
    import sys
    from types import SimpleNamespace

    ns = SimpleNamespace()
    if wedge is not None:
        ns.wedge_decision = wedge
    if alive is not None:
        ns.alive_log_due = alive
    monkeypatch.setitem(sys.modules, "brutex_pull_state", ns)


def _seam_wedge_watchdog() -> tuple[PullWatchdog, _FakeClock, list[WorkerEvent]]:
    """A started, genuinely-HUNG watchdog with a captured 'revA' baseline,
    silent past the full wedge window — the escalate-shaped module state."""
    clock = _FakeClock()
    wd = PullWatchdog(
        on_hung=lambda _e: None,
        timeout_seconds=30.0,
        spawn=lambda _fn: None,
        clock=clock,
        wedge_multiplier=6.0,
    )
    wedges: list[WorkerEvent] = []
    wd.on_wedged = wedges.append
    wd.start()
    clock.advance(31.0)
    wd._wd.check_once()  # the real HUNG edge
    assert wd.state() is WorkerState.HUNG
    wd._baseline_capture_pending = False
    wd._hung_baseline_captured = True
    wd._hung_baseline_token = "revA"
    clock.advance(180.0)  # total silence 211 s > 6 x 30 s
    return wd, clock, wedges


class TestPullStateRustKernelStall:
    """The flip-phase kernel path: authority, refusals and fail-closed faults."""

    # ── the removed opt-in knob refuses LOUDLY (ANY spelling) ─────────────

    @pytest.mark.parametrize("value", ["1", "0", "true", "off"])
    def test_set_removed_flag_refuses_every_spelling(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """A SET ``BRUTEX_PULL_STATE_RUST`` — truthy AND falsy spellings — is
        the typed refusal at the first kernel consult (the knob is removed;
        a stale deployment config must fail LOUD, never read as honoured)."""
        from brutex.pull_state_rust import PullStateFlagRemovedError

        monkeypatch.setenv("BRUTEX_PULL_STATE_RUST", value)
        wd, _clock, wedges = _seam_wedge_watchdog()
        with pytest.raises(PullStateFlagRemovedError, match="BRUTEX_PULL_STATE_RUST"):
            wd._maybe_escalate_locked("revA")
        assert not wedges  # refused BEFORE any verdict — nothing fired

    # ── kernel AUTHORITY per arm token (fake kernels are SERVED) ──────────

    def test_kernel_escalate_token_is_served_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deliberately-WRONG escalate token from a fake kernel is SERVED
        (fires on a HEALTHY worker the pre-flip law would refuse) — proving
        the kernel is the decision authority, not a re-derived Python law."""
        _install_stall_kernel(monkeypatch, wedge=lambda *a: 5)
        clock = _FakeClock()
        wd = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=30.0, spawn=lambda _fn: None, clock=clock
        )
        wedges: list[WorkerEvent] = []
        wd.on_wedged = wedges.append
        wd.start()  # HEALTHY — the law would refuse at the HUNG guard
        wd._maybe_escalate_locked("revA")
        assert len(wedges) == 1
        assert wd._wedged_fired is True

    @pytest.mark.parametrize("token", [0, 1, 4])
    def test_kernel_noop_tokens_mutate_nothing(
        self, monkeypatch: pytest.MonkeyPatch, token: int
    ) -> None:
        """Tokens 0/1/4 (refused / probe abstained / window unspanned) are
        deliberate no-ops — no hook, no latch, no baseline/window mutation."""
        _install_stall_kernel(monkeypatch, wedge=lambda *a, _t=token: _t)
        wd, _clock, wedges = _seam_wedge_watchdog()
        wd._maybe_escalate_locked("revB")
        assert not wedges
        assert wd._wedged_fired is False
        assert wd._hung_baseline_token == "revA"
        assert wd._wedge_window_restart_at is None

    @pytest.mark.parametrize("token", [2, 3])
    def test_kernel_adopt_and_rebaseline_tokens_restart_the_window(
        self, monkeypatch: pytest.MonkeyPatch, token: int
    ) -> None:
        """Tokens 2/3 run the ONE shared restart-window action body."""
        _install_stall_kernel(monkeypatch, wedge=lambda *a, _t=token: _t)
        wd, clock, wedges = _seam_wedge_watchdog()
        wd._maybe_escalate_locked("revB")
        assert not wedges
        assert wd._hung_baseline_token == "revB"  # the shared action body ran
        assert wd._wedge_window_restart_at == clock.t

    def test_real_kernel_escalate_logline_is_the_pinned_bytes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The escalate-shaped state through the REAL kernel emits the
        byte-pinned CRITICAL printf line — the action body is retained
        Python, the kernel only decided the arm."""
        import logging

        wd, _clock, wedges = _seam_wedge_watchdog()
        with caplog.at_level(logging.CRITICAL, logger="brutex.dashboard.pull_stall"):
            wd._maybe_escalate_locked("revA")
        assert len(wedges) == 1
        (record,) = [r for r in caplog.records if "pull_stall.wedged" in r.getMessage()]
        line = record.getMessage()
        assert "pull_stall.wedged worker=dashboard-pull" in line
        assert "last_beat_seconds_ago=211.0" in line
        assert "wedge_multiplier=6.0" in line

    def test_wants_probe_serves_the_kernel_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """token != 0 ⇔ wants probe — served verbatim; the wants-probe
        consult passes ``current=None`` (the kernel decides lazily)."""
        seen: list[tuple[object, ...]] = []

        def kernel(*args: object) -> int:
            seen.append(args)
            return 1  # abstain-shaped: the guards passed, current is None

        _install_stall_kernel(monkeypatch, wedge=kernel)
        clock = _FakeClock()
        wd = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=30.0, spawn=lambda _fn: None, clock=clock
        )
        wd.on_wedged = lambda _e: None
        wd.start()  # HEALTHY — the pre-flip law would say False
        assert wd._escalation_wants_probe_locked() is True
        assert seen[-1][-1] is None

    def test_wants_probe_kernel_zero_token_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """token 0 ⇔ no probe needed — the lazy-probe gate stays kernel-fed."""
        _install_stall_kernel(monkeypatch, wedge=lambda *a: 0)
        wd, _clock, _wedges = _seam_wedge_watchdog()
        assert wd._escalation_wants_probe_locked() is False

    def test_hook_unbound_short_circuits_the_state_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The input packing keeps the pinned guard ORDER: with no
        ``on_wedged`` the underlying state is never read (an unregistered
        worker cannot raise) and the kernel sees ``hung=False``."""
        seen: list[tuple[object, ...]] = []

        def kernel(*args: object) -> int:
            seen.append(args)
            return 0

        _install_stall_kernel(monkeypatch, wedge=kernel)
        clock = _FakeClock()
        wd = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=30.0, spawn=lambda _fn: None, clock=clock
        )
        wd.start()
        wd.on_wedged = None  # hook unbound — the packing short-circuits here
        assert wd._escalation_wants_probe_locked() is False
        # hung=False was passed WITHOUT reading state (flags refuse first).
        assert seen and seen[-1][0] is False and seen[-1][2] is False

    # ── kernel faults are FAIL-CLOSED typed raises (no oracle exists) ─────

    def test_missing_wheel_raises_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        from brutex.pull_state_rust import PullStateKernelError

        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # ImportError
        wd, _clock, wedges = _seam_wedge_watchdog()
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            wd._maybe_escalate_locked("revA")
        assert not wedges  # nothing fired — refused, never a guessed verdict

    def test_stale_wheel_bomb_is_touched_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The recording bomb proves the kernel module IS consulted — and its
        missing-symbol shape is the typed ``wheel_stale`` refusal."""
        import sys

        from brutex.pull_state_rust import PullStateKernelError

        bomb = _RecordingBombModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        wd, _clock, wedges = _seam_wedge_watchdog()
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            wd._maybe_escalate_locked("revA")
        assert not wedges
        assert "wedge_decision" in bomb.accesses  # PROOF of the consult

    def test_raising_kernel_raises_kernel_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.pull_state_rust import PullStateKernelError

        def boom(*_a: object) -> int:
            raise RuntimeError("wheel poisoned")

        _install_stall_kernel(monkeypatch, wedge=boom, alive=boom)
        wd, _clock, wedges = _seam_wedge_watchdog()
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            wd._maybe_escalate_locked("revA")
        assert not wedges

    @pytest.mark.parametrize("poison", [True, 6, -1, "5", None, 2.0])
    def test_poisoned_token_raises_and_mutates_nothing(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        """The facade's refuse-poison gate (bool pun / out-of-vocabulary /
        wrong type) is the typed raise — and NO action body ran."""
        from brutex.pull_state_rust import PullStateKernelError

        _install_stall_kernel(monkeypatch, wedge=lambda *a, _p=poison: _p)
        wd, _clock, wedges = _seam_wedge_watchdog()
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            wd._maybe_escalate_locked("revB")
        assert not wedges
        assert wd._hung_baseline_token == "revA"  # never rebaselined
        assert wd._wedged_fired is False

    def test_check_once_propagates_a_kernel_fault(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fault escapes :meth:`PullWatchdog.check_once` LOUDLY — the
        timer loop dies visibly rather than serving a silent wrong verdict
        (the fail-closed disposition at the module's public surface)."""
        import sys

        from brutex.pull_state_rust import PullStateKernelError

        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        wd, _clock, _wedges = _seam_wedge_watchdog()
        with pytest.raises(PullStateKernelError):
            wd.check_once()

    # ── the alive-throttle kernel ─────────────────────────────────────────

    def test_alive_kernel_authority_and_stamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fake due=True verdict is SERVED (the pre-flip law would
        throttle at since==0) and the retained stamp body runs."""
        _install_stall_kernel(monkeypatch, alive=lambda *_a: True)
        clock = _FakeClock()
        wd = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=600.0, spawn=lambda _fn: None, clock=clock
        )
        wd.start()
        wd._last_alive_log = clock.t  # the law would THROTTLE (since == 0)
        payload = wd._alive_payload_locked()
        assert payload is not None  # the kernel verdict is served
        assert payload["state"] == "healthy"
        assert wd._last_alive_log == clock.t  # the shared stamp body ran

    def test_alive_kernel_false_suppresses_even_the_first_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_stall_kernel(monkeypatch, alive=lambda *_a: False)
        clock = _FakeClock()
        wd = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=600.0, spawn=lambda _fn: None, clock=clock
        )
        wd.start()  # first check: the -inf seed — the law would emit
        assert wd._alive_payload_locked() is None

    def test_alive_real_kernel_first_check_emits_then_throttles(self) -> None:
        """The REAL kernel serves the pre-flip throttle law: the ``-inf``
        first-check seed emits, the immediate repeat throttles."""
        clock = _FakeClock()
        wd = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=600.0, spawn=lambda _fn: None, clock=clock
        )
        wd.start()
        assert wd._alive_payload_locked() is not None  # the -inf seed emits
        assert wd._alive_payload_locked() is None  # then throttles

    def test_alive_missing_wheel_raises_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        from brutex.pull_state_rust import PullStateKernelError

        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        clock = _FakeClock()
        wd = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=600.0, spawn=lambda _fn: None, clock=clock
        )
        wd.start()
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            wd._alive_payload_locked()

    # ── the FAIL-SAFE resolvers stay Python-only ──────────────────────────

    def test_resolvers_stay_fail_safe_and_never_consult_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Junk env values silently fall back to the audited defaults — the
        resolvers are Python-only (their ``float()`` quirks are frozen in
        the pull-stall golden) and never touch the kernel: the bomb plant
        records ZERO accesses across both resolves."""
        import sys

        bomb = _RecordingBombModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        monkeypatch.setenv(PULL_STALL_ENV, "junk")
        monkeypatch.setenv(PULL_WEDGE_ENV, "-3")
        assert resolve_stall_timeout_seconds() == DEFAULT_STALL_TIMEOUT_SECONDS
        assert resolve_wedge_multiplier() == DEFAULT_WEDGE_MULTIPLIER
        assert bomb.accesses == []
