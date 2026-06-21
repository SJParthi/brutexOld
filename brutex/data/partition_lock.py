"""Cross-process write-lock for a single lake partition (DEC-LAKE-PARTITION-FLOCK-001).

The lake writers (:class:`~brutex.data.parquet_writer.ParquetWriter` and
:mod:`brutex.data.option_parquet_writer`) do a READ-modify-WRITE on a month
partition: read the stored bars, union the incoming ones keyed by timestamp,
then atomically ``os.replace`` the whole file. A per-partition ``threading.Lock``
serialises this WITHIN one process (DEC-DHAN-PARALLEL-WINDOWS-001), but two
SEPARATE processes — e.g. a CLI ``brutex fetch`` and a dashboard pull, which share
the same ``BRUTEX_LAKE_ROOT`` — writing the SAME partition concurrently can still
LOSE data, silently (CLAUDE.md Golden Rule #1):

    P1 reads [T1,T2,T3] · P2 reads [T1,T2,T3] · P1 writes [T1,T2,T3,T4]
    · P2 writes [T1,T2,T3,T5]  →  P2's ``os.replace`` clobbers P1, **T4 is gone**.

This module adds an advisory :func:`fcntl.flock` held ACROSS the read-modify-write,
so cross-process writers to ONE partition serialise (the later writer reads AFTER
the earlier writer's ``os.replace``, so its union includes the earlier bars).

Design notes (COMMON · O(1) · honest):

* **A stable sidecar, never the partition file.** ``os.replace`` swaps the
  partition's INODE on every write, so an ``flock`` held on the partition fd would
  guard an orphaned inode while a second process locks the NEW one — no mutual
  exclusion. The lock is therefore taken on a stable per-partition sidecar under
  ``<lake_root>/.locks/<sha256(dest)>.lock`` that is only ever opened + locked,
  never replaced. The ``.locks`` dir is OUTSIDE the ``bars/`` + ``options/`` trees
  and is not a ``*.parquet`` file, so no lake scanner / dataset glob ever sees it.
* **Per partition, never global.** Writers to DIFFERENT partitions hash to
  DIFFERENT lock files and never contend — the pull's per-window fan-out keeps its
  parallelism; only same-partition writers (across processes) queue. A single
  write holds exactly ONE partition lock, so there is no lock-ordering cycle and no
  cross-partition deadlock.
* **No stale-lock recovery needed.** An ``flock`` is released by the kernel when
  the holding fd is closed OR the process dies, so a crashed pull never leaves a
  stuck lock (unlike a PID-file).
* **Disk-full consistency.** Creating the ``.locks`` dir / lock file on a full
  volume raises ``OSError(ENOSPC)``; that runs inside :func:`lake_write_guard` so
  it maps to a :class:`~brutex.data.errors.LakeWriteHalt` (pull-wide halt),
  matching the writers' DEC-LAKE-WRITE-HALT-001 behaviour rather than crashing raw.

**Honest envelope (Golden Rule #10):** this serialises CONCURRENT writers to one
partition; it is NOT a transaction across partitions and does NOT make a single
write atomic on its own (``atomic_write_via`` already does the crash-safe rename).
``fcntl.flock`` is POSIX (Linux + macOS — the Track-1 hardware envelope); Windows
is out of scope.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from brutex.data.atomic_io import lake_write_guard

__all__ = ["partition_lock_path", "partition_write_lock"]

#: Sidecar lock directory under the lake root. A dot-prefixed sibling of ``bars/``
#: + ``options/`` so it is never matched by a dataset glob or a lake scanner.
_LOCK_SUBDIR = ".locks"


def partition_lock_path(lake_root: Path, dest: Path) -> Path:
    """Return the stable lock-file path for the partition at *dest*.

    Keyed by ``sha256(str(dest))`` so a given partition ALWAYS maps to the same
    lock file (within a lake root) and two different partitions map to different
    files — the per-partition granularity the cross-process lock relies on.

    Parameters
    ----------
    lake_root
        The lake root; the ``.locks`` dir lives directly under it.
    dest
        The partition's Parquet path (its inode is swapped on every write, so the
        path — not the file — is the stable key).

    Returns
    -------
    Path
        ``<lake_root>/.locks/<sha256(dest)>.lock`` (never created here).
    """
    digest = hashlib.sha256(str(dest).encode("utf-8")).hexdigest()
    return lake_root / _LOCK_SUBDIR / f"{digest}.lock"


@contextmanager
def partition_write_lock(lake_root: Path, dest: Path) -> Generator[None, None, None]:
    """Hold an exclusive cross-process advisory lock for the *dest* partition.

    Opens (creating on first use) the stable sidecar lock file for *dest* and takes
    a blocking exclusive :func:`fcntl.flock` on it for the duration of the ``with``
    block, then releases it by closing the fd. Two processes writing the SAME
    partition serialise here; writers to different partitions never contend.

    The lock-file creation runs inside :func:`lake_write_guard`, so a full disk at
    that step becomes a :class:`~brutex.data.errors.LakeWriteHalt` (consistent with
    the writers, DEC-LAKE-WRITE-HALT-001) instead of a raw ``OSError``.

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.locks`` sidecar dir).
    dest
        The partition path being read-modify-written.

    Yields
    ------
    None
        Control returns to the caller while the exclusive lock is held.
    """
    lock_path = partition_lock_path(lake_root, dest)
    with lake_write_guard():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing the fd releases the advisory lock — an ``flock`` is tied to the
        # open file description, so the close both unlocks and frees the fd in one
        # step, on every (success or error) exit.
        os.close(fd)
