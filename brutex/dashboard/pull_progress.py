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
    started_at
        The injected clock's value at the last :meth:`PullProgress.start`
        (DEC-PULL-PROGRESS-HONESTY-001 — the ETA anchor, ported from the engine's
        :class:`~brutex.engine.sweep_progress.SweepProgressSnapshot` pattern).
    phase_started_at, phase_base_completed
        The PHASE-scoped rate baseline: stamped by the FIRST counter mutation after
        :meth:`PullProgress.start` / :meth:`PullProgress.set_phase`, so
        :attr:`units_per_sec` measures only THIS phase's marginal progress — a
        ledger sync that opens on cross-run counts (e.g. thousands of previously
        settled windows) can never fabricate an instant burst rate.
    open_ended
        ``True`` when the phase's unit TOTAL is not final (DEC-PULL-PROGRESS-HONESTY-001,
        the grow-total percent-pin fix): the F&O historical phase's denominator GROWS as
        contracts are planned mid-phase, so a percent / linear ETA over it is an
        illusion. The frontend renders "N units · total unknown" instead of a pinned
        100%, and :attr:`eta_seconds` reads ``None``. Set by
        ``set_phase(..., open_ended=True)`` and by any ``advance(grow_total=True)``;
        STICKY for the rest of the run (a later ``set_phase("prebuild")`` /
        ``set_phase("movers")`` must NOT clear it — the displayed total stays
        non-final); cleared only by the next :meth:`PullProgress.start`.
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
    started_at: float = 0.0
    phase_started_at: float = 0.0
    phase_base_completed: int = 0
    open_ended: bool = False

    @property
    def percent(self) -> float:
        """Completion percentage in ``[0, 100]`` — ``0.0`` when *total* is 0."""
        if self.total <= 0:
            return 0.0
        return min(100.0, round(self.completed / self.total * 100.0, 1))

    @property
    def elapsed_seconds(self) -> float:
        """Seconds between :meth:`PullProgress.start` and the last mutation (clamped ≥ 0)."""
        elapsed = self.updated_at - self.started_at
        return elapsed if elapsed > 0.0 else 0.0

    @property
    def units_per_sec(self) -> float:
        """Observed PHASE-scoped throughput (units completed / elapsed seconds).

        DEC-PULL-PROGRESS-HONESTY-001 — the pull twin of the engine's
        ``SweepProgressSnapshot.combos_per_sec``. Measured from the phase baseline
        (:attr:`phase_base_completed` at :attr:`phase_started_at`), so it reflects only
        the CURRENT phase's marginal progress. ``0.0`` until at least one unit has
        completed past the baseline over a positive elapsed span (avoids a
        divide-by-zero and never fabricates a rate from a cross-run count jump).
        """
        progressed = self.completed - self.phase_base_completed
        span = self.updated_at - self.phase_started_at
        if span <= 0.0 or progressed <= 0:
            return 0.0
        return progressed / span

    @property
    def eta_seconds(self) -> float | None:
        """Linear-extrapolation ETA to completion, in seconds — or an honest ``None``.

        DEC-PULL-PROGRESS-HONESTY-001, ported from the engine's
        ``SweepProgressSnapshot.eta_seconds``. ``None`` when it cannot be estimated
        (idle / settled, no measured rate, no known total) — AND, the mandatory
        honesty amendment, whenever the run is :attr:`open_ended` (a grow-total /
        growing-denominator phase): a linear ETA over a growing denominator is an
        illusion, so none is shown. Honest envelope: an extrapolation from the
        CURRENT phase rate, not a guarantee.
        """
        if self.state != STATE_RUNNING or self.open_ended or self.total <= 0:
            return None
        rate = self.units_per_sec
        if rate <= 0.0:
            return None
        remaining = self.total - self.completed
        if remaining <= 0:
            return 0.0
        return remaining / rate

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
            # DEC-PULL-PROGRESS-HONESTY-001 — ADDITIVE keys only (the integration
            # suite pins that no existing flat key is ever dropped).
            "started_at": self.started_at,
            "units_per_sec": self.units_per_sec,
            "eta_seconds": self.eta_seconds,
            "open_ended": self.open_ended,
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
        # ETA state (DEC-PULL-PROGRESS-HONESTY-001): the run anchor + the PHASE-scoped
        # rate baseline. ``_baseline_pending`` defers the baseline stamp to the FIRST
        # counter mutation after start()/set_phase(), so a ledger sync that opens on
        # cross-run absolute counts never fabricates an instant burst rate.
        self._started_at = 0.0
        self._phase_started_at = 0.0
        self._phase_base_completed = 0
        self._baseline_pending = False
        self._open_ended = False

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
            now = self._clock()
            # A fresh run re-anchors the ETA clock + clears the sticky open-ended
            # marker (DEC-PULL-PROGRESS-HONESTY-001) — the ONLY place that clears it.
            self._started_at = now
            self._phase_started_at = now
            self._phase_base_completed = 0
            self._baseline_pending = True
            self._open_ended = False
            self._updated_at = now

    def _stamp_baseline_locked(self, now: float) -> None:
        """Stamp the phase rate baseline on the FIRST counter mutation (caller holds the lock).

        DEC-PULL-PROGRESS-HONESTY-001: after :meth:`start` / :meth:`set_phase` the
        baseline is PENDING; the first :meth:`advance` / :meth:`sync` that lands stamps
        ``phase_base_completed`` at its OWN post-mutation count and ``phase_started_at``
        at its own instant. The rate therefore measures from the first observed count
        ONWARD — a ledger sync that opens on cross-run absolute counts (e.g. 3,000
        previously settled windows) contributes ZERO to the measured rate instead of an
        absurd instantaneous burst. O(1).
        """
        if self._baseline_pending:
            self._phase_base_completed = self._completed
            self._phase_started_at = now
            self._baseline_pending = False

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
        known denominator — and the snapshot is marked :attr:`ProgressSnapshot.open_ended`
        (DEC-PULL-PROGRESS-HONESTY-001) so the frontend renders "N units · total
        unknown" instead of the pinned-100% illusion, and ``eta_seconds`` reads
        ``None`` (a linear ETA over a growing denominator is meaningless).
        """
        with self._lock:
            if grow_total:
                self._completed += 1
                self._total = max(self._total, self._completed)
                # A grow-total consumer is BY DEFINITION open-ended — mark it so the
                # display never reads the pinned completed/total == 100% as real.
                self._open_ended = True
            else:
                self._completed = min(self._total, self._completed + 1)
            self._bars += max(0, bars)
            if failed:
                self._failed += 1
            if gap:
                self._gaps += 1
            if current:
                self._current = current
            now = self._clock()
            self._updated_at = now
            self._stamp_baseline_locked(now)

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
            now = self._clock()
            self._updated_at = now
            self._stamp_baseline_locked(now)

    def add_bars(self, delta: int) -> None:
        """Fold a bars DELTA into the running tally — O(1), thread-safe.

        DEC-PULL-PROGRESS-HONESTY-001 (the frozen-bars fix): the async F&O fetch
        pipeline's background consumer folds each window's REAL appended-bar count
        via :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.fold_totals`
        while the producer-side per-pair hook honestly reports ``bars == 0`` — so the
        headline bars counter froze at the spot phase's count for the whole F&O
        phase. This is the O(1) live sink for those deltas: each folded window
        contributes its delta exactly once (the no-double-count invariant — the
        sync-mode path adds the per-pair report bars instead, and an async per-pair
        report is 0). Counts bars PROCESSED (the sink-append input count, the same
        semantics as the per-pair ``bars_appended`` it replaces), not net-new lake
        rows. A non-positive *delta* is a no-op tally-wise; a late delta landing
        after the run settled is DROPPED (never mutates a settled snapshot). Does
        NOT touch the unit counters or the ETA baseline (bars are not units).
        """
        with self._lock:
            if self._state != STATE_RUNNING:
                return
            self._bars += max(0, delta)
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

    def set_phase(self, phase: str, *, open_ended: bool = False) -> None:
        """Set the COARSE pipeline phase currently in flight — O(1).

        DEC-UNIVERSE-PER-PHASE-PROGRESS-001. The one-click pipeline's ``_work`` calls
        this at each phase transition (``"spot"`` → ``"metadata"`` → ``"historical"``)
        so the live Universe per-phase view can highlight the running phase. Purely a
        label — no counter is keyed by it (the per-segment window tallies come from the
        ledger counts), so a stale / empty phase can never fabricate progress. An empty
        string is honoured (it clears the label back to "no phase in flight").

        A phase transition also RE-ARMS the ETA rate baseline
        (DEC-PULL-PROGRESS-HONESTY-001): the next counter mutation stamps a fresh
        phase baseline, so one phase's throughput never pollutes the next phase's ETA.

        *open_ended* declares the ENTERING phase's unit total NON-FINAL (the F&O
        historical phase — its ledger denominator grows as contracts are planned
        mid-phase). It is STICKY: ``open_ended=True`` sets the marker, but the
        default ``False`` NEVER clears one already set — a later
        ``set_phase("prebuild")`` / ``set_phase("movers")`` keeps the honest
        "total unknown" display (the pinned invariant); only :meth:`start` clears it.
        """
        with self._lock:
            self._phase = phase
            self._baseline_pending = True
            if open_ended:
                self._open_ended = True
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
        """Clear the stalled-suspect marker (a heartbeat recovered) — O(1)."""
        with self._lock:
            self._stalled = False
            self._stall_detail = ""
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
                started_at=self._started_at,
                phase_started_at=self._phase_started_at,
                phase_base_completed=self._phase_base_completed,
                open_ended=self._open_ended,
            )


__all__ = [
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_IDLE",
    "STATE_RUNNING",
    "ProgressSnapshot",
    "PullProgress",
]
