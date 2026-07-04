"""Movers **prebuild gate** — block the ``/movers`` screens during the PREBUILD phase.

``DEC-PIPELINE-AUTOPREBUILD-001`` (chunk 2 — the movers arm), NARROWED by
``DEC-MOVERS-GATE-PREBUILD-SCOPE-001``: the one-click pull pipeline's FINAL
data stage pre-builds every derived timeframe (19 intraday + the derived
``1day``) for SPOT + FUTURES + OPTIONS
(:mod:`brutex.dashboard.pipeline_prebuild`). While THAT PHASE is running, the
movers screens would read derived partitions mid-overwrite, so the gate blocks
them: the pipeline's ``_prebuild_timeframes`` binding **begins** the gate as
its FIRST statement (movers return an honest "prebuild in progress" payload
with ZERO lake reads) and the gate is **released** when the pipeline job ends
— clean, early-stopped, or raised (the binding site releases in a
``try/finally``), so a failed pull can NEVER leave the movers blocked forever.

The gate's honest meaning is "prebuild in progress" — it deliberately does NOT
cover the multi-hour fetch phases any more (the original chunk-2 binding
gated the WHOLE pipeline, blanking the movers for hours of spot/metadata/
historical fetching whose writes the movers can read live). During the fetch
phases the spot movers additionally prefer the always-fresh 1-minute resample
over the prebuilt stored partitions (``movers_snapshot.compute_spot_candidates
(prefer_resample=True)`` while the pull runs), so an un-gated mid-fetch read
never serves a stored bucket that predates the freshly-landing 1-minute data.
:attr:`GateSnapshot.started_at` (epoch seconds — renderable wall-clock) +
:attr:`GateSnapshot.gated_for_s` (a MONOTONIC delta — immune to wall-clock
jumps) surface how long the prebuild has been gating.

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
from collections.abc import Callable
from dataclasses import dataclass
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

#: No prebuild phase is running — movers serve normally.
STATE_IDLE: Final[str] = "idle"
#: The pipeline's PREBUILD phase is running (not yet released) — movers
#: return the honest gated payload without touching the lake
#: (DEC-MOVERS-GATE-PREBUILD-SCOPE-001 narrowed the gate to this one phase).
STATE_BLOCKING: Final[str] = "blocking"


def _monotonic() -> float:
    """Default duration clock — monotonic (injected for testability)."""
    import time

    return time.monotonic()


def _wall_epoch() -> float:
    """Default display clock — epoch seconds (injected for testability)."""
    import time

    return time.time()


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
    started_at
        EPOCH seconds when the currently-blocking prebuild began (wall clock —
        chosen because it is renderable client-side; a monotonic stamp is
        meaningless outside this process), or ``None`` while idle.
    gated_for_s
        How long the currently-blocking prebuild has been gating, as a
        MONOTONIC clock delta (chosen for the duration because it is immune to
        wall-clock jumps — DEC-MOVERS-GATE-PREBUILD-SCOPE-001), or ``None``
        while idle. Clamped ≥ 0.
    """

    state: str
    runs: int
    last_ok: bool | None
    started_at: float | None = None
    gated_for_s: float | None = None

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

    def __init__(
        self,
        *,
        clock: Callable[[], float] = _monotonic,
        wall_clock: Callable[[], float] = _wall_epoch,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._blocking = False
        self._runs = 0
        self._last_ok: bool | None = None
        #: Monotonic stamp of the current begin (duration source) — ``None`` idle.
        self._started_monotonic: float | None = None
        #: Epoch stamp of the current begin (display source) — ``None`` idle.
        self._started_at: float | None = None

    def begin(self) -> None:
        """The pipeline's PREBUILD phase started — gate the movers (idempotent; counts runs).

        Called as the FIRST statement of the pipeline's ``_prebuild_timeframes``
        binding (DEC-MOVERS-GATE-PREBUILD-SCOPE-001 — the gate covers ONLY the
        prebuild phase, never the multi-hour fetch phases). A double ``begin``
        (defensive — the single-flight job makes it unreachable in production)
        keeps the gate blocking, bumps the run counter and RE-STAMPS the start
        clocks to the newest begin; it never raises.
        """
        with self._lock:
            self._blocking = True
            self._runs += 1
            self._started_monotonic = self._clock()
            self._started_at = self._wall_clock()
            runs = self._runs
        _LOG.info("movers.gate.begin", runs=runs)

    def release(self, ok: bool) -> None:
        """The prebuild finished (``ok``) or the pipeline stopped — un-gate movers.

        Safe without a prior :meth:`begin` (a no-op transition to IDLE, never a
        crash) so the binding site's ``try/finally`` can call it
        unconditionally — under the narrowed scope
        (DEC-MOVERS-GATE-PREBUILD-SCOPE-001) that no-begin path is now the
        NORMAL one for every pipeline that stops before its prebuild phase.
        *ok* is the prebuild's success flag — ``False`` also covers "the
        pipeline never reached the prebuild" (an early stop / failure),
        recorded honestly in :attr:`GateSnapshot.last_ok`. Clears the start
        stamps (an idle gate has no ``started_at`` / ``gated_for_s``).
        """
        with self._lock:
            was_blocking = self._blocking
            self._blocking = False
            self._last_ok = ok
            self._started_monotonic = None
            self._started_at = None
        _LOG.info("pipeline.prebuild.gate_released", ok=ok, was_blocking=was_blocking)

    def snapshot(self) -> GateSnapshot:
        """Return the current :class:`GateSnapshot` — O(1), one lock hold."""
        now = self._clock()
        with self._lock:
            gated_for_s: float | None = None
            if self._blocking and self._started_monotonic is not None:
                gated_for_s = max(now - self._started_monotonic, 0.0)
            return GateSnapshot(
                state=STATE_BLOCKING if self._blocking else STATE_IDLE,
                runs=self._runs,
                last_ok=self._last_ok,
                started_at=self._started_at if self._blocking else None,
                gated_for_s=gated_for_s,
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
    It additionally carries the static ``phase: "prebuild"`` (the gate covers
    ONLY the prebuild phase — DEC-MOVERS-GATE-PREBUILD-SCOPE-001) plus the
    gate's ``started_at`` (epoch) / ``gated_for_s`` (monotonic delta) so the
    page can show HOW LONG the prebuild has been gating (both ``None`` only in
    the defensive idle-snapshot case — the route calls this only while
    blocking).

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
    _LOG.info("movers.gate.blocked", state=snapshot.state, runs=snapshot.runs, asset=asset)
    return {
        "gated": True,
        "phase": "prebuild",
        "started_at": snapshot.started_at,
        "gated_for_s": snapshot.gated_for_s,
        "interval": interval,
        "date": date_str,
        "time": time,
        "top_n": top_n,
        "instrument_count": 0,
        "skipped_count": 0,
        "note": (
            "prebuild in progress — movers available when finished: the pull pipeline is "
            "writing the derived timeframe partitions the movers read, so this screen is "
            "gated until that final stage completes (it retries automatically; no action "
            "needed)."
        ),
    }
