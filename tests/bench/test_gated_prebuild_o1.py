"""O(underlyings) bench for the gated pre-build orchestrator (DEC-PREBUILD-SELECT-001).

:func:`brutex.dashboard.gated_prebuild.run_gated_prebuild` is a SINGLE pass over
the selected underlyings per phase (spot materialise + futures discover/materialise
+ the options no-op), so the PER-UNDERLYING orchestration overhead is FLAT as the
selection scales. This gate measures the mean ns-per-underlying of the
orchestration ALONE (no-op injected callables) at a small and a large selection
and asserts the ratio holds the ceiling — a regression that made the orchestration
scale super-linearly (e.g. an O(n²) merge) blows it.

Honest envelope (Golden Rule #10): NOT an O(1)-TOTAL claim — total orchestration
work is O(underlyings) per phase (one pass). The gate proves the PER-UNDERLYING
overhead is constant; the real per-symbol materialise cost (a 1m read + O(bars)
resample) is gated separately by ``test_fno_resample_streaming_o1`` /
``test_tf_resampler_o1``.
"""

from __future__ import annotations

import time
from decimal import Decimal

from brutex.dashboard.gated_prebuild import run_gated_prebuild
from brutex.data.timeframe_materialize import MaterializeResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment

from tests.bench._o1_tiers import ratio_ceiling

N_SMALL = 100
N_BIG = 20_000


def _underlying(idx: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=idx,
        symbol_name=f"U{idx}",
        display_name=f"U{idx}",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _noop_spot(_inst: Instrument) -> MaterializeResult:
    return MaterializeResult(written={"5m": 1}, skipped=())


def _one_future(inst: Instrument) -> list[str]:
    return [f"{inst.symbol_name}-FUT"]


def _noop_future(_contract: str) -> MaterializeResult:
    return MaterializeResult(written={"5m": 1}, skipped=())


def _time_per_underlying(n: int) -> float:
    underlyings = [_underlying(i) for i in range(n)]
    run_gated_prebuild(  # warm up
        underlyings=underlyings[: min(64, n)],
        materialize_spot=_noop_spot,
        futures_contracts_for=_one_future,
        materialize_future=_noop_future,
    )
    t0 = time.perf_counter_ns()
    report = run_gated_prebuild(
        underlyings=underlyings,
        materialize_spot=_noop_spot,
        futures_contracts_for=_one_future,
        materialize_future=_noop_future,
    )
    t1 = time.perf_counter_ns()
    assert report.ok
    return (t1 - t0) / n


def test_orchestration_per_underlying_cost_is_flat() -> None:
    """Per-underlying gated-orchestration overhead holds the ceiling 100 → 20K."""
    t_small = _time_per_underlying(N_SMALL)
    t_big = _time_per_underlying(N_BIG)
    ratio = t_big / max(t_small, 1.0)

    assert ratio < ratio_ceiling(5.0), (
        f"gated pre-build per-underlying orchestration ratio {ratio:.2f}× "
        f"({N_BIG} vs {N_SMALL}) exceeds the CI ceiling — the orchestration may "
        f"have regressed away from a single O(underlyings) pass. "
        f"t_small={t_small:.2f}ns, t_big={t_big:.2f}ns."
    )


def test_options_phase_never_materializes() -> None:
    """The OPTIONS phase adds zero materialise calls (the no-op honesty)."""
    spot_calls = {"n": 0}

    def counting_spot(_inst: Instrument) -> MaterializeResult:
        spot_calls["n"] += 1
        return MaterializeResult(written={}, skipped=())

    underlyings = [_underlying(i) for i in range(30)]
    report = run_gated_prebuild(
        underlyings=underlyings,
        materialize_spot=counting_spot,
        futures_contracts_for=lambda _i: [],  # no futures → only spot materialises
        materialize_future=_noop_future,
    )
    # Spot materialised once per underlying; OPTIONS materialised nothing.
    assert spot_calls["n"] == len(underlyings)
    options = report.phases[-1]
    assert options.phase == "options"
    assert options.total_bars == 0
