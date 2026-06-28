"""Cross-process single-flight lock for a Groww account pull (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).

The Groww rate controller (:class:`~brutex.adapters.rate_common.RateController`) paces a
pull within ONE process: an adaptive per-second token bucket + a 500 req/min governor
(CLAUDE.md §2 Rule 6). That budget is **per process**, so two pulls running at the same
time — e.g. a CLI ``brutex fetch`` AND a dashboard pull sharing one Groww account /
one ``BRUTEX_LAKE_ROOT`` — each get their OWN ceiling, and the Groww ACCOUNT sees the
SUM of both rates (up to ~16 r/s at the 8 r/s production ceiling). That doubles the
account's request rate, defeating the rate safety the controller exists to provide, and
risks a sustained 429 storm or an account ban (CLAUDE.md Golden Rule #6).

This module adds a **cross-process single-flight gate**: only ONE pull may run at a
time per account/lake. It takes a NON-BLOCKING advisory :func:`fcntl.flock`
(``LOCK_EX | LOCK_NB``) on a stable lock file under the lake root
(``<lake_root>/.locks/pull_singleflight.lock``). If the lock is already held — a peer
process is mid-pull — the acquire RAISES :class:`PullAlreadyRunningError` immediately
rather than blocking or silently doubling the account rate; the caller refuses to start
the second pull and surfaces a clear, actionable message.

Design notes (COMMON · O(1) · honest):

* **Account-wide, not per-partition.** Unlike
  :func:`~brutex.data.partition_lock.partition_write_lock` (one lock PER month
  partition, so the pull's fan-out keeps its parallelism), this is ONE lock for the
  WHOLE pull — it must serialise the entire account's request stream, not a single
  file. The two locks are complementary: this gate stops a SECOND concurrent pull from
  starting; the partition lock still serialises any same-partition writers that remain.
* **Non-blocking, fail-fast.** A pull is a long (often multi-hour) job; a blocking
  acquire would silently queue a second pull behind the first for hours. Instead the
  acquire is ``LOCK_NB`` and raises on contention, so the operator gets an immediate
  "a pull is already running" instead of a mysteriously stalled second pull.
* **Stable lock file, never replaced.** The lock file is opened + locked, never
  ``os.replace``-d, so the ``flock`` always guards the same inode for the life of the
  process (mirroring the partition-lock sidecar reasoning). It lives under the
  dot-prefixed ``.locks/`` sibling of ``bars/`` + ``options/`` so no lake scanner /
  dataset glob ever sees it.
* **No stale-lock recovery needed.** An ``flock`` is released by the kernel when the
  holding fd is closed OR the process dies, so a crashed pull never leaves a stuck lock
  (unlike a PID file). The fd is held for the LIFETIME of the pull and closed (which
  releases the lock) when the pull finishes or errors.

**Honest envelope (Golden Rule #10):** this guards against TWO concurrent pulls sharing
one lake root; it is the account-rate twin of the partition-write lock, NOT a
distributed lock across hosts (an ``flock`` is local to one machine / one filesystem)
and NOT a queue (the second pull is REFUSED, not deferred). ``fcntl.flock`` is POSIX
(Linux + macOS — the Track-1 hardware envelope); Windows is out of scope.

**RESIDUAL — keyed by lake root, NOT by the Groww account (DEC-PULL-HARDENING-FOLLOWUP-001,
MIGRATION.md #996).** The lock file lives UNDER ``lake_root`` (see
:func:`pull_singleflight_lock_path`), so this gate serialises pulls that share ONE lake
root. The *intent* is the ACCOUNT request rate (Golden Rule #6), but the *key* is the
lake root — so it does NOT cover the case where the SAME Groww account is pulled into
TWO distinct ``BRUTEX_LAKE_ROOT`` s at once: those two pulls take DIFFERENT lock files,
BOTH acquire, and the account still sees the SUM of both rates (~16 r/s at the 8 r/s
ceiling). Keying the lock on the credentials / feed-token instead of the lake root would
need the live ``GrowwAPI`` token threaded into BOTH build sites + a system-wide
cross-user lock dir (``/tmp``-style) with its own permission trade-offs — a larger
change, flagged as a FOLLOW-UP and deliberately NOT done here. **Operators running ONE
account into TWO lake roots must not start both pulls at once — this guard will NOT stop
them.** (See ``docs/runbooks/data-fetch-runday.md`` §6a for the operator note.)
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from brutex.data.atomic_io import lake_write_guard
from brutex.models import BruteXError

__all__ = [
    "PULL_SINGLEFLIGHT_LOCK_NAME",
    "PullAlreadyRunningError",
    "pull_singleflight_lock",
    "pull_singleflight_lock_path",
]

#: Sidecar lock directory under the lake root. A dot-prefixed sibling of ``bars/`` +
#: ``options/`` so it is never matched by a dataset glob or a lake scanner (mirrors
#: :data:`brutex.data.partition_lock._LOCK_SUBDIR`).
_LOCK_SUBDIR: Final[str] = ".locks"

#: The single, stable filename for the account-wide pull single-flight lock. ONE name
#: (not per-partition) so EVERY pull against this lake root contends on the same file.
PULL_SINGLEFLIGHT_LOCK_NAME: Final[str] = "pull_singleflight.lock"


class PullAlreadyRunningError(BruteXError):
    """Another pull already holds the account-wide single-flight lock for this lake root.

    Raised by :func:`pull_singleflight_lock` when the non-blocking ``flock`` acquire
    finds the lock already held — a peer process (a CLI ``brutex fetch`` or a second
    dashboard pull) is mid-pull against the same Groww account / lake. The caller must
    REFUSE to start a second pull rather than double the account's request rate
    (CLAUDE.md Golden Rule #6). ``lock_path`` is the contended lock file (non-secret)
    for the operator-facing message + the log.
    """

    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            "a pull is already running for this lake "
            f"(account-wide single-flight lock held at {lock_path}); "
            "refusing to start a second concurrent pull, which would double the "
            "Groww account request rate"
        )
        self.lock_path = lock_path


def pull_singleflight_lock_path(lake_root: Path) -> Path:
    """Return the stable account-wide single-flight lock path under *lake_root*.

    A SINGLE fixed path (``<lake_root>/.locks/pull_singleflight.lock``) — not keyed by
    partition — so every pull against this lake root contends on the same file. O(1).

    Parameters
    ----------
    lake_root
        The lake root; the ``.locks`` dir lives directly under it.

    Returns
    -------
    Path
        ``<lake_root>/.locks/pull_singleflight.lock`` (never created here).
    """
    return lake_root / _LOCK_SUBDIR / PULL_SINGLEFLIGHT_LOCK_NAME


@contextmanager
def pull_singleflight_lock(lake_root: Path) -> Generator[None, None, None]:
    """Hold the account-wide single-flight lock for the *lake_root* for the ``with`` block.

    Opens (creating on first use) the stable single-flight lock file and takes a
    NON-BLOCKING exclusive :func:`fcntl.flock` (``LOCK_EX | LOCK_NB``) on it for the
    duration of the ``with`` block, releasing it by closing the fd on exit. Only ONE
    pull may hold this at a time per lake root, so a SECOND concurrent pull cannot
    start and double the Groww account request rate (CLAUDE.md Golden Rule #6).

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.locks`` sidecar dir). The lock file is created on
        first use; the directory is created with ``parents=True, exist_ok=True``.

    Yields
    ------
    None
        Control returns to the caller while the exclusive lock is held.

    Raises
    ------
    PullAlreadyRunningError
        If the lock is already held by another process (a pull is in flight) — raised
        immediately (non-blocking), so the caller refuses to start a second pull.
    LakeWriteHalt
        If creating the ``.locks`` sidecar dir / lock file hits a full disk (ENOSPC) /
        OOM. Mapped via :func:`~brutex.data.atomic_io.lake_write_guard` so the disk-full
        contract is consistent with :func:`~brutex.data.partition_lock.partition_write_lock`
        (DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996) — a raw ``OSError`` would
        otherwise escape the dashboard ``start()`` path uncaught.
    """
    lock_path = pull_singleflight_lock_path(lake_root)
    # The lock-file creation runs inside ``lake_write_guard`` so a full disk at that step
    # becomes a ``LakeWriteHalt`` (the same contract the partition lock honours), never a
    # raw ``OSError`` that escapes the caller uncaught.
    with lake_write_guard():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600 — owner-only: a private coordination sidecar, never world-readable
        # (CodeQL py/overly-permissive-file-permission).
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            # The lock is held by a peer process: a pull is already running. Refuse —
            # do NOT block (a pull runs for hours) and do NOT double the account rate.
            raise PullAlreadyRunningError(lock_path) from exc
        yield
    finally:
        # Closing the fd releases the advisory lock — an ``flock`` is tied to the open
        # file description, so the close both unlocks and frees the fd in one step, on
        # every (success or error) exit. On the contended path the lock was never taken
        # by THIS fd, so closing it does not touch the peer's lock.
        os.close(fd)
