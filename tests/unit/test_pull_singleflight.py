"""Tests for `brutex.adapters.groww.pull_singleflight` (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).

100% line+branch: the lock path, the held → second-acquire-refused contract (a SEPARATE
fd in the same process raises :class:`PullAlreadyRunningError` under ``LOCK_NB``), that
releasing lets it re-acquire, the disk side-effects (``.locks`` dir created, 0o600 perms),
and the error message carrying the lock path.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest
from brutex.adapters.groww.pull_singleflight import (
    PULL_SINGLEFLIGHT_LOCK_NAME,
    PullAlreadyRunningError,
    pull_singleflight_lock,
    pull_singleflight_lock_path,
)
from brutex.data.errors import LakeWriteHalt


def test_lock_path_is_stable_sidecar_under_lake_root(tmp_path: Path) -> None:
    path = pull_singleflight_lock_path(tmp_path)
    assert path == tmp_path / ".locks" / PULL_SINGLEFLIGHT_LOCK_NAME
    # Computed only — never created by the path helper.
    assert not path.exists()


def test_acquire_creates_locks_dir_and_owner_only_lockfile(tmp_path: Path) -> None:
    with pull_singleflight_lock(tmp_path):
        lock_path = pull_singleflight_lock_path(tmp_path)
        assert lock_path.exists()
        # Owner-only 0o600 (CodeQL py/overly-permissive-file-permission).
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_second_acquire_while_held_is_refused(tmp_path: Path) -> None:
    # Acquire once; a SECOND acquisition (a SEPARATE fd, same process) must be refused
    # immediately by the non-blocking flock — the cross-process guard's core contract.
    # Contexts enter left-to-right: the holder takes the lock, then ``pytest.raises``
    # wraps the contended acquire, whose ``__enter__`` raises while the holder is active.
    with (
        pull_singleflight_lock(tmp_path),
        pytest.raises(PullAlreadyRunningError) as excinfo,
        pull_singleflight_lock(tmp_path),  # pragma: no cover — __enter__ raises first
    ):
        pass  # pragma: no cover — body never runs (the inner acquire raises)
    # The error names the contended lock file for an actionable operator message.
    assert excinfo.value.lock_path == pull_singleflight_lock_path(tmp_path)
    assert "a pull is already running" in str(excinfo.value)


def test_release_lets_it_reacquire(tmp_path: Path) -> None:
    # First holder releases on ``with`` exit; a fresh acquire then succeeds.
    with pull_singleflight_lock(tmp_path):
        pass
    with pull_singleflight_lock(tmp_path):
        pass  # re-acquired without raising — the release worked.


def test_two_distinct_lake_roots_never_contend(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    # Different lake roots hash to different lock files, so both can be held at once.
    with pull_singleflight_lock(root_a), pull_singleflight_lock(root_b):
        assert pull_singleflight_lock_path(root_a) != pull_singleflight_lock_path(root_b)


def test_lock_released_on_error_inside_block(tmp_path: Path) -> None:
    # An exception inside the ``with`` block still releases the lock (finally closes fd).
    class _Boom(Exception):
        pass

    def _raise_inside_lock() -> None:
        with pull_singleflight_lock(tmp_path):
            raise _Boom

    # ``pytest.raises`` catches the error raised inside the lock block; the lock's
    # ``finally`` runs during unwinding so the fd is closed before we return here.
    with pytest.raises(_Boom):
        _raise_inside_lock()
    # The lock is now free → re-acquire succeeds.
    with pull_singleflight_lock(tmp_path):
        pass


def test_contended_path_does_not_close_the_holders_lock(tmp_path: Path) -> None:
    # The refused acquire opens its OWN fd, fails to flock it, and closes only that fd in
    # its finally — the holder's lock must remain held throughout.
    with pull_singleflight_lock(tmp_path):
        for _ in range(3):
            with pytest.raises(PullAlreadyRunningError), pull_singleflight_lock(tmp_path):
                pass  # pragma: no cover — body never runs (acquire raises first)
        # Still held: a fourth attempt is still refused (the holder was never released).
        with pytest.raises(PullAlreadyRunningError), pull_singleflight_lock(tmp_path):
            pass  # pragma: no cover — body never runs (acquire raises first)
    # Holder released now → free.
    with pull_singleflight_lock(tmp_path):
        pass


def test_lock_survives_when_lockfile_preexists(tmp_path: Path) -> None:
    # A pre-existing lock file (from a prior process) is reused (O_CREAT is idempotent),
    # not clobbered — open + flock the same inode.
    locks = tmp_path / ".locks"
    locks.mkdir(parents=True)
    (locks / PULL_SINGLEFLIGHT_LOCK_NAME).write_text("")
    with pull_singleflight_lock(tmp_path):
        assert os.path.exists(locks / PULL_SINGLEFLIGHT_LOCK_NAME)


def test_lockfile_open_enospc_maps_to_lake_write_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M3 (DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996): a full disk at the lock-file
    # open maps to a LakeWriteHalt (consistent with partition_write_lock), NOT a raw OSError
    # that would escape the dashboard start() path uncaught.
    def _enospc(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("brutex.adapters.groww.pull_singleflight.os.open", _enospc)
    with pytest.raises(LakeWriteHalt) as excinfo, pull_singleflight_lock(tmp_path):
        pass  # pragma: no cover — open raises before the body runs
    assert excinfo.value.reason == "disk_full"
