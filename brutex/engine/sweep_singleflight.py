"""Cross-process sweep single-flight + lake read-guard (G6/G10 — DEC-SWEEP-UNKILLABLE-CORE-001).

Two flock gates for the unkillable run core, both reusing the proven
non-blocking advisory-``flock`` idiom of
:mod:`brutex.data.partition_lock` / :mod:`brutex.adapters.groww.pull_singleflight`
(stable sidecar file, never replaced; ``LOCK_NB`` refuse-not-queue; kernel
releases on fd-close OR process death, so a SIGKILLed holder never leaves a
stale lock):

* **G6 — sweep single-flight** (:func:`sweep_run_lock`). Two concurrent
  ``cmd_sweep_all`` runs against ONE results root would both
  ``AuditWriter.open(resume=True)`` the CLI-level sweep chain and interleave
  appends — forking the hash chain (the single-writer contract of
  :mod:`brutex.orchestrator.audit`). The run core takes an exclusive
  ``LOCK_EX | LOCK_NB`` on ``<results_root>/.locks/sweep_singleflight.lock``
  BEFORE any writer opens; contention raises the loud
  :class:`SweepAlreadyRunningError` naming the lock path + the holder pid
  (advertised best-effort in the lock file by the winner).
* **G10 — lake read-guard** (:func:`lake_read_guard`). The sweep's bar
  PRELOAD racing a concurrent pull/heal rewriting the same partitions reads
  torn state. The guard takes a SHARED ``LOCK_SH | LOCK_NB`` on the EXISTING
  pull single-flight lock file
  (:func:`~brutex.adapters.groww.pull_singleflight.pull_singleflight_lock_path`
  — a pure path import; ZERO pull-side edits). SH/EX compose natively: a pull
  holding EX makes our SH acquire fail → :class:`LakeBusyError`; while WE hold
  SH, a starting pull's EX|NB fails → its existing loud refusal. The guard is
  held across the PRELOAD ONLY (bounded hold) — once the bars are in memory
  the sweep is immune to lake writes.

WORDING DEBT (D-13, pinned): while a sweep holds the SH guard, a starting
pull's refusal message still says "a pull is already running" (the peer-pull
wording of :class:`~brutex.adapters.groww.pull_singleflight.PullAlreadyRunningError`)
— misleading but SAFE (the refusal itself is correct). Named follow-up
DEC-SWEEP-LOCK-WORDING-001; the wording is pinned by
``tests/unit/test_engine_sweep_singleflight.py``.

Honest envelope (Golden Rule #10): advisory ``flock`` is POSIX
(Linux + macOS — the Track-1 hardware envelope; Windows out of scope) and is
LOCAL to one machine/filesystem — flock semantics are weaker on network
volumes (NFS), the same envelope as the partition lock. The holder-pid
advertisement is best-effort observability, never load-bearing: mutual
exclusion is the kernel's flock, not the pid bytes.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import structlog

from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock_path
from brutex.data.guarded_read import read_regular_text
from brutex.models import BruteXError

__all__ = [
    "SWEEP_SINGLEFLIGHT_LOCK_NAME",
    "LakeBusyError",
    "SweepAlreadyRunningError",
    "lake_read_guard",
    "sweep_run_lock",
    "sweep_run_lock_path",
]

_LOG = structlog.get_logger("brutex.engine.sweep_singleflight")

#: Sidecar lock directory under the results root (mirrors the lake writers'
#: dot-prefixed ``.locks`` convention — no results scanner ever globs it).
_LOCK_SUBDIR: Final[str] = ".locks"

#: The single, stable filename for the results-root sweep single-flight lock.
SWEEP_SINGLEFLIGHT_LOCK_NAME: Final[str] = "sweep_singleflight.lock"


class SweepAlreadyRunningError(BruteXError):
    """Another sweep already holds the single-flight lock for this results root.

    Raised by :func:`sweep_run_lock` on a contended non-blocking acquire — a
    peer process is mid-sweep against the same results root. The caller must
    REFUSE at launch (before any chain writer opens) rather than fork the
    hash-chained sweep audit (G6). ``lock_path`` is the contended lock file;
    ``holder_pid`` is the winner's advertised pid when readable, else ``None``.
    """

    def __init__(self, lock_path: Path, holder_pid: int | None) -> None:
        pid_part = f" (holder pid {holder_pid})" if holder_pid is not None else ""
        super().__init__(
            f"a sweep is already running against this results root{pid_part} "
            f"(single-flight lock held at {lock_path}); refusing to start a second "
            "concurrent sweep, which would interleave appends and fork the "
            "hash-chained sweep audit"
        )
        self.lock_path = lock_path
        self.holder_pid = holder_pid


class LakeBusyError(BruteXError):
    """A pull/heal holds the lake's pull lock exclusively — the preload must wait.

    Raised by :func:`lake_read_guard` when the shared non-blocking acquire on
    the EXISTING pull single-flight lock fails: a peer pull/heal is mid-write
    against this lake, so preloading sweep bars now would read torn partitions
    (G10). The caller refuses at launch with this actionable message.
    """

    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            "a pull/heal is running against this lake — wait for it to finish or "
            f"stop it (pull single-flight lock held exclusively at {lock_path}); "
            "refusing to preload sweep bars over a partition tree being rewritten"
        )
        self.lock_path = lock_path


def sweep_run_lock_path(results_root: Path) -> Path:
    """Return the stable sweep single-flight lock path under *results_root*.

    A SINGLE fixed path (``<results_root>/.locks/sweep_singleflight.lock``) so
    every sweep against this results root contends on the same file. O(1);
    nothing is created here.
    """
    return results_root / _LOCK_SUBDIR / SWEEP_SINGLEFLIGHT_LOCK_NAME


def _read_holder_pid(lock_path: Path) -> int | None:
    """Best-effort read of the winner's advertised pid from *lock_path*.

    Returns ``None`` on any read/parse failure (empty file, junk bytes, an
    unreadable file) — the pid is observability for the refusal message, never
    load-bearing (the flock itself is the mutual exclusion).
    """
    try:
        raw = read_regular_text(lock_path).strip()
        return int(raw)
    except (OSError, ValueError):
        return None


@contextmanager
def sweep_run_lock(results_root: Path) -> Generator[None, None, None]:
    """Hold the results-root sweep single-flight lock for the ``with`` block (G6).

    Opens (creating on first use, ``0o600``) the stable lock file and takes a
    NON-BLOCKING exclusive ``flock`` on it, releasing by closing the fd on
    every exit. Exactly ONE sweep may hold it per results root; the winner
    best-effort advertises its pid into the file so the loser's refusal can
    name it. The kernel releases the flock on fd-close OR process death, so a
    SIGKILLed sweep never leaves a stale lock.

    Parameters
    ----------
    results_root
        The sweep results root (locates the ``.locks`` sidecar dir, created
        with ``parents=True, exist_ok=True``).

    Yields
    ------
    None
        Control returns to the caller while the exclusive lock is held.

    Raises
    ------
    SweepAlreadyRunningError
        If a peer process holds the lock (a sweep is in flight) — raised
        immediately (non-blocking) so the launch refuses BEFORE any chain
        writer opens.
    OSError
        If creating the lock dir/file fails (e.g. a read-only or full
        volume) — a loud launch-time refusal, deliberately not remapped.
    """
    lock_path = sweep_run_lock_path(results_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 0o600 — owner-only: a private coordination sidecar, never world-readable.
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SweepAlreadyRunningError(lock_path, _read_holder_pid(lock_path)) from exc
        try:
            # Best-effort holder-pid advertisement for the loser's refusal
            # message — observability only, never load-bearing.
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError as exc:
            _LOG.warning(
                "sweep_singleflight.pid_advertise_failed",
                lock_path=str(lock_path),
                error=str(exc),
            )
        yield
    finally:
        # Closing the fd releases the advisory lock — on the contended path the
        # lock was never taken by THIS fd, so the close never touches the peer's.
        os.close(fd)


@contextmanager
def lake_read_guard(lake_root: Path) -> Generator[None, None, None]:
    """Hold a SHARED read-guard on the lake's pull single-flight lock (G10).

    Takes ``LOCK_SH | LOCK_NB`` on the EXISTING
    ``<lake_root>/.locks/pull_singleflight.lock`` (path via the pure
    :func:`~brutex.adapters.groww.pull_singleflight.pull_singleflight_lock_path`
    import — zero pull-side edits). A pull/heal holding the exclusive lock
    makes this acquire fail → :class:`LakeBusyError`; while the guard is held,
    a starting pull's exclusive acquire fails with ITS existing loud refusal
    (wording debt pinned — D-13 / DEC-SWEEP-LOCK-WORDING-001). Multiple shared
    holders compose (two concurrent readers never contend).

    Hold this across the sweep's bar PRELOAD ONLY (bounded hold): once the
    bars are in memory the sweep is immune to lake writes, and a long hold
    would needlessly refuse legitimate pulls.

    Parameters
    ----------
    lake_root
        The lake root (locates the existing pull lock sidecar; the ``.locks``
        dir is created with ``parents=True, exist_ok=True`` when absent).

    Yields
    ------
    None
        Control returns to the caller while the shared lock is held.

    Raises
    ------
    LakeBusyError
        If a pull/heal holds the lock exclusively — raised immediately
        (non-blocking) so the launch refuses instead of reading torn state.
    OSError
        If creating the lock dir/file fails — a loud launch-time refusal.
    """
    lock_path = pull_singleflight_lock_path(lake_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 0o600 — owner-only (matches the pull side's creation mode).
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LakeBusyError(lock_path) from exc
        yield
    finally:
        os.close(fd)
