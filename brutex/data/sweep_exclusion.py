"""Sweep-vs-pull exclusion flock — write-free probes + the sweep run guard.

DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 (Phase 1, D1 Option B — BUILD-SPEC §8). A
brute-force sweep and a live pull against the SAME lake must exclude each
other ("lock wins"), race-free and fail-closed:

* **Sweep side** — :func:`sweep_run_guard` probes the pull + derive locks
  (ZERO writes on a refusal), then creates + holds ``LOCK_SH | LOCK_NB`` on
  the NEW ``<lake>/.locks/sweep.lock`` for the WHOLE run, then RE-probes
  (TOCTOU close). ``LOCK_SH`` deliberately — concurrent sweeps coexist; the
  G6 sweep single-flight is an honest P2 item, NOT smuggled here.
* **Pull side** — :func:`check_pull_exclusion` is the one-liner both
  production pull sites (``pull_jobs._acquire_singleflight`` +
  ``cli._enter_pull_singleflight``) call pre-acquire AND post-acquire: a held
  sweep ``LOCK_SH`` refuses the pull with :class:`SweepRunningError`.
* **Probes** — :func:`pull_in_progress` / :func:`sweep_in_progress` are
  WRITE-FREE (``os.open(O_RDONLY)``; ``FileNotFoundError → False`` WITHOUT
  creating anything — the :func:`~brutex.data.derive_lock.derive_in_progress`
  shape). NEVER the writing ``probe_pull_singleflight`` (it CREATES the lock
  file under ``lake_write_guard`` — ``pull_intent.py``); that function is
  imported by NO preflight module (regression-pinned).

**Race law (test-pinned):** every interleaving of simultaneous starts ends
with BOTH sides refusing (fail-closed, never a double-run): each side
acquires its own lock then RE-probes the other's — a peer that slipped in
between probe and re-probe is caught on both sides.

House lock doctrine (non-negotiable): POSIX ``fcntl.flock`` only (Linux +
macOS — the Track-1 envelope; Windows out of scope); the kernel releases on
fd-close / process death — no PID files, no stale-lock recovery; the lock
file is a STABLE ``.locks/`` sidecar, 0o600, never ``os.replace``-d; creation
runs under :func:`~brutex.data.atomic_io.lake_write_guard` (ENOSPC/OOM →
``LakeWriteHalt``); job-level acquires are ``LOCK_NB`` fail-fast — refuse,
never wait. Dry-run/inspect pulls take no locks AND write nothing —
undetectable, harmless, documented (BUILD-SPEC §13.9).
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.atomic_io import lake_write_guard
from brutex.data.derive_lock import derive_in_progress, derive_lock_path
from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Generator
    from pathlib import Path

__all__ = [
    "PULL_SINGLEFLIGHT_LOCK_NAME",
    "SWEEP_LOCK_FILENAME",
    "PullActiveError",
    "SweepRunningError",
    "check_pull_exclusion",
    "pull_in_progress",
    "pull_lock_path",
    "sweep_in_progress",
    "sweep_lock_path",
    "sweep_run_guard",
]

_LOG = structlog.get_logger("brutex.data.sweep_exclusion")

#: Sidecar lock directory under the lake root — mirrors
#: ``partition_lock._LOCK_SUBDIR`` / ``pull_singleflight._LOCK_SUBDIR`` (the
#: value is part of the on-disk lake layout contract, restated per module —
#: the ``derive_lock.py`` convention; byte-equality is drift-pinned by test.
#: The TEST may import ``brutex.adapters``; this module must not —
#: ``brutex.data`` never imports ``brutex.adapters``, the ``partition_lock``
#: layering law).
_LOCK_SUBDIR: Final[str] = ".locks"

#: The pull single-flight lock filename, restated from
#: ``brutex.adapters.groww.pull_singleflight.PULL_SINGLEFLIGHT_LOCK_NAME``
#: (drift-pinned by test — see the layering note on :data:`_LOCK_SUBDIR`).
#: Every real production pull holds ``LOCK_EX`` on it for its WHOLE duration.
PULL_SINGLEFLIGHT_LOCK_NAME: Final[str] = "pull_singleflight.lock"

#: The NEW sweep-run lock filename — held ``LOCK_SH`` for the whole sweep run
#: (shared: concurrent sweeps coexist; the pull's ``LOCK_EX`` acquire refuses
#: while ANY sweep holds it).
SWEEP_LOCK_FILENAME: Final[str] = "sweep.lock"


class PullActiveError(BruteXError):
    """A pull / prebuild is running for this lake — the SWEEP refuses (lock wins).

    ``lock_path`` is the contended lock file (non-secret) — the pull
    single-flight lock, the derive lock, or the sweep lock itself when a
    peer's momentary exclusive probe bounced the shared acquire (fail-closed).
    """

    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            f"a pull/prebuild is running for this lake (lock held at {lock_path}) — "
            "lock wins; wait for it to finish (watch /universe), then re-run the sweep"
        )
        self.lock_path = lock_path


class SweepRunningError(BruteXError):
    """A sweep is running for this lake — the PULL refuses (sweep-vs-pull exclusion).

    ``lock_path`` is the held sweep lock file (non-secret) for the
    operator-facing message + the log.
    """

    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            f"a sweep is running for this lake (lock held at {lock_path}) — the pull "
            "refuses (sweep-vs-pull exclusion); re-run after the sweep completes"
        )
        self.lock_path = lock_path


def sweep_lock_path(lake_root: Path) -> Path:
    """Return the FIXED sweep-run lock sidecar path for *lake_root*.

    ``<lake_root>/.locks/sweep.lock`` — a stable, never-replaced file (opened
    + flocked only). Never created here.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).

    Returns
    -------
    Path
        The sweep lock file path (not created).
    """
    return lake_root / _LOCK_SUBDIR / SWEEP_LOCK_FILENAME


def pull_lock_path(lake_root: Path) -> Path:
    """Return the pull single-flight lock path for *lake_root* (restated, probed only).

    ``<lake_root>/.locks/pull_singleflight.lock`` — the SAME path
    ``pull_singleflight_lock_path`` derives adapter-side (drift-pinned by
    test). This module only ever PROBES it (never creates, never holds).

    Parameters
    ----------
    lake_root
        Top-level lake directory.

    Returns
    -------
    Path
        The pull single-flight lock file path (not created).
    """
    return lake_root / _LOCK_SUBDIR / PULL_SINGLEFLIGHT_LOCK_NAME


def _probe_lock_held(lock_path: Path, *, mode: int) -> bool:
    """WRITE-FREE non-blocking probe: is a conflicting flock held on *lock_path*?

    The :func:`~brutex.data.derive_lock.derive_in_progress` shape: the open is
    ``O_RDONLY`` and ``FileNotFoundError → False`` WITHOUT creating the file (a
    read-only / never-locked lake is never written by a probe). *mode* is the
    non-blocking flock to ATTEMPT (``LOCK_SH`` detects an exclusive holder;
    ``LOCK_EX`` detects any holder incl. shared) — refused
    (``BlockingIOError``) ⇒ ``True``; granted ⇒ released immediately by the fd
    close ⇒ ``False``. Any OTHER ``OSError`` (an unreadable ``.locks``)
    propagates LOUDLY — never a silent ``False``. O(1); flock conflicts are
    per OPEN FILE DESCRIPTION, so a same-process holder is detected too.
    """
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def pull_in_progress(lake_root: Path) -> bool:
    """Return ``True`` iff a real pull currently holds the pull single-flight lock.

    A WRITE-FREE ``O_RDONLY`` + ``LOCK_SH | LOCK_NB`` probe of
    :func:`pull_lock_path` (every real production pull — dashboard AND CLI —
    holds ``LOCK_EX`` on it for its whole duration). Deliberately NOT the
    writing ``probe_pull_singleflight`` (which CREATES the lock file under
    ``lake_write_guard`` — ``pull_intent.py``). Absent file ⇒ ``False``
    without creating it. HONEST envelope: a point-in-time signal — the guard's
    probe → hold → RE-probe protocol closes the launch race; a dry-run /
    inspect pull takes no locks (and writes nothing) so it is undetectable by
    design.

    Parameters
    ----------
    lake_root
        Top-level lake directory.

    Returns
    -------
    bool
        ``True`` while a real pull holds the exclusive pull lock.
    """
    return _probe_lock_held(pull_lock_path(lake_root), mode=fcntl.LOCK_SH)


def sweep_in_progress(lake_root: Path) -> bool:
    """Return ``True`` iff a sweep currently holds the shared sweep lock.

    A WRITE-FREE ``O_RDONLY`` probe of :func:`sweep_lock_path` attempting
    ``LOCK_EX | LOCK_NB`` (valid POSIX on an ``O_RDONLY`` fd — flock ignores
    the open mode): refused ⇒ a sweep holds ``LOCK_SH`` ⇒ ``True``; granted ⇒
    released immediately ⇒ ``False``. Absent file ⇒ ``False`` without
    creating it. The momentary exclusive grant this probe takes is exactly the
    "peer's momentary EX probe" a launching sweep's shared acquire treats as
    :class:`PullActiveError` (fail-closed — both sides refuse).

    Parameters
    ----------
    lake_root
        Top-level lake directory.

    Returns
    -------
    bool
        ``True`` while a sweep holds the shared sweep lock.
    """
    return _probe_lock_held(sweep_lock_path(lake_root), mode=fcntl.LOCK_EX)


def check_pull_exclusion(lake_root: Path) -> None:
    """The PULL-side exclusion check — raise :class:`SweepRunningError` if a sweep runs.

    Called by both production pull sites (``pull_jobs._acquire_singleflight``
    + ``cli._enter_pull_singleflight``) BEFORE acquiring their own locks (fast
    honest refusal) AND again AFTER (the TOCTOU re-probe — a sweep that
    slipped in mid-acquire is caught, the pull rolls its locks back LIFO and
    refuses; both sides refuse, fail-closed). Emits one structlog
    ``pull.sweep_exclusion_refused`` on refusal. Write-free.

    Parameters
    ----------
    lake_root
        Top-level lake directory.

    Raises
    ------
    SweepRunningError
        If a sweep currently holds the shared sweep lock.
    """
    lock_path = sweep_lock_path(lake_root)
    if sweep_in_progress(lake_root):
        _LOG.warning("pull.sweep_exclusion_refused", lock_path=str(lock_path))
        raise SweepRunningError(lock_path)


def _refuse_if_pull_active(lake_root: Path) -> None:
    """Raise :class:`PullActiveError` if a pull OR a derive is in flight — write-free.

    The sweep-side probe pair: the pull single-flight lock
    (:func:`pull_in_progress`) covers every real pull end-to-end; the derive
    lock (:func:`~brutex.data.derive_lock.derive_in_progress`, reused
    VERBATIM) additionally catches a standalone prebuild/derive. Emits one
    structlog ``sweep.preflight.lock_refused`` naming the holder before
    raising.
    """
    if pull_in_progress(lake_root):
        held = pull_lock_path(lake_root)
        _LOG.warning("sweep.preflight.lock_refused", lock_path=str(held), holder="pull_in_progress")
        raise PullActiveError(held)
    if derive_in_progress(lake_root):
        held = derive_lock_path(lake_root)
        _LOG.warning(
            "sweep.preflight.lock_refused", lock_path=str(held), holder="derive_in_progress"
        )
        raise PullActiveError(held)


@contextmanager
def sweep_run_guard(lake_root: Path) -> Generator[None, None, None]:
    """Hold the shared sweep lock for the WHOLE sweep run — probe → hold → RE-probe.

    The sweep-side exclusion protocol (BUILD-SPEC §8, all steps test-pinned):

    1. probe pull + derive (:func:`_refuse_if_pull_active`) — a refusal here
       writes NOTHING, not even ``sweep.lock``;
    2. create + hold ``LOCK_SH | LOCK_NB`` on :func:`sweep_lock_path`
       (``mkdir`` + ``O_CREAT`` under
       :func:`~brutex.data.atomic_io.lake_write_guard`, mode ``0o600``, a
       stable sidecar never ``os.replace``-d). A concurrent sweep's SH
       coexists (G6 single-flight stays P2). A ``BlockingIOError`` here — a
       peer's momentary exclusive probe — is treated as
       :class:`PullActiveError` (fail-closed);
    3. RE-probe pull + derive; active ⇒ release the fd + raise
       :class:`PullActiveError` (TOCTOU close: simultaneous starts ⇒ BOTH
       sides refuse — fail-closed, no double-run);
    4. yield; ``finally`` ``os.close(fd)`` — the kernel releases the flock on
       fd-close / process death (no PID files, no stale-lock recovery).

    Parameters
    ----------
    lake_root
        Top-level lake directory.

    Yields
    ------
    None
        Control returns to the caller while the shared sweep lock is held.

    Raises
    ------
    PullActiveError
        If a pull / derive is in flight (either probe), or a peer's momentary
        exclusive probe bounced the shared acquire.
    brutex.data.errors.LakeWriteHalt
        If creating the sidecar lock file hits a full disk (ENOSPC) / OOM.
    """
    _refuse_if_pull_active(lake_root)
    lock_path = sweep_lock_path(lake_root)
    with lake_write_guard():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600 — owner-only: a private coordination sidecar, never
        # world-readable (mirrors partition_write_lock / derive_write_lock).
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            _LOG.warning(
                "sweep.preflight.lock_refused",
                lock_path=str(lock_path),
                holder="sweep_lock_contended",
            )
            raise PullActiveError(lock_path) from exc
        _refuse_if_pull_active(lake_root)
        yield
    finally:
        # Closing the fd releases the shared flock (flock is tied to the open
        # file description) on every exit — success or error.
        os.close(fd)
