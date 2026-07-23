"""Unit tests for the Stage-3 Mode-A grid bijection (`brutex.engine.stage3_grid`).

DEC-STAGE3-GRID-001 (ADR-0007 §5 slice 3, condition 3; math re-hosted on the
``brutex_refine`` wheel by DEC-RUST-STAGE3-REFINE-001) — 100% line+branch of
the [0,1] ↔ int-paisa mapping: the locked Mode-A axis constants pinned against
`TRACK1_BACKTEST_SPEC.md:243-245` (SL [5,200]/5-tick/40 steps, TP
[10,500]/10-tick/50 steps, 2,000 cells), every :class:`GridAxis` validation
branch, denormalize's clamp/snap/NaN behaviour (including the documented
round-half-to-even tie), normalize's range + tick rejection, the EXHAUSTIVE
``denormalize(normalize(p)) == p`` round-trip over all 90 grid points, the
(SL, TP) pair conveniences, and the condition-3 proof that only ints reach the
9-field combo hash's canonical form.
"""

from __future__ import annotations

import math

import pytest
from brutex.engine.stage3_grid import (
    MODE_A_GRID_CELLS,
    MODE_A_SL_AXIS,
    MODE_A_TP_AXIS,
    GridAxis,
    Stage3GridError,
    denormalize_sl_tp,
    normalize_sl_tp,
)
from brutex.strategies.combo import canonicalise_params

pytest.importorskip("brutex_refine")  # the Stage-3 Rust wheel — built in the CI unit job


def _grid_points(axis: GridAxis) -> list[int]:
    """Every on-grid paisa value of *axis*, ascending."""
    return list(range(axis.lo_paisa, axis.hi_paisa + 1, axis.tick_paisa))


class TestModeAConstants:
    """The locked spec constants — `TRACK1_BACKTEST_SPEC.md:243-245`, never data-derived."""

    def test_sl_axis_is_5_to_200_on_5_paisa_tick(self) -> None:
        assert GridAxis(name="sl_paisa", lo_paisa=5, hi_paisa=200, tick_paisa=5) == MODE_A_SL_AXIS

    def test_tp_axis_is_10_to_500_on_10_paisa_tick(self) -> None:
        assert GridAxis(name="tp_paisa", lo_paisa=10, hi_paisa=500, tick_paisa=10) == MODE_A_TP_AXIS

    def test_sl_axis_has_40_steps(self) -> None:
        assert MODE_A_SL_AXIS.steps == 40

    def test_tp_axis_has_50_steps(self) -> None:
        assert MODE_A_TP_AXIS.steps == 50

    def test_mode_a_grid_is_2000_cells_per_mask(self) -> None:
        assert MODE_A_GRID_CELLS == 2_000


class TestGridAxisValidation:
    """Every constructor guard — an axis that could break the bijection is refused."""

    def test_valid_axis_constructs(self) -> None:
        axis = GridAxis(name="t", lo_paisa=5, hi_paisa=45, tick_paisa=10)
        assert axis.steps == 5

    def test_non_positive_lo_rejected(self) -> None:
        with pytest.raises(Stage3GridError, match="lo_paisa must be > 0"):
            GridAxis(name="t", lo_paisa=0, hi_paisa=100, tick_paisa=5)

    def test_non_positive_tick_rejected(self) -> None:
        with pytest.raises(Stage3GridError, match="tick_paisa must be > 0"):
            GridAxis(name="t", lo_paisa=5, hi_paisa=100, tick_paisa=0)

    def test_hi_not_above_lo_rejected(self) -> None:
        # hi == lo would leave steps == 1 and a zero denominator in normalize.
        with pytest.raises(Stage3GridError, match="hi_paisa must be > lo_paisa"):
            GridAxis(name="t", lo_paisa=100, hi_paisa=100, tick_paisa=5)

    def test_span_not_whole_ticks_rejected(self) -> None:
        with pytest.raises(Stage3GridError, match="not a whole"):
            GridAxis(name="t", lo_paisa=5, hi_paisa=201, tick_paisa=5)


class TestDenormalize:
    """Clamp into [0,1], snap to the nearest grid index, always return int."""

    @pytest.mark.parametrize("axis", [MODE_A_SL_AXIS, MODE_A_TP_AXIS], ids=lambda a: a.name)
    def test_edge_zero_maps_to_lo(self, axis: GridAxis) -> None:
        assert axis.denormalize(0.0) == axis.lo_paisa

    @pytest.mark.parametrize("axis", [MODE_A_SL_AXIS, MODE_A_TP_AXIS], ids=lambda a: a.name)
    def test_edge_one_maps_to_hi(self, axis: GridAxis) -> None:
        assert axis.denormalize(1.0) == axis.hi_paisa

    @pytest.mark.parametrize("u", [-0.25, -1e9, -math.inf])
    def test_below_box_clamps_to_lo(self, u: float) -> None:
        assert MODE_A_SL_AXIS.denormalize(u) == MODE_A_SL_AXIS.lo_paisa

    @pytest.mark.parametrize("u", [1.25, 1e9, math.inf])
    def test_above_box_clamps_to_hi(self, u: float) -> None:
        assert MODE_A_SL_AXIS.denormalize(u) == MODE_A_SL_AXIS.hi_paisa

    def test_nan_rejected(self) -> None:
        with pytest.raises(Stage3GridError, match="cannot denormalize NaN"):
            MODE_A_SL_AXIS.denormalize(math.nan)

    def test_result_type_is_int_never_float(self) -> None:
        # Condition 3: floats must never reach the 9-field combo hash.
        for u in (0.0, 0.123, 0.5, 1.0):
            assert type(MODE_A_SL_AXIS.denormalize(u)) is int
            assert type(MODE_A_TP_AXIS.denormalize(u)) is int

    def test_snaps_to_nearest_grid_index(self) -> None:
        # SL axis: 0.5 · 39 = 19.5 → even index 20 → 5 + 20·5 = 105 paisa.
        assert MODE_A_SL_AXIS.denormalize(0.5) == 105
        # Just below/above the 19.5 midpoint resolve to indices 19 / 20.
        assert MODE_A_SL_AXIS.denormalize(19.4 / 39) == 100
        assert MODE_A_SL_AXIS.denormalize(19.6 / 39) == 105

    def test_exact_half_ties_round_to_even_index(self) -> None:
        # Synthetic power-of-two axis (steps=5, so steps-1=4) makes the tie
        # products EXACT binary64 values: 0.125·4 = 0.5 and 0.375·4 = 1.5.
        axis = GridAxis(name="t", lo_paisa=5, hi_paisa=45, tick_paisa=10)
        assert axis.denormalize(0.125) == 5  # 0.5 → even index 0
        assert axis.denormalize(0.375) == 25  # 1.5 → even index 2


class TestNormalize:
    """Exact inverse on the grid; off-grid / out-of-range input is refused."""

    @pytest.mark.parametrize("axis", [MODE_A_SL_AXIS, MODE_A_TP_AXIS], ids=lambda a: a.name)
    def test_lo_maps_to_zero(self, axis: GridAxis) -> None:
        assert axis.normalize(axis.lo_paisa) == 0.0

    @pytest.mark.parametrize("axis", [MODE_A_SL_AXIS, MODE_A_TP_AXIS], ids=lambda a: a.name)
    def test_hi_maps_to_one(self, axis: GridAxis) -> None:
        assert axis.normalize(axis.hi_paisa) == 1.0

    def test_interior_point_exact_coordinate(self) -> None:
        assert MODE_A_SL_AXIS.normalize(10) == 1 / 39
        assert MODE_A_TP_AXIS.normalize(20) == 1 / 49

    def test_below_range_rejected(self) -> None:
        with pytest.raises(Stage3GridError, match="outside"):
            MODE_A_SL_AXIS.normalize(4)

    def test_above_range_rejected(self) -> None:
        with pytest.raises(Stage3GridError, match="outside"):
            MODE_A_TP_AXIS.normalize(510)

    @pytest.mark.parametrize(
        ("axis", "paisa"),
        [(MODE_A_SL_AXIS, 7), (MODE_A_TP_AXIS, 15)],
        ids=["sl_paisa", "tp_paisa"],
    )
    def test_off_tick_rejected_never_snapped(self, axis: GridAxis, paisa: int) -> None:
        # Silent snapping here would let a never-evaluated (SL, TP) pair
        # masquerade as a measured grid point.
        with pytest.raises(Stage3GridError, match="off-grid"):
            axis.normalize(paisa)


class TestRoundTripExhaustive:
    """``denormalize(normalize(p)) == p`` over the ENTIRE Mode-A grid — all 90 points."""

    @pytest.mark.parametrize("axis", [MODE_A_SL_AXIS, MODE_A_TP_AXIS], ids=lambda a: a.name)
    def test_identity_on_every_grid_point(self, axis: GridAxis) -> None:
        points = _grid_points(axis)
        assert len(points) == axis.steps
        for paisa in points:
            u = axis.normalize(paisa)
            assert 0.0 <= u <= 1.0
            recovered = axis.denormalize(u)
            assert recovered == paisa
            assert type(recovered) is int


class TestSlTpPair:
    """The (SL, TP) conveniences the refinement runner calls per evaluation."""

    def test_denormalize_pair_edges(self) -> None:
        assert denormalize_sl_tp(0.0, 1.0) == (5, 500)

    def test_normalize_pair_edges(self) -> None:
        assert normalize_sl_tp(5, 500) == (0.0, 1.0)

    def test_pair_round_trip(self) -> None:
        u_sl, u_tp = normalize_sl_tp(105, 250)
        assert denormalize_sl_tp(u_sl, u_tp) == (105, 250)

    def test_nan_in_either_coordinate_rejected(self) -> None:
        with pytest.raises(Stage3GridError, match="sl_paisa"):
            denormalize_sl_tp(math.nan, 0.5)
        with pytest.raises(Stage3GridError, match="tp_paisa"):
            denormalize_sl_tp(0.5, math.nan)

    def test_quantized_params_reach_the_hash_as_ints(self) -> None:
        # ADR-0007 condition 3: the canonical 9-field-hash param form built
        # from denormalized values must contain int literals — no float ever
        # reaches the combo hash.
        sl_paisa, tp_paisa = denormalize_sl_tp(0.5003, 0.49)
        canonical = canonicalise_params({"sl_paisa": sl_paisa, "tp_paisa": tp_paisa})
        assert canonical == f'{{"sl_paisa":{sl_paisa},"tp_paisa":{tp_paisa}}}'
        assert "." not in canonical


class TestRustRuntimeRefusal:
    """DEC-RUST-STAGE3-REFINE-001: the math is wheel-hosted, no fallback."""

    def test_denormalize_refuses_loudly_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.refine_rust import REFINE_RUST_ENV, RefineRustUnavailableError

        monkeypatch.setenv(REFINE_RUST_ENV, "0")
        with pytest.raises(RefineRustUnavailableError, match="NO fallback"):
            MODE_A_SL_AXIS.denormalize(0.5)

    def test_normalize_refuses_loudly_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.engine.refine_rust import REFINE_RUST_ENV, RefineRustUnavailableError

        monkeypatch.setenv(REFINE_RUST_ENV, "0")
        with pytest.raises(RefineRustUnavailableError, match="NO fallback"):
            MODE_A_SL_AXIS.normalize(105)

    def test_axis_construction_is_wheel_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Validation is pure Python (the dashboard-boots-wheel-free law): a
        # disabled runtime still constructs + validates axes and still
        # REJECTS junk axes — only the bijection MATH needs the wheel.
        from brutex.engine.refine_rust import REFINE_RUST_ENV

        monkeypatch.setenv(REFINE_RUST_ENV, "0")
        axis = GridAxis(name="t", lo_paisa=5, hi_paisa=45, tick_paisa=10)
        assert axis.steps == 5
        with pytest.raises(Stage3GridError, match="lo_paisa must be > 0"):
            GridAxis(name="t", lo_paisa=0, hi_paisa=45, tick_paisa=5)

    def test_crate_validation_twin_matches_python_messages(self) -> None:
        # The crate re-validates axes per call with the SAME rules + message
        # text (grid_validate is the callable drift pin): every Python
        # __post_init__ refusal reproduces byte-identically through the wheel.
        import brutex_refine

        assert brutex_refine.grid_validate("t", 5, 45, 10) == 5
        for lo, hi, tick, fragment in [
            (0, 100, 5, "lo_paisa must be > 0"),
            (5, 100, 0, "tick_paisa must be > 0"),
            (100, 100, 5, "hi_paisa must be > lo_paisa"),
            (5, 201, 5, "not a whole"),
        ]:
            with pytest.raises(ValueError, match=fragment) as rust_exc:
                brutex_refine.grid_validate("t", lo, hi, tick)
            with pytest.raises(Stage3GridError, match=fragment) as py_exc:
                GridAxis(name="t", lo_paisa=lo, hi_paisa=hi, tick_paisa=tick)
            assert str(rust_exc.value) == str(py_exc.value)  # byte-identical text


class TestSweepGridConfigStage3FailFast:
    """The orchestrator's stage3 axis validation under the wheel-hosted math."""

    def _grid_kwargs(self) -> dict[str, object]:
        from brutex.engine.sweep_orchestrator import Timeframe
        from brutex.strategies.types import Side

        return {
            "instruments": ["NIFTY"],
            "timeframes": [Timeframe("1minute", 1)],
            "directions": [Side.BUY],
            "k_values": [1],
            "condition_bits": [1],
            "stop_losses": [50],
            "take_profits": [100],
            "proximities": [None],
            "mode": "A",
            "data_snapshot_hash": "ab" * 32,
            "indicator_version": "trading-core-0.0.0",
            "code_commit_sha": "c" * 40,
            "stage3_enable": True,
        }

    def test_enabled_grid_validates_axes_through_the_wheel(self) -> None:
        from brutex.engine.sweep_orchestrator import SweepGridConfig

        grid = SweepGridConfig(**self._grid_kwargs())  # type: ignore[arg-type]
        assert grid.stage3_enable is True

    def test_enabled_grid_without_runtime_is_a_clean_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DEC-RUST-STAGE3-REFINE-001: a stage3-enabled grid on a flag-off /
        # wheel-less box fails fast at CONSTRUCTION as the same clean
        # ValueError the CLI already surfaces — never a raw facade traceback.
        from brutex.engine.refine_rust import REFINE_RUST_ENV
        from brutex.engine.sweep_orchestrator import SweepGridConfig

        monkeypatch.setenv(REFINE_RUST_ENV, "0")
        with pytest.raises(ValueError, match="Stage-3 Rust runtime"):
            SweepGridConfig(**self._grid_kwargs())  # type: ignore[arg-type]
