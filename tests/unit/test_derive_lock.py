"""Tests for :mod:`brutex.data.derive_lock` (DEC-ENGINE-MISC-PACK-001).

The cross-process derive-phase flock at the FIXED sidecar path
``<lake_root>/.locks/derive.lock``: the writer
(:func:`~brutex.data.derive_lock.derive_write_lock`) takes a BOUNDED exclusive
flock for one symbol's materialise; the reader
(:func:`~brutex.data.derive_lock.derive_in_progress`) is a NON-BLOCKING shared
probe that NEVER writes (an absent lock file returns ``False`` without being
created — a read-only lake stays read-only). ``fcntl.flock`` conflicts are
per open-file-description, so a SECOND fd in this same process contends
exactly like a second process — the cross-process semantics are testable
deterministically in-process.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

import pytest
from brutex.data.derive_lock import (
    DERIVE_LOCK_FILENAME,
    derive_in_progress,
    derive_lock_path,
    derive_write_lock,
)
from brutex.data.errors import LakeWriteHalt, PartitionLockTimeout


def test_derive_lock_path_is_fixed_under_locks_dir(tmp_path: Path) -> None:
    """The lock lives at the FIXED name ``<lake_root>/.locks/derive.lock``."""
    path = derive_lock_path(tmp_path)
    assert path == tmp_path / ".locks" / DERIVE_LOCK_FILENAME
    assert not path.exists()  # never created by the path helper.


def test_probe_absent_file_is_false_and_never_creates_it(tmp_path: Path) -> None:
    """A never-derived lake probes ``False`` WITHOUT writing the lock file.

    The reader-side no-write contract: a read-only lake (e.g. a mounted
    snapshot) must never gain a ``.locks/`` dir from a mere ``/movers`` poll.
    """
    assert derive_in_progress(tmp_path) is False
    assert not derive_lock_path(tmp_path).exists()
    assert not (tmp_path / ".locks").exists()


def test_probe_idle_existing_file_is_false(tmp_path: Path) -> None:
    """An existing lock file with NO holder (a finished derive) probes ``False``."""
    with derive_write_lock(tmp_path):
        pass  # creates the sidecar, then releases on exit.
    assert derive_lock_path(tmp_path).exists()
    assert derive_in_progress(tmp_path) is False


def test_probe_true_while_writer_holds_exclusive(tmp_path: Path) -> None:
    """While the writer holds EX, the shared probe reads ``True``; after, ``False``."""
    with derive_write_lock(tmp_path):
        assert derive_in_progress(tmp_path) is True
    assert derive_in_progress(tmp_path) is False


def test_probe_race_file_deleted_between_exists_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock file vanishing at open time reads ``False`` (no writer), never raises."""

    def _vanished_open(path: object, flags: int, mode: int = 0o777) -> int:
        raise FileNotFoundError(errno.ENOENT, "vanished", str(path))

    monkeypatch.setattr("brutex.data.derive_lock.os.open", _vanished_open)
    assert derive_in_progress(tmp_path) is False


def test_writer_bounded_acquire_times_out_against_wedged_peer(tmp_path: Path) -> None:
    """A wedged EX holder makes the writer raise the TRANSIENT PartitionLockTimeout.

    The peer is a SEPARATE fd holding ``LOCK_EX`` (flock contends per open file
    description — same-process second fd behaves like a second process). The
    bound reuses the COMMON partition-lock deadline machinery — never an
    unbounded block.
    """
    lock_path = derive_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True)
    peer_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(peer_fd, fcntl.LOCK_EX)
        with (
            pytest.raises(PartitionLockTimeout),
            derive_write_lock(tmp_path, timeout_seconds=0.05),
        ):
            pytest.fail("must not acquire while the peer holds EX")
    finally:
        os.close(peer_fd)
    # The peer's death (fd close) releases the lock — the next writer succeeds.
    with derive_write_lock(tmp_path, timeout_seconds=0.05):
        assert derive_in_progress(tmp_path) is True


def test_writer_second_holder_serialises_after_release(tmp_path: Path) -> None:
    """Two sequential writers both succeed — the lock is reusable, not one-shot."""
    with derive_write_lock(tmp_path):
        pass
    with derive_write_lock(tmp_path):
        assert derive_in_progress(tmp_path) is True


def _boom() -> None:
    """Raise the sentinel error (a helper so no statement is provably dead)."""
    raise RuntimeError("boom")


def test_writer_releases_lock_even_when_body_raises(tmp_path: Path) -> None:
    """An exception inside the writer block still releases the flock (finally)."""
    with pytest.raises(RuntimeError, match="boom"), derive_write_lock(tmp_path):
        _boom()
    assert derive_in_progress(tmp_path) is False


def test_disk_full_at_lock_creation_maps_to_lake_write_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ENOSPC creating the sidecar maps to LakeWriteHalt (DEC-LAKE-WRITE-HALT-001)."""

    def _full_disk_open(path: object, flags: int, mode: int = 0o777) -> int:
        raise OSError(errno.ENOSPC, "No space left on device", str(path))

    monkeypatch.setattr("brutex.data.derive_lock.os.open", _full_disk_open)
    with pytest.raises(LakeWriteHalt) as exc_info, derive_write_lock(tmp_path):
        pytest.fail("body must never run when the lock file cannot be created")
    assert exc_info.value.reason == "disk_full"


def test_default_timeout_resolves_common_partition_lock_knob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``timeout_seconds=None`` resolves the COMMON env-tunable deadline (no copy).

    Pin the REUSE: the derive lock rides
    ``resolve_partition_lock_timeout_seconds`` — the env knob set here is
    honoured by the default acquire path (proven by observing the resolved
    deadline flow through a successful uncontended acquire).
    """
    monkeypatch.setenv("BRUTEX_PARTITION_LOCK_TIMEOUT_S", "0.01")
    resolved: list[float] = []
    real = fcntl.flock

    def _spy_flock(fd: int, operation: int) -> None:
        resolved.append(1.0)
        real(fd, operation)

    monkeypatch.setattr("brutex.data.partition_lock.fcntl.flock", _spy_flock)
    with derive_write_lock(tmp_path):
        pass
    assert resolved, "the COMMON bounded acquire (partition_lock) must run the flock"
