"""Detail-sink seam tests for :mod:`brutex.engine.trade_sim` (WP1, DEC-SWEEP-RESULTS-DEPTH-001).

The additive ``detail_sink`` must (a) capture the exit reason + exact fills for
every one of the six exit reasons, (b) stay positionally aligned with the
returned trades, and (c) leave the walker's output BYTE-IDENTICAL with or
without a sink. Under DEC-FILL-EXEC-GRID-1M-001 the :class:`TradeDetail` SHAPE
and the exit-reason vocabulary are UNCHANGED (the lane W6 contract) — the fill
prices are now 1-MINUTE bar prices. Every expected fill is hand-derived in its
test's comment (tf==1 degenerate: the same bars are decision AND execution
stream, so a signal on bar N fills at bar N+1 — the historical next-bar law).
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


def _ts(hh: int, mm: int, *, day: date = _DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str, *, day: date = _DAY) -> CandleBar:
    return CandleBar(
        timestamp=_ts(hh, mm, day=day),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


def _sim(
    bars: list[CandleBar],
    signals: list[bool],
    *,
    direction: Side = Side.BUY,
    sl: int | None = None,
    tp: int | None = None,
    exits: list[bool] | None = None,
    sink: list[TradeDetail] | None = None,
) -> list[object]:
    """tf==1 degenerate batch: the SAME bars are decision AND execution stream."""
    return simulate_trades(
        bars,
        signals,
        direction=direction,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exits,
        detail_sink=sink,
    )


# The trade-sim golden's Fixture A (verbatim): TP → SL → forced-15:20 exits.
# Hand-derivation under the 1m-grid law (entry-bar ranges never touch a level,
# so no §2.4 deferral fires in this fixture):
#   T1: signal@09:15 → entry @09:16 HIGH 100.10; TP level 100.60; @09:17 high
#       100.60 fires → fill min(10060, low 9990) = 9990 → pnl −20.
#   T2: signal@09:18 → entry @09:19 HIGH 100.05; SL level 99.75; @09:20 low
#       99.60 breaches → fill min(9975, 9960) = 9960 → pnl −45.
#   T3: signal@15:17 → entry @15:18 HIGH 100.20; @15:20 forced → fill at the
#       forced bar's adverse LOW 100.00 → pnl −20.
_A_BARS = [
    _bar(9, 15, "100", "100", "100", "100"),
    _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
    _bar(9, 17, "100.00", "100.60", "99.90", "100.40"),
    _bar(9, 18, "100", "100", "100", "100"),
    _bar(9, 19, "100.00", "100.05", "99.90", "99.95"),
    _bar(9, 20, "100.00", "100.10", "99.60", "99.70"),
    _bar(15, 17, "100", "100", "100", "100"),
    _bar(15, 18, "100.00", "100.20", "99.95", "100.10"),  # low ABOVE SL 99.90
    _bar(15, 19, "100.00", "100.30", "99.95", "100.20"),  # low ABOVE SL 99.90
    _bar(15, 20, "100.15", "101.00", "100.00", "100.50"),
]
_A_SIGNALS = [True, False, False, True, False, False, True, False, False, False]


def test_exit_reasons_vocabulary_is_the_contract_set() -> None:
    # APPEND-ONLY (lane W6 contract): session_gap joined LAST; the first five
    # members are byte-frozen in this order (P3→P4 wire contract).
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
    trades = _sim(_A_BARS, _A_SIGNALS, sl=30, tp=50, sink=sink)
    assert [d.exit_reason for d in sink] == ["take_profit", "stop_loss", "forced_1520"]
    # Positional alignment: detail i annotates trade i (the SAME object).
    assert [d.trade for d in sink] == trades
    assert all(d.trade is t for d, t in zip(sink, trades, strict=True))
    # Exact fills under the one worst-case law on the 1m grid: every BUY
    # entry at its fill bar's HIGH; the TP touch fills at the trigger bar's
    # LOW 99.90; the SL breach at the LOW 99.60; the forced 15:20 exit at
    # that bar's adverse LOW 100.00.
    assert [d.entry_fill_paisa for d in sink] == [10_010, 10_005, 10_020]
    assert [d.exit_fill_paisa for d in sink] == [9_990, 9_960, 10_000]


def test_sink_captures_signal_exit() -> None:
    # SELL entry @09:16 LOW 99.50; exit signal on the 09:17 decision bar →
    # fill @09:18 at that bar's adverse HIGH 100.00 → pnl 9950 − 10000 = −50.
    bars = [
        _bar(9, 15, "100", "100", "100", "100"),
        _bar(9, 16, "100.00", "100.20", "99.50", "100.10"),
        _bar(9, 17, "100.00", "100.20", "99.50", "100.10"),
        _bar(9, 18, "99.70", "100.00", "99.60", "99.90"),
    ]
    sink: list[TradeDetail] = []
    _sim(
        bars,
        [True, False, False, False],
        direction=Side.SELL,
        exits=[False, False, True, False],
        sink=sink,
    )
    assert [d.exit_reason for d in sink] == ["signal"]
    assert sink[0].entry_fill_paisa == 9_950  # SELL entry at the bar LOW
    assert sink[0].exit_fill_paisa == 10_000  # exit-fill bar's adverse HIGH
    assert sink[0].trade.pnl_paisa == -50  # worst-case: sold low, covered high


def test_sink_captures_end_of_data_exit() -> None:
    bars = [
        _bar(9, 15, "100", "100", "100", "100"),
        _bar(9, 16, "100.00", "100.20", "99.80", "100.05"),
        _bar(9, 17, "100.00", "100.10", "99.90", "99.95"),
    ]
    sink: list[TradeDetail] = []
    _sim(bars, [True, False, False], sink=sink)
    assert [d.exit_reason for d in sink] == ["end_of_data"]
    assert sink[0].exit_fill_paisa == 9_990  # last bar's adverse LOW (BUY)


def test_sink_captures_b3_terminal_close_basis() -> None:
    """The B3 terminal degenerate is a NAMED detail: the entry bar is the
    stream's last stored bar, so the end_of_data fill is that bar's CLOSE
    (design §3.12 — never the adverse extreme)."""
    bars = [
        _bar(9, 15, "100", "100", "100", "100"),
        _bar(9, 16, "100.20", "100.60", "100.10", "100.45"),  # entry AND last bar
    ]
    sink: list[TradeDetail] = []
    trades = _sim(bars, [True, False], sink=sink)
    assert [d.exit_reason for d in sink] == ["end_of_data"]
    assert sink[0].entry_fill_paisa == 10_060  # entry bar HIGH
    assert sink[0].exit_fill_paisa == 10_045  # the CLOSE, never the LOW 10010
    assert trades[0].holding_bars == 1


def test_sink_captures_session_gap_exit() -> None:
    """The boundary close is a named detail: a position still open when the
    feed steps to the NEXT IST session date closes retroactively at day-1's
    last stored bar's ADVERSE extreme (entry bar ≠ last bar here, so no B3)."""
    next_day = date(2024, 1, 3)
    bars = [
        _bar(11, 10, "100", "100", "100", "100"),
        _bar(11, 11, "100.00", "100.20", "99.80", "100.05"),
        _bar(11, 12, "100.00", "100.10", "99.90", "99.95"),  # day-1's LAST stored bar
        _bar(9, 15, "600", "601", "599", "600", day=next_day),
    ]
    sink: list[TradeDetail] = []
    trades = _sim(bars, [True, False, False, False], sink=sink)
    assert [d.exit_reason for d in sink] == ["session_gap"]
    assert sink[0].exit_fill_paisa == 9_990  # day-1's last stored adverse LOW
    assert sink[0].trade is trades[0]
    assert trades[0].pnl_paisa == 9_990 - 10_020  # entry @HIGH 100.20
    assert trades[0].exit_ts == bars[2].timestamp


def test_output_identical_with_and_without_sink() -> None:
    """The golden-safety regression: a sink can never perturb the trades."""
    without = _sim(_A_BARS, _A_SIGNALS, sl=30, tp=50)
    sink: list[TradeDetail] = []
    with_sink = _sim(_A_BARS, _A_SIGNALS, sl=30, tp=50, sink=sink)
    assert with_sink == without
    assert len(sink) == len(with_sink)


def test_exit_indices_wrapper_threads_sink() -> None:
    sink: list[TradeDetail] = []
    trades, exit_indices = simulate_trades_with_exit_indices(
        _A_BARS,
        _A_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=_A_BARS,
        stop_loss_paisa=30,
        take_profit_paisa=50,
        detail_sink=sink,
    )
    assert exit_indices is not None
    assert [d.trade for d in sink] == trades


def test_exit_fill_arithmetic_invariant() -> None:
    """pnl == exit − entry (BUY) — the belt trade_capture re-derives against."""
    sink: list[TradeDetail] = []
    _sim(_A_BARS, _A_SIGNALS, sl=30, tp=50, sink=sink)
    for detail in sink:
        assert detail.trade.pnl_paisa == detail.exit_fill_paisa - detail.entry_fill_paisa
