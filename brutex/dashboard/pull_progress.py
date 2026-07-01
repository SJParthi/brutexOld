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
            self._updated_at = self._clock()

    def advance(
        self, *, current: str = "", bars: int = 0, failed: bool = False, gap: bool = False
    ) -> None:
        """Record one processed unit — O(1).

        Increments ``completed`` (clamped to ``total``), adds *bars* to the
        running total, bumps ``failed`` when *failed* is true, bumps ``gaps``
        when *gap* is true (a benign DH-905 no-data boundary — NOT a failure,
        DEC-DHAN-NO-DATA-GAP-COUNT-001), and stamps *current* as the most-recent
        label. ``failed`` and ``gap`` are mutually exclusive at the call sites,
        but both are honoured independently here.
        """
        with self._lock:
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
        self, *, completed: int, total: int, bars: int, failed: int = 0, current: str = ""
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
        """
        with self._lock:
            self._total = max(0, total)
            self._completed = max(0, min(self._total, completed))
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

    def finish(self, *, detail: str = "") -> None:
        """Mark the job cleanly complete (``done``) — O(1)."""
        with self._lock:
            self._state = STATE_DONE
            self._detail = detail
            self._updated_at = self._clock()

    def fail(self, detail: str) -> None:
        """Mark the job terminally failed (``error``) with a reason — O(1)."""
        with self._lock:
            self._state = STATE_ERROR
            self._detail = detail
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
            )


__all__ = [
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_IDLE",
    "STATE_RUNNING",
    "ProgressSnapshot",
    "PullProgress",
]
