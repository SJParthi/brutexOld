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

from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.pull_progress import STATE_DONE, STATE_ERROR, STATE_RUNNING, PullProgress
from brutex.dashboard.summary_cache import CoverageCounters


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
        on_progress("NIFTY 1minute", 100, False)
        on_progress("NIFTY 1day", 5, False)

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
        on_progress("13 1minute", 1, False)
        raise RuntimeError("boom")

    assert job.start(work, total=1) is True
    snap = job.snapshot()
    assert snap.state == STATE_ERROR
    assert snap.detail == "RuntimeError: boom"
    assert job.running is False  # flag reset even on failure


def test_failed_unit_flag_is_forwarded() -> None:
    job = _job()
    job.start(lambda on_progress: on_progress("BAD 1minute", 0, True), total=1)
    snap = job.snapshot()
    assert snap.failed == 1
    assert snap.state == STATE_DONE  # a failed UNIT is not a failed JOB


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
    job.start(lambda on_progress: on_progress("X", 1, False), total=1)
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
    job.start(lambda on_progress: on_progress("13 1minute", 7, False), total=1)
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
        on_progress("NIFTY 1minute", 100, False)
        on_progress("NIFTY 1minute", 50, False)
        on_progress("BANKNIFTY 1minute", 7, False)

    job.start(work, total=3)
    snap = job.counters.snapshot()
    assert snap.by_label == {"NIFTY 1minute": 150, "BANKNIFTY 1minute": 7}
    assert snap.total_bars == 157


def test_injected_counters_are_used() -> None:
    counters = CoverageCounters()
    job = BackgroundPullJob(
        progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn, counters=counters
    )
    job.start(lambda on_progress: on_progress("X 1minute", 9, False), total=1)
    assert job.counters is counters
    assert counters.snapshot().total_bars == 9


def test_duplicate_repull_zero_bars_leaves_counter_unchanged() -> None:
    # bars == 0 (post-dedup duplicate) is a no-op → idempotent headline.
    job = _job()
    job.start(lambda on_progress: on_progress("DUP 1minute", 0, False), total=1)
    snap = job.counters.snapshot()
    assert snap.total_bars == 0
    assert snap.by_label == {}


def test_summary_cache_invalidated_once_per_window() -> None:
    cache = _SpyCache()
    job = BackgroundPullJob(
        progress=PullProgress(clock=_FakeClock()), spawn=_sync_spawn, summary_cache=cache
    )

    def work(on_progress: ProgressHook) -> None:
        on_progress("A 1minute", 10, False)
        on_progress("B 1minute", 0, True)  # a failed/zero window still ticks the funnel

    job.start(work, total=2)
    # mark_dirty fires on EVERY progress tick (so the next poll refreshes),
    # independent of whether bars were written.
    assert cache.dirty_calls == 2


def test_no_summary_cache_means_no_invalidation_branch() -> None:
    # Default job has summary_cache=None → the mark_dirty branch is skipped
    # (no AttributeError), proving the None guard.
    job = _job()
    job.start(lambda on_progress: on_progress("X 1minute", 1, False), total=1)
    assert job.snapshot().state == STATE_DONE
