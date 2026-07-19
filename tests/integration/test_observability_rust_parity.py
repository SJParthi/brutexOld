"""ON-arm integration parity for the observability Rust lane (DEC-RUST-OBSERVABILITY-001).

Drives the FOUR REAL public entry points — ``loop_watchdog.check_once``,
``Watchdog.check_once``, ``SLADetector.check_once`` and
``validate_stage_stamp``/``read_stage_stamp`` — with the flag ON, the REAL
built wheel installed, and ``remaining=0`` (post-burn-in steady state), while
the Python ORACLE laws are monkeypatched to BOMBS: the engagement spy. A
correct result with the oracle bombed proves the CRATE served the decision
(never the oracle); the ``observability_rust.engaged`` INFO is the log-side
witness. Plus the real-wheel burn-in lifecycle proof (``burn_in_complete``).

Wheel-GATED: the whole module skips cleanly when ``brutex_observability`` is
not importable (main CI never installs it — the LAKE posture; the dedicated
``observability-rust-parity.yml`` lane is where this file has teeth).
"""

from __future__ import annotations

import itertools
import json
import types
from typing import TYPE_CHECKING, Any, NoReturn

import pytest
from brutex.engine.sweep_progress import STATE_DONE, STATE_RUNNING, SweepProgressSnapshot
from brutex.observability import (
    loop_watchdog,
    observability_rust,
    run_tripwires,
    sla,
    stage_stamp,
    watchdog,
)
from brutex.observability.loop_watchdog import WatchdogState
from brutex.observability.sla import SLADetector, SLAEvent, SLAPredicate, SLARule, SLAState
from brutex.observability.stage_stamp import (
    StageStampError,
    StampVerdict,
    read_stage_stamp,
    stamp_path,
    validate_stage_stamp,
    write_stage_stamp,
)
from brutex.observability.watchdog import Watchdog, WorkerEvent, WorkerState
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytest.importorskip("brutex_observability")


def _oracle_bomb(*args: object, **kwargs: object) -> NoReturn:
    """The engagement spy — a routed call must never consult the oracle."""
    raise AssertionError("the flag-ON steady-state path consulted the Python oracle")


def _snapshot(
    *,
    enumerated: int = 100,
    evaluated: int = 0,
    survivors: int = 0,
    skipped: int = 0,
    state: str = STATE_RUNNING,
    started_at: float = 0.0,
    updated_at: float | None = None,
) -> SweepProgressSnapshot:
    """A real :class:`SweepProgressSnapshot` for the tripwire entry pins."""
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


def _rate_snapshot(rate: float, evaluated: int) -> SweepProgressSnapshot:
    """A snapshot whose ``combos_per_sec`` reads exactly *rate*."""
    return _snapshot(
        enumerated=10_000, evaluated=evaluated, started_at=0.0, updated_at=evaluated / rate
    )


@pytest.fixture(autouse=True)
def _flag_on_steady(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Flag ON + post-burn-in steady state around every test; reset after."""
    monkeypatch.setenv(observability_rust.OBSERVABILITY_RUST_ENV, "1")
    observability_rust.reset_observability_rust_for_tests(remaining=0)
    yield
    observability_rust.reset_observability_rust_for_tests()


class TestLoopWatchdogEntry:
    def test_warn_edge_served_by_crate_with_engaged_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        with capture_logs() as logs:
            verdict = loop_watchdog.check_once(10.0, 0.0, WatchdogState())
        assert verdict.warn_edge is True
        assert verdict.critical_edge is False
        assert verdict.lag_s == 10.0
        assert verdict.state.tier == loop_watchdog.TIER_WARNED
        assert verdict.state.stall_started_beat == 0.0
        assert verdict.state.last_logged_at == 10.0
        assert verdict.state.episodes == 1
        engaged = [e for e in logs if e["event"] == "observability_rust.engaged"]
        assert len(engaged) == 1

    def test_ok_to_ok_recomposes_equal_but_not_identical_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The documented flag-ON envelope: EQUAL frozen dataclasses, not the
        # oracle's object identity (flag-OFF preserves identity instead).
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        state = WatchdogState()
        verdict = loop_watchdog.check_once(0.0, 0.0, state)
        assert verdict.state == state
        assert verdict.state is not state
        assert verdict.lag_s == 0.0
        assert verdict.recovered is False

    def test_recovery_totals_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        stalled = WatchdogState(
            tier=loop_watchdog.TIER_CRITICAL,
            stall_started_beat=5.0,
            last_logged_at=40.0,
            last_alerted_at=40.0,
            episodes=3,
        )
        verdict = loop_watchdog.check_once(50.0, 49.0, stalled)
        assert verdict.recovered is True
        assert verdict.stall_total_s == 44.0  # last_beat − stall_started_beat
        assert verdict.state.tier == loop_watchdog.TIER_OK
        assert verdict.state.episodes == 3  # carried across recovery


class TestWatchdogEntry:
    def test_hung_then_recovery_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(watchdog, "_worker_decide_python", _oracle_bomb)
        times = [0.0, 20.0, 21.0, 22.0]
        events: list[WorkerEvent] = []
        dog = Watchdog(
            timeout_seconds=15.0,
            on_hung=events.append,
            on_recovery=events.append,
            clock=lambda: times.pop(0),
        )
        dog.register("w")  # t = 0.0
        hung = dog.check_once()  # t = 20.0 → elapsed 20 > 15 → HUNG edge
        assert [e.new_state for e in hung] == [WorkerState.HUNG]
        assert hung[0].last_beat_seconds_ago == 20.0
        assert dog.state_of("w") is WorkerState.HUNG
        dog.beat("w")  # t = 21.0
        recovered = dog.check_once()  # t = 22.0 → elapsed 1 → recovery edge
        assert [e.new_state for e in recovered] == [WorkerState.HEALTHY]
        assert events == [*hung, *recovered]

    def test_exactly_at_timeout_is_not_hung(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(watchdog, "_worker_decide_python", _oracle_bomb)
        times = [0.0, 15.0]
        dog = Watchdog(
            timeout_seconds=15.0,
            on_hung=_oracle_bomb,
            on_recovery=_oracle_bomb,
            clock=lambda: times.pop(0),
        )
        dog.register("w")
        assert dog.check_once() == ()  # strict `>` — the boundary tie is healthy
        assert dog.state_of("w") is WorkerState.HEALTHY


class TestSlaEntry:
    def test_breach_then_recovery_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sla, "_sla_decide_python", _oracle_bomb)
        values = [150.0, 50.0]
        events: list[SLAEvent] = []
        detector = SLADetector(
            rules=(
                SLARule(
                    name="rate",
                    value_provider=lambda: values.pop(0),
                    predicate=SLAPredicate.GREATER_THAN,
                    threshold=100.0,
                ),
            ),
            on_breach=events.append,
            on_recovery=events.append,
        )
        breach = detector.check_once()
        assert [e.new_state for e in breach] == [SLAState.BREACH]
        assert breach[0].observed_value == 150.0  # the ORIGINAL value object
        assert detector.state_of("rate") is SLAState.BREACH
        recovery = detector.check_once()
        assert [e.new_state for e in recovery] == [SLAState.OK]
        assert events == [*breach, *recovery]


class TestStampEntry:
    @staticmethod
    def _bomb_oracles(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stage_stamp, "_stamp_shape_index_python", _oracle_bomb)
        monkeypatch.setattr(stage_stamp, "_stamp_verdict_python", _oracle_bomb)

    def test_validate_ok_and_hash_mismatch_served_by_crate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._bomb_oracles(monkeypatch)
        artifact = tmp_path / "artifact.bin"
        artifact.write_bytes(b"payload")
        write_stage_stamp(
            artifact,
            stage="cascade_manifest",
            run_id="r1",
            extra={"data_snapshot_hash": "snap"},
        )
        verdict, stamp = validate_stage_stamp(
            artifact,
            expected_stage="cascade_manifest",
            expected_run_id="r1",
            expected_snapshot_hash="snap",
        )
        assert verdict is StampVerdict.OK  # the enum SINGLETON identity law
        assert stamp is not None and stamp.run_id == "r1"
        artifact.write_bytes(b"tampered")  # torn artifact → recompute mismatch
        verdict, stamp = validate_stage_stamp(artifact, expected_stage="cascade_manifest")
        assert verdict is StampVerdict.HASH_MISMATCH
        assert stamp is not None  # forensics stamp returned

    def test_malformed_stamp_message_minted_from_crate_index(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._bomb_oracles(monkeypatch)
        artifact = tmp_path / "artifact.bin"
        artifact.write_bytes(b"payload")
        stamp_path(artifact).write_text(
            json.dumps(
                {
                    "stage": 5,
                    "run_id": "r1",
                    "content_hash": "h",
                    "produced_at_utc": 1.0,
                    "extra": {},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(StageStampError) as excinfo:
            read_stage_stamp(artifact)
        assert "field 'stage' must be a str, got 5" in str(excinfo.value)
        # validate folds the SAME crate-served reject into MALFORMED.
        verdict, stamp = validate_stage_stamp(artifact, expected_stage="cascade_manifest")
        assert verdict is StampVerdict.MALFORMED
        assert stamp is None


class TestOverflowContainmentEntry:
    def test_u64_max_episodes_degrades_to_oracle_through_public_check_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The panic-containment fix at the PUBLIC surface: a warn edge at
        # episodes == u64::MAX takes the crate's checked_add ValueError
        # refusal ("episodes counter overflow") → the degrade set → the
        # ORACLE serves the unbounded-int truth — pre-fix this exact call
        # propagated the wheel's per-extension PanicException (a
        # BaseException NEITHER name-based belt home could resolve) out of
        # the monitor tick. The oracle is deliberately NOT bombed here —
        # the degrade arm legitimately consults it.
        with capture_logs() as logs:
            verdict = loop_watchdog.check_once(10.0, 0.0, WatchdogState(episodes=2**64 - 1))
        assert verdict.warn_edge is True
        assert verdict.state.episodes == 2**64  # Python ints are unbounded
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "loop_error"
        assert "episodes counter overflow" in warns[0]["error_detail"]
        assert observability_rust.rust_observability_disabled() is False
        # NOT latched: the very next in-domain call is crate-served again.
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        assert loop_watchdog.check_once(0.0, 0.0, WatchdogState()).lag_s == 0.0


class TestBurnInLifecycleOnRealWheel:
    def test_burn_in_completes_then_steady(self, monkeypatch: pytest.MonkeyPatch) -> None:
        observability_rust.reset_observability_rust_for_tests(remaining=1)
        with capture_logs() as logs:
            verdict = loop_watchdog.check_once(0.0, 0.0, WatchdogState())
        assert verdict.lag_s == 0.0
        done = [e for e in logs if e["event"] == "observability_rust.burn_in_complete"]
        assert [e["law"] for e in done] == ["loop"]
        assert observability_rust.rust_observability_disabled() is False
        # Steady thereafter: the oracle is out of the path entirely.
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        assert loop_watchdog.check_once(0.0, 0.0, WatchdogState()).lag_s == 0.0


_TW_ORACLE_NAMES: tuple[str, ...] = (
    "_reconcile_decide_python",
    "_throughput_decide_python",
    "_memory_budget_decide_python",
    "_memory_slope_decide_python",
    "_summary_classify_python",
    "_stage_missing_python",
    "_ru_maxrss_python",
)


def _bomb_all_tripwire_oracles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bomb ALL SEVEN seamed run_tripwires oracles — the strongest spy: any
    event a bombed-oracle monitor still produces was crate-decided."""
    for name in _TW_ORACLE_NAMES:
        monkeypatch.setattr(run_tripwires, name, _oracle_bomb)


def _event_views(
    events: list[run_tripwires.TripwireEvent],
) -> list[tuple[str, str, str, dict[str, float]]]:
    """The ts-free projection of an event stream (ts_utc is wall-clock)."""
    return [(e.name, e.severity, e.detail, e.values) for e in events]


class TestTripwiresEntry:
    """The full flag-ON tripwire entry-point parity matrix
    (DEC-RUST-OBSERVABILITY-TRIPWIRES-002 S5): a REAL ``TripwireMonitor``
    driven at ``remaining=0`` against the REAL 0.2.0 wheel with the seamed
    run_tripwires oracles BOMBED — a byte-exact event detail (or a pinned
    ABSENCE of events at a legal boundary) proves the crate served every
    trip / boundary / recovery / dwell / re-arm decision."""

    @staticmethod
    def _monitor(
        config: run_tripwires.TripwireConfig,
        total: int = 100,
        *,
        ram: int = 2**30,
        probe: Any = None,
    ) -> tuple[run_tripwires.TripwireMonitor, list[run_tripwires.TripwireEvent]]:
        events: list[run_tripwires.TripwireEvent] = []
        counter = itertools.count(1)
        monitor = run_tripwires.TripwireMonitor(
            config=config,
            enumerated_total=total,
            clock=lambda: float(next(counter)),
            on_trip=events.append,
            ram_total_bytes=ram,
            rss_probe=(lambda: 1) if probe is None else probe,
        )
        return monitor, events

    def test_reconcile_trip_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_tripwires, "_reconcile_decide_python", _oracle_bomb)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config)
        monitor.on_progress(_snapshot(evaluated=90), in_flight=11)
        assert [e.detail for e in events] == ["evaluated 90 + in_flight 11 > enumerated 100"]
        assert monitor.suspect is True

    def test_throughput_fire_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_tripwires, "_throughput_decide_python", _oracle_bomb)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.2,
            throughput_dwell=3,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config, total=10_000)
        monitor.on_progress(_rate_snapshot(10.0, 100))
        monitor.on_progress(_rate_snapshot(1.0, 200))
        monitor.on_progress(_rate_snapshot(1.0, 300))
        monitor.on_progress(_rate_snapshot(1.0, 400))
        assert [e.detail for e in events] == [
            "throughput 1.000/s below floor 2.000/s (peak 10.000/s × 0.2) for 3 consecutive samples"
        ]

    def test_memory_budget_trip_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_tripwires, "_memory_budget_decide_python", _oracle_bomb)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.5,
            memory_slope_bytes_per_tuple=0,
        )
        events: list[run_tripwires.TripwireEvent] = []
        monitor = run_tripwires.TripwireMonitor(
            config=config,
            enumerated_total=100,
            clock=itertools.count(1).__next__,
            on_trip=events.append,
            ram_total_bytes=1000,
            rss_probe=lambda: 501,
        )
        monitor.on_progress(_snapshot(evaluated=1))
        assert [e.detail for e in events] == ["RSS 501 bytes exceeds budget 500 bytes (0.5 × 1000)"]

    def test_memory_slope_fire_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_tripwires, "_memory_slope_decide_python", _oracle_bomb)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=100,
            memory_dwell=2,
        )
        events: list[run_tripwires.TripwireEvent] = []
        readings = iter([1000, 1200, 1400])
        monitor = run_tripwires.TripwireMonitor(
            config=config,
            enumerated_total=100,
            clock=itertools.count(1).__next__,
            on_trip=events.append,
            ram_total_bytes=10**12,
            rss_probe=lambda: next(readings),
        )
        monitor.on_progress(_snapshot(evaluated=1))
        monitor.on_progress(_snapshot(evaluated=2))
        monitor.on_progress(_snapshot(evaluated=3))
        assert [e.detail for e in events] == [
            "RSS growing 200 bytes/tuple > 100 bytes/tuple for 2 consecutive samples"
        ]

    def test_summary_screens_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_tripwires, "_summary_classify_python", _oracle_bomb)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config)
        monitor.on_tuple_summary({"sharpe": float("nan"), "net_pnl_paisa": 2e15})
        assert [e.name for e in events] == ["absurd_value", "nan_metric"]
        assert events[1].detail == "non-finite metric-summary value: sharpe=nan"

    def test_ru_maxrss_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_tripwires, "_ru_maxrss_python", _oracle_bomb)
        assert run_tripwires.normalized_ru_maxrss_bytes(ru_maxrss=1000, platform="linux") == (
            1_024_000
        )
        assert run_tripwires.normalized_ru_maxrss_bytes(ru_maxrss=7, platform="darwin") == 7

    def test_stage_invariant_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_tripwires, "_stage_missing_python", _oracle_bomb)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config)
        monitor.on_stage("artifacts", stamps_present=frozenset({"cascade_manifest"}))
        assert [e.detail for e in events] == [
            "stage 'artifacts' entered without prerequisite stamp(s): ['cascade_report']"
        ]
        monitor.on_stage("cascade", stamps_present=frozenset({"stage3"}))
        assert len(events) == 1  # a satisfied stage clears — crate-decided

    # ── The S5 full matrix — every unit-matrix class at the entry points ──

    def test_reconcile_multi_violation_detail_and_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FIVE co-firing violations in one tick — the joined detail must be
        # the templates in BIT order (== the pre-seam append order), built
        # from the crate-served bitmask; the values dict is the exact
        # five-float projection.
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config)
        monitor.on_progress(
            _snapshot(enumerated=90, evaluated=2, skipped=5, survivors=3, state=STATE_DONE),
            in_flight=-1,
        )
        assert _event_views(events) == [
            (
                "count_reconciliation",
                run_tripwires.SEVERITY_CRITICAL,
                "in_flight -1 < 0; snapshot enumerated 90 != armed total 100; "
                "skipped 5 > evaluated 2 (skipped ⊆ evaluated); "
                "survivors 3 > evaluated 2; "
                "run done but evaluated 2 != enumerated 100",
                {
                    "enumerated": 100.0,
                    "evaluated": 2.0,
                    "skipped": 5.0,
                    "survivors": 3.0,
                    "in_flight": -1.0,
                },
            )
        ]
        assert monitor.suspect is True

    def test_reconcile_boundary_identities_are_legal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The exact-equality boundaries the crate must read LEGAL:
        # skipped == evaluated · survivors == evaluated ·
        # evaluated + in_flight == total · done with evaluated == total.
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config)
        monitor.on_progress(_snapshot(evaluated=60, skipped=60, survivors=60), in_flight=40)
        monitor.on_progress(_snapshot(evaluated=100, state=STATE_DONE))
        assert events == []
        assert monitor.suspect is False

    def test_reconcile_backwards_edge_and_rearm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # backwards fires on the edge; the non-violating middle tick CLEARS
        # (re-arms) so a second regression fires AGAIN — the whole
        # fire→clear→re-fire walk crate-decided.
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config)
        monitor.on_progress(_snapshot(evaluated=50))
        monitor.on_progress(_snapshot(evaluated=49))
        monitor.on_progress(_snapshot(evaluated=49))
        monitor.on_progress(_snapshot(evaluated=48))
        assert [e.detail for e in events] == [
            "evaluated went backwards: 49 < 50",
            "evaluated went backwards: 48 < 49",
        ]

    def test_throughput_boundary_dwell_edge_and_rearm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # exactly-at-floor is NOT below (strict `<`); the fire lands on the
        # dwell EDGE only (`==` — a past-dwell tick never re-fires); a
        # recovery CLEARS + resets so the next regime fires again.
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.2,
            throughput_dwell=2,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config, total=10_000)
        monitor.on_progress(_rate_snapshot(10.0, 100))  # peak 10 → floor 2.0
        monitor.on_progress(_rate_snapshot(2.0, 200))  # exactly AT floor — clear
        monitor.on_progress(_rate_snapshot(1.0, 300))  # below 1 — none
        monitor.on_progress(_rate_snapshot(1.0, 400))  # below 2 == dwell — FIRE
        monitor.on_progress(_rate_snapshot(1.0, 500))  # below 3 != dwell — none
        monitor.on_progress(_rate_snapshot(5.0, 600))  # recovery — clear + reset
        monitor.on_progress(_rate_snapshot(1.0, 700))  # below 1 — none
        monitor.on_progress(_rate_snapshot(1.0, 800))  # below 2 == dwell — FIRE
        assert [e.name for e in events] == ["throughput_floor", "throughput_floor"]
        assert events[0].detail == (
            "throughput 1.000/s below floor 2.000/s (peak 10.000/s × 0.2) for 2 consecutive samples"
        )
        assert events[0].severity == run_tripwires.SEVERITY_WARNING

    def test_throughput_zero_peak_decides_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # All-zero rates: peak stays <= 0 → the crate returns TW_NONE and
        # nothing fires or clears (the pre-seam early return).
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.2,
            throughput_dwell=1,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config, total=10_000)
        monitor.on_progress(_snapshot(enumerated=10_000, evaluated=0, updated_at=100.0))
        monitor.on_progress(_snapshot(enumerated=10_000, evaluated=0, updated_at=200.0))
        assert events == []

    def test_memory_budget_boundary_exactly_at_is_legal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 500-vs-501 on a 0.5 × 1000 budget: exactly AT the budget is legal
        # (strict `>`); one byte over fires — both ticks crate-decided.
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.5,
            memory_slope_bytes_per_tuple=0,
        )
        readings = iter([500, 501])
        monitor, events = self._monitor(config, ram=1000, probe=lambda: next(readings))
        monitor.on_progress(_snapshot(evaluated=1))
        assert events == []  # exactly at budget — sane
        monitor.on_progress(_snapshot(evaluated=2))
        assert [e.detail for e in events] == ["RSS 501 bytes exceeds budget 500 bytes (0.5 × 1000)"]

    def test_memory_slope_boundary_recovery_and_rearm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # exactly-at-threshold is sane (strict `>`); the fire lands on the
        # dwell edge; a recovery resets the dwell counter AND re-arms.
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=100,
            memory_dwell=1,
        )
        readings = iter([1000, 1100, 1300, 1350, 1600])
        monitor, events = self._monitor(config, ram=10**12, probe=lambda: next(readings))
        for evaluated in range(1, 6):
            monitor.on_progress(_snapshot(evaluated=evaluated))
        assert [e.detail for e in events] == [
            # 1100→1300 slope 200 > 100 (the 1000→1100 tick was exactly AT)
            "RSS growing 200 bytes/tuple > 100 bytes/tuple for 1 consecutive samples",
            # 1300→1350 slope 50 recovered (clear + reset), 1350→1600 re-fires
            "RSS growing 250 bytes/tuple > 100 bytes/tuple for 1 consecutive samples",
        ]

    def test_summary_nan_rearm_fires_once_per_edge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config)
        monitor.on_tuple_summary({"sharpe": float("nan")})
        monitor.on_tuple_summary({"sharpe": float("nan")})  # still breached — held
        monitor.on_tuple_summary({"sharpe": 1.0})  # clear — re-arm
        monitor.on_tuple_summary({"sharpe": float("inf")})  # fires again
        assert [e.detail for e in events] == [
            "non-finite metric-summary value: sharpe=nan",
            "non-finite metric-summary value: sharpe=inf",
        ]

    def test_summary_absurd_boundary_and_screen_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Strict `>` on |value|: exactly-at is sane; an UNscreened key of
        # any magnitude never trips the absurd screen; an EMPTY summary is a
        # clean no-op; a screened non-finite value fires ONLY the NaN key
        # (the absurd key is untouched — the pre-seam `continue`).
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
            absurd_pnl_paisa=1000,
            absurd_value_names=("a", "b"),
        )
        monitor, events = self._monitor(config)
        monitor.on_tuple_summary({})  # empty batch — nothing
        monitor.on_tuple_summary({"a": 1000.0, "b": -1000.0, "junk": 9e18})
        assert events == []  # at-threshold sane both signs; unscreened key inert
        monitor.on_tuple_summary({"a": -1000.5, "b": 3.0})
        assert _event_views(events) == [
            (
                "absurd_value",
                run_tripwires.SEVERITY_CRITICAL,
                "absurd metric-summary magnitude: a=-1000.5 (|value| > 1000 paisa)",
                {"a": -1000.5},
            )
        ]
        monitor.on_tuple_summary({"a": float("-inf")})  # screened + non-finite
        assert [e.name for e in events] == ["absurd_value", "nan_metric"]

    def test_stage_matrix_unknown_inert_empty_required_and_rearm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _bomb_all_tripwire_oracles(monkeypatch)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )
        monitor, events = self._monitor(config)
        monitor.on_stage("no_such_stage", stamps_present=frozenset())  # unknown — inert
        monitor.on_stage("sweep", stamps_present=frozenset())  # empty required — clear
        monitor.on_stage("artifacts", stamps_present=frozenset())  # BOTH stamps missing
        assert [e.detail for e in events] == [
            "stage 'artifacts' entered without prerequisite stamp(s): "
            "['cascade_manifest', 'cascade_report']"
        ]
        monitor.on_stage(
            "artifacts", stamps_present=frozenset({"cascade_manifest", "cascade_report"})
        )  # satisfied — clears + re-arms
        monitor.on_stage("artifacts", stamps_present=frozenset({"cascade_report"}))
        assert [e.detail for e in events][-1] == (
            "stage 'artifacts' entered without prerequisite stamp(s): ['cascade_manifest']"
        )
        assert len(events) == 2


class TestTripwiresFailSoftEntry:
    """The I-3 layering pins of spec §4 at the INTEGRATION level: the facade
    is total over Exception, the escape trio re-raises RAW, and the L1
    blanket folds exactly what it folds on the pure-Python arm."""

    @staticmethod
    def _config() -> run_tripwires.TripwireConfig:
        return run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )

    def _monitor(self) -> run_tripwires.TripwireMonitor:
        counter = itertools.count(1)
        return run_tripwires.TripwireMonitor(
            config=self._config(),
            enumerated_total=100,
            clock=lambda: float(next(counter)),
            on_trip=lambda _event: None,
            ram_total_bytes=2**30,
            rss_probe=lambda: 1,
        )

    def test_junk_duck_snapshot_gate_refuses_oracle_raises_crate_never_called(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A junk duck field is refused by the facade's exact-type INPUT gate
        # BEFORE any crate call (the real wheel is bomb-wrapped as the spy),
        # the ORACLE's verbatim comparison raises, and the I-3 blanket folds
        # it to ONE observe_failed WARN — exactly today's pure-Python arm.
        real_crate = observability_rust._resolve_crate()
        assert real_crate is not None

        def _crate_bomb(_name: str) -> NoReturn:
            raise AssertionError("a gate-refused tripwire input reached the crate")

        monkeypatch.setattr(observability_rust.importlib, "import_module", _crate_bomb)
        duck = types.SimpleNamespace(
            state=STATE_RUNNING,
            enumerated=100,
            evaluated="one",  # non-int → gate refuses; `skipped > "one"` raises
            survivors=0,
            skipped=0,
            combos_per_sec=0.0,
        )
        monitor = self._monitor()
        with capture_logs() as logs:
            monitor.on_progress(duck)  # type: ignore[arg-type]  # the duck IS the test
        warns = [e for e in logs if e["event"] == "run_tripwires.observe_failed"]
        assert [(w["method"], w["error_type"]) for w in warns] == [("on_progress", "TypeError")]
        assert monitor.suspect is False

    def test_fake_crate_memory_error_matches_oracle_memory_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The OOM-landing INVERSION, both arms at the integration level: a
        # MemoryError raised crate-side re-raises RAW through the facade and
        # a MemoryError raised oracle-side propagates identically — the L1
        # blanket except Exception CATCHES either → one observe_failed WARN,
        # zero suspect, the run continues (identical on both arms).
        def _drive_one(mp: pytest.MonkeyPatch) -> list[tuple[str, str]]:
            monitor = self._monitor()
            with capture_logs() as logs:
                monitor.on_progress(_snapshot(evaluated=1))
            assert monitor.suspect is False
            return [
                (e["method"], e["error_type"])
                for e in logs
                if e["event"] == "run_tripwires.observe_failed"
            ]

        def _oom(*args: object, **kwargs: object) -> NoReturn:
            raise MemoryError("oom")

        with pytest.MonkeyPatch.context() as mp:
            fake = types.ModuleType("brutex_observability")
            real = observability_rust._resolve_crate()
            assert real is not None
            for name in observability_rust._CRATE_API:
                setattr(fake, name, getattr(real, name))
            fake.tripwire_reconcile_counts = _oom  # type: ignore[attr-defined]
            mp.setattr(observability_rust.importlib, "import_module", lambda _n: fake)
            crate_arm = _drive_one(mp)
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv(observability_rust.OBSERVABILITY_RUST_ENV, raising=False)
            mp.setattr(run_tripwires, "_reconcile_decide_python", _oom)
            oracle_arm = _drive_one(mp)
        assert crate_arm == oracle_arm == [("on_progress", "MemoryError")]


class TestTripwiresLifecycleEntry:
    """The engaged INFO + per-law burn_in_complete lifecycle through REAL
    monitor ticks on the real wheel (remaining=1 → complete → steady)."""

    def test_one_progress_tick_completes_three_law_burn_ins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observability_rust.reset_observability_rust_for_tests(remaining=1)
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.2,
            rss_budget_fraction=0.5,
            memory_slope_bytes_per_tuple=0,
        )
        counter = itertools.count(1)
        monitor = run_tripwires.TripwireMonitor(
            config=config,
            enumerated_total=10_000,
            clock=lambda: float(next(counter)),
            on_trip=lambda _event: None,
            ram_total_bytes=2**30,
            rss_probe=lambda: 1,
        )
        with capture_logs() as logs:
            monitor.on_progress(_rate_snapshot(10.0, 100))
        done = sorted(e["law"] for e in logs if e["event"] == "observability_rust.burn_in_complete")
        assert done == ["tw_budget", "tw_reconcile", "tw_throughput"]
        engaged = [e for e in logs if e["event"] == "observability_rust.engaged"]
        assert len(engaged) == 1
        assert observability_rust.rust_observability_disabled() is False
        # Steady thereafter: the SAME entry point with the oracles bombed.
        _bomb_all_tripwire_oracles(monkeypatch)
        monitor.on_progress(_rate_snapshot(10.0, 200))
        assert monitor.suspect is False


class TestTripwiresOnOffEquivalence:
    """The integration-level differential: the SAME operation sequence
    driven through a flag-ON steady-state monitor (crate-decided, LIVE
    oracles) and a flag-OFF monitor must produce IDENTICAL event streams,
    suspect verdicts and dwell state — over hypothesis-chosen hostile
    rates, RSS walks, summary floats and stage sets."""

    _RATES = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
    _SUMMARY_VALUES = st.floats(allow_nan=True, allow_infinity=True, width=64)

    @settings(max_examples=50, deadline=None)
    @given(data=st.data())
    def test_event_streams_identical_on_and_off(self, data: st.DataObject) -> None:
        evaluated_steps = data.draw(
            st.lists(st.integers(min_value=0, max_value=50), min_size=1, max_size=8)
        )
        rss_walk = data.draw(
            st.lists(
                st.integers(min_value=0, max_value=10**9),
                min_size=len(evaluated_steps),
                max_size=len(evaluated_steps),
            )
        )
        rates = data.draw(
            st.lists(self._RATES, min_size=len(evaluated_steps), max_size=len(evaluated_steps))
        )
        summaries = data.draw(
            st.lists(
                st.dictionaries(
                    st.sampled_from(["net_pnl_paisa", "max_dd_paisa", "sharpe"]),
                    self._SUMMARY_VALUES,
                    max_size=3,
                ),
                max_size=4,
            )
        )
        stages = data.draw(
            st.lists(
                st.tuples(
                    st.sampled_from(["stage3", "cascade", "artifacts", "unknown"]),
                    st.frozensets(
                        st.sampled_from(["stage2", "stage3", "cascade_manifest", "cascade_report"]),
                        max_size=3,
                    ),
                ),
                max_size=4,
            )
        )
        config = run_tripwires.TripwireConfig(
            reconcile_interval_s=1e-9,
            throughput_floor_fraction=0.5,
            throughput_dwell=2,
            rss_budget_fraction=0.9,
            memory_slope_bytes_per_tuple=1000,
            memory_dwell=2,
            absurd_pnl_paisa=10**6,
        )

        def _drive() -> tuple[list[tuple[str, str, str, str]], bool]:
            events: list[run_tripwires.TripwireEvent] = []
            counter = itertools.count(1)
            readings = iter(rss_walk)
            monitor = run_tripwires.TripwireMonitor(
                config=config,
                enumerated_total=10_000,
                clock=lambda: float(next(counter)),
                on_trip=events.append,
                ram_total_bytes=10**9,
                rss_probe=lambda: next(readings),
            )
            evaluated = 0
            for step, rate in zip(evaluated_steps, rates, strict=True):
                evaluated += step
                snapshot = _snapshot(
                    enumerated=10_000,
                    evaluated=evaluated,
                    started_at=0.0,
                    updated_at=(evaluated / rate) if rate > 0.0 else 1.0,
                )
                monitor.on_progress(snapshot)
            for tallies in summaries:
                monitor.on_tuple_summary(tallies)
            for stage, present in stages:
                monitor.on_stage(stage, stamps_present=present)
            views = [
                (e.name, e.severity, e.detail, repr(sorted(e.values.items())))
                for e in monitor.events
            ]
            return views, monitor.suspect

        observability_rust.reset_observability_rust_for_tests(remaining=0)
        on_views, on_suspect = _drive()
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv(observability_rust.OBSERVABILITY_RUST_ENV, raising=False)
            off_views, off_suspect = _drive()
        assert on_views == off_views
        assert on_suspect == off_suspect
