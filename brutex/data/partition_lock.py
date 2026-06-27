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
The acquire is a BLOCKING ``LOCK_EX`` with no timeout: a CRASHED holder is released
by the kernel (no stale lock), but a HUNG holder (e.g. stuck mid-``write_parquet``
on a slow volume) stalls other writers to the SAME partition until it finishes or
dies — the deliberate correctness-first trade-off (impact bounded to one partition;
writers to OTHER partitions proceed unblocked). ``fcntl.flock`` is POSIX
(Linux + macOS — the Track-1 hardware envelope); Windows is out of scope.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
from collections import OrderedDict
from collections.abc import Generator, Hashable
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from brutex.data.atomic_io import lake_write_guard

__all__ = [
    "DEFAULT_LOCK_REGISTRY_CAPACITY",
    "BoundedLockRegistry",
    "partition_lock_path",
    "partition_write_lock",
]

#: Sidecar lock directory under the lake root. A dot-prefixed sibling of ``bars/``
#: + ``options/`` so it is never matched by a dataset glob or a lake scanner.
_LOCK_SUBDIR = ".locks"

#: Default cap on the number of interned per-partition :class:`threading.Lock`
#: objects kept resident in a :class:`BoundedLockRegistry`. A full multi-year pull
#: touches 10^5–10^6 distinct month partitions over its life, but only a handful are
#: ever in flight at once (the fetch fan-out is pool-bounded), so an unbounded intern
#: dict (the prior ``_PARTITION_LOCKS``) leaked a ``Lock`` per partition forever. 4096
#: dwarfs the live working set while staying tiny in memory; an IN-USE lock is NEVER
#: evicted (refcounted), so correctness is independent of the cap.
DEFAULT_LOCK_REGISTRY_CAPACITY: Final[int] = 4096


class _RefCountedLock:
    """A :class:`threading.Lock` plus the number of holders/waiters intending to use it.

    ``refs`` is incremented BEFORE a caller blocks on :attr:`lock` and decremented
    AFTER it releases, so a lock with ``refs > 0`` has at least one thread mid-acquire
    or holding it. The registry evicts only ``refs == 0`` entries, so an in-flight
    partition write can never have its lock dropped + re-created underneath it (which
    would split mutual exclusion across two distinct ``Lock`` objects).
    """

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refs = 0


class BoundedLockRegistry:
    """Capacity-bounded intern of per-key :class:`threading.Lock` objects (CLAUDE.md §11 COMMON).

    The lake writers serialise concurrent same-partition merge-writes with one shared
    :class:`threading.Lock` per destination path. Interning those locks in a plain
    ``dict`` (the prior ``_PARTITION_LOCKS``) grew one ``Lock`` per partition path
    FOREVER, so a full multi-year pull could accumulate 10^5–10^6 ``Lock`` objects.

    This registry caps the resident set at *capacity* with LRU eviction, evicting ONLY
    locks with no current holders/waiters (``refs == 0``) — so an in-flight write keeps
    its lock (correctness is independent of the cap). The exclusive section is entered
    via :meth:`acquire`, an O(1) context manager that interns-or-reuses the key's lock,
    refcounts it across the blocking acquire, and releases + decrements on exit. Two
    callers for the SAME key always get the SAME live lock object (so they serialise);
    once both finish, the lock becomes evictable.

    Parameters
    ----------
    capacity
        Maximum number of ``refs == 0`` (idle) locks kept resident; must be ``>= 1``.
        IN-USE locks are never counted against eviction, so the live set may briefly
        exceed *capacity* under heavy fan-out — bounded by the concurrent-writer count,
        not by the number of partitions ever written.
    """

    def __init__(self, *, capacity: int = DEFAULT_LOCK_REGISTRY_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._guard = threading.Lock()
        self._locks: OrderedDict[Hashable, _RefCountedLock] = OrderedDict()

    @property
    def capacity(self) -> int:
        """The maximum number of idle (evictable) interned locks — O(1)."""
        return self._capacity

    def size(self) -> int:
        """The number of interned locks currently resident — O(1)."""
        with self._guard:
            return len(self._locks)

    def _checkout(self, key: Hashable) -> _RefCountedLock:
        """Intern-or-reuse *key*'s lock, refcount it, and prune idle locks. O(1) amortised."""
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = _RefCountedLock()
                self._locks[key] = entry
            entry.refs += 1
            self._locks.move_to_end(key)  # most-recently-used.
            self._prune_locked()
            return entry

    def _checkin(self, key: Hashable, entry: _RefCountedLock) -> None:
        """Drop *key*'s refcount; once idle it becomes eligible for eviction. O(1)."""
        with self._guard:
            entry.refs -= 1
            self._prune_locked()

    def _prune_locked(self) -> None:
        """Evict least-recently-used IDLE (``refs == 0``) locks while over capacity.

        Called only under :attr:`_guard`. Walks oldest-first and removes only idle
        entries, so an in-use lock is never dropped — it merely defers pruning until it
        goes idle. Counting only idle locks against the cap means the live (in-use) set
        is bounded by the concurrent-writer count, never the partition count.
        """
        # The IDLE (evictable, ``refs == 0``) keys oldest-first; in-use keys are excluded so
        # they are NEVER evicted (correctness independent of the cap). Drop exactly the
        # oldest ``len(idle) - capacity`` of them, so the resident idle set lands at the cap.
        # ``[:negative]`` / ``[:0]`` is the empty slice — a no-op when already within budget,
        # so there is no separate "nothing to do" branch.
        idle_keys = [key for key, entry in self._locks.items() if entry.refs == 0]
        for key in idle_keys[: len(idle_keys) - self._capacity]:
            del self._locks[key]

    @contextmanager
    def acquire(self, key: Hashable) -> Generator[None, None, None]:
        """Hold *key*'s shared exclusive lock for the ``with`` block (interned, O(1)).

        The first caller for a key creates the lock; every later caller (any thread)
        gets the SAME live object, so same-key sections serialise. The lock is
        refcounted across the (blocking) acquire so it can never be evicted while a
        thread is mid-acquire or holding it; on exit the lock is released and the
        refcount dropped, after which the entry is eligible for LRU eviction.
        """
        entry = self._checkout(key)
        try:
            with entry.lock:
                yield
        finally:
            self._checkin(key, entry)


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
        # 0o600 — owner-only: the lock file is a private coordination sidecar, never
        # world-readable (CodeQL py/overly-permissive-file-permission).
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing the fd releases the advisory lock — an ``flock`` is tied to the
        # open file description, so the close both unlocks and frees the fd in one
        # step, on every (success or error) exit.
        os.close(fd)
