"""Unit tests for the cross-process lake partition write-lock.

DEC-LAKE-PARTITION-FLOCK-001 — :mod:`brutex.data.partition_lock` adds an advisory
``fcntl.flock`` held across a partition's read-modify-write so two PROCESSES (a CLI
``brutex fetch`` + a dashboard pull sharing one lake) can't lost-update the same
month partition. The lock is on a stable ``<lake_root>/.locks/<sha256(dest)>.lock``
sidecar (never the partition file, whose inode ``os.replace`` swaps), per partition,
and a full disk at lock creation maps to a :class:`LakeWriteHalt`.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.partition_lock import partition_lock_path, partition_write_lock


def _raise(exc: BaseException) -> None:
    """Raise *exc* via a CALL, not a literal ``raise`` statement.

    Keeps the assertion AFTER a ``with pytest.raises(...), partition_write_lock(...)``
    block reachable to the static analyzer: a literal ``raise`` as the last with-body
    statement makes CodeQL mark the following assert "unreachable" (a false positive —
    ``pytest.raises`` suppresses the exception). A function call is not treated as
    unconditionally terminating, so the assert stays reachable (mirrors
    ``tests/unit/test_atomic_io.py``).
    """
    raise exc


def _try_lock_held_by_other_fd(lock_path: Path) -> bool:
    """Return True iff a SEPARATE fd cannot take the exclusive lock (i.e. it is held).

    A second ``open`` of the same path is a distinct open file description, so
    ``fcntl.flock`` treats it like another PROCESS — exactly the cross-process
    contention the production lock must win. A non-blocking ``LOCK_EX`` that raises
    ``BlockingIOError`` proves the lock is currently held.
    """
    probe_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(probe_fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(probe_fd)


# ── partition_lock_path ──────────────────────────────────────────────────────
def test_lock_path_is_under_locks_dir(tmp_path: Path) -> None:
    dest = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2025" / "05.parquet"
    lock_path = partition_lock_path(tmp_path, dest)
    assert lock_path.parent == tmp_path / ".locks"
    assert lock_path.suffix == ".lock"
    # The .locks dir is OUTSIDE the bars/ tree and is not a *.parquet file, so no
    # dataset glob / lake scanner ever sees it.
    assert ".parquet" not in lock_path.name


def test_lock_path_is_deterministic_per_partition(tmp_path: Path) -> None:
    """Same dest → same lock file; different dest → different lock file (per-partition)."""
    dest_a = tmp_path / "bars" / "a" / "05.parquet"
    dest_b = tmp_path / "bars" / "b" / "05.parquet"
    assert partition_lock_path(tmp_path, dest_a) == partition_lock_path(tmp_path, dest_a)
    assert partition_lock_path(tmp_path, dest_a) != partition_lock_path(tmp_path, dest_b)


# ── partition_write_lock ─────────────────────────────────────────────────────
def test_lock_creates_locks_dir_and_holds_then_releases(tmp_path: Path) -> None:
    dest = tmp_path / "bars" / "x" / "05.parquet"
    lock_path = partition_lock_path(tmp_path, dest)
    assert not lock_path.exists()

    with partition_write_lock(tmp_path, dest):
        assert lock_path.exists()  # created lazily on first use
        # While held, a separate fd (≈ another process) CANNOT acquire it.
        assert _try_lock_held_by_other_fd(lock_path) is True

    # After the block the lock is released (fd closed) — a fresh acquire succeeds.
    assert _try_lock_held_by_other_fd(lock_path) is False


def test_lock_released_even_when_body_raises(tmp_path: Path) -> None:
    """The lock is released on the error path too (the caller's error still propagates)."""
    dest = tmp_path / "bars" / "y" / "05.parquet"
    lock_path = partition_lock_path(tmp_path, dest)

    with pytest.raises(ValueError, match="boom"), partition_write_lock(tmp_path, dest):
        _raise(ValueError("boom"))

    assert _try_lock_held_by_other_fd(lock_path) is False


def test_different_partitions_do_not_contend(tmp_path: Path) -> None:
    """Holding one partition's lock leaves a DIFFERENT partition free (parallelism kept)."""
    dest_a = tmp_path / "bars" / "a" / "05.parquet"
    dest_b = tmp_path / "bars" / "b" / "05.parquet"
    with partition_write_lock(tmp_path, dest_a):
        # A nested acquire of a DIFFERENT partition does not block.
        with partition_write_lock(tmp_path, dest_b):
            assert _try_lock_held_by_other_fd(partition_lock_path(tmp_path, dest_b)) is True
        assert _try_lock_held_by_other_fd(partition_lock_path(tmp_path, dest_a)) is True


def test_disk_full_at_lock_creation_maps_to_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ENOSPC opening the lock file maps to LakeWriteHalt (DEC-LAKE-WRITE-HALT-001)."""
    dest = tmp_path / "bars" / "z" / "05.parquet"

    def _full_disk_open(*_a: object, **_k: object) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("brutex.data.partition_lock.os.open", _full_disk_open)
    # The guard maps ENOSPC inside `__enter__` (before the body), so enter directly —
    # a `with ...: <body>` would make the body unreachable dead code.
    cm = partition_write_lock(tmp_path, dest)
    with pytest.raises(LakeWriteHalt) as exc_info:
        cm.__enter__()
    assert exc_info.value.reason == "disk_full"
