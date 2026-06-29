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
* **Achieved r/s is the controller's MEASURED rolling-60s rate**
  (``recent_minute / 60``), NOT the transport call-meter's inter-arrival ``effective_rps``
  (which lives in the F&O client pool, built inside the candle factory + not reachable
  here). It is a REAL measured achieved rate (the controller's own issue count over the
  trailing minute), so the widget's flowing / stalled verdict is honest — it is simply a
  per-minute average rather than the spot phase's per-call inter-arrival figure. The
  ``req/min`` + ``req/hour`` gauges read the same ``recent_minute`` the spot path does.
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

#: Seconds in the controller's rolling-minute window — divides ``recent_minute`` (the
#: MEASURED rolling-60s issue count) into the achieved-r/s figure the panel's flow / stall
#: verdict reads. The controller's own per-minute window is 60 s
#: (:data:`brutex.adapters.rate_common.PER_MIN_WINDOW`).
_PER_MIN_SECONDS: Final[float] = 60.0

#: Background-loop spawner type — a callable that runs *fn* off the calling thread. The
#: production default spawns a daemon thread; tests inject a capturing fake for
#: deterministic, thread-free coverage (mirrors
#: :class:`~brutex.dashboard.pull_stall.PullStallWatchdog`'s ``spawn`` seam).
SamplerSpawn = Callable[[Callable[[], None]], None]

_LOG = structlog.get_logger("brutex.dashboard.rate_sampler")


def _daemon_spawn(fn: Callable[[], None]) -> None:
    """Run *fn* on a named daemon thread (the production background-loop spawner)."""
    threading.Thread(target=fn, name="dashboard-rate-sampler", daemon=True).start()


def _int_or_zero(value: object) -> int:
    """Narrow a controller-snapshot value (typed ``object``) to ``int``, else ``0``.

    :meth:`~brutex.adapters.rate_common.RateController.snapshot` is typed
    ``dict[str, object]``, so its values arrive as ``object``. ``recent_minute`` is an
    ``int`` by contract, but a defensive narrowing keeps this ``mypy --strict``-clean and
    yields an honest ``0`` for an absent / unexpected value (never a crash, never a
    fabricated rate). ``bool`` is an ``int`` subclass but never appears for this key.
    """
    return value if isinstance(value, int) else 0


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
        """Push ONE controller snapshot to the panel — returns whether a tick fired.

        Reads the shared controller via
        :meth:`~brutex.dashboard.pull_controller.SessionRateController.peek`. When no
        controller is built yet (a dry-run / a pull that has not paced anything) it
        returns ``False`` and pushes nothing — the panel stays honestly idle. Otherwise it
        snapshots the controller, derives the achieved r/s from the MEASURED rolling-minute
        issue count (``recent_minute / 60``), and feeds the *observer* the
        ``(controller_snapshot, meter_snapshot)`` pair, returning ``True``.

        Best-effort: a snapshot / observer error is swallowed + logged (the sampler can
        never crash the pull it watches) and reported as ``False``. O(1) per call.
        """
        controller = self._session_rate.peek()
        if controller is None:
            return False
        try:
            snap = controller.snapshot()
            # The controller snapshot carries the ALLOWED rate + the MEASURED rolling-60s
            # issue count, but NOT the transport call-meter's achieved ``effective_rps``
            # (that lives in the F&O client pool, unreachable here). Derive a real measured
            # achieved r/s from the controller's own minute count so the panel's flow /
            # stall verdict is honest (a per-minute average, openly so) instead of pinned
            # to a fake 0 (which would falsely read "stalled" all through the F&O phase).
            recent_minute = _int_or_zero(snap.get("recent_minute"))
            meter_snapshot = {"effective_rps": recent_minute / _PER_MIN_SECONDS}
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
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the background sampler (context-manager exit; never suppresses)."""
        self.stop()


__all__ = [
    "DEFAULT_SAMPLE_INTERVAL_S",
    "RatePanelSampler",
    "SamplerSpawn",
]
