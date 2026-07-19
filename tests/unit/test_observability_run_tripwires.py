"""Unit tests — :mod:`brutex.observability.run_tripwires` (R39 tripwire engine).

Every :class:`~brutex.observability.run_tripwires.TripwireKind` gets a trip
test, a no-trip BOUNDARY test at the exact threshold, and (for the trend
watches) dwell-edge tests — mutation-hardened with exact values. The D-12
set-once SUSPECT latch, the D-9 exact SweepProgress-counter reconciliation,
and the D-16 ``ru_maxrss`` platform normalization + probe-failure stand-down
are all pinned here. WP-1 modules are NOT imported — the fan-out is a local
stub through the injected ``on_trip`` callable (design WP-2 dependency note).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from typing import cast

import pytest
from brutex.engine.sweep_progress import STATE_DONE, STATE_RUNNING, SweepProgressSnapshot
from brutex.observability import run_tripwires
from brutex.observability.run_tripwires import (
    DEFAULT_ABSURD_VALUE_NAMES,
    ENV_ABSURD_PNL_PAISA,
    ENV_MEMORY_DWELL,
    ENV_MEMORY_SLOPE_BYTES,
    ENV_RECONCILE_INTERVAL_S,
    ENV_RSS_BUDGET_FRACTION,
    ENV_THROUGHPUT_DWELL,
    ENV_THROUGHPUT_FLOOR,
    EVENTS_RING_CAP,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    STAGE_PREREQUISITES,
    TripwireConfig,
    TripwireEvent,
    TripwireKind,
    TripwireMonitor,
    normalized_ru_maxrss_bytes,
)
from structlog.testing import capture_logs

#: D-6 traceability — test name → requirement/gap tokens (consumed by the
#: WP-6 mechanized matrix).
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_r39_count_reconciliation_trips_on_overflow": ("R39",),
    "test_r39_count_reconciliation_reads_progress_counters_exactly": ("R39",),
    "test_r39_suspect_latch_is_set_once": ("R39", "R16"),
    "test_r39_throughput_floor_dwell_edges": ("R39",),
    "test_r39_memory_budget_trip_and_boundary": ("R39",),
    "test_r39_memory_slope_dwell_edges": ("R39",),
    "test_r39_nan_metric_trips": ("R39",),
    "test_r39_absurd_value_boundary_is_strict": ("R39",),
    "test_r39_stage_invariant_matrix": ("R39", "R42"),
    "test_r39_ru_maxrss_platform_normalization": ("R39",),
    "test_r39_rss_probe_failure_disables_memory_watch_with_one_warn": ("R39",),
    "test_r39_fail_soft_observation_bodies": ("R39", "R16"),
}


class _Clock:
    """Deterministic injectable monotonic clock."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _TripSink:
    """Counting ``on_trip`` stub (local Protocol stand-in — no WP-1 import)."""

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.events: list[TripwireEvent] = []
        self.raise_on_call = raise_on_call

    def __call__(self, event: TripwireEvent) -> None:
        if self.raise_on_call:
            raise RuntimeError("sink down")
        self.events.append(event)


def _snap(
    *,
    enumerated: int = 100,
    evaluated: int = 0,
    survivors: int = 0,
    skipped: int = 0,
    state: str = STATE_RUNNING,
    started_at: float = 0.0,
    updated_at: float | None = None,
) -> SweepProgressSnapshot:
    """A real :class:`SweepProgressSnapshot` (D-9: the EXACT counter source)."""
    return SweepProgressSnapshot(
        state=state,
        enumerated=enumerated,
        evaluated=evaluated,
        survivors=survivors,
        skipped=skipped,
        current="",
        detail="",
        started_at=started_at,
        updated_at=started_at + 10.0 if updated_at is None else updated_at,
    )


def _config(**overrides: object) -> TripwireConfig:
    """A config that samples on every tick unless a test overrides it."""
    kwargs: dict[str, object] = {"reconcile_interval_s": 1e-9}
    kwargs.update(overrides)
    return TripwireConfig(**kwargs)  # type: ignore[arg-type]


def _monitor(
    *,
    config: TripwireConfig | None = None,
    total: int = 100,
    clock: _Clock | None = None,
    sink: _TripSink | None = None,
    ram: int | None = 2**30,
    probe: Callable[[], int] | None = None,
) -> tuple[TripwireMonitor, _TripSink, _Clock]:
    the_sink = _TripSink() if sink is None else sink
    the_clock = _Clock() if clock is None else clock
    monitor = TripwireMonitor(
        config=_config() if config is None else config,
        enumerated_total=total,
        clock=the_clock,
        on_trip=the_sink,
        ram_total_bytes=ram,
        rss_probe=probe,
    )
    return monitor, the_sink, the_clock


def _tick(
    monitor: TripwireMonitor, clock: _Clock, snapshot: SweepProgressSnapshot, **kw: int
) -> None:
    """Advance the clock past any interval and feed one snapshot."""
    clock.now += 1.0
    monitor.on_progress(snapshot, **kw)


# ── TripwireConfig ─────────────────────────────────────────────────────────


class TestTripwireConfig:
    def test_defaults_exact(self) -> None:
        cfg = TripwireConfig()
        assert cfg.reconcile_interval_s == 60.0
        assert cfg.throughput_floor_fraction == 0.2
        assert cfg.throughput_dwell == 5
        assert cfg.rss_budget_fraction == 0.85
        assert cfg.memory_slope_bytes_per_tuple == 8 * 2**20
        assert cfg.memory_dwell == 5
        assert cfg.absurd_pnl_paisa == 10**15
        assert cfg.absurd_value_names == DEFAULT_ABSURD_VALUE_NAMES
        assert DEFAULT_ABSURD_VALUE_NAMES == ("net_pnl_paisa", "max_dd_paisa")

    @pytest.mark.parametrize(
        ("field", "value", "fragment"),
        [
            ("reconcile_interval_s", 0.0, "reconcile_interval_s"),
            ("reconcile_interval_s", -1.0, "reconcile_interval_s"),
            ("throughput_floor_fraction", -0.1, "throughput_floor_fraction"),
            ("throughput_floor_fraction", 1.0, "throughput_floor_fraction"),
            ("throughput_dwell", 0, "throughput_dwell"),
            ("rss_budget_fraction", -0.01, "rss_budget_fraction"),
            ("rss_budget_fraction", 1.01, "rss_budget_fraction"),
            ("memory_slope_bytes_per_tuple", -1, "memory_slope_bytes_per_tuple"),
            ("memory_dwell", 0, "memory_dwell"),
            ("absurd_pnl_paisa", 0, "absurd_pnl_paisa"),
            ("absurd_pnl_paisa", -5, "absurd_pnl_paisa"),
        ],
    )
    def test_invalid_field_raises(self, field: str, value: object, fragment: str) -> None:
        with pytest.raises(ValueError, match=fragment):
            TripwireConfig(**{field: value})  # type: ignore[arg-type]

    def test_boundary_values_accepted(self) -> None:
        # 0 disables the two fraction watches; 1.0 rss fraction is legal.
        cfg = TripwireConfig(
            throughput_floor_fraction=0.0,
            rss_budget_fraction=1.0,
            memory_slope_bytes_per_tuple=0,
            throughput_dwell=1,
            memory_dwell=1,
            absurd_pnl_paisa=1,
        )
        assert cfg.throughput_floor_fraction == 0.0
        assert cfg.rss_budget_fraction == 1.0
        assert cfg.memory_slope_bytes_per_tuple == 0

    def test_from_env_all_defaults_when_unset(self) -> None:
        assert TripwireConfig.from_env({}) == TripwireConfig()

    def test_from_env_reads_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (
            ENV_RECONCILE_INTERVAL_S,
            ENV_THROUGHPUT_FLOOR,
            ENV_THROUGHPUT_DWELL,
            ENV_RSS_BUDGET_FRACTION,
            ENV_MEMORY_SLOPE_BYTES,
            ENV_MEMORY_DWELL,
            ENV_ABSURD_PNL_PAISA,
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv(ENV_THROUGHPUT_DWELL, "7")
        cfg = TripwireConfig.from_env()
        assert cfg.throughput_dwell == 7
        assert cfg.reconcile_interval_s == 60.0

    def test_from_env_every_knob_applies_exactly(self) -> None:
        cfg = TripwireConfig.from_env(
            {
                ENV_RECONCILE_INTERVAL_S: "30.5",
                ENV_THROUGHPUT_FLOOR: "0.5",
                ENV_THROUGHPUT_DWELL: "2",
                ENV_RSS_BUDGET_FRACTION: "0.9",
                ENV_MEMORY_SLOPE_BYTES: "1024",
                ENV_MEMORY_DWELL: "3",
                ENV_ABSURD_PNL_PAISA: "123456",
            }
        )
        assert cfg.reconcile_interval_s == 30.5
        assert cfg.throughput_floor_fraction == 0.5
        assert cfg.throughput_dwell == 2
        assert cfg.rss_budget_fraction == 0.9
        assert cfg.memory_slope_bytes_per_tuple == 1024
        assert cfg.memory_dwell == 3
        assert cfg.absurd_pnl_paisa == 123456

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            (ENV_RECONCILE_INTERVAL_S, "sixty"),
            (ENV_THROUGHPUT_FLOOR, "half"),
            (ENV_THROUGHPUT_DWELL, "5.5"),
            (ENV_RSS_BUDGET_FRACTION, "x"),
            (ENV_MEMORY_SLOPE_BYTES, "8MiB"),
            (ENV_MEMORY_DWELL, "many"),
            (ENV_ABSURD_PNL_PAISA, "1e15junk"),
        ],
    )
    def test_from_env_junk_fails_loud(self, key: str, value: str) -> None:
        with pytest.raises(ValueError, match=key):
            TripwireConfig.from_env({key: value})

    def test_from_env_out_of_range_fails_through_validation(self) -> None:
        with pytest.raises(ValueError, match="throughput_floor_fraction"):
            TripwireConfig.from_env({ENV_THROUGHPUT_FLOOR: "1.5"})

    def test_from_env_whitespace_only_is_unset(self) -> None:
        cfg = TripwireConfig.from_env({ENV_MEMORY_DWELL: "   "})
        assert cfg.memory_dwell == 5


# ── Constructor + read surface ─────────────────────────────────────────────


class TestMonitorConstruction:
    def test_negative_enumerated_total_rejected(self) -> None:
        with pytest.raises(ValueError, match="enumerated_total"):
            TripwireMonitor(
                config=_config(), enumerated_total=-1, on_trip=_TripSink(), ram_total_bytes=1
            )

    def test_ram_total_none_resolves_via_detector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_tripwires, "detect_total_ram_bytes", lambda: 42)
        monitor = TripwireMonitor(
            config=_config(), enumerated_total=1, on_trip=_TripSink(), ram_total_bytes=None
        )
        assert monitor.ram_total_bytes == 42

    def test_explicit_ram_total_wins(self) -> None:
        monitor, _, _ = _monitor(ram=7)
        assert monitor.ram_total_bytes == 7

    def test_initial_read_surface(self) -> None:
        monitor, _, _ = _monitor()
        assert monitor.suspect is False
        assert monitor.events == ()
        assert monitor.memory_probe_disabled is False


# ── Count reconciliation (D-9) ─────────────────────────────────────────────


class TestCountReconciliation:
    def _memory_off(self) -> TripwireConfig:
        return _config(rss_budget_fraction=0.0, memory_slope_bytes_per_tuple=0)

    def test_r39_count_reconciliation_reads_progress_counters_exactly(self) -> None:
        """A legal counter state never trips; every identity is checked exactly."""
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(evaluated=40, survivors=10, skipped=5), in_flight=60)
        _tick(monitor, clock, _snap(evaluated=100, survivors=10, skipped=100), in_flight=0)
        _tick(
            monitor,
            clock,
            _snap(evaluated=100, survivors=10, skipped=100, state=STATE_DONE),
            in_flight=0,
        )
        assert sink.events == []
        assert monitor.suspect is False

    def test_r39_count_reconciliation_trips_on_overflow(self) -> None:
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(evaluated=90), in_flight=11)
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.name == TripwireKind.COUNT_RECONCILIATION.value
        assert event.severity == SEVERITY_CRITICAL
        assert "evaluated 90 + in_flight 11 > enumerated 100" in event.detail
        assert event.values == {
            "enumerated": 100.0,
            "evaluated": 90.0,
            "skipped": 0.0,
            "survivors": 0.0,
            "in_flight": 11.0,
        }
        assert monitor.suspect is True

    def test_boundary_exact_full_house_is_clean(self) -> None:
        """evaluated + in_flight == enumerated is legal (strict > trips)."""
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(evaluated=90), in_flight=10)
        assert sink.events == []
        assert monitor.suspect is False

    def test_negative_in_flight_trips(self) -> None:
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(evaluated=1), in_flight=-1)
        assert len(sink.events) == 1
        assert "in_flight -1 < 0" in sink.events[0].detail

    def test_enumerated_drift_trips(self) -> None:
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(enumerated=99, evaluated=1))
        assert len(sink.events) == 1
        assert "snapshot enumerated 99 != armed total 100" in sink.events[0].detail

    def test_skipped_exceeding_evaluated_trips(self) -> None:
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(evaluated=5, skipped=6))
        assert len(sink.events) == 1
        assert "skipped 6 > evaluated 5" in sink.events[0].detail

    def test_survivors_exceeding_evaluated_trips(self) -> None:
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(evaluated=5, survivors=6))
        assert len(sink.events) == 1
        assert "survivors 6 > evaluated 5" in sink.events[0].detail

    def test_evaluated_going_backwards_trips(self) -> None:
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(evaluated=50))
        _tick(monitor, clock, _snap(evaluated=49))
        assert len(sink.events) == 1
        assert "evaluated went backwards: 49 < 50" in sink.events[0].detail

    def test_done_with_incomplete_count_trips(self) -> None:
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(evaluated=99, state=STATE_DONE))
        assert len(sink.events) == 1
        assert "run done but evaluated 99 != enumerated 100" in sink.events[0].detail

    def test_breach_edge_no_refire_until_recovery(self) -> None:
        monitor, sink, clock = _monitor(config=self._memory_off(), total=100)
        _tick(monitor, clock, _snap(evaluated=90), in_flight=11)
        _tick(monitor, clock, _snap(evaluated=90), in_flight=11)
        assert len(sink.events) == 1
        _tick(monitor, clock, _snap(evaluated=90), in_flight=0)  # recovers
        _tick(monitor, clock, _snap(evaluated=90), in_flight=20)  # re-breaches
        assert len(sink.events) == 2

    def test_interval_gate_skips_within_interval(self) -> None:
        clock = _Clock()
        monitor, sink, _ = _monitor(
            config=TripwireConfig(
                reconcile_interval_s=60.0,
                rss_budget_fraction=0.0,
                memory_slope_bytes_per_tuple=0,
            ),
            clock=clock,
            total=100,
        )
        clock.now = 0.0
        monitor.on_progress(_snap(evaluated=90), in_flight=11)  # first: samples
        assert len(sink.events) == 1
        clock.now = 59.9
        monitor.on_progress(_snap(evaluated=90), in_flight=50)  # gated: skipped
        assert len(sink.events) == 1
        clock.now = 60.0  # exactly the interval: samples again (>= law)
        monitor.on_progress(_snap(evaluated=90), in_flight=11)
        assert len(sink.events) == 1  # still breached — edge, no re-fire


# ── Throughput floor ───────────────────────────────────────────────────────


class TestThroughputFloor:
    def _config(self, dwell: int = 3) -> TripwireConfig:
        return _config(
            throughput_floor_fraction=0.2,
            throughput_dwell=dwell,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )

    @staticmethod
    def _rate_snap(rate: float, evaluated: int) -> SweepProgressSnapshot:
        # combos_per_sec == evaluated / elapsed — craft elapsed for the rate.
        return _snap(
            enumerated=10_000, evaluated=evaluated, started_at=0.0, updated_at=evaluated / rate
        )

    def test_r39_throughput_floor_dwell_edges(self) -> None:
        monitor, sink, clock = _monitor(config=self._config(dwell=3), total=10_000)
        _tick(monitor, clock, self._rate_snap(10.0, 100))  # peak = 10
        floor_rate = 1.0  # < 0.2 × 10
        _tick(monitor, clock, self._rate_snap(floor_rate, 200))  # below #1
        _tick(monitor, clock, self._rate_snap(floor_rate, 300))  # below #2
        assert sink.events == []
        _tick(monitor, clock, self._rate_snap(floor_rate, 400))  # below #3 → edge
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.name == TripwireKind.THROUGHPUT_FLOOR.value
        assert event.severity == SEVERITY_WARNING
        assert event.values["peak"] == 10.0
        assert event.values["floor"] == pytest.approx(2.0)
        _tick(monitor, clock, self._rate_snap(floor_rate, 500))  # still below: no re-fire
        assert len(sink.events) == 1

    def test_boundary_rate_exactly_at_floor_is_not_below(self) -> None:
        monitor, sink, clock = _monitor(config=self._config(dwell=1), total=10_000)
        _tick(monitor, clock, self._rate_snap(10.0, 100))  # peak = 10 → floor = 2.0
        _tick(monitor, clock, self._rate_snap(2.0, 200))  # exactly AT floor
        assert sink.events == []

    def test_recovery_resets_dwell(self) -> None:
        monitor, sink, clock = _monitor(config=self._config(dwell=2), total=10_000)
        _tick(monitor, clock, self._rate_snap(10.0, 100))
        _tick(monitor, clock, self._rate_snap(1.0, 200))  # below #1
        _tick(monitor, clock, self._rate_snap(9.0, 300))  # recovers → reset
        _tick(monitor, clock, self._rate_snap(1.0, 400))  # below #1 again
        assert sink.events == []
        _tick(monitor, clock, self._rate_snap(1.0, 500))  # below #2 → fires
        assert len(sink.events) == 1

    def test_disabled_by_zero_fraction(self) -> None:
        monitor, sink, clock = _monitor(
            config=_config(
                throughput_floor_fraction=0.0,
                rss_budget_fraction=0.0,
                memory_slope_bytes_per_tuple=0,
            ),
            total=10_000,
        )
        _tick(monitor, clock, self._rate_snap(10.0, 100))
        _tick(monitor, clock, self._rate_snap(0.001, 200))
        assert sink.events == []

    def test_zero_rate_before_any_peak_is_ignored(self) -> None:
        monitor, sink, clock = _monitor(config=self._config(dwell=1), total=10_000)
        _tick(monitor, clock, _snap(enumerated=10_000, evaluated=0))  # rate 0, no peak
        assert sink.events == []
        assert monitor.suspect is False


# ── Memory watches (D-16) ──────────────────────────────────────────────────


class _Probe:
    """Scripted RSS probe."""

    def __init__(self, readings: list[int] | None = None, *, fail: bool = False) -> None:
        self._readings = iter(readings or [])
        self._fail = fail

    def __call__(self) -> int:
        if self._fail:
            raise OSError("getrusage down")
        return next(self._readings)


class TestMemoryWatches:
    def _config(self, *, fraction: float = 0.5, slope: int = 100, dwell: int = 2) -> TripwireConfig:
        return _config(
            throughput_floor_fraction=0.0,
            rss_budget_fraction=fraction,
            memory_slope_bytes_per_tuple=slope,
            memory_dwell=dwell,
        )

    def test_r39_memory_budget_trip_and_boundary(self) -> None:
        # RAM 1000 × fraction 0.5 → budget 500. 500 is legal; 501 trips.
        probe = _Probe([500, 501])
        monitor, sink, clock = _monitor(
            config=self._config(slope=0), ram=1000, probe=probe, total=100
        )
        _tick(monitor, clock, _snap(evaluated=1))
        assert sink.events == []
        _tick(monitor, clock, _snap(evaluated=2))
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.name == TripwireKind.MEMORY_BUDGET.value
        assert event.severity == SEVERITY_CRITICAL
        assert event.values == {"rss_bytes": 501.0, "budget_bytes": 500.0}

    def test_budget_recovery_rearms(self) -> None:
        probe = _Probe([501, 400, 502])
        monitor, sink, clock = _monitor(
            config=self._config(slope=0), ram=1000, probe=probe, total=100
        )
        _tick(monitor, clock, _snap(evaluated=1))
        _tick(monitor, clock, _snap(evaluated=2))
        _tick(monitor, clock, _snap(evaluated=3))
        assert len(sink.events) == 2

    def test_r39_memory_slope_dwell_edges(self) -> None:
        # slope threshold 100 B/tuple, dwell 2; +200 B per +1 evaluated = 200 B/t.
        probe = _Probe([1000, 1200, 1400, 1600])
        monitor, sink, clock = _monitor(
            config=self._config(fraction=0.0, slope=100, dwell=2),
            ram=10**12,
            probe=probe,
            total=100,
        )
        _tick(monitor, clock, _snap(evaluated=1))  # first sample: no prev
        _tick(monitor, clock, _snap(evaluated=2))  # over #1
        assert sink.events == []
        _tick(monitor, clock, _snap(evaluated=3))  # over #2 → edge
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.name == TripwireKind.MEMORY_SLOPE.value
        assert event.severity == SEVERITY_WARNING
        assert event.values["slope_bytes_per_tuple"] == 200.0
        _tick(monitor, clock, _snap(evaluated=4))  # over #3: past dwell, no re-fire
        assert len(sink.events) == 1

    def test_slope_boundary_exactly_at_threshold_is_sane(self) -> None:
        # +100 B per tuple == threshold → strict > means NO breach.
        probe = _Probe([1000, 1100, 1200])
        monitor, sink, clock = _monitor(
            config=self._config(fraction=0.0, slope=100, dwell=1),
            ram=10**12,
            probe=probe,
            total=100,
        )
        _tick(monitor, clock, _snap(evaluated=1))
        _tick(monitor, clock, _snap(evaluated=2))
        _tick(monitor, clock, _snap(evaluated=3))
        assert sink.events == []

    def test_slope_recovery_resets_dwell_and_rearms(self) -> None:
        probe = _Probe([1000, 1200, 1200, 1400, 1600])
        monitor, sink, clock = _monitor(
            config=self._config(fraction=0.0, slope=100, dwell=2),
            ram=10**12,
            probe=probe,
            total=100,
        )
        _tick(monitor, clock, _snap(evaluated=1))
        _tick(monitor, clock, _snap(evaluated=2))  # over #1
        _tick(monitor, clock, _snap(evaluated=3))  # flat → reset
        _tick(monitor, clock, _snap(evaluated=4))  # over #1
        assert sink.events == []
        _tick(monitor, clock, _snap(evaluated=5))  # over #2 → fires
        assert len(sink.events) == 1

    def test_slope_skips_sample_when_evaluated_static(self) -> None:
        probe = _Probe([1000, 10**9, 10**9])
        monitor, sink, clock = _monitor(
            config=self._config(fraction=0.0, slope=100, dwell=1),
            ram=10**12,
            probe=probe,
            total=100,
        )
        _tick(monitor, clock, _snap(evaluated=5))
        _tick(monitor, clock, _snap(evaluated=5))  # no progress → no slope sample
        assert sink.events == []
        _tick(monitor, clock, _snap(evaluated=6))  # 0 growth from prev → clean
        assert sink.events == []

    def test_r39_rss_probe_failure_disables_memory_watch_with_one_warn(self) -> None:
        probe = _Probe(fail=True)
        monitor, sink, clock = _monitor(config=self._config(), ram=1000, probe=probe, total=100)
        with capture_logs() as logs:
            _tick(monitor, clock, _snap(evaluated=1))
            _tick(monitor, clock, _snap(evaluated=2))
        warns = [e for e in logs if e["event"] == "run_tripwires.rss_probe_failed"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "OSError"
        assert monitor.memory_probe_disabled is True
        assert sink.events == []
        assert monitor.suspect is False  # disabled, never a fabricated trip

    def test_ram_unknown_stands_down_budget_with_one_warn(self) -> None:
        probe = _Probe([10**12, 10**12])
        monitor, sink, clock = _monitor(config=self._config(slope=0), ram=0, probe=probe, total=100)
        with capture_logs() as logs:
            _tick(monitor, clock, _snap(evaluated=1))
            _tick(monitor, clock, _snap(evaluated=2))
        warns = [e for e in logs if e["event"] == "run_tripwires.ram_total_unknown"]
        assert len(warns) == 1
        assert sink.events == []

    def test_both_watches_disabled_never_probes(self) -> None:
        probe = _Probe(fail=True)  # would raise if ever called
        monitor, sink, clock = _monitor(
            config=self._config(fraction=0.0, slope=0), ram=1000, probe=probe, total=100
        )
        _tick(monitor, clock, _snap(evaluated=1))
        assert monitor.memory_probe_disabled is False
        assert sink.events == []

    def test_r39_ru_maxrss_platform_normalization(self) -> None:
        """D-16 unit pins: macOS reports bytes verbatim; Linux reports KiB."""
        assert normalized_ru_maxrss_bytes(ru_maxrss=1_000, platform="darwin") == 1_000
        assert normalized_ru_maxrss_bytes(ru_maxrss=1_000, platform="linux") == 1_024_000
        assert normalized_ru_maxrss_bytes(ru_maxrss=0, platform="linux") == 0

    def test_default_probe_returns_positive_bytes(self) -> None:
        monitor, sink, clock = _monitor(config=self._config(slope=0), ram=2**60, probe=None)
        _tick(monitor, clock, _snap(evaluated=1))
        assert monitor.memory_probe_disabled is False
        assert sink.events == []  # a real process is far below a 2**60-byte budget


# ── Tuple-summary screens ──────────────────────────────────────────────────


class TestTupleSummaryScreens:
    def test_r39_nan_metric_trips(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_tuple_summary({"sharpe": math.nan})
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.name == TripwireKind.NAN_METRIC.value
        assert event.severity == SEVERITY_CRITICAL
        assert "sharpe" in event.detail
        assert math.isnan(event.values["sharpe"])
        assert monitor.suspect is True

    @pytest.mark.parametrize("bad", [math.inf, -math.inf])
    def test_infinities_trip(self, bad: float) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_tuple_summary({"omega": bad})
        assert len(sink.events) == 1
        assert sink.events[0].name == TripwireKind.NAN_METRIC.value

    def test_finite_values_clean_and_rearm(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_tuple_summary({"sharpe": math.nan})
        monitor.on_tuple_summary({"sharpe": math.nan})  # still breached: no re-fire
        assert len(sink.events) == 1
        monitor.on_tuple_summary({"sharpe": 1.5})  # recovers
        monitor.on_tuple_summary({"sharpe": math.nan})  # re-fires
        assert len(sink.events) == 2

    def test_r39_absurd_value_boundary_is_strict(self) -> None:
        threshold = 10**15
        monitor, sink, _ = _monitor()
        monitor.on_tuple_summary({"net_pnl_paisa": float(threshold)})  # exactly AT: sane
        assert sink.events == []
        monitor.on_tuple_summary({"net_pnl_paisa": float(threshold + 2**10)})
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.name == TripwireKind.ABSURD_VALUE.value
        assert event.severity == SEVERITY_CRITICAL
        assert "net_pnl_paisa" in event.detail

    def test_absurd_negative_magnitude_trips(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_tuple_summary({"max_dd_paisa": -1e16})
        assert len(sink.events) == 1
        assert sink.events[0].name == TripwireKind.ABSURD_VALUE.value

    def test_absurd_screen_only_named_keys(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_tuple_summary({"unscreened_thing": 1e30})
        assert sink.events == []

    def test_absurd_recovery_rearms(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_tuple_summary({"net_pnl_paisa": 2e15})
        monitor.on_tuple_summary({"net_pnl_paisa": 1.0})
        monitor.on_tuple_summary({"net_pnl_paisa": 2e15})
        assert len(sink.events) == 2

    def test_empty_summary_is_noop(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_tuple_summary({})
        assert sink.events == []
        assert monitor.suspect is False


# ── Stage invariants ───────────────────────────────────────────────────────


class TestStageInvariants:
    def test_r39_stage_invariant_matrix(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_stage("preflight", stamps_present=frozenset())
        monitor.on_stage("sweep", stamps_present=frozenset())
        monitor.on_stage("stage3", stamps_present=frozenset({"stage2"}))
        monitor.on_stage("cascade", stamps_present=frozenset({"stage3"}))
        monitor.on_stage(
            "artifacts", stamps_present=frozenset({"cascade_manifest", "cascade_report"})
        )
        assert sink.events == []
        monitor.on_stage("cascade", stamps_present=frozenset())
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.name == TripwireKind.STAGE_INVARIANT.value
        assert event.severity == SEVERITY_CRITICAL
        assert "'cascade'" in event.detail
        assert "['stage3']" in event.detail
        assert event.values == {"missing": 1.0}

    def test_partial_stamps_trip_names_all_missing(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_stage("artifacts", stamps_present=frozenset({"cascade_manifest"}))
        assert len(sink.events) == 1
        assert "['cascade_report']" in sink.events[0].detail

    def test_unknown_stage_is_tolerated(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_stage("brand_new_link", stamps_present=frozenset())
        assert sink.events == []
        assert monitor.suspect is False

    def test_stage_edge_and_rearm(self) -> None:
        monitor, sink, _ = _monitor()
        monitor.on_stage("stage3", stamps_present=frozenset())
        monitor.on_stage("stage3", stamps_present=frozenset())
        assert len(sink.events) == 1
        monitor.on_stage("stage3", stamps_present=frozenset({"stage2"}))  # recovers
        monitor.on_stage("stage3", stamps_present=frozenset())
        assert len(sink.events) == 2

    def test_prerequisite_map_is_exact(self) -> None:
        assert dict(STAGE_PREREQUISITES) == {
            "preflight": frozenset(),
            "sweep": frozenset(),
            "stage3": frozenset({"stage2"}),
            "cascade": frozenset({"stage3"}),
            "artifacts": frozenset({"cascade_manifest", "cascade_report"}),
        }


# ── SUSPECT latch (D-12) + fan-out ─────────────────────────────────────────


class TestSuspectLatch:
    def test_r39_suspect_latch_is_set_once(self) -> None:
        """D-12: plain set-once bool; each EVENT fans out once via on_trip."""
        monitor, sink, _ = _monitor()
        before = monitor.suspect
        assert before is False
        monitor.on_tuple_summary({"sharpe": math.nan})
        after_first = monitor.suspect
        assert after_first is True
        assert len(sink.events) == 1
        # A second, distinct tripwire records + fans out, latch stays True.
        monitor.on_stage("cascade", stamps_present=frozenset())
        assert monitor.suspect is True
        assert len(sink.events) == 2
        # A clean observation NEVER clears the latch (set-once — no unlatch).
        monitor.on_tuple_summary({"sharpe": 1.0})
        monitor.on_stage("cascade", stamps_present=frozenset({"stage3"}))
        assert monitor.suspect is True

    def test_latch_set_even_when_sink_raises(self) -> None:
        sink = _TripSink(raise_on_call=True)
        monitor, _, _ = _monitor(sink=sink)
        with capture_logs() as logs:
            monitor.on_tuple_summary({"sharpe": math.nan})
        assert monitor.suspect is True
        assert len(monitor.events) == 1  # ring kept despite the dead sink
        warns = [e for e in logs if e["event"] == "run_tripwires.on_trip_failed"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "RuntimeError"

    def test_trip_emits_structlog_error(self) -> None:
        monitor, _, _ = _monitor()
        with capture_logs() as logs:
            monitor.on_tuple_summary({"sharpe": math.nan})
        errors = [e for e in logs if e["event"] == "run_tripwires.tripped"]
        assert len(errors) == 1
        assert errors[0]["log_level"] == "error"
        assert errors[0]["name"] == "nan_metric"
        assert errors[0]["severity"] == SEVERITY_CRITICAL

    def test_event_ring_caps_at_64(self) -> None:
        monitor, sink, _ = _monitor()
        for i in range(EVENTS_RING_CAP + 10):
            monitor.on_tuple_summary({f"m{i}": math.nan})
        assert len(sink.events) == EVENTS_RING_CAP + 10  # fan-out sees every event
        assert len(monitor.events) == EVENTS_RING_CAP  # ring is bounded
        # Oldest first; the earliest 10 rolled off.
        assert monitor.events[0].detail.startswith("non-finite metric-summary value: m10")

    def test_event_ts_is_wall_clock_observability_only(self) -> None:
        import time as _time

        before = _time.time()
        monitor, sink, _ = _monitor()
        monitor.on_tuple_summary({"sharpe": math.nan})
        after = _time.time()
        assert before <= sink.events[0].ts_utc <= after


# ── Fail-soft bodies (I-3) ─────────────────────────────────────────────────


class _ExplodingSnapshot:
    """Duck snapshot whose counter access raises — the fail-soft probe."""

    @property
    def enumerated(self) -> int:
        raise RuntimeError("torn snapshot")


class _ExplodingMapping(Mapping[str, float]):
    def __getitem__(self, key: str) -> float:
        raise RuntimeError("bad mapping")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("bad mapping")

    def __len__(self) -> int:
        return 1


class TestFailSoft:
    def test_r39_fail_soft_observation_bodies(self) -> None:
        """A broken input NEVER raises into the sweep — one WARN per failure."""
        monitor, sink, clock = _monitor()
        with capture_logs() as logs:
            clock.now += 1.0
            monitor.on_progress(cast("SweepProgressSnapshot", _ExplodingSnapshot()), in_flight=0)
            monitor.on_tuple_summary(_ExplodingMapping())
            monitor.on_stage("cascade", stamps_present=cast("frozenset[str]", object()))
        warns = [e for e in logs if e["event"] == "run_tripwires.observe_failed"]
        assert [w["method"] for w in warns] == ["on_progress", "on_tuple_summary", "on_stage"]
        assert sink.events == []
        assert monitor.suspect is False

    def test_junk_summary_value_fails_soft(self) -> None:
        monitor, sink, _ = _monitor()
        with capture_logs() as logs:
            monitor.on_tuple_summary(cast("Mapping[str, float]", {"sharpe": "junk"}))
        warns = [e for e in logs if e["event"] == "run_tripwires.observe_failed"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "ValueError"
        assert sink.events == []

    def test_warn_helper_swallows_a_broken_logger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _BoomLogger:
            def warning(self, *a: object, **k: object) -> None:
                raise RuntimeError("logger down")

            def error(self, *a: object, **k: object) -> None:
                raise RuntimeError("logger down")

        monitor, sink, _ = _monitor()
        monkeypatch.setattr(run_tripwires, "_LOG", _BoomLogger())
        monitor.on_tuple_summary({"sharpe": math.nan})  # ERROR log raises → warn path
        assert monitor.suspect is True  # latch survived a fully dead logger
        assert len(monitor.events) == 1
        assert sink.events == []  # fan-out was aborted by the logger raise…
        monitor.on_stage("cascade", stamps_present=frozenset())  # …but the run continues
        assert len(monitor.events) == 2


class TestEventShape:
    def test_tripwire_event_fields_exact(self) -> None:
        event = TripwireEvent(
            ts_utc=1.5, name="nan_metric", severity=SEVERITY_WARNING, detail="d", values={"x": 1.0}
        )
        assert (event.ts_utc, event.name, event.severity, event.detail, event.values) == (
            1.5,
            "nan_metric",
            "warning",
            "d",
            {"x": 1.0},
        )

    def test_kind_vocabulary_exact(self) -> None:
        assert {k.value for k in TripwireKind} == {
            "count_reconciliation",
            "nan_metric",
            "absurd_value",
            "throughput_floor",
            "memory_budget",
            "memory_slope",
            "stage_invariant",
        }

    def test_severity_vocabulary_exact(self) -> None:
        assert SEVERITY_WARNING == "warning"
        assert SEVERITY_CRITICAL == "critical"


# ────────────────────────────────────────────────────────────────────────
# DEC-RUST-SWEEP-OPS-001 — flag-ON routing parity (wheel-free fake crate)
# ────────────────────────────────────────────────────────────────────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the D-9 count-reconciliation fold.

    Wheel-free: the shared behavior-correct FAKE crate is injected by
    patching the facade's ``importlib`` binding. Flag ON must fire the
    SAME events (detail bytes included) as the flag-OFF Python fold that
    every OTHER test in this file drives.
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        crate = build_fake_sweep_ops_crate(version="fake")
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    def _fire(self) -> tuple[bool, list[str]]:
        monitor, sink, clock = _monitor(total=90)
        _tick(monitor, clock, _snap(enumerated=100, evaluated=150, survivors=200))
        return (monitor.suspect, [event.detail for event in sink.events])

    def test_flag_on_fires_identical_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        off = self._fire()
        assert off[0] is True
        assert "enumerated 100 != armed total 90" in off[1][0]
        self._arm_fake_crate(monkeypatch)
        try:
            assert self._fire() == off
        finally:
            self._reset_facade()

    def test_flag_on_clean_counts_fire_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._arm_fake_crate(monkeypatch)
        try:
            monitor, sink, clock = _monitor(total=100)
            _tick(monitor, clock, _snap(evaluated=50, survivors=5, skipped=10))
            assert monitor.suspect is False
            assert sink.events == []
        finally:
            self._reset_facade()
