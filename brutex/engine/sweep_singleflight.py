"""Cross-process single-flight lock for a CLI ``brutex sweep-all`` run.

The dashboard serialises its sweeps through :class:`~brutex.dashboard.pull_jobs.\
BackgroundPullJob` + the account/lake flocks in
:mod:`brutex.adapters.groww.pull_singleflight`, but the bare CLI
``brutex sweep-all`` path (:func:`brutex.cli.cmd_sweep_all`) took NO lock. Two
concurrent CLI sweeps sharing one ``--results-root`` each ``resume``-open the
SAME hash-chained audit writers (``<out_root>/audit/sweep-all.jsonl`` +
``sweep-all-supervisor.jsonl``), so both append onto the same chain from the
same starting hash, FORK it into two divergent branches, and each exit ``0`` —
a silent audit-integrity corruption a later ``sweep-verify`` could not even
detect until its audit-chain tier (DEFECT 1b, this branch) landed.

This module adds the CLI's cross-process single-flight gate, keyed by the
results root (the shared artifact the two sweeps contend on). It REUSES the ONE
COMMON non-blocking flock core — :func:`~brutex.adapters.groww.pull_singleflight.\
flock_singleflight` (lake-write-guarded ``.locks/`` sidecar, ``LOCK_EX |
LOCK_NB``, fd-close release, kernel-released on process death) — rather than
hand-rolling a second flock. On contention it translates the pull-flavoured
:class:`~brutex.adapters.groww.pull_singleflight.PullAlreadyRunningError` into a
sweep-flavoured :class:`SweepAlreadyRunningError` so the CLI refuses with a clear
"another sweep is already running on this results root" message.

**Honest envelope (Golden Rule #10):** an ``fcntl.flock`` is advisory and local
to one machine / one filesystem — this serialises CLI sweeps that share a
results root ON ONE HOST, exactly the account-pull single-flight's envelope
(POSIX Linux + macOS; a network volume's cross-host coordination and Windows
are out of scope). It REFUSES the second sweep (fail-fast), it does not queue
it.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    flock_singleflight,
)

__all__ = [
    "SWEEP_SINGLEFLIGHT_LOCK_NAME",
    "SweepAlreadyRunningError",
    "sweep_singleflight_lock",
    "sweep_singleflight_lock_path",
]

#: Sidecar lock directory under the results root. The same dot-prefixed ``.locks``
#: convention as the pull single-flight (a sibling of ``audit/`` etc.) so no
#: results-tree glob / ``sweep-verify`` scanner ever matches it.
_LOCK_SUBDIR: Final[str] = ".locks"

#: The single, stable filename for the sweep-all single-flight lock — one name (not
#: per-tuple) so EVERY ``sweep-all`` against this results root contends on one file.
SWEEP_SINGLEFLIGHT_LOCK_NAME: Final[str] = "sweep_all.lock"


class SweepAlreadyRunningError(Exception):
    """Another sweep already holds the single-flight lock for this results root.

    Raised by :func:`sweep_singleflight_lock` when the non-blocking ``flock``
    acquire finds the lock held — a peer ``brutex sweep-all`` is mid-run against
    the same ``--results-root``. The caller must REFUSE to start a second sweep
    rather than fork the shared audit hash chain. ``lock_path`` is the contended
    lock file (non-secret) for the operator-facing message + the log.
    """

    def __init__(self, lock_path: Path, message: str | None = None) -> None:
        """*message* ``None`` composes the default sweep refusal wording."""
        super().__init__(
            message
            if message is not None
            else (
                "another sweep is already running on this results root "
                f"(single-flight lock held at {lock_path}); refusing to start a "
                "second concurrent sweep, which would fork the shared audit hash chain"
            )
        )
        self.lock_path = lock_path


def sweep_singleflight_lock_path(out_root: Path) -> Path:
    """Return the stable sweep single-flight lock path under *out_root*.

    A SINGLE fixed path (``<out_root>/.locks/sweep_all.lock``) — not keyed by
    tuple — so every ``sweep-all`` against this results root contends on one
    file. O(1) path computation; nothing is created here.
    """
    return out_root / _LOCK_SUBDIR / SWEEP_SINGLEFLIGHT_LOCK_NAME


@contextmanager
def sweep_singleflight_lock(out_root: Path) -> Generator[None, None, None]:
    """Hold the sweep-all single-flight lock for *out_root* for the ``with`` block.

    Acquires the COMMON non-blocking exclusive ``flock``
    (:func:`~brutex.adapters.groww.pull_singleflight.flock_singleflight`) on the
    stable lock file, releasing it by closing the fd on exit. Only ONE
    ``sweep-all`` may hold this at a time per results root, so a SECOND concurrent
    sweep cannot resume-open the shared audit chain and fork it.

    The pull-flavoured
    :class:`~brutex.adapters.groww.pull_singleflight.PullAlreadyRunningError`
    raised by the shared core on the ACQUIRE is translated into the
    sweep-flavoured :class:`SweepAlreadyRunningError`; a body exception (raised
    while the lock is held) propagates unchanged — the translation is scoped to
    acquisition alone.

    Parameters
    ----------
    out_root
        The sweep results root (locates the ``.locks`` sidecar dir). The lock
        file + directory are created on first use.

    Raises
    ------
    SweepAlreadyRunningError
        If the lock is already held by another process (a sweep is in flight) —
        raised immediately (non-blocking).
    LakeWriteHalt
        If creating the ``.locks`` sidecar dir / lock file hits ENOSPC / OOM
        (the shared flock core's :func:`~brutex.data.atomic_io.lake_write_guard`
        contract).
    """
    lock_path = sweep_singleflight_lock_path(out_root)
    inner = flock_singleflight(lock_path)
    try:
        inner.__enter__()
    except PullAlreadyRunningError as exc:
        raise SweepAlreadyRunningError(lock_path) from exc
    try:
        yield
    finally:
        inner.__exit__(None, None, None)
