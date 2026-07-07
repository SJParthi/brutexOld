"""Sweep exit-status artifact + progress-payload enrichment (G5 — DEC-SWEEP-UNKILLABLE-CORE-001).

The dashboard is exit-code blind today (G5): a sweep subprocess's exit code /
refusal text dies in a generic finish detail and never reaches the page. The
run core writes ONE small atomic JSON artifact per run —
``<results_root>/runs/<run_id>/exit-status.json`` — at enter
(``state=running``) and at every terminal boundary, and
:func:`enrich_progress_payload` folds it ADDITIVELY into the existing
``/api/v1/sweep-progress`` payload (the route already delegates to
``sweep_supervisor.read_progress``; the enrichment lands there in the run-core
wiring — ZERO route edits).

Schema (frozen by the Phase-2 design §3): ``{run_id, state, exit_code|null,
refusal_text|null, error_type|null, attempt, pid, wall_started_utc,
wall_ended_utc|null, suspect}``. ``attempt`` is observability-only (D-1);
wall-clock fields are observability-only and never part of any identity.

Fail-soft law (I-3): the artifact is observability — the
:func:`write_exit_status_soft` twin (for ``finally`` blocks) maps an
``OSError`` to ONE WARN + ``False`` so a dead disk at run end can never mask
the body's real exception; the strict :func:`write_exit_status` propagates
``OSError`` for launch-time fail-loud use. A corrupt/missing artifact reads as
honest ``None`` — never a 500 (failure-matrix A13/F3).

Honest envelope (Golden Rule #10): every operation here is O(1) — one small
JSON read or one atomic write (:func:`brutex.data.atomic_io.atomic_write_simple`)
regardless of run size — bench-pinned by
``tests/bench/test_sweep_progress_poll_o1.py``.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple

__all__ = [
    "EXIT_STATUS_FILENAME",
    "RunState",
    "enrich_progress_payload",
    "exit_status_path",
    "read_exit_status",
    "write_exit_status",
    "write_exit_status_soft",
]

_LOG = structlog.get_logger("brutex.engine.sweep_exit_status")

#: The exit-status artifact filename under ``runs/<run_id>/``.
EXIT_STATUS_FILENAME: Final[str] = "exit-status.json"


class RunState(StrEnum):
    """The exit-status state machine (design §2 — values are the wire strings).

    ``RUNNING`` is written at run-core enter; every other state is terminal.
    ``SUSPECT_COMPLETE`` is the D-12 terminal state — the run otherwise
    completed but a tripwire/diffcheck latched SUSPECT (its payload always
    carries ``suspect: true``).
    """

    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    REFUSED = "refused"
    HALTED = "halted"
    SUSPECT_COMPLETE = "suspect_complete"


def exit_status_path(results_root: Path, run_id: str) -> Path:
    """Return ``<results_root>/runs/<run_id>/exit-status.json`` (never created here). O(1)."""
    return results_root / "runs" / run_id / EXIT_STATUS_FILENAME


def _finite_float(value: object) -> float | None:
    """Return *value* as a finite ``float`` when it is a real number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def write_exit_status(
    results_root: Path,
    *,
    run_id: str,
    state: RunState | str,
    exit_code: int | None = None,
    refusal_text: str | None = None,
    error_type: str | None = None,
    attempt: int,
    pid: int,
    suspect: bool | None = None,
    clock: Callable[[], float] = time.time,
) -> None:
    """Atomically write the run's exit-status artifact (state machine boundary).

    Wall-clock law: ``state=running`` stamps ``wall_started_utc=now`` and
    ``wall_ended_utc=null``; a terminal state PRESERVES the prior artifact's
    ``wall_started_utc`` when readable (else stamps ``now``) and stamps
    ``wall_ended_utc=now``. ``suspect`` resolution: an explicit *suspect*
    wins; ``None`` preserves the prior artifact's flag (default ``False``);
    :attr:`RunState.SUSPECT_COMPLETE` forces ``True`` (D-12).

    Parameters
    ----------
    results_root
        The sweep results root (``runs/<run_id>/`` is created if needed).
    run_id
        The run identity (D-1); non-empty.
    state
        A :class:`RunState` (or its wire string) — junk fails loudly.
    exit_code
        Process exit code for a terminal state (``None`` while running).
    refusal_text
        The loud refusal message for ``state=refused`` (``None`` otherwise).
    error_type
        Exception class name for ``failed``/``halted`` (``None`` otherwise).
    attempt
        Observability-only attempt ordinal (``>= 1``).
    pid
        The writing process's pid (``>= 1``).
    suspect
        Explicit suspect flag; ``None`` = preserve prior (see above).
    clock
        Epoch-seconds source (injected for deterministic tests).

    Raises
    ------
    ValueError
        On an empty *run_id*, an unknown *state*, ``attempt < 1``,
        ``pid < 1``, or a ``bool`` *exit_code* (a truthy typo, not a code).
    OSError
        If the atomic write fails (the strict variant — use
        :func:`write_exit_status_soft` in ``finally`` blocks).
    """
    if not run_id:
        raise ValueError("run_id must be non-empty")
    resolved_state = RunState(state)
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    if pid < 1:
        raise ValueError(f"pid must be >= 1, got {pid}")
    if isinstance(exit_code, bool):
        raise ValueError(f"exit_code must be an int or None, got {exit_code!r}")
    now = float(clock())
    prior = read_exit_status(results_root, run_id)
    if resolved_state is RunState.RUNNING:
        started = now
        ended: float | None = None
    else:
        prior_started = _finite_float(prior.get("wall_started_utc")) if prior else None
        started = prior_started if prior_started is not None else now
        ended = now
    if resolved_state is RunState.SUSPECT_COMPLETE:
        resolved_suspect = True
    elif suspect is not None:
        resolved_suspect = suspect
    else:
        resolved_suspect = bool(prior.get("suspect", False)) if prior else False
    doc: dict[str, Any] = {
        "run_id": run_id,
        "state": resolved_state.value,
        "exit_code": exit_code,
        "refusal_text": refusal_text,
        "error_type": error_type,
        "attempt": attempt,
        "pid": pid,
        "wall_started_utc": started,
        "wall_ended_utc": ended,
        "suspect": resolved_suspect,
    }
    dest = exit_status_path(results_root, run_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(doc, sort_keys=True).encode("utf-8")
    atomic_write_simple(dest, lambda tmp: tmp.write_bytes(content))


def write_exit_status_soft(
    results_root: Path,
    *,
    run_id: str,
    state: RunState | str,
    exit_code: int | None = None,
    refusal_text: str | None = None,
    error_type: str | None = None,
    attempt: int,
    pid: int,
    suspect: bool | None = None,
    clock: Callable[[], float] = time.time,
) -> bool:
    """The fail-soft ``finally`` twin of :func:`write_exit_status` (I-3).

    An ``OSError`` (dead disk / ENOSPC at run end) is mapped to ONE
    ``sweep_exit_status.write_failed`` WARN + ``False`` so the artifact write
    can never mask the run body's real exception. Validation errors
    (``ValueError`` — a code bug, not an environment failure) still propagate.

    Returns
    -------
    bool
        ``True`` iff the artifact was durably written.
    """
    try:
        write_exit_status(
            results_root,
            run_id=run_id,
            state=state,
            exit_code=exit_code,
            refusal_text=refusal_text,
            error_type=error_type,
            attempt=attempt,
            pid=pid,
            suspect=suspect,
            clock=clock,
        )
    except OSError as exc:
        _LOG.warning(
            "sweep_exit_status.write_failed",
            run_id=run_id,
            state=str(state),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False
    return True


def read_exit_status(results_root: Path, run_id: str) -> dict[str, Any] | None:
    """Read the run's exit-status artifact, or ``None`` (honest, never raises).

    A missing file, an OS read error, malformed JSON, or a non-object JSON
    body all return ``None`` — a corrupt observability artifact must degrade
    honestly, never 500 a poll (failure-matrix A13/F3). O(1).
    """
    path = exit_status_path(results_root, run_id)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def enrich_progress_payload(progress: dict[str, Any], *, results_root: Path) -> dict[str, Any]:
    """Additively merge the exit-status artifact into a progress payload (G5).

    Returns a NEW dict (the input is never mutated) in which every
    pre-existing key is byte-unchanged and:

    * ``exit_status`` is set to the run's artifact dict (looked up by the
      payload's own ``run_id`` key) or honest ``None`` when the payload names
      no run / the artifact is missing/corrupt;
    * ``suspect`` is ADDED (never overwritten — the progress writer stays
      authoritative) from the artifact's flag, defaulting ``False``.

    The existing ``/api/v1/sweep-progress`` route serves this shape with zero
    route edits once ``read_progress`` is wired through it (run-core wiring).
    O(1) — one small file read regardless of run size (bench-pinned).
    """
    out: dict[str, Any] = dict(progress)
    run_id = progress.get("run_id")
    status: dict[str, Any] | None = None
    if isinstance(run_id, str) and run_id:
        status = read_exit_status(results_root, run_id)
    out["exit_status"] = status
    if "suspect" not in out:
        out["suspect"] = bool(status.get("suspect", False)) if status else False
    return out
