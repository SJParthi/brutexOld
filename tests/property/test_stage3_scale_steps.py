"""Property battery — the scaled Stage-3 grid keeps EXACTLY the Mode-A shape.

DEC-STAGE3-SLTP-INSTRUMENT-SCALE-001 (LANE W3): over the WHOLE median-range
domain (and hostile span fractions) the derived axes hold the STRUCTURAL
invariants — 40 × 50 steps (the 2,000-cell Mode-A shape), ``lo == tick``,
``hi == steps·tick``, positive ticks, deterministic outputs, the
default-omit fragment law, and the coarse-ladder cell laws.
"""

from __future__ import annotations

from brutex.engine.stage3_grid import MODE_A_SL_AXIS, MODE_A_TP_AXIS
from brutex.engine.stage3_scale import (
    coarse_sltp_cells,
    derive_scaled_axes,
    instrument_axis_params_fragment,
    is_mode_a_axes,
)
from hypothesis import given
from hypothesis import strategies as st

_RANGES = st.integers(min_value=1, max_value=10**12)
_SPANS = st.tuples(
    st.integers(min_value=1, max_value=1000), st.integers(min_value=1, max_value=1000)
)


@given(median=_RANGES)
def test_default_spans_always_forty_by_fifty(median: int) -> None:
    sl_axis, tp_axis = derive_scaled_axes(median)
    assert sl_axis.steps == MODE_A_SL_AXIS.steps == 40
    assert tp_axis.steps == MODE_A_TP_AXIS.steps == 50
    assert sl_axis.steps * tp_axis.steps == 2000


@given(median=_RANGES, sl_span=_SPANS, tp_span=_SPANS)
def test_shape_and_grid_laws_over_hostile_spans(
    median: int, sl_span: tuple[int, int], tp_span: tuple[int, int]
) -> None:
    sl_axis, tp_axis = derive_scaled_axes(median, sl_span=sl_span, tp_span=tp_span)
    for axis, steps in ((sl_axis, 40), (tp_axis, 50)):
        assert axis.steps == steps
        assert axis.tick_paisa >= 1
        assert axis.lo_paisa == axis.tick_paisa  # the Mode-A shape lo == tick
        assert axis.hi_paisa == steps * axis.tick_paisa
        # The GridAxis bijection endpoints hold by construction.
        assert axis.denormalize(0.0) == axis.lo_paisa
        assert axis.denormalize(1.0) == axis.hi_paisa
        assert axis.normalize(axis.lo_paisa) == 0.0
        assert axis.normalize(axis.hi_paisa) == 1.0


@given(median=_RANGES)
def test_derivation_is_deterministic(median: int) -> None:
    assert derive_scaled_axes(median) == derive_scaled_axes(median)


@given(median=_RANGES)
def test_fragment_default_omit_law(median: int) -> None:
    """The fragment is {} EXACTLY when the derived pair IS the Mode-A grid."""
    sl_axis, tp_axis = derive_scaled_axes(median)
    fragment = instrument_axis_params_fragment(sl_axis, tp_axis)
    if is_mode_a_axes(sl_axis, tp_axis):
        assert fragment == {}
    else:
        assert set(fragment) == {"stage3_sl_axis", "stage3_tp_axis"}
        assert fragment["stage3_sl_axis"] == [
            sl_axis.lo_paisa,
            sl_axis.hi_paisa,
            sl_axis.tick_paisa,
        ]


@given(median=_RANGES)
def test_coarse_cells_laws(median: int) -> None:
    cells = coarse_sltp_cells(median)
    assert 1 <= len(cells) <= 5
    previous_sl = 0
    for sl, tp in cells:
        assert sl >= 1
        assert tp == 2 * sl  # the 2:1 W3-default reward:risk law
        assert sl > previous_sl  # strictly increasing after the dedup
        previous_sl = sl
