"""Golden oracle for the Stage-2 Mode-A trade-sim walker.

A hand-derived fixture whose every emitted ``ClosedTrade`` field is locked below
(pinned literal values — the oracle). Any change to the fill mechanics (N+1/M+1
open, SL/TP detection, forced-15:20, MFE/MAE, P&L sign) breaks this test
(DEC-TRADE-SIM-MODEA-001 / NFR-DET-001). Mirrors the literal-pinning style of
``tests/golden/test_engine_stage1a_golden.py``.

Fixture (one IST session, prices in rupees):

* **0** 09:15 flat ``100`` — signal A.
* **1** 09:16 ``O100.00 H100.60 L99.95 C100.40`` — entry A @100.00; high 100.60
  ≥ TP 100.50 → breach at bar 1.
* **2** 09:17 ``O100.50 H100.55 L100.45 C100.50`` — exit A @ OPEN 100.50; signal B.
* **3** 09:18 ``O100.50 H101.10 L100.45 C101.00`` — entry B @100.50; high 101.10
  ≥ TP 101.00 → breach at bar 3.
* **4** 09:19 ``O101.00 H101.10 L100.95 C101.05`` — exit B @ OPEN 101.00.
* **5** 15:20 flat ``101`` — forced bar (never reached).

Hand-derived (``sl_paisa=200``, ``tp_paisa=50``, BUY):

* **Trade A** — entry 100.00 → exit 100.50: ``pnl=50``, ``holding=1`` (bar1→bar2),
  ``mfe=60`` (100.60−100.00), ``mae=5`` (100.00−99.95).
* **Trade B** — entry 100.50 → exit 101.00: ``pnl=50``, ``holding=1`` (bar3→bar4),
  ``mfe=60`` (101.10−100.50), ``mae=5`` (100.50−100.45).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar
from brutex.engine.trade_sim import simulate_session_mode_a
from brutex.strategies.types import Side

_IST = ZoneInfo("Asia/Kolkata")


def _bar(hour: int, minute: int, o: str, h: str, low: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, 2, hour, minute, tzinfo=_IST).astimezone(UTC),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


_FIXTURE = [
    _bar(9, 15, "100", "100", "100", "100"),
    _bar(9, 16, "100.00", "100.60", "99.95", "100.40"),
    _bar(9, 17, "100.50", "100.55", "100.45", "100.50"),
    _bar(9, 18, "100.50", "101.10", "100.45", "101.00"),
    _bar(9, 19, "101.00", "101.10", "100.95", "101.05"),
    _bar(15, 20, "101", "101", "101", "101"),
]


def _ist(hour: int, minute: int) -> datetime:
    return datetime(2024, 1, 2, hour, minute, tzinfo=_IST).astimezone(UTC)


def test_golden_two_take_profit_trades() -> None:
    trades = simulate_session_mode_a(
        _FIXTURE, signal_bars=[0, 2], direction=Side.BUY, sl_paisa=200, tp_paisa=50
    )
    assert len(trades) == 2

    a, b = trades
    assert (a.pnl_paisa, a.holding_bars, a.mfe_paisa, a.mae_paisa) == (50, 1, 60, 5)
    assert (a.entry_ts, a.exit_ts, a.direction) == (_ist(9, 16), _ist(9, 17), Side.BUY)

    assert (b.pnl_paisa, b.holding_bars, b.mfe_paisa, b.mae_paisa) == (50, 1, 60, 5)
    assert (b.entry_ts, b.exit_ts, b.direction) == (_ist(9, 18), _ist(9, 19), Side.BUY)


def test_golden_is_deterministic() -> None:
    run1 = simulate_session_mode_a(
        _FIXTURE, signal_bars=[0, 2], direction=Side.BUY, sl_paisa=200, tp_paisa=50
    )
    run2 = simulate_session_mode_a(
        _FIXTURE, signal_bars=[2, 0], direction=Side.BUY, sl_paisa=200, tp_paisa=50
    )
    assert run1 == run2  # signal input order does not matter


def test_golden_short_mirror() -> None:
    # Same fixture, SELL: a falling target / rising stop mirror. Signal A entry
    # 100.00; bar1 low 99.95 (TP 99.50 not hit), high 100.60 ≥ SL 100.30 → stop.
    trades = simulate_session_mode_a(
        _FIXTURE, signal_bars=[0], direction=Side.SELL, sl_paisa=30, tp_paisa=50
    )
    (a,) = trades
    assert a.pnl_paisa == -50  # short stop: entry 100.00 − exit 100.50
    assert a.direction is Side.SELL
    assert a.holding_bars == 1
