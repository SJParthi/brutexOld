"""Sweep single-flight lock + holder-note seams (hostile-review MAJOR-2 fix).

The sweep twin of ``test_pull_singleflight_nightly.py``: the NEW sweep lock beside
the pull/nightly locks (two overlapping ``sweep-all`` runs over one output root are
mechanically impossible — pinned by a REAL two-process test AND a ``kill -9`` release
test), reusing the ONE ``_flock_singleflight`` core (existing callers byte-identical).
This is the lock-level proof that the concurrent-sweep refusal claim is TRUE (a real
cross-process advisory flock), not merely disclosed-false.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from brutex.adapters.groww.pull_singleflight import (
    SWEEP_SINGLEFLIGHT_LOCK_NAME,
    NightlyAlreadyRunningError,
    PullAlreadyRunningError,
    SweepAlreadyRunningError,
    nightly_singleflight_lock,
    pull_singleflight_lock,
    pull_singleflight_lock_path,
    sweep_singleflight_lock,
    sweep_singleflight_lock_path,
)


def test_sweep_lock_path_is_stable_sidecar_beside_pull_lock(tmp_path: Path) -> None:
    """The sweep lock lives at ``<out_root>/.locks/sweep_singleflight.lock`` — never created here."""
    path = sweep_singleflight_lock_path(tmp_path)
    assert path == tmp_path / ".locks" / SWEEP_SINGLEFLIGHT_LOCK_NAME
    assert path.parent == pull_singleflight_lock_path(tmp_path).parent
    assert path != pull_singleflight_lock_path(tmp_path)
    assert not path.exists()


def test_sweep_pull_nightly_locks_never_contend(tmp_path: Path) -> None:
    """Distinct lock files: the sweep, pull, and nightly locks can be held together."""
    with (
        sweep_singleflight_lock(tmp_path),
        pull_singleflight_lock(tmp_path),
        nightly_singleflight_lock(tmp_path),
    ):
        pass  # all three held at once — no contention across the three names


def test_sweep_second_acquire_while_held_is_refused(tmp_path: Path) -> None:
    """Same-process second fd contention raises the sweep-specific error."""
    with (
        pull_singleflight_lock(tmp_path),  # the PULL lock never blocks the sweep lock
        sweep_singleflight_lock(tmp_path),
        pytest.raises(SweepAlreadyRunningError) as excinfo,
        sweep_singleflight_lock(tmp_path),  # pragma: no cover — __enter__ raises first
    ):
        pass  # pragma: no cover — body never runs (the inner acquire raises)
    assert excinfo.value.lock_path == sweep_singleflight_lock_path(tmp_path)
    assert "another sweep is already running on this output dir" in str(excinfo.value)


def test_different_output_roots_never_contend(tmp_path: Path) -> None:
    """Two sweeps over DIFFERENT output roots hold their own locks at once (per-root granularity)."""
    a = tmp_path / "run-a"
    b = tmp_path / "run-b"
    with sweep_singleflight_lock(a), sweep_singleflight_lock(b):
        pass  # distinct output roots → distinct lock files → no contention


def test_sweep_release_lets_it_reacquire(tmp_path: Path) -> None:
    with sweep_singleflight_lock(tmp_path):
        pass
    with sweep_singleflight_lock(tmp_path):
        pass  # re-acquired without raising — the release worked


def test_holder_note_written_and_echoed_in_sweep_refusal(tmp_path: Path) -> None:
    """A held sweep lock's note is echoed in the contended peer's refusal message."""
    with (
        sweep_singleflight_lock(tmp_path, holder_note="sweep-all:grid-abc"),
        pytest.raises(SweepAlreadyRunningError) as excinfo,
        sweep_singleflight_lock(tmp_path),  # pragma: no cover — acquire raises
    ):
        pass  # pragma: no cover — body never runs
    assert "(holder: sweep-all:grid-abc)" in str(excinfo.value)


def test_no_note_keeps_sweep_refusal_message_stable(tmp_path: Path) -> None:
    with (
        sweep_singleflight_lock(tmp_path),
        pytest.raises(SweepAlreadyRunningError) as excinfo,
        sweep_singleflight_lock(tmp_path),  # pragma: no cover — acquire raises
    ):
        pass  # pragma: no cover — body never runs
    msg = str(excinfo.value)
    assert "(holder:" not in msg  # no note → no holder suffix
    assert "corrupt the shared resume checkpoint" in msg


def test_sweep_error_is_not_a_pull_or_nightly_error(tmp_path: Path) -> None:
    """``SweepAlreadyRunningError`` is its own type — a caller catching only it never
    swallows a genuine pull/nightly refusal (and vice versa)."""
    err = SweepAlreadyRunningError(sweep_singleflight_lock_path(tmp_path))
    assert not isinstance(err, (PullAlreadyRunningError, NightlyAlreadyRunningError))
    assert err.lock_path == sweep_singleflight_lock_path(tmp_path)


def _child_holds_sweep_lock_src() -> str:
    """A minimal child that takes the sweep flock directly (raw fcntl, no brutex import
    — fast startup, same mechanics as the production ``_flock_singleflight`` core)."""
    return textwrap.dedent(
        """
        import fcntl, os, sys
        from pathlib import Path

        lock = Path(sys.argv[1]) / ".locks" / "sweep_singleflight.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, b"sweep-all:peer-grid")
        print("held", flush=True)
        sys.stdin.readline()
        """
    )


def test_two_sweeps_mechanically_impossible_across_processes(tmp_path: Path) -> None:
    """A REAL second process holding the sweep flock refuses this process's acquire.

    The child takes the flock directly and writes a holder note; the parent's acquire
    must raise :class:`SweepAlreadyRunningError` echoing the note, and succeed once the
    child exits (kernel-released flock — no stale-lock state, no pid files).
    """
    with subprocess.Popen(  # fixed argv, test-owned script — no shell
        [sys.executable, "-c", _child_holds_sweep_lock_src(), str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "held"
            with (
                pytest.raises(SweepAlreadyRunningError) as excinfo,
                sweep_singleflight_lock(tmp_path),  # pragma: no cover — acquire raises
            ):
                pass  # pragma: no cover — body never runs
            assert "another sweep is already running on this output dir" in str(excinfo.value)
            assert "(holder: sweep-all:peer-grid)" in str(excinfo.value)
        finally:
            assert child.stdin is not None
            child.stdin.write("\n")
            child.stdin.close()
            child.wait(timeout=30)
    # The child exited → the kernel released its flock → a fresh acquire succeeds.
    with sweep_singleflight_lock(tmp_path):
        pass


def test_killed_holder_releases_the_sweep_lock(tmp_path: Path) -> None:
    """``kill -9`` of the holder releases the flock (kernel-released on process death),
    so no stale-lock wedge: a fresh acquire succeeds after the SIGKILL — the honest
    "released on every exit path INCLUDING kill -9" guarantee."""
    child = subprocess.Popen(
        [sys.executable, "-c", _child_holds_sweep_lock_src(), str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        # While the child holds it, THIS process is refused.
        with (
            pytest.raises(SweepAlreadyRunningError),
            sweep_singleflight_lock(tmp_path),  # pragma: no cover — acquire raises
        ):
            pass  # pragma: no cover — body never runs
        # SIGKILL the holder — no chance for a graceful __exit__/finally to run.
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()
    # The kernel released the dead child's flock → a fresh acquire succeeds, proving no
    # stale-lock recovery is needed even on a hard kill. (Poll briefly: the OS reclaims
    # the fd asynchronously on process teardown.)
    deadline = time.monotonic() + 10.0
    while True:
        try:
            with sweep_singleflight_lock(tmp_path):
                break  # acquired — the SIGKILL'd holder's lock was released
        except SweepAlreadyRunningError:  # pragma: no cover — transient teardown window
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
