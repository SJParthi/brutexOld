"""Live pull-progress tracker — O(1) thread-safe state for the real-time feed.

The shared progress state the dashboard's **live** pull view reads while a
``pull_all_spot`` / ``pull_options`` run is in flight (DEC-UNIVERSE-DASHBOARD-BUTTONS-001
follow-up). A background pull worker pushes O(1) updates
(:meth:`PullProgress.start` → :meth:`PullProgress.advance` × N →
:meth:`PullProgress.finish` / :meth:`PullProgress.fail`); the SSE / polling
route reads an immutable :class:`ProgressSnapshot` via :meth:`PullProgress.snapshot`.

Every mutation + read is **O(1)** (a handful of integer writes under one
lock — no scans, no per-unit allocation) so the tracker never becomes the
bottleneck of a 200-symbol universe pull (CLAUDE.md §2 Rule 5). It is
**thread-safe** (a single :class:`threading.Lock` guards the counters) so the
worker thread and the reader request can never observe a torn update, and the
``clock`` is injected so the timestamps are deterministic under test.

State machine (one pull job): ``idle → running → done`` (clean) or
``idle → running → error`` (terminal failure). ``start`` resets to ``running``
so the **same** tracker instance is reusable across re-clicks (idempotent —
CLAUDE.md §2 Rule 7).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

#: The four lifecycle states a pull job moves through.
STATE_IDLE: Final[str] = "idle"
STATE_RUNNING: Final[str] = "running"
STATE_DONE: Final[str] = "done"
STATE_ERROR: Final[str] = "error"


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """An immutable point-in-time view of a pull job's progress.

    Attributes
    ----------
    state
        One of :data:`STATE_IDLE` / :data:`STATE_RUNNING` / :data:`STATE_DONE`
        / :data:`STATE_ERROR`.
    total
        Total units of work (symbols for a spot pull, underlyings for an
        options fan-out). ``0`` before the first :meth:`PullProgress.start`.
    completed, failed
        Units finished cleanly / quarantined so far (``completed`` counts both
        clean and failed units — it is the units *processed*).
    gaps
        Units that returned NO data for a benign, expected reason — a vendor
        ``DH-905`` "no data" boundary (e.g. NIFTY NEXT 50's daily history that
        starts after 2021-01-01). A gap is NOT a failure (it is re-tried next
        pull, never masked) — counting it separately keeps the ``failed`` tally
        honest so a benign boundary stops reading as "1 failed"
        (DEC-DHAN-NO-DATA-GAP-COUNT-001).
    bars
        Running total of bars/rows appended so far.
    current
        Label of the unit most recently processed (e.g. the symbol).
    detail
        Free-text status / error message (empty unless set).
    updated_at
        The injected clock's value at the last mutation.
    phase
        The COARSE pipeline phase currently in flight — one of ``"spot"`` /
        ``"metadata"`` / ``"futures"`` / ``"options"`` / ``"historical"`` (or ``""``
        when idle / not set). Set by the one-click pipeline's ``_work`` at each phase
        transition so the live Universe per-phase view can highlight the running phase
        (DEC-UNIVERSE-PER-PHASE-PROGRESS-001). Purely informational — no counter is
        keyed by it (the spot/futures/options window tallies come from the ledger
        counts, not this label), so a stale / empty phase never fabricates progress.
    stalled, stall_detail
        The stall-watchdog's honest DEGRADED marker (DEC-PIPELINE-STALL-HEARTBEAT-001):
        ``stalled=True`` means no heartbeat has landed for the audited timeout while
        the worker thread is STILL running — a stalled-SUSPECT observation, NOT a
        terminal failure (``state`` stays ``running``; the run may still succeed).
        ``stall_detail`` names the silent phase + the silence duration. Cleared on a
        heartbeat recovery and on ``finish`` / ``fail``.
    wedged, wedge_detail
        The wedged-worker TERMINAL escalation marker (DEC-PULL-WEDGE-ESCALATE-001,
        STAGE 1): ``wedged=True`` means the stall watchdog observed the DUAL wedge
        signal — zero heartbeats for the wedge multiple of the stall timeout AND no
        lake-revision movement since the HUNG edge — so the run needs an operator
        RESTART (surfaced as ``restart_required`` in :meth:`to_dict`). Terminal for
        the run: a later heartbeat does NOT clear it (``clear_stalled`` leaves it);
        only ``finish`` / ``fail`` / a fresh ``start`` reset it. The worker is NOT
        killed and the single-flight stays held — auto-restart is a NAMED follow-up.
    """

    state: str
    total: int
    completed: int
    failed: int
    bars: int
    current: str
    detail: str
    updated_at: float
    gaps: int = 0
    phase: str = ""
    stalled: bool = False
    stall_detail: str = ""
    wedged: bool = False
    wedge_detail: str = ""

    @property
    def percent(self) -> float:
        """Completion percentage in ``[0, 100]`` — ``0.0`` when *total* is 0."""
        if self.total <= 0:
            return 0.0
        return min(100.0, round(self.completed / self.total * 100.0, 1))

    @property
    def running(self) -> bool:
        """``True`` iff the job is currently in flight."""
        return self.state == STATE_RUNNING

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the SSE / polling progress route."""
        return {
            "state": self.state,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "gaps": self.gaps,
            "bars": self.bars,
            "current": self.current,
            "detail": self.detail,
            "percent": self.percent,
            "running": self.running,
            "updated_at": self.updated_at,
            "phase": self.phase,
            "stalled": self.stalled,
            "stall_detail": self.stall_detail,
            "wedged": self.wedged,
            "wedge_detail": self.wedge_detail,
            # The operator-facing verdict alias (DEC-PULL-WEDGE-ESCALATE-001): a wedged
            # run requires a process restart to release the single-flight + the worker.
            "restart_required": self.wedged,
        }


def _monotonic() -> float:
    """Default clock — the monotonic wall clock (injected for testability)."""
    import time

    return time.monotonic()


class PullProgress:
    """Thread-safe, O(1) progress state for one live pull job.

    Parameters
    ----------
    clock
        Zero-arg time source stamped on every mutation (defaults to
        :func:`time.monotonic`). Injected so tests are deterministic.
    """

    def __init__(self, *, clock: Callable[[], float] = _monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state = STATE_IDLE
        self._total = 0
        self._completed = 0
        self._failed = 0
        self._gaps = 0
        self._bars = 0
        self._current = ""
        self._detail = ""
        self._updated_at = 0.0
        self._phase = ""
        self._stalled = False
        self._stall_detail = ""
        self._wedged = False
        self._wedge_detail = ""

    def start(self, *, total: int, detail: str = "") -> None:
        """Begin a job of *total* units — resets all counters to ``running``.

        Reusable: calling :meth:`start` again resets the same instance for a
        fresh pull (idempotent re-click, CLAUDE.md §2 Rule 7). A negative
        *total* is clamped to ``0``. O(1).
        """
        with self._lock:
            self._state = STATE_RUNNING
            self._total = max(0, total)
            self._completed = 0
            self._failed = 0
            self._gaps = 0
            self._bars = 0
            self._current = ""
            self._detail = detail
            self._phase = ""
            self._stalled = False
            self._stall_detail = ""
            self._wedged = False
            self._wedge_detail = ""
            self._updated_at = self._clock()

    def advance(
        self,
        *,
        current: str = "",
        bars: int = 0,
        failed: bool = False,
        gap: bool = False,
        grow_total: bool = False,
    ) -> None:
        """Record one processed unit — O(1).

        Increments ``completed`` (clamped to ``total``), adds *bars* to the
        running total, bumps ``failed`` when *failed* is true, bumps ``gaps``
        when *gap* is true (a benign DH-905 no-data boundary — NOT a failure,
        DEC-DHAN-NO-DATA-GAP-COUNT-001), and stamps *current* as the most-recent
        label. ``failed`` and ``gap`` are mutually exclusive at the call sites,
        but both are honoured independently here.

        *grow_total* (DEC-PIPELINE-STALL-HEARTBEAT-001, the frozen-counter fix):
        when True, ``total`` GROWS with ``completed`` instead of clamping it — the
        open-ended pair counter for a phase whose true unit total is unknown at
        the job layer (the pipeline's F&O historical fill), so the units + bars
        tallies keep MOVING instead of freezing at the prior phase's counts.
        Honest: the displayed total is then "units processed so far", not a
        known denominator (percent reads 100 — no worse than the frozen 45/45
        it replaces, and the bars/failed tallies are now real).
        """
        with self._lock:
            if grow_total:
                self._completed += 1
                self._total = max(self._total, self._completed)
            else:
                self._completed = min(self._total, self._completed + 1)
            self._bars += max(0, bars)
            if failed:
                self._failed += 1
            if gap:
                self._gaps += 1
            if current:
                self._current = current
            self._updated_at = self._clock()

    def sync(
        self, *, completed: int, total: int, bars: int | None, failed: int = 0, current: str = ""
    ) -> None:
        """Set ABSOLUTE progress from an authoritative source (the WindowLedger) — O(1).

        DEC-SPOT-WINDOW-PROGRESS-001. Unlike :meth:`advance` (which INCREMENTS, so it can
        only tick once per finished unit), this SETS the counters to the values an
        authoritative idempotent source already computed — the spot pull's live
        :class:`~brutex.data.window_ledger.LedgerProgress`. Driving the bar this way lets
        the units bar + percent track WINDOW-level progress on a ~1 s cadence (instead of
        only when a whole symbol's backfill finishes), and a repeated sync never
        double-counts: ``completed`` / ``total`` / ``bars`` / ``failed`` are absolute,
        clamped non-negative with ``completed`` clamped to ``total``. ``current`` updates
        the most-recent label only when non-empty (so a sync that has no live label keeps
        the last one). ``gaps`` is untouched (Groww has no known-gap concept).

        ``bars=None`` PRESERVES the running bars tally (DEC-PIPELINE-STALL-HEARTBEAT-001,
        the frozen-counter fix): a sync source with no bar concept — the metadata pull,
        which persists only the chain SHAPE — no longer wipes the cumulative bar count an
        earlier phase already streamed (the "0 BARS while 14.48M stream in" display bug).
        """
        with self._lock:
            self._total = max(0, total)
            self._completed = max(0, min(self._total, completed))
            if bars is not None:
                self._bars = max(0, bars)
            self._failed = max(0, failed)
            if current:
                self._current = current
            self._updated_at = self._clock()

    def set_current(self, current: str) -> None:
        """Update only the most-recent unit label (no counter change) — O(1).

        The light-touch companion to :meth:`sync` (DEC-SPOT-WINDOW-PROGRESS-001): a
        ledger-synced pull drives its counters from :meth:`sync` and uses this to refresh
        the "current symbol" label per finished pair WITHOUT the per-pair
        :meth:`advance` that would fight the absolute ledger sync. An empty label is a
        no-op.
        """
        if not current:
            return
        with self._lock:
            self._current = current
            self._updated_at = self._clock()

    def set_phase(self, phase: str) -> None:
        """Set the COARSE pipeline phase currently in flight — O(1).

        DEC-UNIVERSE-PER-PHASE-PROGRESS-001. The one-click pipeline's ``_work`` calls
        this at each phase transition (``"spot"`` → ``"metadata"`` → ``"historical"``)
        so the live Universe per-phase view can highlight the running phase. Purely a
        label — no counter is keyed by it (the per-segment window tallies come from the
        ledger counts), so a stale / empty phase can never fabricate progress. An empty
        string is honoured (it clears the label back to "no phase in flight").
        """
        with self._lock:
            self._phase = phase
            self._updated_at = self._clock()

    def mark_stalled(self, detail: str) -> None:
        """Flag the run STALLED-SUSPECT (no heartbeat for the timeout) — O(1).

        DEC-PIPELINE-STALL-HEARTBEAT-001: the stall watchdog's HUNG edge marks the run
        DEGRADED — an honest observation naming the silent phase — WITHOUT flipping the
        state to ``error`` (the worker thread is still running and may yet succeed; the
        forensically-proven false-FAILED headline this replaces). The start/finish
        ``detail`` is untouched (the marker rides its own ``stall_detail`` slot). A no-op
        unless the job is RUNNING — a late timer edge landing after finish/fail can never
        stamp a stalled marker onto a settled run.
        """
        with self._lock:
            if self._state != STATE_RUNNING:
                return
            self._stalled = True
            self._stall_detail = detail
            self._updated_at = self._clock()

    def clear_stalled(self) -> None:
        """Clear the stalled-suspect marker (a heartbeat recovered) — O(1).

        Deliberately does NOT clear the :meth:`mark_wedged` marker
        (DEC-PULL-WEDGE-ESCALATE-001): WEDGED is terminal for the run — a stray late
        heartbeat after the wedge verdict never silently withdraws the
        restart-required state; only ``finish`` / ``fail`` / a fresh ``start`` reset it.
        """
        with self._lock:
            self._stalled = False
            self._stall_detail = ""
            self._updated_at = self._clock()

    def mark_wedged(self, detail: str) -> None:
        """Flag the run WEDGED (terminal — restart required) — O(1).

        DEC-PULL-WEDGE-ESCALATE-001, STAGE 1: the stall watchdog observed the DUAL
        wedge signal (zero heartbeats for the wedge multiple of the timeout AND no
        lake-revision movement since the HUNG edge). The state stays ``running`` —
        the worker thread is NOT killed and the single-flight stays held (the
        DEC-PIPELINE-STALL-HEARTBEAT-001 double-run-race fix is preserved); the marker
        + the ``restart_required`` verdict tell the operator a process restart is the
        recovery path. A no-op unless the job is RUNNING, so a late timer edge can
        never stamp a settled run.
        """
        with self._lock:
            if self._state != STATE_RUNNING:
                return
            self._wedged = True
            self._wedge_detail = detail
            self._updated_at = self._clock()

    def finish(self, *, detail: str = "") -> None:
        """Mark the job cleanly complete (``done``) — O(1).

        Clears any stalled-suspect marker: a run that finished has, by definition,
        recovered (DEC-PIPELINE-STALL-HEARTBEAT-001).
        """
        with self._lock:
            self._state = STATE_DONE
            self._detail = detail
            self._stalled = False
            self._stall_detail = ""
            self._wedged = False
            self._wedge_detail = ""
            self._updated_at = self._clock()

    def fail(self, detail: str) -> None:
        """Mark the job terminally failed (``error``) with a reason — O(1).

        Clears any stalled-suspect marker — the terminal verdict supersedes the
        suspicion (DEC-PIPELINE-STALL-HEARTBEAT-001).
        """
        with self._lock:
            self._state = STATE_ERROR
            self._detail = detail
            self._stalled = False
            self._stall_detail = ""
            self._wedged = False
            self._wedge_detail = ""
            self._updated_at = self._clock()

    def snapshot(self) -> ProgressSnapshot:
        """Return an immutable, consistent O(1) view of the current state."""
        with self._lock:
            return ProgressSnapshot(
                state=self._state,
                total=self._total,
                completed=self._completed,
                failed=self._failed,
                gaps=self._gaps,
                bars=self._bars,
                current=self._current,
                detail=self._detail,
                updated_at=self._updated_at,
                phase=self._phase,
                stalled=self._stalled,
                stall_detail=self._stall_detail,
                wedged=self._wedged,
                wedge_detail=self._wedge_detail,
            )


__all__ = [
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_IDLE",
    "STATE_RUNNING",
    "ProgressSnapshot",
    "PullProgress",
]
