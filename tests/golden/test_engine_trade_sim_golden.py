"""Golden byte-equality oracle for the Stage-2 trade-sim walker.

Two small, fully hand-derived fixtures whose every emitted :class:`ClosedTrade`
field is computed by hand below and locked as a literal expected list. Any
change to the walker (the ONE worst-case fill law — DEC-FILL-WORST-CASE-ONLY-001
— entry at the adverse extreme, level fills clamped to the bar extreme, the
same-bar SL-first tie-break, the forced-exit adverse-extreme override, MFE/MAE
folding, paisa P&L) that alters output breaks this test (DEC-TRADESIM-001 /
NFR-DET-001 bit-exactness).

All prices are rupees; money is int64 paisa throughout. Under the one law a
BUY entry fills at the entry bar's HIGH and a SELL entry at its LOW; a long
take-profit fills at ``min(level, bar.low)``, a long stop at
``min(level, bar.low)``'s adverse twin ``min(level, low)`` — i.e. the fill is
never better than the bar's adverse extreme; forced/signal exits fill at the
exit bar's adverse extreme (BUY → low, SELL → high).

Fixture A — a long, three round-trips (every value re-derived BY HAND for the
worst-case law):

* **Trade 1 (take-profit)** — enter @09:16 at the bar HIGH 100.10 (10_010p).
  TP level = 10_010 + 50 = 10_060 (100.60). @09:17 the high 100.60 touches the
  level → worst-case fill ``min(100.60, low 99.90)`` = **99.90** →
  **pnl 9_990 − 10_010 = −20**. Excursions vs entry 10_010: @09:16
  (H100.10/L99.95 → fav 0 / adv 15), @09:17 (H100.60/L99.90 → fav 50 /
  adv 20) → **mfe 50, mae 20**; **holding 2**. (SL level 99.80 is never
  touched: lows 99.95/99.90 stay above it — no same-bar tie.)
* **Trade 2 (stop-loss)** — enter @09:19 at the bar HIGH 100.05 (10_005).
  SL level = 10_005 − 30 = 9_975 (99.75). @09:20 the low 99.60 breaches →
  worst-case fill ``min(99.75, 99.60)`` = **99.60** →
  **pnl 9_960 − 10_005 = −45**. Excursions: @09:19 (H100.05/L99.90 → fav 0 /
  adv 15), @09:20 (H100.10/L99.60 → fav 5 / adv 45) → **mfe 5, mae 45**;
  **holding 2**.
* **Trade 3 (forced 15:20)** — enter @15:18 at the bar HIGH 100.20 (10_020).
  The 15:18/15:19 lows sit at 99.95, ABOVE the SL level 99.90
  (10_020 − 30 = 9_990), so the position survives to the forced window; the
  15:20 bar forces an exit at its adverse extreme LOW **100.00** →
  **pnl 10_000 − 10_020 = −20** (the forced exit overrides the bar's 101.00
  high, which would otherwise hit the target). Excursions: @15:18 (fav 0 /
  adv 25), @15:19 (H100.30 → fav 10 / L99.95 → adv 25), exit fill 100.00 →
  adv 20 (≤ 25) → **mfe 10, mae 25**; **holding 3**.

Fixture B — a short closed by a Mode-B signal exit. PROVABLY LAW-INVARIANT:
the entry bar (@09:16) and the exit-fill bar (@09:18) are both FLAT (O=H=L=C),
so the adverse extreme IS the open on both — the expected trade is
byte-identical to the precise-era lock. Enter @09:16 at the bar LOW 100.00;
the exit signal on @09:17 (H100.20/L99.50 → short fav 50 / adv 20) fills on
@09:18 at the bar's adverse extreme HIGH 99.70 (flat) → **pnl +30**;
open-only excursion at 99.70 leaves **mfe 50, mae 20**; **holding 3**.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import simulate_trades
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
    _bar(9, 16, "100.00", "100.10", "99.95", "100.05"),  # 1 entry (worst fill = HIGH 100.10)
    _bar(9, 17, "100.00", "100.60", "99.90", "100.40"),  # 2 TP touch → fill @low 99.90
    _bar(9, 18, "100", "100", "100", "100"),  # 3 signal → enter @4
    _bar(9, 19, "100.00", "100.05", "99.90", "99.95"),  # 4 entry (worst fill = HIGH 100.05)
    _bar(9, 20, "100.00", "100.10", "99.60", "99.70"),  # 5 SL breach → fill @low 99.60
    _bar(15, 17, "100", "100", "100", "100"),  # 6 signal → enter @7
    _bar(15, 18, "100.00", "100.20", "99.95", "100.10"),  # 7 entry (worst fill = HIGH 100.20)
    _bar(15, 19, "100.00", "100.30", "99.95", "100.20"),  # 8 open (low above SL 99.90)
    _bar(15, 20, "100.15", "101.00", "100.00", "100.50"),  # 9 forced exit @adverse LOW 100.00
]
_A_SIGNALS = [True, False, False, True, False, False, True, False, False, False]

_A_EXPECTED = [
    ClosedTrade(
        pnl_paisa=-20,
        mfe_paisa=50,
        mae_paisa=20,
        holding_bars=2,
        entry_ts=_ts(9, 16),
        exit_ts=_ts(9, 17),
        direction=Side.BUY,
    ),
    ClosedTrade(
        pnl_paisa=-45,
        mfe_paisa=5,
        mae_paisa=45,
        holding_bars=2,
        entry_ts=_ts(9, 19),
        exit_ts=_ts(9, 20),
        direction=Side.BUY,
    ),
    ClosedTrade(
        pnl_paisa=-20,
        mfe_paisa=10,
        mae_paisa=25,
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


# ── Fixture B — short closed by a Mode-B signal exit (flat bars → invariant) ─
_B_BARS = [
    _bar(9, 15, "100", "100", "100", "100"),  # 0 signal → enter @1
    _bar(9, 16, "100.00", "100.00", "100.00", "100.00"),  # 1 entry (flat → fill 100.00)
    _bar(9, 17, "100.00", "100.20", "99.50", "99.80"),  # 2 exit signal
    _bar(9, 18, "99.70", "99.70", "99.70", "99.70"),  # 3 exit fill (flat → 99.70)
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

    walker = TradeSimWalker(direction=Side.SELL, tf_minutes=1)
    streamed: list[ClosedTrade] = []
    for bar, entry, exit_ in zip(_B_BARS, _B_ENTRY, _B_EXIT, strict=True):
        closed = walker.push(bar, entry_signal=entry, exit_signal=exit_)
        if closed is not None:
            streamed.append(closed)
    tail = walker.finish()
    if tail is not None:
        streamed.append(tail)
    assert streamed == _B_EXPECTED
