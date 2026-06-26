"""Unit tests for the per-worker heartbeat WIRING (DEC-PULL-WORKER-HEARTBEAT-001).

Proves the dashboard live-pull build site BINDS the per-worker heartbeat alongside the
aggregate :class:`PullHealthMonitor`:

* :func:`_build_pull_heartbeat` returns ``None`` for a dry-run (no controller), a real
  :class:`~brutex.observability.pull_heartbeat.PullHeartbeat` for a live controller, and
  ``None`` (best-effort) when construction raises — never propagating into the pull.
* :func:`brutex.dashboard.pull_controller._observe_rate` ticks the wired heartbeat by
  calling :meth:`PullHeartbeat.scan` on its cadence, so a wedged worker is flagged; the
  no-op guard short-circuits a dry-run (no controller).
* End-to-end: a worker beaten then left stale → one ``pull_degraded`` push through the
  heartbeat's wired :meth:`SweepAlerter.pull_degraded` alert.

Test types (CLAUDE.md §12): unit (every builder branch + the scan-tick branch) +
integration (a stuck worker routes through the wired heartbeat → real
:class:`SweepAlerter` + fake notifier → ``pull_degraded`` event).
"""

# Whitebox test of the module-private `_build_pull_heartbeat` (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_CEILING
from brutex.adapters.rate_common import RateController
from brutex.dashboard.pull_controller import _build_pull_heartbeat, _observe_rate
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.notifications.telegram import TelegramEvent
from brutex.observability.metrics import (
    MetricRegistry,
    get_registry,
    reset_global_registry,
)
from brutex.observability.pull_heartbeat import PullHeartbeat, make_heartbeat_window_tick


class _FakeNotifier:
    """Captures every published event so a stuck worker's ``pull_degraded`` is visible."""

    def __init__(self) -> None:
        self.events: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        self.events.append(event)


class _FakeMeter:
    def __init__(self, snap: dict[str, object]) -> None:
        self._snap = snap

    def snapshot(self) -> dict[str, object]:
        return self._snap


class _FakeClient:
    def __init__(self, meter_snap: dict[str, object]) -> None:
        self.meter = _FakeMeter(meter_snap)


class _FakeAdapter:
    """A minimal adapter whose ``client.meter`` drives :func:`_observe_rate`."""

    def __init__(self, meter_snap: dict[str, object]) -> None:
        self.client = _FakeClient(meter_snap)


class _StubClock:
    """A manually-advanced monotonic clock for deterministic staleness."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _live_controller() -> RateController:
    """A live controller mirroring the dashboard build (production ceiling)."""
    return RateController(ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING, ceiling_per_min=500)


class TestBuildPullHeartbeat:
    """Every branch of the best-effort :func:`_build_pull_heartbeat`."""

    def test_none_controller_disables_heartbeat(self) -> None:
        """A dry-run / injected-factory pull (no controller) builds NO heartbeat."""
        assert _build_pull_heartbeat(None) is None

    def test_live_controller_builds_a_wired_heartbeat(self) -> None:
        """A live controller yields a real heartbeat on the process-global registry."""
        reset_global_registry()
        try:
            hb = _build_pull_heartbeat(_live_controller(), units_total=42)
            assert isinstance(hb, PullHeartbeat)
            # It registered its gauges into the process-global registry, so /metrics
            # will carry the live pull-progress series (declared at 0.0).
            assert get_registry().gauge_value("brutex_pull_workers_active") == 0.0
        finally:
            reset_global_registry()

    def test_construction_failure_is_swallowed(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A raise during construction yields ``None`` — never crashes the pull."""

        def _boom(*_args: object, **_kwargs: object) -> PullHeartbeat:
            raise RuntimeError("simulated heartbeat construction failure")

        monkeypatch.setattr(
            "brutex.observability.pull_heartbeat.PullHeartbeat",
            _boom,
        )
        assert _build_pull_heartbeat(_live_controller()) is None


class TestObserveRateTicksHeartbeat:
    """The :func:`_observe_rate` ``heartbeat`` branch calls :meth:`PullHeartbeat.scan`."""

    def test_scan_runs_when_heartbeat_wired(self) -> None:
        """A live controller + a wired heartbeat → _observe_rate scans it (gauges set)."""
        registry = MetricRegistry()
        hb = PullHeartbeat(registry=registry)
        hb.beat("w1")
        adapter = _FakeAdapter({"effective_rps": 4.0, "latency_ms_p95": 100.0, "in_flight": 4})
        controller = _live_controller()
        # No rate_observer, no monitor — ONLY the heartbeat is wired. The guard must
        # still proceed (heartbeat is not None) and scan, mirroring the progress gauge.
        _observe_rate(None, controller, adapter, None, hb)  # type: ignore[arg-type]
        assert registry.gauge_value("brutex_pull_workers_active") == 1.0

    def test_no_op_when_controller_none(self) -> None:
        """A dry-run (no controller) short-circuits BEFORE scanning the heartbeat."""
        registry = MetricRegistry()
        hb = PullHeartbeat(registry=registry)
        hb.beat("w1")
        adapter = _FakeAdapter({})
        _observe_rate(None, None, adapter, None, hb)  # type: ignore[arg-type]
        # The heartbeat was never scanned → no progress gauge mirrored.
        assert registry.gauge_value("brutex_pull_workers_active") == 0.0


class TestStuckWorkerFiresPullDegraded:
    """A stuck worker fed through the WIRED heartbeat reaches a real ``pull_degraded``."""

    def test_stuck_routes_to_pull_degraded(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """End-to-end: a builder-wired heartbeat's stuck worker → one ``pull_degraded``.

        The builder injects no clock (it uses the real monotonic clock + the 120 s
        default), so to drive staleness deterministically we build through the builder
        with a STUB clock injected as the ``PullHeartbeat`` default — patched at the
        class level so the builder's ``PullHeartbeat(...)`` picks it up — proving the
        builder's ``on_stuck`` → ``SweepAlerter.pull_degraded`` wiring end-to-end.
        """
        notifier = _FakeNotifier()
        real_alerter = SweepAlerter(notifier=notifier)
        monkeypatch.setattr(
            "brutex.notifications.sweep_alerts.SweepAlerter",
            lambda: real_alerter,
        )
        clock = _StubClock()
        # The builder calls PullHeartbeat(registry=, units_total=, on_stuck=) WITHOUT a
        # clock, so the default applies. Wrap the real class to inject the stub clock +
        # a small stale threshold so the scan flags deterministically; the builder's
        # on_stuck wiring is preserved verbatim.
        real_cls = PullHeartbeat

        def _patched(**kwargs: object) -> PullHeartbeat:
            kwargs.setdefault("stale_after_s", 10.0)
            kwargs.setdefault("clock", clock)
            return real_cls(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.observability.pull_heartbeat.PullHeartbeat", _patched)

        controller = _live_controller()
        hb = _build_pull_heartbeat(controller)
        assert isinstance(hb, PullHeartbeat)

        tick = make_heartbeat_window_tick(hb, lambda: None, worker_id=lambda: "NSE-NIFTY 1minute")
        tick()  # beat at clock.now == 0.0
        clock.now = 200.0  # 200 s since the beat ≫ 10 s threshold → stuck
        adapter = _FakeAdapter({"effective_rps": 4.0, "latency_ms_p95": 100.0, "in_flight": 4})
        _observe_rate(None, controller, adapter, None, hb)  # type: ignore[arg-type]

        # The message is MarkdownV2-escaped (``pull.worker_stuck`` → ``pull\.worker\_stuck``),
        # so match on the un-escaped tokens that survive escaping.
        degraded = [e for e in notifier.events if "Pull health degraded" in e.message]
        assert len(degraded) == 1
        assert "wedged" in degraded[0].message
