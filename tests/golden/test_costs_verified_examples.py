"""Golden oracles: COSTS_VERIFIED.md §5 Examples 1-4 vs the REAL calculator.

R2-OC-9: the doc's worked examples had silently drifted from the code
(Example 3 said ₹52.19 where the calculator produces ₹51.18 — the doc
rounded CGST/SGST separately, the exact pre-DEC-COST-001 defect its own §4
documents as fixed, and mis-rounded a sub-paise SEBI fee to ₹0.01). The §5
tables were REGENERATED 2026-07-03 from actual calculator output; this test
recomputes each example's scenario through ``compute_trade_cost`` and asserts
every regenerated figure, so the doc is enforceably the oracle again (the
``TradeCostBreakdown`` docstring's "byte-exact oracles" claim).

Example 5 (Muhurat ₹0 brokerage) is deliberately NOT here — the calculator
has no Muhurat branch, so the doc marks it SPEC-ONLY.

Common fixture (per the doc): trade date 2026-05-15 IST (post-1-Apr-2026 STT
0.15%; lots NIFTY 65 / BANKNIFTY 30 / SENSEX 20), long_call, Groww,
intra-state GST; the calculator bakes the flat 1-tick adverse slippage into
the fills (buy open+₹0.05, sell open−₹0.05).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from brutex.costs.calculator import compute_trade_cost
from brutex.costs.types import Instrument, TradeCostBreakdown, TradeInput

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2026, 5, 15, 9, 16, tzinfo=_IST)
_EXIT = datetime(2026, 5, 15, 15, 20, tzinfo=_IST)
_EXPIRY = date(2026, 5, 28)


def _run(*, instrument: Instrument, entry_p: int, exit_p: int, n_lots: int) -> TradeCostBreakdown:
    return compute_trade_cost(
        TradeInput(
            entry_dt_ist=_ENTRY,
            exit_dt_ist=_EXIT,
            instrument=instrument,
            option_type="CE",
            strike=25_000 * 100,
            expiry_date=_EXPIRY,
            entry_bar_open_paisa=entry_p,
            exit_bar_open_paisa=exit_p,
            side="long_call",
            n_lots=n_lots,
        )
    )


def test_example_1_nifty_one_lot() -> None:
    """§5 Example 1: NIFTY ATM call, 1 lot, ₹100 → ₹120."""
    b = _run(instrument="NIFTY", entry_p=10_000, exit_p=12_000, n_lots=1)
    assert b.n_shares_per_lot == 65
    assert b.n_shares == 65
    assert b.buy_fill_paisa == 10_005  # ₹100.05
    assert b.sell_fill_paisa == 11_995  # ₹119.95
    assert b.buy_notional_paisa == 650_325  # ₹6,503.25
    assert b.sell_notional_paisa == 779_675  # ₹7,796.75
    assert b.brokerage_paisa == 4_000  # ₹40.00
    assert b.stt_paisa == 1_200  # ₹12.00
    assert b.exch_paisa == 501  # ₹5.01
    assert b.sebi_paisa == 2  # ₹0.02
    assert b.ipft_paisa == 7  # ₹0.07
    assert b.stamp_paisa == 0  # ₹0.00
    assert b.gst_paisa == 800  # ₹8.00 — 18% of ₹45.10 rounded ONCE
    assert b.total_charges_paisa == 6_510  # ₹65.10
    assert b.gross_pnl_paisa == 129_350  # +₹1,293.50
    assert b.net_pnl_paisa == 122_840  # +₹1,228.40
    assert b.lot_size_basis == "trade_date"


def test_example_2_nifty_five_lots() -> None:
    """§5 Example 2: NIFTY ATM call, 5 lots (multi-lot; brokerage flat)."""
    b = _run(instrument="NIFTY", entry_p=10_000, exit_p=12_000, n_lots=5)
    assert b.n_shares == 325
    assert b.buy_notional_paisa == 3_251_625  # ₹32,516.25
    assert b.sell_notional_paisa == 3_898_375  # ₹38,983.75
    assert b.brokerage_paisa == 4_000  # flat ₹40 regardless of lots
    assert b.stt_paisa == 5_800  # ₹58.00 (the pre-regeneration doc's ₹59 drift)
    assert b.exch_paisa == 2_505  # ₹25.05
    assert b.sebi_paisa == 7  # ₹0.07
    assert b.ipft_paisa == 35  # ₹0.35
    assert b.stamp_paisa == 100  # ₹1.00
    assert b.gst_paisa == 1_200  # ₹12.00 — 18% of ₹65.47 rounded ONCE
    assert b.total_charges_paisa == 13_647  # ₹136.47 (doc said ₹137.48 pre-regen)
    assert b.gross_pnl_paisa == 646_750  # +₹6,467.50
    assert b.net_pnl_paisa == 633_103  # +₹6,331.03


def test_example_3_banknifty_one_lot() -> None:
    """§5 Example 3: BANKNIFTY ATM, 1 lot, ₹50 → ₹60 — THE R2-OC-9 drift.

    The doc claimed ₹52.19 (CGST/SGST rounded separately → GST ₹8; SEBI
    ₹0.01). The real calculator: GST rounded ONCE → ₹7; SEBI ₹0.0033 → ₹0.00.
    """
    b = _run(instrument="BANKNIFTY", entry_p=5_000, exit_p=6_000, n_lots=1)
    assert b.n_shares_per_lot == 30
    assert b.buy_notional_paisa == 150_150  # ₹1,501.50
    assert b.sell_notional_paisa == 179_850  # ₹1,798.50
    assert b.brokerage_paisa == 4_000
    assert b.stt_paisa == 300  # ₹3.00
    assert b.exch_paisa == 116  # ₹1.16
    assert b.sebi_paisa == 0  # ₹0.00 — NOT the doc's old ₹0.01
    assert b.ipft_paisa == 2  # ₹0.02
    assert b.stamp_paisa == 0
    assert b.gst_paisa == 700  # ₹7.00 — round-ONCE (NOT ₹4 + ₹4 = ₹8)
    assert b.total_charges_paisa == 5_118  # ₹51.18 — NOT the doc's old ₹52.19
    assert b.gross_pnl_paisa == 29_700  # +₹297.00
    assert b.net_pnl_paisa == 24_582  # +₹245.82


def test_example_4_sensex_one_lot_bse() -> None:
    """§5 Example 4: SENSEX ATM, 1 lot, ₹80 → ₹100 (BSE rates, IPFT 0)."""
    b = _run(instrument="SENSEX", entry_p=8_000, exit_p=10_000, n_lots=1)
    assert b.n_shares_per_lot == 20
    assert b.buy_notional_paisa == 160_100  # ₹1,601.00
    assert b.sell_notional_paisa == 199_900  # ₹1,999.00
    assert b.brokerage_paisa == 4_000
    assert b.stt_paisa == 300  # ₹3.00
    assert b.exch_paisa == 117  # ₹1.17 (BSE 0.03250%)
    assert b.sebi_paisa == 0  # ₹0.00
    assert b.ipft_paisa == 0  # BSE IPFT treated as 0 (UNVERIFIED)
    assert b.stamp_paisa == 0
    assert b.gst_paisa == 700  # ₹7.00 — round-ONCE (doc's old ₹8 was round-twice)
    assert b.total_charges_paisa == 5_117  # ₹51.17 (doc said ₹52.17 pre-regen)
    assert b.gross_pnl_paisa == 39_800  # +₹398.00
    assert b.net_pnl_paisa == 34_683  # +₹346.83
