"""Per-worker heartbeat + live pull-progress signal (DEC-PULL-WORKER-HEARTBEAT-001).

The historical pull fans a worklist (``(instrument, interval)`` pairs / per-contract
chains) across a bounded thread pool (:class:`~brutex.orchestrator.parallel.PoolRunParallel`).
The existing :class:`~brutex.observability.pull_health.PullHealthMonitor` watches the
AGGREGATE rate (achieved vs allowed r/s, latency, 429 storms) — but it cannot see an
INDIVIDUAL worker that has silently wedged (a hung socket, a stuck merge-read lock,
a non-progressing retry loop). A single wedged worker can leave the achieved rate
looking *plausible* while one symbol never finishes, and nothing today surfaces it.
This is the top open row in the pull failure-detection matrix: **per-worker liveness +
a live "X of Y done" progress signal**.

This module closes that gap WITHOUT a new background thread, WITHOUT changing the
fan-out, and WITHOUT being able to slow or crash the pull:

* :class:`PullHeartbeat` is a tiny, lock-protected registry. Each worker calls
  :meth:`beat` once per completed unit (the natural cadence is the spot pull's
  per-window ``on_window`` tick / the option pull's per-contract progress) to stamp a
  monotonic timestamp + bump that worker's completed-unit counter. The write is O(1)
  (a dict insert under a short lock — see "Honest complexity" below).
* :meth:`scan` (called from the SAME observe cadence the rate monitor already uses,
  e.g. ``_observe_rate``) walks the registry and flags any worker whose last beat is
  older than an operator-tunable ``stale_after_s`` threshold → a structlog WARNING +
  a Prometheus gauge of the stuck count + a best-effort ``on_stuck`` callback (which
  production wires to the existing ``SweepAlerter`` path). A worker fires AT MOST once
  per continuous stuck spell (edge-triggered, like the SLA detector's hysteresis), so
  a sustained stall coalesces into one alert, not one per scan.
* :meth:`progress` exposes the aggregate live snapshot — ``{units_done, units_total,
  workers_active, workers_stuck, oldest_beat_age_s}`` — mirrored into the metric
  registry and available for a structlog progress line / the dashboard.

## Honest complexity (Golden Rule #10)

* :meth:`beat` is O(1): one dict write + a counter bump under a short
  :class:`threading.Lock` (NEVER held across I/O). Under N concurrent workers the lock
  serialises the dict write only — microseconds — so it cannot meaningfully slow a pull
  whose unit cadence is hundreds of milliseconds per window.
* :meth:`scan` and :meth:`progress` are O(W) in the number of REGISTERED workers (one
  pass over the registry), NOT O(1). W is the pool width (≤ 32, :data:`MAX_FANOUT`),
  so this is a tiny bounded scan, but it is NOT claimed O(1) — it runs at the slow
  observe cadence (seconds), never per unit.
* The whole thing is **best-effort**: :meth:`beat`, :meth:`scan` and :meth:`progress`
  are each fully guarded so a malformed callback / a metric error can NEVER raise into
  the pull. A heartbeat failure degrades to "no liveness signal", never to a slower or
  crashed pull. This is DETECT-and-WARN observability, NOT auto-remediation: a flagged
  worker is reported, never killed or restarted.

## Decision: DEC-PULL-WORKER-HEARTBEAT-001 (MIGRATION.md entry #936).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from brutex.observability.metrics import MetricRegistry

logger = logging.getLogger(__name__)

#: A no-argument per-window progress callback — the spot pull's ``on_window`` tick
#: (kept loosely typed here to avoid an import cycle with the adapters package; it
#: structurally matches :data:`brutex.adapters.groww.ingest.WindowTick`).
WindowTick = Callable[[], None]

# ────────────────────────────────────────────────────────────────────────
# Default thresholds (operator-tunable heuristics — NOT vendor-verified)
# ────────────────────────────────────────────────────────────────────────

#: A worker that has not beaten within this many seconds is FLAGGED stuck. A pull's
#: unit cadence is per-window (seconds to low-tens-of-seconds under a 429 storm at the
#: floor rate), so a default 120 s is well above the worst legitimate per-unit latency
#: yet catches a genuine wedge quickly. Operator-tunable per pull.
DEFAULT_STALE_AFTER_S: Final[float] = 120.0

#: Prometheus gauges mirrored from the registry on each :meth:`scan` / :meth:`progress`.
_GAUGE_UNITS_DONE: Final[str] = "brutex_pull_units_done"
_GAUGE_UNITS_TOTAL: Final[str] = "brutex_pull_units_total"
_GAUGE_WORKERS_ACTIVE: Final[str] = "brutex_pull_workers_active"
_GAUGE_WORKERS_STUCK: Final[str] = "brutex_pull_workers_stuck"
_GAUGE_OLDEST_BEAT_AGE_S: Final[str] = "brutex_pull_oldest_beat_age_seconds"


# ────────────────────────────────────────────────────────────────────────
# Snapshots
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkerBeat:
    """One worker's last-progress record (immutable view returned by :meth:`scan`)."""

    worker_id: str
    #: Monotonic instant of the worker's last :meth:`PullHeartbeat.beat`.
    last_beat: float
    #: Completed units this worker has stamped.
    units: int
    #: Seconds since the last beat, evaluated at the scan instant (clamped ``>= 0``).
    age_s: float
    #: ``True`` iff ``age_s`` exceeded the registry's ``stale_after_s`` at the scan.
    stuck: bool


@dataclass(frozen=True, slots=True)
class PullProgress:
    """The aggregate live pull-progress snapshot (returned by :meth:`PullHeartbeat.progress`)."""

    #: Total units completed across all workers (sum of per-worker counters).
    units_done: int
    #: Planned total units, or ``None`` when the caller did not declare one.
    units_total: int | None
    #: Number of registered workers that have beaten at least once.
    workers_active: int
    #: Number of registered workers currently flagged stuck (``age_s > stale_after_s``).
    workers_stuck: int
    #: Age (seconds) of the OLDEST worker's last beat, or ``None`` when no worker has beaten.
    oldest_beat_age_s: float | None


# ────────────────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _WorkerSlot:
    """Mutable per-worker state guarded by :attr:`PullHeartbeat._lock`."""

    last_beat: float
    units: int = 0
    #: Edge-trigger latch — ``True`` while the worker is in a flagged-stuck spell, so a
    #: sustained stall fires :meth:`PullHeartbeat.scan`'s ``on_stuck`` at most ONCE.
    stuck_latched: bool = field(default=False)


class PullHeartbeat:
    """Lock-protected per-worker heartbeat registry + live pull-progress signal.

    Built ONCE per pull and shared across every fan-out worker. A worker calls
    :meth:`beat` once per completed unit; the pull's observe cadence calls :meth:`scan`
    (to flag wedged workers) and :meth:`progress` (to surface the aggregate). All three
    are best-effort and never raise into the pull.

    Parameters
    ----------
    registry
        The metric registry to mirror the progress gauges into (the process singleton
        in production).
    units_total
        Optional planned unit total for the ``X of Y`` progress signal. ``None`` (the
        default) leaves the total undeclared (``units_total`` reads ``None``).
    on_stuck
        Best-effort edge callback ``(worker_id, age_s, units) -> None`` fired ONCE when a
        worker first crosses ``stale_after_s`` (production wires it to the
        :class:`~brutex.notifications.sweep_alerts.SweepAlerter` path). ``None`` (the
        default) logs only.
    stale_after_s
        Seconds without a beat before a worker is flagged stuck (default
        :data:`DEFAULT_STALE_AFTER_S`). ``<= 0`` flags every never-fresh worker on the
        first scan (eager mode), never raises.
    clock
        Injected monotonic clock for deterministic testing (default
        :func:`time.monotonic`).
    """

    __slots__ = (
        "_clock",
        "_lock",
        "_on_stuck",
        "_registry",
        "_stale_after_s",
        "_units_total",
        "_workers",
    )

    def __init__(
        self,
        *,
        registry: MetricRegistry,
        units_total: int | None = None,
        on_stuck: Callable[[str, float, int], None] | None = None,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._units_total = units_total
        self._on_stuck = on_stuck
        self._stale_after_s = stale_after_s
        self._clock = clock
        self._lock = threading.Lock()
        self._workers: dict[str, _WorkerSlot] = {}
        self._register_gauges()

    # ── Setup ────────────────────────────────────────────────────────

    def _register_gauges(self) -> None:
        """Declare the mirrored Prometheus gauges (idempotent re-registration)."""
        self._registry.gauge(_GAUGE_UNITS_DONE, "Pull units completed across workers")
        self._registry.gauge(_GAUGE_UNITS_TOTAL, "Pull planned total units (0 when undeclared)")
        self._registry.gauge(_GAUGE_WORKERS_ACTIVE, "Pull workers that have beaten at least once")
        self._registry.gauge(_GAUGE_WORKERS_STUCK, "Pull workers flagged stuck (stale heartbeat)")
        self._registry.gauge(_GAUGE_OLDEST_BEAT_AGE_S, "Pull oldest worker last-beat age (seconds)")

    # ── Heartbeat ────────────────────────────────────────────────────

    def beat(self, worker_id: str, *, units: int = 1) -> None:
        """Stamp *worker_id*'s last-progress instant + add *units* to its counter — O(1).

        Called once per completed unit on the WORKER thread (the spot pull's per-window
        ``on_window`` tick / the option pull's per-contract progress). Best-effort: the
        whole body is guarded so a heartbeat write can NEVER raise into the pull worker.
        Re-beating a worker that was flagged stuck clears its latch, so a recovered
        worker can flag again on a fresh stall (and a recovery is observable).

        Parameters
        ----------
        worker_id
            A stable identifier for the worker / work-unit (e.g. the symbol or
            ``symbol interval`` label). Distinct ids are tracked independently.
        units
            Units this beat completed (default ``1``). A non-positive value is treated
            as ``0`` (a liveness-only beat — the timestamp advances, the counter does not
            go backwards), so the aggregate ``units_done`` is monotonic.
        """
        try:
            now = self._clock()
            add = units if units > 0 else 0
            with self._lock:
                slot = self._workers.get(worker_id)
                if slot is None:
                    self._workers[worker_id] = _WorkerSlot(last_beat=now, units=add)
                else:
                    slot.last_beat = now
                    slot.units += add
                    slot.stuck_latched = False
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash a worker
            logger.exception("pull_heartbeat.beat failed for %r; the pull is unaffected", worker_id)

    # ── Watch ────────────────────────────────────────────────────────

    def scan(self) -> tuple[WorkerBeat, ...]:
        """Flag workers whose last beat aged past ``stale_after_s`` — best-effort, O(W).

        Walks the registry once (O(W) in the registered-worker count W, bounded by the
        pool width), building an immutable :class:`WorkerBeat` per worker, mirroring the
        progress gauges, and firing ``on_stuck`` ONCE per continuous stuck spell
        (edge-triggered via the per-worker latch — a sustained stall coalesces into one
        alert). A newly-stuck worker also logs a structlog WARNING. Returns the per-worker
        views (newest-first by age) for the caller / tests; returns ``()`` on any internal
        error (never raises into the pull).
        """
        try:
            now = self._clock()
            newly_stuck: list[tuple[str, float, int]] = []
            beats: list[WorkerBeat] = []
            with self._lock:
                for worker_id, slot in self._workers.items():
                    age = max(0.0, now - slot.last_beat)
                    is_stuck = age > self._stale_after_s
                    if is_stuck and not slot.stuck_latched:
                        slot.stuck_latched = True
                        newly_stuck.append((worker_id, age, slot.units))
                    elif not is_stuck:
                        slot.stuck_latched = False
                    beats.append(
                        WorkerBeat(
                            worker_id=worker_id,
                            last_beat=slot.last_beat,
                            units=slot.units,
                            age_s=age,
                            stuck=is_stuck,
                        )
                    )
            self._mirror_gauges(now)
            for worker_id, age, units in newly_stuck:
                self._fire_stuck(worker_id, age, units)
            beats.sort(key=lambda b: b.age_s, reverse=True)
            return tuple(beats)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
            logger.exception("pull_heartbeat.scan failed; the pull is unaffected")
            return ()

    def _fire_stuck(self, worker_id: str, age_s: float, units: int) -> None:
        """Route a newly-stuck worker to structlog + the best-effort ``on_stuck`` callback."""
        logger.warning(
            "pull_heartbeat.worker_stuck worker=%s age_s=%.1f units=%d "
            "(no progress past the stale threshold; the pull continues, this worker is wedged)",
            worker_id,
            age_s,
            units,
        )
        if self._on_stuck is None:
            return
        try:
            self._on_stuck(worker_id, age_s, units)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort alert
            logger.exception(
                "pull_heartbeat: on_stuck callback raised for worker %r; monitor continues",
                worker_id,
            )

    # ── Progress ─────────────────────────────────────────────────────

    def progress(self) -> PullProgress:
        """Return the aggregate live pull-progress snapshot — best-effort, O(W).

        Mirrors the progress gauges and returns the ``{units_done, units_total,
        workers_active, workers_stuck, oldest_beat_age_s}`` view. On any internal error
        returns a zeroed snapshot (never raises into the pull). The total comes from the
        ``units_total`` declared at construction (``None`` when undeclared).
        """
        try:
            now = self._clock()
            return self._compute_progress(now)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
            logger.exception("pull_heartbeat.progress failed; the pull is unaffected")
            return PullProgress(
                units_done=0,
                units_total=self._units_total,
                workers_active=0,
                workers_stuck=0,
                oldest_beat_age_s=None,
            )

    def _compute_progress(self, now: float) -> PullProgress:
        """Build the :class:`PullProgress` snapshot under the lock + mirror its gauges."""
        with self._lock:
            units_done = sum(slot.units for slot in self._workers.values())
            workers_active = len(self._workers)
            ages = [max(0.0, now - slot.last_beat) for slot in self._workers.values()]
            workers_stuck = sum(1 for age in ages if age > self._stale_after_s)
            oldest = max(ages) if ages else None
        self._mirror_gauges(now)
        return PullProgress(
            units_done=units_done,
            units_total=self._units_total,
            workers_active=workers_active,
            workers_stuck=workers_stuck,
            oldest_beat_age_s=oldest,
        )

    # ── Gauge mirroring ──────────────────────────────────────────────

    def _mirror_gauges(self, now: float) -> None:
        """Mirror the current aggregate into the Prometheus gauges (best-effort, no raise).

        Computes the aggregate under the lock (NOT calling :meth:`_compute_progress` to
        avoid a re-entrant mirror), then sets the gauges OUTSIDE the lock so the metric
        registry's own lock never nests under this one.
        """
        with self._lock:
            units_done = sum(slot.units for slot in self._workers.values())
            workers_active = len(self._workers)
            ages = [max(0.0, now - slot.last_beat) for slot in self._workers.values()]
            workers_stuck = sum(1 for age in ages if age > self._stale_after_s)
            oldest = max(ages) if ages else 0.0
        self._registry.set(_GAUGE_UNITS_DONE, units_done)
        self._registry.set(_GAUGE_UNITS_TOTAL, float(self._units_total or 0))
        self._registry.set(_GAUGE_WORKERS_ACTIVE, workers_active)
        self._registry.set(_GAUGE_WORKERS_STUCK, workers_stuck)
        self._registry.set(_GAUGE_OLDEST_BEAT_AGE_S, oldest)


# ────────────────────────────────────────────────────────────────────────
# Window-tick beat wrapper (the COMMON beat seam for the spot pull fan-out)
# ────────────────────────────────────────────────────────────────────────


def make_heartbeat_window_tick(
    heartbeat: PullHeartbeat | None,
    inner: WindowTick,
    *,
    worker_id: Callable[[], str] = lambda: threading.current_thread().name,
) -> WindowTick:
    """Wrap a per-window ``inner`` tick so each fire ALSO stamps a worker heartbeat.

    The spot pull fires :data:`WindowTick` once per PROCESSED window on the WORKER
    thread (:func:`brutex.adapters.groww.ingest.ingest_spot`'s ``on_window`` hook),
    which is exactly the per-worker progress cadence the heartbeat wants. This returns a
    drop-in replacement that calls *heartbeat*'s :meth:`PullHeartbeat.beat` (keyed by
    *worker_id*, default the current thread name — the fan-out's natural per-worker id)
    BEFORE delegating to *inner*, so wiring the heartbeat into a pull is a one-line
    wrap of the existing ``on_window`` callback with NO change to the ingest signature.

    A ``None`` *heartbeat* returns *inner* unchanged (the seam is a no-op when the
    heartbeat is not wired — the dry-run / CLI / test paths pay nothing). The beat is
    best-effort inside :meth:`PullHeartbeat.beat`, so the wrap can never make *inner*
    raise where it would not have.

    Parameters
    ----------
    heartbeat
        The shared registry to stamp, or ``None`` to disable (returns *inner* as-is).
    inner
        The underlying per-window callback (e.g. the throttled ledger ``live_sync``).
    worker_id
        A zero-arg id provider (default the current thread name). Injectable for tests.
    """
    if heartbeat is None:
        return inner

    def _tick() -> None:
        heartbeat.beat(worker_id())
        inner()

    return _tick


__all__ = [
    "DEFAULT_STALE_AFTER_S",
    "PullHeartbeat",
    "PullProgress",
    "WindowTick",
    "WorkerBeat",
    "make_heartbeat_window_tick",
]
