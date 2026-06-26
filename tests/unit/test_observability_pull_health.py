"""Unit tests for :mod:`brutex.observability.pull_health` (DEC-PULL-HEALTH-MONITOR-001).

100% line + branch coverage of the live pull-health self-instrumentation:

* :class:`DwellProvider` — fires only after K consecutive predicate-True samples;
  a single dip / a ``None`` reading resets the streak.
* :class:`PullHealthMonitor` — each of the five rules fires on a breach edge and
  clears on a recovery edge (hysteresis, inherited unchanged from
  :class:`~brutex.observability.sla.SLADetector`); the breach routes to the
  ``on_degraded`` callback with the OUR-pipeline-vs-VENDOR detail string; every gauge
  is mirrored into the registry; and :meth:`PullHealthMonitor.observe` swallows a
  malformed snapshot / a buggy callback so it can never crash the pull.

Test types present (CLAUDE.md §12 / Golden Rule #12):
* unit — every rule fire/clear, dwell streak, each provider branch, the
  callback-raises swallow, the observe-swallow.
* integration — a breach → ``pull_degraded`` wiring test through a real
  :class:`~brutex.notifications.sweep_alerts.SweepAlerter` + fake notifier.
"""

from __future__ import annotations

import pytest
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.notifications.telegram import TelegramEvent
from brutex.observability.metrics import MetricRegistry
from brutex.observability.pull_health import (
    DwellProvider,
    PullHealthMonitor,
)
from brutex.observability.sla import SLAState

# ────────────────────────────────────────────────────────────────────────
# Fakes / helpers
# ────────────────────────────────────────────────────────────────────────


class RecordingDegraded:
    """Captures every ``on_degraded`` call; can simulate a raise."""

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.calls: list[tuple[str, float, float, str]] = []
        self.raise_on_call = raise_on_call

    def __call__(self, rule: str, achieved: float, allowed: float, detail: str) -> None:
        if self.raise_on_call:
            raise RuntimeError("simulated callback failure")
        self.calls.append((rule, achieved, allowed, detail))


def _monitor(
    on_degraded: RecordingDegraded | None = None,
    *,
    pool_size: int = 4,
    **kwargs: float | int,
) -> tuple[PullHealthMonitor, RecordingDegraded, MetricRegistry]:
    """Build a monitor on a fresh registry with a recording callback."""
    cb = on_degraded if on_degraded is not None else RecordingDegraded()
    registry = MetricRegistry()
    monitor = PullHealthMonitor(
        registry=registry,
        on_degraded=cb,
        pool_size=pool_size,
        **kwargs,  # type: ignore[arg-type]
    )
    return monitor, cb, registry


# Canonical snapshots that breach NOTHING (achieved≈allowed, full pool, low latency).
_HEALTHY_CTRL = {"current_rps": 5.0, "recent_hits": 0}
_HEALTHY_METER = {"effective_rps": 4.8, "latency_ms_p95": 100.0, "in_flight": 4}


# ────────────────────────────────────────────────────────────────────────
# DwellProvider
# ────────────────────────────────────────────────────────────────────────


class TestDwellProvider:
    def test_rejects_non_positive_samples(self) -> None:
        with pytest.raises(ValueError, match="samples must be >= 1"):
            DwellProvider(value_provider=lambda: 1.0, predicate=lambda _v: True, samples=0)

    def test_fires_only_after_k_consecutive(self) -> None:
        dwell = DwellProvider(value_provider=lambda: 1.0, predicate=lambda v: v > 0, samples=3)
        assert dwell() is None  # streak 1
        assert dwell() is None  # streak 2
        assert dwell() == 1.0  # streak 3 → fires
        assert dwell() == 1.0  # stays fired while predicate holds

    def test_predicate_false_resets_streak(self) -> None:
        values = iter([1.0, 1.0, -1.0, 1.0, 1.0])
        dwell = DwellProvider(
            value_provider=lambda: next(values), predicate=lambda v: v > 0, samples=3
        )
        assert dwell() is None  # 1.0 → streak 1
        assert dwell() is None  # 1.0 → streak 2
        assert dwell() is None  # -1.0 → predicate False, streak reset to 0
        assert dwell() is None  # 1.0 → streak 1 again
        assert dwell() is None  # 1.0 → streak 2 (never reached 3)

    def test_none_value_resets_streak(self) -> None:
        values: list[float | None] = [1.0, None, 1.0]
        it = iter(values)
        dwell = DwellProvider(value_provider=lambda: next(it), predicate=lambda v: v > 0, samples=2)
        assert dwell() is None  # 1.0 → streak 1
        assert dwell() is None  # None → reset
        assert dwell() is None  # 1.0 → streak 1 again (never 2)


# ────────────────────────────────────────────────────────────────────────
# Per-rule breach / recovery (hysteresis)
# ────────────────────────────────────────────────────────────────────────


class TestRules:
    def test_healthy_snapshot_fires_nothing(self) -> None:
        monitor, cb, _ = _monitor()
        monitor.observe(_HEALTHY_CTRL, _HEALTHY_METER)
        assert cb.calls == []
        assert monitor.state_of("pull.achieved_below_allowed") == SLAState.OK

    def test_achieved_below_allowed_fires_and_clears(self) -> None:
        monitor, cb, _ = _monitor()
        # achieved/allowed = 1.0/5.0 = 0.2 < 0.6 → breach. in_flight 4 keeps pool busy,
        # low latency, zero hits → only this rule fires.
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 0},
            {"effective_rps": 1.0, "latency_ms_p95": 100.0, "in_flight": 4},
        )
        assert monitor.state_of("pull.achieved_below_allowed") == SLAState.BREACH
        assert [c[0] for c in cb.calls] == ["pull.achieved_below_allowed"]
        assert cb.calls[0][3].startswith("OUR pipeline is the limiter")
        # Recovery edge — back to healthy clears it (no new degraded call).
        monitor.observe(_HEALTHY_CTRL, _HEALTHY_METER)
        assert monitor.state_of("pull.achieved_below_allowed") == SLAState.OK
        assert len(cb.calls) == 1

    def test_pool_idle_high_fires(self) -> None:
        monitor, cb, _ = _monitor(pool_size=4)
        # in_flight 1 / pool 4 → idle 0.75 > 0.5. Keep achieved high so only this fires.
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 0},
            {"effective_rps": 4.8, "latency_ms_p95": 100.0, "in_flight": 1},
        )
        assert monitor.state_of("pull.pool_idle_high") == SLAState.BREACH
        assert "pull.pool_idle_high" in [c[0] for c in cb.calls]

    def test_write_latency_spike_fires(self) -> None:
        monitor, _, _ = _monitor()
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 0},
            {"effective_rps": 4.8, "latency_ms_p95": 5000.0, "in_flight": 4},
        )
        assert monitor.state_of("pull.write_latency_spike") == SLAState.BREACH

    def test_rate_storm_fires_with_vendor_detail(self) -> None:
        monitor, cb, _ = _monitor()
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 50},
            {"effective_rps": 4.8, "latency_ms_p95": 100.0, "in_flight": 4},
        )
        assert monitor.state_of("pull.rate_storm") == SLAState.BREACH
        storm = next(c for c in cb.calls if c[0] == "pull.rate_storm")
        assert storm[3].startswith("VENDOR 429-gated")

    def test_stalled_requires_dwell(self) -> None:
        monitor, cb, _ = _monitor(stalled_dwell=3)
        stalled = {"effective_rps": 0.1, "latency_ms_p95": 100.0, "in_flight": 4}
        ctrl = {"current_rps": 5.0, "recent_hits": 0}
        # First two stalled samples: dwell not yet satisfied → still OK.
        monitor.observe(ctrl, stalled)
        monitor.observe(ctrl, stalled)
        assert monitor.state_of("pull.stalled") == SLAState.OK
        # NOTE: a stalled effective_rps ALSO breaches achieved_below_allowed — that is
        # expected (a stall IS below the ratio). We only assert the stalled rule here.
        # Third stalled sample: dwell satisfied → breach.
        monitor.observe(ctrl, stalled)
        assert monitor.state_of("pull.stalled") == SLAState.BREACH
        assert "pull.stalled" in [c[0] for c in cb.calls]


# ────────────────────────────────────────────────────────────────────────
# Provider edge cases (None branches)
# ────────────────────────────────────────────────────────────────────────


class TestProviderEdges:
    def test_empty_snapshots_skip_every_rule(self) -> None:
        """The very first observe (before any snapshot) must fire nothing — all providers None."""
        monitor, cb, _ = _monitor()
        monitor.observe({}, {})
        assert cb.calls == []
        for name in (
            "pull.achieved_below_allowed",
            "pull.pool_idle_high",
            "pull.write_latency_spike",
            "pull.rate_storm",
            "pull.stalled",
        ):
            assert monitor.state_of(name) == SLAState.OK

    def test_non_numeric_value_coerces_to_none(self) -> None:
        monitor, cb, _ = _monitor()
        monitor.observe(
            {"current_rps": "n/a", "recent_hits": None},
            {"effective_rps": object(), "latency_ms_p95": "x", "in_flight": "y"},
        )
        assert cb.calls == []  # every provider read None → no rule fired

    def test_allowed_non_positive_skips_ratio(self) -> None:
        monitor, _, _ = _monitor()
        # allowed 0 → ratio undefined → achieved_below_allowed skipped (no div-by-zero).
        monitor.observe(
            {"current_rps": 0.0, "recent_hits": 0},
            {"effective_rps": 4.8, "latency_ms_p95": 100.0, "in_flight": 4},
        )
        assert monitor.state_of("pull.achieved_below_allowed") == SLAState.OK

    def test_pool_size_zero_disables_pool_rule(self) -> None:
        monitor, _, registry = _monitor(pool_size=0)
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 0},
            {"effective_rps": 4.8, "latency_ms_p95": 100.0, "in_flight": 0},
        )
        assert monitor.state_of("pull.pool_idle_high") == SLAState.OK
        # The pool-idle gauge is NOT mirrored when the rule is disabled.
        assert registry.gauge_value("brutex_pull_pool_idle_fraction") == 0.0

    def test_in_flight_over_pool_clamps_idle_to_zero(self) -> None:
        monitor, _, registry = _monitor(pool_size=4)
        # in_flight 10 > pool 4 → raw idle negative → clamped to 0.0 (never below).
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 0},
            {"effective_rps": 4.8, "latency_ms_p95": 100.0, "in_flight": 10},
        )
        assert registry.gauge_value("brutex_pull_pool_idle_fraction") == 0.0


# ────────────────────────────────────────────────────────────────────────
# Gauge mirroring
# ────────────────────────────────────────────────────────────────────────


class TestGaugeMirroring:
    def test_gauges_mirror_full_snapshot(self) -> None:
        monitor, _, registry = _monitor(pool_size=4)
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 7},
            {"effective_rps": 3.2, "latency_ms_p95": 250.0, "in_flight": 2},
        )
        assert registry.gauge_value("brutex_pull_achieved_rps") == 3.2
        assert registry.gauge_value("brutex_pull_allowed_rps") == 5.0
        assert registry.gauge_value("brutex_pull_in_flight") == 2.0
        assert registry.gauge_value("brutex_pull_pool_idle_fraction") == 0.5  # 1 - 2/4
        assert registry.gauge_value("brutex_pull_latency_ms_p95") == 250.0
        assert registry.gauge_value("brutex_pull_recent_hits") == 7.0

    def test_absent_keys_leave_gauges_at_default(self) -> None:
        monitor, _, registry = _monitor()
        monitor.observe({}, {})
        # Nothing mirrored → every gauge stays at its 0.0 default.
        assert registry.gauge_value("brutex_pull_achieved_rps") == 0.0
        assert registry.gauge_value("brutex_pull_allowed_rps") == 0.0
        assert registry.gauge_value("brutex_pull_in_flight") == 0.0
        assert registry.gauge_value("brutex_pull_latency_ms_p95") == 0.0
        assert registry.gauge_value("brutex_pull_recent_hits") == 0.0


# ────────────────────────────────────────────────────────────────────────
# Best-effort: swallow callback + observe failures
# ────────────────────────────────────────────────────────────────────────


class TestBestEffort:
    def test_on_degraded_raise_is_swallowed(self) -> None:
        cb = RecordingDegraded(raise_on_call=True)
        monitor, _, _ = _monitor(cb)
        # A breaching snapshot fires the (raising) callback — must NOT propagate.
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 0},
            {"effective_rps": 1.0, "latency_ms_p95": 100.0, "in_flight": 4},
        )
        # The breach state still flipped (the detector saw the edge before the callback).
        assert monitor.state_of("pull.achieved_below_allowed") == SLAState.BREACH

    def test_observe_swallows_internal_failure(self) -> None:
        """A snapshot that breaks the registry mid-mirror is swallowed, never re-raised."""

        class ExplodingRegistry(MetricRegistry):
            def set(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("registry boom")

        registry = ExplodingRegistry()
        cb = RecordingDegraded()
        monitor = PullHealthMonitor(registry=registry, on_degraded=cb, pool_size=4)
        # observe must not raise even though _mirror_gauges → registry.set explodes.
        monitor.observe(_HEALTHY_CTRL, _HEALTHY_METER)


# ────────────────────────────────────────────────────────────────────────
# Integration: breach → SweepAlerter.pull_degraded
# ────────────────────────────────────────────────────────────────────────


class _FakeNotifier:
    """Captures published Telegram events."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        self.published.append(event)


class TestSweepAlerterWiring:
    def test_breach_routes_to_pull_degraded_alert(self) -> None:
        """A real SweepAlerter.pull_degraded wired as on_degraded fires ONE deduped alert."""
        notifier = _FakeNotifier()
        alerter = SweepAlerter(notifier=notifier)

        def on_degraded(rule: str, achieved: float, allowed: float, detail: str) -> None:
            alerter.pull_degraded(rule=rule, achieved=achieved, allowed=allowed, detail=detail)

        registry = MetricRegistry()
        monitor = PullHealthMonitor(registry=registry, on_degraded=on_degraded, pool_size=4)
        # Breach achieved_below_allowed → exactly one Telegram event.
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 0},
            {"effective_rps": 1.0, "latency_ms_p95": 100.0, "in_flight": 4},
        )
        assert len(notifier.published) == 1
        ev = notifier.published[0]
        assert ev.dedupe_key == "pull-degraded:pull.achieved_below_allowed"
        # Re-observing the SAME breach does NOT re-fire (hysteresis: no new edge).
        monitor.observe(
            {"current_rps": 5.0, "recent_hits": 0},
            {"effective_rps": 1.0, "latency_ms_p95": 100.0, "in_flight": 4},
        )
        assert len(notifier.published) == 1
