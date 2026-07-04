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
from dataclasses import dataclass
from typing import Any, Final

import structlog

from brutex.dashboard.movers_snapshot import parse_snapshot_date

_LOG = structlog.get_logger(__name__)

__all__ = [
    "GENERIC_GATE_NOTE",
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

#: The phase-agnostic gated note — BYTE-IDENTICAL to the pre-phase-aware wording
#: (DEC-PIPELINE-AUTOPREBUILD-001 chunk 2), kept as the honest fallback whenever the
#: caller has no phase signal (``phase=None`` / ``""``) or hands an unknown label
#: (DEC-PIPELINE-PHASE-UI-HONESTY-001 — never a fabricated phase claim, Golden Rule #1).
GENERIC_GATE_NOTE: Final[str] = (
    "prebuild in progress — movers available when finished: the pull pipeline is "
    "writing the derived timeframe partitions the movers read, so this screen is "
    "gated until that final stage completes (it retries automatically; no action "
    "needed)."
)

#: Per-phase HONEST gated notes (DEC-PIPELINE-PHASE-UI-HONESTY-001). The keys are
#: EXACTLY the pipeline's real ``set_phase`` labels — ``spot`` / ``metadata`` /
#: ``historical`` / ``movers`` / ``prebuild`` (verified exhaustive over every
#: ``set_phase(`` call site; there is NO "greeks" label — the Greeks enrichment runs
#: INSIDE the ``historical`` phase, so its note names it there). The gate is DOWN for
#: the WHOLE pipeline run, so the old single "prebuild in progress" wording over-claimed
#: during the spot/metadata/historical phases; each note now names what is ACTUALLY
#: running while keeping the same promise: movers stay gated until the pipeline's FINAL
#: prebuild stage completes and the screen recovers by itself.
_PHASE_NOTES: Final[dict[str, str]] = {
    "spot": (
        "pull pipeline running — phase: spot 1-minute fetch. Movers are gated until "
        "the pipeline's final timeframe prebuild completes (it retries automatically; "
        "no action needed)."
    ),
    "metadata": (
        "pull pipeline running — phase: F&O expiries + contracts metadata fetch. "
        "Movers are gated until the pipeline's final timeframe prebuild completes "
        "(it retries automatically; no action needed)."
    ),
    "historical": (
        "pull pipeline running — phase: F&O historical 1-minute fetch (incl. the "
        "Greeks enrichment). Movers are gated until the pipeline's final timeframe "
        "prebuild completes (it retries automatically; no action needed)."
    ),
    "prebuild": (
        "pull pipeline running — phase: timeframe prebuild, writing the derived "
        "timeframe partitions the movers read. Movers recover the moment this final "
        "stage completes (it retries automatically; no action needed)."
    ),
    "movers": (
        "pull pipeline running — phase: movers warm-up (local cache warm, no vendor "
        "calls). This screen recovers automatically in a few seconds; no action "
        "needed."
    ),
}


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
    """

    state: str
    runs: int
    last_ok: bool | None

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

    def begin(self) -> None:
        """A pipeline pull started — gate the movers (idempotent; counts runs).

        A double ``begin`` (defensive — the single-flight job makes it
        unreachable in production) simply keeps the gate blocking and bumps the
        run counter; it never raises.
        """
        with self._lock:
            self._blocking = True
            self._runs += 1
            runs = self._runs
        _LOG.info("movers.gate.begin", runs=runs)

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
        _LOG.info("pipeline.prebuild.gate_released", ok=ok, was_blocking=was_blocking)

    def snapshot(self) -> GateSnapshot:
        """Return the current :class:`GateSnapshot` — O(1), one lock hold."""
        with self._lock:
            return GateSnapshot(
                state=STATE_BLOCKING if self._blocking else STATE_IDLE,
                runs=self._runs,
                last_ok=self._last_ok,
            )


def gated_movers_payload(
    snapshot: GateSnapshot,
    *,
    asset: str,
    interval: str,
    date: str | None,
    time: str | None,
    top_n: int,
    phase: str | None = None,
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
    ``note``) with ``gated: true``, the normalised ``phase`` echo, and NO ranked
    category lists — the page reads ``j.gated`` and renders the note, then keeps
    polling until the gate lifts.

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
    phase
        The pipeline's CURRENT coarse phase label (the route reads it from the
        background job's ``PullProgress`` snapshot — our own ``set_phase``
        strings, never user input; DEC-PIPELINE-PHASE-UI-HONESTY-001). A known
        label (``spot`` / ``metadata`` / ``historical`` / ``movers`` /
        ``prebuild``) selects its honest per-phase note; ``None`` / ``""`` / an
        unknown label falls back to :data:`GENERIC_GATE_NOTE` — BYTE-IDENTICAL
        to the pre-phase-aware wording, never a fabricated phase claim.

    Raises
    ------
    ValueError
        On a malformed *date* (the route maps it to a 400).
    """
    date_str = parse_snapshot_date(date).isoformat()
    phase_str = phase or ""
    _LOG.info(
        "movers.gate.blocked",
        state=snapshot.state,
        runs=snapshot.runs,
        asset=asset,
        phase=phase_str,
    )
    return {
        "gated": True,
        "interval": interval,
        "date": date_str,
        "time": time,
        "top_n": top_n,
        "instrument_count": 0,
        "skipped_count": 0,
        "phase": phase_str,
        "note": _PHASE_NOTES.get(phase_str, GENERIC_GATE_NOTE),
    }
