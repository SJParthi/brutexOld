"""Golden paisa-accounting oracle — 12-fill Σ PnL (Phase 2 corpus #8).

DEC-ENGINE-PAISA-ACCOUNTING-GOLDEN-001 (``tests/golden/fixtures/README.md``
corpus #8 — "12-fill paisa accounting / gross-net-Σ PnL (P-03, P-04)").

Locks the int64-paisa accounting across MANY back-to-back round-trips: that the
Stage-2 trade-sim walker emits one :class:`ClosedTrade` per fill with the exact
per-trade paisa P&L (**P-03**), and that the aggregate Σ PnL is the EXACT integer
sum with no float drift over a long sequence (**P-04**).

Correct-by-construction fixture (no un-runnable hand-math): the corpus is **12
identical copies** of the already-hand-derived + golden-locked Fixture-A trade 1
from ``test_engine_trade_sim_golden.py`` under the ONE worst-case fill law
(DEC-FILL-WORST-CASE-ONLY-001) — a long that enters at the entry bar's adverse
HIGH 100.10 (10_010p); the +50p target level 10_060 is touched by the next
bar's high 100.60 and fills at ``min(level, low 99.90)`` = 9_990 →
**pnl −20, mfe 50, mae 20, holding 2** (excursions vs 10_010: entry bar fav 0
/ adv 15; TP bar fav 50 / adv 20). Twelve back-to-back round-trips (signal →
entry → TP, three bars each, 09:15-onward so the 15:20 forced-exit rule never
fires) ⇒ **Σ pnl = 12 × −20 = −240 paisa** exactly. For Track-1 signal-only
spot there is NO brokerage (CLAUDE.md §7), so **net == gross**: the −240-paisa
gross IS the net.

Re-locked UNCHANGED for the 1-minute execution grid
(DEC-FILL-EXEC-GRID-1M-001): the run is the ``tf_minutes == 1`` fixed point
(the same list passed as decision AND execution stream — fill on 1m bar N+1,
the historical timing) and the two new laws are provably inert here — each
round-trip's ENTRY bar range [99.95, 100.10] touches neither the SL level
99.80 (= 100.10 − 0.30) nor the TP level 100.60 (= 100.10 + 0.50), so the
§2.4 same-bar deferral changes nothing, and every trade exits via TP (never
end_of_data/session_gap), so the B3 terminal-CLOSE carve-out never applies.
``holding_bars`` 2 is now a 1-MINUTE count (exit_idx − entry_idx + 1 =
(base+2) − (base+1) + 1), numerically identical at this fixed point.

Any walker regression (fill timing, the same-bar tie-break, MFE/MAE folding,
paisa P&L, or float creeping into the Σ) breaks this test (NFR-DET-001
bit-exactness / Golden Rule #4 reproducibility).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import simulate_trades
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_DAY = date(2024, 1, 2)
#: Number of identical back-to-back round-trips in the corpus (README "12-fill").
_N_TRADES = 12
#: Bars per round-trip: signal → entry → take-profit (mirrors Fixture-A trade 1).
_BARS_PER_TRADE = 3
#: SL / TP levels (paisa) — the proven Fixture-A trade-1 parameters.
_STOP_LOSS_PAISA = 30
_TAKE_PROFIT_PAISA = 50

#: The per-trade oracle (one take-profit round-trip), hand-derived + already
#: golden-locked in ``test_engine_trade_sim_golden.py`` (Fixture A, trade 1).
_PER_TRADE_PNL = -20
_PER_TRADE_MFE = 50
_PER_TRADE_MAE = 20
_PER_TRADE_HOLDING = 2


def _ts(minute_index: int) -> datetime:
    """UTC timestamp for the ``minute_index``-th 1-minute bar from 09:15 IST."""
    base = datetime(_DAY.year, _DAY.month, _DAY.day, 9, 15, tzinfo=IST)
    return (base + timedelta(minutes=minute_index)).astimezone(UTC)


def _bar(minute_index: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    return CandleBar(
        timestamp=_ts(minute_index),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


def _build_fixture() -> tuple[list[CandleBar], list[bool]]:
    """Build the 36-bar / 12-round-trip fixture + its entry-signal list.

    Each round-trip is the proven 3-bar Fixture-A trade-1 shape:
    ``signal (flat) → entry (open 100.00) → take-profit (high 100.60)``.
    """
    bars: list[CandleBar] = []
    signals: list[bool] = []
    for k in range(_N_TRADES):
        base = k * _BARS_PER_TRADE
        # bar 0 — flat signal bar; entry fires on the NEXT bar's open.
        bars.append(_bar(base, "100", "100", "100", "100"))
        signals.append(True)
        # bar 1 — entry bar (worst fill = HIGH 100.10); excursion fav 0 / adv 15.
        bars.append(_bar(base + 1, "100.00", "100.10", "99.95", "100.05"))
        signals.append(False)
        # bar 2 — take-profit bar (high 100.60 touches level 100.60; fill
        # clamps to the low 99.90); excursion fav 50 / adv 20 → mfe 50, mae 20.
        bars.append(_bar(base + 2, "100.00", "100.60", "99.90", "100.40"))
        signals.append(False)
    return bars, signals


def _expected_trades() -> list[ClosedTrade]:
    """The 12 identical expected closed trades (entry @bar1, exit @bar2 of each)."""
    out: list[ClosedTrade] = []
    for k in range(_N_TRADES):
        base = k * _BARS_PER_TRADE
        out.append(
            ClosedTrade(
                pnl_paisa=_PER_TRADE_PNL,
                mfe_paisa=_PER_TRADE_MFE,
                mae_paisa=_PER_TRADE_MAE,
                holding_bars=_PER_TRADE_HOLDING,
                entry_ts=_ts(base + 1),
                exit_ts=_ts(base + 2),
                direction=Side.BUY,
            )
        )
    return out


def _run() -> list[ClosedTrade]:
    bars, signals = _build_fixture()
    # tf==1 fixed point: the same list is decision AND execution stream.
    return simulate_trades(
        bars,
        signals,
        direction=Side.BUY,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=_STOP_LOSS_PAISA,
        take_profit_paisa=_TAKE_PROFIT_PAISA,
    )


def test_twelve_fills_emit_one_closed_trade_each() -> None:
    """P-03: every fill yields exactly one closed trade with the proven paisa fields."""
    assert _run() == _expected_trades()


def test_sigma_pnl_is_exact_int64_sum() -> None:
    """P-04: Σ PnL is the EXACT integer sum (no float drift) — 12 × −20 = −240 paisa."""
    trades = _run()
    total = sum(t.pnl_paisa for t in trades)
    assert total == _N_TRADES * _PER_TRADE_PNL == -240
    assert isinstance(total, int)  # int64 paisa, never a float


def test_net_equals_gross_for_signal_only_spot() -> None:
    """Track-1 spot is signal-only → NO brokerage (CLAUDE.md §7): net == gross."""
    trades = _run()
    gross = sum(t.pnl_paisa for t in trades)
    net = gross  # no cost model applied for signal-only spot
    assert net == gross == -240


def test_every_trade_is_identical_in_paisa() -> None:
    """All 12 round-trips share the same paisa P&L + excursions (only ts differ)."""
    trades = _run()
    assert len(trades) == _N_TRADES
    assert {t.pnl_paisa for t in trades} == {_PER_TRADE_PNL}
    assert {t.mfe_paisa for t in trades} == {_PER_TRADE_MFE}
    assert {t.mae_paisa for t in trades} == {_PER_TRADE_MAE}
    assert {t.holding_bars for t in trades} == {_PER_TRADE_HOLDING}
