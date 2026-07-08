"""Unit tests for :mod:`brutex.observability.soak_drift` (DEC-SOAK-REAL-TIER-001).

100% line + branch coverage of the pure drift LOGIC — every provider branch and
every rule edge driven by synthetic RSS / fd / latency windows so a leak /
drift is caught deterministically, never by eyeball:

* baseline adoption (first fully-populated window only) + abstention when a
  window has no rss/fd or no latencies;
* each rule provider's abstain branches (probe abstained, baseline not set,
  zero-latency baseline);
* the RSS-ceiling / fd-growth / p99-drift breach AND recovery edges (hysteresis
  from :class:`~brutex.observability.sla.SLADetector`);
* the gauge mirror (present / absent value branches);
* the blanket-guarded breach + recovery callbacks (a raising alert sink never
  crashes the soak).
"""

from __future__ import annotations

import pytest
from brutex.observability.metrics import MetricRegistry
from brutex.observability.sla import SLAEvent, SLAState
from brutex.observability.soak_drift import (
    RULE_FD_GROWTH,
    RULE_P99_DRIFT,
    RULE_RSS_CEILING,
    SoakDriftMonitor,
)


class RecordingSink:
    """Captures every breach / recovery event; can simulate a raise."""

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.events: list[SLAEvent] = []
        self.raise_on_call = raise_on_call

    def __call__(self, event: SLAEvent) -> None:
        if self.raise_on_call:
            raise RuntimeError("simulated alert-sink failure")
        self.events.append(event)


def _monitor(
    *,
    breach: RecordingSink | None = None,
    recovery: RecordingSink | None = None,
    **kwargs: float | int,
) -> tuple[SoakDriftMonitor, RecordingSink, RecordingSink, MetricRegistry]:
    on_breach = breach if breach is not None else RecordingSink()
    on_recovery = recovery if recovery is not None else RecordingSink()
    registry = MetricRegistry()
    monitor = SoakDriftMonitor(
        registry=registry,
        on_breach=on_breach,
        on_recovery=on_recovery,
        **kwargs,  # type: ignore[arg-type]
    )
    return monitor, on_breach, on_recovery, registry


# ── Baseline adoption ────────────────────────────────────────────────


class TestBaselineAdoption:
    def test_first_fully_populated_window_becomes_baseline(self) -> None:
        monitor, _b, _r, _reg = _monitor()
        # A window with latencies + fd is fully populated → baseline set → the
        # fd-growth provider is now live (0 growth vs itself, no breach).
        monitor.observe_window(rss_kib=1000, fd_count=10, latency_samples=[1.0, 2.0])
        assert monitor.state_of(RULE_FD_GROWTH) is SLAState.OK

    def test_non_fully_populated_window_does_not_set_baseline(self) -> None:
        monitor, _b, _r, _reg = _monitor()
        # Latencies present but NO rss and NO fd → not fully populated → baseline
        # stays unset → fd-growth abstains (stays OK, provider returns None).
        events = monitor.observe_window(rss_kib=None, fd_count=None, latency_samples=[1.0, 2.0])
        assert events == ()
        assert monitor.state_of(RULE_FD_GROWTH) is SLAState.OK
        # A later fully-populated window is the one adopted as baseline.
        monitor.observe_window(rss_kib=500, fd_count=5, latency_samples=[1.0])
        assert monitor._baseline is not None
        assert monitor._baseline.fd_count == 5


# ── RSS ceiling rule ─────────────────────────────────────────────────


class TestRssCeiling:
    def test_breaches_and_recovers(self) -> None:
        monitor, breach, recovery, _reg = _monitor(rss_ceiling_kib=2000.0)
        monitor.observe_window(rss_kib=1000, fd_count=1, latency_samples=[1.0])
        assert monitor.state_of(RULE_RSS_CEILING) is SLAState.OK
        # Cross the ceiling → one breach edge.
        monitor.observe_window(rss_kib=5000, fd_count=1, latency_samples=[1.0])
        assert monitor.state_of(RULE_RSS_CEILING) is SLAState.BREACH
        assert [e.rule_name for e in breach.events] == [RULE_RSS_CEILING]
        # Drop back under → one recovery edge.
        monitor.observe_window(rss_kib=800, fd_count=1, latency_samples=[1.0])
        assert monitor.state_of(RULE_RSS_CEILING) is SLAState.OK
        assert [e.rule_name for e in recovery.events] == [RULE_RSS_CEILING]

    def test_abstains_when_rss_probe_returned_none(self) -> None:
        monitor, breach, _r, _reg = _monitor(rss_ceiling_kib=1.0)
        # fd present so the window is fully populated, but rss is None → the
        # RSS rule provider abstains even though the ceiling is tiny.
        monitor.observe_window(rss_kib=None, fd_count=1, latency_samples=[1.0])
        assert monitor.state_of(RULE_RSS_CEILING) is SLAState.OK
        assert breach.events == []


# ── fd growth rule ───────────────────────────────────────────────────


class TestFdGrowth:
    def test_growth_above_tolerance_breaches(self) -> None:
        monitor, breach, _r, _reg = _monitor(fd_growth_tolerance=10)
        monitor.observe_window(rss_kib=100, fd_count=20, latency_samples=[1.0])  # baseline fd=20
        monitor.observe_window(rss_kib=100, fd_count=25, latency_samples=[1.0])  # +5, no breach
        assert monitor.state_of(RULE_FD_GROWTH) is SLAState.OK
        monitor.observe_window(rss_kib=100, fd_count=40, latency_samples=[1.0])  # +20 > 10
        assert monitor.state_of(RULE_FD_GROWTH) is SLAState.BREACH
        assert [e.rule_name for e in breach.events] == [RULE_FD_GROWTH]

    def test_abstains_when_baseline_unset(self) -> None:
        monitor, _b, _r, _reg = _monitor(fd_growth_tolerance=0)
        # No fully-populated window yet (no latencies) → baseline None → abstain.
        monitor.observe_window(rss_kib=None, fd_count=5, latency_samples=[])
        assert monitor.state_of(RULE_FD_GROWTH) is SLAState.OK

    def test_abstains_when_latest_fd_none(self) -> None:
        monitor, _b, _r, _reg = _monitor(fd_growth_tolerance=0)
        monitor.observe_window(rss_kib=100, fd_count=5, latency_samples=[1.0])  # baseline
        # Latest window's fd probe abstained → fd-growth abstains (stays OK).
        monitor.observe_window(rss_kib=100, fd_count=None, latency_samples=[1.0])
        assert monitor.state_of(RULE_FD_GROWTH) is SLAState.OK

    def test_abstains_when_baseline_fd_none(self) -> None:
        monitor, _b, _r, _reg = _monitor(fd_growth_tolerance=0)
        # Baseline is fully populated via RSS but its fd is None → the growth
        # delta is unknowable → abstain even with a later real fd.
        monitor.observe_window(rss_kib=100, fd_count=None, latency_samples=[1.0])  # baseline
        assert monitor._baseline is not None
        monitor.observe_window(rss_kib=100, fd_count=50, latency_samples=[1.0])
        assert monitor.state_of(RULE_FD_GROWTH) is SLAState.OK


# ── p99 latency drift rule ───────────────────────────────────────────


class TestP99Drift:
    def test_drift_above_ratio_breaches_and_recovers(self) -> None:
        monitor, breach, recovery, _reg = _monitor(p99_drift_ratio=2.0)
        # Baseline p99 ≈ 10 (linear-interp over a flat 10-series).
        monitor.observe_window(rss_kib=100, fd_count=1, latency_samples=[10.0] * 20)
        assert monitor.state_of(RULE_P99_DRIFT) is SLAState.OK
        # Latest p99 ≈ 30 → ratio 3.0 > 2.0 → breach.
        monitor.observe_window(rss_kib=100, fd_count=1, latency_samples=[30.0] * 20)
        assert monitor.state_of(RULE_P99_DRIFT) is SLAState.BREACH
        assert [e.rule_name for e in breach.events] == [RULE_P99_DRIFT]
        # Latest p99 back to ≈ 12 → ratio 1.2 < 2.0 → recovery.
        monitor.observe_window(rss_kib=100, fd_count=1, latency_samples=[12.0] * 20)
        assert monitor.state_of(RULE_P99_DRIFT) is SLAState.OK
        assert [e.rule_name for e in recovery.events] == [RULE_P99_DRIFT]

    def test_abstains_when_baseline_unset(self) -> None:
        monitor, _b, _r, _reg = _monitor(p99_drift_ratio=0.001)
        # No fully-populated window (no rss/fd) → baseline None → abstain.
        monitor.observe_window(rss_kib=None, fd_count=None, latency_samples=[5.0, 9.0])
        assert monitor.state_of(RULE_P99_DRIFT) is SLAState.OK

    def test_abstains_when_latest_window_empty(self) -> None:
        # ratio 2.0 so the baseline window (self-ratio 1.0) does not breach; the
        # empty latest window then abstains, leaving the rule OK.
        monitor, _b, _r, _reg = _monitor(p99_drift_ratio=2.0)
        monitor.observe_window(rss_kib=100, fd_count=1, latency_samples=[10.0])  # baseline
        # Latest window measured no ops → empty latencies → p99 rule abstains.
        monitor.observe_window(rss_kib=100, fd_count=1, latency_samples=[])
        assert monitor.state_of(RULE_P99_DRIFT) is SLAState.OK

    def test_abstains_when_baseline_p99_non_positive(self) -> None:
        monitor, _b, _r, _reg = _monitor(p99_drift_ratio=0.001)
        # Baseline all-zero latencies (fully populated via rss) → base p99 == 0 →
        # the ratio is meaningless → abstain (never a divide-by-zero).
        monitor.observe_window(rss_kib=100, fd_count=1, latency_samples=[0.0, 0.0, 0.0])
        monitor.observe_window(rss_kib=100, fd_count=1, latency_samples=[5.0, 9.0])
        assert monitor.state_of(RULE_P99_DRIFT) is SLAState.OK


# ── Gauge mirror ─────────────────────────────────────────────────────


class TestGaugeMirror:
    def test_mirrors_present_values(self) -> None:
        monitor, _b, _r, registry = _monitor()
        monitor.observe_window(rss_kib=100, fd_count=7, latency_samples=[10.0])  # baseline
        monitor.observe_window(rss_kib=250, fd_count=9, latency_samples=[20.0])
        assert registry.gauge_value("brutex_soak_rss_kib") == 250.0
        assert registry.gauge_value("brutex_soak_fd_count") == 9.0
        # ratio ≈ 2.0 (20/10) is mirrored once both windows have latencies.
        assert registry.gauge_value("brutex_soak_p99_ratio") == pytest.approx(2.0)

    def test_skips_absent_values(self) -> None:
        monitor, _b, _r, registry = _monitor()
        # rss + fd both None, no baseline yet → all three gauges stay at their
        # default 0.0 (nothing mirrored).
        monitor.observe_window(rss_kib=None, fd_count=None, latency_samples=[10.0])
        assert registry.gauge_value("brutex_soak_rss_kib") == 0.0
        assert registry.gauge_value("brutex_soak_fd_count") == 0.0
        assert registry.gauge_value("brutex_soak_p99_ratio") == 0.0


# ── Guarded callbacks ────────────────────────────────────────────────


class TestGuardedCallbacks:
    def test_raising_breach_callback_is_swallowed(self) -> None:
        breach = RecordingSink(raise_on_call=True)
        monitor, _b, _r, _reg = _monitor(breach=breach, rss_ceiling_kib=100.0)
        monitor.observe_window(rss_kib=50, fd_count=1, latency_samples=[1.0])
        # The breaching window must NOT raise out of observe_window even though
        # the alert sink raises — the soak survives.
        monitor.observe_window(rss_kib=500, fd_count=1, latency_samples=[1.0])
        assert monitor.state_of(RULE_RSS_CEILING) is SLAState.BREACH

    def test_raising_recovery_callback_is_swallowed(self) -> None:
        recovery = RecordingSink(raise_on_call=True)
        monitor, _b, _r, _reg = _monitor(recovery=recovery, rss_ceiling_kib=100.0)
        monitor.observe_window(rss_kib=500, fd_count=1, latency_samples=[1.0])  # breach
        # Recovery edge fires the raising sink → swallowed, state still flips OK.
        monitor.observe_window(rss_kib=50, fd_count=1, latency_samples=[1.0])
        assert monitor.state_of(RULE_RSS_CEILING) is SLAState.OK
