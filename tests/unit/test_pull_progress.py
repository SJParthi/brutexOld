"""Tests for `brutex.dashboard.pull_progress` — the live pull-progress tracker.

100% line+branch: the state machine (start → advance → finish/fail), the O(1)
counters (completed clamp, bars accumulation, failed tally, current label), the
``percent`` zero-total + normal branches, the snapshot/`to_dict` projection, the
reusable re-start reset, the default monotonic clock, and thread-safety under
concurrent advances.
"""

from __future__ import annotations

import threading

from brutex.dashboard.pull_progress import (
    STATE_DONE,
    STATE_ERROR,
    STATE_IDLE,
    STATE_RUNNING,
    ProgressSnapshot,
    PullProgress,
)


class _FakeClock:
    """A deterministic, monotonically-increasing injected clock."""

    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        self._t += 1.0
        return self._t


def test_initial_state_is_idle() -> None:
    p = PullProgress(clock=_FakeClock())
    snap = p.snapshot()
    assert snap.state == STATE_IDLE
    assert snap.total == 0
    assert snap.completed == 0
    assert snap.percent == 0.0  # zero-total branch
    assert snap.running is False


def test_start_sets_running_and_total() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=4, detail="kick")
    snap = p.snapshot()
    assert snap.state == STATE_RUNNING
    assert snap.total == 4
    assert snap.detail == "kick"
    assert snap.running is True
    assert snap.percent == 0.0  # nothing completed yet (total>0 branch)


def test_negative_total_clamped_to_zero() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=-5)
    assert p.snapshot().total == 0


def test_advance_counts_and_percent() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=4)
    p.advance(current="NIFTY", bars=100)
    p.advance(current="BANKNIFTY", bars=50, failed=True)
    snap = p.snapshot()
    assert snap.completed == 2
    assert snap.bars == 150
    assert snap.failed == 1
    assert snap.current == "BANKNIFTY"
    assert snap.percent == 50.0  # 2/4


def test_advance_without_current_keeps_prior_label() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=2)
    p.advance(current="NIFTY", bars=10)
    p.advance(bars=5)  # no current → label unchanged (the `if current` False branch)
    snap = p.snapshot()
    assert snap.current == "NIFTY"
    assert snap.bars == 15
    assert snap.failed == 0  # the `if failed` False branch


def test_negative_bars_ignored() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=1)
    p.advance(bars=-99)
    assert p.snapshot().bars == 0


def test_completed_clamps_to_total() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=1)
    p.advance()
    p.advance()  # second advance would exceed total → clamped
    snap = p.snapshot()
    assert snap.completed == 1
    assert snap.percent == 100.0


def test_finish_marks_done() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=2)
    p.advance()
    p.finish(detail="all good")
    snap = p.snapshot()
    assert snap.state == STATE_DONE
    assert snap.detail == "all good"
    assert snap.running is False


def test_fail_marks_error() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=2)
    p.fail("DhanError: token expired")
    snap = p.snapshot()
    assert snap.state == STATE_ERROR
    assert snap.detail == "DhanError: token expired"


def test_restart_resets_counters() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=3)
    p.advance(current="X", bars=7, failed=True)
    p.finish()
    # Re-click → a fresh run on the SAME instance resets everything.
    p.start(total=2)
    snap = p.snapshot()
    assert snap.state == STATE_RUNNING
    assert (snap.total, snap.completed, snap.failed, snap.bars) == (2, 0, 0, 0)
    assert snap.current == ""


def test_updated_at_advances_with_clock() -> None:
    clock = _FakeClock()
    p = PullProgress(clock=clock)
    p.start(total=1)
    first = p.snapshot().updated_at
    p.advance()
    assert p.snapshot().updated_at > first


def test_to_dict_shape() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=2)
    p.advance(current="NIFTY", bars=10)
    d = p.snapshot().to_dict()
    assert d == {
        "state": STATE_RUNNING,
        "total": 2,
        "completed": 1,
        "failed": 0,
        "bars": 10,
        "current": "NIFTY",
        "detail": "",
        "percent": 50.0,
        "running": True,
        "updated_at": d["updated_at"],
    }
    assert isinstance(d["updated_at"], float)


def test_default_monotonic_clock() -> None:
    # No injected clock → exercises the module-level _monotonic default.
    p = PullProgress()
    p.start(total=1)
    assert p.snapshot().updated_at > 0.0


def test_thread_safe_concurrent_advances() -> None:
    p = PullProgress(clock=_FakeClock())
    p.start(total=1000)

    def worker() -> None:
        for _ in range(100):
            p.advance(bars=1)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = p.snapshot()
    # 10 threads × 100 advances = 1000 — no lost updates under the lock.
    assert snap.completed == 1000
    assert snap.bars == 1000


def test_snapshot_is_immutable() -> None:
    snap = PullProgress(clock=_FakeClock()).snapshot()
    assert isinstance(snap, ProgressSnapshot)
    try:
        snap.total = 9  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover - frozen dataclass must reject assignment
        raise AssertionError("ProgressSnapshot must be frozen")
