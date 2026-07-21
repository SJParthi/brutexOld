"""Property tests for the 1m-execution-grid trade-sim walker (`brutex.engine.trade_sim`).

Invariants (hold for any chronologically-ordered dual feed + signal stream —
here the tf==1 degenerate where the SAME 1m sequence is decision AND
execution stream, with optional HOLES, a session boundary, and the forced
15:20 window swept by the generator):

* **Determinism** (Golden Rule #4) — the same bars + signals + config always
  produce the same trades / exit indices / details, bit-for-bit; both batch
  wrappers agree.
* **Structural trade invariants** — exit indices strictly increasing and
  positionally aligned; ``entry_idx = exit_idx − holding + 1 >= 0``;
  ``bars[entry_idx].timestamp == entry_ts`` and
  ``bars[exit_idx].timestamp == exit_ts`` (the exit-index certificate);
  trades never overlap (the next trade's entry bar is strictly after the
  previous trade's exit bar); the detail fill arithmetic
  ``pnl == ±(exit_fill − entry_fill)``.
* **The §2.4 deferral law** — an SL/TP/signal/forced exit NEVER resolves on
  the trade's own entry 1m bar (``holding_bars >= 2``); ``holding_bars == 1``
  happens ONLY for the legalized ``end_of_data`` / ``session_gap`` terminal
  degenerate, priced at the entry bar's CLOSE — the **B3 dominance pin**:
  ``|pnl| <= high − low`` of the entry bar (the close lies within the bar's
  range, so no full-range phantom loss can re-enter through a price-basis
  regression; design §3.12).
* **No look-ahead** (design §3b.4) — every entry fill bar is an armed map
  bar whose decision source carries a live signal, and its timestamp is
  at-or-after the decision bar's close instant (``label + tf``); truncating
  the dual stream after bar ``k`` never changes a trade already emitted on
  bars ``0..k`` (dual-stream suffix independence — the map is prefix-stable).
* **Streaming ≡ batch** — driving ``push_1m`` manually from the map's
  ``arm_source`` projections reproduces ``run()`` exactly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.exec_grid import build_exec_grid_map
from brutex.engine.trade_sim import (
    TradeDetail,
    TradeSimWalker,
    simulate_trades,
    simulate_trades_with_exit_indices,
)
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

_DAY = date(2024, 1, 2)  # a regular NSE trading Tuesday
_NEXT_DAY = date(2024, 1, 3)  # the next NSE session (Wednesday)

#: The two exit classes whose terminal same-bar (holding == 1) close is
#: LEGALIZED by the B3 carve-out (design §3.12).
_TERMINAL_REASONS = frozenset({"end_of_data", "session_gap"})


def _bar_at(ts: datetime, o: int, h: int, lo: int, c: int) -> CandleBar:
    """Build a bar from int-paisa prices (exact 2dp Decimals — round-trip safe)."""
    return CandleBar(
        timestamp=ts,
        open=Decimal(o) / 100,
        high=Decimal(h) / 100,
        low=Decimal(lo) / 100,
        close=Decimal(c) / 100,
        volume=0,
        open_interest=None,
    )


def _ist(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


@st.composite
def _ohlc_paisa(draw: st.DrawFn) -> tuple[int, int, int, int]:
    """One valid OHLC quadruple in int paisa (low <= open, close <= high)."""
    o = draw(st.integers(min_value=9_000, max_value=11_000))
    c = draw(st.integers(min_value=9_000, max_value=11_000))
    up = draw(st.integers(min_value=0, max_value=300))
    dn = draw(st.integers(min_value=0, max_value=300))
    return o, max(o, c) + up, min(o, c) - dn, c


@st.composite
def _scenario(
    draw: st.DrawFn,
) -> tuple[list[CandleBar], list[bool], list[bool] | None, Side, int | None, int | None]:
    """A dual-feed tf==1 scenario: bars (holes / session boundary / forced
    window possible), entry + optional exit signals, direction, SL/TP."""
    shape = draw(st.sampled_from(["single", "two_day", "forced"]))
    minutes: list[datetime] = []
    if shape == "single":
        n = draw(st.integers(min_value=2, max_value=24))
        minutes = [_ist(_DAY, 11, 0) + timedelta(minutes=m) for m in range(n)]
    elif shape == "two_day":
        n1 = draw(st.integers(min_value=1, max_value=12))
        n2 = draw(st.integers(min_value=1, max_value=12))
        minutes = [_ist(_DAY, 11, 0) + timedelta(minutes=m) for m in range(n1)]
        minutes += [_ist(_NEXT_DAY, 9, 15) + timedelta(minutes=m) for m in range(n2)]
    else:  # forced — a window crossing the 15:20 IST boundary
        n = draw(st.integers(min_value=2, max_value=18))
        minutes = [_ist(_DAY, 15, 10) + timedelta(minutes=m) for m in range(n)]
    # Punch up to two interior HOLES (never the first bar) — slid fills +
    # B4 collisions become reachable.
    if len(minutes) > 2:
        holes = draw(st.sets(st.integers(min_value=1, max_value=len(minutes) - 2), max_size=2))
        minutes = [ts for m, ts in enumerate(minutes) if m not in holes]
    bars = [_bar_at(ts, *draw(_ohlc_paisa())) for ts in minutes]
    n_bars = len(bars)
    entry = draw(st.lists(st.booleans(), min_size=n_bars, max_size=n_bars))
    exits = draw(
        st.one_of(
            st.none(),
            st.lists(st.booleans(), min_size=n_bars, max_size=n_bars),
        )
    )
    direction = draw(st.sampled_from([Side.BUY, Side.SELL]))
    sl = draw(st.sampled_from([None, 10, 50, 200]))
    tp = draw(st.sampled_from([None, 10, 50, 200]))
    return bars, entry, exits, direction, sl, tp


def _run(
    bars: list[CandleBar],
    entry: list[bool],
    exits: list[bool] | None,
    direction: Side,
    sl: int | None,
    tp: int | None,
) -> tuple[list[ClosedTrade], list[int] | None, list[TradeDetail]]:
    sink: list[TradeDetail] = []
    trades, idx = simulate_trades_with_exit_indices(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exits,
        detail_sink=sink,
    )
    return trades, idx, sink


@given(_scenario())
@settings(max_examples=60, deadline=None)
def test_determinism_and_wrapper_agreement(
    scenario: tuple[list[CandleBar], list[bool], list[bool] | None, Side, int | None, int | None],
) -> None:
    bars, entry, exits, direction, sl, tp = scenario
    first_trades, first_idx, first_sink = _run(bars, entry, exits, direction, sl, tp)
    second_trades, second_idx, second_sink = _run(bars, entry, exits, direction, sl, tp)
    assert first_trades == second_trades
    assert first_idx == second_idx
    assert [(d.exit_reason, d.entry_fill_paisa, d.exit_fill_paisa) for d in first_sink] == [
        (d.exit_reason, d.entry_fill_paisa, d.exit_fill_paisa) for d in second_sink
    ]
    # The plain wrapper rides the SAME walk — byte-identical trades.
    plain = simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exits,
    )
    assert plain == first_trades


@given(_scenario())
@settings(max_examples=60, deadline=None)
def test_structural_invariants_and_the_deferral_law(
    scenario: tuple[list[CandleBar], list[bool], list[bool] | None, Side, int | None, int | None],
) -> None:
    bars, entry, exits, direction, sl, tp = scenario
    trades, idx, sink = _run(bars, entry, exits, direction, sl, tp)
    # Contiguous-minus-holes minutes are strictly increasing → certified.
    assert idx is not None
    assert len(idx) == len(trades) == len(sink)
    assert idx == sorted(set(idx))  # strictly increasing exit indices
    prev_exit_idx = -1
    for trade, exit_idx, detail in zip(trades, idx, sink, strict=True):
        assert trade.direction is direction
        assert trade.holding_bars >= 1
        assert trade.mfe_paisa >= 0
        assert trade.mae_paisa >= 0
        entry_idx = exit_idx - trade.holding_bars + 1
        assert entry_idx >= 0
        # The exit-index certificate + the entry-bar identity.
        assert bars[entry_idx].timestamp == trade.entry_ts
        assert bars[exit_idx].timestamp == trade.exit_ts
        # Non-overlap: the next trade's entry bar is strictly after the
        # previous trade's exit bar (the exit bar is consumed).
        assert entry_idx > prev_exit_idx
        prev_exit_idx = exit_idx
        # Detail fill arithmetic (the trade_capture re-derivation belt).
        if direction is Side.BUY:
            assert trade.pnl_paisa == detail.exit_fill_paisa - detail.entry_fill_paisa
        else:
            assert trade.pnl_paisa == detail.entry_fill_paisa - detail.exit_fill_paisa
        # §2.4: an SL/TP/signal/forced exit NEVER resolves on the entry bar.
        if detail.exit_reason not in _TERMINAL_REASONS:
            assert trade.holding_bars >= 2
        if trade.holding_bars == 1:
            # The legalized B3 terminal degenerate: end_of_data/session_gap
            # only, priced at the entry bar's CLOSE — dominance: the close
            # lies within [low, high], so |pnl| is bounded by the bar range.
            assert detail.exit_reason in _TERMINAL_REASONS
            entry_bar = bars[entry_idx]
            bar_range = int((entry_bar.high - entry_bar.low) * 100)
            assert abs(trade.pnl_paisa) <= bar_range


@given(_scenario(), st.data())
@settings(max_examples=60, deadline=None)
def test_suffix_independence_truncation_prefix(
    scenario: tuple[list[CandleBar], list[bool], list[bool] | None, Side, int | None, int | None],
    data: st.DataObject,
) -> None:
    """Truncating the dual stream after bar ``k`` never changes a trade
    already emitted on bars ``0..k`` (the map is prefix-stable, the walker is
    forward-only). The truncated run's trades — minus a trailing finish()
    ``end_of_data`` close of a still-open position — are a strict PREFIX of
    the full run's trades."""
    bars, entry, exits, direction, sl, tp = scenario
    full_trades, _, _ = _run(bars, entry, exits, direction, sl, tp)
    k = data.draw(st.integers(min_value=0, max_value=len(bars) - 1), label="k")
    t_bars = bars[: k + 1]
    t_entry = entry[: k + 1]
    t_exits = None if exits is None else exits[: k + 1]
    t_trades, _, t_sink = _run(t_bars, t_entry, t_exits, direction, sl, tp)
    if t_sink and t_sink[-1].exit_reason == "end_of_data":
        t_trades = t_trades[:-1]  # the finish() close of a still-open position
    assert full_trades[: len(t_trades)] == t_trades


@given(_scenario())
@settings(max_examples=60, deadline=None)
def test_no_lookahead_every_entry_fill_is_an_armed_signal_bar(
    scenario: tuple[list[CandleBar], list[bool], list[bool] | None, Side, int | None, int | None],
) -> None:
    bars, entry, exits, direction, sl, tp = scenario
    trades, idx, _ = _run(bars, entry, exits, direction, sl, tp)
    assert idx is not None
    exec_map = build_exec_grid_map(bars, bars, tf_minutes=1)
    for trade, exit_idx in zip(trades, idx, strict=True):
        entry_idx = exit_idx - trade.holding_bars + 1
        src = exec_map.arm_source[entry_idx]
        assert src >= 0  # the entry bar is an ARMED map bar
        assert entry[src]  # ... whose decision source carried a live signal
        # No look-ahead: the fill instant is at-or-after the decision bar's
        # close instant (label + tf_minutes) — the signal is fully formed
        # strictly before the fill bar's first trade.
        assert bars[entry_idx].timestamp >= bars[src].timestamp + timedelta(minutes=1)


@given(_scenario())
@settings(max_examples=60, deadline=None)
def test_streaming_push_matches_batch_run(
    scenario: tuple[list[CandleBar], list[bool], list[bool] | None, Side, int | None, int | None],
) -> None:
    """The batch path IS the streaming path + the map projection."""
    bars, entry, exits, direction, sl, tp = scenario
    batch = simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exits,
    )
    exec_map = build_exec_grid_map(bars, bars, tf_minutes=1)
    walker = TradeSimWalker(
        direction=direction, tf_minutes=1, stop_loss_paisa=sl, take_profit_paisa=tp
    )
    streamed: list[ClosedTrade] = []
    for j, bar in enumerate(bars):
        src = exec_map.arm_source[j]
        entry_due = src >= 0 and bool(entry[src])
        exit_due = src >= 0 and exits is not None and bool(exits[src])
        closed = walker.push_1m(bar, entry_due=entry_due, exit_due=exit_due)
        if closed is not None:
            streamed.append(closed)
    tail = walker.finish()
    if tail is not None:
        streamed.append(tail)
    assert streamed == batch
