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
    DOMINANT_TUPLE_SAMPLE,
    ENV_PER_TUPLE_SECONDS,
    SLA_ENVELOPE_DAYS,
    SLA_ENVELOPE_SECONDS,
    SWEEP_PER_TUPLE_SECONDS,
    SlaDecision,
    SlaProjection,
    TupleCostCell,
    WeightedGridCost,
    check_sweep_sla,
    project_weighted_grid_cost,
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


# ── k-aware weighted projection (DEC-SWEEP-SCALE-GUARDS-001) ──


def _cell(k: int, *, n_bars: int = 1000, n_bits: int = 60, mult: int = 1) -> TupleCostCell:
    return TupleCostCell(
        label=f"NIFTY 5minute k={k}", k=k, n_bits=n_bits, n_bars=n_bars, multiplicity=mult
    )


class TestTupleCostCell:
    def test_weight_is_combos_times_bars(self) -> None:
        cell = _cell(5, n_bars=2000, n_bits=60)
        assert cell.weight() == math.comb(60, 5) * 2000

    def test_dominant_sample_constant(self) -> None:
        assert DOMINANT_TUPLE_SAMPLE == 3

    @pytest.mark.parametrize(
        ("k", "n_bits"),
        [(-1, 60), (61, 60), (6, 5)],
    )
    def test_rejects_k_out_of_range(self, k: int, n_bits: int) -> None:
        with pytest.raises(ValueError, match="0 <= k <= n_bits"):
            TupleCostCell(label="x", k=k, n_bits=n_bits, n_bars=10, multiplicity=1)

    @pytest.mark.parametrize("bad_bars", [0, -1])
    def test_rejects_non_positive_bars(self, bad_bars: int) -> None:
        with pytest.raises(ValueError, match="n_bars must be >= 1"):
            TupleCostCell(label="x", k=1, n_bits=60, n_bars=bad_bars, multiplicity=1)

    @pytest.mark.parametrize("bad_mult", [0, -3])
    def test_rejects_non_positive_multiplicity(self, bad_mult: int) -> None:
        with pytest.raises(ValueError, match="multiplicity must be >= 1"):
            TupleCostCell(label="x", k=1, n_bits=60, n_bars=10, multiplicity=bad_mult)


class TestProjectWeightedGridCost:
    def test_uniform_k_projects_exactly_flat(self) -> None:
        # A k-uniform grid (even with mixed N_bars) projects EXACTLY
        # per_tuple_seconds × total_tuples — the reference is the mean, so it
        # cancels. This is the "k=1-only grid projects the same order" pin, tight
        # to equality for the uniform case.
        cells = [
            _cell(1, n_bars=1000, mult=4),
            _cell(1, n_bars=9000, mult=6),  # different N_bars, same k
        ]
        cost = project_weighted_grid_cost(cells, per_tuple_seconds=30.0)
        assert cost.total_tuples == 10
        assert cost.projected_seconds == pytest.approx(30.0 * 10)

    def test_high_k_projects_orders_higher(self) -> None:
        # A k=5-heavy grid projects ORDERS higher than the same tuple count of
        # k=1 — tracking the C(60,1)→C(60,5) ≈ 91,000× mask-population spread.
        k1 = project_weighted_grid_cost([_cell(1, mult=100)], per_tuple_seconds=30.0)
        heavy = project_weighted_grid_cost(
            [_cell(1, mult=1), _cell(5, mult=99)], per_tuple_seconds=30.0
        )
        # Same 100 tuples, but the k=5-dominated grid is thousands of times costlier.
        assert heavy.total_tuples == k1.total_tuples == 100
        assert heavy.projected_seconds > 1000 * k1.projected_seconds

    def test_reference_anchors_on_smallest_k(self) -> None:
        # With k∈{1,5}, the reference is the k=1 mean weight, so a lone k=5 tuple
        # projects per_tuple_seconds × C(60,5)/C(60,1) (N_bars equal) ≈ 91,025×.
        cost = project_weighted_grid_cost(
            [_cell(1, mult=1), _cell(5, mult=1)], per_tuple_seconds=1.0
        )
        expected_k5 = math.comb(60, 5) / math.comb(60, 1)
        assert cost.projected_seconds == pytest.approx(1.0 * (1.0 + expected_k5))

    def test_dominant_labels_name_the_heaviest_tuples(self) -> None:
        cost = project_weighted_grid_cost(
            [_cell(1, mult=10), _cell(3, mult=2), _cell(5, mult=1)],
            per_tuple_seconds=30.0,
        )
        # Heaviest first: k=5 then k=3 then k=1 (all same N_bars/n_bits).
        assert cost.dominant_labels == (
            "NIFTY 5minute k=5",
            "NIFTY 5minute k=3",
            "NIFTY 5minute k=1",
        )

    def test_dominant_count_caps_the_sample(self) -> None:
        cells = [_cell(k) for k in (1, 2, 3, 4, 5)]
        cost = project_weighted_grid_cost(cells, per_tuple_seconds=30.0, dominant_count=2)
        assert cost.dominant_labels == ("NIFTY 5minute k=5", "NIFTY 5minute k=4")

    def test_empty_cells_refused(self) -> None:
        with pytest.raises(ValueError, match="cells must be non-empty"):
            project_weighted_grid_cost([], per_tuple_seconds=30.0)

    def test_bad_dominant_count_refused(self) -> None:
        with pytest.raises(ValueError, match="dominant_count must be >= 1"):
            project_weighted_grid_cost([_cell(1)], per_tuple_seconds=30.0, dominant_count=0)

    def test_returns_weighted_grid_cost(self) -> None:
        cost = project_weighted_grid_cost([_cell(1, mult=3)], per_tuple_seconds=30.0)
        assert isinstance(cost, WeightedGridCost)
        assert cost.total_weight == _cell(1).weight() * 3


class TestCheckSweepSlaWeighted:
    def test_k1_only_grid_projects_same_order_as_flat(self) -> None:
        # Pin: a k=1-only grid's weighted projection matches the flat model's
        # order (here EXACTLY, k uniform). 100k k=1 tuples × 30 s = ~34.7 days.
        cells = [_cell(1, mult=100_000)]
        weighted = check_sweep_sla(100_000, acknowledge=False, cells=cells, env={})
        flat = check_sweep_sla(100_000, acknowledge=False, env={})
        assert weighted.projected_seconds == pytest.approx(flat.projected_seconds)
        assert weighted.weighted is True
        assert flat.weighted is False

    def test_k5_heavy_grid_refuses_where_same_count_of_k1_allows(self) -> None:
        # A k=5-HEAVY grid (a small k=1 anchor + many k=5 tuples) that the FLAT
        # model would wave through is REFUSED once the k-aware weighting sees the
        # true per-tuple cost. The projection is GRID-RELATIVE: the reference is
        # the k=1 anchor, so the k=5 mass projects ~91,000× each.
        cells = [_cell(1, mult=1), _cell(5, mult=200)]
        flat = check_sweep_sla(201, acknowledge=False, env={})
        weighted = check_sweep_sla(201, acknowledge=False, cells=cells, env={})
        assert flat.decision is SlaDecision.ALLOWED
        assert weighted.decision is SlaDecision.REFUSED
        assert weighted.projected_seconds > flat.projected_seconds * 1000

    def test_uniform_high_k_grid_stays_flat_honest_envelope(self) -> None:
        # HONEST ENVELOPE (Golden Rule #10): the projection is grid-RELATIVE, so
        # a grid whose smallest K IS 5 anchors the 30 s calibration on the k=5
        # tuple itself and projects flat — the guard cannot claim k=5 is
        # "expensive" in absolute wall-clock without a benchmark (which does not
        # exist). The blow-up manifests only against a SMALLER-k anchor present.
        weighted = check_sweep_sla(200, acknowledge=False, cells=[_cell(5, mult=200)], env={})
        flat = check_sweep_sla(200, acknowledge=False, env={})
        assert weighted.projected_seconds == pytest.approx(flat.projected_seconds)

    def test_refusal_names_dominant_tuples(self) -> None:
        # Pin: the refusal text NAMES the dominant tuples so the operator sees
        # which cells drive the projection.
        cells = [_cell(1, mult=1), _cell(5, mult=500)]
        proj = check_sweep_sla(501, acknowledge=False, cells=cells, env={})
        assert proj.decision is SlaDecision.REFUSED
        message = proj.refusal_message()
        assert "Dominant tuples" in message
        assert "NIFTY 5minute k=5" in message
        assert "per-tuple-cost-weighted" in message

    def test_acknowledged_warning_also_names_dominant_tuples(self) -> None:
        cells = [_cell(1, mult=1), _cell(5, mult=500)]
        proj = check_sweep_sla(501, acknowledge=True, cells=cells, env={})
        assert proj.decision is SlaDecision.ACKNOWLEDGED
        assert "NIFTY 5minute k=5" in proj.acknowledged_warning()

    def test_weighted_log_event_marks_the_model(self) -> None:
        cells = [_cell(5, mult=500)]
        with capture_logs() as logs:
            check_sweep_sla(500, acknowledge=False, cells=cells, env={})
        (event,) = [e for e in logs if e["event"] == "sweep.sla_projection"]
        assert event["projection_model"] == "k_weighted"
        assert "NIFTY 5minute k=5" in event["dominant_tuples"]

    def test_empty_cells_sequence_refused_as_config_error(self) -> None:
        with pytest.raises(ValueError, match="cells must be non-empty"):
            check_sweep_sla(0, acknowledge=False, cells=[], env={})
