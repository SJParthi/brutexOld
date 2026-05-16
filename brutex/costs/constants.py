"""Verified rate constants for the Track 2 cost calculator.

All values sourced from `docs/track2/COSTS_VERIFIED.md` (LOCKED
2026-05-16). Each constant is annotated with its statutory citation
or broker-page source.

Encoding convention
-------------------
- Money: int64 paise (1 INR = 100 paise). NEVER float.
- Rates: integer basis-points × 100 = bps_x100. So 0.10% = 1000.
  This avoids float multiplication in the hot path.
- Dates: ``datetime.date`` for regime-switch boundaries.

Per `CALCULATOR_SPEC.md §2` table, all rates here are locked
single-source-of-truth. Tests verify the math matches the worked
examples in `COSTS_VERIFIED.md §5`.
"""

from __future__ import annotations

from datetime import date
from typing import Final

# ─────────────────────────────────────────────────────────────────
# Money / quantum constants
# ─────────────────────────────────────────────────────────────────

TICK_PAISA: Final[int] = 5
"""Option premium tick size: ₹0.05 = 5 paise. NSE/BSE option strikes
all trade in multiples of this tick. Source: NSE/BSE option contract
specs; COSTS_VERIFIED §11."""


# ─────────────────────────────────────────────────────────────────
# Brokerage (per executed order, flat)
# ─────────────────────────────────────────────────────────────────

DHAN_BROKERAGE_PAISA: Final[int] = 2_000
"""Dhan: ₹20 per executed order (flat for F&O). Same whether the
order is 1 lot or 1000 lots. CALCULATOR_SPEC §4.4 corrects an
earlier wrong "₹20 per contract" claim. Source: COSTS_VERIFIED §8.1."""

ZERODHA_BROKERAGE_PAISA: Final[int] = 2_000
"""Zerodha: ₹20 per executed order (flat for F&O, since June 2024).
Same flat rate as Dhan for our 1-lot-per-trade simulator design.
Source: COSTS_VERIFIED §8.2."""


# ─────────────────────────────────────────────────────────────────
# Securities Transaction Tax (STT)
# ─────────────────────────────────────────────────────────────────
#
# STT has 3 distinct regimes for options (sell-side premium):
#
#   Pre 2024-10-01:  0.0625% (= 6250 bps×100)
#   2024-10-01 →    0.10%   (= 10000 bps×100) — Finance Act 2024
#   2026-04-01 →    0.15%   (= 15000 bps×100) — Finance Act 2026 (DEC-COST-003)
#
# DEC-COST-002 (2026-05-16): the regime is selected by entry_dt.date()
# of the trade. Boundary handling: if entry_date >= regime.start_date,
# use that regime.

STT_OPTIONS_PRE_OCT_2024_BPS_X100: Final[int] = 6_250
"""STT 0.0625% on sell-side options premium, pre 2024-10-01."""

STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100: Final[int] = 10_000
"""STT 0.10% on sell-side options premium, 2024-10-01 to 2026-03-31.
Source: Zerodha Z-Connect, ClearTax, Finance Act 2024 gazette."""

STT_OPTIONS_POST_APR_2026_BPS_X100: Final[int] = 15_000
"""STT 0.15% on sell-side options premium, from 2026-04-01.
Source: Finance Bill 2026 (DEC-COST-003); VERIFIED via TaxGuru,
Wikipedia STT page, 1Finance citing indiabudget.gov.in/doc/Finance_Bill.pdf."""

STT_EXERCISE_PRE_APR_2026_BPS_X100: Final[int] = 12_500
"""STT on options exercise: 0.125% on intrinsic value, pre 2026-04-01.
Used only when trade_outcome == EXPIRY_EXERCISE (Track 2 never sets
this; forward-compat stub)."""

STT_EXERCISE_POST_APR_2026_BPS_X100: Final[int] = 15_000
"""STT on options exercise: 0.15% on intrinsic value, from 2026-04-01.
Source: Finance Act 2026 (DEC-COST-004)."""

STT_REGIME_OCT_2024_START: Final[date] = date(2024, 10, 1)
"""Boundary date: Finance Act 2024 STT hike takes effect."""

STT_REGIME_APR_2026_START: Final[date] = date(2026, 4, 1)
"""Boundary date: Finance Act 2026 STT hike takes effect."""


# ─────────────────────────────────────────────────────────────────
# Exchange transaction charges (premium both sides)
# ─────────────────────────────────────────────────────────────────

NSE_ETC_OPTIONS_BPS_X100: Final[int] = 3_503
"""NSE F&O exchange charge: 0.03503% on options premium, both sides.
Source: NSE FA64232 / FA56129; COSTS_VERIFIED §3."""

BSE_ETC_OPTIONS_BPS_X100: Final[int] = 3_250
"""BSE F&O exchange charge: 0.03250% on options premium, both sides
(post 2024-10-01 for SENSEX/Bankex). Source: BSE notice 27-Sep-2024;
COSTS_VERIFIED §3."""


# ─────────────────────────────────────────────────────────────────
# SEBI turnover fee
# ─────────────────────────────────────────────────────────────────

SEBI_FEE_BPS_X100: Final[int] = 10
"""SEBI turnover fee: 0.0001% (= ₹10/cr) on premium both sides.
Source: sebi.gov.in fee schedule; COSTS_VERIFIED §4."""


# ─────────────────────────────────────────────────────────────────
# IPFT (Investor Protection Fund Trust)
# ─────────────────────────────────────────────────────────────────

NSE_IPFT_BPS_X100: Final[int] = 50
"""NSE IPFT: 0.0005% (= ₹50/cr) on premium both sides.
Source: NSE/FA/56129; COSTS_VERIFIED §5."""

BSE_IPFT_BPS_X100: Final[int] = 0
"""BSE IPFT: near-zero (UNVERIFIED whether exactly 0 or trivially small).
Track 2 treats as 0; flagged in COSTS_VERIFIED §9 UNVERIFIED list."""


# ─────────────────────────────────────────────────────────────────
# Stamp duty (buy-side only)
# ─────────────────────────────────────────────────────────────────

STAMP_DUTY_OPTIONS_BPS_X100: Final[int] = 300
"""Stamp duty: 0.003% (= ₹300/cr) on options premium, buy side ONLY.
Uniform across India since 2020-07-01 (Indian Stamp Act 2020).
Source: COSTS_VERIFIED §6."""


# ─────────────────────────────────────────────────────────────────
# GST
# ─────────────────────────────────────────────────────────────────

GST_TOTAL_BPS_X100: Final[int] = 180_000
"""GST 18% on (brokerage + exchange + SEBI + IPFT). Per DEC-COST-001
(2026-05-16), the TOTAL is rounded ONCE — NOT CGST/SGST separately.
Section 170 CGST mandates rounding the total tax. Earlier spec rounded
each component separately at 9%, creating systematic +₹1 overcharge."""


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def stt_options_rate_bps_x100(entry_date: date) -> int:
    """Pick the STT rate (options sell premium) for a given entry date.

    Per DEC-COST-002, the entry date determines which regime applies.

    Parameters
    ----------
    entry_date : date
        The trade's entry date in IST.

    Returns
    -------
    int
        STT rate in bps×100 (e.g. 10_000 for 0.10%).
    """
    if entry_date >= STT_REGIME_APR_2026_START:
        return STT_OPTIONS_POST_APR_2026_BPS_X100
    if entry_date >= STT_REGIME_OCT_2024_START:
        return STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100
    return STT_OPTIONS_PRE_OCT_2024_BPS_X100


def stt_exercise_rate_bps_x100(entry_date: date) -> int:
    """Pick the STT-on-exercise rate for a given entry date.

    Used only when trade_outcome == EXPIRY_EXERCISE. Track 2 never
    sets this (intraday only per DEC-RISK-011); kept as forward-compat
    stub for future tracks that hold to expiry.

    Parameters
    ----------
    entry_date : date
        The trade's entry date in IST.

    Returns
    -------
    int
        STT-on-exercise rate in bps×100 (e.g. 15_000 for 0.15%).
    """
    if entry_date >= STT_REGIME_APR_2026_START:
        return STT_EXERCISE_POST_APR_2026_BPS_X100
    return STT_EXERCISE_PRE_APR_2026_BPS_X100


def exchange_charge_bps_x100(instrument: str) -> int:
    """Pick the exchange transaction-charge rate for an instrument.

    NIFTY 50 / BANKNIFTY trade on NSE; SENSEX trades on BSE.
    """
    if instrument == "SENSEX":
        return BSE_ETC_OPTIONS_BPS_X100
    return NSE_ETC_OPTIONS_BPS_X100


def ipft_bps_x100(instrument: str) -> int:
    """Pick the IPFT rate for an instrument."""
    if instrument == "SENSEX":
        return BSE_IPFT_BPS_X100
    return NSE_IPFT_BPS_X100


def brokerage_paisa(broker: str) -> int:
    """Pick the per-order brokerage in paise for a broker.

    Note: per CALCULATOR_SPEC §4.4 this is per executed ORDER, not
    per contract. 1 lot or 1000 lots = same ₹20.
    """
    if broker == "zerodha":
        return ZERODHA_BROKERAGE_PAISA
    return DHAN_BROKERAGE_PAISA
