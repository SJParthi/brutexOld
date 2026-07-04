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

from bisect import bisect_right
from collections.abc import Sequence
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

GROWW_BROKERAGE_PAISA: Final[int] = 2_000
"""Groww: ₹20 per executed order (flat for F&O). Same whether the
order is 1 lot or 1000 lots. CALCULATOR_SPEC §4.4 corrects an
earlier wrong "₹20 per contract" claim. Source: COSTS_VERIFIED §8.1."""

ZERODHA_BROKERAGE_PAISA: Final[int] = 2_000
"""Zerodha: ₹20 per executed order (flat for F&O, since June 2024).
Same flat rate as Groww for our 1-lot-per-trade simulator design.
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
#
# The exchange transaction charge (ETC) — like STT — is a DATE-REGIME
# quantity: the current NSE 0.03503% / BSE 0.03250% rates took effect
# 2024-10-01 (NSE circular FA64232 Oct-2024 revision; BSE notice
# 27-Sep-2024 — both cited in COSTS_VERIFIED §2/§15, alongside the
# Zerodha Z-Connect "revision in exchange transaction charges … from
# October 1, 2024" secondary source). The rates in force BEFORE
# 2024-10-01 are **UNVERIFIED OFFLINE** (Golden Rule #1 — the specific
# pre-revision circular values have not been citation-grounded in this
# repo), so the regime tables below deliberately carry the CURRENT
# verified rate as an UNVERIFIED placeholder for ALL earlier dates:
# behaviour is byte-identical to the previous date-blind lookup, and a
# verified historical rate slots in as one extra table row without any
# re-architecting.
#
# HONEST DIRECTION NOTE: the widely-reported pre-Oct-2024 NSE options
# ETC (~0.0495–0.050%, UNVERIFIED offline) was HIGHER than the current
# 0.03503%, so a pre-2024-10-01 trade is likely UNDER-costed by this
# placeholder until the real circular values land.
#
# TODO(UNVERIFIED): source the pre-2024-10-01 NSE F&O options ETC rate
# (the circular FA64232 supersedes — need the prior fee circular) and
# the pre-2024-10-01 BSE SENSEX/Bankex options rate (the BSE notice
# 27-Sep-2024 supersedes), then replace the ``date.min`` placeholder
# rows below with the true ``(effective_date, rate)`` history.

NSE_ETC_OPTIONS_BPS_X100: Final[int] = 3_503
"""NSE F&O exchange charge: 0.03503% on options premium, both sides,
effective 2024-10-01 (NSE FA64232 / FA56129; COSTS_VERIFIED §3).
Pre-2024-10-01 the rate differed — see the UNVERIFIED note above:
until the prior circular values are sourced, this rate is applied to
ALL dates (pre-boundary trades are likely UNDER-costed)."""

BSE_ETC_OPTIONS_BPS_X100: Final[int] = 3_250
"""BSE F&O exchange charge: 0.03250% on options premium, both sides,
effective 2024-10-01 for SENSEX/Bankex. Source: BSE notice 27-Sep-2024;
COSTS_VERIFIED §3. Pre-2024-10-01 the rate differed — see the
UNVERIFIED note above: until the prior notice values are sourced, this
rate is applied to ALL dates."""

ETC_REGIME_OCT_2024_START: Final[date] = date(2024, 10, 1)
"""Boundary date: the current NSE/BSE exchange-transaction-charge rates
take effect (NSE FA64232; BSE notice 27-Sep-2024; SEBI true-to-label)."""


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

GST_TOTAL_BPS_X100: Final[int] = 1_800_000
"""GST 18% on (brokerage + exchange + SEBI + IPFT). Per DEC-COST-001
(2026-05-16), the TOTAL is rounded ONCE — NOT CGST/SGST separately.
Section 170 CGST mandates rounding the total tax. Earlier spec rounded
each component separately at 9%, creating systematic +₹1 overcharge.

Encoding: rate_decimal × 10_000_000 = 0.18 × 10_000_000 = 1_800_000."""


# ─────────────────────────────────────────────────────────────────
# Date-regime lookup (COMMON — one mechanism for STT AND ETC)
# ─────────────────────────────────────────────────────────────────

#: A date-regime table: ``((effective_date, rate_bps_x100), …)`` sorted
#: ascending by effective_date. The rate in force on a date D is the
#: rate of the LATEST row whose ``effective_date <= D``.
RegimeTable = tuple[tuple[date, int], ...]


def rate_for_date(regimes: Sequence[tuple[date, int]], on_date: date) -> int:
    """Return the rate in force on *on_date* from a date-regime table.

    The ONE shared regime-lookup mechanism (CLAUDE.md §11 COMMON) used
    by the STT options / STT exercise / NSE ETC / BSE ETC tables below.
    O(log N) bisect over N regime rows (N ≤ 3 today — effectively O(1)).

    Parameters
    ----------
    regimes : Sequence[tuple[date, int]]
        ``(effective_date, rate_bps_x100)`` rows sorted ascending. A
        table whose first row starts at ``date.min`` covers every date.
    on_date : date
        The trade's regime-selection date (per DEC-COST-002 this is
        ``entry_dt_ist.date()`` for every per-trade charge).

    Returns
    -------
    int
        The rate (bps×100) of the latest regime with
        ``effective_date <= on_date``.

    Raises
    ------
    ValueError
        If *regimes* is empty, or *on_date* precedes the first row's
        effective date — no rate is ever fabricated for a date outside
        the recorded history (Golden Rule #1). Unreachable for the
        shipped tables (each is anchored at ``date.min``); the guard
        exists so a future table whose UNVERIFIED placeholder anchor is
        REMOVED fails loud instead of guessing.
    """
    if not regimes:
        msg = "rate_for_date: empty regime table"
        raise ValueError(msg)
    if on_date < regimes[0][0]:
        msg = (
            f"rate_for_date: on_date {on_date} precedes the first recorded "
            f"regime ({regimes[0][0]}) — refusing to fabricate a rate"
        )
        raise ValueError(msg)
    idx = bisect_right([start for start, _ in regimes], on_date) - 1
    return regimes[idx][1]


# STT options (sell premium): the fully-VERIFIED 3-regime history.
_STT_OPTIONS_REGIMES: Final[RegimeTable] = (
    (date.min, STT_OPTIONS_PRE_OCT_2024_BPS_X100),
    (STT_REGIME_OCT_2024_START, STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100),
    (STT_REGIME_APR_2026_START, STT_OPTIONS_POST_APR_2026_BPS_X100),
)

# STT on exercise: the VERIFIED 2-regime history (DEC-COST-004).
_STT_EXERCISE_REGIMES: Final[RegimeTable] = (
    (date.min, STT_EXERCISE_PRE_APR_2026_BPS_X100),
    (STT_REGIME_APR_2026_START, STT_EXERCISE_POST_APR_2026_BPS_X100),
)

# ETC: ONLY the current (effective 2024-10-01) rate is VERIFIED. The
# ``date.min`` anchor row is an explicit UNVERIFIED placeholder carrying
# the current rate back through history so behaviour is unchanged — see
# the TODO(UNVERIFIED) block above the ETC constants. When the real
# pre-2024-10-01 circular rates land, each becomes its own row here
# (e.g. ``(date.min, <verified 2020 rate>), (<mid change>, …),
# (ETC_REGIME_OCT_2024_START, NSE_ETC_OPTIONS_BPS_X100)``) with NO
# mechanism change.
_NSE_ETC_OPTIONS_REGIMES: Final[RegimeTable] = (
    (date.min, NSE_ETC_OPTIONS_BPS_X100),  # UNVERIFIED pre-2024-10-01 placeholder
    (ETC_REGIME_OCT_2024_START, NSE_ETC_OPTIONS_BPS_X100),  # VERIFIED current
)

_BSE_ETC_OPTIONS_REGIMES: Final[RegimeTable] = (
    (date.min, BSE_ETC_OPTIONS_BPS_X100),  # UNVERIFIED pre-2024-10-01 placeholder
    (ETC_REGIME_OCT_2024_START, BSE_ETC_OPTIONS_BPS_X100),  # VERIFIED current
)


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def stt_options_rate_bps_x100(entry_date: date) -> int:
    """Pick the STT rate (options sell premium) for a given entry date.

    Per DEC-COST-002, the entry date determines which regime applies.
    Delegates to the COMMON :func:`rate_for_date` mechanism (byte-
    identical to the previous hand-rolled if-ladder across all three
    regimes + both boundaries — regression-pinned).

    Parameters
    ----------
    entry_date : date
        The trade's entry date in IST.

    Returns
    -------
    int
        STT rate in bps×100 (e.g. 10_000 for 0.10%).
    """
    return rate_for_date(_STT_OPTIONS_REGIMES, entry_date)


def stt_exercise_rate_bps_x100(entry_date: date) -> int:
    """Pick the STT-on-exercise rate for a given entry date.

    Used only when trade_outcome == EXPIRY_EXERCISE. Track 2 never
    sets this (intraday only per DEC-RISK-011); kept as forward-compat
    stub for future tracks that hold to expiry. Delegates to the COMMON
    :func:`rate_for_date` mechanism.

    Parameters
    ----------
    entry_date : date
        The trade's entry date in IST.

    Returns
    -------
    int
        STT-on-exercise rate in bps×100 (e.g. 15_000 for 0.15%).
    """
    return rate_for_date(_STT_EXERCISE_REGIMES, entry_date)


def exchange_charge_bps_x100(instrument: str, on_date: date) -> int:
    """Pick the exchange transaction-charge rate for an instrument + date.

    NIFTY 50 / BANKNIFTY trade on NSE; SENSEX trades on BSE. The rate is
    date-regime-selected through the COMMON :func:`rate_for_date`
    mechanism, mirroring the STT lookup (per DEC-COST-002 the caller
    passes ``entry_dt_ist.date()``).

    HONESTY (Golden Rule #1): only the CURRENT rates (effective
    2024-10-01 — NSE FA64232 / BSE notice 27-Sep-2024) are verified. The
    pre-2024-10-01 rates are UNVERIFIED OFFLINE, so the tables carry the
    current rate as an explicit placeholder for earlier dates — i.e. a
    pre-2024-10-01 trade is charged today's rate and is likely
    UNDER-costed (the widely-reported prior NSE rate ~0.0495–0.050% was
    HIGHER, itself unverified) pending the prior circular values. See
    the TODO(UNVERIFIED) block above the ETC constants.
    """
    if instrument == "SENSEX":
        return rate_for_date(_BSE_ETC_OPTIONS_REGIMES, on_date)
    return rate_for_date(_NSE_ETC_OPTIONS_REGIMES, on_date)


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
    return GROWW_BROKERAGE_PAISA
