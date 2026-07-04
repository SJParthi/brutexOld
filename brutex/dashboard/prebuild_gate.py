"""Movers **prebuild gate** — block the ``/movers`` screens during a pipeline pull.

``DEC-PIPELINE-AUTOPREBUILD-001`` (chunk 2 — the movers arm). The one-click pull
pipeline's FINAL data stage pre-builds every derived timeframe (19 intraday +
the derived ``1day``) for SPOT + FUTURES + OPTIONS
(:mod:`brutex.dashboard.pipeline_prebuild`). While that pipeline is running,
the movers screens would read a half-filled lake (stale derived partitions
mid-overwrite, a 1-minute series still landing), so the operator decree gates
them: a pipeline pull **begins** the gate (movers return an honest "prebuild in
progress" payload with ZERO lake reads) and the gate is **released** when the
prebuild finishes — or when the pipeline stops early / fails for ANY reason
(the binding site releases in a ``try/finally``), so a failed pull can NEVER
leave the movers blocked forever.

## Honest envelope (Golden Rule #1 / #10)

* The gated payload is a **200 with ``gated: true``** and a note — never a 500,
  never a fabricated (or half-fresh) mover row. The page keeps polling and
  recovers by itself the moment the gate lifts.
* The gate is **process-local** state on the app instance (injected via
  ``build_app`` — no module-global mutable state, CLAUDE.md §4): a CLI pull in
  a SEPARATE process does not gate the dashboard movers (stated, not hidden —
  the dashboard's own pipeline is the surface the decree covers).
* Only the FULL pipeline route (``/api/v1/universe/pull-pipeline``) gates; a
  ``dry_run`` pipeline (which writes no real data) does NOT gate.

Thread-safe: ``begin`` / ``release`` / ``snapshot`` serialise on one
:class:`threading.Lock` — the pipeline job thread flips it, the movers route's
threadpool workers read it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog

from brutex.dashboard.movers_snapshot import parse_snapshot_date

_LOG = structlog.get_logger(__name__)

__all__ = [
    "STATE_BLOCKING",
    "STATE_IDLE",
    "GateSnapshot",
    "PrebuildGate",
    "gated_movers_payload",
]

#: No pipeline pull is running — movers serve normally.
STATE_IDLE: Final[str] = "idle"
#: A pipeline pull is running (its prebuild has not been released yet) —
#: movers return the honest gated payload without touching the lake.
STATE_BLOCKING: Final[str] = "blocking"


@dataclass(frozen=True, slots=True)
class GateSnapshot:
    """One immutable point-in-time view of the gate (what the movers route reads).

    Attributes
    ----------
    state
        :data:`STATE_IDLE` or :data:`STATE_BLOCKING`.
    runs
        Monotonically-increasing count of :meth:`PrebuildGate.begin` calls —
        how many pipeline pulls have gated the movers in this process's life.
    last_ok
        The ``ok`` flag of the most recent :meth:`PrebuildGate.release`
        (``True`` = the prebuild succeeded; ``False`` = it failed OR the
        pipeline stopped before reaching it), or ``None`` before any release —
        the honest provenance for a note/diagnostic, never fabricated.
    phase
        The gating pipeline's CURRENT phase label
        (``"pull"``/``"spot"``/``"metadata"``/``"historical"``/``"prebuild"``/
        ``"movers"`` — DEC-PREBUILD-GATE-HONEST-NOTE-001), or ``None`` when not
        blocking. Honest: the gate releases only AFTER the movers warm too, so
        the payload can name WHICH multi-hour stage is actually running instead
        of the old blanket "prebuild in progress".
    started_at
        ISO-8601 UTC instant :meth:`PrebuildGate.begin` was called (``None``
        when not blocking).
    elapsed_s
        Wall-clock seconds since :meth:`PrebuildGate.begin` (``None`` when not
        blocking) — monotonic-clock derived, so a host clock step never yields
        a negative/backwards elapsed.
    progress_done, progress_total
        The current phase's coordinate progress (the movers-warm ``i/N`` fed by
        :meth:`PrebuildGate.set_progress` via ``warm_movers_snapshots``'s
        ``on_progress`` seam — DEC-MOVERS-WARM-PROGRESS-001); both ``None``
        when the running phase reports no progress.
    """

    state: str
    runs: int
    last_ok: bool | None
    phase: str | None = None
    started_at: str | None = None
    elapsed_s: float | None = None
    progress_done: int | None = None
    progress_total: int | None = None

    @property
    def blocking(self) -> bool:
        """``True`` iff the movers screens are currently gated."""
        return self.state == STATE_BLOCKING


class PrebuildGate:
    """Thread-safe pipeline↔movers gate (begin → BLOCKING, release → IDLE).

    Owned by the app instance (built/injected in ``build_app`` — CLAUDE.md §4,
    no module-global mutable state). The ``/pull-pipeline`` background job calls
    :meth:`begin` when the pull starts and :meth:`release` in a ``try/finally``
    when the prebuild finishes OR the pipeline stops early/fails — so a crashed
    pull can never wedge the movers shut. The ``/api/v1/movers`` route reads
    :meth:`snapshot` per request (O(1) — one lock acquisition, no I/O).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocking = False
        self._runs = 0
        self._last_ok: bool | None = None
        self._phase: str | None = None
        self._started_monotonic: float | None = None
        self._started_at: str | None = None
        self._progress: tuple[int, int] | None = None

    def begin(self) -> None:
        """A pipeline pull started — gate the movers (idempotent; counts runs).

        A double ``begin`` (defensive — the single-flight job makes it
        unreachable in production) simply keeps the gate blocking and bumps the
        run counter; it never raises. Stamps the run's start instant (the honest
        elapsed clock — DEC-PREBUILD-GATE-HONEST-NOTE-001) and opens at phase
        ``"pull"`` until :meth:`set_phase` names a finer stage.
        """
        with self._lock:
            self._blocking = True
            self._runs += 1
            self._phase = "pull"
            self._started_monotonic = time.monotonic()
            self._started_at = datetime.now(UTC).isoformat(timespec="seconds")
            self._progress = None
            runs = self._runs
        _LOG.info("movers.gate.begin", runs=runs)

    def set_phase(self, phase: str) -> None:
        """Annotate the CURRENT pipeline phase for the gated payload's honest note.

        Best-effort annotation (DEC-PREBUILD-GATE-HONEST-NOTE-001): O(1), one
        lock hold, never raises. Called next to the job's own ``set_phase`` at
        each pipeline stage; resets any prior phase's progress (a new phase's
        ``i/N`` starts clean). Safe on an idle gate (a dry-run pipeline never
        ``begin``s — the annotation is then invisible: the snapshot exposes
        phase/elapsed only while blocking).
        """
        with self._lock:
            self._phase = phase
            self._progress = None

    def set_progress(self, done: int, total: int) -> None:
        """Record the current phase's coordinate progress (``done``/``total``).

        Fed by ``warm_movers_snapshots``'s ``on_progress`` seam
        (DEC-MOVERS-WARM-PROGRESS-001) so the gated movers payload can show
        ``movers warm i/N`` live. O(1), one lock hold, never raises.
        """
        with self._lock:
            self._progress = (done, total)

    def release(self, ok: bool) -> None:
        """The prebuild finished (``ok``) or the pipeline stopped — un-gate movers.

        Safe without a prior :meth:`begin` (a no-op transition to IDLE, never a
        crash) so the binding site's ``try/finally`` can call it
        unconditionally. *ok* is the prebuild's success flag — ``False`` also
        covers "the pipeline never reached the prebuild" (an early stop /
        failure), recorded honestly in :attr:`GateSnapshot.last_ok`.
        """
        with self._lock:
            was_blocking = self._blocking
            self._blocking = False
            self._last_ok = ok
            self._phase = None
            self._started_monotonic = None
            self._started_at = None
            self._progress = None
        _LOG.info("pipeline.prebuild.gate_released", ok=ok, was_blocking=was_blocking)

    def snapshot(self) -> GateSnapshot:
        """Return the current :class:`GateSnapshot` — O(1), one lock hold."""
        with self._lock:
            blocking = self._blocking
            elapsed = (
                round(time.monotonic() - self._started_monotonic, 3)
                if blocking and self._started_monotonic is not None
                else None
            )
            progress = self._progress if blocking else None
            return GateSnapshot(
                state=STATE_BLOCKING if blocking else STATE_IDLE,
                runs=self._runs,
                last_ok=self._last_ok,
                phase=self._phase if blocking else None,
                started_at=self._started_at if blocking else None,
                elapsed_s=elapsed,
                progress_done=progress[0] if progress is not None else None,
                progress_total=progress[1] if progress is not None else None,
            )


def gated_movers_payload(
    snapshot: GateSnapshot,
    *,
    asset: str,
    interval: str,
    date: str | None,
    time: str | None,
    top_n: int,
) -> dict[str, Any]:
    """Build the honest 200 payload the ``/api/v1/movers`` route returns while gated.

    ZERO lake reads (Golden Rule #1 — the gate exists precisely so a half-filled
    lake is never read): the echo fields are pure (``date`` is normalised via
    :func:`~brutex.dashboard.movers_snapshot.parse_snapshot_date`, which raises
    :class:`ValueError` on a malformed value — the route maps it to a 400, the
    same contract as the normal path). Logs one ``movers.gate.blocked`` event per
    gated request (*asset* appears only in the log, never echoed into the
    payload — no reflected-input sink, DEC-DASHBOARD-XSS-HARDENING-001).

    The payload mirrors the normal snapshot echo shape (``interval`` / ``date``
    / ``time`` / ``top_n`` / ``instrument_count`` / ``skipped_count`` /
    ``note``) with ``gated: true`` and NO ranked category lists — the page reads
    ``j.gated`` and renders the note, then keeps polling until the gate lifts.

    HONEST NOTE + LIVE STATUS (DEC-PREBUILD-GATE-HONEST-NOTE-001): the old note
    said "prebuild in progress — movers available when finished", but the gate
    actually releases only AFTER the movers warm too, and a multi-hour stage
    showed no phase/elapsed at all. The note now states the real release
    condition, and the payload carries the gate's ``phase`` + ``started_at`` +
    ``elapsed_s`` (+ ``warm_done``/``warm_total`` when the movers warm reports
    progress — DEC-MOVERS-WARM-PROGRESS-001's ``on_progress`` seam) so
    ``movers.html`` can render "final stage: <phase> — running Xh Ym
    (auto-refreshes)" via ``textContent``. The NOTE itself stays STABLE per
    phase (no ticking elapsed inside it) so the page's paint-once gated-shell
    guard (DEC-DASHBOARD-JS-PERF-001) keeps skipping identical repaints; the
    ticking elapsed rides the separate ``elapsed_s`` field.

    Parameters
    ----------
    snapshot
        The gate's current :class:`GateSnapshot` (logged for provenance).
    asset
        The requested ``?asset=`` (``spot``/``futures``/``options``/other) —
        log-only.
    interval, date, time, top_n
        The request's echo coordinates (``date`` blank ⇒ today, pure — the
        gated path never resolves the lake's latest day).

    Raises
    ------
    ValueError
        On a malformed *date* (the route maps it to a 400).
    """
    date_str = parse_snapshot_date(date).isoformat()
    _LOG.info(
        "movers.gate.blocked",
        state=snapshot.state,
        runs=snapshot.runs,
        asset=asset,
        phase=snapshot.phase,
        elapsed_s=snapshot.elapsed_s,
    )
    phase_clause = f" (current stage: {snapshot.phase})" if snapshot.phase is not None else ""
    return {
        "gated": True,
        "interval": interval,
        "date": date_str,
        "time": time,
        "top_n": top_n,
        "instrument_count": 0,
        "skipped_count": 0,
        # Live-status fields for the frontend's textContent status line — the
        # ticking elapsed lives HERE, never inside the note, so the paint-once
        # gated-shell key (keyed on the note) stays stable per phase.
        "phase": snapshot.phase,
        "started_at": snapshot.started_at,
        "elapsed_s": snapshot.elapsed_s,
        "warm_done": snapshot.progress_done,
        "warm_total": snapshot.progress_total,
        "note": (
            f"pull pipeline running{phase_clause} — movers stay gated until the final "
            "prebuild AND the movers warm both finish, so a half-filled lake is never "
            "ranked (this screen retries automatically; no action needed)."
        ),
    }
