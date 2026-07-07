"""Sweep-vs-pull exclusion wiring in ``BackgroundPullJob._acquire_singleflight``.

DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 (§8 pull-side protocol, extreme test 9's
dashboard half): the dashboard pull site now (0) PRE-probes the sweep lock
(``check_pull_exclusion`` — write-free) before taking its own locks, and
(3) RE-probes after BOTH pull locks are held (the TOCTOU close) — a sweep
that slipped in mid-acquire wins, the pull rolls its locks back LIFO and
refuses with the honest sweep-exclusion message as the job's error state
(one ``pull.sweep_exclusion_refused`` structlog per refusal, emitted inside
the probe). Real ``fcntl.flock``-held sweep locks in tmp lakes — never a
mocked verdict.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from brutex.dashboard.pull_jobs import BackgroundPullJob
from brutex.dashboard.pull_progress import STATE_DONE, STATE_ERROR, PullProgress
from brutex.data.sweep_exclusion import sweep_lock_path
from structlog.testing import capture_logs


def _ticking_clock() -> Callable[[], float]:
    """A deterministic, monotonically-increasing injected clock (playbook shape)."""
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    return clock


def _sync_spawn(fn: Callable[[], None]) -> None:
    fn()


@contextmanager
def _hold_sweep_lock(lake: Path) -> Generator[None]:
    """Hold a REAL sweep ``LOCK_SH`` on ``<lake>/.locks/sweep.lock`` (a running sweep)."""
    path = sweep_lock_path(lake)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class _OrderedGuard(AbstractContextManager[None]):
    """A guard double appending named enter/exit events to a SHARED order log."""

    def __init__(self, name: str, order: list[str]) -> None:
        self._name = name
        self._order = order

    def __enter__(self) -> None:
        self._order.append(f"enter:{self._name}")

    def __exit__(self, *exc: object) -> None:
        self._order.append(f"exit:{self._name}")


class _SweepSlipsInGuard(AbstractContextManager[None]):
    """A lake-root guard whose acquire coincides with a sweep taking its lock.

    ``__enter__`` grabs a REAL shared flock on the lake's sweep lock —
    simulating a sweep that started between the pull's PRE-probe and its lock
    acquires — so the pull's post-acquire RE-probe must catch it (TOCTOU).
    """

    def __init__(self, lake: Path, order: list[str]) -> None:
        self._lake = lake
        self._order = order
        self._fd: int | None = None

    def __enter__(self) -> None:
        self._order.append("enter:lake")
        path = sweep_lock_path(self._lake)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_SH | fcntl.LOCK_NB)

    def __exit__(self, *exc: object) -> None:
        self._order.append("exit:lake")
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _job(
    lake: Path,
    *,
    spawn: Callable[[Callable[[], None]], None],
    lake_guard: AbstractContextManager[None],
    account_guard: AbstractContextManager[None],
) -> BackgroundPullJob:
    return BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=spawn,
        lake_root_resolver=lambda: lake,
        singleflight_factory=lambda _root: lake_guard,
        account_singleflight_factory=lambda _key: account_guard,
        account_key_resolver=lambda: "key-A",
    )


def test_pre_probe_refuses_before_any_lock_is_taken(tmp_path: Path) -> None:
    """A running sweep refuses the pull FAST — neither pull lock is consulted."""
    order: list[str] = []
    spawned: list[Callable[[], None]] = []
    job = _job(
        tmp_path,
        spawn=spawned.append,
        lake_guard=_OrderedGuard("lake", order),
        account_guard=_OrderedGuard("account", order),
    )
    with _hold_sweep_lock(tmp_path), capture_logs() as logs:
        started = job.start(lambda _h: None, total=1)
    assert started is False
    assert spawned == []  # NO work spawned
    assert order == []  # neither pull lock was even consulted
    assert job.running is False  # in-process flag rolled back
    snap = job.snapshot()
    assert snap.state == STATE_ERROR
    assert "SweepRunningError" in snap.detail
    assert "a sweep is running for this lake" in snap.detail
    assert "sweep-vs-pull exclusion" in snap.detail
    events = [entry for entry in logs if entry["event"] == "pull.sweep_exclusion_refused"]
    assert len(events) == 1  # ONE structlog per refusal, from the probe itself


def test_post_acquire_re_probe_rolls_back_lifo_and_refuses(tmp_path: Path) -> None:
    """The TOCTOU close: a sweep slipping in mid-acquire wins; the pull rolls back."""
    order: list[str] = []
    spawned: list[Callable[[], None]] = []
    job = _job(
        tmp_path,
        spawn=spawned.append,
        lake_guard=_SweepSlipsInGuard(tmp_path, order),
        account_guard=_OrderedGuard("account", order),
    )
    with capture_logs() as logs:
        started = job.start(lambda _h: None, total=1)
    assert started is False
    assert spawned == []
    # LIFO rollback: account entered first, lake second; lake exits first.
    assert order == ["enter:account", "enter:lake", "exit:lake", "exit:account"]
    assert job.running is False
    snap = job.snapshot()
    assert snap.state == STATE_ERROR
    assert "SweepRunningError" in snap.detail
    events = [entry for entry in logs if entry["event"] == "pull.sweep_exclusion_refused"]
    assert len(events) == 1


def test_no_sweep_lock_happy_path_probes_pass(tmp_path: Path) -> None:
    """No sweep running ⇒ both probes pass; the pull runs + releases normally."""
    order: list[str] = []
    job = _job(
        tmp_path,
        spawn=_sync_spawn,
        lake_guard=_OrderedGuard("lake", order),
        account_guard=_OrderedGuard("account", order),
    )
    with capture_logs() as logs:
        started = job.start(lambda on_progress: on_progress("X", 1, False, False), total=1)
    assert started is True
    assert job.snapshot().state == STATE_DONE
    assert order == ["enter:account", "enter:lake", "exit:lake", "exit:account"]
    assert not any(entry["event"] == "pull.sweep_exclusion_refused" for entry in logs)


def test_refused_pull_can_start_again_after_the_sweep_finishes(tmp_path: Path) -> None:
    """The refusal is transient: once the sweep lock is released, a pull starts."""
    order: list[str] = []
    job = _job(
        tmp_path,
        spawn=_sync_spawn,
        lake_guard=_OrderedGuard("lake", order),
        account_guard=_OrderedGuard("account", order),
    )
    with _hold_sweep_lock(tmp_path):
        assert job.start(lambda _h: None, total=1) is False
    # The sweep lock is now released (fd closed) — the SAME job starts cleanly.
    assert job.start(lambda on_progress: on_progress("X", 1, False, False), total=1) is True
    assert job.snapshot().state == STATE_DONE


def test_probe_is_write_free_on_refusal(tmp_path: Path) -> None:
    """The pull-side probes never create lock files of their own (write-free)."""
    order: list[str] = []
    spawned: list[Callable[[], None]] = []
    job = _job(
        tmp_path,
        spawn=spawned.append,
        lake_guard=_OrderedGuard("lake", order),
        account_guard=_OrderedGuard("account", order),
    )
    with _hold_sweep_lock(tmp_path):
        before = sorted(str(p) for p in tmp_path.rglob("*"))
        assert job.start(lambda _h: None, total=1) is False
        after = sorted(str(p) for p in tmp_path.rglob("*"))
    assert after == before  # only the sweep's own lock file exists — nothing added


def test_none_lake_root_skips_the_sweep_probes(tmp_path: Path) -> None:
    """A ``None`` lake root (dry-run/test) disables the guards AND the sweep probes."""
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: None,
    )
    # Even with a sweep lock held SOMEWHERE, a guard-disabled job is unaffected.
    with _hold_sweep_lock(tmp_path):
        started = job.start(lambda on_progress: on_progress("X", 1, False, False), total=1)
    assert started is True
    assert job.snapshot().state == STATE_DONE
