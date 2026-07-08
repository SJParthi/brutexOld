"""Detached sweep launch + liveness probe (R7 tail — DEC-SWEEP-UNKILLABLE-CORE-001).

A dashboard restart must not kill a multi-day sweep (R7 "detached survival").
:func:`spawn_detached_sweep` launches the sweep as its OWN session leader
(``start_new_session=True`` → ``setsid`` in the child), so the parent's death
— dashboard restart, closed terminal, SIGHUP to the parent's group — can
never propagate a signal to it. The child owns its progress file, exit-status
artifact, and Telegram alerts (the run core writes all three); the parent
keeps only a small :class:`DetachedSweepHandle` plus a durable pidfile so a
RESTARTED dashboard can re-discover the run.

Wiring notes: stdin is ``DEVNULL``; stdout+stderr are redirected (append) to a
log file under ``<results_root>/runs/`` — a detached child writing to a dead
tty would otherwise die on ``SIGPIPE``/``EIO``. The pidfile is written
atomically AFTER the spawn (pid, full cmdline, run-id hint, wall start —
observability only, never identity).

:func:`probe_handle` cross-checks pid liveness against the run's published
progress file and answers with the COMMON
:class:`~brutex.engine.sweep_liveness.RunVerdict` — fail-closed: an alive pid
with no readable progress evidence is :attr:`RunVerdict.UNKNOWN` (a recycled
pid must never read as a confirmed live sweep), and a progress file whose
``pid`` differs from the handle's is ``UNKNOWN`` too (SOME sweep runs, but not
provably ours).

Honest envelope (Golden Rule #10): ``start_new_session`` detaches the child
from the parent's SESSION, not from the process tree — while the original
parent lives, the child remains its waitable child (a caller that stays alive
may reap it); once the parent dies the child is reparented to init and keeps
running, which is the survival property R7 needs. This module never kills or
reaps on its own. POSIX only (Linux + macOS — the Track-1 envelope).
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 — list-argv detached launch, no shell
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.sweep_liveness import (
    DEFAULT_FRESH_SECONDS,
    RunVerdict,
    default_pid_alive,
    progress_verdict,
)

__all__ = [
    "DETACHED_ENV_SENTINEL",
    "DETACH_LOG_FILENAME",
    "DETACH_PIDFILE_NAME",
    "DetachedSweepHandle",
    "default_detach_command",
    "default_detach_log_path",
    "default_progress_path",
    "detach_pidfile_path",
    "probe_handle",
    "running_as_detached_child",
    "spawn_detached_sweep",
]

_LOG = structlog.get_logger("brutex.engine.sweep_detach")

#: Default log filename (under ``runs/``) the detached child's stdout+stderr
#: append to. A STABLE name (successive runs append) — never a wall-clock name.
DETACH_LOG_FILENAME: Final[str] = "detached-sweep.log"

#: Durable pidfile (under ``runs/``) a restarted dashboard re-discovers the
#: detached run from. Rewritten atomically per spawn.
DETACH_PIDFILE_NAME: Final[str] = "detached-sweep.pid"

#: Environment sentinel stamped into the detached child's environment. The
#: child honours ``--detach`` ONLY when this sentinel is ABSENT, so an
#: abbreviated flag (argparse's default ``allow_abbrev`` matches ``--det`` /
#: ``--deta`` / ``--detac`` → ``detach=True``) that survives the parent's
#: exact-string argv filter can NEVER re-trigger detach in the child. The
#: sentinel is spelling-immune where a raw ``sys.argv`` string match is not —
#: it closes the fork-bomb re-exec loop (hostile-review round 3, #1594).
DETACHED_ENV_SENTINEL: Final[str] = "BRUTEX_SWEEP_DETACHED"


def running_as_detached_child(env: Mapping[str, str] | None = None) -> bool:
    """Return ``True`` iff this process is already a detached sweep child.

    A detached child carries :data:`DETACHED_ENV_SENTINEL` == ``"1"`` in its
    environment (stamped by :func:`spawn_detached_sweep`). The CLI's
    ``sweep-all`` handler consults this BEFORE re-spawning, so ``--detach`` (or
    any argparse abbreviation of it) is executed at most once — the child runs
    the real sweep instead of forking another detached generation. O(1).

    Parameters
    ----------
    env
        Environment mapping to inspect; defaults to :data:`os.environ`.
    """
    source = os.environ if env is None else env
    return source.get(DETACHED_ENV_SENTINEL) == "1"


@dataclass(frozen=True, slots=True)
class DetachedSweepHandle:
    """One spawned detached sweep (immutable).

    Attributes
    ----------
    pid
        The child's process id (its own session leader).
    run_id_hint
        The caller's run-id hint when known at spawn time (the child computes
        the authoritative run id itself), else ``None``.
    results_root
        The sweep results root the child writes progress/exit-status under.
    cmdline
        The full argv the child was launched with (forensics + pidfile).
    """

    pid: int
    run_id_hint: str | None
    results_root: Path
    cmdline: tuple[str, ...]


def default_detach_command() -> tuple[str, ...]:
    """Return the default launcher prefix — ``(sys.executable, "-m", "brutex")``.

    The caller's *argv* (e.g. ``("sweep-all", "--lake-root", …)``) is appended
    to this prefix. Exposed as a function (not a constant) so the interpreter
    path is resolved at CALL time — a re-exec'd venv never bakes a stale path.
    """
    return (sys.executable, "-m", "brutex")


def default_detach_log_path(results_root: Path) -> Path:
    """Return ``<results_root>/runs/detached-sweep.log`` (never created here). O(1)."""
    return results_root / "runs" / DETACH_LOG_FILENAME


def detach_pidfile_path(results_root: Path) -> Path:
    """Return ``<results_root>/runs/detached-sweep.pid`` (never created here). O(1)."""
    return results_root / "runs" / DETACH_PIDFILE_NAME


def default_progress_path(results_root: Path) -> Path:
    """Return the run's live progress file path — ``progress/sweep-progress.json``.

    The byte-identical mirror of the dashboard's
    ``sweep_runner.sweep_progress_file`` (the engine may not import
    ``brutex.dashboard``; the equality is pinned by
    ``tests/unit/test_engine_sweep_detach.py`` so the two spellings can never
    silently drift). O(1).
    """
    return results_root / "progress" / "sweep-progress.json"


def spawn_detached_sweep(
    argv: Sequence[str],
    *,
    results_root: Path,
    log_path: Path | None = None,
    command: Sequence[str] | None = None,
    run_id_hint: str | None = None,
    clock: Callable[[], float] = time.time,
    env: Mapping[str, str] | None = None,
) -> DetachedSweepHandle:
    """Spawn the sweep as a detached session leader (R7 detached survival).

    ``Popen(command + argv, start_new_session=True, stdin=DEVNULL,
    stdout=<log append>, stderr=STDOUT)`` — the child survives the parent's
    death (module docstring). The pidfile is atomically rewritten with the
    child's pid + cmdline so a restarted dashboard can re-discover the run via
    :func:`probe_handle`.

    Parameters
    ----------
    argv
        The sweep arguments appended to *command* (e.g. ``("sweep-all", …)``).
    results_root
        The sweep results root (hosts the ``runs/`` log + pidfile).
    log_path
        Override of the child's stdout+stderr log file; defaults to
        :func:`default_detach_log_path`. Opened in APPEND mode.
    command
        Override of the launcher prefix (tests inject a stub child); defaults
        to :func:`default_detach_command`.
    run_id_hint
        Optional run-id hint carried on the handle + pidfile (observability).
    clock
        Epoch-seconds source for the pidfile's ``started_utc`` stamp
        (observability only — never identity).
    env
        Base environment for the child; defaults to the current
        :data:`os.environ`. :data:`DETACHED_ENV_SENTINEL` == ``"1"`` is ALWAYS
        stamped on top (spelling-immune re-detach guard — see
        :func:`running_as_detached_child`).

    Returns
    -------
    DetachedSweepHandle
        The spawned child's handle.

    Raises
    ------
    OSError
        If the log/pidfile cannot be created or the executable cannot be
        spawned — a loud launch-time refusal.
    """
    prefix = tuple(command) if command is not None else default_detach_command()
    cmdline = prefix + tuple(argv)
    log = log_path if log_path is not None else default_detach_log_path(results_root)
    log.parent.mkdir(parents=True, exist_ok=True)
    # Stamp the spelling-immune re-detach sentinel on top of the child env so
    # the child ignores any surviving ``--detach`` abbreviation (fork-bomb
    # guard — hostile-review round 3, #1594).
    base_env = os.environ if env is None else env
    child_env = {**base_env, DETACHED_ENV_SENTINEL: "1"}
    with log.open("ab") as log_file:
        proc = subprocess.Popen(  # noqa: S603  # nosec B603 — argv is caller-built, never shell-parsed
            cmdline,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
            env=child_env,
        )
    child_pid = proc.pid
    # DISOWN the Popen object: the child is detached BY DESIGN (nobody here
    # will ever wait() it — the parent may die first). Stamping a returncode
    # tells Popen.__del__ the handle is settled, so garbage collection never
    # raises the "subprocess is still running" ResourceWarning for a child
    # that is deliberately abandoned. The pid remains waitable by any caller
    # that stays alive (os.waitpid); the honest zombie envelope is in the
    # module docstring.
    proc.returncode = 0
    handle = DetachedSweepHandle(
        pid=child_pid,
        run_id_hint=run_id_hint,
        results_root=results_root,
        cmdline=cmdline,
    )
    pidfile = detach_pidfile_path(results_root)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "pid": handle.pid,
        "cmdline": list(cmdline),
        "run_id_hint": run_id_hint,
        "results_root": str(results_root),
        "started_utc": float(clock()),
    }
    content = json.dumps(doc, sort_keys=True).encode("utf-8")
    atomic_write_simple(pidfile, lambda tmp: tmp.write_bytes(content))
    _LOG.info(
        "sweep_detach.spawned",
        pid=handle.pid,
        log=str(log),
        pidfile=str(pidfile),
        run_id_hint=run_id_hint,
        cmdline=list(cmdline),
    )
    return handle


def _read_progress_mapping(path: Path) -> dict[str, Any] | None:
    """Read the progress JSON at *path*, or ``None`` (missing/corrupt — honest)."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def probe_handle(
    handle: DetachedSweepHandle,
    *,
    now_wall: float | None = None,
    fresh_seconds: float = DEFAULT_FRESH_SECONDS,
    pid_alive: Callable[[int], bool] = default_pid_alive,
    progress_path: Path | None = None,
) -> RunVerdict:
    """Judge a detached sweep's liveness — pid + progress-file cross-check (R7).

    Decision law (fail-closed, mirroring
    :func:`~brutex.engine.sweep_liveness.progress_verdict`):

    1. No readable progress file → :attr:`RunVerdict.NOT_RUNNING` when the
       handle's pid is dead, else :attr:`RunVerdict.UNKNOWN` (an alive pid
       alone is NOT evidence a SWEEP runs — pid recycling).
    2. Readable progress → the COMMON :func:`progress_verdict` over its
       ``wall_time_utc``/``pid``/``running`` keys.
    3. A ``RUNNING`` verdict whose progress ``pid`` differs from the handle's
       → :attr:`RunVerdict.UNKNOWN` (some sweep runs, but not provably ours).

    Parameters
    ----------
    handle
        The spawned handle (or one rebuilt from the pidfile).
    now_wall
        Wall-clock "now" (epoch seconds); defaults to ``time.time()``.
    fresh_seconds
        Freshness window forwarded to :func:`progress_verdict`.
    pid_alive
        Injected pid probe (deterministic tests).
    progress_path
        Override of the progress file; defaults to
        :func:`default_progress_path` under the handle's results root.

    Returns
    -------
    RunVerdict
        The cross-checked liveness verdict. O(1) — one small file read.
    """
    path = (
        progress_path if progress_path is not None else default_progress_path(handle.results_root)
    )
    now = time.time() if now_wall is None else now_wall
    progress = _read_progress_mapping(path)
    if progress is None:
        if not pid_alive(handle.pid):
            return RunVerdict.NOT_RUNNING
        return RunVerdict.UNKNOWN
    verdict = progress_verdict(
        progress, now_wall=now, fresh_seconds=fresh_seconds, pid_alive=pid_alive
    )
    if verdict is RunVerdict.RUNNING and progress.get("pid") != handle.pid:
        return RunVerdict.UNKNOWN
    return verdict
