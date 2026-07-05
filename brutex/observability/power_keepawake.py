"""macOS keep-awake guard for long pulls — best-effort, never raises.

DEC-PULL-KEEPAWAKE-001 (operator-decreed P0, mechanization-sweep register #5).
A sleeping Mac silently PAUSES the whole pull pipeline (the register cites a
real 6.5 h mid-pull host sleep) — no error, no alert, just a frozen pull that
resumes hours later. macOS provides ``caffeinate`` for exactly this:
``caffeinate -i -s -w <pid>`` takes an idle+system power assertion that lasts
until the watched pid exits, so the host stays awake for the pull's lifetime
and the assertion can never leak past it even if we crash (the ``-w`` watch is
the kernel-side cleanup).

:func:`keep_awake` is the ONE context manager both production pull entrypoints
wrap (CLAUDE.md §11 COMMON — the dashboard ``/pull-pipeline`` job body and the
CLI ``brutex fetch`` pass):

* on ``darwin`` it spawns ``caffeinate -i -s -w <os.getpid()>`` (one
  ``power.keepawake.engaged`` log) and terminates it on exit (one
  ``power.keepawake.released`` log; ``terminate`` → bounded ``wait`` →
  ``kill`` on a wedged process);
* anywhere else — or when the binary is missing / any ``OSError`` fires — it
  logs ONE ``power.keepawake.unavailable`` note and no-ops; the guard can
  NEVER raise into (or mask the outcome of) the pull it protects.

Honest envelope (Golden Rule #10): this holds the HOST awake — it does not and
cannot keep an external SSD from its own power management, nor survive a lid
close with ``caffeinate -i`` alone (``-s`` covers AC-powered sleep; on battery
macOS may still sleep on lid close — a macOS policy, not ours). The spawn /
terminate seams are injectable so the guard is 100% unit-testable with no real
``caffeinate`` in CI.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 — fixed argv, no shell, macOS power assertion only
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Protocol

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_LOG = structlog.get_logger(__name__)

__all__ = [
    "CAFFEINATE_BINARY",
    "TERMINATE_TIMEOUT_S",
    "KeepAwakeProcess",
    "keep_awake",
]

#: The macOS power-assertion binary (PATH-resolved; a missing binary is the
#: graceful ``power.keepawake.unavailable`` path, never an error).
CAFFEINATE_BINARY: Final[str] = "caffeinate"

#: Bound on each ``wait`` after ``terminate``/``kill`` — a wedged caffeinate
#: must never hang the pull's exit path (it dies with us anyway via ``-w``).
TERMINATE_TIMEOUT_S: Final[float] = 5.0


class KeepAwakeProcess(Protocol):
    """The ``subprocess.Popen`` subset the guard drives (injectable for tests)."""

    @property
    def pid(self) -> int:
        """The spawned process id (logged, never signalled directly)."""
        raise NotImplementedError

    def terminate(self) -> None:
        """Send SIGTERM."""

    def kill(self) -> None:
        """Send SIGKILL."""

    def wait(self, timeout: float | None = None) -> int:
        """Wait for exit (bounded); raises ``subprocess.TimeoutExpired`` on timeout."""
        raise NotImplementedError


def _default_spawn(argv: list[str]) -> KeepAwakeProcess:
    """Spawn *argv* detached from our stdio — the production seam default."""
    return subprocess.Popen(  # noqa: S603  # nosec B603 — fixed argv, no shell, no user input
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _release(proc: KeepAwakeProcess, *, reason: str) -> None:
    """Terminate the assertion process — bounded, best-effort, never raises.

    ``terminate`` → ``wait(TERMINATE_TIMEOUT_S)``; a wedged process is
    ``kill``-ed and re-waited (also bounded). Any error is logged
    (``power.keepawake.release_failed``) and swallowed — the release must never
    mask the pull's own outcome, and the ``-w <pid>`` watch means an orphaned
    caffeinate exits with this process regardless.
    """
    try:
        pid = proc.pid
        proc.terminate()
        try:
            proc.wait(timeout=TERMINATE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=TERMINATE_TIMEOUT_S)
        _LOG.info("power.keepawake.released", reason=reason, pid=pid)
    except Exception as exc:  # pylint: disable=broad-except — best-effort release, never masks the pull
        _LOG.warning(
            "power.keepawake.release_failed",
            reason=reason,
            error_type=type(exc).__name__,
            error=str(exc),
        )


@contextmanager
def keep_awake(
    reason: str,
    *,
    spawn: Callable[[list[str]], KeepAwakeProcess] | None = None,
    platform: str | None = None,
) -> Generator[None]:
    """Hold the macOS host awake for the enclosed block — best-effort, never raises.

    Parameters
    ----------
    reason
        A short label naming WHAT is being kept awake (logged on every event,
        e.g. ``"brutex pull pipeline"`` / ``"brutex fetch"``).
    spawn
        Injectable process spawner (defaults to a devnull'd
        ``subprocess.Popen``) — tests inject a fake so CI never runs a real
        ``caffeinate``.
    platform
        Injectable platform token (defaults to ``sys.platform``) — the guard
        engages ONLY on ``"darwin"``.
    """
    plat = platform if platform is not None else sys.platform
    proc: KeepAwakeProcess | None = None
    if plat != "darwin":
        _LOG.info(
            "power.keepawake.unavailable",
            reason=reason,
            platform=plat,
            note="not macOS — no power assertion taken (host sleep policy applies)",
        )
    else:
        argv = [CAFFEINATE_BINARY, "-i", "-s", "-w", str(os.getpid())]
        try:
            proc = (spawn if spawn is not None else _default_spawn)(argv)
        except OSError as exc:
            # FileNotFoundError (no caffeinate on PATH) is an OSError subclass —
            # the whole spawn failure family degrades to the honest no-op.
            _LOG.warning(
                "power.keepawake.unavailable",
                reason=reason,
                platform=plat,
                error_type=type(exc).__name__,
                error=str(exc),
                note="caffeinate could not be spawned — the host MAY sleep mid-pull",
            )
        else:
            _LOG.info(
                "power.keepawake.engaged",
                reason=reason,
                pid=proc.pid,
                note="caffeinate -i -s -w <pid> holds the host awake until released",
            )
    try:
        yield
    finally:
        if proc is not None:
            _release(proc, reason=reason)
