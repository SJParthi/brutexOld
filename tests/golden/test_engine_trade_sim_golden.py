"""Golden byte-equality oracle for the Stage-2 trade-sim walker.

Two small, fully hand-derived fixtures whose every emitted :class:`ClosedTrade`
field is computed by hand below and locked as a literal expected list. Any change
to the walker (entry/exit fill timing, the same-bar tie-break, the forced-exit
override, MFE/MAE folding, paisa P&L) that alters output breaks this test
(DEC-TRADESIM-001 / NFR-DET-001 bit-exactness).

All prices are rupees; entries fill at the next bar's OPEN (= 100.00 → 10000
paisa in both fixtures). Money is int64 paisa throughout.

Fixture A — a long with the default conservative policy (AT_LEVEL, SL-first),
three round-trips:

* **Trade 1 (take-profit)** — enter @09:16 (open 100.00). @09:17 the high 100.60
  crosses the +50p target (level 100.50) → fill 100.50 → **pnl +50**. Excursion
  folds @09:16 (H100.10/L99.95 → fav 10 / adv 5) then @09:17 (H100.60/L99.90 →
  fav 60 / adv 10) → **mfe 60, mae 10**; **holding 2**.
* **Trade 2 (stop-loss)** — enter @09:19 (open 100.00). @09:20 the low 99.60
  breaches the −30p stop (level 99.70) → fill 99.70 → **pnl −30**. Excursion
  @09:19 (fav 5 / adv 10) then @09:20 (H100.10/L99.60 → fav 10 / adv 40) →
  **mfe 10, mae 40**; **holding 2**.
* **Trade 3 (forced 15:20)** — enter @15:18 (open 100.00). @15:18 (fav 20 /
  adv 20) and @15:19 (H100.30 → fav 30 / L99.90 → adv 10) keep it open; the
  15:20 bar forces an exit at its OPEN 100.15 → **pnl +15** (the forced exit
  overrides the bar's 101.00 high, which would otherwise hit the target).
  Open-only excursion at 100.15 → **mfe 30, mae 20**; **holding 3**.

Fixture B — a short closed by a Mode-B signal exit (next-bar OPEN fill): enter
@09:16 (open 100.00); the exit signal on @09:17 (H100.20/L99.50 → short fav 50 /
adv 20) fills at @09:18 OPEN 99.70 → **pnl +30** (a short profits as price
falls); open-only excursion at 99.70 leaves **mfe 50, mae 20**; **holding 3**.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import FillPolicy, LevelFill, simulate_trades
from brutex.metrics.types import ClosedTrade
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


# ── Fixture A — long: take-profit, stop-loss, forced 15:20 ──────────────────
_A_BARS = [
    _bar(9, 15, "100", "100", "100", "100"),  # 0 signal → enter @1
    _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),  # 1 entry (open 100.00)
    _bar(9, 17, "100.00", "100.60", "99.90", "100.40"),  # 2 TP hit
    _bar(9, 18, "100", "100", "100", "100"),  # 3 signal → enter @4
    _bar(9, 19, "100.00", "100.05", "99.90", "99.95"),  # 4 entry
    _bar(9, 20, "100.00", "100.10", "99.60", "99.70"),  # 5 SL hit
    _bar(15, 17, "100", "100", "100", "100"),  # 6 signal → enter @7
    _bar(15, 18, "100.00", "100.20", "99.80", "100.10"),  # 7 entry
    _bar(15, 19, "100.00", "100.30", "99.90", "100.20"),  # 8 open
    _bar(15, 20, "100.15", "101.00", "100.00", "100.50"),  # 9 forced exit @open
]
_A_SIGNALS = [True, False, False, True, False, False, True, False, False, False]

_A_EXPECTED = [
    ClosedTrade(
        pnl_paisa=50,
        mfe_paisa=60,
        mae_paisa=10,
        holding_bars=2,
        entry_ts=_ts(9, 16),
        exit_ts=_ts(9, 17),
        direction=Side.BUY,
    ),
    ClosedTrade(
        pnl_paisa=-30,
        mfe_paisa=10,
        mae_paisa=40,
        holding_bars=2,
        entry_ts=_ts(9, 19),
        exit_ts=_ts(9, 20),
        direction=Side.BUY,
    ),
    ClosedTrade(
        pnl_paisa=15,
        mfe_paisa=30,
        mae_paisa=20,
        holding_bars=3,
        entry_ts=_ts(15, 18),
        exit_ts=_ts(15, 20),
        direction=Side.BUY,
    ),
]


def test_golden_long_tp_sl_forced() -> None:
    trades = simulate_trades(
        _A_BARS,
        _A_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=30,
        take_profit_paisa=50,
    )
    assert trades == _A_EXPECTED


# ── Fixture B — short closed by a Mode-B signal exit (next-open fill) ────────
_B_BARS = [
    _bar(9, 15, "100", "100", "100", "100"),  # 0 signal → enter @1
    _bar(9, 16, "100.00", "100.00", "100.00", "100.00"),  # 1 entry (open 100.00)
    _bar(9, 17, "100.00", "100.20", "99.50", "99.80"),  # 2 exit signal
    _bar(9, 18, "99.70", "99.70", "99.70", "99.70"),  # 3 exit fill @open 99.70
    _bar(9, 19, "100", "100", "100", "100"),  # 4 flat
]
_B_ENTRY = [True, False, False, False, False]
_B_EXIT = [False, False, True, False, False]

_B_EXPECTED = [
    ClosedTrade(
        pnl_paisa=30,
        mfe_paisa=50,
        mae_paisa=20,
        holding_bars=3,
        entry_ts=_ts(9, 16),
        exit_ts=_ts(9, 18),
        direction=Side.SELL,
    )
]


def test_golden_short_signal_exit() -> None:
    trades = simulate_trades(
        _B_BARS,
        _B_ENTRY,
        direction=Side.SELL,
        tf_minutes=1,
        exit_signals=_B_EXIT,
    )
    assert trades == _B_EXPECTED


def test_golden_short_signal_exit_streaming_matches_batch() -> None:
    # The streaming push/finish path must reproduce the same golden trades.
    from brutex.engine.trade_sim import TradeSimWalker

    walker = TradeSimWalker(direction=Side.SELL, tf_minutes=1, policy=FillPolicy())
    streamed: list[ClosedTrade] = []
    for bar, entry, exit_ in zip(_B_BARS, _B_ENTRY, _B_EXIT, strict=True):
        closed = walker.push(bar, entry_signal=entry, exit_signal=exit_)
        if closed is not None:
            streamed.append(closed)
    tail = walker.finish()
    if tail is not None:
        streamed.append(tail)
    assert streamed == _B_EXPECTED


def test_golden_take_profit_next_open_policy() -> None:
    # Same fixture-A trade-1 window but with NEXT_OPEN TP fill: the +50 target is
    # touched @09:17 yet fills at @09:18 OPEN (here 100.00) → pnl 0, not +50.
    bars = [
        _bar(9, 15, "100", "100", "100", "100"),
        _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),
        _bar(9, 17, "100.00", "100.60", "99.90", "100.40"),
        _bar(9, 18, "100.00", "100.00", "100.00", "100.00"),
    ]
    trades = simulate_trades(
        bars,
        [True, False, False, False],
        direction=Side.BUY,
        tf_minutes=1,
        take_profit_paisa=50,
        policy=FillPolicy(take_profit=LevelFill.NEXT_OPEN),
    )
    assert trades == [
        ClosedTrade(
            pnl_paisa=0,
            mfe_paisa=60,
            mae_paisa=10,
            holding_bars=3,
            entry_ts=_ts(9, 16),
            exit_ts=_ts(9, 18),
            direction=Side.BUY,
        )
    ]
