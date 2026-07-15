"""Detail-sink seam tests for :mod:`brutex.engine.trade_sim` (WP1, DEC-SWEEP-RESULTS-DEPTH-001).

The additive ``detail_sink`` must (a) capture the exit reason + exact fills for
every one of the five exit reasons, (b) stay positionally aligned with the
returned trades, and (c) leave the walker's output BYTE-IDENTICAL with or
without a sink — the golden-safety regression the design mandates alongside the
unmodified existing golden suite.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import (
    EXIT_REASONS,
    TradeDetail,
    simulate_trades,
    simulate_trades_with_exit_indices,
)
from brutex.strategies.types import Side

_DAY = date(2024, 1, 2)


def _ts(hh: int, mm: int) -> datetime:
    return datetime(_DAY.year, _DAY.month, _DAY.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=_ts(hh, mm),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


# The trade-sim golden's Fixture A (verbatim): TP → SL → forced-15:20 exits.
_A_BARS = [
    _bar(9, 15, "100", "100", "100", "100"),
    _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
    _bar(9, 17, "100.00", "100.60", "99.90", "100.40"),
    _bar(9, 18, "100", "100", "100", "100"),
    _bar(9, 19, "100.00", "100.05", "99.90", "99.95"),
    _bar(9, 20, "100.00", "100.10", "99.60", "99.70"),
    _bar(15, 17, "100", "100", "100", "100"),
    _bar(15, 18, "100.00", "100.20", "99.80", "100.10"),
    _bar(15, 19, "100.00", "100.30", "99.90", "100.20"),
    _bar(15, 20, "100.15", "101.00", "100.00", "100.50"),
]
_A_SIGNALS = [True, False, False, True, False, False, True, False, False, False]


def test_exit_reasons_vocabulary_is_the_contract_set() -> None:
    # APPEND-ONLY: session_gap joined LAST (DEC-TRADESIM-OPEN-SESSION-GUARD-001);
    # the first five members are byte-frozen in this order (P3→P4 wire contract).
    assert EXIT_REASONS == (
        "stop_loss",
        "take_profit",
        "forced_1520",
        "signal",
        "end_of_data",
        "session_gap",
    )


def test_sink_captures_tp_sl_forced_reasons_and_fills() -> None:
    sink: list[TradeDetail] = []
    trades = simulate_trades(
        _A_BARS,
        _A_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=30,
        take_profit_paisa=50,
        detail_sink=sink,
    )
    assert [d.exit_reason for d in sink] == ["take_profit", "stop_loss", "forced_1520"]
    # Positional alignment: detail i annotates trade i (the SAME object).
    assert [d.trade for d in sink] == trades
    assert all(d.trade is t for d, t in zip(sink, trades, strict=True))
    # Exact fills: every entry at 100.00; TP at level 100.50; SL at level
    # 99.70; forced at the 15:20 bar's OPEN 100.15.
    assert [d.entry_fill_paisa for d in sink] == [10_000, 10_000, 10_000]
    assert [d.exit_fill_paisa for d in sink] == [10_050, 9_970, 10_015]


def test_sink_captures_signal_exit() -> None:
    bars = [
        _bar(9, 15, "100", "100", "100", "100"),
        _bar(9, 16, "100.00", "100.20", "99.50", "100.10"),
        _bar(9, 17, "100.00", "100.20", "99.50", "100.10"),
        _bar(9, 18, "99.70", "100.00", "99.60", "99.90"),
    ]
    sink: list[TradeDetail] = []
    simulate_trades(
        bars,
        [True, False, False, False],
        direction=Side.SELL,
        tf_minutes=1,
        exit_signals=[False, False, True, False],
        detail_sink=sink,
    )
    assert [d.exit_reason for d in sink] == ["signal"]
    assert sink[0].entry_fill_paisa == 10_000
    assert sink[0].exit_fill_paisa == 9_970  # next bar's OPEN
    assert sink[0].trade.pnl_paisa == 30  # short profits as price falls


def test_sink_captures_end_of_data_exit() -> None:
    bars = [
        _bar(9, 15, "100", "100", "100", "100"),
        _bar(9, 16, "100.00", "100.20", "99.80", "100.05"),
        _bar(9, 17, "100.00", "100.10", "99.90", "99.95"),
    ]
    sink: list[TradeDetail] = []
    simulate_trades(bars, [True, False, False], direction=Side.BUY, tf_minutes=1, detail_sink=sink)
    assert [d.exit_reason for d in sink] == ["end_of_data"]
    assert sink[0].exit_fill_paisa == 9_995  # last bar's CLOSE


def test_sink_captures_session_gap_exit() -> None:
    """DEC-TRADESIM-OPEN-SESSION-GUARD-001: the boundary close is a named detail.

    A position still open when the feed steps to the NEXT IST session date
    (day-1's stored tail never reached a forced-exit bar) closes retroactively
    at day-1's last stored bar's CLOSE — ``exit_reason == "session_gap"``,
    ``exit_fill_paisa`` = the PRIOR session's close, never a next-session fill.
    """
    next_day = date(2024, 1, 3)
    gap_ts = datetime(next_day.year, next_day.month, next_day.day, 9, 15, tzinfo=IST).astimezone(
        UTC
    )
    bars = [
        _bar(11, 10, "100", "100", "100", "100"),
        _bar(11, 11, "100.00", "100.20", "99.80", "100.05"),
        _bar(11, 12, "100.00", "100.10", "99.90", "99.95"),  # day-1's LAST stored bar
        CandleBar(
            timestamp=gap_ts,
            open=Decimal("600"),
            high=Decimal("601"),
            low=Decimal("599"),
            close=Decimal("600"),
            volume=0,
            open_interest=None,
        ),
    ]
    sink: list[TradeDetail] = []
    trades = simulate_trades(
        bars, [True, False, False, False], direction=Side.BUY, tf_minutes=1, detail_sink=sink
    )
    assert [d.exit_reason for d in sink] == ["session_gap"]
    assert sink[0].exit_fill_paisa == 9_995  # day-1's last stored CLOSE — not the gap bar
    assert sink[0].trade is trades[0]
    assert trades[0].pnl_paisa == 9_995 - 10_000
    assert trades[0].exit_ts == bars[2].timestamp


def test_output_identical_with_and_without_sink() -> None:
    """The golden-safety regression: a sink can never perturb the trades."""
    without = simulate_trades(
        _A_BARS,
        _A_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=30,
        take_profit_paisa=50,
    )
    sink: list[TradeDetail] = []
    with_sink = simulate_trades(
        _A_BARS,
        _A_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=30,
        take_profit_paisa=50,
        detail_sink=sink,
    )
    assert with_sink == without
    assert len(sink) == len(with_sink)


def test_exit_indices_wrapper_threads_sink() -> None:
    sink: list[TradeDetail] = []
    trades, exit_indices = simulate_trades_with_exit_indices(
        _A_BARS,
        _A_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=30,
        take_profit_paisa=50,
        detail_sink=sink,
    )
    assert exit_indices is not None
    assert [d.trade for d in sink] == trades


def test_exit_fill_arithmetic_invariant() -> None:
    """pnl == exit − entry (BUY) — the belt trade_capture re-derives against."""
    sink: list[TradeDetail] = []
    simulate_trades(
        _A_BARS,
        _A_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=30,
        take_profit_paisa=50,
        detail_sink=sink,
    )
    for detail in sink:
        assert detail.trade.pnl_paisa == detail.exit_fill_paisa - detail.entry_fill_paisa
