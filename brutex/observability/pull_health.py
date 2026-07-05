"""Live pull-health self-instrumentation (DEC-PULL-HEALTH-MONITOR-001).

The Groww historical pull already exposes two rich live snapshots — the adaptive
:class:`~brutex.adapters.rate_common.RateController` (``current_rps`` / ``recent_hits``
/ ``recent_minute``) and the transport :class:`~brutex.adapters.call_meter.CallMeter`
(``effective_rps`` / ``latency_ms_p95`` / ``rps_1s`` / ``in_flight``) — surfaced on the
dashboard rate panel via :func:`brutex.dashboard.pull_controller._observe_rate`. But the
operator only ever SAW those numbers; nothing WATCHED them. A pull that quietly achieves
3.2 r/s against a 5 r/s ceiling (the DEC-FETCH-WRITE-OVERLAP-001 slack), or that stalls
under a 429 storm, produced no alert — the operator learned of it only by reading the
chart.

This module closes that gap WITHOUT adding a background thread or a new polling cadence.
:class:`PullHealthMonitor` reuses the EXISTING rails:

* :class:`~brutex.observability.sla.SLADetector` — the hysteresis-aware rule engine
  (an OK↔BREACH edge fires once, a steady state is silent). NO change is made to it.
* :class:`~brutex.observability.metrics.MetricRegistry` — every observed gauge is
  mirrored as a Prometheus gauge so ``/metrics`` carries the live pull health.
* :class:`~brutex.notifications.sweep_alerts.SweepAlerter` — a breach routes to ONE
  best-effort Telegram ``pull_degraded`` alert (deduped per rule).

## The rules (value-provider closures over the last snapshot)

Every rule reads a closure over the monitor's LAST-observed snapshot pair, so the
detector stays decoupled from the snapshot source (the sla.py contract):

* ``pull.achieved_below_allowed`` — ``effective_rps / current_rps < RATIO`` (default
  ``0.6``) for K consecutive samples (default ``5``): our pipeline is leaving the
  vendor budget on the table (a write-latency / pool-width problem, NOT the vendor)
  — WARN, dwell-gated (DEC-PULL-HEALTH-WARN-DWELL-001).
* ``pull.pool_idle_high`` — ``1 − in_flight / pool_size > FRACTION`` (default ``0.5``)
  for K consecutive samples (default ``5``): more than half the connection pool sits
  idle — WARN, dwell-gated (DEC-PULL-HEALTH-WARN-DWELL-001).
* ``pull.write_latency_spike`` — ``latency_ms_p95 > BUDGET_MS`` (default ``1500``):
  the per-call p95 latency blew its budget — WARN.
* ``pull.rate_storm`` — ``recent_hits > N`` (default ``20``): a vendor-429 burst —
  EXPECTED, the pull continues, so VENDOR-gated WARN (not our code).
* ``pull.stalled`` — ``effective_rps < FLOOR`` (default ``0.5``) for K consecutive
  samples (default ``5``) — the pull is effectively dead — CRITICAL.

``pull.stalled`` AND the two cold-start-prone WARN rules
(``pull.achieved_below_allowed`` + ``pull.pool_idle_high``) use a
:class:`DwellProvider`: a value-provider wrapper that returns the observed value ONLY
after its predicate has held for K consecutive samples, and returns ``None`` otherwise.
``None`` is exactly the "value not available yet" signal :class:`SLADetector` already
skips — so the dwell requirement is layered on TOP of the detector with ZERO change to
it (the sla.py None-skip contract does the work). The two WARN rules are dwell-gated
(:data:`WARN_DWELL_SAMPLES`, DEC-PULL-HEALTH-WARN-DWELL-001) because a ~1s-old pool
breaches BOTH on its first observe tick — the :class:`~brutex.adapters.call_meter.\
CallMeter` ``effective_rps`` divides by a FIXED 10 s window (so it ramps from 0 over
the first seconds — fully deterministic) and the connection pool has not yet fanned out
(``in_flight`` 0–1 → idle ≥ 0.75 — near-deterministic, strictly needs ``in_flight <
2``). Those are startup artifacts, not degradations; without the dwell, every real pull
opened with two scary Telegram WARNs. The ``/metrics`` gauges are NOT dwelled — they
mirror the raw instantaneous values from tick 1; only the ALERT edge is dwell-gated.

## Honest envelope (Golden Rule #10)

:meth:`PullHealthMonitor.observe` is **best-effort, never raises**: the whole body is
wrapped so a malformed snapshot / a buggy callback can NEVER block, slow, or crash the
pull. The thresholds are operator-tunable defaults, NOT vendor-verified limits — they
are heuristics for "something looks wrong", and a breach is an OBSERVATION, never an
auto-remediation. ``pull.rate_storm`` is labelled VENDOR-gated precisely because a 429
burst is expected behaviour the adaptive controller already handles; the alert says so
explicitly so the operator does not chase our code for a vendor signal.

## Decision: DEC-PULL-HEALTH-MONITOR-001 (MIGRATION.md entry #932).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from brutex.observability.metrics import MetricRegistry
from brutex.observability.sla import (
    SLADetector,
    SLAEvent,
    SLAPredicate,
    SLARule,
    SLASeverity,
    SLAState,
)

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Default thresholds (operator-tunable heuristics — NOT vendor-verified)
# ────────────────────────────────────────────────────────────────────────

#: ``effective_rps / current_rps`` below this → OUR pipeline is under-using the
#: vendor budget (DEC-FETCH-WRITE-OVERLAP-001 slack). Heuristic, not a vendor limit.
ACHIEVED_BELOW_ALLOWED_RATIO: Final[float] = 0.6

#: Idle fraction (``1 − in_flight / pool_size``) above this → the pool is mostly idle.
POOL_IDLE_FRACTION: Final[float] = 0.5

#: Per-call p95 latency budget (ms); above it the lake write / network is spiking.
WRITE_LATENCY_BUDGET_MS: Final[float] = 1500.0

#: ``recent_hits`` (recent vendor 429s) above this → a rate storm (VENDOR-gated).
RATE_STORM_HITS: Final[int] = 20

#: ``effective_rps`` below this for :data:`STALLED_DWELL_SAMPLES` consecutive samples
#: → the pull is effectively stalled (CRITICAL).
STALLED_RPS_FLOOR: Final[float] = 0.5

#: Consecutive samples ``pull.stalled`` must hold before it fires (dwell).
STALLED_DWELL_SAMPLES: Final[int] = 5

#: Consecutive breaching samples the two cold-start-prone WARN rules
#: (``pull.achieved_below_allowed`` + ``pull.pool_idle_high``) must dwell before firing
#: (DEC-PULL-HEALTH-WARN-DWELL-001). A ~1s-old pool breaches BOTH on its FIRST tick —
#: the CallMeter ``effective_rps`` fixed-10s-divisor ramp makes the achieved ratio
#: deterministically < 0.6 for the first seconds, and the not-yet-fanned-out pool reads
#: near-deterministically idle — a startup artifact, not a degradation.
WARN_DWELL_SAMPLES: Final[int] = 5

#: Prometheus metric names mirrored from each observed snapshot pair.
_GAUGE_ACHIEVED_RPS: Final[str] = "brutex_pull_achieved_rps"
_GAUGE_ALLOWED_RPS: Final[str] = "brutex_pull_allowed_rps"
_GAUGE_IN_FLIGHT: Final[str] = "brutex_pull_in_flight"
_GAUGE_POOL_IDLE_FRACTION: Final[str] = "brutex_pull_pool_idle_fraction"
_GAUGE_LATENCY_P95_MS: Final[str] = "brutex_pull_latency_ms_p95"
_GAUGE_RECENT_HITS: Final[str] = "brutex_pull_recent_hits"


# ────────────────────────────────────────────────────────────────────────
# Dwell-gated value provider
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class DwellProvider:
    """Wrap a value-provider so it fires only after the predicate dwells K samples.

    A :class:`~brutex.observability.sla.SLARule` value-provider that returns the wrapped
    ``value_provider`` reading ONLY once :meth:`predicate` has held ``True`` for
    ``samples`` consecutive calls. Because
    :class:`~brutex.observability.sla.SLADetector` skips a rule whose provider returns
    ``None`` (the documented "value not available yet" contract), the dwell requirement
    is layered on top of the detector WITHOUT modifying it — a transient single-sample
    dip never trips a CRITICAL alert, only a sustained one does.

    The dwell gate is ASYMMETRIC by design (DEC-PULL-STALLED-REFIRE-001):

    * While the predicate holds but the streak is still building (``< samples``),
      the provider returns ``None`` — no premature breach.
    * Once the streak reaches ``samples``, the (breaching) value is surfaced —
      the detector fires the OK→BREACH edge.
    * On the FIRST healthy sample (predicate ``False`` with a real value), the
      provider surfaces the REAL value — the detector sees a non-breaching
      reading and fires the BREACH→OK recovery edge immediately (recovery has
      no dwell; paging fast on recovery is harmless, paging never on a
      re-stall is not).
    * ``None`` is returned ONLY when the underlying value itself is
      unavailable — preserving the detector's None-skip contract exactly.

    Before DEC-PULL-STALLED-REFIRE-001 the recovery branch ALSO returned
    ``None``, so the detector's state stuck at BREACH forever: recovery was
    never logged and a second genuine stall produced no OK→BREACH edge — the
    CRITICAL ``pull.stalled`` alert fired AT MOST ONCE per process.

    Parameters
    ----------
    value_provider
        The underlying observed-value reader (a closure over the monitor's last
        snapshot). Returns the float to surface once the dwell is satisfied, or ``None``
        when the value itself is unavailable.
    predicate
        Maps the observed value to ``True`` when it counts toward the dwell streak. The
        streak resets to zero on the first ``False`` (or ``None``) sample.
    samples
        Consecutive ``True`` samples required before the value is surfaced (must be
        ``>= 1``).

    Notes
    -----
    Stateful — one :class:`DwellProvider` instance tracks one rule's streak. The
    instance is NOT itself thread-safe: under the live pull, up to eight parallel pool
    workers each fire a per-window :meth:`PullHealthMonitor.observe` tick (via
    :func:`brutex.dashboard.pull_controller._observe_rate`), and an unsynchronised
    ``_streak`` increment racing with the OK↔BREACH transition would interleave. The
    serialisation is provided by :class:`PullHealthMonitor`, which holds a single
    :class:`threading.Lock` around the WHOLE :meth:`PullHealthMonitor.observe` body
    (snapshot assignment + gauge mirror + ``check_once``), so every provider — this one
    included — is only ever invoked under that lock. The dwell streak therefore advances
    by exactly one per logical tick with no torn read (DEC-PULL-HEALTH-CONCURRENCY-001).
    """

    value_provider: Callable[[], float | None]
    predicate: Callable[[float], bool]
    samples: int
    _streak: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.samples < 1:
            raise ValueError(f"DwellProvider.samples must be >= 1, got {self.samples}")

    def __call__(self) -> float | None:
        """Return the dwelt breaching value, the real value on recovery, or ``None``.

        ``None`` means ONLY "value genuinely unavailable this tick" (or the
        breach streak is still building) — a healthy reading is surfaced so
        the detector's BREACH→OK recovery edge can fire
        (DEC-PULL-STALLED-REFIRE-001).
        """
        value = self.value_provider()
        if value is None:
            self._streak = 0
            return None
        if not self.predicate(value):
            self._streak = 0
            return value
        self._streak += 1
        if self._streak < self.samples:
            return None
        return value


# ────────────────────────────────────────────────────────────────────────
# Monitor
# ────────────────────────────────────────────────────────────────────────


class PullHealthMonitor:
    """Watch the live pull's rate + throughput snapshots and route breaches to alerts.

    Built ONCE per pull (the dashboard / CLI live path) and fed each rate-observer tick
    via :meth:`observe`. Holds an :class:`~brutex.observability.sla.SLADetector` whose
    rules read closures over the last-observed snapshot pair, mirrors every gauge into a
    :class:`~brutex.observability.metrics.MetricRegistry`, and routes a breach / recovery
    to a ``pull_degraded`` callback (the :class:`~brutex.notifications.sweep_alerts.\
SweepAlerter` Telegram alert) + structlog.

    Parameters
    ----------
    registry
        The metric registry to mirror gauges into (the process singleton in production).
    on_degraded
        Best-effort breach callback ``(rule, achieved, allowed, detail) -> None`` —
        production wires :meth:`brutex.notifications.sweep_alerts.SweepAlerter.pull_degraded`.
        A breach edge fires it once (hysteresis); a recovery edge logs only.
    pool_size
        The connection-pool width used by ``pull.pool_idle_high`` (NOT present in either
        snapshot). ``<= 0`` disables that single rule's reading (its provider returns
        ``None``), never raising.
    achieved_ratio, pool_idle_fraction, latency_budget_ms, rate_storm_hits,
    stalled_floor, stalled_dwell
        Threshold overrides (default to the module constants).
    warn_dwell
        Consecutive breaching samples ``pull.achieved_below_allowed`` and
        ``pull.pool_idle_high`` must dwell before firing (default
        :data:`WARN_DWELL_SAMPLES`). A ~1s-old pool breaches both on its first
        observe tick (the CallMeter fixed-divisor ``effective_rps`` ramp + a
        not-yet-fanned-out pool), so an undwelled first tick paged the operator at
        every pull start (DEC-PULL-HEALTH-WARN-DWELL-001). Recovery has no dwell.
    """

    __slots__ = (
        "_detector",
        "_last_controller",
        "_last_meter",
        "_observe_lock",
        "_on_degraded",
        "_pool_size",
        "_registry",
    )

    def __init__(
        self,
        *,
        registry: MetricRegistry,
        on_degraded: Callable[[str, float, float, str], None],
        pool_size: int,
        achieved_ratio: float = ACHIEVED_BELOW_ALLOWED_RATIO,
        pool_idle_fraction: float = POOL_IDLE_FRACTION,
        latency_budget_ms: float = WRITE_LATENCY_BUDGET_MS,
        rate_storm_hits: int = RATE_STORM_HITS,
        stalled_floor: float = STALLED_RPS_FLOOR,
        stalled_dwell: int = STALLED_DWELL_SAMPLES,
        warn_dwell: int = WARN_DWELL_SAMPLES,
    ) -> None:
        self._registry = registry
        self._on_degraded = on_degraded
        self._pool_size = pool_size
        self._last_controller: Mapping[str, object] = {}
        self._last_meter: Mapping[str, object] = {}
        # One lock serialises the WHOLE observe() body so each per-window tick is atomic
        # across the up-to-8 parallel pool workers that call observe() concurrently
        # (DEC-PULL-HEALTH-CONCURRENCY-001). Without it the snapshot assignment, the
        # DwellProvider streak increment, and the SLADetector OK↔BREACH transition could
        # interleave (a torn read / a double-counted dwell). The on_degraded Telegram
        # alert is async / best-effort, so no network I/O is ever held under this lock.
        self._observe_lock = threading.Lock()
        self._register_gauges()
        self._detector = SLADetector(
            rules=self._build_rules(
                achieved_ratio=achieved_ratio,
                pool_idle_fraction=pool_idle_fraction,
                latency_budget_ms=latency_budget_ms,
                rate_storm_hits=rate_storm_hits,
                stalled_floor=stalled_floor,
                stalled_dwell=stalled_dwell,
                warn_dwell=warn_dwell,
            ),
            on_breach=self._on_breach,
            on_recovery=self._on_recovery,
        )

    # ── Setup ────────────────────────────────────────────────────────

    def _register_gauges(self) -> None:
        """Declare the mirrored Prometheus gauges (idempotent re-registration)."""
        self._registry.gauge(_GAUGE_ACHIEVED_RPS, "Pull achieved (effective) requests/sec")
        self._registry.gauge(_GAUGE_ALLOWED_RPS, "Pull controller-allowed requests/sec")
        self._registry.gauge(_GAUGE_IN_FLIGHT, "Pull in-flight concurrent calls")
        self._registry.gauge(_GAUGE_POOL_IDLE_FRACTION, "Pull connection-pool idle fraction")
        self._registry.gauge(_GAUGE_LATENCY_P95_MS, "Pull per-call p95 latency (ms)")
        self._registry.gauge(_GAUGE_RECENT_HITS, "Pull recent vendor 429 hits")

    def _build_rules(
        self,
        *,
        achieved_ratio: float,
        pool_idle_fraction: float,
        latency_budget_ms: float,
        rate_storm_hits: int,
        stalled_floor: float,
        stalled_dwell: int,
        warn_dwell: int,
    ) -> tuple[SLARule, ...]:
        """Construct the five pull-health rules over the last-snapshot closures.

        The two cold-start-prone WARN rules are wrapped in a :class:`DwellProvider`
        (``samples=warn_dwell``) exactly like ``pull.stalled`` — a ~1s-old pool
        breaches both on its first tick, so the alert edge must dwell while the raw
        instantaneous values stay live on the ``/metrics`` gauges
        (DEC-PULL-HEALTH-WARN-DWELL-001).
        """
        return (
            SLARule(
                name="pull.achieved_below_allowed",
                value_provider=DwellProvider(
                    value_provider=self._achieved_ratio_provider,
                    predicate=lambda ratio: ratio < achieved_ratio,
                    samples=warn_dwell,
                ),
                predicate=SLAPredicate.LESS_THAN,
                threshold=achieved_ratio,
                severity=SLASeverity.WARN,
                description="Achieved r/s is far below the allowed ceiling",
            ),
            SLARule(
                name="pull.pool_idle_high",
                value_provider=DwellProvider(
                    value_provider=self._pool_idle_provider,
                    predicate=lambda idle: idle > pool_idle_fraction,
                    samples=warn_dwell,
                ),
                predicate=SLAPredicate.GREATER_THAN,
                threshold=pool_idle_fraction,
                severity=SLASeverity.WARN,
                description="Most of the connection pool is idle",
            ),
            SLARule(
                name="pull.write_latency_spike",
                value_provider=self._latency_provider,
                predicate=SLAPredicate.GREATER_THAN,
                threshold=latency_budget_ms,
                severity=SLASeverity.WARN,
                description="Per-call p95 latency exceeded its budget",
            ),
            SLARule(
                name="pull.rate_storm",
                value_provider=self._recent_hits_provider,
                predicate=SLAPredicate.GREATER_THAN,
                threshold=float(rate_storm_hits),
                severity=SLASeverity.WARN,
                description="Vendor 429 storm (expected — the pull continues)",
            ),
            SLARule(
                name="pull.stalled",
                value_provider=DwellProvider(
                    value_provider=self._achieved_rps_provider,
                    predicate=lambda rps: rps < stalled_floor,
                    samples=stalled_dwell,
                ),
                predicate=SLAPredicate.LESS_THAN,
                threshold=stalled_floor,
                severity=SLASeverity.CRITICAL,
                description="Pull effectively stalled (sustained near-zero throughput)",
            ),
        )

    # ── Value-provider closures over the last snapshot ───────────────

    @staticmethod
    def _coerce(snapshot: Mapping[str, object], key: str) -> float | None:
        """Return ``snapshot[key]`` as a float, or ``None`` when absent / non-numeric."""
        if key not in snapshot:
            return None
        try:
            return float(snapshot[key])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _achieved_rps_provider(self) -> float | None:
        """The meter's ``effective_rps`` (or ``None`` until the first snapshot)."""
        return self._coerce(self._last_meter, "effective_rps")

    def _achieved_ratio_provider(self) -> float | None:
        """``effective_rps / current_rps`` — the achieved-vs-allowed fraction.

        Returns ``None`` when either gauge is missing or the allowed rate is not
        positive (a zero ceiling makes the ratio meaningless — the detector skips it
        rather than dividing by zero).
        """
        achieved = self._coerce(self._last_meter, "effective_rps")
        allowed = self._coerce(self._last_controller, "current_rps")
        if achieved is None or allowed is None or allowed <= 0.0:
            return None
        return achieved / allowed

    def _pool_idle_provider(self) -> float | None:
        """``1 − in_flight / pool_size`` — the idle fraction of the connection pool.

        Returns ``None`` when ``pool_size <= 0`` (rule disabled) or ``in_flight`` is
        unavailable. The fraction is clamped to ``[0, 1]`` so a transient
        ``in_flight > pool_size`` race never reads as a negative idle.
        """
        if self._pool_size <= 0:
            return None
        in_flight = self._coerce(self._last_meter, "in_flight")
        if in_flight is None:
            return None
        idle = 1.0 - (in_flight / self._pool_size)
        return min(1.0, max(0.0, idle))

    def _latency_provider(self) -> float | None:
        """The meter's ``latency_ms_p95`` (or ``None`` until the first snapshot)."""
        return self._coerce(self._last_meter, "latency_ms_p95")

    def _recent_hits_provider(self) -> float | None:
        """The controller's ``recent_hits`` (recent vendor 429s)."""
        return self._coerce(self._last_controller, "recent_hits")

    # ── Breach / recovery routing ────────────────────────────────────

    def _detail_for(self, event: SLAEvent) -> str:
        """Build the OUR-code-vs-VENDOR distinction string for *event*."""
        if event.rule_name == "pull.rate_storm":
            return (
                "VENDOR 429-gated — expected; the adaptive controller already "
                "throttled and the pull continues"
            )
        return "OUR pipeline is the limiter — check write latency / pool width"

    def _on_breach(self, event: SLAEvent) -> None:
        """Route a rule OK→BREACH edge to the degraded callback + structlog."""
        detail = self._detail_for(event)
        logger.warning(
            "pull_health.breach rule=%s severity=%s observed=%.4f threshold=%.4f detail=%s",
            event.rule_name,
            event.severity.value,
            event.observed_value,
            event.threshold,
            detail,
        )
        achieved = self._coerce(self._last_meter, "effective_rps") or 0.0
        allowed = self._coerce(self._last_controller, "current_rps") or 0.0
        try:
            self._on_degraded(event.rule_name, achieved, allowed, detail)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort alert
            logger.exception(
                "pull_health: on_degraded callback raised for rule %s; monitor continues",
                event.rule_name,
            )

    def _on_recovery(self, event: SLAEvent) -> None:
        """Log a rule BREACH→OK edge (recovery is informational, no alert)."""
        logger.info(
            "pull_health.recovery rule=%s observed=%.4f threshold=%.4f",
            event.rule_name,
            event.observed_value,
            event.threshold,
        )

    # ── The observe hook ─────────────────────────────────────────────

    def observe(
        self,
        controller_snap: Mapping[str, object],
        meter_snap: Mapping[str, object],
    ) -> None:
        """Ingest one rate-observer tick: mirror gauges, then evaluate the rules.

        Best-effort — the whole body is guarded so a malformed snapshot or a buggy
        downstream callback can NEVER block, slow, or crash the pull (Golden Rule #10).
        Called from the existing :func:`brutex.dashboard.pull_controller._observe_rate`
        seam on the pull's own cadence, so the monitor adds NO background thread.

        Thread-safety (DEC-PULL-HEALTH-CONCURRENCY-001): the up-to-8 parallel Groww pool
        workers each fire this per-window tick concurrently, so the WHOLE body — the
        last-snapshot assignment, the gauge mirror, AND ``check_once`` (which advances the
        :class:`DwellProvider` streak + flips the OK↔BREACH state) — runs under a single
        :attr:`_observe_lock`, making each tick atomic. The breach callback fires inside
        ``check_once`` under the lock, but it only ENQUEUES an async, best-effort Telegram
        alert (no synchronous network I/O), so the lock is held only for the cheap
        in-memory rule evaluation.

        Parameters
        ----------
        controller_snap
            A :meth:`brutex.adapters.rate_common.RateController.snapshot` mapping
            (``current_rps`` / ``recent_hits`` / …).
        meter_snap
            A :meth:`brutex.adapters.call_meter.CallMeter.snapshot` mapping
            (``effective_rps`` / ``latency_ms_p95`` / ``in_flight`` / …).
        """
        try:
            with self._observe_lock:
                self._last_controller = controller_snap
                self._last_meter = meter_snap
                self._mirror_gauges()
                self._detector.check_once()
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the pull
            logger.exception("pull_health.observe failed; the pull is unaffected")

    def _mirror_gauges(self) -> None:
        """Mirror the current snapshot pair into the Prometheus gauges (best-effort)."""
        achieved = self._coerce(self._last_meter, "effective_rps")
        if achieved is not None:
            self._registry.set(_GAUGE_ACHIEVED_RPS, achieved)
        allowed = self._coerce(self._last_controller, "current_rps")
        if allowed is not None:
            self._registry.set(_GAUGE_ALLOWED_RPS, allowed)
        in_flight = self._coerce(self._last_meter, "in_flight")
        if in_flight is not None:
            self._registry.set(_GAUGE_IN_FLIGHT, in_flight)
        idle = self._pool_idle_provider()
        if idle is not None:
            self._registry.set(_GAUGE_POOL_IDLE_FRACTION, idle)
        latency = self._coerce(self._last_meter, "latency_ms_p95")
        if latency is not None:
            self._registry.set(_GAUGE_LATENCY_P95_MS, latency)
        hits = self._coerce(self._last_controller, "recent_hits")
        if hits is not None:
            self._registry.set(_GAUGE_RECENT_HITS, hits)

    # ── Test-only accessor ───────────────────────────────────────────

    def state_of(self, rule_name: str) -> SLAState:
        """Return the current per-rule :class:`~brutex.observability.sla.SLAState`."""
        return self._detector.state_of(rule_name)


__all__ = [
    "ACHIEVED_BELOW_ALLOWED_RATIO",
    "POOL_IDLE_FRACTION",
    "RATE_STORM_HITS",
    "STALLED_DWELL_SAMPLES",
    "STALLED_RPS_FLOOR",
    "WARN_DWELL_SAMPLES",
    "WRITE_LATENCY_BUDGET_MS",
    "DwellProvider",
    "PullHealthMonitor",
]
