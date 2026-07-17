"""Golden corpus #3 — SENSEX circuit-close edge (forced 15:20 exit + EOD robustness).

DEC-PHASE2-CORPORA-001 (``tests/golden/fixtures/README.md`` corpus #3 —
"SENSEX — circuit-close edge case / forced-exit (P-09) + simulator robustness"),
re-locked for the ONE worst-case fill law (DEC-FILL-WORST-CASE-ONLY-001).

Drives the REAL Stage-2 trade-sim walker
(:func:`brutex.engine.trade_sim.simulate_trades`) over two SENSEX-scale fixtures
that lock the two end-of-session exit paths:

* **P-09 forced 15:20 IST exit** — a near-limit "circuit" move into the close: a
  long is open across 15:18/15:19 and the 15:20 bar forces an exit at its
  adverse LOW, **overriding** that bar's high (which would otherwise hit the
  take-profit target). This is the proven Fixture-A trade-3 window from
  ``test_engine_trade_sim_golden.py`` (15:18/15:19 lows at 99.95, ABOVE the SL
  level 99.90 so the position reaches the forced window) **translated up by
  ₹72 900** to SENSEX scale — and every ``ClosedTrade`` field except the
  timestamps is a *price difference* (pnl / mfe / mae) or a *count* (holding),
  which translation leaves invariant. HAND-DERIVED (₹100 scale): entry fills
  at the bar HIGH 100.20 (10_020p); excursions 15:18 (fav 0 / adv 25), 15:19
  (H100.30 → fav 10 / adv 25); forced fill @15:20 LOW 100.00 →
  **pnl −20, mfe 10, mae 25, holding 3**.

* **simulator robustness — position open at end of data** — a Mode-B long
  (no stop, no target, no exit signal) that never closes intraday: the feed
  simply ends before 15:20, so :meth:`TradeSimWalker.finish` closes it at the
  **last bar's ADVERSE extreme** (BUY → LOW). Hand-derived on the
  translation-invariant window: enter @ entry bar HIGH ₹73000.40 (fav 0 /
  adv 50), fold the last bar's excursion (H100.50 → fav 10 / L99.80 →
  adv 60), exit at the final LOW ₹72999.80 ⇒
  **pnl −60, mfe 10, mae 60, holding 2**.

Track-1 spot is signal-only → NO brokerage (CLAUDE.md §7), so the paisa pnl IS
the net. Any walker regression (forced-exit override, end-of-data fallback,
MFE/MAE folding, the worst-case fill law) breaks these literal expectations
(NFR-DET-001 / Golden Rule #4).
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
#: SENSEX-scale translation offset applied to the proven Fixture-A window.
_OFFSET = Decimal("72900")


def _ts(hour: int, minute: int) -> datetime:
    return datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=IST).astimezone(UTC)


def _bar(hour: int, minute: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    """A SENSEX bar: the ``+72900`` translation of the proven ₹100-scale window."""
    return CandleBar(
        timestamp=_ts(hour, minute),
        open=_OFFSET + Decimal(o),
        high=_OFFSET + Decimal(h),
        low=_OFFSET + Decimal(lo),
        close=_OFFSET + Decimal(c),
        volume=0,
        open_interest=None,
    )


# ── Forced 15:20 exit (proven Fixture-A trade-3 window, translated) ──────────
_FORCED_BARS = [
    _bar(15, 17, "100", "100", "100", "100"),  # 0 signal → enter @1
    _bar(15, 18, "100.00", "100.20", "99.95", "100.10"),  # 1 entry (worst fill = HIGH)
    _bar(15, 19, "100.00", "100.30", "99.95", "100.20"),  # 2 still open (low above SL)
    _bar(15, 20, "100.15", "101.00", "100.00", "100.50"),  # 3 forced @adverse LOW
]
_FORCED_SIGNALS = [True, False, False, False]
_FORCED_EXPECTED = [
    ClosedTrade(
        pnl_paisa=-20,
        mfe_paisa=10,
        mae_paisa=25,
        holding_bars=3,
        entry_ts=_ts(15, 18),
        exit_ts=_ts(15, 20),
        direction=Side.BUY,
    )
]


# ── Position still open at end of data → closes at the adverse LOW ───────────
_EOD_BARS = [
    _bar(9, 30, "100", "100", "100", "100"),  # 0 signal → enter @1
    _bar(9, 31, "100.00", "100.40", "99.90", "100.05"),  # 1 entry @HIGH; fav 0 / adv 50
    _bar(9, 32, "100.00", "100.50", "99.80", "100.30"),  # 2 last bar; fav 10 / adv 60
]
_EOD_SIGNALS = [True, False, False]
_EOD_EXPECTED = [
    ClosedTrade(
        pnl_paisa=-60,
        mfe_paisa=10,
        mae_paisa=60,
        holding_bars=2,
        entry_ts=_ts(9, 31),
        exit_ts=_ts(9, 32),
        direction=Side.BUY,
    )
]


def test_forced_exit_overrides_at_circuit_close() -> None:
    """P-09: the 15:20 bar forces an exit at its adverse LOW, overriding the
    would-be TP at that bar's high."""
    trades = simulate_trades(
        _FORCED_BARS,
        _FORCED_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=30,
        take_profit_paisa=50,
    )
    assert trades == _FORCED_EXPECTED


def test_open_position_at_end_of_data_closes_at_last_close() -> None:
    """Robustness: a never-triggered Mode-B long closes at the final bar's
    ADVERSE extreme (BUY → LOW) under the one law."""
    trades = simulate_trades(
        _EOD_BARS,
        _EOD_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
    )
    assert trades == _EOD_EXPECTED


def test_net_equals_gross_for_signal_only_spot() -> None:
    """Track-1 spot is signal-only → no brokerage (CLAUDE.md §7): net == gross."""
    forced = simulate_trades(
        _FORCED_BARS,
        _FORCED_SIGNALS,
        direction=Side.BUY,
        tf_minutes=1,
        stop_loss_paisa=30,
        take_profit_paisa=50,
    )
    eod = simulate_trades(_EOD_BARS, _EOD_SIGNALS, direction=Side.BUY, tf_minutes=1)
    gross = forced[0].pnl_paisa + eod[0].pnl_paisa
    assert gross == -80  # −20 (forced) + −60 (eod), exact int64 paisa
    assert isinstance(gross, int)
