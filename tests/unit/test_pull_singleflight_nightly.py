"""Nightly single-flight lock + holder-note seams (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Extends the ``test_pull_singleflight*`` stem family: the NEW nightly lock beside the
pull locks (two overlapping nightlies mechanically impossible — pinned by a REAL
two-process test), the ``contention_error`` / ``holder_note`` kw seams on the ONE
``_flock_singleflight`` core (existing callers byte-identical), the holder-note echo
in both refusal errors, and the note-write-failure-is-swallowed contract.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from brutex.adapters.groww.pull_singleflight import (
    NIGHTLY_SINGLEFLIGHT_LOCK_NAME,
    NightlyAlreadyRunningError,
    PullAlreadyRunningError,
    nightly_singleflight_lock,
    nightly_singleflight_lock_path,
    pull_singleflight_lock,
    pull_singleflight_lock_path,
)


def test_nightly_lock_path_is_stable_sidecar_beside_pull_lock(tmp_path: Path) -> None:
    """The nightly lock lives at ``<lake>/.locks/nightly_singleflight.lock`` — never created here."""
    path = nightly_singleflight_lock_path(tmp_path)
    assert path == tmp_path / ".locks" / NIGHTLY_SINGLEFLIGHT_LOCK_NAME
    assert path.parent == pull_singleflight_lock_path(tmp_path).parent
    assert path != pull_singleflight_lock_path(tmp_path)
    assert not path.exists()


def test_nightly_and_pull_locks_never_contend(tmp_path: Path) -> None:
    """Distinct lock files: the nightly lock and the pull lock can be held together."""
    with nightly_singleflight_lock(tmp_path), pull_singleflight_lock(tmp_path):
        pass  # both held at once — no contention across the two names


def test_nightly_second_acquire_while_held_is_refused(tmp_path: Path) -> None:
    """Same-process second fd contention raises the nightly-specific error."""
    with (
        pull_singleflight_lock(tmp_path),  # the PULL lock never blocks the nightly lock
        nightly_singleflight_lock(tmp_path),
        pytest.raises(NightlyAlreadyRunningError) as excinfo,
        nightly_singleflight_lock(tmp_path),  # pragma: no cover — __enter__ raises first
    ):
        pass  # pragma: no cover — body never runs (the inner acquire raises)
    assert excinfo.value.lock_path == nightly_singleflight_lock_path(tmp_path)
    assert "a nightly chain is already running" in str(excinfo.value)


def test_nightly_release_lets_it_reacquire(tmp_path: Path) -> None:
    with nightly_singleflight_lock(tmp_path):
        pass
    with nightly_singleflight_lock(tmp_path):
        pass  # re-acquired without raising — the release worked


def test_two_nightlies_mechanically_impossible_across_processes(tmp_path: Path) -> None:
    """A REAL second process holding the nightly flock refuses this process's acquire.

    The child takes the flock directly (same fcntl mechanics, no brutex import — fast
    startup) and writes a holder note; the parent's acquire must raise
    :class:`NightlyAlreadyRunningError` echoing the note, and succeed once the child
    exits (kernel-released flock — no stale-lock state, no pid files).
    """
    child_src = textwrap.dedent(
        """
        import fcntl, os, sys
        from pathlib import Path

        lock = Path(sys.argv[1]) / ".locks" / "nightly_singleflight.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, b"nightly:peer-id")
        print("held", flush=True)
        sys.stdin.readline()
        """
    )
    # ``with Popen`` closes both pipes + waits, so no fd outlives the test
    # (a leaked pipe would surface as a PytestUnraisableExceptionWarning later).
    with subprocess.Popen(  # fixed argv, test-owned script — no shell
        [sys.executable, "-c", child_src, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "held"
            with (
                pytest.raises(NightlyAlreadyRunningError) as excinfo,
                nightly_singleflight_lock(tmp_path),  # pragma: no cover — acquire raises
            ):
                pass  # pragma: no cover — body never runs
            assert "(holder: nightly:peer-id)" in str(excinfo.value)
        finally:
            assert child.stdin is not None
            child.stdin.write("\n")
            child.stdin.close()
            child.wait(timeout=30)
    # The child exited → the kernel released its flock → a fresh acquire succeeds.
    with nightly_singleflight_lock(tmp_path):
        pass


def test_holder_note_written_and_echoed_in_pull_refusal(tmp_path: Path) -> None:
    """A noted pull-lock holder is NAMED in the contended peer's refusal message."""
    with (
        pull_singleflight_lock(tmp_path, holder_note="nightly:n-1"),
        pytest.raises(PullAlreadyRunningError) as excinfo,
        pull_singleflight_lock(tmp_path),  # pragma: no cover — acquire raises first
    ):
        pass  # pragma: no cover — body never runs
    message = str(excinfo.value)
    assert "a pull is already running" in message
    assert message.endswith("(holder: nightly:n-1)")


def test_no_note_keeps_pull_refusal_message_byte_identical(tmp_path: Path) -> None:
    """Default (note-less) callers keep the exact pre-note refusal message."""
    with (
        pull_singleflight_lock(tmp_path),
        pytest.raises(PullAlreadyRunningError) as excinfo,
        pull_singleflight_lock(tmp_path),  # pragma: no cover — acquire raises first
    ):
        pass  # pragma: no cover — body never runs
    lock_path = pull_singleflight_lock_path(tmp_path)
    assert str(excinfo.value) == (
        "a pull is already running for this lake "
        f"(account-wide single-flight lock held at {lock_path}); "
        "refusing to start a second concurrent pull, which would double the "
        "Groww account request rate"
    )


def test_stale_note_cleared_by_next_noteless_holder(tmp_path: Path) -> None:
    """A prior holder's note can never lie: the next acquire truncates it."""
    with pull_singleflight_lock(tmp_path, holder_note="nightly:old"):
        pass
    # A NEW note-less holder truncates the stale note on acquire...
    with (
        pull_singleflight_lock(tmp_path),
        pytest.raises(PullAlreadyRunningError) as excinfo,
        pull_singleflight_lock(tmp_path),  # pragma: no cover — acquire raises first
    ):
        pass  # pragma: no cover — body never runs
    # ...so the refusal never blames the long-gone nightly.
    assert "(holder:" not in str(excinfo.value)


def test_note_write_failure_is_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing advisory note write never blocks the acquired lock."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("ftruncate failed")

    monkeypatch.setattr("brutex.adapters.groww.pull_singleflight.os.ftruncate", _boom)
    with nightly_singleflight_lock(tmp_path, holder_note="nightly:x"):
        pass  # acquired despite the note-write failure — advisory only


def test_missing_lock_file_reads_as_no_holder_note(tmp_path: Path) -> None:
    """Constructing the error for a nonexistent lock path stays note-free (OSError arm)."""
    err = PullAlreadyRunningError(tmp_path / "absent" / "no.lock")
    assert "(holder:" not in str(err)
    nightly_err = NightlyAlreadyRunningError(tmp_path / "absent" / "no.lock")
    assert "(holder:" not in str(nightly_err)
