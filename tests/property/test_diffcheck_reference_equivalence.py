"""Property — the R45 reference simulator equals production ``simulate_trades``.

THE independence check (design D-11 / WP-2 acceptance): the naive
reference implementation in :mod:`brutex.engine.sweep_diffcheck` and the
production walker (:func:`brutex.engine.trade_sim.simulate_trades`) are
two SEPARATE implementations of the same locked rule set — hypothesis
drives random bars / signal streams / SL-TP grids / directions /
timeframes across regular-session, forced-exit-boundary and
overnight-gap scenarios and asserts they agree field-for-field
(ints/datetimes exact) INCLUDING exit indices, plus the drawdown 4-tuple
bit-for-bit under both the production ``drawdown_compare_key`` and the
re-derived ``drawdown_bit_key``. The production modules are imported by
THIS TEST only — the reference module itself is AST-banned from them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.rust_metrics import drawdown_compare_key
from brutex.engine.sweep_diffcheck import (
    RefTrade,
    drawdown_bit_key,
    reference_drawdown,
    reference_simulate,
)
from brutex.engine.sweep_driver import build_equity_curve
from brutex.engine.trade_sim import simulate_trades_with_exit_indices
from brutex.metrics.compute import _drawdown_stats  # pyright: ignore[reportPrivateUsage]
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (as tests/property/test_engine_trade_sim_properties.py): the
# `@st.composite` strategies are invoked with no args because the decorator
# injects `draw` at runtime; pyright does not model the signature rewrite.

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_r45_reference_equals_production_field_for_field": ("R45",),
    "test_r45_reference_drawdown_equals_production_bit_for_bit": ("R45",),
}

#: Session anchors (all REAL NSE trading days — regular sessions).
#: 09:15 IST Tuesday 2024-01-02 — far from the forced-exit boundary.
_MORNING_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)
#: 15:10 IST — a 30-bar 1m feed straddles the 15:20 forced boundary.
_LATE_UTC = datetime(2024, 1, 2, 9, 40, tzinfo=UTC)
#: 14:55 IST day 1 + 09:15 IST day 2 — the overnight-gap scenario.
_DAY1_UTC = datetime(2024, 1, 2, 9, 25, tzinfo=UTC)
_DAY2_UTC = datetime(2024, 1, 3, 3, 45, tzinfo=UTC)

_PRICE_PAISA = st.integers(min_value=9_950, max_value=10_050)
_WICK_PAISA = st.integers(min_value=0, max_value=30)
_LEG_PAISA = st.one_of(st.none(), st.integers(min_value=5, max_value=60))
_TF_MINUTES = st.sampled_from([1, 5, 15])
_DIRECTIONS = st.sampled_from([Side.BUY, Side.SELL])

_START_EQUITY_PAISA = 10_000_000


def _rupees(paisa: int) -> Decimal:
    return Decimal(paisa) / Decimal(100)


def _bar_at(ts: datetime, open_p: int, close_p: int, up_wick: int, down_wick: int) -> CandleBar:
    high_p = max(open_p, close_p) + up_wick
    low_p = min(open_p, close_p) - down_wick
    return CandleBar(
        timestamp=ts,
        open=_rupees(open_p),
        high=_rupees(high_p),
        low=_rupees(low_p),
        close=_rupees(close_p),
        volume=0,
        open_interest=None,
    )


@st.composite
def _scenarios(
    draw: st.DrawFn,
) -> tuple[list[CandleBar], list[bool], Side, int, int | None, int | None]:
    """Draw (bars, entry_fires, direction, tf_minutes, sl, tp) across 3 shapes."""
    tf_minutes = draw(_TF_MINUTES)
    shape = draw(st.sampled_from(["morning", "late", "two_day"]))
    timestamps: list[datetime] = []
    if shape == "morning":
        n = draw(st.integers(min_value=2, max_value=30))
        timestamps = [_MORNING_UTC + timedelta(minutes=tf_minutes * i) for i in range(n)]
    elif shape == "late":
        # Straddles the 15:20 IST forced boundary (and beyond, coarser TFs).
        n = draw(st.integers(min_value=2, max_value=20))
        timestamps = [_LATE_UTC + timedelta(minutes=tf_minutes * i) for i in range(n)]
    else:  # two_day — the overnight-gap / same-session-gate scenario
        n1 = draw(st.integers(min_value=1, max_value=8))
        n2 = draw(st.integers(min_value=1, max_value=8))
        timestamps = [_DAY1_UTC + timedelta(minutes=tf_minutes * i) for i in range(n1)] + [
            _DAY2_UTC + timedelta(minutes=tf_minutes * i) for i in range(n2)
        ]
    bars = [
        _bar_at(ts, draw(_PRICE_PAISA), draw(_PRICE_PAISA), draw(_WICK_PAISA), draw(_WICK_PAISA))
        for ts in timestamps
    ]
    entry = draw(st.lists(st.booleans(), min_size=len(bars), max_size=len(bars)))
    direction = draw(_DIRECTIONS)
    sl = draw(_LEG_PAISA)
    tp = draw(_LEG_PAISA)
    return bars, entry, direction, tf_minutes, sl, tp


def _assert_field_for_field(
    prod: list[ClosedTrade], prod_exit_indices: list[int] | None, ref: list[RefTrade]
) -> None:
    assert len(prod) == len(ref)
    for p, r in zip(prod, ref, strict=True):
        # Int/datetime-only compare surface — exact equality, never approx.
        assert p.pnl_paisa == r.pnl_paisa
        assert p.mfe_paisa == r.mfe_paisa
        assert p.mae_paisa == r.mae_paisa
        assert p.holding_bars == r.holding_bars
        assert p.entry_ts == r.entry_ts
        assert p.exit_ts == r.exit_ts
    # The generated feeds are strictly increasing, so the production walker
    # CERTIFIES its exit indices — they must equal the reference's exactly.
    assert prod_exit_indices is not None
    assert prod_exit_indices == [r.exit_idx for r in ref]


@settings(max_examples=100, deadline=None)
@given(_scenarios())
def test_r45_reference_equals_production_field_for_field(
    scenario: tuple[list[CandleBar], list[bool], Side, int, int | None, int | None],
) -> None:
    """Reference == production trades, field-for-field, on any legal scenario."""
    bars, entry, direction, tf_minutes, sl, tp = scenario
    prod, exit_indices = simulate_trades_with_exit_indices(
        bars,
        entry,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
    )
    ref = reference_simulate(
        bars,
        entry,
        direction=direction.value,
        tf_minutes=tf_minutes,
        sl_paisa=sl,
        tp_paisa=tp,
    )
    _assert_field_for_field(prod, exit_indices, ref)


@settings(max_examples=100, deadline=None)
@given(_scenarios())
def test_r45_reference_drawdown_equals_production_bit_for_bit(
    scenario: tuple[list[CandleBar], list[bool], Side, int, int | None, int | None],
) -> None:
    """Reference drawdown fold == production equity-curve fold, bit-for-bit."""
    bars, entry, direction, tf_minutes, sl, tp = scenario
    prod, _ = simulate_trades_with_exit_indices(
        bars,
        entry,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
    )
    ref = reference_simulate(
        bars,
        entry,
        direction=direction.value,
        tf_minutes=tf_minutes,
        sl_paisa=sl,
        tp_paisa=tp,
    )
    prod_stats = _drawdown_stats(
        build_equity_curve(
            bars, prod, start_equity_paisa=_START_EQUITY_PAISA, tf_minutes=tf_minutes
        )
    )
    ref_stats = reference_drawdown(ref, n_bars=len(bars), start_equity_paisa=_START_EQUITY_PAISA)
    # Bit-exact under BOTH keys: the production burn-in key and the
    # reference-side re-derived key (their parity is itself unit-pinned).
    assert drawdown_compare_key(prod_stats) == drawdown_compare_key(ref_stats)
    assert drawdown_bit_key(prod_stats) == drawdown_bit_key(ref_stats)
