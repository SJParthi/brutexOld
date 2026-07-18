"""Integration proof of the cost-scope rule — DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001.

Proves end-to-end that the ONE cost-scope predicate
(:func:`brutex.costs.scope.is_cost_free`) lines up with the ACTUAL behaviour of
the two production pricing paths:

* the brute-force ENGINE spot/index walker
  (:func:`brutex.engine.trade_sim.simulate_trades`) prices an **index-spot** and
  an **index-future** trade signal-only — the emitted P&L is the RAW directional
  price move with ZERO brokerage / STT / slippage / liquidity / fill-scenario —
  and for both the predicate returns ``is_cost_free() is True``;

* the Track-2 cost calculator
  (:func:`brutex.costs.calculator.compute_trade_cost`) prices an **index OPTION**
  with a NON-ZERO brokerage + STT + slippage stack, and for the index option the
  predicate returns ``is_cost_bearing() is True``.

This is the guarantee: the predicate is not a dangling label — it agrees with
the code paths that either apply or skip costs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.calendars.nse import IST
from brutex.costs.calculator import compute_trade_cost
from brutex.costs.scope import is_cost_bearing, is_cost_free
from brutex.costs.types import TradeInput
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import simulate_trades
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.strategies.types import Side

_DAY = date(2025, 3, 3)  # a regular NSE trading Monday


def _ts(hh: int, mm: int) -> datetime:
    """UTC-aware timestamp for an IST wall-clock ``hh:mm`` (bars store UTC)."""
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


def _index_spot() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY 50",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _index_future() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=100,
        symbol_name="NIFTY25MARFUT",
        display_name=None,
        instrument=InstrumentType.FUT,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=26000,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=None,
    )


def _index_option() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=200,
        symbol_name="NIFTY25MAR24000CE",
        display_name=None,
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=26000,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=Decimal("24000"),
    )


class TestEngineIndexPathIsSignalOnly:
    """The engine walker prices index spot + index futures with ZERO cost."""

    def _run_one_trade(self) -> int:
        # Signal on bar 0 → enter at bar 1 (FLAT at ₹100.00 — under the one
        # worst-case law, DEC-FILL-WORST-CASE-ONLY-001, the fill is the bar's
        # adverse extreme, which on a flat bar IS ₹100.00); a Mode-B signal
        # exit at bar 1 fills at bar 2 (flat at ₹110.00 — adverse extreme
        # ₹110.00). Long P&L = +₹10.00 = 1000 paisa, RAW — no
        # brokerage/STT/slippage is ever subtracted by the signal-only walker.
        bars = [
            _bar(9, 15, "100.00", "100.00", "100.00", "100.00"),
            _bar(9, 16, "100.00", "100.00", "100.00", "100.00"),
            _bar(9, 17, "110.00", "110.00", "110.00", "110.00"),
        ]
        entry = [True, False, False]
        exit_sig = [False, True, False]
        trades = simulate_trades(
            bars,
            entry,
            direction=Side.BUY,
            tf_minutes=1,
            exit_signals=exit_sig,
        )
        assert len(trades) == 1
        return trades[0].pnl_paisa

    def test_index_spot_predicate_is_cost_free(self) -> None:
        assert is_cost_free(_index_spot()) is True

    def test_index_future_predicate_is_cost_free(self) -> None:
        assert is_cost_free(_index_future()) is True

    def test_engine_pnl_is_raw_directional_move_zero_cost(self) -> None:
        # Enter ₹100.00, exit ₹110.00 → exactly +1000 paisa. If ANY brokerage
        # (₹20 = 2000 paisa) or STT or slippage were applied, this would differ.
        assert self._run_one_trade() == 1000

    def test_engine_path_matches_predicate_for_both_index_classes(self) -> None:
        # The walker applies no cost to EITHER cost-free class; the predicate
        # agrees for both.
        for inst in (_index_spot(), _index_future()):
            assert is_cost_free(inst) is True
        # And the raw-move invariant holds (proving the walker is signal-only).
        assert self._run_one_trade() == 1000


class TestOptionPathIsCostBearing:
    """The Track-2 calculator prices an INDEX option with the full cost stack."""

    def test_index_option_predicate_is_cost_bearing(self) -> None:
        assert is_cost_bearing(_index_option()) is True
        assert is_cost_free(_index_option()) is False

    def test_index_option_cost_stack_is_non_zero(self) -> None:
        # An index option carries real brokerage + STT + slippage — the exact
        # opposite of the signal-only engine path.
        trade = TradeInput(
            entry_dt_ist=datetime(2025, 3, 3, 10, 0),
            exit_dt_ist=datetime(2025, 3, 3, 11, 0),
            instrument="NIFTY",
            option_type="CE",
            strike=2_400_000,
            expiry_date=date(2025, 3, 27),
            entry_bar_open_paisa=10_000,
            exit_bar_open_paisa=12_000,
            # The one worst-case fill law REQUIRES the fill-bar extremes
            # (DEC-FILL-WORST-CASE-ONLY-001): flat bars here — the adverse
            # extreme is the open, so the ± one-tick discipline is the whole
            # slippage term.
            entry_bar_high_paisa=10_000,
            entry_bar_low_paisa=10_000,
            exit_bar_high_paisa=12_000,
            exit_bar_low_paisa=12_000,
            side="long_call",
            n_lots=1,
            broker="groww",
            gst_split="intra_state",
            trade_outcome="NORMAL_CLOSE",
        )
        breakdown = compute_trade_cost(trade)
        # The full cost stack is genuinely applied (NOT signal-only).
        assert breakdown.brokerage_paisa > 0
        assert breakdown.stt_paisa > 0
        assert breakdown.slippage_paisa > 0
        assert breakdown.total_charges_paisa > 0
        # Net P&L is strictly below the raw gross move → costs really bit.
        assert breakdown.net_pnl_paisa < breakdown.gross_pnl_paisa
