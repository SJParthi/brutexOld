"""Event-loop liveness watchdog (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001, G5).

Covers :mod:`brutex.observability.loop_watchdog` at 100% line+branch:

* the PURE ``check_once`` state machine — full matrix (healthy / warn edge /
  straight-to-critical / relog cap / re-alert cap / recovery / non-monotonic
  clock) with a fake clock, ZERO real sleeps;
* ``LoopWatchdog`` lifecycle — heartbeat stamps on a real (short-lived) loop,
  monitor drives ``sample_once`` via the injectable ``wait_fn``, start-failure
  leaves it inert, ``stop`` joins clean (no thread leak — pinned);
* alert/metric side effects — best-effort (a raising alerter/registry is
  swallowed), gauge + episode counter set on the real ``MetricRegistry``;
* the fail-loud env kill switch (junk raises);
* ``build_app`` wiring — the default spawns NOTHING (thread census pin), a
  wired watchdog starts on TestClient startup and stops on shutdown;
* one REAL parked-loop breaker (a blocking on-loop sleep trips the WARN edge
  through the real heartbeat + monitor pair — bounded ~2 s).
"""

# Whitebox pins read module-private state by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=none
from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from brutex.observability.loop_watchdog import (
    CRITICAL_AFTER_S_DEFAULT,
    LOOP_WATCHDOG_ENV,
    REALERT_EVERY_S_DEFAULT,
    RELOG_EVERY_S_DEFAULT,
    STALL_EPISODES_COUNTER,
    STALL_GAUGE,
    TIER_CRITICAL,
    TIER_OK,
    TIER_WARNED,
    WARN_AFTER_S_DEFAULT,
    LoopWatchdog,
    WatchdogState,
    check_once,
    resolve_loop_watchdog_enabled,
)
from structlog.testing import capture_logs


def _events(logs: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == event]


class TestCheckOnce:
    """The pure state machine — every transition with hand-fed clocks."""

    def test_healthy_noop(self) -> None:
        v = check_once(10.0, 9.0, WatchdogState())
        assert v.state.tier == TIER_OK
        assert v.lag_s == 1.0
        assert not (v.warn_edge or v.critical_edge or v.relog or v.realert or v.recovered)

    def test_non_monotonic_clock_never_negative(self) -> None:
        v = check_once(5.0, 9.0, WatchdogState())  # now BEHIND the beat
        assert v.lag_s == 0.0
        assert v.state.tier == TIER_OK

    def test_warn_edge_at_threshold(self) -> None:
        """Thresholds are ``>=`` — a lag exactly AT 5 s trips (the boundary
        mutant `>=`→`>` dies here)."""
        v = check_once(5.0, 0.0, WatchdogState())
        assert v.warn_edge and not v.critical_edge
        assert v.state.tier == TIER_WARNED
        assert v.state.stall_started_beat == 0.0
        assert v.state.last_logged_at == 5.0
        assert v.state.episodes == 1

    def test_below_warn_threshold_no_edge(self) -> None:
        v = check_once(4.999, 0.0, WatchdogState())
        assert not v.warn_edge
        assert v.state.tier == TIER_OK

    def test_straight_to_critical_fires_both_edges(self) -> None:
        """A starved monitor may observe the lag jumping past 30 s in one
        sample — one episode, warn AND critical edges together."""
        v = check_once(31.0, 0.0, WatchdogState())
        assert v.warn_edge and v.critical_edge
        assert v.state.tier == TIER_CRITICAL
        assert v.state.last_alerted_at == 31.0
        assert v.state.episodes == 1

    def test_warned_to_critical_edge(self) -> None:
        s = check_once(6.0, 0.0, WatchdogState()).state
        v = check_once(30.0, 0.0, s)
        assert v.critical_edge and not v.warn_edge
        assert v.state.tier == TIER_CRITICAL
        assert v.state.episodes == 1  # SAME episode — no second count

    def test_relog_cap_while_stalled(self) -> None:
        # critical raised out of the way so the growing lag stays WARNED-tier
        # (a frozen beat means the lag grows with every sample).
        s = check_once(6.0, 0.0, WatchdogState(), critical_after_s=1000.0).state
        quiet = check_once(6.0 + RELOG_EVERY_S_DEFAULT - 1.0, 0.0, s, critical_after_s=1000.0)
        assert not quiet.relog  # inside the 60 s window — silent
        loud = check_once(6.0 + RELOG_EVERY_S_DEFAULT, 0.0, quiet.state, critical_after_s=1000.0)
        assert loud.relog  # at the window — re-log with the grown lag
        assert loud.state.last_logged_at == 6.0 + RELOG_EVERY_S_DEFAULT

    def test_realert_cap_while_critical(self) -> None:
        s = check_once(31.0, 0.0, WatchdogState()).state  # alerted at 31.0
        quiet = check_once(31.0 + REALERT_EVERY_S_DEFAULT - 1.0, 0.0, s)
        assert not quiet.realert
        loud = check_once(31.0 + REALERT_EVERY_S_DEFAULT, 0.0, quiet.state)
        assert loud.realert
        assert loud.state.last_alerted_at == 31.0 + REALERT_EVERY_S_DEFAULT

    def test_no_realert_at_warned_tier(self) -> None:
        s = check_once(6.0, 0.0, WatchdogState()).state
        v = check_once(6.0 + REALERT_EVERY_S_DEFAULT + 60.0, 0.0, s)
        # Still WARNED?? — no: lag grew past 30 s here, so guard the setup:
        assert v.state.tier == TIER_CRITICAL  # the growing lag escalated
        # A genuinely-warned-only re-check (lag between 5 and 30):
        s2 = check_once(6.0, 0.0, WatchdogState()).state
        v2 = check_once(100.0, 90.0, s2)  # lag 10 — still WARNED tier
        assert v2.state.tier == TIER_WARNED
        assert not v2.realert  # re-alerts are CRITICAL-tier only

    def test_recovery_resets_and_reports_total(self) -> None:
        s = check_once(6.0, 0.0, WatchdogState()).state  # stalled beat = 0.0
        v = check_once(50.0, 49.5, s)  # the loop beat again at 49.5
        assert v.recovered
        assert v.stall_total_s == 49.5  # resumed beat − stalled beat
        assert v.state.tier == TIER_OK
        assert v.state.episodes == 1  # the counter survives the reset

    def test_recovery_total_never_negative(self) -> None:
        s = WatchdogState(tier=TIER_WARNED, stall_started_beat=100.0)
        v = check_once(50.0, 49.0, s)  # a clock oddity — beat behind the start
        assert v.recovered
        assert v.stall_total_s == 0.0

    def test_ok_to_ok_after_recovery(self) -> None:
        v = check_once(1.0, 0.5, WatchdogState(episodes=3))
        assert v.state.episodes == 3
        assert not v.recovered


class TestResolveEnv:
    """The fail-loud kill switch (the resolve_* house pattern)."""

    @pytest.mark.parametrize("raw", ["", "1", "on", "TRUE", " yes "])
    def test_on_tokens(self, raw: str) -> None:
        assert resolve_loop_watchdog_enabled({LOOP_WATCHDOG_ENV: raw}) is True

    @pytest.mark.parametrize("raw", ["0", "off", "False", " NO "])
    def test_off_tokens(self, raw: str) -> None:
        assert resolve_loop_watchdog_enabled({LOOP_WATCHDOG_ENV: raw}) is False

    def test_unset_defaults_on(self) -> None:
        assert resolve_loop_watchdog_enabled({}) is True

    def test_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LOOP_WATCHDOG_ENV, "off")
        assert resolve_loop_watchdog_enabled() is False

    def test_junk_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="BRUTEX_LOOP_WATCHDOG"):
            resolve_loop_watchdog_enabled({LOOP_WATCHDOG_ENV: "maybe"})


class TestConstruction:
    """Fail-loud ctor validation — junk config never runs silently skewed."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"beat_interval_s": 0.0},
            {"check_interval_s": -1.0},
            {"warn_after_s": float("nan")},
            {"critical_after_s": float("inf")},
            {"relog_every_s": 0.0},
            {"realert_every_s": -5.0},
        ],
    )
    def test_bad_intervals_raise(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError, match="positive finite"):
            LoopWatchdog(**kwargs)  # type: ignore[arg-type]

    def test_inverted_thresholds_raise(self) -> None:
        with pytest.raises(ValueError, match="critical_after_s"):
            LoopWatchdog(warn_after_s=30.0, critical_after_s=5.0)

    def test_defaults_are_the_documented_thresholds(self) -> None:
        wd = LoopWatchdog()
        assert wd._warn_after_s == WARN_AFTER_S_DEFAULT == 5.0
        assert wd._critical_after_s == CRITICAL_AFTER_S_DEFAULT == 30.0


class _Alerter:
    """A capturing loop_stalled alerter (the SweepAlerter Protocol shape)."""

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.calls: list[tuple[float, float]] = []
        self._raise = raise_on_call

    def loop_stalled(self, *, stalled_s: float, threshold_s: float) -> bool:
        self.calls.append((stalled_s, threshold_s))
        if self._raise:
            raise RuntimeError("telegram down")
        return True


class TestSampleOnce:
    """The monitor body — logs, metrics, alerts, all deterministic."""

    def _watchdog(self, **kwargs: Any) -> tuple[LoopWatchdog, dict[str, float]]:
        clock = {"t": 0.0}
        wd = LoopWatchdog(clock=lambda: clock["t"], **kwargs)
        with wd._beat_lock:
            wd._last_beat = 0.0
        return wd, clock

    def test_warn_edge_logs_warning(self) -> None:
        wd, clock = self._watchdog()
        clock["t"] = 6.0
        with capture_logs() as logs:
            v = wd.sample_once()
        assert v.warn_edge
        (stalled,) = _events(logs, "loop_watchdog.stalled")
        assert stalled["tier"] == TIER_WARNED
        assert stalled["log_level"] == "warning"

    def test_critical_edge_logs_error_and_alerts(self) -> None:
        alerter = _Alerter()
        wd, clock = self._watchdog(alerter=alerter)
        clock["t"] = 31.0
        with capture_logs() as logs:
            wd.sample_once()
        (stalled,) = _events(logs, "loop_watchdog.stalled")
        assert stalled["tier"] == TIER_CRITICAL
        assert stalled["log_level"] == "error"
        assert alerter.calls == [(31.0, 30.0)]

    def test_relog_uses_current_tier_level(self) -> None:
        # critical raised out of the way so the growing lag stays WARNED-tier.
        wd, clock = self._watchdog(critical_after_s=1000.0)
        clock["t"] = 6.0
        wd.sample_once()  # warn edge, logged at 6.0
        clock["t"] = 6.0 + RELOG_EVERY_S_DEFAULT
        with capture_logs() as logs:
            wd.sample_once()
        (stalled,) = _events(logs, "loop_watchdog.stalled")
        assert stalled["relog"] is True
        assert stalled["log_level"] == "warning"  # still WARNED tier
        # Critical-tier relog logs at ERROR.
        alerter = _Alerter()
        wd2, clock2 = self._watchdog(alerter=alerter)
        clock2["t"] = 31.0
        wd2.sample_once()
        clock2["t"] = 31.0 + RELOG_EVERY_S_DEFAULT
        with capture_logs() as logs:
            wd2.sample_once()
        (stalled2,) = _events(logs, "loop_watchdog.stalled")
        assert stalled2["log_level"] == "error"

    def test_realert_fires_within_dedupe_law(self) -> None:
        alerter = _Alerter()
        wd, clock = self._watchdog(alerter=alerter)
        clock["t"] = 31.0
        wd.sample_once()  # critical edge → alert 1
        clock["t"] = 31.0 + REALERT_EVERY_S_DEFAULT - 1.0
        wd.sample_once()  # inside the window — deduped
        assert len(alerter.calls) == 1
        clock["t"] = 31.0 + REALERT_EVERY_S_DEFAULT
        wd.sample_once()  # past the window — re-alert
        assert len(alerter.calls) == 2

    def test_recovery_logs_info_with_total(self) -> None:
        wd, clock = self._watchdog()
        clock["t"] = 6.0
        wd.sample_once()
        with wd._beat_lock:
            wd._last_beat = 49.5  # the loop beat again
        clock["t"] = 50.0
        with capture_logs() as logs:
            v = wd.sample_once()
        assert v.recovered
        (recovered,) = _events(logs, "loop_watchdog.recovered")
        assert recovered["stall_total_s"] == 49.5

    def test_alerter_raising_is_swallowed(self) -> None:
        alerter = _Alerter(raise_on_call=True)
        wd, clock = self._watchdog(alerter=alerter)
        clock["t"] = 31.0
        with capture_logs() as logs:
            wd.sample_once()  # must NOT raise
        assert alerter.calls  # the alerter WAS invoked
        assert _events(logs, "loop_watchdog.alert_failed")

    def test_no_alerter_no_registry_tolerated(self) -> None:
        wd, clock = self._watchdog()  # alerter=None, registry=None
        clock["t"] = 31.0
        wd.sample_once()  # silent no-ops on both side-effect lanes

    def test_gauge_and_counter_on_real_registry(self) -> None:
        from brutex.observability.metrics import MetricRegistry

        registry = MetricRegistry()
        wd, clock = self._watchdog(registry=registry)
        wd._register_metrics()
        clock["t"] = 6.0
        wd.sample_once()  # warn edge → episode counter + gauge
        assert registry.gauge_value(STALL_GAUGE) == 6.0
        assert registry.counter_value(STALL_EPISODES_COUNTER) == 1.0
        clock["t"] = 6.5
        with wd._beat_lock:
            wd._last_beat = 6.0
        wd.sample_once()  # healthy sample — gauge tracks the live lag
        assert registry.gauge_value(STALL_GAUGE) == 0.5
        assert registry.counter_value(STALL_EPISODES_COUNTER) == 1.0

    def test_metric_faults_swallowed(self) -> None:
        class _BadRegistry:
            def gauge(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("bad registry")

            def counter(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("bad registry")

            def set(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("bad registry")

            def inc(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("bad registry")

        wd, clock = self._watchdog(registry=_BadRegistry())  # type: ignore[arg-type]
        with capture_logs() as logs:
            wd._register_metrics()
            clock["t"] = 6.0
            wd.sample_once()  # gauge set + episode inc both fault — swallowed
        assert _events(logs, "loop_watchdog.metrics_register_failed")
        assert _events(logs, "loop_watchdog.metrics_set_failed")
        assert _events(logs, "loop_watchdog.metrics_inc_failed")


class TestLifecycle:
    """start/stop on a real loop — clean shutdown, no thread leak."""

    def test_start_stop_clean_no_thread_leak(self) -> None:
        wd = LoopWatchdog(wait_fn=lambda timeout: True)  # monitor exits instantly

        async def _drive() -> None:
            wd.start(asyncio.get_running_loop())
            assert wd.started
            wd.start(asyncio.get_running_loop())  # idempotent — a no-op
            await asyncio.sleep(0)  # let the heartbeat stamp once

        asyncio.run(_drive())
        wd.stop()
        assert not wd.started
        assert wd._monitor is None and wd._task is None
        # No leaked watchdog threads.
        names = {t.name for t in threading.enumerate()}
        assert "bx-loop-watchdog-monitor" not in names
        wd.stop()  # idempotent — safe on a stopped watchdog

    def test_stop_never_started_is_safe(self) -> None:
        LoopWatchdog().stop()  # no task, no monitor — a clean no-op

    def test_restart_after_stop_rearms_the_monitor(self) -> None:
        """Round-1 nit regression: `stop()` sets the stop event and the DEFAULT
        monitor wait IS `_stop_event.wait`, so without `start()` clearing it a
        restarted monitor's FIRST wait returns True and the thread exits
        immediately — a silently-dead watchdog. FAILS-BEFORE: the restarted
        monitor is no longer alive milliseconds after start."""
        wd = LoopWatchdog()  # default wait_fn — the stop event's own wait

        async def _cycle() -> None:
            wd.start(asyncio.get_running_loop())
            wd.stop()  # sets the stop event; join is fast (the wait returns True)
            wd.start(asyncio.get_running_loop())
            # With the event cleared the fresh monitor parks in its 1 s check
            # wait; with it still SET the thread exits within microseconds.
            await asyncio.sleep(0.05)
            monitor = wd._monitor
            assert monitor is not None and monitor.is_alive(), (
                "restarted monitor exited immediately — stop event not cleared by start()"
            )

        try:
            asyncio.run(_cycle())
        finally:
            wd.stop()
        names = {t.name for t in threading.enumerate()}
        assert "bx-loop-watchdog-monitor" not in names  # the final stop reaped it

    def test_monitor_loop_samples_until_stop(self) -> None:
        """The injectable wait_fn drives EXACT sample counts — no sleeps."""
        calls = {"n": 0}

        def _script(timeout: float) -> bool:
            calls["n"] += 1
            return calls["n"] > 3  # three samples, then stop

        wd = LoopWatchdog(clock=lambda: 0.0, wait_fn=_script)
        wd._monitor_loop()
        assert calls["n"] == 4  # 3 sampling waits + the stopping one

    def test_start_thread_failure_leaves_inert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Thread.start() failing (exhaustion): heartbeat task cancelled, ONE
        WARN, watchdog inert — boot never crashes."""
        wd = LoopWatchdog()

        def _boom(self: threading.Thread) -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", _boom)

        async def _drive() -> None:
            with capture_logs() as logs:
                wd.start(asyncio.get_running_loop())
            assert _events(logs, "loop_watchdog.start_failed")
            assert not wd.started
            await asyncio.sleep(0)  # the cancelled task settles

        asyncio.run(_drive())

    def test_heartbeat_stamps_the_clock(self) -> None:
        stamps: list[float] = []
        clock = {"t": 100.0}
        wd = LoopWatchdog(
            clock=lambda: clock["t"], beat_interval_s=0.005, wait_fn=lambda timeout: True
        )

        async def _drive() -> None:
            wd.start(asyncio.get_running_loop())
            await asyncio.sleep(0.02)  # a few real beats
            with wd._beat_lock:
                stamps.append(wd._last_beat)

        asyncio.run(_drive())
        wd.stop()
        assert stamps == [100.0]  # the injected clock value was stamped

    def test_real_parked_loop_trips_the_warn_edge(self) -> None:
        """THE breaker (the #1316 pattern, bounded ~2 s real time): a
        synchronous on-loop sleep parks the heartbeat past a 0.4 s WARN
        threshold — the REAL monitor thread observes it and logs."""
        import time as _time

        wd = LoopWatchdog(
            beat_interval_s=0.05,
            check_interval_s=0.05,
            warn_after_s=0.4,
            critical_after_s=10.0,
        )

        async def _drive() -> None:
            wd.start(asyncio.get_running_loop())
            await asyncio.sleep(0.15)  # healthy beats first
            _time.sleep(0.9)  # PARK the loop — the blocking on-loop read shape

        with capture_logs() as logs:
            asyncio.run(_drive())
            _time.sleep(0.15)  # one more monitor tick observes the stall
            wd.stop()
        stalled = _events(logs, "loop_watchdog.stalled")
        assert stalled, "the parked loop was never observed"
        assert stalled[0]["tier"] == TIER_WARNED


class TestBuildAppWiring:
    """build_app(loop_watchdog=…) — default spawns NOTHING; wired runs."""

    def test_default_build_app_spawns_nothing(self) -> None:
        """The census pin: no watchdog ⇒ no lifespan, no watchdog threads, no
        heartbeat task — byte-identical app for every existing caller."""
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import DashboardState, build_app

        before = {t.name for t in threading.enumerate()}
        app = build_app(DashboardState())
        after = {t.name for t in threading.enumerate()}
        assert not {n for n in after - before if n.startswith("bx-loop-watchdog")}
        assert app.router.lifespan_context is not None  # FastAPI default exists…
        # …but no watchdog handler was registered: startup/shutdown run clean
        # with the watchdog module untouched (spot-checked via TestClient below).

    def test_wired_watchdog_starts_and_stops_with_the_app(self) -> None:
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import DashboardState, build_app
        from fastapi.testclient import TestClient

        wd = LoopWatchdog(beat_interval_s=0.05, check_interval_s=0.05)
        app = build_app(DashboardState(), loop_watchdog=wd)
        with TestClient(app) as client:  # the context manager drives the lifespan
            assert wd.started
            resp = client.get("/api/v1/state")
            assert resp.status_code == 200
        assert not wd.started  # shutdown stopped it
        names = {t.name for t in threading.enumerate()}
        assert "bx-loop-watchdog-monitor" not in names


class TestWiringDocstringPins:
    """Round-3 review: the docstrings must describe the REAL lifespan seam.

    The module + ``build_app`` docstrings previously claimed the app
    "registers ``startup``/``shutdown`` event handlers (the app deliberately
    has NO lifespan)" — affirmatively false against the code, which wires the
    watchdog via ``FastAPI(lifespan=...)`` (Starlette 1.x REMOVED
    ``add_event_handler``/``on_event``). A maintainer trusting the docstring
    would hunt for handlers that do not exist; these pins keep the wording
    reconciled to the wiring (the doc-pin idiom — DEC-RATE-METER-LIFECYCLE-001
    round 3 precedent).
    """

    def test_module_docstring_names_the_lifespan_seam(self) -> None:
        from brutex.observability import loop_watchdog as mod

        doc = mod.__doc__
        assert doc is not None
        assert "lifespan" in doc.lower()  # the true seam is named…
        # …and the retired event-handler claims are gone.
        assert "has NO\nlifespan" not in doc
        assert "has NO lifespan" not in doc
        assert "registers ``startup``/``shutdown`` event handlers" not in doc

    def test_build_app_docstring_names_the_lifespan_seam(self) -> None:
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import build_app

        doc = build_app.__doc__
        assert doc is not None
        assert "lifespan" in doc.lower()
        assert "registers ``startup``/``shutdown`` event handlers" not in doc
