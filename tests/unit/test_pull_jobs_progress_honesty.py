"""DEC-PULL-PROGRESS-HONESTY-001 — BackgroundPullJob honesty seams.

Fails-before framing (the old illusion, forensically verified on origin/main
``23dbf08``): the pipeline's F&O historical phase ran
``set_phase("historical", pair_progress=True)`` so every per-pair tick advanced
``grow_total=True`` → ``total`` tracked ``completed`` → **percent read a fabricated
100.0 the entire phase** while thousands of ledger windows were still pending; and
because the async per-pair reports carry 0 bars, the bars headline FROZE at the spot
phase's count. The new path: ``set_phase("historical", open_ended=True)`` + absolute
market-filtered ledger syncs + the ``add_bars`` live-delta funnel.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from brutex.dashboard import pull_jobs
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.pull_progress import STATE_DONE, PullProgress


@pytest.fixture(autouse=True)
def _disable_account_singleflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the account-wide cross-process pull lock (same as test_pull_jobs)."""
    monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)


def _ticking_clock() -> Callable[[], float]:
    """Deterministic monotonically-increasing injected clock (team playbook shape)."""
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    return clock


class _SpyCache:
    """Minimal summary-cache double recording ``mark_dirty`` calls."""

    def __init__(self) -> None:
        self.dirty_calls = 0

    def mark_dirty(self) -> None:
        self.dirty_calls += 1


def test_old_illusion_percent_pinned_at_100_is_gone_on_the_new_path() -> None:
    """Fails-before: pair_progress ticks read percent==100 with windows pending.

    The regression drives the NEW F&O wiring shape end-to-end at the job layer:
    the phase declares itself open-ended and the throttled ledger poll syncs the
    ABSOLUTE market-filtered counts — the snapshot now reads the honest 25% +
    ``open_ended`` instead of a pinned 100.
    """
    job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=lambda fn: fn())

    def work(on_progress: ProgressHook) -> None:
        job.sync_from_ledger(completed=45, total=45, bars=None)  # metadata settles 45/45
        job.set_phase("historical", open_ended=True)
        # Per-pair ticks now only refresh the label (no pair_progress) …
        on_progress("NSE-NIFTY-25Jun26-FUT 1minute", 0, False, False)
        # … and the ledger poll drives the REAL denominator (5 of 20 F&O windows).
        job.sync_from_ledger(completed=5, total=20, bars=None, current="NIFTY FUT 1minute")
        snap = job.snapshot()
        assert snap.percent == 25.0  # the fabricated 100.0 is DEAD
        assert snap.open_ended is True
        assert snap.eta_seconds is None  # never a linear ETA over a growing total
        assert snap.current == "NIFTY FUT 1minute"

    assert job.start(work, total=45, ledger_synced=True) is True
    assert job.snapshot().state == STATE_DONE


def test_add_bars_funnels_progress_cache_and_watchdog() -> None:
    """``add_bars`` = progress tally + mark_dirty + a stall-watchdog beat (real progress)."""
    cache = _SpyCache()
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=lambda fn: fn(),
        summary_cache=cache,
    )

    def work(on_progress: ProgressHook) -> None:
        del on_progress
        job.add_bars(375)
        job.add_bars(42)
        snap = job.snapshot()
        assert snap.bars == 417
        assert snap.completed == 0  # bars are NOT units

    assert job.start(work, total=1, ledger_synced=True) is True
    assert cache.dirty_calls >= 2  # one funnel tick per folded window


def test_add_bars_without_cache_is_safe() -> None:
    job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=lambda fn: fn())

    def work(on_progress: ProgressHook) -> None:
        del on_progress
        job.add_bars(7)
        assert job.snapshot().bars == 7

    assert job.start(work, total=1) is True


def test_frozen_bars_counter_regression_async_shape() -> None:
    """Fails-before: async F&O per-pair reports carry 0 bars → the headline froze.

    Simulates the async split at the job layer: per-pair ticks add 0 bars (the
    producer-resolved reports) while the pipeline's ``on_bars`` fold-deltas land
    through ``add_bars`` — the counter must advance by EXACTLY the folded deltas,
    each once (no double count with the 0-bar pair ticks).
    """
    job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=lambda fn: fn())
    folded = [375, 0, 42]

    def work(on_progress: ProgressHook) -> None:
        job.set_phase("historical", open_ended=True)
        for delta in folded:
            on_progress("pair 1minute", 0, False, False)  # honest async 0
            job.add_bars(delta)  # the pipeline fold's real delta
        assert job.snapshot().bars == sum(folded)

    assert job.start(work, total=3, ledger_synced=True) is True


def test_set_phase_open_ended_defaults_off_and_sticky() -> None:
    job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=lambda fn: fn())

    def work(on_progress: ProgressHook) -> None:
        del on_progress
        job.set_phase("spot")
        assert job.snapshot().open_ended is False
        job.set_phase("historical", open_ended=True)
        job.set_phase("prebuild")
        job.set_phase("movers")
        # PINNED: the later phases keep the honest "total unknown" marker.
        assert job.snapshot().open_ended is True

    assert job.start(work, total=2, ledger_synced=True) is True


def test_add_bars_is_thread_safe_under_concurrent_folds() -> None:
    """The consumer threads fold concurrently — the tally must never tear."""
    progress = PullProgress(clock=_ticking_clock())
    job = BackgroundPullJob(progress=progress, spawn=lambda fn: fn())

    def work(on_progress: ProgressHook) -> None:
        del on_progress
        threads = [
            threading.Thread(target=lambda: [job.add_bars(1) for _ in range(200)]) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert job.snapshot().bars == 800

    assert job.start(work, total=1) is True
