"""Golden byte-equality oracle for the Stage-2 trade-sim walker.

Two small, fully hand-derived fixtures whose every emitted :class:`ClosedTrade`
field is computed by hand below and locked as a literal expected list. Any
change to the walker (the ONE worst-case fill law — DEC-FILL-WORST-CASE-ONLY-001
— entry at the adverse extreme, level fills clamped to the bar extreme, the
same-bar SL-first tie-break, the forced-exit adverse-extreme override, MFE/MAE
folding, paisa P&L) that alters output breaks this test (DEC-TRADESIM-001 /
NFR-DET-001 bit-exactness).

Re-locked for the 1-MINUTE execution grid (DEC-FILL-EXEC-GRID-1M-001): both
fixtures run at ``tf_minutes == 1`` with the SAME contiguous list passed as
decision AND execution stream (the §6 degenerate fixed point), so a
decision-bar-N signal fills on 1m bar N+1 — exactly the historical next-bar
timing. The two NEW laws are PROVABLY INERT on these fixtures, so every
locked value below is byte-identical to the PR-A lock:

* §2.4 same-bar deferral — no ENTRY bar's [low, high] here ever touches an
  SL/TP level (derived per trade below), so deferring level detection to the
  entry bar's successor changes nothing;
* B3 terminal-CLOSE carve-out — no trade here ends end_of_data/session_gap
  on its own entry bar (Trade 3 is a forced 15:20 close; Fixture B is a
  Mode-B signal exit).

All prices are rupees; money is int64 paisa throughout. Under the one law a
BUY entry fills at the entry bar's HIGH and a SELL entry at its LOW; a long
take-profit fills at ``min(level, bar.low)``, a long stop at
``min(level, bar.low)``'s adverse twin ``min(level, low)`` — i.e. the fill is
never better than the bar's adverse extreme; forced/signal exits fill at the
exit bar's adverse extreme (BUY → low, SELL → high). ``holding_bars`` /
entry/exit indices are 1-MINUTE quantities (identical to the decision count
at this fixed point).

Fixture A — a long, three round-trips (every value re-derived BY HAND for the
worst-case law on the 1m grid):

* **Trade 1 (take-profit)** — enter @09:16 at the bar HIGH 100.10 (10_010p).
  TP level = 10_010 + 50 = 10_060 (100.60); SL level = 10_010 − 30 = 9_980
  (99.80). The ENTRY bar's range [99.95, 100.10] touches neither level →
  no deferral, and detection starting @09:17 is exactly the PR-A walk.
  @09:17 the high 100.60 touches the TP → worst-case fill
  ``min(100.60, low 99.90)`` = **99.90** → **pnl 9_990 − 10_010 = −20**.
  Excursions vs entry 10_010: @09:16 (H100.10/L99.95 → fav 0 / adv 15),
  @09:17 (H100.60/L99.90 → fav 50 / adv 20) → **mfe 50, mae 20**;
  **holding = exit_idx 2 − entry_idx 1 + 1 = 2** (1m bars). (SL 99.80 never
  touched: lows 99.95/99.90 stay above it — no same-bar tie.)
* **Trade 2 (stop-loss)** — enter @09:19 at the bar HIGH 100.05 (10_005).
  SL level = 10_005 − 30 = 9_975 (99.75); TP level = 10_055. The ENTRY bar's
  range [99.90, 100.05] touches neither (99.90 > 99.75) → no deferral.
  @09:20 the low 99.60 breaches the SL → worst-case fill
  ``min(99.75, 99.60)`` = **99.60** → **pnl 9_960 − 10_005 = −45**.
  Excursions: @09:19 (H100.05/L99.90 → fav 0 / adv 15), @09:20
  (H100.10/L99.60 → fav 5 / adv 45) → **mfe 5, mae 45**;
  **holding = 5 − 4 + 1 = 2**.
* **Trade 3 (forced 15:20)** — enter @15:18 at the bar HIGH 100.20 (10_020).
  Entry-bar range [99.95, 100.20] touches neither level (SL 9_990 =
  99.90 < 99.95; TP 10_070) → no deferral. The 15:18/15:19 lows sit at
  99.95, ABOVE the SL level 99.90, so the position survives to the forced
  window; the 15:20 1m bar (>= the session boundary 15:20, no slide) forces
  an exit at its adverse extreme LOW **100.00** →
  **pnl 10_000 − 10_020 = −20** (the forced exit overrides the bar's 101.00
  high, which would otherwise hit the target). Excursions: @15:18 (fav 0 /
  adv 25), @15:19 (H100.30 → fav 10 / L99.95 → adv 25), forced bar folds
  (open, open) = 100.15 → fav −5→0 / adv 5 (≤ 25) → **mfe 10, mae 25**;
  **holding = 9 − 7 + 1 = 3**.

Fixture B — a short closed by a Mode-B signal exit. PROVABLY LAW-INVARIANT:
the entry bar (@09:16) and the exit-fill bar (@09:18) are both FLAT (O=H=L=C),
so the adverse extreme IS the open on both — the expected trade is
byte-identical to the precise-era lock. Enter @09:16 at the bar LOW 100.00;
the exit signal on decision bar @09:17 (H100.20/L99.50 → short fav 50 /
adv 20) maps to its next-1m fill bar @09:18 (NOT the entry bar — no
deferral) at that bar's adverse extreme HIGH 99.70 (flat) → **pnl +30**;
open-only (open, open) excursion fold at 99.70 leaves **mfe 50, mae 20**;
**holding = 3 − 1 + 1 = 3**.
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
    # tf==1 fixed point: the same list is decision AND execution stream.
    trades = simulate_trades(
        _A_BARS,
        _A_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=_A_BARS,
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
    # tf==1 fixed point: the same list is decision AND execution stream.
    trades = simulate_trades(
        _B_BARS,
        _B_ENTRY,
        direction=Side.SELL,
        tf_minutes=1,
        exec_bars_1m=_B_BARS,
        exit_signals=_B_EXIT,
    )
    assert trades == _B_EXPECTED


def test_golden_short_signal_exit_streaming_matches_batch() -> None:
    # The streaming push_1m/finish path must reproduce the same golden trades.
    # Under DEC-FILL-EXEC-GRID-1M-001 the walker's per-bar face takes the
    # PRECOMPUTED due-fill projections (entry_due/exit_due) — the batch path's
    # ExecGridMap projection ``signals[arm_source[j]]`` is replayed by hand
    # here (the retired per-bar ``push(bar, entry_signal=, exit_signal=)``
    # decision-TF face is GONE).
    from brutex.engine.exec_grid import build_exec_grid_map
    from brutex.engine.trade_sim import TradeSimWalker

    exec_map = build_exec_grid_map(_B_BARS, _B_BARS, tf_minutes=1)
    walker = TradeSimWalker(direction=Side.SELL, tf_minutes=1)
    streamed: list[ClosedTrade] = []
    for j, bar in enumerate(_B_BARS):
        src = exec_map.arm_source[j]
        closed = walker.push_1m(
            bar,
            entry_due=src >= 0 and _B_ENTRY[src],
            exit_due=src >= 0 and _B_EXIT[src],
        )
        if closed is not None:
            streamed.append(closed)
    tail = walker.finish()
    if tail is not None:
        streamed.append(tail)
    assert streamed == _B_EXPECTED
