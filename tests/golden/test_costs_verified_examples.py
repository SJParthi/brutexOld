"""Golden oracles: COSTS_VERIFIED.md §5 Examples 1-4 vs the REAL calculator.

R2-OC-9 history: the doc's worked examples had silently drifted from the code
(Example 3 said ₹52.19 where the calculator produced ₹51.18 — the doc
rounded CGST/SGST separately, the exact pre-DEC-COST-001 defect its own §4
documents as fixed). The §5 tables were regenerated 2026-07-03 from actual
calculator output and this suite enforces them byte-for-byte.

ONE-FILL-LAW RE-LOCK (DEC-FILL-WORST-CASE-ONLY-001): worst-case is now the
ONLY fill law, which changes this suite in TWO ways —

1. **Fill anchors.** Each leg's 1-tick adverse slippage now anchors on the
   fill bar's ADVERSE EXTREME (long: entry HIGH + tick / exit LOW − tick) and
   the four extremes are REQUIRED inputs. Examples 1-4 use FLAT fill bars
   (high == low == open) by construction, so the worst-case anchor coincides
   with the retired open anchor and every fill/notional/gross figure is
   provably invariant (buy 10_005 / sell 11_995 etc.). Example W is the
   NON-flat worst-case-anchor golden.
2. **Adverse levy rounding is PERMANENT.** The DEC-FILL-WORST-CASE-ONLY-001
   fix-round-2 adverse-levy law (every rate-scaled levy rounds ADVERSELY —
   ceiling toward +∞; paisa for exch/SEBI/IPFT, whole-rupee for
   STT/stamp/GST) was worst-case-mode-conditional; with the mode axis deleted
   it applies unconditionally, so the charge stack below is RE-DERIVED BY
   HAND under ceiling rounding (each changed figure annotated). The §5 doc
   tables are regenerated to these values in the same change.

Example 5 (Muhurat ₹0 brokerage) is deliberately NOT here — the calculator
has no Muhurat branch, so the doc marks it SPEC-ONLY.

Common fixture (per the doc): trade date 2026-05-15 IST (post-1-Apr-2026 STT
0.15%; lots NIFTY 65 / BANKNIFTY 30 / SENSEX 20), long_call, Groww,
intra-state GST. Rates: brokerage flat ₹40/order-pair; exchange txn NSE
0.03503% / BSE 0.03250% per leg; SEBI 0.0001% per leg; NSE IPFT 0.0005% per
leg (BSE 0); stamp 0.003% buy leg (rupee); STT 0.15% sell leg (rupee);
GST 18% of (brokerage + exch + SEBI + IPFT), rounded ONCE (rupee).
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


def _run(
    *,
    instrument: Instrument,
    entry_p: int,
    exit_p: int,
    n_lots: int,
    entry_high: int | None = None,
    entry_low: int | None = None,
    exit_high: int | None = None,
    exit_low: int | None = None,
) -> TradeCostBreakdown:
    """§5 scenario runner — FLAT fill bars by default (extreme == open).

    A flat fill bar makes the worst-case adverse-extreme anchor equal the
    bar open, keeping Examples 1-4's fills/notionals/gross byte-identical
    to the pre-relock tables; Example W overrides the extremes.
    """
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
            entry_bar_high_paisa=entry_high if entry_high is not None else entry_p,
            entry_bar_low_paisa=entry_low if entry_low is not None else entry_p,
            exit_bar_high_paisa=exit_high if exit_high is not None else exit_p,
            exit_bar_low_paisa=exit_low if exit_low is not None else exit_p,
        )
    )


def test_example_1_nifty_one_lot() -> None:
    """§5 Example 1: NIFTY ATM call, 1 lot, ₹100 → ₹120 (flat fill bars).

    HAND-DERIVED levy stack (adverse/ceiling rounding, per-leg where noted):
    exch = ceil(650_325×0.03503%)=ceil(227.81)=228 + ceil(779_675×0.03503%)
    =ceil(273.12)=274 → 502; SEBI = ceil(0.65)+ceil(0.78) = 2;
    IPFT = ceil(3.25)+ceil(3.90) = 4+4 = 8; stamp = 650_325×0.003% = 19.51p
    → ceil to rupee → 100; STT = 779_675×0.15% = 1_169.5p → ceil rupee →
    1_200; GST = 18%×(4_000+502+2+8) = 812.16p → ceil rupee → 900.
    """
    b = _run(instrument="NIFTY", entry_p=10_000, exit_p=12_000, n_lots=1)
    assert b.n_shares_per_lot == 65
    assert b.n_shares == 65
    assert b.buy_fill_paisa == 10_005  # ₹100.05 — flat bar: HIGH(10_000)+tick
    assert b.sell_fill_paisa == 11_995  # ₹119.95 — flat bar: LOW(12_000)−tick
    assert b.buy_notional_paisa == 650_325  # ₹6,503.25
    assert b.sell_notional_paisa == 779_675  # ₹7,796.75
    assert b.brokerage_paisa == 4_000  # ₹40.00
    assert b.stt_paisa == 1_200  # ₹12.00 (ceil ₹11.695 → ₹12)
    assert b.exch_paisa == 502  # ₹5.02 — per-leg CEIL (half-up gave 501)
    assert b.sebi_paisa == 2  # ₹0.02
    assert b.ipft_paisa == 8  # ₹0.08 — per-leg CEIL (half-up gave 7)
    assert b.stamp_paisa == 100  # ₹1.00 — CEIL of 19.51p (half-up gave 0)
    assert b.gst_paisa == 900  # ₹9.00 — CEIL of 812.16p, rounded ONCE
    assert b.total_charges_paisa == 6_712  # ₹67.12 = 40+12+5.02+0.02+0.08+1+9
    assert b.gross_pnl_paisa == 129_350  # +₹1,293.50
    assert b.net_pnl_paisa == 122_638  # 129_350 − 6_712
    assert b.lot_size_basis == "trade_date"


def test_example_2_nifty_five_lots() -> None:
    """§5 Example 2: NIFTY ATM call, 5 lots (multi-lot; brokerage flat).

    HAND-DERIVED: exch = ceil(3_251_625×0.03503% = 1_139.04) = 1_140 +
    ceil(3_898_375×0.03503% = 1_365.60) = 1_366 → 2_506; SEBI = ceil(3.25)
    + ceil(3.90) = 8; IPFT = ceil(16.26) + ceil(19.49) = 17+20 = 37;
    stamp = 3_251_625×0.003% = 97.55p → ceil rupee → 100; STT =
    3_898_375×0.15% = 5_847.56p → ceil rupee → 5_900; GST = 18%×(4_000+
    2_506+8+37) = 1_179.18p → ceil rupee → 1_200.
    """
    b = _run(instrument="NIFTY", entry_p=10_000, exit_p=12_000, n_lots=5)
    assert b.n_shares == 325
    assert b.buy_notional_paisa == 3_251_625  # ₹32,516.25
    assert b.sell_notional_paisa == 3_898_375  # ₹38,983.75
    assert b.brokerage_paisa == 4_000  # flat ₹40 regardless of lots
    assert b.stt_paisa == 5_900  # ₹59.00 — CEIL (half-up gave 5_800)
    assert b.exch_paisa == 2_506  # ₹25.06 — per-leg CEIL (half-up gave 2_505)
    assert b.sebi_paisa == 8  # ₹0.08 — per-leg CEIL (half-up gave 7)
    assert b.ipft_paisa == 37  # ₹0.37 — per-leg CEIL (half-up gave 35)
    assert b.stamp_paisa == 100  # ₹1.00
    assert b.gst_paisa == 1_200  # ₹12.00 — CEIL, rounded ONCE
    assert b.total_charges_paisa == 13_751  # ₹137.51
    assert b.gross_pnl_paisa == 646_750  # +₹6,467.50
    assert b.net_pnl_paisa == 632_999  # 646_750 − 13_751


def test_example_3_banknifty_one_lot() -> None:
    """§5 Example 3: BANKNIFTY ATM, 1 lot, ₹50 → ₹60 — the R2-OC-9 case.

    HAND-DERIVED: exch = ceil(150_150×0.03503% = 52.60) = 53 +
    ceil(179_850×0.03503% = 63.0015) = 64 → 117; SEBI = ceil(0.15) +
    ceil(0.18) = 2 (half-up gave 0); IPFT = ceil(0.75)+ceil(0.90) = 2;
    stamp = 150_150×0.003% = 4.50p → ceil rupee → 100 (half-up gave 0);
    STT = 179_850×0.15% = 269.78p → ceil rupee → 300; GST = 18%×(4_000+
    117+2+2) = 741.78p → ceil rupee → 800 — still rounded ONCE, never the
    doc's old separate-CGST/SGST ₹4+₹4.
    """
    b = _run(instrument="BANKNIFTY", entry_p=5_000, exit_p=6_000, n_lots=1)
    assert b.n_shares_per_lot == 30
    assert b.buy_notional_paisa == 150_150  # ₹1,501.50
    assert b.sell_notional_paisa == 179_850  # ₹1,798.50
    assert b.brokerage_paisa == 4_000
    assert b.stt_paisa == 300  # ₹3.00
    assert b.exch_paisa == 117  # ₹1.17 — per-leg CEIL (half-up gave 116)
    assert b.sebi_paisa == 2  # ₹0.02 — CEIL of sub-paisa legs (was 0)
    assert b.ipft_paisa == 2  # ₹0.02
    assert b.stamp_paisa == 100  # ₹1.00 — CEIL of 4.50p (was 0)
    assert b.gst_paisa == 800  # ₹8.00 — CEIL of 741.78p, rounded ONCE
    assert b.total_charges_paisa == 5_321  # ₹53.21
    assert b.gross_pnl_paisa == 29_700  # +₹297.00
    assert b.net_pnl_paisa == 24_379  # 29_700 − 5_321


def test_example_4_sensex_one_lot_bse() -> None:
    """§5 Example 4: SENSEX ATM, 1 lot, ₹80 → ₹100 (BSE rates, IPFT 0).

    HAND-DERIVED: exch (BSE 0.03250%) = ceil(160_100×0.0325% = 52.03) = 53
    + ceil(199_900×0.0325% = 64.97) = 65 → 118; SEBI = ceil(0.16) +
    ceil(0.20) = 2; IPFT = 0 (BSE, UNVERIFIED → 0); stamp = 160_100×0.003%
    = 4.80p → ceil rupee → 100; STT = 199_900×0.15% = 299.85p → ceil rupee
    → 300; GST = 18%×(4_000+118+2+0) = 741.6p → ceil rupee → 800.
    """
    b = _run(instrument="SENSEX", entry_p=8_000, exit_p=10_000, n_lots=1)
    assert b.n_shares_per_lot == 20
    assert b.buy_notional_paisa == 160_100  # ₹1,601.00
    assert b.sell_notional_paisa == 199_900  # ₹1,999.00
    assert b.brokerage_paisa == 4_000
    assert b.stt_paisa == 300  # ₹3.00
    assert b.exch_paisa == 118  # ₹1.18 — per-leg CEIL (half-up gave 117)
    assert b.sebi_paisa == 2  # ₹0.02 — CEIL (was 0)
    assert b.ipft_paisa == 0  # BSE IPFT treated as 0 (UNVERIFIED)
    assert b.stamp_paisa == 100  # ₹1.00 — CEIL of 4.80p (was 0)
    assert b.gst_paisa == 800  # ₹8.00 — CEIL, rounded ONCE
    assert b.total_charges_paisa == 5_320  # ₹53.20
    assert b.gross_pnl_paisa == 39_800  # +₹398.00
    assert b.net_pnl_paisa == 34_480  # 39_800 − 5_320


def test_example_w_worst_case_anchor_nifty() -> None:
    """The NON-flat worst-case-anchor golden (DEC-FILL-WORST-CASE-ONLY-001).

    Same Example-1 scenario but with RANGED fill bars, HAND-DERIVED:

    * entry bar: open 10_000, HIGH 10_050, low 9_980 → buy anchors the
      HIGH → fill = 10_050 + 5 = **10_055** (₹100.55 — never the open's
      10_005);
    * exit bar: open 12_000, high 12_040, LOW 11_900 → sell anchors the
      LOW → fill = 11_900 − 5 = **11_895** (₹118.95 — never the open's
      11_995);
    * notionals (65 shares): buy 10_055 × 65 = **653_575** (₹6,535.75),
      sell 11_895 × 65 = **773_175** (₹7,731.75);
    * gross P&L = 773_175 − 653_575 = **+119_600** (₹1,196.00) — 9_750p
      WORSE than Example 1's 129_350, the two bars' open→extreme adverse
      depth (50 + 100 paisa) × 65 shares.
    """
    b = _run(
        instrument="NIFTY",
        entry_p=10_000,
        exit_p=12_000,
        n_lots=1,
        entry_high=10_050,
        entry_low=9_980,
        exit_high=12_040,
        exit_low=11_900,
    )
    assert b.n_shares == 65
    assert b.buy_fill_paisa == 10_055  # HIGH-anchored, NOT open-anchored
    assert b.sell_fill_paisa == 11_895  # LOW-anchored, NOT open-anchored
    assert b.buy_notional_paisa == 653_575
    assert b.sell_notional_paisa == 773_175
    assert b.gross_pnl_paisa == 119_600
    # Calculator identity: net = gross − total charges (the levy stack
    # itself is oracle-locked by Examples 1-4 above).
    assert b.net_pnl_paisa == b.gross_pnl_paisa - b.total_charges_paisa
