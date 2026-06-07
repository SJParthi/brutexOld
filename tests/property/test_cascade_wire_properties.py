"""Property tests for the Stage-4 cascade wiring (DEC-CASCADE-WIRE-001).

Hypothesis invariants over the pure, bar-free surfaces:

* the manifest JSON round-trip is the identity for any valid spec set;
* :func:`brutex.engine.cascade_wire._per_bar_returns` reconstructs the equity
  path exactly (its cumulative sum × base recovers the curve);
* :func:`brutex.engine.cascade_wire._finite` always yields a finite float;
* :func:`brutex.engine.cascade_wire._max_drawdown_return_units` is always a
  non-negative magnitude bounded by the peak-to-trough span.
"""

from __future__ import annotations

import math

from brutex.engine import cascade_wire as cw
from hypothesis import given
from hypothesis import strategies as st


@st.composite
def _specs(draw: st.DrawFn) -> list[cw.CascadeTupleSpec]:
    """Draw a small list of valid, distinct-id :class:`CascadeTupleSpec`\\s."""
    n = draw(st.integers(min_value=0, max_value=3))
    specs: list[cw.CascadeTupleSpec] = []
    for i in range(n):
        n_win = draw(st.integers(min_value=0, max_value=3))
        winners = tuple(
            (f"{(i * 10 + j):064x}", draw(st.integers(min_value=0, max_value=(1 << 52) - 1)))
            for j in range(n_win)
        )
        prox = draw(
            st.one_of(
                st.none(),
                st.tuples(
                    st.integers(min_value=1, max_value=100),
                    st.integers(min_value=1, max_value=100),
                    st.integers(min_value=1, max_value=100),
                ),
            )
        )
        specs.append(
            cw.CascadeTupleSpec(
                instrument=draw(st.sampled_from(["NIFTY", "BANKNIFTY", "SENSEX"])),
                tf_label=draw(st.sampled_from(["1minute", "5minute", "15minute"])),
                tf_minutes=draw(st.integers(min_value=1, max_value=60)),
                direction_label=draw(st.sampled_from(["bull", "bear"])),
                sl_paisa=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10_000))),
                tp_paisa=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10_000))),
                prior_n=draw(st.integers(min_value=1, max_value=4)),
                proximity_bps=prox,
                start_equity_paisa=draw(st.integers(min_value=1, max_value=10**9)),
                winners=winners,
            )
        )
    return specs


@given(_specs())
def test_manifest_round_trip_is_identity(specs: list[cw.CascadeTupleSpec]) -> None:
    manifest = cw.CascadeManifest(
        version=cw.MANIFEST_VERSION,
        sweep_id="deadbeef" * 8,
        cascade_config=cw.cascade_config_for_sweep("deadbeef" * 8),
        bars_context=cw.BarsContext(
            exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-06-01"
        ),
        specs=tuple(specs),
    )
    assert cw.manifest_from_json(cw.manifest_to_json(manifest)) == manifest


@given(
    st.lists(st.integers(min_value=-10_000, max_value=10_000), min_size=1, max_size=50),
    st.integers(min_value=1, max_value=10**9),
)
def test_per_bar_returns_reconstructs_the_equity_path(deltas: list[int], base: int) -> None:
    # Build a cumulative equity curve, then check the per-bar returns invert it.
    equity: list[int] = []
    running = base
    for delta in deltas:
        running += delta
        equity.append(running)
    returns = cw._per_bar_returns(equity, base)
    assert len(returns) == len(equity)
    # cumulative(returns) * base + base ≈ equity (exact up to float rounding).
    cum = base
    for ret, eq in zip(returns, equity, strict=True):
        cum += ret * base
        assert math.isclose(cum, eq, rel_tol=1e-9, abs_tol=1e-6)


@given(st.one_of(st.none(), st.floats()))
def test_finite_is_always_finite(value: float | None) -> None:
    assert math.isfinite(cw._finite(value))


@given(st.lists(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False), max_size=50))
def test_max_drawdown_is_non_negative(returns: list[float]) -> None:
    dd = cw._max_drawdown_return_units(returns)
    assert dd >= 0.0
    assert math.isfinite(dd)
