"""Tests for `brutex.dashboard.pull_jobs.BackgroundPullJob`.

100% line+branch via a synchronous injected spawner (no real threads): the
single-flight guard, the success → done path, the worker-exception → error
path, the positional→keyword progress adaptation, and the `running` / snapshot
accessors. One real-thread test exercises the daemon default end-to-end.

The DEC-DASHBOARD-FASTREAD-001 hooks are covered here too: the write funnel
feeds the incremental :class:`CoverageCounters` (idempotent on ``bars == 0``) and
invalidates an injected summary cache (``mark_dirty``) once per window.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from brutex.dashboard import pull_jobs
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.pull_progress import STATE_DONE, STATE_ERROR, STATE_RUNNING, PullProgress
from brutex.dashboard.pull_stall import PullWatchdog
from brutex.dashboard.summary_cache import CoverageCounters
from brutex.observability.watchdog import WorkerEvent, WorkerState


@pytest.fixture(autouse=True)
def _disable_account_singleflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the account-wide cross-process pull lock for these unit tests.

    DEC-ACCOUNT-PULL-SINGLEFLIGHT-001 makes :class:`BackgroundPullJob` take a real
    ``fcntl.flock`` under the production lake root by default. These tests are about the
    job's progress / single-flight-flag / watchdog behaviour, NOT the cross-process
    guard, so we point the default lake-root resolver at ``None`` (guard disabled) — this
    avoids touching the real ``~/.brutex/lake`` and avoids a captured-but-never-run worker
    leaking a process-wide flock into a sibling test. The dedicated guard tests live in
    ``test_pull_jobs_singleflight.py`` and inject their own resolver / factory.
    """
    monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)


class _FakeClock:
    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        self._t += 1.0
        return self._t


def _sync_spawn(fn: Callable[[], None]) -> None:
    """Run the worker inline so the test is deterministic."""
    fn()


def _job() -> BackgroundPullJob:
    return BackgroundPullJob(progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn)


def test_successful_job_advances_then_finishes() -> None:
    job = _job()

    def work(on_progress: ProgressHook) -> None:
        on_progress("NIFTY 1minute", 100, False, False)
        on_progress("NIFTY 1day", 5, False, False)

    started = job.start(work, total=2, detail="kick")
    assert started is True
    snap = job.snapshot()
    assert snap.state == STATE_DONE
    assert snap.detail == "done"
    assert snap.completed == 2
    assert snap.bars == 105
    assert snap.failed == 0
    assert snap.current == "NIFTY 1day"
    assert job.running is False


def test_worker_exception_becomes_error_state() -> None:
    job = _job()

    def work(on_progress: ProgressHook) -> None:
        on_progress("13 1minute", 1, False, False)
        raise RuntimeError("boom")

    assert job.start(work, total=1) is True
    snap = job.snapshot()
    assert snap.state == STATE_ERROR
    assert snap.detail == "RuntimeError: boom"
    assert job.running is False  # flag reset even on failure


def test_on_success_fires_only_on_a_successful_run() -> None:
    """A clean return fires the success hook exactly once (MAJOR-1, DEC-DASHBOARD-RESULTS-PAGE-001)."""
    job = _job()
    calls: list[int] = []

    def work(on_progress: ProgressHook) -> None:
        on_progress("NIFTY 1minute", 10, False, False)

    # Call OUT of the assert: ``start`` spawns the worker (a side effect) and a
    # bare ``assert call()`` is dropped under ``python -O`` (CodeQL
    # py/side-effect-in-assert). Store the result, then assert it.
    started = job.start(work, total=1, on_success=lambda: calls.append(1))
    assert started is True
    assert job.snapshot().state == STATE_DONE
    assert calls == [1]  # fired once on success


def test_on_success_does_not_fire_on_a_failed_run() -> None:
    """A worker exception NEVER fires the success hook (the leaderboard cache stays untouched)."""
    job = _job()
    calls: list[int] = []

    def work(on_progress: ProgressHook) -> None:
        raise RuntimeError("boom")

    started = job.start(work, total=1, on_success=lambda: calls.append(1))
    assert started is True
    assert job.snapshot().state == STATE_ERROR
    assert calls == []  # never fired on failure


def test_on_success_raising_is_swallowed_and_never_crashes_the_worker() -> None:
    """A success hook that raises is logged + swallowed; the job still reaches DONE + clears running."""
    job = _job()

    def work(on_progress: ProgressHook) -> None:
        return None

    def boom() -> None:
        raise RuntimeError("hook blew up")

    started = job.start(work, total=1, on_success=boom)
    assert started is True
    assert job.snapshot().state == STATE_DONE  # success state unaffected by the raising hook
    assert job.running is False  # the finally still cleared the single-flight flag


def test_pull_halt_global_stop_fires_alert_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pull-wide LakeWriteHalt → error state AND a best-effort halt alert fires once.

    DEC-TELEGRAM-PULL-HALT-DASHBOARD-001 — the dashboard twin of the CLI cmd_fetch
    wiring: a disk-full/OOM/quota global-stop halts EVERY remaining window, so the
    operator gets a CRITICAL Telegram alert. The error state already reported is
    unchanged; the alert is best-effort (the emit is monkeypatched here).
    """
    from brutex.data.errors import LakeWriteHalt

    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    job = _job()

    def work(_on_progress: ProgressHook) -> None:
        raise LakeWriteHalt("disk_full")

    assert job.start(work, total=1) is True
    snap = job.snapshot()
    assert snap.state == STATE_ERROR  # error state byte-unchanged
    assert "LakeWriteHalt" in snap.detail
    assert len(halts) == 1  # alert fired EXACTLY once for the global stop
    assert isinstance(halts[0], LakeWriteHalt)


def test_generic_worker_error_does_not_fire_pull_halt_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-symbol / generic worker error → error state but NO pull-halt alert."""
    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    job = _job()

    def work(_on_progress: ProgressHook) -> None:
        raise RuntimeError("boom")

    assert job.start(work, total=1) is True
    assert job.snapshot().state == STATE_ERROR
    assert halts == []  # a generic worker error NEVER fires the pull-halt alert


def test_failed_unit_flag_is_forwarded() -> None:
    job = _job()
    job.start(lambda on_progress: on_progress("BAD 1minute", 0, True, False), total=1)
    snap = job.snapshot()
    assert snap.failed == 1
    assert snap.state == STATE_DONE  # a failed UNIT is not a failed JOB


def test_no_data_gap_flag_is_forwarded_and_not_a_failure() -> None:
    # DEC-DHAN-NO-DATA-GAP-COUNT-001: a benign DH-905 gap tick (gap=True) bumps
    # ``gaps``, NOT ``failed`` — so a known empty boundary stops reading as a failure.
    job = _job()
    job.start(lambda on_progress: on_progress("NIFTY NEXT 50 1day", 0, False, True), total=1)
    snap = job.snapshot()
    assert snap.gaps == 1
    assert snap.failed == 0
    assert snap.state == STATE_DONE


def test_single_flight_blocks_second_start() -> None:
    # A spawner that captures (does NOT run) the worker keeps the job "running",
    # so a second start must be a no-op returning False.
    captured: list[Callable[[], None]] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_FakeClock()),
        spawn=captured.append,
    )
    first = job.start(lambda _h: None, total=1)
    assert first is True
    assert job.running is True  # spawner hasn't run the worker yet
    second = job.start(lambda _h: None, total=1)
    assert second is False  # single-flight: blocked
    assert len(captured) == 1  # only the first job was scheduled
    # Drain the captured worker → job goes idle again.
    captured[0]()
    assert job.running is False
    assert job.snapshot().state == STATE_DONE


def test_running_true_during_work() -> None:
    job = _job()
    seen: list[bool] = []
    job.start(lambda _h: seen.append(job.running), total=1)
    assert seen == [True]  # inside the worker the job reports running


def test_default_progress_when_none_injected() -> None:
    # No injected tracker → a fresh PullProgress is created internally.
    job = BackgroundPullJob(spawn=_sync_spawn)
    job.start(lambda on_progress: on_progress("X", 1, False, False), total=1)
    assert job.snapshot().state == STATE_DONE


def test_real_daemon_thread_end_to_end() -> None:
    # Exercise the default daemon spawner (the pragma path is the lambda only;
    # here we use a real thread + join to prove the engine works off-thread).
    done = threading.Event()
    progress = PullProgress(clock=_FakeClock())

    def spawn(fn: Callable[[], None]) -> None:
        t = threading.Thread(target=fn)
        t.start()
        t.join()
        done.set()

    job = BackgroundPullJob(progress=progress, spawn=spawn)
    job.start(lambda on_progress: on_progress("13 1minute", 7, False, False), total=1)
    assert done.is_set()
    assert job.snapshot().state == STATE_DONE
    assert job.snapshot().bars == 7


def test_snapshot_running_state_before_completion() -> None:
    # With a capturing spawner the tracker is started but not yet finished.
    captured: list[Callable[[], None]] = []
    job = BackgroundPullJob(progress=PullProgress(clock=_FakeClock()), spawn=captured.append)
    job.start(lambda _h: None, total=3, detail="warming")
    snap = job.snapshot()
    assert snap.state == STATE_RUNNING
    assert snap.total == 3
    assert snap.detail == "warming"


# ────────────────────────────────────────────────────────────────────────
# DEC-DASHBOARD-FASTREAD-001 — coverage counters + summary-cache invalidation
# ────────────────────────────────────────────────────────────────────────


class _SpyCache:
    """A minimal summary-cache double recording every mark_dirty call."""

    def __init__(self) -> None:
        self.dirty_calls = 0

    def mark_dirty(self) -> None:
        self.dirty_calls += 1


def test_write_funnel_feeds_coverage_counters() -> None:
    # The default (auto-created) counters accumulate every window's POST-DEDUP
    # bar count through the single _on_progress funnel.
    job = _job()

    def work(on_progress: ProgressHook) -> None:
        on_progress("NIFTY 1minute", 100, False, False)
        on_progress("NIFTY 1minute", 50, False, False)
        on_progress("BANKNIFTY 1minute", 7, False, False)

    job.start(work, total=3)
    snap = job.counters.snapshot()
    assert snap.by_label == {"NIFTY 1minute": 150, "BANKNIFTY 1minute": 7}
    assert snap.total_bars == 157


def test_injected_counters_are_used() -> None:
    counters = CoverageCounters()
    job = BackgroundPullJob(
        progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn, counters=counters
    )
    job.start(lambda on_progress: on_progress("X 1minute", 9, False, False), total=1)
    assert job.counters is counters
    assert counters.snapshot().total_bars == 9


def test_duplicate_repull_zero_bars_leaves_counter_unchanged() -> None:
    # bars == 0 (post-dedup duplicate) is a no-op → idempotent headline.
    job = _job()
    job.start(lambda on_progress: on_progress("DUP 1minute", 0, False, False), total=1)
    snap = job.counters.snapshot()
    assert snap.total_bars == 0
    assert snap.by_label == {}


def test_summary_cache_invalidated_once_per_window() -> None:
    cache = _SpyCache()
    job = BackgroundPullJob(
        progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn, summary_cache=cache
    )

    def work(on_progress: ProgressHook) -> None:
        on_progress("A 1minute", 10, False, False)
        on_progress("B 1minute", 0, True, False)  # a failed/zero window still ticks the funnel

    job.start(work, total=2)
    # mark_dirty fires on EVERY progress tick (so the next poll refreshes),
    # independent of whether bars were written.
    assert cache.dirty_calls == 2


def test_no_summary_cache_means_no_invalidation_branch() -> None:
    # Default job has summary_cache=None → the mark_dirty branch is skipped
    # (no AttributeError), proving the None guard.
    job = _job()
    job.start(lambda on_progress: on_progress("X 1minute", 1, False, False), total=1)
    assert job.snapshot().state == STATE_DONE


class _Cache:
    """A minimal mark_dirty-able double (CLAUDE.md §4 — behaviour not concrete type)."""

    def __init__(self) -> None:
        self.dirty = 0

    def mark_dirty(self) -> None:
        self.dirty += 1


class TestLedgerSynced:
    """DEC-SPOT-WINDOW-PROGRESS-001: ledger-synced mode drives the bar per WINDOW."""

    def test_sync_from_ledger_sets_absolute_bar(self) -> None:
        cache = _Cache()
        job = BackgroundPullJob(
            progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn, summary_cache=cache
        )

        def work(_on_progress: ProgressHook) -> None:
            job.sync_from_ledger(
                completed=118, total=10906, bars=12054, failed=0, current="NIFTYSMALL 1day"
            )

        job.start(work, total=779, ledger_synced=True)
        snap = job.snapshot()
        assert snap.total == 10906
        assert snap.completed == 118
        assert snap.bars == 12054
        assert snap.current == "NIFTYSMALL 1day"
        assert cache.dirty >= 1  # the sync refreshes the coverage summary mid-pull

    def test_synced_mode_per_pair_hook_only_sets_label_not_completed(self) -> None:
        # In ledger-synced mode the per-pair hook must NOT advance completed (the bar
        # comes from the ledger sync); it only refreshes the "current" label + counters.
        job = BackgroundPullJob(progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn)

        def work(on_progress: ProgressHook) -> None:
            on_progress("NIFTY 1day", 500, False, False)  # a finished pair
            on_progress("BANKNIFTY 1day", 700, False, False)

        job.start(work, total=10, ledger_synced=True)
        snap = job.snapshot()
        assert snap.completed == 0  # NOT advanced per pair (ledger drives it)
        assert snap.bars == 0  # bars come from the ledger sync, not the per-pair hook
        assert snap.current == "BANKNIFTY 1day"  # label still refreshed
        assert job.counters.snapshot().total_bars == 1200  # counters still fed (500 + 700)

    def test_default_mode_still_advances_per_pair(self) -> None:
        # ledger_synced defaults to False → the per-pair advance is byte-unchanged.
        job = BackgroundPullJob(progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn)
        job.start(lambda h: h("NIFTY 1day", 5, False, False), total=2)
        assert job.snapshot().completed == 1

    def test_sync_from_ledger_without_cache_is_a_noop_on_dirty(self) -> None:
        # No summary cache wired → sync still updates the bar, just no mark_dirty.
        job = BackgroundPullJob(progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn)
        job.start(
            lambda _h: job.sync_from_ledger(completed=3, total=10, bars=30),
            total=10,
            ledger_synced=True,
        )
        assert job.snapshot().completed == 3


# ────────────────────────────────────────────────────────────────────────
# Stall watchdog `_on_hung` + the watchdog-identity gate (DEC-PULL-STALL-DETECTION-001)
# ────────────────────────────────────────────────────────────────────────


def _noop_spawn(_fn: Callable[[], None]) -> None:
    """A timer spawner that NEVER runs the timer body, so a real ``PullWatchdog``
    can be built (and its ``on_hung`` captured) without a background thread firing.
    """


def _hung_event() -> WorkerEvent:
    """A HEALTHY→HUNG :class:`WorkerEvent` for driving ``_on_hung`` deterministically."""
    return WorkerEvent(
        worker_name="dashboard-pull",
        new_state=WorkerState.HUNG,
        last_beat_seconds_ago=120.0,
        timeout_seconds=30.0,
    )


class TestOnHung:
    """``BackgroundPullJob._on_hung``: the stall-watchdog HUNG edge + identity gate.

    The job builds its watchdog via the injected ``watchdog_factory`` (given the
    ``on_hung`` edge callback). Here the factory captures that callback so the test
    can fire it directly with a HUNG ``WorkerEvent`` — exercising the whole
    best-effort body (stop the timer, halt alert, ``progress.fail``, identity-gated
    ``_running`` clear) WITHOUT a real daemon timer.
    """

    def _job_capturing_on_hung(
        self, captured: list[Callable[[WorkerEvent], None]]
    ) -> BackgroundPullJob:
        """A job whose watchdog factory records the ``on_hung`` callback + never spawns."""

        def factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
            captured.append(on_hung)
            return PullWatchdog(
                on_hung=on_hung, timeout_seconds=30.0, spawn=_noop_spawn, clock=_FakeClock()
            )

        def _capture_only(_fn: Callable[[], None]) -> None:
            """Capture-only spawner: never runs the worker, so the run stays in flight."""

        return BackgroundPullJob(
            progress=PullProgress(clock=_FakeClock()),
            spawn=_capture_only,
            watchdog_factory=factory,
        )

    def test_on_hung_marks_failed_and_clears_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A HUNG edge while THIS run is current: progress → failed, _running cleared,
        # and a best-effort halt alert fires (monkeypatched so no real notifier runs).
        alerts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)
        captured: list[Callable[[WorkerEvent], None]] = []
        job = self._job_capturing_on_hung(captured)

        # Start the job with a capture-only spawner → the worker never runs, so the
        # watchdog stays the CURRENT run's watchdog when on_hung fires.
        started = job.start(lambda _h: None, total=1, detail="stalled-pull")
        assert started is True
        assert job.running is True

        captured[0](_hung_event())  # fire the HUNG edge directly

        snap = job.snapshot()
        assert snap.state == STATE_ERROR  # _on_hung marked the run failed
        assert "stalled" in snap.detail  # the stall detail is surfaced
        assert job.running is False  # identity gate matched → _running cleared
        assert len(alerts) == 1  # one best-effort halt alert
        assert isinstance(alerts[0], TimeoutError)

    def test_on_hung_late_fire_does_not_clobber_a_newer_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A STALE on_hung (its run already superseded by a newer watchdog) must NOT
        # clear _running — the identity gate (`self._watchdog is watchdog`) is False.
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        captured: list[Callable[[WorkerEvent], None]] = []
        job = self._job_capturing_on_hung(captured)
        started = job.start(lambda _h: None, total=1)
        assert started is True
        stale_on_hung = captured[0]

        # Supersede the run: a newer watchdog now owns the job's identity, so the OLD
        # on_hung must see `self._watchdog is watchdog` as False (the ->exit branch).
        newer = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=30.0, spawn=_noop_spawn, clock=_FakeClock()
        )
        job._watchdog = newer  # pyright: ignore[reportPrivateUsage]

        stale_on_hung(_hung_event())  # the late fire
        # progress.fail still ran (best-effort), but the identity gate refused to
        # touch _running (it belongs to the newer run now).
        assert job._watchdog is newer  # pyright: ignore[reportPrivateUsage]

    def test_on_hung_body_is_best_effort_and_swallows_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the halt alert raises, the whole _on_hung body is swallowed (logged) so
        # the daemon timer can never crash — the except arm of the best-effort wrap.
        def _raise(_exc: BaseException) -> None:
            raise RuntimeError("alert blew up")

        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", _raise)
        captured: list[Callable[[WorkerEvent], None]] = []
        job = self._job_capturing_on_hung(captured)
        started = job.start(lambda _h: None, total=1)
        assert started is True

        captured[0](_hung_event())  # must NOT raise out — swallowed + logged
        # The run is unaffected by the swallowed error (no crash propagated).
        assert job._watchdog is not None  # pyright: ignore[reportPrivateUsage]


def test_run_finally_skips_clearing_running_when_superseded() -> None:
    """``_run``'s finally identity gate: a worker whose watchdog was superseded mid-run
    must NOT clear ``_running`` (the ``self._watchdog is watchdog`` ->exit branch)."""
    captured: list[Callable[[], None]] = []
    newer = PullWatchdog(
        on_hung=lambda _e: None, timeout_seconds=30.0, spawn=_noop_spawn, clock=_FakeClock()
    )

    def factory(_on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
        return PullWatchdog(
            on_hung=_on_hung, timeout_seconds=30.0, spawn=_noop_spawn, clock=_FakeClock()
        )

    job = BackgroundPullJob(
        progress=PullProgress(clock=_FakeClock()),
        spawn=captured.append,  # capture the worker so we can supersede before it runs
        watchdog_factory=factory,
    )
    started = job.start(lambda _h: None, total=1)
    assert started is True
    job._running = True  # pyright: ignore[reportPrivateUsage]
    # Supersede THIS run's watchdog before draining the captured worker, so the
    # worker's finally sees `self._watchdog is watchdog` as False (->exit).
    job._watchdog = newer  # pyright: ignore[reportPrivateUsage]
    captured[0]()  # drain the worker → its finally runs the identity gate
    assert job._running is True  # pyright: ignore[reportPrivateUsage]  # NOT cleared (superseded)


def test_beat_is_a_noop_when_no_watchdog_wired() -> None:
    """``_beat`` short-circuits (the ``self._watchdog is not None`` ->exit branch) when
    no watchdog is wired — e.g. before any ``start`` built one."""
    job = BackgroundPullJob(progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn)
    assert job._watchdog is None  # pyright: ignore[reportPrivateUsage]
    job._beat()  # pyright: ignore[reportPrivateUsage]  # must not raise — the None guard
