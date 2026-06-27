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
The acquire is BOUNDED (DEC-PULL-CONTENTION-ANTISTALL-001): it retries a non-blocking
``LOCK_EX | LOCK_NB`` under a short deadline (env ``BRUTEX_PARTITION_LOCK_TIMEOUT_S``,
default :data:`DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS`, well under the 600 s pull
watchdog) so a WEDGED holder (e.g. stuck mid-``write_parquet`` on a hung volume) can
NEVER pin a worker until the watchdog force-kills the whole pull. On expiry it raises
the TRANSIENT :class:`~brutex.data.errors.PartitionLockTimeout` — the ingest spines
catch it at the window boundary, record the window ``FAILED``, and DEFER it to the next
pull, exactly like a 429 / timeout (the peer genuinely making progress finishes and the
deferred window succeeds on the re-pull). A CRASHED holder is still released by the
kernel (no stale lock). On each poll the bounded wait fires a
:func:`~brutex.observability.progress_beat.emit_progress_beat` so a slow-but-progressing
pull contending on a busy partition keeps its watchdog heartbeat alive (a no-op when no
watchdog is registered). ``fcntl.flock`` is POSIX (Linux + macOS — the Track-1 hardware
envelope); Windows is out of scope.
"""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from brutex.data.atomic_io import lake_write_guard
from brutex.data.errors import PartitionLockTimeout
from brutex.observability.progress_beat import emit_progress_beat

__all__ = [
    "DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS",
    "PARTITION_LOCK_POLL_SECONDS",
    "PARTITION_LOCK_TIMEOUT_ENV",
    "partition_lock_path",
    "partition_write_lock",
    "resolve_partition_lock_timeout_seconds",
]

#: Sidecar lock directory under the lake root. A dot-prefixed sibling of ``bars/``
#: + ``options/`` so it is never matched by a dataset glob or a lake scanner.
_LOCK_SUBDIR = ".locks"

#: Default bounded-acquire deadline (seconds). A WELL-under-600 s value (the pull
#: watchdog's force-fail timeout) so a wedged peer defers the window long before the
#: watchdog would kill the whole pull — the real-root anti-stall fix. Generous enough
#: that a peer doing a normal multi-second read-modify-write is simply waited out, not
#: spuriously deferred. Operator-tunable via :data:`PARTITION_LOCK_TIMEOUT_ENV`.
DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS: float = 120.0

#: Sleep between non-blocking re-acquire attempts (seconds). Short so the wait reacts
#: promptly when the peer releases, but not a busy-spin.
PARTITION_LOCK_POLL_SECONDS: float = 0.05

#: Environment override for the bounded-acquire deadline (operator escape hatch).
PARTITION_LOCK_TIMEOUT_ENV: str = "BRUTEX_PARTITION_LOCK_TIMEOUT_S"


def resolve_partition_lock_timeout_seconds() -> float:
    """Resolve the bounded lock-acquire deadline: env override, else the audited default.

    Reads :data:`PARTITION_LOCK_TIMEOUT_ENV`
    (:data:`BRUTEX_PARTITION_LOCK_TIMEOUT_S`); falls back to
    :data:`DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS` when unset, blank, non-numeric,
    non-positive, OR non-finite (``nan`` / ``inf`` parse as floats but would defeat the
    bound — a ``nan`` deadline makes ``clock() >= deadline`` perpetually False, an ``inf``
    deadline never expires — re-introducing the infinite block this fix removes). A bad
    value can therefore NEVER disable the bound. O(1).
    """
    raw = os.environ.get(PARTITION_LOCK_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0.0:
        return DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS
    return value


def _acquire_bounded(
    fd: int,
    *,
    timeout_seconds: float,
    poll_seconds: float = PARTITION_LOCK_POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Acquire an exclusive ``flock`` on *fd*, retrying NON-BLOCKING under a deadline.

    Loops ``fcntl.flock(fd, LOCK_EX | LOCK_NB)`` catching ``BlockingIOError`` (the peer
    holds it), firing a liveness :func:`~brutex.observability.progress_beat.emit_progress_beat`
    + sleeping *poll_seconds* between tries, until the lock is taken or *timeout_seconds*
    of monotonic time has elapsed. On expiry raises
    :class:`~brutex.data.errors.PartitionLockTimeout` (the TRANSIENT defer signal) — never
    an unbounded block. *clock* / *sleep* are injected for deterministic tests.
    """
    deadline = clock() + timeout_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            # The peer holds the lock. Beat the watchdog (a slow-but-progressing pull
            # contending here must keep its heartbeat alive — DEC-PULL-CONTENTION-
            # ANTISTALL-001 GAP #15) then re-check the deadline.
            emit_progress_beat()
            if clock() >= deadline:
                raise PartitionLockTimeout(timeout_seconds) from None
            sleep(poll_seconds)


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
def partition_write_lock(
    lake_root: Path, dest: Path, *, timeout_seconds: float | None = None
) -> Generator[None, None, None]:
    """Hold an exclusive cross-process advisory lock for the *dest* partition.

    Opens (creating on first use) the stable sidecar lock file for *dest* and takes a
    BOUNDED exclusive :func:`fcntl.flock` on it for the duration of the ``with`` block,
    then releases it by closing the fd. Two processes writing the SAME partition
    serialise here; writers to different partitions never contend.

    The acquire is bounded (DEC-PULL-CONTENTION-ANTISTALL-001): it retries a
    non-blocking ``LOCK_EX | LOCK_NB`` under *timeout_seconds* (default
    :func:`resolve_partition_lock_timeout_seconds`) so a WEDGED peer can never pin a
    worker until the 600 s pull watchdog force-kills the pull. On expiry it raises
    :class:`~brutex.data.errors.PartitionLockTimeout` (a TRANSIENT defer signal the
    ingest spines catch at the window boundary) — never an unbounded block. Each poll
    fires a :func:`~brutex.observability.progress_beat.emit_progress_beat` so a
    slow-but-progressing pull contending here keeps its watchdog heartbeat alive.

    The lock-file creation runs inside :func:`lake_write_guard`, so a full disk at
    that step becomes a :class:`~brutex.data.errors.LakeWriteHalt` (consistent with
    the writers, DEC-LAKE-WRITE-HALT-001) instead of a raw ``OSError``.

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.locks`` sidecar dir).
    dest
        The partition path being read-modify-written.
    timeout_seconds
        Bounded-acquire deadline; defaults to
        :func:`resolve_partition_lock_timeout_seconds` (env-tunable).

    Yields
    ------
    None
        Control returns to the caller while the exclusive lock is held.

    Raises
    ------
    PartitionLockTimeout
        If the lock is held by a peer for longer than the deadline.
    LakeWriteHalt
        If creating the sidecar lock file hits a full disk (ENOSPC) / OOM.
    """
    deadline = (
        resolve_partition_lock_timeout_seconds() if timeout_seconds is None else timeout_seconds
    )
    lock_path = partition_lock_path(lake_root, dest)
    with lake_write_guard():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600 — owner-only: the lock file is a private coordination sidecar, never
        # world-readable (CodeQL py/overly-permissive-file-permission).
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _acquire_bounded(fd, timeout_seconds=deadline)
        yield
    finally:
        # Closing the fd releases the advisory lock — an ``flock`` is tied to the
        # open file description, so the close both unlocks and frees the fd in one
        # step, on every (success or error) exit.
        os.close(fd)
