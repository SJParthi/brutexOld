"""Unit tests for the live pull-health monitor WIRING (DEC-PULL-HEALTH-WIRING-001).

PR #893 (DEC-PULL-HEALTH-MONITOR-001) shipped :class:`PullHealthMonitor` + the
:func:`brutex.dashboard.pull_controller._observe_rate` ``monitor`` seam, but NO
production build site constructed one — so on a real pull it watched nothing. This
suite proves the dashboard live-pull build site now BINDS the monitor:

* :func:`_build_pull_health_monitor` returns ``None`` for a dry-run (no controller),
  a real :class:`PullHealthMonitor` for a live controller, and ``None`` (best-effort)
  when construction raises — never propagating into the pull.
* The built monitor is wired to the real :class:`MetricRegistry` (gauges mirror) AND
  to the :meth:`SweepAlerter.pull_degraded` Telegram alert, so a simulated breach fed
  through :func:`_observe_rate` fires exactly one ``pull_degraded`` push.

Test types (CLAUDE.md §12): unit (every branch of the builder) + integration (a
breach edge routes through the wired monitor → real :class:`SweepAlerter` + fake
notifier → ``pull_degraded`` event).
"""

# Whitebox test of the module-private `_build_pull_health_monitor` (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_CEILING
from brutex.adapters.rate_common import RateController
from brutex.dashboard.pull_controller import _build_pull_health_monitor, _observe_rate
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.notifications.telegram import TelegramEvent
from brutex.observability.metrics import (
    MetricRegistry,
    get_registry,
    reset_global_registry,
)
from brutex.observability.pull_health import PullHealthMonitor


class _FakeNotifier:
    """Captures every published event so a breach's ``pull_degraded`` push is visible."""

    def __init__(self) -> None:
        self.events: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        self.events.append(event)


class _FakeClient:
    """A pull adapter's ``client`` exposing a ``meter`` with a fixed snapshot."""

    def __init__(self, meter_snap: dict[str, object]) -> None:
        self.meter = _FakeMeter(meter_snap)


class _FakeMeter:
    def __init__(self, snap: dict[str, object]) -> None:
        self._snap = snap

    def snapshot(self) -> dict[str, object]:
        return self._snap


class _FakeAdapter:
    """A minimal adapter whose ``client.meter`` drives :func:`_observe_rate`."""

    def __init__(self, meter_snap: dict[str, object]) -> None:
        self.client = _FakeClient(meter_snap)


def _live_controller() -> RateController:
    """A live controller mirroring the dashboard build (production ceiling)."""
    return RateController(ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING, ceiling_per_min=500)


class TestBuildPullHealthMonitor:
    """Every branch of the best-effort :func:`_build_pull_health_monitor`."""

    def test_none_controller_disables_monitor(self) -> None:
        """A dry-run / injected-factory pull (no controller) builds NO monitor."""
        assert _build_pull_health_monitor(None) is None

    def test_live_controller_builds_a_wired_monitor(self) -> None:
        """A live controller yields a real monitor on the process-global registry."""
        reset_global_registry()
        try:
            monitor = _build_pull_health_monitor(_live_controller())
            assert isinstance(monitor, PullHealthMonitor)
            # The monitor registered its gauges into the process-global registry,
            # so /metrics will carry the live pull-health series (declared at 0.0).
            assert get_registry().gauge_value("brutex_pull_achieved_rps") == 0.0
        finally:
            reset_global_registry()

    def test_construction_failure_is_swallowed(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A raise during construction yields ``None`` — never crashes the pull."""

        def _boom(*_args: object, **_kwargs: object) -> PullHealthMonitor:
            raise RuntimeError("simulated monitor construction failure")

        # Patch the symbol the builder imports lazily INSIDE its try block.
        monkeypatch.setattr(
            "brutex.observability.pull_health.PullHealthMonitor",
            _boom,
        )
        assert _build_pull_health_monitor(_live_controller()) is None


class TestBreachFiresPullDegraded:
    """A breach fed through the WIRED monitor reaches a real ``pull_degraded`` alert."""

    def test_observe_breach_routes_to_pull_degraded(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """End-to-end: build → _observe_rate(breach snapshot) → one pull_degraded push."""
        notifier = _FakeNotifier()
        # Force the builder's SweepAlerter() to use our fake notifier so a fired
        # pull_degraded alert is captured (Telegram disabled in CI otherwise).
        # SweepAlerter is imported lazily INSIDE the builder's try block, so patch the
        # source symbol the lazy import resolves.
        real_alerter = SweepAlerter(notifier=notifier)
        monkeypatch.setattr(
            "brutex.notifications.sweep_alerts.SweepAlerter",
            lambda: real_alerter,
        )

        controller = _live_controller()
        monitor = _build_pull_health_monitor(controller)
        assert isinstance(monitor, PullHealthMonitor)

        # A snapshot that breaches ONLY pull.write_latency_spike (p95 ≫ 1500 ms budget):
        # a fresh controller paces at its ceiling, so achieved/allowed = effective/ceiling
        # ≥ 0.6 (no achieved-below breach); the pool is full (in_flight == pool_size 4 →
        # no idle breach); recent_hits is 0 on a fresh controller (no rate storm). Fed
        # through the REAL _observe_rate seam — the production tick path.
        effective_at_ceiling = float(GROWW_RATE_LIMIT_RPS_CEILING)
        adapter = _FakeAdapter(
            {"effective_rps": effective_at_ceiling, "latency_ms_p95": 9000.0, "in_flight": 4}
        )
        _observe_rate(None, controller, adapter, monitor)  # type: ignore[arg-type]

        degraded = [e for e in notifier.events if "Pull health degraded" in e.message]
        assert len(degraded) == 1
        assert "write latency" in degraded[0].message

    def test_cold_start_first_tick_fires_no_alert_through_observe_rate(
        self,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """The production cold-start symptom, pinned at the production seam.

        DEC-PULL-HEALTH-WARN-DWELL-001: ~1s into a real pull the fresh controller
        paces AT the ceiling while the CallMeter's fixed-10s-divisor ``effective_rps``
        has barely ramped (ratio ≤ 0.1 < 0.6) and the pool has not fanned out
        (``in_flight`` 1 → idle 0.75 > 0.5). Before the warn dwell, that FIRST
        ``_observe_rate`` tick fired two scary ``pull_degraded`` Telegram WARNs at
        every pull start. Now: ZERO alerts on the first tick — while the ``/metrics``
        gauges still mirror the raw instantaneous values (only the ALERT is dwelled).
        """
        notifier = _FakeNotifier()
        real_alerter = SweepAlerter(notifier=notifier)
        monkeypatch.setattr(
            "brutex.notifications.sweep_alerts.SweepAlerter",
            lambda: real_alerter,
        )

        reset_global_registry()
        try:
            controller = _live_controller()
            monitor = _build_pull_health_monitor(controller)
            assert isinstance(monitor, PullHealthMonitor)

            # The ~1s-old-pool meter shape (near-zero ramp, one in-flight call).
            adapter = _FakeAdapter({"effective_rps": 0.1, "latency_ms_p95": 50.0, "in_flight": 1})
            _observe_rate(None, controller, adapter, monitor)  # type: ignore[arg-type]

            # ZERO pull_degraded pushes on the first tick (fails-before: two WARNs).
            degraded = [e for e in notifier.events if "Pull health degraded" in e.message]
            assert degraded == []
            # Raw-gauge honesty pin: the instantaneous values ARE mirrored from tick 1.
            assert get_registry().gauge_value("brutex_pull_achieved_rps") == 0.1
            assert get_registry().gauge_value("brutex_pull_allowed_rps") == float(
                GROWW_RATE_LIMIT_RPS_CEILING
            )
        finally:
            reset_global_registry()

    def test_observe_without_monitor_fires_nothing(self) -> None:
        """A pull with no monitor wired (None) never observes — the no-op guard."""
        registry = MetricRegistry()
        monitor = PullHealthMonitor(
            registry=registry,
            on_degraded=lambda *_: None,
            pool_size=4,
        )
        adapter = _FakeAdapter({"effective_rps": 4.0, "latency_ms_p95": 100.0, "in_flight": 4})
        # controller None (dry-run) → _observe_rate short-circuits before touching monitor.
        _observe_rate(None, None, adapter, monitor)  # type: ignore[arg-type]
        # No gauge was ever set (the monitor never observed).
        assert registry.gauge_value("brutex_pull_latency_ms_p95") == 0.0
