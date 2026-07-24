"""Pure sweep-run liveness verdict over a published progress payload (G4 — DEC-SWEEP-UNKILLABLE-CORE-001).

The 2026-07-06 gap hunt confirmed the HIGHEST-DANGER progress-honesty defect
(G4): the sweep-heal region compared ``time.time()`` against the progress
file's ``updated_at`` — which is ``time.monotonic()``-based
(``brutex/engine/sweep_progress.py``) — so "fresh" was ~always ``False`` and a
LIVE run's stores were quarantinable mid-run. This module is the pure verdict
core of the fix: it judges a progress payload's WALL-clock (``wall_time_utc``)
+ ``pid`` + ``running`` keys (the additive keys ``publish_progress`` gains in
the run-core wiring) and returns a four-state :class:`RunVerdict`.

The FAIL-CLOSED law (G4): a LEGACY payload (monotonic-only keys — today's
on-disk shape) or a CORRUPT payload (wrong field types) is
:attr:`RunVerdict.UNKNOWN`, **never** :attr:`RunVerdict.NOT_RUNNING` — the
heal caller must REFUSE to quarantine on ``UNKNOWN`` (doubt protects live
bytes; the verdict enum encodes the refusal).

Honest envelope (Golden Rule #10): this is a pure O(1) judgment over one small
mapping — it performs NO I/O itself (the ``pid`` probe is one ``os.kill(pid,
0)`` signal-0 syscall in the injectable default) and it cannot distinguish a
recycled pid from the original writer; the wall-clock freshness window bounds
that exposure. Verdict monotonicity in staleness is property-pinned
(``tests/property/test_sweep_liveness_property.py``).
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Final

from brutex.engine.sweep_exit_status import RunState

# DEC-RUST-SWEEP-OPS-001 (slice 1b): the default-ON Rust routing facade. The
# `progress_verdict` decision TAIL (steps 3-5, after the payload typing + the
# pid probe) gates on `sweep_ops_rust_enabled()` FIRST so the flag-OFF
# default path is byte-identical; `_liveness_tail_python` below is the live
# oracle. The fail-closed UNKNOWN arm (Python-type introspection of an
# untyped payload) + the `os.kill` probe stay Python by construction.
from brutex.engine.sweep_ops_rust import routed_liveness_tail, sweep_ops_rust_enabled

__all__ = [
    "DEFAULT_FRESH_SECONDS",
    "RunVerdict",
    "default_pid_alive",
    "exit_status_running",
    "progress_verdict",
]

#: The exit-status wire string for a live run — single-sourced from the
#: exit-status state machine (``RunState.RUNNING``) so this liveness reader can
#: never drift from the writer (COMMON; imported as a value, not re-minted).
_EXIT_STATUS_RUNNING: Final[str] = RunState.RUNNING.value

#: Default freshness window (seconds) for the ``wall_time_utc`` heartbeat. A
#: healthy supervised sweep publishes progress at least once per tuple; 600 s
#: (10 min) is generous against the slowest legitimate tuple while still
#: catching a machine-death-stale file well before an operator heals.
DEFAULT_FRESH_SECONDS: Final[float] = 600.0


class RunVerdict(StrEnum):
    """The four liveness verdicts over a published sweep-progress payload.

    ``UNKNOWN`` is the FAIL-CLOSED verdict (G4): a heal/quarantine caller must
    treat it as "possibly live — refuse to touch", never as ``NOT_RUNNING``.
    """

    RUNNING = "running"
    STALE = "stale"
    NOT_RUNNING = "not_running"
    UNKNOWN = "unknown"


def default_pid_alive(pid: int) -> bool:
    """Return ``True`` iff *pid* names a live process (signal-0 probe).

    ``os.kill(pid, 0)`` sends no signal — it only checks deliverability.
    ``ProcessLookupError`` means the pid is dead; ``PermissionError`` (EPERM)
    means the process EXISTS but belongs to another user — alive. O(1).

    Parameters
    ----------
    pid
        The process id to probe (``> 0`` — callers validate; see
        :func:`progress_verdict`).

    Returns
    -------
    bool
        ``True`` when the process exists.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def progress_verdict(
    progress: Mapping[str, object],
    *,
    now_wall: float,
    fresh_seconds: float = DEFAULT_FRESH_SECONDS,
    pid_alive: Callable[[int], bool] = default_pid_alive,
) -> RunVerdict:
    """Judge a published sweep-progress payload's liveness (pure, O(1)).

    Decision law (G4, fail-closed — every branch pinned by the unit matrix):

    1. ``running`` not a ``bool``, ``wall_time_utc`` not a finite number, or
       ``pid`` not a positive ``int`` (missing keys included — the LEGACY
       monotonic-only payload shape) → :attr:`RunVerdict.UNKNOWN`. The heal
       caller must REFUSE to quarantine on ``UNKNOWN``.
    2. ``running is False`` → :attr:`RunVerdict.NOT_RUNNING` (a clean exit
       wrote the terminal payload).
    3. dead ``pid`` → :attr:`RunVerdict.NOT_RUNNING` (the writer died).
    4. ``now_wall - wall_time_utc > fresh_seconds`` (strictly) →
       :attr:`RunVerdict.STALE`.
    5. else → :attr:`RunVerdict.RUNNING`.

    Parameters
    ----------
    progress
        The parsed progress payload (as published by ``publish_progress``).
    now_wall
        The caller's wall-clock "now" (epoch seconds) — injected so the
        verdict is deterministic under test and monotone in staleness.
    fresh_seconds
        Freshness window for the wall-clock heartbeat; must be a positive,
        finite number of seconds (fail-loud otherwise — a ``nan`` window would
        silently disable the staleness arm).
    pid_alive
        Injected pid probe (defaults to :func:`default_pid_alive`).

    Returns
    -------
    RunVerdict
        The liveness verdict.

    Raises
    ------
    ValueError
        If *fresh_seconds* is not a positive, finite number.
    """
    if not math.isfinite(fresh_seconds) or fresh_seconds <= 0:
        raise ValueError(
            f"fresh_seconds must be a positive, finite number of seconds; got {fresh_seconds!r}"
        )
    running = progress.get("running")
    wall = progress.get("wall_time_utc")
    pid = progress.get("pid")
    if not isinstance(running, bool):
        return RunVerdict.UNKNOWN
    if isinstance(wall, bool) or not isinstance(wall, int | float) or not math.isfinite(wall):
        return RunVerdict.UNKNOWN
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return RunVerdict.UNKNOWN
    if not running:
        return RunVerdict.NOT_RUNNING
    # Probe the pid exactly where the pre-conversion law did (only a
    # running=True payload is ever probed — the injected-probe call count is
    # part of the observable contract).
    alive = pid_alive(pid)
    wall_f = float(wall)
    if sweep_ops_rust_enabled():
        return RunVerdict(
            routed_liveness_tail(
                alive,
                now_wall,
                wall_f,
                fresh_seconds,
                python_law=lambda: _liveness_tail_python(alive, now_wall, wall_f, fresh_seconds),
            )
        )
    return RunVerdict(_liveness_tail_python(alive, now_wall, wall_f, fresh_seconds))


def _liveness_tail_python(alive: bool, now_wall: float, wall_f: float, fresh_seconds: float) -> str:
    """The verbatim verdict tail (steps 3-5) — the live parity oracle
    (DEC-RUST-SWEEP-OPS-001; retained until the final slice's default flip).

    Returns the :class:`RunVerdict` wire value the caller re-wraps (a
    ``StrEnum`` lookup returns the same singleton member the pre-conversion
    branches returned directly).
    """
    if not alive:
        return RunVerdict.NOT_RUNNING.value
    if now_wall - wall_f > fresh_seconds:
        return RunVerdict.STALE.value
    return RunVerdict.RUNNING.value


def exit_status_running(
    progress: Mapping[str, object],
    *,
    pid_alive: Callable[[int], bool] = default_pid_alive,
) -> bool:
    """Return ``True`` iff the enriched *progress* payload names a LIVE running run.

    Reads the ``exit_status`` sub-dict that
    :func:`brutex.engine.sweep_exit_status.enrich_progress_payload` merges into
    a ``read_progress`` payload: the run is LIVE when that artifact reads
    ``state == "running"`` AND its ``pid`` is alive.

    The C6 resume-startup residual (round-4 hostile-review fix): on a
    crash-RESUME the on-disk *progress* file still carries the PRIOR attempt's
    DEAD pid until ``_reset_progress_at_launch`` rewrites it — which happens
    AFTER the checkpoint preload — so :func:`progress_verdict` alone reads
    :attr:`RunVerdict.NOT_RUNNING` during that window and a concurrent
    ``sweep-heal --execute`` could quarantine the resuming run's
    stores/checkpoint. The ``exit-status.json`` artifact, by contrast, is
    written FIRST at run-core enter (``state=running`` + the LIVE pid, under
    the G6 results-root flock, before the preload), so it does NOT lag the
    resuming run — it is the signal that closes the startup window. Any other
    shape (no ``exit_status``, a non-running state, a missing/non-int/``bool``/
    non-positive pid, or a dead pid) → ``False``, so the heal caller falls back
    to the progress-file verdict. Pure O(1) (one ``os.kill(pid, 0)`` in the
    default probe); NO file I/O of its own — the enrichment already read the
    artifact.

    Parameters
    ----------
    progress
        The enriched progress payload (as returned by ``read_progress``).
    pid_alive
        Injected pid probe (defaults to :func:`default_pid_alive`).

    Returns
    -------
    bool
        ``True`` when the exit-status artifact reports a live running run.
    """
    status = progress.get("exit_status")
    if not isinstance(status, Mapping):
        return False
    if status.get("state") != _EXIT_STATUS_RUNNING:
        return False
    pid = status.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    return pid_alive(pid)
