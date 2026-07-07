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

__all__ = [
    "DEFAULT_FRESH_SECONDS",
    "RunVerdict",
    "default_pid_alive",
    "progress_verdict",
]

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
    if not pid_alive(pid):
        return RunVerdict.NOT_RUNNING
    if now_wall - float(wall) > fresh_seconds:
        return RunVerdict.STALE
    return RunVerdict.RUNNING
