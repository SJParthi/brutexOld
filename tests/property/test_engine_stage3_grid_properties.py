"""Property tests for the Stage-3 Mode-A grid bijection (`brutex.engine.stage3_grid`).

Invariants (hold on BOTH locked Mode-A axes — SL [5,200]/5, TP [10,500]/10):

* **Round-trip exactness** — ``denormalize(normalize(p)) == p`` for every grid
  point, with the recovered value an ``int`` (ADR-0007 condition 3: floats
  never reach the 9-field combo hash).
* **Bounds / totality** — ANY non-NaN float (inside [0,1], outside it, or
  ±inf) denormalizes to an in-range, on-tick int paisa; the function is total
  over its domain, never silently off-grid.
* **Monotonicity** — denormalize is non-decreasing in *u* (clamp, multiply by
  a positive constant, round, affine map are each order-preserving), and
  normalize is STRICTLY increasing across grid points (it is injective — the
  bijection's other half).
* **Nearest-snap** — the chosen grid index never sits more than half an index
  from the clamped coordinate's exact position ``u · (steps - 1)``.
* **Edges** — ``u=0.0`` → ``lo_paisa`` and ``u=1.0`` → ``hi_paisa`` exactly.
"""

from __future__ import annotations

import pytest
from brutex.engine.stage3_grid import MODE_A_SL_AXIS, MODE_A_TP_AXIS, GridAxis

pytest.importorskip("brutex_refine")  # the wheel hosts the bijection math
from hypothesis import given
from hypothesis import strategies as st

#: Both locked Mode-A axes — every invariant must hold on each.
_AXES: tuple[GridAxis, ...] = (MODE_A_SL_AXIS, MODE_A_TP_AXIS)

_axis = st.sampled_from(_AXES)

#: An (axis, on-grid index) pair — the index is drawn within THAT axis' steps.
_axis_and_index = _axis.flatmap(
    lambda axis: st.tuples(st.just(axis), st.integers(min_value=0, max_value=axis.steps - 1))
)

#: Coordinates inside the unit box.
_unit_u = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)

#: ANY non-NaN coordinate a (mis)behaving optimizer could propose — unbounded
#: magnitudes and ±inf included; NaN is the one rejected input (unit-tested).
_any_u = st.floats(allow_nan=False)


@given(pair=_axis_and_index)
def test_round_trip_is_identity_on_every_grid_point(pair: tuple[GridAxis, int]) -> None:
    axis, index = pair
    paisa = axis.lo_paisa + index * axis.tick_paisa
    u = axis.normalize(paisa)
    assert 0.0 <= u <= 1.0
    recovered = axis.denormalize(u)
    assert recovered == paisa
    assert type(recovered) is int


@given(axis=_axis, u=_unit_u)
def test_unit_box_lands_in_range_on_tick(axis: GridAxis, u: float) -> None:
    paisa = axis.denormalize(u)
    assert axis.lo_paisa <= paisa <= axis.hi_paisa
    assert (paisa - axis.lo_paisa) % axis.tick_paisa == 0


@given(axis=_axis, u=_any_u)
def test_any_non_nan_float_clamps_onto_the_grid(axis: GridAxis, u: float) -> None:
    paisa = axis.denormalize(u)
    assert axis.lo_paisa <= paisa <= axis.hi_paisa
    assert (paisa - axis.lo_paisa) % axis.tick_paisa == 0
    assert type(paisa) is int


@given(axis=_axis, u1=_any_u, u2=_any_u)
def test_denormalize_is_monotone_non_decreasing(axis: GridAxis, u1: float, u2: float) -> None:
    lo_u, hi_u = sorted((u1, u2))
    assert axis.denormalize(lo_u) <= axis.denormalize(hi_u)


@given(axis=_axis, u=_unit_u)
def test_snap_is_to_the_nearest_grid_index(axis: GridAxis, u: float) -> None:
    # round() never moves more than half an index away from the exact
    # (clamped) position u · (steps - 1).
    index = (axis.denormalize(u) - axis.lo_paisa) // axis.tick_paisa
    assert abs(index - u * (axis.steps - 1)) <= 0.5


@given(pair=_axis_and_index, offset=st.integers(min_value=1, max_value=49))
def test_normalize_is_strictly_increasing_across_grid_points(
    pair: tuple[GridAxis, int], offset: int
) -> None:
    axis, index = pair
    higher = min(index + offset, axis.steps - 1)
    if higher == index:
        return  # index was already the top of this axis — nothing above it
    lower_paisa = axis.lo_paisa + index * axis.tick_paisa
    higher_paisa = axis.lo_paisa + higher * axis.tick_paisa
    assert axis.normalize(lower_paisa) < axis.normalize(higher_paisa)


@pytest.mark.parametrize("axis", _AXES, ids=lambda a: a.name)
def test_edges_map_exactly_to_the_bounds(axis: GridAxis) -> None:
    assert axis.denormalize(0.0) == axis.lo_paisa
    assert axis.denormalize(1.0) == axis.hi_paisa
    assert axis.normalize(axis.lo_paisa) == 0.0
    assert axis.normalize(axis.hi_paisa) == 1.0
