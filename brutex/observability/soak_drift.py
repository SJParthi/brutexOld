"""Pure, deterministic soak-drift detector — RSS ceiling / fd growth / p99 latency drift.

DEC-SOAK-REAL-TIER-001 (MIGRATION.md entry #1540). A real multi-hour soak must
answer three questions AUTOMATICALLY — never by eyeballing a chart (CLAUDE.md §11
AUTOMATED, "ZERO clicks"):

1. **RSS ceiling** — did resident memory ever cross an absolute wall? (catches a
   native-side leak ``tracemalloc`` cannot see; the fallback ``ru_maxrss`` peak
   read on macOS makes this a CEILING backstop — see
   :mod:`brutex.observability.process_probe`).
2. **fd growth** — did the open-descriptor count climb above its start? (a
   Parquet partition / DuckDB connection / flock sidecar / audit writer leaked).
3. **p99 latency drift** — did the LATE windows' p99 latency drift above the
   FIRST window's by more than a tolerated ratio? (GC-pressure creep /
   cache-degradation that only shows after hours — the signal a one-shot bench
   `p99(1M)/p99(1K)` scale-invariance gate structurally cannot see).

This module is the COMMON drift LOGIC, factored OUT of any wall-clock runner so
it is unit-testable to 100% line+branch on synthetic RSS/fd/latency series in
milliseconds. It reuses the EXISTING rails exactly like
:class:`brutex.observability.pull_health.PullHealthMonitor` (the in-repo
precedent): the hysteresis-aware :class:`~brutex.observability.sla.SLADetector`
(an OK↔BREACH edge fires once; a steady breach never re-spams), a
:class:`~brutex.observability.metrics.MetricRegistry` gauge mirror (``/metrics``
carries the live soak health), and a best-effort breach callback (production
wires :meth:`brutex.notifications.sweep_alerts.SweepAlerter.soak_drift`).

## The three rules (value-provider closures over baseline + latest)

The detector holds three GREATER_THAN rules, all CRITICAL, whose value-providers
read closures over the monitor's FIRST fully-populated window (``_baseline``)
and its MOST-RECENT window (``_latest``):

* ``soak.rss_ceiling`` — ``latest.rss_kib > rss_ceiling_kib``.
* ``soak.fd_growth`` — ``latest.fd_count − baseline.fd_count > fd_growth_tolerance``.
* ``soak.p99_drift`` — ``percentile(latest.lat, 99) / percentile(baseline.lat, 99)
  > p99_drift_ratio``.

Every provider returns ``None`` — the :class:`SLADetector` "value not available
yet" skip contract — when its inputs are unavailable: the probe abstained on
this platform (``rss``/``fd`` ``None``), the baseline is not yet set, a latency
window is empty, or the baseline p99 is non-positive (a zero-latency baseline
makes the ratio meaningless). ``None`` never fabricates a 0 (Golden Rule #10).

The p99 percentile is the ONE COMMON :func:`brutex.metrics.compute.percentile`
(NumPy linear-interp over a SORTED copy) for BOTH windows — a single convention,
never a nearest-rank-vs-linear mismatch (Golden Rule #11).

## Honest envelope (Golden Rule #10)

:meth:`SoakDriftMonitor.observe_window` is **best-effort at the alert edge**: the
breach / recovery callbacks are blanket-guarded so a Telegram outage mid-soak
can NEVER mask a real drift breach nor crash the hours-long run (the same
robustness :class:`PullHealthMonitor` gives ``on_degraded``). The RSS rule is an
absolute CEILING, not a growth slope — the ``ru_maxrss`` fallback the macOS
soak tier uses is a process-lifetime PEAK, so it answers "did we ever cross a
hard wall" not "did we grow N bytes/window". Thresholds are operator-tunable
heuristics, not proven limits.

## Decision: DEC-SOAK-REAL-TIER-001 (MIGRATION.md entry #1540).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from brutex.metrics.compute import percentile
from brutex.observability.metrics import MetricRegistry
from brutex.observability.sla import (
    SLADetector,
    SLAEvent,
    SLAEventCallback,
    SLAPredicate,
    SLARule,
    SLASeverity,
    SLAState,
)

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Default thresholds (operator-tunable heuristics — NOT proven limits)
# ────────────────────────────────────────────────────────────────────────

#: Absolute resident-set ceiling (KiB) above which ``soak.rss_ceiling`` fires.
#: Deliberately generous — a CEILING backstop for a native leak, not a tight
#: per-window slope (the macOS fallback reads a PEAK ``ru_maxrss``).
DEFAULT_RSS_CEILING_KIB: Final[float] = 4 * 1024 * 1024  # 4 GiB

#: Open-fd growth (latest − baseline) above which ``soak.fd_growth`` fires. A
#: small positive tolerance absorbs benign transient handles; a genuine leak
#: climbs without bound and crosses it.
DEFAULT_FD_GROWTH_TOLERANCE: Final[int] = 64

#: p99-latency drift ratio (latest / baseline) above which ``soak.p99_drift``
#: fires. ``2.0`` = the late windows' p99 doubled vs the first — a clear
#: time-drift signal well past normal jitter.
DEFAULT_P99_DRIFT_RATIO: Final[float] = 2.0

#: The percentile the drift rule tracks (p99, per CLAUDE.md §2 Rule #12 SLAs).
_DRIFT_PERCENTILE: Final[float] = 99.0

#: Prometheus gauge names mirrored from each observed window.
_GAUGE_RSS_KIB: Final[str] = "brutex_soak_rss_kib"
_GAUGE_FD_COUNT: Final[str] = "brutex_soak_fd_count"
_GAUGE_P99_RATIO: Final[str] = "brutex_soak_p99_ratio"

#: Rule names (also the Telegram dedupe keys).
RULE_RSS_CEILING: Final[str] = "soak.rss_ceiling"
RULE_FD_GROWTH: Final[str] = "soak.fd_growth"
RULE_P99_DRIFT: Final[str] = "soak.p99_drift"


@dataclass(frozen=True, slots=True)
class _Window:
    """One observed soak window's raw samples.

    Attributes
    ----------
    rss_kib
        Resident-set size (KiB) at the window, or ``None`` when the probe
        abstained (no ``/proc`` and no ``resource`` — see :mod:`process_probe`).
    fd_count
        Open file-descriptor count at the window, or ``None`` when the probe
        abstained.
    latencies
        The window's per-operation latency samples (any unit; the drift rule is
        a ratio so the unit cancels). Empty when the window measured no ops.
    """

    rss_kib: int | None
    fd_count: int | None
    latencies: tuple[float, ...]

    @property
    def fully_populated(self) -> bool:
        """A window fit to be the baseline: non-empty latencies AND an rss or fd read."""
        return bool(self.latencies) and (self.rss_kib is not None or self.fd_count is not None)


class SoakDriftMonitor:
    """Watch a soak's per-window RSS / fd / p99-latency and route drift to alerts.

    Built ONCE per soak run and fed each time window via :meth:`observe_window`.
    Holds an :class:`~brutex.observability.sla.SLADetector` whose rules read
    closures over the first fully-populated window (the baseline) and the most
    recent window, mirrors every gauge into a
    :class:`~brutex.observability.metrics.MetricRegistry`, and routes a breach /
    recovery edge to the (blanket-guarded) callbacks.

    Parameters
    ----------
    registry
        The metric registry to mirror gauges into.
    on_breach, on_recovery
        The :class:`~brutex.observability.sla.SLAEventCallback` edges. Both are
        blanket-guarded before reaching the detector, so a raising alert sink (a
        Telegram outage) can never crash the hours-long soak nor mask a later
        breach (Golden Rule #10; mirrors :class:`PullHealthMonitor`). Production
        wires :meth:`brutex.notifications.sweep_alerts.SweepAlerter.soak_drift`.
    rss_ceiling_kib, fd_growth_tolerance, p99_drift_ratio
        Threshold overrides (default to the module constants).
    """

    __slots__ = (
        "_baseline",
        "_detector",
        "_latest",
        "_on_breach",
        "_on_recovery",
        "_registry",
    )

    def __init__(
        self,
        *,
        registry: MetricRegistry,
        on_breach: SLAEventCallback,
        on_recovery: SLAEventCallback,
        rss_ceiling_kib: float = DEFAULT_RSS_CEILING_KIB,
        fd_growth_tolerance: int = DEFAULT_FD_GROWTH_TOLERANCE,
        p99_drift_ratio: float = DEFAULT_P99_DRIFT_RATIO,
    ) -> None:
        self._registry = registry
        self._on_breach = on_breach
        self._on_recovery = on_recovery
        #: The first fully-populated window (baseline for fd + p99 rules).
        self._baseline: _Window | None = None
        #: The most-recent window — an EMPTY sentinel until the first observe, so
        #: the providers never need a ``latest is None`` branch (they abstain on
        #: the empty sentinel's ``None`` / empty fields exactly as designed).
        self._latest: _Window = _Window(rss_kib=None, fd_count=None, latencies=())
        self._register_gauges()
        self._detector = SLADetector(
            rules=self._build_rules(
                rss_ceiling_kib=rss_ceiling_kib,
                fd_growth_tolerance=fd_growth_tolerance,
                p99_drift_ratio=p99_drift_ratio,
            ),
            on_breach=self._guarded_breach,
            on_recovery=self._guarded_recovery,
        )

    # ── Setup ────────────────────────────────────────────────────────

    def _register_gauges(self) -> None:
        """Declare the mirrored Prometheus gauges (idempotent re-registration)."""
        self._registry.gauge(_GAUGE_RSS_KIB, "Soak resident-set size (KiB)")
        self._registry.gauge(_GAUGE_FD_COUNT, "Soak open file-descriptor count")
        self._registry.gauge(_GAUGE_P99_RATIO, "Soak p99 latency drift ratio (latest / baseline)")

    def _build_rules(
        self,
        *,
        rss_ceiling_kib: float,
        fd_growth_tolerance: int,
        p99_drift_ratio: float,
    ) -> tuple[SLARule, ...]:
        """Construct the three drift rules over the baseline + latest closures."""
        return (
            SLARule(
                name=RULE_RSS_CEILING,
                value_provider=self._rss_provider,
                predicate=SLAPredicate.GREATER_THAN,
                threshold=rss_ceiling_kib,
                severity=SLASeverity.CRITICAL,
                description="Resident memory crossed its absolute ceiling",
            ),
            SLARule(
                name=RULE_FD_GROWTH,
                value_provider=self._fd_growth_provider,
                predicate=SLAPredicate.GREATER_THAN,
                threshold=float(fd_growth_tolerance),
                severity=SLASeverity.CRITICAL,
                description="Open file-descriptor count grew above its baseline",
            ),
            SLARule(
                name=RULE_P99_DRIFT,
                value_provider=self._p99_ratio_provider,
                predicate=SLAPredicate.GREATER_THAN,
                threshold=p99_drift_ratio,
                severity=SLASeverity.CRITICAL,
                description="p99 latency drifted above the baseline window",
            ),
        )

    # ── Value-provider closures over baseline + latest ───────────────

    def _rss_provider(self) -> float | None:
        """The latest window's RSS (KiB), or ``None`` when the probe abstained."""
        if self._latest.rss_kib is None:
            return None
        return float(self._latest.rss_kib)

    def _fd_growth_provider(self) -> float | None:
        """``latest.fd − baseline.fd`` growth, or ``None`` when unavailable.

        ``None`` when the baseline is not yet set, or either window's fd read
        abstained.
        """
        if self._baseline is None:
            return None
        if self._latest.fd_count is None or self._baseline.fd_count is None:
            return None
        return float(self._latest.fd_count - self._baseline.fd_count)

    def _p99_ratio_provider(self) -> float | None:
        """``p99(latest) / p99(baseline)`` drift ratio, or ``None`` when unavailable.

        ``None`` when the baseline is not yet set, the latest window is empty, or
        the baseline p99 is non-positive (a zero-latency baseline makes the ratio
        meaningless — the detector skips rather than dividing by zero). The
        baseline's own latencies are guaranteed non-empty (a window is only
        adopted as the baseline when :attr:`_Window.fully_populated`, which
        requires non-empty latencies), so only the LATEST window can be empty.
        """
        if self._baseline is None:
            return None
        if not self._latest.latencies:
            return None
        base_p99 = percentile(sorted(self._baseline.latencies), _DRIFT_PERCENTILE)
        if base_p99 <= 0.0:
            return None
        latest_p99 = percentile(sorted(self._latest.latencies), _DRIFT_PERCENTILE)
        return latest_p99 / base_p99

    # ── Guarded edge routing ─────────────────────────────────────────

    def _guarded_breach(self, event: SLAEvent) -> None:
        """Route an OK→BREACH edge to *on_breach*, swallowing any callback error."""
        logger.warning(
            "soak_drift.breach rule=%s observed=%.4f threshold=%.4f",
            event.rule_name,
            event.observed_value,
            event.threshold,
        )
        try:
            self._on_breach(event)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort alert
            logger.exception(
                "soak_drift: on_breach callback raised for rule %s; monitor continues",
                event.rule_name,
            )

    def _guarded_recovery(self, event: SLAEvent) -> None:
        """Route a BREACH→OK edge to *on_recovery*, swallowing any callback error."""
        logger.info(
            "soak_drift.recovery rule=%s observed=%.4f threshold=%.4f",
            event.rule_name,
            event.observed_value,
            event.threshold,
        )
        try:
            self._on_recovery(event)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort alert
            logger.exception(
                "soak_drift: on_recovery callback raised for rule %s; monitor continues",
                event.rule_name,
            )

    # ── The observe hook ─────────────────────────────────────────────

    def observe_window(
        self,
        *,
        rss_kib: int | None,
        fd_count: int | None,
        latency_samples: Sequence[float],
    ) -> tuple[SLAEvent, ...]:
        """Ingest one soak window: set the baseline (first time), mirror, evaluate.

        Sets ``_baseline`` on the FIRST fully-populated window (non-empty
        latencies AND an rss or fd read), always updates ``_latest``, mirrors the
        present values into the gauges, then runs one :meth:`SLADetector.check_once`
        pass and returns its (possibly empty) events.

        Parameters
        ----------
        rss_kib, fd_count
            The window's probe reads (``None`` = the probe abstained on this
            platform; the corresponding rule then abstains too).
        latency_samples
            The window's per-op latency samples (empty = the window measured no
            ops; the p99 rule abstains).
        """
        window = _Window(
            rss_kib=rss_kib,
            fd_count=fd_count,
            latencies=tuple(latency_samples),
        )
        if self._baseline is None and window.fully_populated:
            self._baseline = window
        self._latest = window
        self._mirror_gauges()
        return self._detector.check_once()

    def _mirror_gauges(self) -> None:
        """Mirror the latest window's present values into the gauges (best-effort)."""
        if self._latest.rss_kib is not None:
            self._registry.set(_GAUGE_RSS_KIB, float(self._latest.rss_kib))
        if self._latest.fd_count is not None:
            self._registry.set(_GAUGE_FD_COUNT, float(self._latest.fd_count))
        ratio = self._p99_ratio_provider()
        if ratio is not None:
            self._registry.set(_GAUGE_P99_RATIO, ratio)

    # ── Test-only accessor ───────────────────────────────────────────

    def state_of(self, rule_name: str) -> SLAState:
        """Return the current per-rule :class:`~brutex.observability.sla.SLAState`."""
        return self._detector.state_of(rule_name)


__all__ = [
    "DEFAULT_FD_GROWTH_TOLERANCE",
    "DEFAULT_P99_DRIFT_RATIO",
    "DEFAULT_RSS_CEILING_KIB",
    "RULE_FD_GROWTH",
    "RULE_P99_DRIFT",
    "RULE_RSS_CEILING",
    "SoakDriftMonitor",
]
