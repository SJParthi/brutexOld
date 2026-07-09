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

The manager YIELDS ``True`` iff the assertion is actually ENGAGED (darwin +
caffeinate spawned) and ``False`` on every graceful unavailable no-op path, so
a caller printing operator-facing claims can tell the truth about whether the
host is really held awake (Golden Rule #10 — the serve banner reads it).

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
    "SERVE_KEEP_AWAKE_ENV",
    "TERMINATE_TIMEOUT_S",
    "KeepAwakeProcess",
    "keep_awake",
    "serve_keep_awake_enabled",
]

#: The macOS power-assertion binary (PATH-resolved; a missing binary is the
#: graceful ``power.keepawake.unavailable`` path, never an error).
CAFFEINATE_BINARY: Final[str] = "caffeinate"

#: Opt-in env flag holding a keep-awake assertion for the LIFE of ``brutex serve``
#: (DEC-AUDIT-ALERT-CLAMP-OBS-001). Default OFF: today ``keep_awake`` is held ONLY
#: during a pull, so once the pull releases it macOS sleep can take the whole
#: serving dashboard down (the real 8h10m dark-gap incident). This flag is OPT-IN
#: on purpose — silently changing a machine's power behaviour just by starting a
#: server is user-hostile, so the default keeps macOS's normal sleep policy and
#: the serve boot logs an HONEST banner stating the server does NOT prevent sleep.
SERVE_KEEP_AWAKE_ENV: Final[str] = "BRUTEX_SERVE_KEEP_AWAKE"

#: The truthy env-literal vocabulary (mirrors the repo convention —
#: ``brutex/engine/sweep_lookahead.py`` / ``brutex/data/bad_tick.py``).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

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


def serve_keep_awake_enabled(raw: str | None = None) -> bool:
    """Whether ``brutex serve`` should hold a keep-awake assertion for its lifetime.

    OPT-IN, default OFF (DEC-AUDIT-ALERT-CLAMP-OBS-001): only an explicit truthy
    :data:`SERVE_KEEP_AWAKE_ENV` (``1/true/yes/on``, case-insensitive) enables it.
    Any other value — unset, empty, ``0/false/no/off``, or junk — is OFF, so the
    server never silently changes the host's power policy and the serve boot logs
    an honest "does NOT prevent sleep" banner (never raises; O(1)).

    Parameters
    ----------
    raw
        The raw env value (``None`` reads :data:`SERVE_KEEP_AWAKE_ENV` from
        ``os.environ``; pass explicitly in tests).
    """
    value = os.environ.get(SERVE_KEEP_AWAKE_ENV) if raw is None else raw
    return value is not None and value.strip().lower() in _TRUTHY


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
) -> Generator[bool]:
    """Hold the macOS host awake for the enclosed block — best-effort, never raises.

    Yields
    ------
    bool
        ``True`` iff the power assertion is actually ENGAGED (darwin AND
        caffeinate spawned); ``False`` on the graceful unavailable no-op paths
        (non-darwin host, or the spawn failed). Callers that print
        operator-facing claims MUST reflect this — never state "holds the host
        awake" over a ``False`` (Golden Rule #10).

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
        yield proc is not None
    finally:
        if proc is not None:
            _release(proc, reason=reason)
