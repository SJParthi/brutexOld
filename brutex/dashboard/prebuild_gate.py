"""Movers **prebuild gate** — block the ``/movers`` screens during the PREBUILD phase.

``DEC-PIPELINE-AUTOPREBUILD-001`` (chunk 2 — the movers arm), since NARROWED at
BOTH ends. The one-click pull pipeline's FINAL data stage pre-builds every
derived timeframe (19 intraday + the derived ``1day``) for SPOT + FUTURES +
OPTIONS (:mod:`brutex.dashboard.pipeline_prebuild`). While THAT PHASE is
running, the movers screens would read a half-filled lake (stale derived
partitions mid-overwrite), so the gate blocks them — and ONLY during that
phase:

* START (``DEC-MOVERS-GATE-PREBUILD-SCOPE-001``): the pipeline's
  ``_prebuild_timeframes`` binding **begins** the gate as its FIRST statement
  (movers return an honest "prebuild in progress" payload with ZERO lake
  reads), so the gate deliberately does NOT cover the multi-hour fetch phases
  (the original chunk-2 binding gated the WHOLE pipeline, blanking the movers
  for hours of spot/metadata/historical fetching whose writes the movers can
  read live). During the fetch phases the spot movers additionally prefer the
  always-fresh 1-minute resample over the prebuilt stored partitions
  (``movers_snapshot.compute_spot_candidates(prefer_resample=True)`` while the
  pull runs), so an un-gated mid-fetch read never serves a stored bucket that
  predates the freshly-landing 1-minute data.
* END (``DEC-MOVERS-WARM-UNBLOCK-001``): the gate is **released the moment
  the prebuild outcome is known** (ok or failed — the half-filled-lake hazard
  the gate exists for ends at prebuild completion, so the movers warm that
  FOLLOWS runs un-gated in the background as a pure cache accelerator and can
  never hold ``/movers`` hostage; the 2026-07-04/05 incident held the gate for
  hours behind 39 slow warm keys) — or when the pipeline stops early / fails
  for ANY reason (the binding site ALSO releases in a ``try/finally``, the
  idempotent backstop), so a failed pull can NEVER leave the movers blocked
  forever.

While blocking, the honest note states that real release condition
(DEC-PREBUILD-GATE-HONEST-NOTE-001) and the payload names the RUNNING stage
(:attr:`GateSnapshot.phase`, fed by :meth:`PrebuildGate.set_phase`) plus the
live progress ``i/N`` (:meth:`PrebuildGate.set_progress` —
DEC-MOVERS-WARM-PROGRESS-001). Duration honesty rides FOUR stamps:
:attr:`GateSnapshot.started_at` (ISO-8601 UTC — human-readable provenance) +
:attr:`GateSnapshot.started_at_epoch` (epoch seconds — renderable client-side)
+ :attr:`GateSnapshot.elapsed_s` / :attr:`GateSnapshot.gated_for_s` (MONOTONIC
deltas — immune to wall-clock jumps).

## Honest envelope (Golden Rule #1 / #10)

* The gated payload is a **200 with ``gated: true``** and a note — never a 500,
  never a fabricated (or half-fresh) mover row. The page keeps polling and
  recovers by itself the moment the gate lifts.
* The gate's BEGIN/RELEASE state is **process-local** on the app instance
  (injected via ``build_app`` — no module-global mutable state, CLAUDE.md §4),
  but :meth:`PrebuildGate.snapshot` additionally consults an OPTIONAL
  **cross-process derive probe** (``cross_probe`` —
  DEC-DERIVE-LOCK-VISIBILITY-001, closing the named PrebuildGate follow-up of
  DEC-ENGINE-MISC-PACK-001): while a SEPARATE process (a CLI-launched pipeline
  / lake heal) holds the fixed ``<lake_root>/.locks/derive.lock`` exclusive
  flock (:func:`~brutex.data.derive_lock.derive_in_progress`), the movers gate
  honestly reads BLOCKING with :attr:`GateSnapshot.source` =
  :data:`SOURCE_CROSS_PROCESS` — a half-refreshed derived-TF set (incl. the
  prev-day ``1day`` baselines the futures/options movers read) is never
  ranked. HONEST envelope: the foreign process's phase/start instant is
  unknowable, so the cross-process snapshot carries phase
  :data:`CROSS_PROCESS_PHASE` and ``None`` stamps (never fabricated); the
  probe is a point-in-time signal refreshed per poll; a probe ``OSError``
  fails OPEN to idle with ONE ``movers.gate.cross_probe_failed`` WARN per
  outage (the movers read's own :func:`derive_in_progress` probe + resample
  fallback still guard the spot stored-first path); the probe does lake-volume
  I/O, so the route snapshots the gate via ``run_in_threadpool`` — never on
  the event loop.
* Only the FULL pipeline route (``/api/v1/universe/pull-pipeline``) gates; a
  ``dry_run`` pipeline (which writes no real data) does NOT gate.

Thread-safe: ``begin`` / ``set_phase`` / ``set_progress`` / ``release`` /
``snapshot`` serialise on one :class:`threading.Lock` — the pipeline job
thread flips it, the movers route's threadpool workers read it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import structlog

from brutex.dashboard.movers_snapshot import parse_snapshot_date

_LOG = structlog.get_logger(__name__)

__all__ = [
    "CROSS_PROCESS_PHASE",
    "SOURCE_CROSS_PROCESS",
    "SOURCE_PIPELINE",
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
#: :attr:`GateSnapshot.source` — this process's own pipeline gated (begin/release).
SOURCE_PIPELINE: Final[str] = "pipeline"
#: :attr:`GateSnapshot.source` — a SEPARATE process holds the cross-process
#: derive lock (DEC-DERIVE-LOCK-VISIBILITY-001).
SOURCE_CROSS_PROCESS: Final[str] = "cross_process"
#: The phase label a cross-process gated snapshot carries — the only honest
#: fact about the foreign holder is "a derive is in flight" (the derive lock
#: is held EXACTLY for the derived-TF rewrite); its finer stage / start
#: instant are unknowable cross-process and stay ``None``, never fabricated.
CROSS_PROCESS_PHASE: Final[str] = "derive"


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
    phase
        The gating pipeline's CURRENT phase label
        (``"pull"``/``"spot"``/``"metadata"``/``"historical"``/``"prebuild"``
        — DEC-PREBUILD-GATE-HONEST-NOTE-001), or ``None`` when not blocking,
        so the payload can name WHICH stage is actually running instead of
        the old blanket "prebuild in progress". Under the narrowed gate
        window — begin as the prebuild phase's first statement
        (DEC-MOVERS-GATE-PREBUILD-SCOPE-001) + release the moment the
        prebuild outcome is known (DEC-MOVERS-WARM-UNBLOCK-001) — the only
        phase observable while blocking is the prebuild itself: every
        fetch-phase annotation lands on a still-idle gate and the
        ``"movers"`` (warm) annotation lands on an already-released one, so
        neither is ever visible while blocking.
    started_at
        ISO-8601 UTC instant :meth:`PrebuildGate.begin` was called (``None``
        when not blocking).
    started_at_epoch
        The SAME begin instant as EPOCH seconds (wall clock — chosen because it
        is renderable client-side; a monotonic stamp is meaningless outside
        this process — DEC-MOVERS-GATE-PREBUILD-SCOPE-001), or ``None`` while
        idle.
    elapsed_s
        Wall-clock seconds since :meth:`PrebuildGate.begin` (``None`` when not
        blocking) — monotonic-clock derived, so a host clock step never yields
        a negative/backwards elapsed. Rounded to 3 dp for display.
    gated_for_s
        The SAME monotonic delta UNROUNDED (immune to wall-clock jumps —
        DEC-MOVERS-GATE-PREBUILD-SCOPE-001), or ``None`` while idle.
        Clamped ≥ 0.
    progress_done, progress_total
        The current phase's coordinate progress (the movers-warm ``i/N`` fed by
        :meth:`PrebuildGate.set_progress` via ``warm_movers_snapshots``'s
        ``on_progress`` seam — DEC-MOVERS-WARM-PROGRESS-001); both ``None``
        when the running phase reports no progress.
    progress_segment
        The progress's SEGMENT name (``"spot"``/``"futures"``/``"options"`` —
        DEC-MOVERS-WARM-UNBLOCK-001 honesty: a bare "6/9" read as
        7-of-9-TOTAL to the operator; the label makes it "futures 6/9"), or
        ``None`` when the reporter named none / not blocking.
    source
        WHO is gating (DEC-DERIVE-LOCK-VISIBILITY-001):
        :data:`SOURCE_PIPELINE` = this process's own pipeline (begin/release);
        :data:`SOURCE_CROSS_PROCESS` = a SEPARATE process holds the
        cross-process derive lock; ``None`` while idle (and on a snapshot
        constructed without the field — back-compat honest unknown, the
        payload note then keeps the pipeline wording).
    """

    state: str
    runs: int
    last_ok: bool | None
    phase: str | None = None
    started_at: str | None = None
    started_at_epoch: float | None = None
    elapsed_s: float | None = None
    gated_for_s: float | None = None
    progress_done: int | None = None
    progress_total: int | None = None
    progress_segment: str | None = None
    source: str | None = None

    @property
    def blocking(self) -> bool:
        """``True`` iff the movers screens are currently gated."""
        return self.state == STATE_BLOCKING


class PrebuildGate:
    """Thread-safe pipeline↔movers gate (begin → BLOCKING, release → IDLE).

    Owned by the app instance (built/injected in ``build_app`` — CLAUDE.md §4,
    no module-global mutable state). The ``/pull-pipeline`` background job calls
    :meth:`begin` when its PREBUILD phase starts (the first statement of the
    ``_prebuild_timeframes`` binding — DEC-MOVERS-GATE-PREBUILD-SCOPE-001, so
    the multi-hour fetch phases never gate) and :meth:`release` the MOMENT the
    prebuild outcome is known (DEC-MOVERS-WARM-UNBLOCK-001 — BEFORE the movers
    warm, which continues un-gated in the background), PLUS unconditionally in
    a ``try/finally`` (the idempotent backstop) so an early stop / raised phase
    / crashed pull can never wedge the movers shut. The ``/api/v1/movers``
    route reads :meth:`snapshot` per request — O(1): one lock acquisition,
    plus (idle, probe wired — DEC-DERIVE-LOCK-VISIBILITY-001) ONE non-blocking
    flock probe of the lake's derive lock, which is lake-volume I/O, so the
    route snapshots via ``run_in_threadpool`` (never on the event loop).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = _monotonic,
        wall_clock: Callable[[], float] = _wall_epoch,
        cross_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        #: Optional cross-process derive-lock probe
        #: (DEC-DERIVE-LOCK-VISIBILITY-001): a zero-arg callable returning
        #: ``True`` iff a SEPARATE process holds the lake's exclusive derive
        #: flock (``build_app`` wires ``derive_in_progress`` over the store's
        #: lake root). Consulted by :meth:`snapshot` ONLY while this process's
        #: own gate is idle; ``None`` (the default) keeps the gate purely
        #: process-local — byte-identical to the pre-DEC-DERIVE-LOCK-VISIBILITY-001
        #: behaviour (every existing injected-gate test rides this default).
        self._cross_probe = cross_probe
        #: Warn-once-per-outage flag for a failing probe (reset on the first
        #: successful probe) — a wedged/permission-broken lake volume logs ONE
        #: ``movers.gate.cross_probe_failed`` WARN, never a per-poll storm.
        self._cross_probe_warned = False
        self._lock = threading.Lock()
        self._blocking = False
        self._runs = 0
        self._last_ok: bool | None = None
        self._phase: str | None = None
        #: Monotonic stamp of the current begin (duration source) — ``None`` idle.
        self._started_monotonic: float | None = None
        #: ISO-8601 UTC stamp of the current begin (provenance) — ``None`` idle.
        self._started_at: str | None = None
        #: Epoch stamp of the current begin (display source) — ``None`` idle.
        self._started_at_epoch: float | None = None
        self._progress: tuple[int, int, str | None] | None = None

    def begin(self) -> None:
        """The pipeline's PREBUILD phase started — gate the movers (idempotent; counts runs).

        Called as the FIRST statement of the pipeline's ``_prebuild_timeframes``
        binding (DEC-MOVERS-GATE-PREBUILD-SCOPE-001 — the gate covers ONLY the
        prebuild phase, never the multi-hour fetch phases; the movers warm that
        follows runs un-gated too, the release having fired the moment the
        prebuild outcome was known — DEC-MOVERS-WARM-UNBLOCK-001). A double
        ``begin`` (defensive — the single-flight job makes it
        unreachable in production) keeps the gate blocking, bumps the run
        counter and RE-STAMPS the start clocks to the newest begin; it never
        raises. Stamps the run's start instant (the honest elapsed clock —
        DEC-PREBUILD-GATE-HONEST-NOTE-001) and opens at phase ``"pull"`` until
        :meth:`set_phase` names a finer stage (the binding site annotates
        ``"prebuild"`` immediately after this call).
        """
        with self._lock:
            self._blocking = True
            self._runs += 1
            self._phase = "pull"
            epoch = self._wall_clock()
            self._started_monotonic = self._clock()
            self._started_at = datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")
            self._started_at_epoch = epoch
            self._progress = None
            runs = self._runs
        _LOG.info("movers.gate.begin", runs=runs)

    def set_phase(self, phase: str) -> None:
        """Annotate the CURRENT pipeline phase for the gated payload's honest note.

        Best-effort annotation (DEC-PREBUILD-GATE-HONEST-NOTE-001): O(1), one
        lock hold, never raises. Called next to the job's own ``set_phase`` at
        each pipeline stage; resets any prior phase's progress (a new phase's
        ``i/N`` starts clean). Safe on an idle gate (a dry-run pipeline never
        ``begin``s — and under the narrowed scope of
        DEC-MOVERS-GATE-PREBUILD-SCOPE-001 every FETCH-phase annotation lands
        on a still-idle gate; the annotation is then invisible: the snapshot
        exposes phase/elapsed only while blocking).
        """
        with self._lock:
            self._phase = phase
            self._progress = None

    def set_progress(self, done: int, total: int, *, segment: str | None = None) -> None:
        """Record the current phase's coordinate progress (``done``/``total``).

        Fed by ``warm_movers_snapshots``'s ``on_progress`` seam
        (DEC-MOVERS-WARM-PROGRESS-001) so the gated movers payload can show
        ``movers warm i/N`` live. *segment* optionally NAMES which segment the
        ``i/N`` belongs to (DEC-MOVERS-WARM-UNBLOCK-001 — a bare "6/9" was the
        per-SEGMENT count, misread as 7-of-9-TOTAL; the payload renders it
        "futures 6/9"). O(1), one lock hold, never raises.
        """
        with self._lock:
            self._progress = (done, total, segment)

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
        stamps + phase + progress (an idle gate exposes none of them).
        """
        with self._lock:
            was_blocking = self._blocking
            self._blocking = False
            self._last_ok = ok
            self._phase = None
            self._started_monotonic = None
            self._started_at = None
            self._started_at_epoch = None
            self._progress = None
        _LOG.info("pipeline.prebuild.gate_released", ok=ok, was_blocking=was_blocking)

    def snapshot(self) -> GateSnapshot:
        """Return the current :class:`GateSnapshot` — O(1), one lock hold.

        Resolution order (DEC-DERIVE-LOCK-VISIBILITY-001): this process's own
        pipeline state wins (``source="pipeline"`` with the full phase /
        stamps / progress echo); only while it is IDLE is the optional
        cross-process derive probe consulted — OUTSIDE the gate lock, because
        the probe does one O(1) flock syscall on the lake volume and holding
        the lock across a wedged volume would block the pipeline job's own
        ``begin``/``release``. A held cross-process derive lock reads BLOCKING
        with ``source="cross_process"``, phase :data:`CROSS_PROCESS_PHASE`,
        and ``None`` stamps (the foreign holder's start instant is unknowable
        — never fabricated).
        """
        now = self._clock()
        with self._lock:
            blocking = self._blocking
            runs = self._runs
            last_ok = self._last_ok
            gated_for_s: float | None = None
            elapsed: float | None = None
            if blocking and self._started_monotonic is not None:
                # ONE monotonic delta, two honest surfaces: `gated_for_s` raw
                # (DEC-MOVERS-GATE-PREBUILD-SCOPE-001) + `elapsed_s` rounded 3 dp
                # (DEC-PREBUILD-GATE-HONEST-NOTE-001); both clamped ≥ 0 so an
                # injected-backwards test clock never yields a negative duration.
                gated_for_s = max(now - self._started_monotonic, 0.0)
                elapsed = round(gated_for_s, 3)
            progress = self._progress if blocking else None
            if blocking:
                return GateSnapshot(
                    state=STATE_BLOCKING,
                    runs=runs,
                    last_ok=last_ok,
                    phase=self._phase,
                    started_at=self._started_at,
                    started_at_epoch=self._started_at_epoch,
                    elapsed_s=elapsed,
                    gated_for_s=gated_for_s,
                    progress_done=progress[0] if progress is not None else None,
                    progress_total=progress[1] if progress is not None else None,
                    progress_segment=progress[2] if progress is not None else None,
                    source=SOURCE_PIPELINE,
                )
        # Process-locally idle — consult the cross-process derive probe (a
        # no-probe gate short-circuits to the byte-identical idle snapshot).
        if self._cross_derive_blocking():
            return GateSnapshot(
                state=STATE_BLOCKING,
                runs=runs,
                last_ok=last_ok,
                phase=CROSS_PROCESS_PHASE,
                source=SOURCE_CROSS_PROCESS,
            )
        return GateSnapshot(state=STATE_IDLE, runs=runs, last_ok=last_ok)

    def _cross_derive_blocking(self) -> bool:
        """``True`` iff the cross-process derive probe reports a held lock.

        Best-effort (DEC-DERIVE-LOCK-VISIBILITY-001): no probe wired →
        ``False`` with zero syscalls. A probe :class:`OSError` (a wedged /
        permission-broken lake volume) FAILS OPEN to ``False`` — the movers
        then serve normally, still guarded by the spot stored-first read's own
        :func:`~brutex.data.derive_lock.derive_in_progress` probe + resample
        fallback — with ONE ``movers.gate.cross_probe_failed`` WARN per outage
        (the flag resets on the first successful probe). A non-``OSError`` is
        a genuine code bug and propagates (CLAUDE.md §4 — no bare except).
        """
        if self._cross_probe is None:
            return False
        try:
            held = self._cross_probe()
        except OSError as exc:
            with self._lock:
                first = not self._cross_probe_warned
                self._cross_probe_warned = True
            if first:
                _LOG.warning(
                    "movers.gate.cross_probe_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            return False
        with self._lock:
            self._cross_probe_warned = False
        return held


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

    HONEST NOTE + LIVE STATUS (DEC-PREBUILD-GATE-HONEST-NOTE-001, release
    condition re-stated by DEC-MOVERS-WARM-UNBLOCK-001): the note states the
    REAL release condition — the gate lifts the moment the FINAL prebuild's
    outcome is known (the movers warm that follows runs un-gated in the
    background as a pure accelerator, never a hold on this screen) — and the
    payload carries the gate's ``phase`` + ``started_at`` (ISO UTC) +
    ``started_at_epoch`` (epoch — renderable client-side,
    DEC-MOVERS-GATE-PREBUILD-SCOPE-001) + ``elapsed_s`` / ``gated_for_s``
    (monotonic deltas)
    (+ ``warm_done``/``warm_total``/``warm_segment`` when a phase reports
    progress — DEC-MOVERS-WARM-PROGRESS-001's ``on_progress`` seam; the
    SEGMENT name makes "6/9" read "futures 6/9", never mistaken for
    7-of-9-total) so ``movers.html`` can render "final stage: <phase> —
    running Xh Ym (auto-refreshes)" via ``textContent``. The NOTE itself stays
    STABLE per phase (no ticking elapsed inside it) so the page's paint-once
    gated-shell guard (DEC-DASHBOARD-JS-PERF-001) keeps skipping identical
    repaints; the ticking elapsed rides the separate ``elapsed_s`` /
    ``gated_for_s`` fields.

    A CROSS-PROCESS gated snapshot (``source == "cross_process"`` —
    DEC-DERIVE-LOCK-VISIBILITY-001) carries its own stable note naming the
    real release condition (the foreign process releasing the derive lock);
    the payload's additive ``source`` key says WHO gates. The PIPELINE note
    text is byte-unchanged (pinned), and a legacy snapshot constructed
    without ``source`` (``None``) keeps the pipeline wording — back-compat,
    never a fabricated cross-process claim.

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
        source=snapshot.source,
    )
    if snapshot.source == SOURCE_CROSS_PROCESS:
        # DEC-DERIVE-LOCK-VISIBILITY-001: a SEPARATE process holds the lake's
        # derive lock — name the real (cross-process) release condition. The
        # note is STABLE (no ticking numbers) so the paint-once gated-shell
        # guard keeps skipping identical repaints.
        note = (
            "a derive in another process (a CLI-launched pipeline or lake heal "
            "on this lake) is rewriting the derived timeframes — movers stay "
            "gated until it releases the cross-process derive lock, so a "
            "half-refreshed timeframe set is never ranked (this screen retries "
            "automatically; no action needed)."
        )
    else:
        phase_clause = f" (current stage: {snapshot.phase})" if snapshot.phase is not None else ""
        note = (
            f"pull pipeline running{phase_clause} — movers stay gated until the final "
            "prebuild finishes (the movers warm then continues un-gated in the "
            "background), so a half-filled lake is never ranked (this screen retries "
            "automatically; no action needed)."
        )
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
        "started_at_epoch": snapshot.started_at_epoch,
        "elapsed_s": snapshot.elapsed_s,
        "gated_for_s": snapshot.gated_for_s,
        "warm_done": snapshot.progress_done,
        "warm_total": snapshot.progress_total,
        "warm_segment": snapshot.progress_segment,
        # WHO gates (DEC-DERIVE-LOCK-VISIBILITY-001): "pipeline" (this
        # process's own pull) / "cross_process" (a separate-process derive) /
        # None (a legacy snapshot constructed without the field — honest
        # unknown, the note keeps the pipeline wording). Additive key.
        "source": snapshot.source,
        "note": note,
    }
