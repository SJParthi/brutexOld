"""Unit tests for :mod:`brutex.engine.sweep_sla_guard` (DEC-SLA-LAUNCH-GUARD-001).

100% line + branch coverage of the SLA-honesty launch guard: the wall-clock
projection (``tuple_count × per-tuple estimate``) against the CLAUDE.md §8
2-4-day envelope, the allowed / refused / acknowledged decision, the env-var
calibration override, the input validation (including the NaN/inf bypass the
hostile review flagged — MINOR-1), the honest operator messages, and the loud
``sweep.sla_projection`` structlog event.

Honesty pins (Golden Rule #10): the per-tuple constant is an UNBENCHMARKED
estimate — the messages must SAY so, must name the SERIAL projection model
(hostile-review MINOR-4), and must never claim the §8 SLA certified.
"""

from __future__ import annotations

import math

import pytest
from brutex.engine.sweep_sla_guard import (
    ENV_PER_TUPLE_SECONDS,
    SLA_ENVELOPE_DAYS,
    SLA_ENVELOPE_SECONDS,
    SWEEP_PER_TUPLE_SECONDS,
    SlaDecision,
    SlaProjection,
    check_sweep_sla,
    resolve_per_tuple_seconds,
)
from structlog.testing import capture_logs


class TestConstants:
    def test_envelope_is_the_section8_four_day_ceiling(self) -> None:
        assert SLA_ENVELOPE_DAYS == 4.0
        assert SLA_ENVELOPE_SECONDS == 4.0 * 86_400.0

    def test_per_tuple_placeholder_value(self) -> None:
        # The UNBENCHMARKED conservative placeholder — 30 s/tuple until a real
        # measurement on the §8 envelope replaces it (env-overridable). The
        # knob says TUPLE, not "trial" — a §8 trial is one mask evaluation,
        # orders of magnitude finer than a grid tuple (hostile-review MINOR-5).
        assert SWEEP_PER_TUPLE_SECONDS == 30.0
        assert ENV_PER_TUPLE_SECONDS == "BRUTEX_SWEEP_PER_TUPLE_SECONDS"
        assert "TRIAL" not in ENV_PER_TUPLE_SECONDS


class TestResolvePerTupleSeconds:
    def test_default_without_env(self) -> None:
        assert resolve_per_tuple_seconds(env={}) == SWEEP_PER_TUPLE_SECONDS

    def test_env_override_used(self) -> None:
        assert resolve_per_tuple_seconds(env={ENV_PER_TUPLE_SECONDS: "0.25"}) == 0.25

    def test_none_env_reads_process_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_PER_TUPLE_SECONDS, "12.5")
        assert resolve_per_tuple_seconds() == 12.5

    def test_non_numeric_env_raises(self) -> None:
        with pytest.raises(ValueError, match="positive, finite number of seconds.*'abc'"):
            resolve_per_tuple_seconds(env={ENV_PER_TUPLE_SECONDS: "abc"})

    @pytest.mark.parametrize("bad", ["0", "-1.5"])
    def test_nonpositive_env_raises(self, bad: str) -> None:
        with pytest.raises(ValueError, match="positive, finite number of seconds"):
            resolve_per_tuple_seconds(env={ENV_PER_TUPLE_SECONDS: bad})

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
    def test_nonfinite_env_raises(self, bad: str) -> None:
        # MINOR-1 regression: float("nan") <= 0 is False, so a bare "> 0"
        # check ACCEPTED a NaN override and projected "≈ nan days". A
        # non-finite override must be the same clean user-config error.
        with pytest.raises(ValueError, match="positive, finite number of seconds"):
            resolve_per_tuple_seconds(env={ENV_PER_TUPLE_SECONDS: bad})


class TestCheckSweepSla:
    def test_within_envelope_is_allowed(self) -> None:
        # 100 tuples × 30 s = 3000 s ≪ 4 days.
        projection = check_sweep_sla(100, acknowledge=False, env={})
        assert projection.decision is SlaDecision.ALLOWED
        assert projection.tuple_count == 100
        assert projection.per_tuple_seconds == SWEEP_PER_TUPLE_SECONDS
        assert projection.projected_seconds == 3_000.0
        assert projection.envelope_seconds == SLA_ENVELOPE_SECONDS

    def test_exactly_at_envelope_is_allowed(self) -> None:
        # The boundary is INCLUSIVE — "2-4 days" means 4 days still fits.
        projection = check_sweep_sla(4, acknowledge=False, per_tuple_seconds=86_400.0)
        assert projection.decision is SlaDecision.ALLOWED

    def test_over_envelope_without_acknowledge_is_refused(self) -> None:
        projection = check_sweep_sla(1_000_000, acknowledge=False, env={})
        assert projection.decision is SlaDecision.REFUSED
        assert projection.projected_seconds > projection.envelope_seconds

    def test_over_envelope_with_acknowledge_proceeds_loudly(self) -> None:
        projection = check_sweep_sla(1_000_000, acknowledge=True, env={})
        assert projection.decision is SlaDecision.ACKNOWLEDGED

    def test_acknowledge_on_a_fitting_grid_is_still_allowed(self) -> None:
        # Passing the flag on a grid that fits never fabricates a warning.
        projection = check_sweep_sla(10, acknowledge=True, env={})
        assert projection.decision is SlaDecision.ALLOWED

    def test_explicit_per_tuple_overrides_env(self) -> None:
        projection = check_sweep_sla(
            10,
            acknowledge=False,
            per_tuple_seconds=2.0,
            env={ENV_PER_TUPLE_SECONDS: "999999"},
        )
        assert projection.per_tuple_seconds == 2.0
        assert projection.projected_seconds == 20.0

    def test_env_calibration_feeds_the_projection(self) -> None:
        projection = check_sweep_sla(10, acknowledge=False, env={ENV_PER_TUPLE_SECONDS: "100000"})
        assert projection.decision is SlaDecision.REFUSED

    def test_zero_tuples_is_allowed(self) -> None:
        assert check_sweep_sla(0, acknowledge=False, env={}).decision is SlaDecision.ALLOWED

    def test_negative_tuple_count_raises(self) -> None:
        with pytest.raises(ValueError, match="tuple_count must be >= 0"):
            check_sweep_sla(-1, acknowledge=False, env={})

    @pytest.mark.parametrize("bad", [0.0, -60.0, math.nan, math.inf])
    def test_bad_envelope_raises(self, bad: float) -> None:
        with pytest.raises(ValueError, match="envelope_seconds must be a positive, finite"):
            check_sweep_sla(1, acknowledge=False, envelope_seconds=bad, env={})

    @pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
    def test_bad_explicit_per_tuple_raises(self, bad: float) -> None:
        with pytest.raises(ValueError, match="per_tuple_seconds must be a positive, finite"):
            check_sweep_sla(1, acknowledge=False, per_tuple_seconds=bad)


class TestStructlogEvent:
    def test_allowed_logs_info_with_decision(self) -> None:
        with capture_logs() as logs:
            check_sweep_sla(10, acknowledge=False, env={})
        (event,) = [e for e in logs if e["event"] == "sweep.sla_projection"]
        assert event["log_level"] == "info"
        assert event["decision"] == "allowed"
        assert event["tuple_count"] == 10
        assert event["per_tuple_seconds"] == SWEEP_PER_TUPLE_SECONDS
        assert event["projected_wall_clock_seconds"] == 300.0
        assert event["envelope_seconds"] == SLA_ENVELOPE_SECONDS
        # The event itself carries the honesty flags: the estimate is
        # UNBENCHMARKED and the model is SERIAL (no multi-worker speedup).
        assert event["per_tuple_estimate_benchmarked"] is False
        assert event["projection_model"] == "serial"

    @pytest.mark.parametrize(
        ("acknowledge", "decision"), [(False, "refused"), (True, "acknowledged")]
    )
    def test_overrun_logs_warning_with_decision(self, acknowledge: bool, decision: str) -> None:
        with capture_logs() as logs:
            check_sweep_sla(1_000_000, acknowledge=acknowledge, env={})
        (event,) = [e for e in logs if e["event"] == "sweep.sla_projection"]
        assert event["log_level"] == "warning"
        assert event["decision"] == decision


class TestMessagesHonesty:
    def _refused(self) -> SlaProjection:
        return check_sweep_sla(1_000_000, acknowledge=False, env={})

    def test_refusal_message_is_honest_and_actionable(self) -> None:
        message = self._refused().refusal_message()
        assert message.startswith("REFUSED:")
        assert "UNBENCHMARKED" in message
        assert ENV_PER_TUPLE_SECONDS in message
        assert "--acknowledge-sla-overrun" in message
        assert "does NOT certify" in message  # never claims the §8 SLA either way
        assert "4 days" in message

    def test_refusal_message_names_the_serial_model_and_worker_lever(self) -> None:
        # MINOR-4: the projection ignores the parallel driver's multi-worker
        # speedup (it can over-refuse a parallel launch); the refusal must say
        # so and name the worker count as a lever alongside shrinking the grid.
        message = self._refused().refusal_message()
        assert "SERIAL" in message
        assert "BRUTEX_SWEEP_WORKERS" in message
        assert "--max-tuples" in message

    def test_acknowledged_warning_is_loud_and_honest(self) -> None:
        projection = check_sweep_sla(1_000_000, acknowledge=True, env={})
        message = projection.acknowledged_warning()
        assert message.startswith("SLA OVERRUN ACKNOWLEDGED:")
        assert "UNBENCHMARKED" in message
        assert "not a" in message and "certification" in message

    def test_projected_days_property(self) -> None:
        projection = check_sweep_sla(2, acknowledge=False, per_tuple_seconds=86_400.0)
        assert projection.projected_days == 2.0

    def test_projection_is_frozen(self) -> None:
        projection = check_sweep_sla(1, acknowledge=False, env={})
        with pytest.raises(AttributeError):
            projection.tuple_count = 2  # type: ignore[misc]


# ────────── DEC-RUST-SWEEP-OPS-001 — slice-1b routing (wheel-free) ──────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the SLA projection decision.

    Wheel-free: the shared behavior-correct FAKE crate is injected by
    patching the facade's ``importlib`` binding. Flag ON must be
    value-identical to the flag-OFF Python law (which every OTHER test in
    this file drives).
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        # The shared §11-COMMON builder carries EVERY `_CRATE_API` symbol —
        # a partial fake would latch `crate_stale` and turn this class vacuous.
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

    def test_flag_on_matches_python_law_all_decisions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        off = [
            check_sweep_sla(100, acknowledge=False, env={}),
            check_sweep_sla(1_000_000, acknowledge=False, env={}),
            check_sweep_sla(1_000_000, acknowledge=True, env={}),
        ]
        self._arm_fake_crate(monkeypatch)
        try:
            on = [
                check_sweep_sla(100, acknowledge=False, env={}),
                check_sweep_sla(1_000_000, acknowledge=False, env={}),
                check_sweep_sla(1_000_000, acknowledge=True, env={}),
            ]
            assert on == off
            assert [p.decision.value for p in on] == ["allowed", "refused", "acknowledged"]
        finally:
            self._reset_facade()
