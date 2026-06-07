"""Golden byte-equality oracle for the options executor (DEC-OPTIONS-EXECUTOR-001).

Two fully hand-derived fixtures whose every emitted field — selected leg, paisa
conversions, and the full per-charge cost breakdown — is computed by hand below
and locked as a literal. Any change to the executor's composition (strike
selection, rupees→paisa snap, cost-stack wiring) that alters output breaks this
test. The cost-rate constants themselves are independently golden-tested in
``tests/golden`` / ``tests/unit/test_costs_calculator.py``; this test pins the
*executor's use* of them.

Entry date 2024-05-02 (STT regime: pre-2024-10-01 → 0.0625%). Lot sizes from
``brutex.options.lot_size_history``: NIFTY = 25, SENSEX = 10. 1-tick = 5 paisa.

Fixture A — NIFTY BULL ``atm`` (long_call), entry ₹120.00 → exit ₹150.00:
  ATM(25012, step 50) = 25000; CE strike 25000, offset 0.
  buy_fill = 12000 + 5 = 12005;  sell_fill = 15000 − 5 = 14995;  n_shares = 25.
  buy_notional = 300125;  sell_notional = 374875;  gross = +74750.
  brokerage = 2×2000 = 4000.
  STT = round₹(374875×6250//1e7 = 234) = 200.
  exch(NSE 3503) = ⌈300125×3503⌉ₚ + ⌈374875×3503⌉ₚ = 105 + 131 = 236.
  sebi(10) = 0 + 0 = 0;  ipft(NSE 50) = 2 + 2 = 4;  stamp(300, buy) = round₹(9) = 0.
  GST = round₹((4000+236+0+4)×18% = 763) = 800.
  total = 4000+200+236+0+4+0+800 = 5240;  net = 74750 − 5240 = +69510.

Fixture B — SENSEX BEAR ``atm_plus_1_in_dir`` (long_put), entry ₹200.00 → ₹50.00:
  ATM(81012, step 100) = 81000; PE "further OTM" = lower strike 80900, offset −1.
  buy_fill = 20005;  sell_fill = 4995;  n_shares = 10.
  buy_notional = 200050;  sell_notional = 49950;  gross = −150100.
  brokerage = 4000;  STT = round₹(49950×6250//1e7 = 31) = 0.
  exch(BSE 3250) = ⌈200050×3250⌉ₚ + ⌈49950×3250⌉ₚ = 65 + 16 = 81.
  sebi = 0;  ipft(BSE 0) = 0;  stamp = round₹(6) = 0.
  GST = round₹((4000+81)×18% = 734) = 700.
  total = 4000+0+81+0+0+0+700 = 4781;  net = −150100 − 4781 = −154881.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import OptionBar
from brutex.options.executor import (
    MaterializedTrade,
    OptionBarPair,
    SpotSignal,
    materialize_trade,
)
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import RULES_BY_NAME, TradeDirection

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY_TS = datetime(2024, 5, 2, 9, 16, tzinfo=_IST)
_EXIT_TS = datetime(2024, 5, 2, 15, 20, tzinfo=_IST)


def _bar(side: str, open_rupees: str) -> OptionBar:
    px = Decimal(open_rupees)
    return OptionBar(
        timestamp=_ENTRY_TS,
        open=px,
        high=px,
        low=px,
        close=px,
        volume=10,
        open_interest=100,
        side=side,
        strike=Decimal("25000"),
        spot=Decimal("25012"),
        iv=Decimal("0.15"),
    )


class _Source:
    def __init__(self, entry_open: str, exit_open: str) -> None:
        self._entry = entry_open
        self._exit = exit_open

    def load_pair(self, *, option_type: str, **_kw: object) -> OptionBarPair:
        return OptionBarPair(
            entry_bar=_bar(option_type, self._entry),
            exit_bar=_bar(option_type, self._exit),
        )


def test_golden_nifty_bull_atm() -> None:
    sig = SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=_ENTRY_TS,
        exit_ts=_EXIT_TS,
        spot_at_entry=Decimal("25012"),
        expiry_date=date(2024, 5, 30),
    )
    out = materialize_trade(sig, RULES_BY_NAME["atm"], source=_Source("120.00", "150.00"))
    assert isinstance(out, MaterializedTrade)

    assert out.leg.option_type == "CE"
    assert out.leg.side == "long_call"
    assert out.leg.atm_strike == 25000
    assert out.leg.strike == 25000
    assert out.leg.strike_offset == 0
    assert out.entry_open_paisa == 12000
    assert out.exit_open_paisa == 15000
    assert out.iv_at_entry == Decimal("0.15")

    b = out.breakdown
    assert b.n_shares == 25
    assert b.buy_fill_paisa == 12005
    assert b.sell_fill_paisa == 14995
    assert b.brokerage_paisa == 4000
    assert b.stt_paisa == 200
    assert b.exch_paisa == 236
    assert b.sebi_paisa == 0
    assert b.ipft_paisa == 4
    assert b.stamp_paisa == 0
    assert b.gst_paisa == 800
    assert b.total_charges_paisa == 5240
    assert b.gross_pnl_paisa == 74750
    assert b.net_pnl_paisa == 69510
    assert out.net_pnl_paisa == 69510


def test_golden_sensex_bear_atm_plus_1() -> None:
    sig = SpotSignal(
        instrument=OptionsUnderlying.SENSEX,
        direction=TradeDirection.BEAR,
        entry_ts=_ENTRY_TS,
        exit_ts=_EXIT_TS,
        spot_at_entry=Decimal("81012"),
        expiry_date=date(2024, 5, 30),
    )
    out = materialize_trade(
        sig, RULES_BY_NAME["atm_plus_1_in_dir"], source=_Source("200.00", "50.00")
    )
    assert isinstance(out, MaterializedTrade)

    assert out.leg.option_type == "PE"
    assert out.leg.side == "long_put"
    assert out.leg.atm_strike == 81000
    assert out.leg.strike == 80900
    assert out.leg.strike_offset == -1
    assert out.entry_open_paisa == 20000
    assert out.exit_open_paisa == 5000

    b = out.breakdown
    assert b.n_shares == 10
    assert b.buy_fill_paisa == 20005
    assert b.sell_fill_paisa == 4995
    assert b.brokerage_paisa == 4000
    assert b.stt_paisa == 0
    assert b.exch_paisa == 81
    assert b.sebi_paisa == 0
    assert b.ipft_paisa == 0
    assert b.stamp_paisa == 0
    assert b.gst_paisa == 700
    assert b.total_charges_paisa == 4781
    assert b.gross_pnl_paisa == -150100
    assert b.net_pnl_paisa == -154881
