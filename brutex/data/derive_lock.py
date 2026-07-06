"""Cross-process derive-phase lock for the lake's materialised timeframes (DEC-ENGINE-MISC-PACK-001).

The pipeline prebuild (:func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes`)
rewrites a symbol's WHOLE derived-TF SET — 19 intraday partitions plus the derived
``1day``. Each individual month-file write is already atomic + flock-guarded
(:mod:`brutex.data.partition_lock`, DEC-LAKE-PARTITION-FLOCK-001), but nothing
signalled ACROSS PROCESSES that a derive was mid-flight: a dashboard ``/movers``
stored-first read (:func:`~brutex.dashboard.movers_stored_read.read_stored_day_frame`)
concurrent with a CLI-process pipeline could observe a HALF-REFRESHED TF set (some
timeframes rewritten, some stale). The in-process
:class:`~brutex.dashboard.prebuild_gate.PrebuildGate` is explicitly PROCESS-LOCAL
(stated in its docstring), so a separate-process derive had no reader-visible signal.

This module adds ONE advisory :func:`fcntl.flock` at the FIXED sidecar path
``<lake_root>/.locks/derive.lock`` (the fixed-single-path precedent of
``pull_singleflight.lock``; the per-partition hash-keyed sidecars of
:mod:`~brutex.data.partition_lock` stay untouched):

* **Writer** — :func:`derive_write_lock` takes a BOUNDED exclusive flock for the
  duration of ONE symbol's materialise (`materialize_lake_timeframes` wraps its
  whole per-symbol derivation), released between symbols so readers are blocked
  only while a derive is actually in flight. Reuses the COMMON bounded acquire +
  timeout resolution of :mod:`~brutex.data.partition_lock` (CLAUDE.md §11 COMMON —
  no copy): on expiry it raises the SAME transient
  :class:`~brutex.data.errors.PartitionLockTimeout`, and lock-file creation runs
  inside :func:`~brutex.data.atomic_io.lake_write_guard` so a full disk maps to
  :class:`~brutex.data.errors.LakeWriteHalt` (DEC-LAKE-WRITE-HALT-001).
* **Reader** — :func:`derive_in_progress` is a NON-BLOCKING shared probe: absent
  lock file ⇒ ``False`` WITHOUT creating it (a read-only lake is never written by
  a probe); ``LOCK_SH | LOCK_NB`` refused ⇒ a writer holds the exclusive lock ⇒
  ``True``; granted ⇒ released immediately ⇒ ``False``. O(1) per call. The
  ``/movers`` stored-first branch probes it BEFORE the stored read and, while a
  derive is in flight, takes the existing on-the-fly 1-minute-resample fallback —
  the byte-equality reference path (DEC-PIPELINE-AUTOPREBUILD-001), counted under
  the existing honest ``resampled_reads`` provenance.

**Lock ORDER (no cycle):** the derive lock is taken OUTSIDE the per-partition
flocks — only the WRITER holds both (derive → partition, always in that order);
the reader probe never blocks and never takes a partition lock.

**Honest envelope (Golden Rule #10):** this is the scoped FIRST STEP — writer +
the ``/movers`` spot stored-first reader only. NAMED FOLLOW-UPS (not claimed):
probing in the other stored-TF readers (the ``/db`` viewer, the doctor's
derived-freshness compare) and teaching the process-local ``PrebuildGate`` to
honor the cross-process lock (a ``gated:true`` payload for a CLI-process
pipeline). The FUTURES/OPTIONS movers session readers need nothing — they are
locked 1-minute-native (DEC-FNO-TF-DERIVE-OI-001). ``fcntl.flock`` is POSIX
(Linux + macOS — the Track-1 envelope); Windows is out of scope, exactly as for
DEC-LAKE-PARTITION-FLOCK-001. A crashed holder is released by the kernel on
process death — no stale-lock recovery needed.
"""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

from brutex.data.atomic_io import lake_write_guard
from brutex.data.partition_lock import (
    _acquire_bounded,
    resolve_partition_lock_timeout_seconds,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

__all__ = [
    "DERIVE_LOCK_FILENAME",
    "derive_in_progress",
    "derive_lock_path",
    "derive_write_lock",
]

#: The fixed derive-lock filename under ``<lake_root>/.locks/`` — ONE lock per
#: lake (the derive rewrites a whole TF set, so per-partition granularity would
#: defeat the "half-refreshed set" signal). The ``.locks`` sidecar dir is the
#: SAME one :mod:`~brutex.data.partition_lock` uses (dot-prefixed, outside
#: ``bars/`` + ``options/``, never matched by a dataset glob).
DERIVE_LOCK_FILENAME: Final[str] = "derive.lock"

#: Sidecar lock directory under the lake root — mirrors
#: ``partition_lock._LOCK_SUBDIR`` / ``pull_singleflight._LOCK_SUBDIR`` (the
#: value is part of the on-disk lake layout contract, restated per module).
_LOCK_SUBDIR: Final[str] = ".locks"

# Process-local REENTRANCY registry (DEC-HARDWARE-ENVELOPE-001 fold): the
# parallel prebuild holds the derive lock ONCE around its whole thread-pool run
# while every worker's ``materialize_lake_timeframes`` re-enters it per symbol.
# ``fcntl.flock`` is per OPEN FILE DESCRIPTION, so without this registry two
# threads of ONE process would serialise on the exclusive flock — turning the
# thread-pool fan-out back into a sequential run (the bench gate catches it).
# Cross-PROCESS semantics are untouched: only the FIRST holder in this process
# takes (and finally releases) the real flock; nested holders ride it. The
# nested scope MUST sit inside the outer holder's ``with`` block (true by
# construction — the pool is joined before the orchestrator exits).
_REENTRANT_MUTEX: Final[threading.Lock] = threading.Lock()
_HELD_DEPTHS: Final[dict[str, int]] = {}


def derive_lock_path(lake_root: Path) -> Path:
    """Return the FIXED derive-lock sidecar path for *lake_root*.

    ``<lake_root>/.locks/derive.lock`` — a stable, never-replaced file (opened +
    flocked only), so the reader probe can address it by NAME without any
    registry. Never created here.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).

    Returns
    -------
    Path
        The derive-lock file path (not created).
    """
    return lake_root / _LOCK_SUBDIR / DERIVE_LOCK_FILENAME


@contextmanager
def derive_write_lock(
    lake_root: Path, *, timeout_seconds: float | None = None
) -> Generator[None, None, None]:
    """Hold the exclusive cross-process derive lock for the ``with`` block.

    Opens (creating on first use) the fixed sidecar lock file and takes a
    BOUNDED exclusive :func:`fcntl.flock` on it, released by closing the fd on
    exit. Two processes deriving into the SAME lake serialise here; a reader's
    :func:`derive_in_progress` probe reads ``True`` while the block runs.

    The acquire REUSES :func:`brutex.data.partition_lock._acquire_bounded`
    (CLAUDE.md §11 COMMON): a non-blocking ``LOCK_EX | LOCK_NB`` retried under
    *timeout_seconds* (default :func:`~brutex.data.partition_lock.resolve_partition_lock_timeout_seconds`,
    env-tunable ``BRUTEX_PARTITION_LOCK_TIMEOUT_S``), each poll firing a
    watchdog :func:`~brutex.observability.progress_beat.emit_progress_beat`; on
    expiry it raises the TRANSIENT
    :class:`~brutex.data.errors.PartitionLockTimeout` — never an unbounded
    block. Lock-file creation runs inside
    :func:`~brutex.data.atomic_io.lake_write_guard`, so a full disk becomes a
    :class:`~brutex.data.errors.LakeWriteHalt` (consistent with every lake
    writer, DEC-LAKE-WRITE-HALT-001).

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.locks`` sidecar dir).
    timeout_seconds
        Bounded-acquire deadline; ``None`` resolves the COMMON env-tunable
        default.

    Yields
    ------
    None
        Control returns to the caller while the exclusive lock is held.

    Raises
    ------
    PartitionLockTimeout
        If a peer holds the derive lock past the deadline.
    LakeWriteHalt
        If creating the sidecar lock file hits a full disk (ENOSPC) / OOM.
    """
    deadline = (
        resolve_partition_lock_timeout_seconds() if timeout_seconds is None else timeout_seconds
    )
    lock_path = derive_lock_path(lake_root)
    key = str(lock_path)
    with _REENTRANT_MUTEX:
        nested = _HELD_DEPTHS.get(key, 0) > 0
        if nested:
            _HELD_DEPTHS[key] += 1
    if nested:
        # This PROCESS already holds the exclusive flock (the parallel
        # prebuild's whole-run hold) — ride it, never a second flock that
        # would self-serialise the thread pool.
        try:
            yield
        finally:
            with _REENTRANT_MUTEX:
                _HELD_DEPTHS[key] -= 1
        return
    with lake_write_guard():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600 — owner-only: a private coordination sidecar, never
        # world-readable (mirrors partition_write_lock).
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _acquire_bounded(fd, timeout_seconds=deadline)
        with _REENTRANT_MUTEX:
            _HELD_DEPTHS[key] = _HELD_DEPTHS.get(key, 0) + 1
        try:
            yield
        finally:
            with _REENTRANT_MUTEX:
                _HELD_DEPTHS[key] -= 1
                if _HELD_DEPTHS[key] <= 0:
                    del _HELD_DEPTHS[key]
    finally:
        # Closing the fd releases the advisory lock (flock is tied to the open
        # file description) on every exit — success or error.
        os.close(fd)


def derive_in_progress(lake_root: Path) -> bool:
    """Return ``True`` iff a derive writer currently holds the exclusive lock.

    A NON-BLOCKING shared probe — O(1), never waits, never writes:

    * lock file ABSENT → ``False`` without creating it (a read-only / never-
      derived lake must never be written by a reader probe);
    * ``LOCK_SH | LOCK_NB`` REFUSED (``BlockingIOError``) → a writer holds the
      exclusive flock → ``True``;
    * granted → released immediately (fd close) → ``False``.

    The open is ``O_RDONLY`` and races gracefully: a file deleted between the
    existence check and the open reads as ``False`` (no writer). HONEST
    envelope: the probe is a point-in-time signal — a derive STARTING right
    after a ``False`` is caught by the NEXT poll, exactly like the
    process-local ``PrebuildGate``'s 5 s auto-retry semantics.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).

    Returns
    -------
    bool
        ``True`` while a cross-process derive holds the exclusive lock.
    """
    lock_path = derive_lock_path(lake_root)
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)
