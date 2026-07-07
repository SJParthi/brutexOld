"""Periodic live-rate panel sampler — keep the PULL RATE widget fed across every pull phase.

DEC-PULL-RATE-WIDGET-VISIBILITY-001. The dashboard ``/api/v1/pull/rate`` poll (the
:mod:`~brutex.dashboard.bx-rate-monitor` widget) projects the in-memory
:class:`~brutex.dashboard.app.RateLimitPanel`, which is fed by ``_observe_rate`` ONLY on
the SPOT phase's per-window ``on_window`` tick
(:func:`~brutex.dashboard.pull_controller.make_window_observer`). The METADATA phase
(:func:`~brutex.dashboard.metadata_pull.pull_expiries_contracts`) and the long F&O /
historical phase (:func:`~brutex.dashboard.composite_pull.pull_historical_for_underlyings`
→ :func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest`) have NO such
rate-observe seam, so after the ~42 s spot phase the panel's
``last_update_monotonic`` stops moving and — once
:data:`~brutex.dashboard.pull_rate_view.STALE_AFTER_S` (12 s) elapses — the route reports
``idle`` for the ENTIRE ~38-minute F&O phase even though the SHARED rate controller is
actively pacing real fetches. The widget goes blank mid-pull.

This module closes that gap with ONE COMMON, observe-only mechanism. While a pull runs,
:class:`RatePanelSampler` ticks every :data:`DEFAULT_SAMPLE_INTERVAL_S` on a background
daemon and pushes the SHARED
:class:`~brutex.dashboard.pull_controller.SessionRateController`'s
:meth:`~brutex.adapters.rate_common.RateController.snapshot` to the panel through the
EXISTING :func:`~brutex.dashboard.app.make_rate_observer` callback (CLAUDE.md §11 COMMON —
no copy of the panel-mapping logic). The SAME controller paces the metadata + F&O phases
(:func:`~brutex.dashboard.historical_pull._rate_hooks` reads ``session_rate.get()``), so a
periodic snapshot of it keeps the panel fresh through BOTH phases with a single watcher
covering everything the spot per-window seam does not.

## Honest envelope (Golden Rule #1 + #10)

* **Observe-only.** The sampler READS the controller's :meth:`snapshot` and WRITES the
  panel — it touches NO pacing decision (it never calls ``pace`` / ``on_success`` /
  ``on_rate_limit``). The fetch rate is byte-for-byte UNCHANGED.
* **Never builds a controller.** It reads via
  :meth:`~brutex.dashboard.pull_controller.SessionRateController.peek` (which returns
  ``None`` until a real pull has built one), so a dry-run / pre-pull tick pushes NOTHING
  and the panel stays honestly idle — never a fabricated "live" reading.
* **MEASURED "achieved" completed-rate when the meter is reachable, else an HONEST 0**
  (DEC-RATE-WIDGET-FNO-MEASURED-001, the follow-up to DEC-RATE-WIDGET-FNO-HONEST-001). The
  transport call-meter's MEASURED completed-rate (``effective_rps``) lives in the F&O / spot /
  metadata client pool, built deep in the fetch spine. #1022 could not reach it from this seam
  and so pushed a bare ``meter_present=False`` sentinel; NOW the pull build sites REGISTER the
  pool's shared meter into the session controller
  (:meth:`~brutex.dashboard.pull_controller.SessionRateController.register_meter`), so the
  sampler reads it via
  :meth:`~brutex.dashboard.pull_controller.SessionRateController.peek_meter` and pushes the
  meter's REAL ``snapshot`` — the true MEASURED completed rate — during the metadata + F&O
  phases too. When no meter is registered (the pre-registration window / a dry-run / after a
  finished job) it falls back to the honest ``meter_present=False`` sentinel; a dry-run
  explicitly DEREGISTERS at the build sites and the job-finish hook clears the meter, so
  ``peek_meter()`` between jobs is ``None`` by invariant (DEC-RATE-METER-LIFECYCLE-001) — so
  "achieved" stays an
  HONEST 0 (never the old ``recent_minute / 60`` fabrication, which both OVER-reported —
  admissions ≥ completions — and BLINDED the ``stalled`` verdict to an issued-but-all-failing
  stall). The admission per-minute average is still surfaced in its OWN labelled ``req/min
  rate`` field; the ``req/min`` + ``req/hour`` gauges read the same ``recent_minute`` the spot
  path does.
* **O(1) per tick + bounded.** Each tick is one ``snapshot`` (a small O(1) lock) + one
  panel write; the loop sleeps :data:`DEFAULT_SAMPLE_INTERVAL_S` between ticks. No
  unbounded growth, no look-ahead (it reads only the controller's current state).
* **Best-effort.** A snapshot / observer error on a tick is swallowed + logged, so the
  sampler can NEVER block, slow, or crash the pull it is merely watching.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from brutex.dashboard.pull_controller import RateObserver, SessionRateController

#: Seconds between live-panel sample ticks. The spot per-window observe fires at ~1 s
#: under load and the route's idle threshold
#: (:data:`~brutex.dashboard.pull_rate_view.STALE_AFTER_S`) is 12 s, so a 2 s cadence keeps
#: the panel comfortably fresh (≥ one tick well inside every staleness window) without
#: spinning — observability only; it changes no pacing decision.
DEFAULT_SAMPLE_INTERVAL_S: Final[float] = 2.0

#: Background-loop spawner type — a callable that runs *fn* off the calling thread. The
#: production default spawns a daemon thread; tests inject a capturing fake for
#: deterministic, thread-free coverage (mirrors
#: :class:`~brutex.dashboard.pull_stall.PullStallWatchdog`'s ``spawn`` seam).
SamplerSpawn = Callable[[Callable[[], None]], None]

_LOG = structlog.get_logger("brutex.dashboard.rate_sampler")


def _daemon_spawn(fn: Callable[[], None]) -> None:
    """Run *fn* on a named daemon thread (the production background-loop spawner)."""
    threading.Thread(target=fn, name="dashboard-rate-sampler", daemon=True).start()


class RatePanelSampler:
    """Tick the live-rate panel from the shared controller while a pull's later phases run.

    Use as a context manager around the metadata + F&O phases of the pull pipeline::

        with RatePanelSampler(session_rate=_session_rate, observer=make_rate_observer(state)):
            run_full_pipeline(...)

    On ``__enter__`` it starts a background loop that, every *interval* seconds, reads the
    SHARED controller (:meth:`SessionRateController.peek` — never building one) and, when a
    controller exists, pushes its :meth:`snapshot` to the panel via *observer* (the
    :func:`~brutex.dashboard.app.make_rate_observer` callback). On ``__exit__`` it stops the
    loop. The whole thing is observe-only + best-effort (a tick error is swallowed), so it
    can never affect the pull.
    """

    def __init__(
        self,
        *,
        session_rate: SessionRateController,
        observer: RateObserver,
        interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_S,
        spawn: SamplerSpawn = _daemon_spawn,
    ) -> None:
        """Build a sampler.

        Parameters
        ----------
        session_rate
            The dashboard's shared
            :class:`~brutex.dashboard.pull_controller.SessionRateController`. Read via
            :meth:`~brutex.dashboard.pull_controller.SessionRateController.peek` (never
            built) so a dry-run / pre-pull tick conjures no live controller.
        observer
            The panel-feeding callback (production: ``make_rate_observer(state)``). Given a
            ``(controller_snapshot, meter_snapshot)`` pair it updates the rate panel; this
            is the SAME callback the spot per-window observe uses (CLAUDE.md §11 COMMON).
        interval_seconds
            Seconds between ticks (default :data:`DEFAULT_SAMPLE_INTERVAL_S`).
        spawn
            Background-loop spawner (default a daemon thread). Tests inject a capturing
            fake so the loop body is driven deterministically with zero threads.
        """
        self._session_rate = session_rate
        self._observer = observer
        self._interval = interval_seconds
        self._spawn = spawn
        self._stop = threading.Event()
        #: ``True`` between a :meth:`start` that spawned a daemon and the matching
        #: :meth:`stop`. Guards :meth:`start` against spawning a SECOND daemon on a
        #: redundant call: the old ``start`` always spawned, so a double-``start`` leaked
        #: a thread that shared the single ``_stop`` event (a single :meth:`stop` then
        #: could not reliably halt both). A second ``start`` while already running is now a
        #: no-op, making the "idempotent re-arm" the docstring promises actually true.
        self._running = False

    def sample_once(self) -> bool:
        """Push ONE controller (+ meter, when live) snapshot to the panel — returns if a tick fired.

        Reads the shared controller via
        :meth:`~brutex.dashboard.pull_controller.SessionRateController.peek`. When no
        controller is built yet (a dry-run / a pull that has not paced anything) it
        returns ``False`` and pushes nothing — the panel stays honestly idle. Otherwise it
        snapshots the controller and feeds the *observer* the
        ``(controller_snapshot, meter_snapshot)`` pair, returning ``True``.

        The meter snapshot is now HONEST-and-MEASURED when a meter is reachable
        (DEC-RATE-WIDGET-FNO-MEASURED-001): the pull build sites REGISTER the live client
        pool's shared transport :class:`~brutex.adapters.call_meter.CallMeter` into the
        session controller, so this reads it via
        :meth:`~brutex.dashboard.pull_controller.SessionRateController.peek_meter` and, when
        present, pushes the meter's REAL :meth:`~brutex.adapters.call_meter.CallMeter.snapshot`
        (the MEASURED completed ``effective_rps`` — ``meter_present`` then defaults ``True`` in
        the observer, since a real snapshot carries no such key). When NO meter is
        registered — the pre-registration window at pull start, a dry-run (which explicitly
        DEREGISTERS at the build sites), or after a finished job (the ``on_run_end`` hook
        clears the meter, so ``peek_meter()`` between jobs is ``None`` by invariant —
        DEC-RATE-METER-LIFECYCLE-001) — it falls back to the
        honest ``{"meter_present": False}`` sentinel of DEC-RATE-WIDGET-FNO-HONEST-001 — NO
        fabricated ``effective_rps`` (never the old ``recent_minute / 60`` admission-derived
        figure the removed sampler poured into the "achieved" slot, which over-reported AND
        blinded the stall verdict), so the view keeps "achieved" an HONEST 0 and bases the
        stall verdict on the controller's admission signal.

        Best-effort: a snapshot / observer error is swallowed + logged (the sampler can
        never crash the pull it watches) and reported as ``False``. O(1) per call (one
        controller snapshot + one meter snapshot, each a small bounded lock).
        """
        controller = self._session_rate.peek()
        if controller is None:
            return False
        try:
            snap = controller.snapshot()
            # The controller snapshot carries the ALLOWED rate + the MEASURED rolling-60s
            # ADMISSION count. The transport call-meter's MEASURED COMPLETED rate
            # (``effective_rps``) is now REACHABLE via the session controller's registered
            # meter (DEC-RATE-WIDGET-FNO-MEASURED-001 — the pull build sites publish the pool's
            # shared meter there). When a meter is registered, push its REAL snapshot (a
            # measured completed-rate; the observer defaults ``meter_present=True`` because a
            # genuine CallMeter snapshot has no such key). When none is registered yet (a
            # dry-run / a phase before the client pool exists), fall back to the honest
            # ``meter_present=False`` sentinel — NO fabricated ``effective_rps`` — so the view
            # keeps "achieved" an HONEST 0 and bases the stall verdict on the admission signal
            # (DEC-RATE-WIDGET-FNO-HONEST-001), never a made-up completed rate.
            meter = self._session_rate.peek_meter()
            meter_snapshot: dict[str, object]
            if meter is not None:
                meter_snapshot = dict(meter.snapshot())
            else:
                meter_snapshot = {"meter_present": False}
            self._observer(snap, meter_snapshot)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
            _LOG.exception("rate_sampler.tick_failed")
            return False
        return True

    def _drive(self) -> None:
        """Background-loop body: :meth:`sample_once` every *interval* s until stopped.

        Exits promptly when :meth:`stop` sets the event (``Event.wait`` returns ``True``);
        a timeout (``False``) fires one tick and loops. Mirrors
        :meth:`brutex.dashboard.pull_stall.PullStallWatchdog._drive`.
        """
        while not self._stop.wait(self._interval):
            self.sample_once()

    def start(self) -> None:
        """Clear the stop flag + launch the background sample loop (idempotent re-arm).

        Guarded against a double-start: if a daemon is already running
        (:attr:`_running` is ``True`` and :meth:`stop` has not since fired) this is a
        no-op, so a redundant ``start`` never spawns a SECOND daemon sharing the one
        ``_stop`` event. After a :meth:`stop` the sampler can be re-armed by another
        ``start`` (the genuine re-arm the docstring promises).
        """
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._spawn(self._drive)

    def stop(self) -> None:
        """Signal the background loop to exit at its next wake (O(1), thread-safe).

        Clears :attr:`_running` so a later :meth:`start` may re-arm a fresh daemon.
        """
        self._running = False
        self._stop.set()

    def __enter__(self) -> RatePanelSampler:
        """Start the background sampler and return self (context-manager entry)."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop the background sampler (context-manager exit; never suppresses)."""
        self.stop()


__all__ = [
    "DEFAULT_SAMPLE_INTERVAL_S",
    "RatePanelSampler",
    "SamplerSpawn",
]
