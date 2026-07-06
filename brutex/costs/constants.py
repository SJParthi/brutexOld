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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from operator import attrgetter
from typing import Final

from brutex.costs.types import UnverifiedCostRegimeError

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
# quantity. What is VERIFIED (COSTS_VERIFIED §2 rows 3-4): the current
# NSE 0.03503% / BSE 0.03250% rates, effective 2024-10-01 (NSE circular
# FA64232; BSE notice 27-Sep-2024 [20240927-37] — both per the SEBI
# "true-to-label" circular SEBI/HO/MRD/TPD-1/P/CIR/2024/92, alongside the
# Zerodha Z-Connect secondary source). EXACTLY these two rate rows exist.
#
# Every PRE-2024-10-01 window is an explicit rate-less REFUSAL row
# (``RegimeRow.rate_bps_x100 = None`` — DEC-ETC-REGIME-HISTORY-001,
# MIGRATION.md #1523): the 2026-07-06 fetch round IDENTIFIED all the
# superseded circulars (NSE FA46730 / FA56129 / FA61137; BSE notices
# 20231020-46 / 20240430-42) but retrieved ZERO official bodies (all
# official hosts + web.archive.org egress-blocked), and the pre-boundary
# charges are structurally member-monthly-turnover SLAB ladders with no
# honest flat per-trade equivalent. Per Golden Rule #1 a date landing on
# a refusal row REFUSES pricing (``UnverifiedCostRegimeError``); the
# options executor converts that into a per-trade skip-and-count
# (``RejectReason.COST_REGIME_UNVERIFIED``) so the sim completes loudly
# with NO fabricated rate.
#
# HOW A VERIFIED REGIME LANDS: one cited ``RegimeRow(effective_date,
# rate_bps_x100, source)`` per circular + a MIGRATION.md ledger entry +
# golden re-derivation — ZERO mechanism change.
#
# NO RUNTIME ESCAPE HATCH (locked, DEC-ETC-REGIME-HISTORY-001 §9): no
# flag/kwarg may bypass the refusal — a fabricated statutory rate would
# fork every downstream number OUTSIDE blake3 run identity (Golden Rule
# #4). The ONLY sanctioned override paths are DATA: (a) a circular-cited
# RegimeRow; (b) an ADR-backed row stamped ``source="ASSUMED per
# ADR-00XX"``, admissible ONLY when the official full-ladder circular
# body is in-repo + the retail-top-slab assumption is documented with
# era evidence and a direction-of-error note (top slab = conservative
# over-costing) + an operator MIGRATION.md decision ID — and even then
# refusal stays preferred for the BSE Nov-2023 window (its ladder was
# nearest-expiry-scoped, inexpressible in this flat table).
#
# FORWARD-REGIME ALERT (UNVERIFIED — deliberately NOT encoded): NSE
# FA73061 (dated 2026-02-27, effective 2026-03-01) reportedly moved the
# options txn/IPFT split to ₹3,552 + ₹1 per cr (TOTAL unchanged at
# 0.03553%), which would bound the current NSE row's window. Until that
# circular body lands officially, the 3,503 + 50 split below stands —
# MIGRATION.md #1523 follow-up (i).
#
# TODO(UNVERIFIED): closes by replacing each ``rate_bps_x100=None``
# refusal row below with true cited ``RegimeRow(effective_date, rate,
# source)`` rows once the pre-2024-10-01 NSE/BSE circular bodies land
# (official-direct or official-archive transport only).

NSE_ETC_OPTIONS_BPS_X100: Final[int] = 3_503
"""NSE F&O exchange charge: 0.03503% on options premium, both sides,
effective 2024-10-01 (NSE FA64232, per SEBI 2024/92 true-to-label;
COSTS_VERIFIED §2 row 3 — VERIFIED). Pre-2024-10-01 dates land on the
refusal row — see the ETC regime block above; never priced with this
rate."""

BSE_ETC_OPTIONS_BPS_X100: Final[int] = 3_250
"""BSE F&O exchange charge: 0.03250% on options premium, both sides,
effective 2024-10-01 for SENSEX/Bankex (BSE notice 27-Sep-2024, per SEBI
2024/92 true-to-label; COSTS_VERIFIED §2 row 4 — VERIFIED).
Pre-2024-10-01 dates land on the refusal row — see the ETC regime block
above; never priced with this rate."""

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


@dataclass(frozen=True, slots=True)
class RegimeRow:
    """One dated cost-regime row (DEC-ETC-REGIME-HISTORY-001).

    ``rate_bps_x100=None`` == an explicit UNVERIFIED refusal window: any
    date landing on it REFUSES pricing (:class:`UnverifiedCostRegimeError`)
    — the charge-an-unverified-rate state is UNREPRESENTABLE (a
    cited-but-unverified figure lives in ``source`` as text, never in
    ``rate_bps_x100``).

    Parameters
    ----------
    start : date
        The row's effective date (inclusive); ``date.min`` anchors a
        history-start window.
    rate_bps_x100 : int | None
        The verified rate (bps×100), or ``None`` for a refusal window.
    source : str
        The citation (verified row) or the honest citation-gap stamp
        (refusal row). Never blank.

    Raises
    ------
    ValueError
        On a negative rate or a blank/whitespace-only source.
    """

    start: date
    rate_bps_x100: int | None
    source: str

    def __post_init__(self) -> None:
        if self.rate_bps_x100 is not None and self.rate_bps_x100 < 0:
            msg = (
                f"RegimeRow(start={self.start}): rate_bps_x100 "
                f"{self.rate_bps_x100} must be >= 0 or None (refusal row)"
            )
            raise ValueError(msg)
        if not self.source.strip():
            msg = (
                f"RegimeRow(start={self.start}): source must carry its citation "
                f"or its honest citation gap — got {self.source!r}"
            )
            raise ValueError(msg)

    @property
    def verified(self) -> bool:
        """Whether this row carries a verified rate (DERIVED — can never drift)."""
        return self.rate_bps_x100 is not None


#: A date-regime table: ``RegimeRow`` rows sorted strictly ascending by
#: ``start``. The row in force on a date D is the LATEST row whose
#: ``start <= D``; a ``rate_bps_x100=None`` row is a refusal window.
RegimeTable = tuple[RegimeRow, ...]


def validate_regime_table(regimes: RegimeTable, *, label: str) -> RegimeTable:
    """Fail-loud structural validation of a regime table, applied ONCE at
    import time to every shipped table (import-time ONLY — ``rate_for_date``
    does not re-validate per call; row-level rate/source checks live in
    ``RegimeRow.__post_init__``).

    Parameters
    ----------
    regimes : RegimeTable
        The candidate table.
    label : str
        Human name used in the error message (e.g. ``"NSE ETC options"``).

    Returns
    -------
    RegimeTable
        *regimes*, unchanged, for inline ``Final`` assignment.

    Raises
    ------
    ValueError
        If the table is empty, or its ``start`` dates are not STRICTLY
        ascending (one check covers unsorted AND duplicate-start rows).
    """
    if not regimes:
        msg = f"regime table {label!r} is empty"
        raise ValueError(msg)
    for prev, cur in pairwise(regimes):
        if cur.start <= prev.start:
            msg = (
                f"regime table {label!r}: row starts must be strictly "
                f"ascending — got {prev.start} then {cur.start}"
            )
            raise ValueError(msg)
    return regimes


def rate_for_date(
    regimes: Sequence[RegimeRow],
    on_date: date,
    *,
    charge: str = "cost regime",
    exchange: str = "",
) -> int:
    """Return the rate in force on *on_date* from a date-regime table.

    The ONE shared regime-lookup mechanism (CLAUDE.md §11 COMMON) used
    by the STT options / STT exercise / NSE ETC / BSE ETC tables below.
    O(log N) bisect over N regime rows (N ≤ 3 today — effectively O(1)
    per trade, NEVER a literal-O(1)-total claim).

    Parameters
    ----------
    regimes : Sequence[RegimeRow]
        Rows sorted strictly ascending by ``start`` (the shipped tables
        are import-time validated by :func:`validate_regime_table`). A
        table whose first row starts at ``date.min`` covers every date.
    on_date : date
        The trade's regime-selection date (per DEC-COST-002 this is
        ``entry_dt_ist.date()`` for every per-trade charge).
    charge : str
        Human name of the charge, carried into the refusal error.
    exchange : str
        Exchange label (``"NSE"`` / ``"BSE"``), carried into the refusal
        error; may be ``""`` for a non-exchange-scoped table.

    Returns
    -------
    int
        The rate (bps×100) of the latest regime with ``start <= on_date``.

    Raises
    ------
    ValueError
        If *regimes* is empty (programmer error, not an unverified
        regime), or *on_date* precedes the first row's ``start`` — no
        rate is ever fabricated for a date outside the recorded history
        (Golden Rule #1). The pre-history guard is genuinely reachable
        only for a caller-supplied table without a ``date.min`` anchor
        (all four shipped tables anchor ``date.min``, structural-pinned).
    UnverifiedCostRegimeError
        If the selected row is a refusal row (``rate_bps_x100 is None``)
        — REACHABLE and load-bearing for the ETC tables' pre-2024-10-01
        window (DEC-ETC-REGIME-HISTORY-001).
    """
    if not regimes:
        msg = "rate_for_date: empty regime table"
        raise ValueError(msg)
    if on_date < regimes[0].start:
        msg = (
            f"rate_for_date: on_date {on_date} precedes the first recorded "
            f"regime ({regimes[0].start}) — refusing to fabricate a rate"
        )
        raise ValueError(msg)
    idx = bisect_right(regimes, on_date, key=attrgetter("start")) - 1
    row = regimes[idx]
    if row.rate_bps_x100 is None:
        raise UnverifiedCostRegimeError(
            charge=charge,
            exchange=exchange,
            on_date=on_date,
            window_start=row.start,
            verified_from=(regimes[idx + 1].start if idx + 1 < len(regimes) else None),
            source=row.source,
        )
    return row.rate_bps_x100


# STT options (sell premium): the fully-VERIFIED 3-regime history.
_STT_OPTIONS_REGIMES: Final[RegimeTable] = validate_regime_table(
    (
        RegimeRow(
            date.min,
            STT_OPTIONS_PRE_OCT_2024_BPS_X100,
            "COSTS_VERIFIED §1 + DEC-COST-002 — 0.0625% verified for the repo "
            "envelope; NOTE follow-up probe: Finance Act 2023 reportedly moved "
            "options STT 0.05%→0.0625% effective 2023-04-01 — UNVERIFIED "
            "training-knowledge, deliberately NOT encoded (MIGRATION.md #1523 "
            "follow-up (iii))",
        ),
        RegimeRow(
            STT_REGIME_OCT_2024_START,
            STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100,
            "Finance Act 2024 gazette; Zerodha Z-Connect; ClearTax (COSTS_VERIFIED §1)",
        ),
        RegimeRow(
            STT_REGIME_APR_2026_START,
            STT_OPTIONS_POST_APR_2026_BPS_X100,
            "Finance Bill 2026 (DEC-COST-003)",
        ),
    ),
    label="STT options",
)

# STT on exercise: the VERIFIED 2-regime history (DEC-COST-004).
_STT_EXERCISE_REGIMES: Final[RegimeTable] = validate_regime_table(
    (
        RegimeRow(
            date.min,
            STT_EXERCISE_PRE_APR_2026_BPS_X100,
            "0.125% on intrinsic pre-2026-04-01 (DEC-COST-004; COSTS_VERIFIED §1)",
        ),
        RegimeRow(
            STT_REGIME_APR_2026_START,
            STT_EXERCISE_POST_APR_2026_BPS_X100,
            "Finance Act 2026 (DEC-COST-004)",
        ),
    ),
    label="STT exercise",
)

# ETC: exactly TWO rate-carrying rows exist repo-wide (NSE + BSE, both
# effective 2024-10-01, VERIFIED on the pre-existing COSTS_VERIFIED §2
# rows 3-4). Every pre-2024-10-01 window is a rate-less REFUSAL row —
# see the ETC regime block above (DEC-ETC-REGIME-HISTORY-001).
_NSE_ETC_OPTIONS_REGIMES: Final[RegimeTable] = validate_regime_table(
    (
        RegimeRow(
            date.min,
            None,
            "UNVERIFIED — pre-2024-10-01 NSE F&O options ETC: member "
            "monthly-premium-turnover SLAB ladders (NSE/FA/46730 ~2021-01→2023-03 "
            "· FA56129 2023-04→2024-03 · FA61137 2024-04→2024-09 — all IDENTIFIED, "
            "NONE officially retrieved; no honest flat per-trade equivalent "
            "exists). Refusal window per DEC-ETC-REGIME-HISTORY-001 "
            "(MIGRATION.md #1523); Golden Rule #1.",
        ),
        RegimeRow(
            ETC_REGIME_OCT_2024_START,
            NSE_ETC_OPTIONS_BPS_X100,
            "NSE circular FA64232 (SEBI 2024/92 true-to-label); COSTS_VERIFIED §2 "
            "row 3 — VERIFIED. NOTE: window end may be bounded by FA73061 "
            "(eff. 2026-03-01, txn/IPFT split 3,552+1/cr, total unchanged) — "
            "UNVERIFIED, NOT encoded (MIGRATION.md #1523 follow-up (i)).",
        ),
    ),
    label="NSE ETC options",
)

_BSE_ETC_OPTIONS_REGIMES: Final[RegimeTable] = validate_regime_table(
    (
        RegimeRow(
            date.min,
            None,
            "UNVERIFIED — pre-2024-10-01 BSE SENSEX/Bankex options ETC: pre-2023-11 "
            "era has NO identified document; 2023-11-01 notice 20231020-46 "
            "(nearest-expiry-scoped slab ladder) + 2024-05-13 notice 20240430-42 "
            "(slab hike, active flat-vs-slab source conflict) — NONE officially "
            "retrieved; slab + expiry-scoped applicability the flat table cannot "
            "express. Part of this window may predate the SENSEX options relaunch "
            "(no contract existed — refusal still correct). MIGRATION.md #1523.",
        ),
        RegimeRow(
            ETC_REGIME_OCT_2024_START,
            BSE_ETC_OPTIONS_BPS_X100,
            "BSE notice 27-Sep-2024 (20240927-37, per SEBI 2024/92); COSTS_VERIFIED "
            "§2 row 4 — VERIFIED (the repo row cites the notice by date; the "
            "number 20240927-37 was not independently reconfirmable — minor open, "
            "non-blocking).",
        ),
    ),
    label="BSE ETC options",
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


#: Instrument → exchange (the ONE mapping — kills the previous two
#: copy-pasted ``== "SENSEX"`` branches; CLAUDE.md §11 COMMON).
_EXCHANGE_BY_INSTRUMENT: Final[Mapping[str, str]] = {
    "NIFTY": "NSE",
    "BANKNIFTY": "NSE",
    "SENSEX": "BSE",
}

#: Exchange → its ETC options regime table.
_ETC_REGIMES_BY_EXCHANGE: Final[Mapping[str, RegimeTable]] = {
    "NSE": _NSE_ETC_OPTIONS_REGIMES,
    "BSE": _BSE_ETC_OPTIONS_REGIMES,
}


def exchange_for_instrument(instrument: str) -> str:
    """Resolve the exchange an instrument trades on.

    NIFTY 50 / BANKNIFTY trade on NSE; SENSEX trades on BSE.

    Parameters
    ----------
    instrument : str
        One of ``"NIFTY"`` / ``"BANKNIFTY"`` / ``"SENSEX"``.

    Returns
    -------
    str
        ``"NSE"`` or ``"BSE"``.

    Raises
    ------
    ValueError
        On an unknown instrument — fail-loud, naming the allowlist
        (BEHAVIOUR CHANGE per DEC-ETC-REGIME-HISTORY-001: the previous
        silent-NSE default is dead; production callers are
        ``TradeInput.__post_init__``-guarded, so only direct callers see
        this).
    """
    try:
        return _EXCHANGE_BY_INSTRUMENT[instrument]
    except KeyError:
        msg = (
            f"exchange_for_instrument: unknown instrument {instrument!r}; "
            f"allowed: {sorted(_EXCHANGE_BY_INSTRUMENT)}"
        )
        raise ValueError(msg) from None


def exchange_charge_bps_x100(instrument: str, on_date: date) -> int:
    """Pick the exchange transaction-charge rate for an instrument + date.

    The exchange resolves via :func:`exchange_for_instrument`; the rate is
    date-regime-selected through the COMMON :func:`rate_for_date`
    mechanism, mirroring the STT lookup (per DEC-COST-002 the caller
    passes ``entry_dt_ist.date()``).

    HONESTY (Golden Rule #1 — DEC-ETC-REGIME-HISTORY-001): only the
    CURRENT rates (effective 2024-10-01 — NSE FA64232 / BSE notice
    27-Sep-2024) are verified. Every pre-2024-10-01 date lands on an
    explicit refusal row and raises :class:`UnverifiedCostRegimeError` —
    the trade is never priced with a fabricated (or silently-current)
    rate; the options executor skips-and-counts it per trade. See the
    ETC regime block above for the full refusal contract.

    Raises
    ------
    ValueError
        On an unknown instrument (via :func:`exchange_for_instrument`).
    UnverifiedCostRegimeError
        For any pre-2024-10-01 *on_date* (the ETC refusal window).
    """
    exch = exchange_for_instrument(instrument)
    return rate_for_date(
        _ETC_REGIMES_BY_EXCHANGE[exch],
        on_date,
        charge="exchange transaction charge (options premium)",
        exchange=exch,
    )


def ipft_bps_x100(instrument: str) -> int:
    """Pick the IPFT rate for an instrument (NSE 0.0005%; BSE ~0).

    Raises
    ------
    ValueError
        On an unknown instrument (via :func:`exchange_for_instrument`).
    """
    exch = exchange_for_instrument(instrument)
    return BSE_IPFT_BPS_X100 if exch == "BSE" else NSE_IPFT_BPS_X100


def unverified_windows(regimes: RegimeTable) -> tuple[tuple[date, date | None], ...]:
    """Derive the ``[start, next_start-or-None)`` spans of the refusal rows.

    The refusal windows are derived from the table DATA so no doc / CLI
    footer / log line ever hardcodes a date copy that could drift
    (DEC-ETC-REGIME-HISTORY-001).

    Parameters
    ----------
    regimes : RegimeTable
        Any regime table.

    Returns
    -------
    tuple[tuple[date, date | None], ...]
        One ``(window_start, window_end_exclusive)`` pair per refusal row
        in table order; ``None`` end == an open-ended trailing window.
    """
    return tuple(
        (row.start, regimes[i + 1].start if i + 1 < len(regimes) else None)
        for i, row in enumerate(regimes)
        if row.rate_bps_x100 is None
    )


def etc_unverified_windows() -> dict[str, tuple[tuple[date, date | None], ...]]:
    """The ETC refusal windows per exchange — the public accessor the
    options sim's aggregate WARN and the CLI leaderboard footer read.

    Returns
    -------
    dict[str, tuple[tuple[date, date | None], ...]]
        ``{"NSE": ..., "BSE": ...}`` refusal spans (see
        :func:`unverified_windows`).
    """
    return {exch: unverified_windows(table) for exch, table in _ETC_REGIMES_BY_EXCHANGE.items()}


def brokerage_paisa(broker: str) -> int:
    """Pick the per-order brokerage in paise for a broker.

    Note: per CALCULATOR_SPEC §4.4 this is per executed ORDER, not
    per contract. 1 lot or 1000 lots = same ₹20.
    """
    if broker == "zerodha":
        return ZERODHA_BROKERAGE_PAISA
    return GROWW_BROKERAGE_PAISA
