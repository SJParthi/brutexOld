"""Property tests for the Stage-2 Mode-A trade-sim walker (`brutex.engine.trade_sim`).

Invariants (hold for any single-session bar list + signals + Mode-A params):

* **Determinism** (Golden Rule #4) — same inputs always produce the same trades.
* **Intraday closure** (DEC-RISK-011 P-INTRA-1) — every trade's entry + exit fall
  on the same IST date, and the exit never lands after the 15:20 forced bar.
* **ClosedTrade well-formedness** — ``holding_bars >= 1``, ``mfe/mae >= 0``,
  ``exit_ts > entry_ts``, emitted in non-decreasing entry order.
* **No look-ahead** — appending bars strictly after a trade's exit fill bar never
  changes that trade (the walk reads only bars up to each decision/fill).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar
from brutex.engine.trade_sim import simulate_session_mode_a
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (mirrors tests/property/test_engine_stage1a_properties.py): the
# `@st.composite` strategies are invoked with no args; pyright does not model
# the `draw`-injection rewrite.

_IST = ZoneInfo("Asia/Kolkata")
_SESSION_OPEN = datetime(2024, 1, 2, 9, 15, tzinfo=_IST)
_FORCED_EXIT_IST = time(15, 20)


def _forced_ts(bars: list[CandleBar]) -> datetime | None:
    """The ts of the first bar at/after 15:20 IST (the forced-exit fill bar)."""
    for bar in bars:
        if bar.timestamp.astimezone(_IST).time() >= _FORCED_EXIT_IST:
            return bar.timestamp
    return None


def _bar_at(offset_min: int, low_p: int, span_p: int, oc: tuple[int, int]) -> CandleBar:
    """A valid CandleBar at ``09:15 + offset_min`` IST from integer-paise inputs."""
    low = low_p
    high = low_p + span_p
    open_p = low + (oc[0] % (span_p + 1))
    close_p = low + (oc[1] % (span_p + 1))
    ts = (_SESSION_OPEN + timedelta(minutes=offset_min)).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_p) / 100,
        high=Decimal(high) / 100,
        low=Decimal(low) / 100,
        close=Decimal(close_p) / 100,
        volume=0,
        open_interest=None,
    )


@st.composite
def _session(draw: st.DrawFn) -> tuple[list[CandleBar], list[int], Side, int, int]:
    """Draw a valid single-IST-session bar list + signals + direction + SL/TP."""
    offsets = sorted(
        draw(st.sets(st.integers(min_value=0, max_value=374), min_size=2, max_size=18))
    )
    bars: list[CandleBar] = []
    for off in offsets:
        low_p = draw(st.integers(min_value=1_000, max_value=2_000_000))  # ₹10..₹20000
        span_p = draw(st.integers(min_value=0, max_value=500))  # up to ₹5 range
        oc = (draw(st.integers(0, 500)), draw(st.integers(0, 500)))
        bars.append(_bar_at(off, low_p, span_p, oc))
    n = len(bars)
    signals = draw(st.lists(st.integers(min_value=0, max_value=n - 1), max_size=n, unique=True))
    direction = draw(st.sampled_from([Side.BUY, Side.SELL]))
    sl = draw(st.integers(min_value=1, max_value=2_000))
    tp = draw(st.integers(min_value=1, max_value=5_000))
    return bars, signals, direction, sl, tp


@settings(max_examples=200, deadline=None)
@given(_session())
def test_determinism(case: tuple[list[CandleBar], list[int], Side, int, int]) -> None:
    bars, signals, direction, sl, tp = case
    a = simulate_session_mode_a(
        bars, signal_bars=signals, direction=direction, sl_paisa=sl, tp_paisa=tp
    )
    b = simulate_session_mode_a(
        bars, signal_bars=signals, direction=direction, sl_paisa=sl, tp_paisa=tp
    )
    assert a == b


@settings(max_examples=300, deadline=None)
@given(_session())
def test_every_trade_is_well_formed_and_intraday(
    case: tuple[list[CandleBar], list[int], Side, int, int],
) -> None:
    bars, signals, direction, sl, tp = case
    trades = simulate_session_mode_a(
        bars, signal_bars=signals, direction=direction, sl_paisa=sl, tp_paisa=tp
    )
    forced_ts = _forced_ts(bars)
    prev_entry = None
    for trade in trades:
        assert trade.holding_bars >= 1
        assert trade.mfe_paisa >= 0
        assert trade.mae_paisa >= 0
        assert trade.exit_ts > trade.entry_ts
        # P-INTRA-1: same IST date; never holds past the forced-exit (15:20) bar.
        assert trade.entry_ts.astimezone(_IST).date() == trade.exit_ts.astimezone(_IST).date()
        if forced_ts is not None:
            assert trade.exit_ts <= forced_ts
        assert trade.direction is direction
        if prev_entry is not None:
            assert trade.entry_ts >= prev_entry  # entry order
        prev_entry = trade.entry_ts


@settings(max_examples=200, deadline=None)
@given(_session())
def test_no_look_ahead_on_first_trade(
    case: tuple[list[CandleBar], list[int], Side, int, int],
) -> None:
    # The first trade depends only on bars up to its exit fill; truncating the
    # session right after that fill bar must reproduce the identical first trade.
    bars, signals, direction, sl, tp = case
    full = simulate_session_mode_a(
        bars, signal_bars=signals, direction=direction, sl_paisa=sl, tp_paisa=tp
    )
    if not full:
        return
    first = full[0]
    # Find the exit fill bar index by timestamp, truncate inclusive of it.
    exit_idx = next(i for i, b in enumerate(bars) if b.timestamp == first.exit_ts)
    truncated = bars[: exit_idx + 1]
    kept_signals = [s for s in signals if s <= exit_idx]
    again = simulate_session_mode_a(
        truncated, signal_bars=kept_signals, direction=direction, sl_paisa=sl, tp_paisa=tp
    )
    assert again
    assert again[0] == first
