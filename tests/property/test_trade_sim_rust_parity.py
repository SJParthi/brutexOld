"""Property: the Rust trade-sim seam is byte-identical to the Python oracle.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). Over hypothesis-generated bar
feeds (valid OHLC, varied start times so some windows cross the 15:20 forced
exit), masks, SL/TP distances, directions and fill policies, the resident Rust
kernel :class:`brutex.engine.rust_trade_sim.ResidentTradeSim` must emit exactly
the same :class:`~brutex.metrics.types.ClosedTrade` lists as
:func:`brutex.engine.trade_sim.simulate_trades` (Golden Rule #4). This is the
look-ahead-free, determinism, and bit-exactness invariant for the kernel.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.trade_sim import FillPolicy, LevelFill, SameBarTie, simulate_trades
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.property

_DAY = date(2024, 1, 2)
# Start IST times: 09:15 (no forced bars within ~1h) + 15:16 (windows quickly
# cross the 15:20 forced exit), so the forced-exit branch is exercised too.
_START_TIMES = [(9, 15), (15, 16)]


@st.composite
def _feed_and_params(
    draw: st.DrawFn,
) -> tuple[list[CandleBar], list[int], Side, int | None, int | None, FillPolicy]:
    """Generate (bars, masks, direction, sl_paisa, tp_paisa, policy)."""
    n = draw(st.integers(min_value=1, max_value=30))
    start_h, start_m = draw(st.sampled_from(_START_TIMES))
    bars: list[CandleBar] = []
    for i in range(n):
        low = draw(st.integers(min_value=9000, max_value=11000))
        high = low + draw(st.integers(min_value=0, max_value=2000))
        o = draw(st.integers(min_value=low, max_value=high))
        c = draw(st.integers(min_value=low, max_value=high))
        # IST wall-clock marching by one minute; stored UTC (bars are UTC).
        minute_total = start_h * 60 + start_m + i
        hh, mm = divmod(minute_total, 60)
        ts = datetime(_DAY.year, _DAY.month, _DAY.day, hh % 24, mm, tzinfo=IST).astimezone(UTC)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(o) / Decimal(100),
                high=Decimal(high) / Decimal(100),
                low=Decimal(low) / Decimal(100),
                close=Decimal(c) / Decimal(100),
                volume=0,
                open_interest=None,
            )
        )
    direction = draw(st.sampled_from([Side.BUY, Side.SELL]))
    sl = draw(st.none() | st.integers(min_value=1, max_value=4000))
    tp = draw(st.none() | st.integers(min_value=1, max_value=4000))
    policy = FillPolicy(
        stop_loss=draw(st.sampled_from([LevelFill.AT_LEVEL, LevelFill.NEXT_OPEN])),
        take_profit=draw(st.sampled_from([LevelFill.AT_LEVEL, LevelFill.NEXT_OPEN])),
        same_bar=draw(st.sampled_from([SameBarTie.SL_FIRST, SameBarTie.TP_FIRST])),
    )
    # Masks drawn from a small bit space so they actually fire on the feed.
    masks = draw(
        st.lists(st.integers(min_value=0, max_value=63), min_size=1, max_size=6, unique=True)
    )
    return bars, masks, direction, sl, tp, policy


@given(case=_feed_and_params())
@settings(max_examples=200, deadline=None)
def test_rust_seam_equals_python_oracle(
    case: tuple[list[CandleBar], list[int], Side, int | None, int | None, FillPolicy],
) -> None:
    bars, masks, direction, sl, tp, policy = case
    bar_bits = build_bar_bits(bars)
    sim = ResidentTradeSim(bars, bar_bits, tf_minutes=1)
    got = sim.simulate_masks(
        masks, direction=direction, stop_loss_paisa=sl, take_profit_paisa=tp, policy=policy
    )
    expected = [
        simulate_trades(
            bars,
            mask_to_entry_signals(bar_bits, m),
            direction=direction,
            tf_minutes=1,
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
            policy=policy,
        )
        for m in masks
    ]
    assert got == expected
