"""Unit suite for :mod:`brutex.engine.sweep_singleflight` (G6/G10 — DEC-SWEEP-UNKILLABLE-CORE-001).

TWO-PROCESS REAL-flock coverage: a peer process genuinely holds the lock while
this process probes (G6 exactly-one-proceeds, SIGKILL kernel release, the
simultaneous race) and the G10 SH-vs-EX matrix runs in BOTH directions against
the REAL pull single-flight lock — including the D-13 pinned "peer pull"
refusal wording debt (DEC-SWEEP-LOCK-WORDING-001).
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Final

import pytest
from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    pull_singleflight_lock,
    pull_singleflight_lock_path,
)
from brutex.engine.sweep_singleflight import (
    SWEEP_SINGLEFLIGHT_LOCK_NAME,
    LakeBusyError,
    SweepAlreadyRunningError,
    lake_read_guard,
    sweep_run_lock,
    sweep_run_lock_path,
)

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g6_lock_path_is_stable_sidecar": ("G6",),
    "test_g6_two_process_exactly_one_proceeds": ("G6",),
    "test_g6_sigkill_releases_lock": ("G6",),
    "test_g6_simultaneous_race_exactly_one_winner": ("G6",),
    "test_g6_same_process_second_acquire_refused": ("G6",),
    "test_g6_holder_pid_unreadable_is_none": ("G6",),
    "test_g6_holder_pid_junk_is_none": ("G6",),
    "test_g6_holder_pid_read_oserror_is_none": ("G6",),
    "test_g6_pid_advertise_failure_is_soft": ("G6",),
    "test_g6_released_on_body_error": ("G6",),
    "test_g6_distinct_results_roots_never_contend": ("G6",),
    "test_g10_guard_refused_while_pull_holds_ex_two_process": ("G10",),
    "test_g10_pull_refused_while_sweep_holds_sh_two_process_d13_wording": ("G10",),
    "test_g10_shared_guards_compose": ("G10",),
    "test_g10_bounded_hold_released_after_block": ("G10",),
    "test_g10_guard_uses_the_existing_pull_lock_file": ("G10",),
    "test_g10_lake_busy_wording_pinned": ("G10",),
    "test_g10_in_process_pull_ex_refuses_guard": ("G10",),
}

_CHILD_HOLD_SWEEP: Final[str] = """
import sys, time
from pathlib import Path
from brutex.engine.sweep_singleflight import sweep_run_lock
with sweep_run_lock(Path(sys.argv[1])):
    print("HELD", flush=True)
    time.sleep(120)
"""

_CHILD_HOLD_PULL: Final[str] = """
import sys, time
from pathlib import Path
from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock
with pull_singleflight_lock(Path(sys.argv[1])):
    print("HELD", flush=True)
    time.sleep(120)
"""

_CHILD_TRY_PULL: Final[str] = """
import sys
from pathlib import Path
from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    pull_singleflight_lock,
)
try:
    with pull_singleflight_lock(Path(sys.argv[1])):
        print("ACQUIRED", flush=True)
except PullAlreadyRunningError as exc:
    print("REFUSED::" + str(exc), flush=True)
"""

_CHILD_RACE_SWEEP: Final[str] = """
import sys, time
from pathlib import Path
from brutex.engine.sweep_singleflight import SweepAlreadyRunningError, sweep_run_lock
root, go = Path(sys.argv[1]), Path(sys.argv[2])
while not go.exists():
    time.sleep(0.005)
try:
    with sweep_run_lock(root):
        print("WON", flush=True)
        time.sleep(120)
except SweepAlreadyRunningError:
    print("LOST", flush=True)
"""


def _spawn(script: str, *args: str) -> subprocess.Popen[str]:
    """Spawn a real peer process running *script* with *args*."""
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_line(stream: IO[str] | None) -> str:
    """Read one line from a child's stdout (blocking; the child prints promptly)."""
    assert stream is not None
    return stream.readline().strip()


def _kill(proc: subprocess.Popen[str]) -> None:
    """SIGKILL + reap a child + close its pipes (idempotent — safe in finally)."""
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def test_g6_lock_path_is_stable_sidecar(tmp_path: Path) -> None:
    """The lock is a stable dot-prefixed sidecar under the results root."""
    assert sweep_run_lock_path(tmp_path) == tmp_path / ".locks" / "sweep_singleflight.lock"
    assert SWEEP_SINGLEFLIGHT_LOCK_NAME == "sweep_singleflight.lock"


def test_g6_two_process_exactly_one_proceeds(tmp_path: Path) -> None:
    """While a REAL peer process holds the lock, our acquire is refused loudly.

    The refusal names the lock path AND the holder's advertised pid (the
    child's real pid), then — after the peer dies — the lock is acquirable.
    """
    child = _spawn(_CHILD_HOLD_SWEEP, str(tmp_path))
    try:
        assert _read_line(child.stdout) == "HELD"
        with pytest.raises(SweepAlreadyRunningError) as excinfo, sweep_run_lock(tmp_path):
            pytest.fail("second sweep must not proceed while the peer holds the lock")
        err = excinfo.value
        assert err.lock_path == sweep_run_lock_path(tmp_path)
        assert err.holder_pid == child.pid
        assert "a sweep is already running" in str(err)
        assert f"holder pid {child.pid}" in str(err)
        assert str(sweep_run_lock_path(tmp_path)) in str(err)
    finally:
        _kill(child)


def test_g6_sigkill_releases_lock(tmp_path: Path) -> None:
    """SIGKILLing the holder releases the flock (kernel law — no stale lock)."""
    child = _spawn(_CHILD_HOLD_SWEEP, str(tmp_path))
    try:
        assert _read_line(child.stdout) == "HELD"
        with pytest.raises(SweepAlreadyRunningError), sweep_run_lock(tmp_path):
            pytest.fail("must be refused while the peer lives")
    finally:
        os.kill(child.pid, signal.SIGKILL)
        _kill(child)
    acquired = False
    with sweep_run_lock(tmp_path):
        acquired = True
    assert acquired is True


def test_g6_simultaneous_race_exactly_one_winner(tmp_path: Path) -> None:
    """Two peers racing the same results root: EXACTLY one wins, one is refused."""
    go = tmp_path / "go"
    a = _spawn(_CHILD_RACE_SWEEP, str(tmp_path), str(go))
    b = _spawn(_CHILD_RACE_SWEEP, str(tmp_path), str(go))
    try:
        time.sleep(0.3)  # both children are polling for the go file
        go.write_text("go", encoding="utf-8")
        outcomes = sorted([_read_line(a.stdout), _read_line(b.stdout)])
        assert outcomes == ["LOST", "WON"]
    finally:
        _kill(a)
        _kill(b)


def test_g6_same_process_second_acquire_refused(tmp_path: Path) -> None:
    """A second acquire on a NEW fd contends even within one process (flock law)."""
    with sweep_run_lock(tmp_path):
        with pytest.raises(SweepAlreadyRunningError) as excinfo, sweep_run_lock(tmp_path):
            pytest.fail("nested acquire must be refused")
        assert excinfo.value.holder_pid == os.getpid()


def test_g6_holder_pid_unreadable_is_none(tmp_path: Path) -> None:
    """Contention against a holder that advertised NO pid reports holder_pid=None."""
    lock_path = sweep_run_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raw holder — empty lock file
        with pytest.raises(SweepAlreadyRunningError) as excinfo, sweep_run_lock(tmp_path):
            pytest.fail("must be refused")
        assert excinfo.value.holder_pid is None
        assert "holder pid" not in str(excinfo.value)
    finally:
        os.close(fd)


def test_g6_holder_pid_junk_is_none(tmp_path: Path) -> None:
    """Junk bytes in the lock file parse to holder_pid=None (never a crash)."""
    lock_path = sweep_run_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not-a-pid", encoding="utf-8")
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SweepAlreadyRunningError) as excinfo, sweep_run_lock(tmp_path):
            pytest.fail("must be refused")
        assert excinfo.value.holder_pid is None
    finally:
        os.close(fd)


def test_g6_holder_pid_read_oserror_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable lock file (OSError on read) degrades to holder_pid=None."""
    lock_path = sweep_run_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def _boom(*args: object, **kwargs: object) -> str:
            raise OSError("probe failure")

        monkeypatch.setattr(Path, "read_text", _boom)
        with pytest.raises(SweepAlreadyRunningError) as excinfo, sweep_run_lock(tmp_path):
            pytest.fail("must be refused")
        assert excinfo.value.holder_pid is None
    finally:
        os.close(fd)


def test_g6_pid_advertise_failure_is_soft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed pid advertisement never blocks the run — the lock is still held."""

    def _boom(fd: int, length: int) -> None:
        raise OSError("write refused")

    monkeypatch.setattr(os, "ftruncate", _boom)
    entered = False
    with sweep_run_lock(tmp_path):
        entered = True
        # The lock IS held despite the advertisement failure: a fresh probe fd
        # cannot take it.
        probe = os.open(sweep_run_lock_path(tmp_path), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    assert entered is True


def test_g6_released_on_body_error(tmp_path: Path) -> None:
    """An exception inside the with-body releases the lock on the way out."""
    with pytest.raises(RuntimeError, match="body boom"), sweep_run_lock(tmp_path):
        raise RuntimeError("body boom")
    with sweep_run_lock(tmp_path):
        pass  # re-acquirable — the fd was closed on the error path


def test_g6_distinct_results_roots_never_contend(tmp_path: Path) -> None:
    """Two different results roots key different lock files — no contention."""
    with sweep_run_lock(tmp_path / "root_a"), sweep_run_lock(tmp_path / "root_b"):
        pass


def test_g10_guard_refused_while_pull_holds_ex_two_process(tmp_path: Path) -> None:
    """A REAL peer pull (EX holder) makes the sweep's SH read-guard refuse loudly."""
    child = _spawn(_CHILD_HOLD_PULL, str(tmp_path))
    try:
        assert _read_line(child.stdout) == "HELD"
        with pytest.raises(LakeBusyError) as excinfo, lake_read_guard(tmp_path):
            pytest.fail("preload must not proceed while a pull holds the lake")
        assert excinfo.value.lock_path == pull_singleflight_lock_path(tmp_path)
        assert "a pull/heal is running" in str(excinfo.value)
    finally:
        _kill(child)


def test_g10_pull_refused_while_sweep_holds_sh_two_process_d13_wording(
    tmp_path: Path,
) -> None:
    """While the sweep holds SH, a starting peer pull is refused — D-13 wording pin.

    The pull's refusal STILL says "a pull is already running" (the peer-pull
    wording of ``PullAlreadyRunningError``) even though the holder is a SWEEP
    — misleading but SAFE. Pinned as explicit wording debt; follow-up
    DEC-SWEEP-LOCK-WORDING-001. If this pin breaks because the pull side
    learned to name a sweep holder, retire the follow-up with it.
    """
    with lake_read_guard(tmp_path):
        child = _spawn(_CHILD_TRY_PULL, str(tmp_path))
        try:
            line = _read_line(child.stdout)
        finally:
            _kill(child)
    assert line.startswith("REFUSED::")
    assert "a pull is already running" in line  # the D-13 misleading-but-safe wording
    assert "ACQUIRED" not in line


def test_g10_shared_guards_compose(tmp_path: Path) -> None:
    """Two concurrent SH read-guards never contend (shared/shared composes)."""
    with lake_read_guard(tmp_path), lake_read_guard(tmp_path):
        pass


def test_g10_bounded_hold_released_after_block(tmp_path: Path) -> None:
    """The guard is held across the block ONLY — a pull acquires right after."""
    with (
        lake_read_guard(tmp_path),
        pytest.raises(PullAlreadyRunningError),
        pull_singleflight_lock(tmp_path),
    ):
        pytest.fail("pull must be refused while the guard is held")
    with pull_singleflight_lock(tmp_path):
        pass  # released — the preload-bounded hold is over


def test_g10_guard_uses_the_existing_pull_lock_file(tmp_path: Path) -> None:
    """The guard locks the EXISTING pull single-flight file (zero pull edits)."""
    expected = tmp_path / ".locks" / "pull_singleflight.lock"
    assert pull_singleflight_lock_path(tmp_path) == expected
    with lake_read_guard(tmp_path):
        assert expected.exists()


def test_g10_lake_busy_wording_pinned(tmp_path: Path) -> None:
    """The LakeBusyError message carries the actionable operator instruction."""
    err = LakeBusyError(pull_singleflight_lock_path(tmp_path))
    message = str(err)
    assert "a pull/heal is running against this lake" in message
    assert "wait for it to finish or stop it" in message
    assert str(pull_singleflight_lock_path(tmp_path)) in message


def test_g10_in_process_pull_ex_refuses_guard(tmp_path: Path) -> None:
    """The in-process EX-vs-SH direction (second fd) matches the two-process law."""
    with (
        pull_singleflight_lock(tmp_path),
        pytest.raises(LakeBusyError),
        lake_read_guard(tmp_path),
    ):
        pytest.fail("guard must be refused while a pull holds EX")
