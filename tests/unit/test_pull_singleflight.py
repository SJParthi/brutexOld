"""Tests for `brutex.adapters.groww.pull_singleflight` (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).

100% line+branch: the lock path, the held → second-acquire-refused contract (a SEPARATE
fd in the same process raises :class:`PullAlreadyRunningError` under ``LOCK_NB``), that
releasing lets it re-acquire, the disk side-effects (``.locks`` dir created, 0o600 perms),
and the error message carrying the lock path.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path

import pytest
from brutex.adapters.groww.pull_singleflight import (
    ACCOUNT_LOCK_FILE_PREFIX,
    PULL_SINGLEFLIGHT_LOCK_NAME,
    PullAlreadyRunningError,
    account_pull_singleflight_lock,
    account_singleflight_lock_path,
    default_account_lock_dir,
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


# ════════════════════════════════════════════════════════════════════════
# Account-keyed single-flight lock (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001)
# ════════════════════════════════════════════════════════════════════════


def test_account_lock_path_is_digest_only(tmp_path: Path) -> None:
    """The filename carries a 16-hex-char PBKDF2 fingerprint — NEVER the api_key itself.

    PBKDF2-HMAC-SHA256 over the fixed app salt instead of a bare sha256, so the
    visible filename cannot be used as a cheap offline-guessing oracle for the
    credential it fingerprints (CodeQL py/weak-sensitive-data-hashing).
    """
    api_key = "super-secret-api-key"
    path = account_singleflight_lock_path(api_key, lock_dir=tmp_path)
    assert path.parent == tmp_path
    name = path.name
    assert name.startswith(ACCOUNT_LOCK_FILE_PREFIX)
    assert name.endswith(".flock")
    digest = name[len(ACCOUNT_LOCK_FILE_PREFIX) : -len(".flock")]
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
    assert api_key not in str(path)  # no secret in a path or a log line
    expected = hashlib.pbkdf2_hmac(
        "sha256", api_key.encode("utf-8"), b"brutex-pull-account-lock", 200_000
    ).hex()[:16]
    assert digest == expected
    # Computed only — never created by the path helper.
    assert not path.exists()


def test_account_lock_path_stable_per_key_distinct_across_keys(tmp_path: Path) -> None:
    a1 = account_singleflight_lock_path("key-A", lock_dir=tmp_path)
    a2 = account_singleflight_lock_path("key-A", lock_dir=tmp_path)
    b = account_singleflight_lock_path("key-B", lock_dir=tmp_path)
    assert a1 == a2
    assert a1 != b


def test_account_lock_default_dir_is_per_user_home() -> None:
    assert default_account_lock_dir() == Path.home() / ".brutex" / "locks"
    # The default-dir branch of the path helper (path computation only — no disk touch).
    path = account_singleflight_lock_path("key-A")
    assert path.parent == default_account_lock_dir()


def test_account_lock_acquire_creates_dir_and_owner_only_file(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"  # not pre-created — mkdir(parents=True) path
    with account_pull_singleflight_lock("key-A", lock_dir=lock_dir):
        path = account_singleflight_lock_path("key-A", lock_dir=lock_dir)
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_account_lock_contended_refused_and_released_on_exit(tmp_path: Path) -> None:
    """The SAME api_key contends across two handles — even with DIFFERENT lake roots."""
    with (
        account_pull_singleflight_lock("key-A", lock_dir=tmp_path),
        pytest.raises(PullAlreadyRunningError) as excinfo,
        account_pull_singleflight_lock("key-A", lock_dir=tmp_path),  # pragma: no cover
    ):
        pass  # pragma: no cover — the inner acquire raises first
    assert excinfo.value.lock_path == account_singleflight_lock_path("key-A", lock_dir=tmp_path)
    # Holder released on exit → a fresh acquire succeeds.
    with account_pull_singleflight_lock("key-A", lock_dir=tmp_path):
        pass


def test_account_lock_distinct_keys_never_contend(tmp_path: Path) -> None:
    with (
        account_pull_singleflight_lock("key-A", lock_dir=tmp_path),
        account_pull_singleflight_lock("key-B", lock_dir=tmp_path),
    ):
        pass  # two DIFFERENT accounts may pull concurrently


def test_account_lock_enospc_maps_to_lake_write_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _enospc(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("brutex.adapters.groww.pull_singleflight.os.open", _enospc)
    with (
        pytest.raises(LakeWriteHalt) as excinfo,
        account_pull_singleflight_lock("key-A", lock_dir=tmp_path),
    ):
        pass  # pragma: no cover — open raises before the body runs
    assert excinfo.value.reason == "disk_full"
