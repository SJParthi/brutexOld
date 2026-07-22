"""Futures cost stack — VERIFIED-rates-only, refusal-capable (DEC-FUTURES-EXEC-TRANSLATION-001).

The futures twin of the options cost calculator (:mod:`brutex.costs.calculator`),
built for the Phase-5 futures translation executor (R14/R15 of the one-click sweep
law). It prices ONE round-trip futures trade — each fill anchored on the fill
minute-bar's ADVERSE EXTREME ± one adverse tick, the ONLY fill law
(DEC-FILL-WORST-CASE-ONLY-001; see :func:`apply_futures_fill` — the retired
R15 OPEN-anchored PRECISE law is deleted with its mode) —
then the statutory + broker charge stack — under the HARD honesty rule
locked by DEC-ETC-REGIME-HISTORY-001 (MIGRATION.md #1524):

    **A rate that is not citation-grounded in the repo's verified-costs docs is
    NEVER encoded as a number.** It ships as an explicit rate-less
    :class:`~brutex.costs.constants.RegimeRow` REFUSAL window; a date landing on
    it raises :class:`~brutex.costs.types.UnverifiedCostRegimeError` and the
    executor skips-and-counts the trade (never a fabricated charge, never a
    crash — Golden Rule #1).

## Verification ledger (every encoded number traces to a repo row)

VERIFIED (encoded as rates):

* **Groww futures brokerage** ``min(₹20, 0.03% × turnover)`` per executed order —
  ``docs/track2/PLAN_FINALIZATION_2026-05-16.md`` §8.1 row 2 ("✅ VERIFIED — same
  MIS + NRML"); the 0.03% figure also documented as
  ``GROWW_BROKERAGE_FUTURES_BPS_X100 = 3000`` in ``COSTS_VERIFIED.md`` §14.
* **Futures STT 0.02%** (sell-side turnover) effective 2024-10-01 —
  ``docs/track2/TRACK2_TAX_HANDLING.md`` §8.1 rate table ("0.02% (VERIFIED)").
* **Futures STT 0.05%** effective 2026-04-01 — DEC-COST-003 LOCKED
  (``COSTS_VERIFIED.md`` §9 row 2: "futures 0.02%→0.05%", verified via TaxGuru +
  Wikipedia + 1Finance citing the Finance Bill 2026 PDF). The §8.1 table's older
  "UNVERIFIED" marker predates — and is superseded by — that lock.
* **SEBI turnover fee 0.0001%** both sides — ``COSTS_VERIFIED.md`` §2 row 5 +
  §14 ("all segments"); reuses the COMMON
  :data:`~brutex.costs.constants.SEBI_FEE_BPS_X100`.
* **GST 18%** on (brokerage + ETC + SEBI + IPFT), rounded ONCE as the total —
  ``COSTS_VERIFIED.md`` §2 row 8 / DEC-COST-001; reuses the COMMON
  :data:`~brutex.costs.constants.GST_TOTAL_BPS_X100`.

UNVERIFIED for FUTURES (encoded as REFUSAL rows — figures live in ``source``
TEXT only, never in ``rate_bps_x100``):

* **Futures STT pre-2024-10-01** — the 0.0125% figure appears in
  ``TRACK2_TAX_HANDLING.md`` §8.1 WITHOUT a VERIFIED stamp (only the 2024-10-01+
  rows carry one), and the deprecated ``brutex/execution/costs.py`` is a frozen
  stale model, not a source.
* **NSE / BSE futures exchange transaction charge (ETC), EVERY window** — the
  0.00173% NSE figure exists only in the ``COSTS_VERIFIED.md`` §14
  rate-constants *proposal* block; the DEC-ETC-REGIME-HISTORY-001 slab-era
  verification round was OPTIONS-only (§2 rows 3-4 are options rates), so NO
  futures ETC window is repo-verified. Every window refuses.
* **NSE futures IPFT** — the ₹10/cr figure exists only in the §14 proposal
  block; §2 row 6 verifies the OPTIONS 0.0005% only. (BSE IPFT reuses the
  options stack's assumed-0 :data:`~brutex.costs.constants.BSE_IPFT_BPS_X100`,
  the same ``COSTS_VERIFIED.md`` §9 row 3 stance — no new number invented.)
* **Futures stamp duty** — the 0.002% figure exists only in the §14 proposal
  block; §2 row 7 verifies the OPTIONS 0.003% buy-side rate only.

## Cost scope (DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001, MIGRATION.md #1060)

INDEX futures (NIFTY / BANKNIFTY / SENSEX) are **COST-FREE** — the dynamic-track
signal-only decree — so the LIVE executor path returns an all-zero charge stack
and ``net == gross`` (the ± one-tick :func:`apply_futures_fill` haircut is R15
execution-FILL realism, not a statutory cost). Everything else (single-stock
futures) is COST-BEARING and, today, lands on the ETC refusal window for every
date — the executor counts it as ``COST_REGIME_UNVERIFIED``. The charge
arithmetic below is fully unit-tested via SYNTHETIC circular-cited RegimeRow
fixtures (monkeypatched tables), never a shipped unverified production rate.

## No runtime escape hatch (locked, DEC-ETC-REGIME-HISTORY-001 §9)

No flag/kwarg on any function here can bypass a refusal window — a fabricated
statutory rate would fork every downstream number OUTSIDE blake3 run identity
(Golden Rule #4). The only sanctioned override paths are DATA: a circular-cited
``RegimeRow``, or an ADR-gated ``source="ASSUMED per ADR-00XX"`` row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Literal, get_args

from brutex.costs.calculator import _round_levy_paisa, _round_levy_rupee
from brutex.costs.constants import (
    BSE_IPFT_BPS_X100,
    GROWW_BROKERAGE_PAISA,
    GST_TOTAL_BPS_X100,
    SEBI_FEE_BPS_X100,
    STT_REGIME_APR_2026_START,
    STT_REGIME_OCT_2024_START,
    TICK_PAISA,
    ZERODHA_BROKERAGE_PAISA,
    RegimeRow,
    RegimeTable,
    exchange_for_instrument,
    rate_for_date,
    unverified_windows,
    validate_regime_table,
)
from brutex.costs.types import Broker, ChargeStack, GstSplit, require_bar_extremes
from brutex.universe import BRUTE_FORCE_INDEX_UNDERLYINGS

__all__ = [
    "GROWW_FUTURES_BROKERAGE_TURNOVER_BPS_X100",
    "INDEX_FUTURES_UNDERLYINGS",
    "STT_FUTURES_OCT_2024_BPS_X100",
    "STT_FUTURES_POST_APR_2026_BPS_X100",
    "FuturesCostBreakdown",
    "FuturesSide",
    "FuturesTradeInput",
    "apply_futures_fill",
    "compute_futures_trade_cost",
    "futures_brokerage_leg_paisa",
    "futures_etc_unverified_windows",
    "futures_exchange_charge_bps_x100",
    "futures_exchange_for_underlying",
    "ipft_futures_bps_x100",
    "is_cost_free_future_underlying",
    "stamp_futures_rate_bps_x100",
    "stt_futures_rate_bps_x100",
]


# ─────────────────────────────────────────────────────────────────
# Cost scope + venue resolution
# ─────────────────────────────────────────────────────────────────

#: The underlying symbols whose FUTURES are COST-FREE (signal-only) — derived
#: from the ONE engine-scope source :data:`brutex.universe.BRUTE_FORCE_INDEX_UNDERLYINGS`
#: (never a hand-typed copy; CLAUDE.md §11 COMMON). Today: NIFTY / BANKNIFTY / SENSEX.
INDEX_FUTURES_UNDERLYINGS: Final[frozenset[str]] = frozenset(
    underlying for _exchange, underlying in BRUTE_FORCE_INDEX_UNDERLYINGS
)


def is_cost_free_future_underlying(underlying: str) -> bool:
    """Return ``True`` iff a FUTURE on *underlying* is priced signal-only (zero charges).

    The label-level projection of the operator-locked cost-scope rule
    (DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001 — :mod:`brutex.costs.scope` states it over
    the canonical ``Instrument`` object; the futures executor works on underlying
    LABELS, so this predicate keys the SAME index set,
    :data:`INDEX_FUTURES_UNDERLYINGS`). O(1) frozenset membership.

    Parameters
    ----------
    underlying
        The underlying symbol label (``"NIFTY"`` / ``"RELIANCE"``).

    Returns
    -------
    bool
        ``True`` for an index future (cost-free), ``False`` for everything else
        (cost-bearing single-stock futures).
    """
    return underlying in INDEX_FUTURES_UNDERLYINGS


def futures_exchange_for_underlying(underlying: str) -> str:
    """Resolve the exchange an *underlying*'s futures trade on (``"NSE"`` / ``"BSE"``).

    The three brute-force indices resolve through the COMMON
    :func:`~brutex.costs.constants.exchange_for_instrument` map (NIFTY / BANKNIFTY →
    NSE, SENSEX → BSE). Any OTHER underlying is a single-stock future and resolves
    to ``"NSE"`` — the single-stock F&O venue of the lake's expanded scope
    ("200 **NSE** F&O eligible single stocks", CLAUDE.md §2 Rule 3 / DEC-P0-063).
    Today this only scopes which ETC/IPFT REFUSAL window names the failure — no
    stock-futures rate exists to select (see the module verification ledger).

    Parameters
    ----------
    underlying
        The underlying symbol label.

    Returns
    -------
    str
        ``"NSE"`` or ``"BSE"``.
    """
    try:
        return exchange_for_instrument(underlying)
    except ValueError:
        return "NSE"


# ─────────────────────────────────────────────────────────────────
# Verified rate constants (futures)
# ─────────────────────────────────────────────────────────────────

GROWW_FUTURES_BROKERAGE_TURNOVER_BPS_X100: Final[int] = 3_000
"""Groww futures brokerage turnover arm: 0.03% of leg turnover, capped at the
flat ₹20/order (:data:`~brutex.costs.constants.GROWW_BROKERAGE_PAISA`) —
``min(₹20, 0.03% × turnover)`` per executed order. VERIFIED:
PLAN_FINALIZATION_2026-05-16.md §8.1 row 2 ("same MIS + NRML");
COSTS_VERIFIED.md §14 (``GROWW_BROKERAGE_FUTURES_BPS_X100 = 3000``)."""

STT_FUTURES_OCT_2024_BPS_X100: Final[int] = 2_000
"""Futures STT 0.02% on sell-side turnover, effective 2024-10-01. VERIFIED:
TRACK2_TAX_HANDLING.md §8.1 rate table ("0.02% (VERIFIED)")."""

STT_FUTURES_POST_APR_2026_BPS_X100: Final[int] = 5_000
"""Futures STT 0.05% on sell-side turnover, from 2026-04-01. VERIFIED/LOCKED:
DEC-COST-003 (COSTS_VERIFIED.md §9 row 2 — "futures 0.02%→0.05%", Finance Bill
2026 PDF via TaxGuru / Wikipedia / 1Finance)."""


# ─────────────────────────────────────────────────────────────────
# Regime tables (futures) — refusal rows carry the honest citation gap
# ─────────────────────────────────────────────────────────────────

# STT futures: pre-2024-10-01 REFUSAL + two VERIFIED rows. Boundary dates reuse
# the COMMON options-table constants (same Finance Act boundaries).
_STT_FUTURES_REGIMES: Final[RegimeTable] = validate_regime_table(
    (
        RegimeRow(
            date.min,
            None,
            "UNVERIFIED — pre-2024-10-01 futures STT: the 0.0125% figure appears "
            "in TRACK2_TAX_HANDLING.md §8.1 WITHOUT a VERIFIED stamp (only the "
            "2024-10-01+ rows carry one); the deprecated execution/costs.py "
            "0.0125% is a frozen stale model, not a source. Refusal window per "
            "the DEC-ETC-REGIME-HISTORY-001 precedent; Golden Rule #1.",
        ),
        RegimeRow(
            STT_REGIME_OCT_2024_START,
            STT_FUTURES_OCT_2024_BPS_X100,
            "TRACK2_TAX_HANDLING.md §8.1 — futures sell-side STT 0.02% "
            "2024-10-01 → 2026-03-31, marked VERIFIED.",
        ),
        RegimeRow(
            STT_REGIME_APR_2026_START,
            STT_FUTURES_POST_APR_2026_BPS_X100,
            "DEC-COST-003 LOCKED (COSTS_VERIFIED.md §9 row 2): futures STT "
            "0.02%→0.05% effective 2026-04-01, Finance Bill 2026.",
        ),
    ),
    label="STT futures",
)

# ETC futures: EVERY window is a refusal row — no futures ETC rate is
# repo-verified through the DEC-ETC-REGIME-HISTORY-001 slab-era discipline
# (that round was OPTIONS-only). The documented-but-unverified figures live in
# the source TEXT only.
_NSE_ETC_FUTURES_REGIMES: Final[RegimeTable] = validate_regime_table(
    (
        RegimeRow(
            date.min,
            None,
            "UNVERIFIED — NSE futures exchange transaction charge, ALL windows: "
            "the 0.00173% figure exists only in the COSTS_VERIFIED.md §14 "
            "rate-constants PROPOSAL block; the DEC-ETC-REGIME-HISTORY-001 "
            "verification round (MIGRATION.md #1524) covered OPTIONS only "
            "(§2 rows 3-4), and the pre-2024-10-01 era is the same "
            "member-monthly-turnover slab-ladder problem. Refusing every "
            "window until a futures circular body lands; Golden Rule #1.",
        ),
    ),
    label="NSE ETC futures",
)

_BSE_ETC_FUTURES_REGIMES: Final[RegimeTable] = validate_regime_table(
    (
        RegimeRow(
            date.min,
            None,
            "UNVERIFIED — BSE (SENSEX) futures exchange transaction charge, ALL "
            "windows: no BSE futures ETC row exists in COSTS_VERIFIED.md (§2 "
            "row 4 is the OPTIONS 0.03250%); the DEC-ETC-REGIME-HISTORY-001 "
            "round covered options only. Refusing every window; Golden Rule #1.",
        ),
    ),
    label="BSE ETC futures",
)

#: Exchange → its futures-ETC regime table (the futures twin of the options
#: ``_ETC_REGIMES_BY_EXCHANGE`` map).
_ETC_FUTURES_REGIMES_BY_EXCHANGE: Final[dict[str, RegimeTable]] = {
    "NSE": _NSE_ETC_FUTURES_REGIMES,
    "BSE": _BSE_ETC_FUTURES_REGIMES,
}

# NSE futures IPFT: refusal — the ₹10/cr figure is proposal-block-only.
_NSE_IPFT_FUTURES_REGIMES: Final[RegimeTable] = validate_regime_table(
    (
        RegimeRow(
            date.min,
            None,
            "UNVERIFIED — NSE futures IPFT: the 0.0001% (₹10/cr) figure exists "
            "only in the COSTS_VERIFIED.md §14 PROPOSAL block; §2 row 6 "
            "verifies the OPTIONS 0.0005% only. Refusing every window; "
            "Golden Rule #1.",
        ),
    ),
    label="NSE IPFT futures",
)

# Stamp duty futures: refusal — the 0.002% figure is proposal-block-only
# (§2 row 7 / TRACK2_TAX_HANDLING §8.2 verify the OPTIONS 0.003% only).
_STAMP_FUTURES_REGIMES: Final[RegimeTable] = validate_regime_table(
    (
        RegimeRow(
            date.min,
            None,
            "UNVERIFIED — futures stamp duty: the 0.002% buy-side figure exists "
            "only in the COSTS_VERIFIED.md §14 PROPOSAL block; §2 row 7 + "
            "TRACK2_TAX_HANDLING.md §8.2 verify the OPTIONS 0.003% only. "
            "Refusing every window; Golden Rule #1.",
        ),
    ),
    label="stamp duty futures",
)


# ─────────────────────────────────────────────────────────────────
# Rate accessors (all delegate to the COMMON rate_for_date mechanism)
# ─────────────────────────────────────────────────────────────────


def stt_futures_rate_bps_x100(entry_date: date) -> int:
    """Pick the futures STT rate (sell-side turnover) for an entry date.

    Regime selected by the trade's entry date (the DEC-COST-002 convention),
    through the COMMON :func:`~brutex.costs.constants.rate_for_date` mechanism.

    Parameters
    ----------
    entry_date
        The trade's entry date in IST.

    Returns
    -------
    int
        The STT rate in bps×100 (``2_000`` for 0.02%, ``5_000`` for 0.05%).

    Raises
    ------
    UnverifiedCostRegimeError
        For any pre-2024-10-01 date — the futures-STT refusal window (see the
        module verification ledger).
    """
    return rate_for_date(_STT_FUTURES_REGIMES, entry_date, charge="STT (futures turnover)")


def futures_exchange_charge_bps_x100(instrument: str, on_date: date) -> int:
    """Pick the futures exchange-transaction-charge rate for an instrument + date.

    The exchange resolves via :func:`futures_exchange_for_underlying`; the rate is
    date-regime-selected through the COMMON
    :func:`~brutex.costs.constants.rate_for_date`.

    HONESTY (Golden Rule #1): NO futures ETC window is repo-verified today, so
    EVERY date raises :class:`~brutex.costs.types.UnverifiedCostRegimeError` —
    the futures executor prechecks this and skips-and-counts the trade
    (``COST_REGIME_UNVERIFIED``). A verified futures circular lands as ONE cited
    ``RegimeRow`` + a MIGRATION.md entry — zero mechanism change.

    Parameters
    ----------
    instrument
        The underlying symbol label.
    on_date
        The trade's entry date in IST.

    Returns
    -------
    int
        The rate in bps×100 — unreachable today (every window refuses); the
        return path goes live the day a cited futures RegimeRow lands.

    Raises
    ------
    UnverifiedCostRegimeError
        For EVERY *on_date* today (all-refusal tables).
    """
    exch = futures_exchange_for_underlying(instrument)
    return rate_for_date(
        _ETC_FUTURES_REGIMES_BY_EXCHANGE[exch],
        on_date,
        charge="exchange transaction charge (futures turnover)",
        exchange=exch,
    )


def ipft_futures_bps_x100(instrument: str, on_date: date) -> int:
    """Pick the futures IPFT rate for an instrument + date.

    BSE reuses the options stack's assumed-0
    :data:`~brutex.costs.constants.BSE_IPFT_BPS_X100` (COSTS_VERIFIED.md §9 row 3
    stance — treated as 0, flagged UNVERIFIED there; no new number invented).
    NSE lands on the refusal table — the futures ₹10/cr figure is
    proposal-block-only (see the module verification ledger).

    Parameters
    ----------
    instrument
        The underlying symbol label.
    on_date
        The trade's entry date in IST.

    Returns
    -------
    int
        ``0`` for BSE; the NSE return path goes live only when a cited futures
        IPFT RegimeRow lands.

    Raises
    ------
    UnverifiedCostRegimeError
        For every NSE *on_date* today (all-refusal table).
    """
    exch = futures_exchange_for_underlying(instrument)
    if exch == "BSE":
        return BSE_IPFT_BPS_X100
    return rate_for_date(
        _NSE_IPFT_FUTURES_REGIMES,
        on_date,
        charge="IPFT (futures turnover)",
        exchange=exch,
    )


def stamp_futures_rate_bps_x100(entry_date: date) -> int:
    """Pick the futures stamp-duty rate (buy-side turnover) for an entry date.

    Parameters
    ----------
    entry_date
        The trade's entry date in IST.

    Returns
    -------
    int
        The rate in bps×100 — unreachable today (every window refuses); goes
        live only when a cited futures stamp RegimeRow lands.

    Raises
    ------
    UnverifiedCostRegimeError
        For EVERY *entry_date* today (all-refusal table — the 0.002% figure is
        proposal-block-only, see the module verification ledger).
    """
    return rate_for_date(_STAMP_FUTURES_REGIMES, entry_date, charge="stamp duty (futures buy)")


def futures_etc_unverified_windows() -> dict[str, tuple[tuple[date, date | None], ...]]:
    """The futures-ETC refusal windows per exchange — DERIVED from table DATA.

    The public accessor the futures sim's aggregate WARN and the leaderboard
    honesty footer read (the futures twin of
    :func:`~brutex.costs.constants.etc_unverified_windows`); spans are derived
    via the COMMON :func:`~brutex.costs.constants.unverified_windows` so no
    consumer ever hardcodes a date copy that could drift.

    Returns
    -------
    dict[str, tuple[tuple[datetime.date, datetime.date | None], ...]]
        ``{"NSE": ..., "BSE": ...}`` refusal spans; a ``None`` end is an
        open-ended trailing window (today: BOTH exchanges are one open-ended
        history-start window).
    """
    return {
        exch: unverified_windows(table) for exch, table in _ETC_FUTURES_REGIMES_BY_EXCHANGE.items()
    }


# ─────────────────────────────────────────────────────────────────
# Fill application — PRECISE (R15, open ± one tick) / WORST_CASE
# (DEC-FILL-WORST-CASE-ONLY-001, adverse bar extreme ± the SAME tick)
# ─────────────────────────────────────────────────────────────────

FuturesSide = Literal["long", "short"]
"""Which leg opens the position: ``long`` (BULL — buy first) or ``short``
(BEAR — sell first). The futures executor supports BOTH directions (unlike the
BUY-only options track): a futures short is a plain sell-first round trip."""

_FUTURES_SIDE_VALUES: Final[frozenset[str]] = frozenset(get_args(FuturesSide))
_BROKER_VALUES: Final[frozenset[str]] = frozenset(get_args(Broker))
_GST_SPLIT_VALUES: Final[frozenset[str]] = frozenset(get_args(GstSplit))


def apply_futures_fill(
    *,
    side: FuturesSide,
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
) -> tuple[int, int, int]:
    """Apply the one-tick ADVERSE fill around the worst-case bar extremes.

    The ONE fill law (DEC-FILL-WORST-CASE-ONLY-001; the retired R15
    OPEN-anchored PRECISE law is deleted with its mode): each fill anchors
    on the fill minute-bar's ADVERSE EXTREME and then applies ONE adverse
    tick on top (:data:`~brutex.costs.constants.TICK_PAISA` = ₹0.05; every
    fill floored at one tick — nothing trades below ₹0.05). Keeping the
    tick ON TOP of the extreme preserves the tick discipline: on a flat bar
    (``high == open``) the bare ``high`` would beat the retired
    ``open + tick`` fill by one tick; with the tick the flat bar is EQUAL,
    so the worst-case fill can never flatter (the proven counterexample;
    the bar model guarantees ``low ≤ open ≤ high``).

    * ``long``: buy = entry HIGH **+** tick; sell = exit LOW **−** tick —
      byte-equal to the options calculator's ``_apply_slippage("long", …)``
      (golden-pinned, CLAUDE.md §11 COMMON).
    * ``short``: sell = entry LOW **−** tick (opening sell fills
      adverse-LOWER); buy = exit HIGH **+** tick (the cover fills
      adverse-HIGHER) — computed adverse-both-sides DIRECTLY (the resolved
      ``# CHECK SIGN`` law, calculator.py MINOR-4).

    Parameters
    ----------
    side
        ``"long"`` or ``"short"``.
    entry_high_paisa, entry_low_paisa, exit_high_paisa, exit_low_paisa
        The fill bars' extremes in paisa — the adverse anchors (already
        snapped to the 2dp tick grid at the lake-write boundary,
        DEC-SPOT-PRICE-2DP-001, so the ± tick here is applied exactly once).
        All four REQUIRED — worst-case is the only law, so an anchor-less
        call is unpriceable.

    Returns
    -------
    tuple[int, int, int]
        ``(buy_fill_paisa, sell_fill_paisa, realized_slippage_per_unit_paisa)``.
        The realized slippage is the adverse TICK movement truly baked into
        the floored fills, measured against the worst-case fill anchors
        (``2 × tick`` normally; ``1 × tick`` when the one-tick floor caps a
        degenerate sub-2-tick leg) — the open→extreme move is fill-ANCHOR
        semantics carried by the gross P&L, not slippage, so the line is
        never over-stated.

    Raises
    ------
    ValueError
        On an unknown *side* (fail-loud; never silently priced as long).
    """
    if side not in _FUTURES_SIDE_VALUES:
        msg = f"apply_futures_fill: side {side!r} is not one of {sorted(_FUTURES_SIDE_VALUES)}"
        raise ValueError(msg)
    tick = TICK_PAISA
    if side == "long":
        buy_raw, sell_raw = entry_high_paisa, exit_low_paisa
    else:
        # Short: the OPENING sell fills adverse-LOWER off the entry LOW, the
        # closing cover adverse-HIGHER off the exit HIGH — both legs adverse.
        buy_raw, sell_raw = exit_high_paisa, entry_low_paisa
    buy_fill, sell_fill = buy_raw + tick, sell_raw - tick
    buy_fill, sell_fill = max(buy_fill, tick), max(sell_fill, tick)
    realized_per_unit = abs(buy_fill - buy_raw) + abs(sell_raw - sell_fill)
    return buy_fill, sell_fill, realized_per_unit


def futures_brokerage_leg_paisa(notional_paisa: int, broker: Broker) -> int:
    """Brokerage for ONE executed futures order (one leg) in paisa.

    * ``groww``: ``min(₹20, 0.03% × leg turnover)`` — the VERIFIED Groww futures
      law (PLAN_FINALIZATION_2026-05-16.md §8.1 row 2); the turnover arm rounds
      ADVERSELY to the next paisa (ceiling — the permanent adverse-levy-
      rounding law, DEC-FILL-WORST-CASE-ONLY-001: the turnover arm is a
      rounded charge, so it never rounds in the trade's favor). The flat ₹20
      arm is rounding-free.
    * ``zerodha``: flat ₹20 per executed order — the in-repo COSTS_VERIFIED.md
      §8.2 row ("flat for F&O since June 2024"); the min-formula verification is
      Groww-specific, so Zerodha deliberately keeps its own verified flat row.

    Parameters
    ----------
    notional_paisa
        The leg's turnover (fill × shares) in paisa.
    broker
        ``"groww"`` or ``"zerodha"``.

    Returns
    -------
    int
        The leg brokerage in paisa.
    """
    if broker == "zerodha":
        return _brokerage_leg_paisa(notional_paisa, ZERODHA_BROKERAGE_PAISA, None)
    return _brokerage_leg_paisa(
        notional_paisa, GROWW_BROKERAGE_PAISA, GROWW_FUTURES_BROKERAGE_TURNOVER_BPS_X100
    )


def _brokerage_leg_paisa(
    notional_paisa: int, flat_paisa: int, turnover_bps_x100: int | None
) -> int:
    """One-leg brokerage from caller-RESOLVED broker parameters (int-only core).

    The ONE copy of the ``min(flat, adverse-ceil(notional × turnover))`` law
    (§11 COMMON; the turnover arm rounds ADVERSELY per the permanent
    adverse-levy rule, DEC-FILL-WORST-CASE-ONLY-001) in the integer parameter
    shape the charge stack — and its Rust twin (DEC-EXEC-RUST-CORE-001,
    MIGRATION.md #2215) — consumes: ``turnover_bps_x100 = None`` means a
    flat-only broker (Zerodha); a rate means the Groww min() arm.
    :func:`futures_brokerage_leg_paisa` stays the public broker-keyed API and
    delegates here byte-identically.
    """
    if turnover_bps_x100 is None:
        return flat_paisa
    return min(flat_paisa, _round_levy_paisa(notional_paisa * turnover_bps_x100))


# ─────────────────────────────────────────────────────────────────
# Trade input / breakdown
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FuturesTradeInput:
    """One round-trip futures trade specification (the futures ``TradeInput`` twin).

    All prices int64 paisa; all times IST. The lot size is supplied by the
    CALLER (the executor's first reject gate already resolved it via
    :func:`brutex.futures.executor.futures_lot_size`), so the calculator never
    re-looks it up.

    Parameters
    ----------
    entry_dt_ist, exit_dt_ist
        Entry / exit timestamps in IST. Intraday-only (R18): both must fall on
        the SAME IST date — a futures trade never holds overnight, so a trade
        can never straddle its contract's expiry (ROLL pin).
    instrument
        The underlying symbol label (``"NIFTY"``); selects the cost scope
        (:func:`is_cost_free_future_underlying`) + the exchange.
    contract_symbol
        The resolved front-month contract ``groww_symbol`` (audit identity).
    expiry_date
        The contract's expiry; must be ``>= entry_dt_ist.date()`` (a trade on a
        dead contract is unrepresentable).
    entry_bar_open_paisa, exit_bar_open_paisa
        The entry / exit minute-bar OPEN prices in paisa (``>=`` one tick).
    side
        ``"long"`` or ``"short"`` (:data:`FuturesSide`).
    lot_size
        Shares per lot (``>= 1``) — resolved by the caller from the date-keyed
        derivative-lot history.
    n_lots
        Number of lots (``>= 1``).
    broker, gst_split
        Cost-model parameters (defaults: Groww, intra-state GST).
    entry_bar_high_paisa, entry_bar_low_paisa, exit_bar_high_paisa, exit_bar_low_paisa
        The fill bars' extremes in paisa — the worst-case fill anchors,
        REQUIRED all four (DEC-FILL-WORST-CASE-ONLY-001: worst-case is the
        ONLY fill law, so an anchor-less input is unpriceable and refuses
        fail-loud; the ``| None`` typing survives only so the refusal is a
        validated ``ValueError``). Each pair must bracket its bar's open
        (``low <= open <= high`` — the bar-model invariant restated at this
        boundary).

    Raises
    ------
    ValueError
        On any violated invariant (fail-loud at the boundary).
    """

    entry_dt_ist: datetime
    exit_dt_ist: datetime
    instrument: str
    contract_symbol: str
    expiry_date: date
    entry_bar_open_paisa: int
    exit_bar_open_paisa: int
    side: FuturesSide
    lot_size: int
    n_lots: int = 1
    broker: Broker = "groww"
    gst_split: GstSplit = "intra_state"
    entry_bar_high_paisa: int | None = None
    entry_bar_low_paisa: int | None = None
    exit_bar_high_paisa: int | None = None
    exit_bar_low_paisa: int | None = None

    def __post_init__(self) -> None:
        if self.exit_dt_ist < self.entry_dt_ist:
            msg = f"exit_dt_ist {self.exit_dt_ist} must be >= entry_dt_ist {self.entry_dt_ist}"
            raise ValueError(msg)
        if self.exit_dt_ist.date() != self.entry_dt_ist.date():
            msg = (
                f"futures trades are intraday-only (R18): entry {self.entry_dt_ist.date()} "
                f"and exit {self.exit_dt_ist.date()} must be the same IST date"
            )
            raise ValueError(msg)
        if not self.instrument.strip():
            msg = "instrument must be a non-blank underlying label"
            raise ValueError(msg)
        if not self.contract_symbol.strip():
            msg = "contract_symbol must be a non-blank groww_symbol"
            raise ValueError(msg)
        if self.expiry_date < self.entry_dt_ist.date():
            msg = (
                f"expiry_date {self.expiry_date} precedes the entry date "
                f"{self.entry_dt_ist.date()} — a trade on a dead contract is unrepresentable"
            )
            raise ValueError(msg)
        if self.entry_bar_open_paisa < TICK_PAISA:
            msg = f"entry_bar_open_paisa {self.entry_bar_open_paisa} below 1 tick ({TICK_PAISA})"
            raise ValueError(msg)
        if self.exit_bar_open_paisa < TICK_PAISA:
            msg = f"exit_bar_open_paisa {self.exit_bar_open_paisa} below 1 tick ({TICK_PAISA})"
            raise ValueError(msg)
        if self.lot_size < 1:
            msg = f"lot_size {self.lot_size} must be >= 1"
            raise ValueError(msg)
        if self.n_lots < 1:
            msg = f"n_lots {self.n_lots} must be >= 1"
            raise ValueError(msg)
        for field_name, value, allowed in (
            ("side", self.side, _FUTURES_SIDE_VALUES),
            ("broker", self.broker, _BROKER_VALUES),
            ("gst_split", self.gst_split, _GST_SPLIT_VALUES),
        ):
            if value not in allowed:
                msg = f"{field_name} {value!r} is not a valid value; allowed: {sorted(allowed)}"
                raise ValueError(msg)
        # DEC-FILL-WORST-CASE-ONLY-001: worst-case is the ONLY fill law, so
        # the four adverse-anchor extremes are REQUIRED unconditionally — an
        # input without them cannot be priced and refuses fail-loud (never a
        # silent open-anchored fill; the retired PRECISE law is deleted).
        missing = [
            name
            for name in (
                "entry_bar_high_paisa",
                "entry_bar_low_paisa",
                "exit_bar_high_paisa",
                "exit_bar_low_paisa",
            )
            if getattr(self, name) is None
        ]
        if missing:
            msg = (
                f"the worst-case fill law needs the fill-bar extremes; missing {missing} "
                f"(worst-case is the ONLY fill law — DEC-FILL-WORST-CASE-ONLY-001)"
            )
            raise ValueError(msg)
        for label, open_p, high, low in (
            (
                "entry",
                self.entry_bar_open_paisa,
                self.entry_bar_high_paisa,
                self.entry_bar_low_paisa,
            ),
            ("exit", self.exit_bar_open_paisa, self.exit_bar_high_paisa, self.exit_bar_low_paisa),
        ):
            # Ordering only (the bar-model `low <= open <= high` invariant);
            # positivity/tick floors on the EXTREMES are deliberately NOT
            # demanded — a degenerate sub-tick LOW is absorbed by the fill
            # floor in apply_futures_fill, never a sim crash.
            if high is not None and high < open_p:
                msg = f"{label}_bar_high_paisa {high} < {label}_bar_open_paisa {open_p}"
                raise ValueError(msg)
            if low is not None and low > open_p:
                msg = f"{label}_bar_low_paisa {low} > {label}_bar_open_paisa {open_p}"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class FuturesCostBreakdown:
    """Per-component futures charge breakdown + roll-ups (all int64 paisa).

    ``cost_free=True`` (an INDEX future) carries an all-zero charge stack with
    ``net == gross`` — the signal-only decree
    (DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001); the ± one-tick fill haircut is
    already baked into ``buy_fill_paisa`` / ``sell_fill_paisa`` (R15 fill
    realism, not a statutory cost).

    Raises
    ------
    ValueError
        If ``net_pnl_paisa != gross_pnl_paisa - total_charges_paisa`` or a
        count/size field is non-positive (internal-consistency fail-loud).
    """

    n_shares_per_lot: int
    n_shares: int
    buy_fill_paisa: int
    sell_fill_paisa: int
    buy_notional_paisa: int
    sell_notional_paisa: int
    brokerage_paisa: int
    stt_paisa: int
    exch_paisa: int
    sebi_paisa: int
    ipft_paisa: int
    stamp_paisa: int
    gst_paisa: int
    slippage_paisa: int
    total_charges_paisa: int
    gross_pnl_paisa: int
    net_pnl_paisa: int
    cost_free: bool
    lot_size_basis: Literal["trade_date"] = "trade_date"

    def __post_init__(self) -> None:
        if self.n_shares_per_lot < 1 or self.n_shares < 1:
            msg = (
                f"FuturesCostBreakdown: n_shares_per_lot {self.n_shares_per_lot} and "
                f"n_shares {self.n_shares} must be >= 1"
            )
            raise ValueError(msg)
        if self.net_pnl_paisa != self.gross_pnl_paisa - self.total_charges_paisa:
            msg = (
                f"FuturesCostBreakdown: net_pnl_paisa {self.net_pnl_paisa} != gross "
                f"{self.gross_pnl_paisa} - charges {self.total_charges_paisa}"
            )
            raise ValueError(msg)


def _futures_charge_stack_py(
    entry_high_paisa: int,
    entry_low_paisa: int,
    exit_high_paisa: int,
    exit_low_paisa: int,
    *,
    is_long: bool,
    lot_size: int,
    n_lots: int,
    cost_free: bool,
    brokerage_flat_paisa: int,
    brokerage_turnover_bps_x100: int | None,
    stt_rate: int,
    etc_rate: int,
    sebi_rate: int,
    ipft_rate: int,
    stamp_rate: int,
    gst_rate: int,
) -> ChargeStack:
    """The futures charge stack as ONE pure integer function.

    The dispatchable futures core (DEC-EXEC-RUST-CORE-001, MIGRATION.md
    #1979), the futures twin of
    :func:`brutex.costs.calculator._charge_stack_py`: the worst-case
    bar-extreme fills (DEC-FILL-WORST-CASE-ONLY-001) → notionals → gross,
    then the ``cost_free`` short-circuit (an INDEX future returns the
    all-zero stack with ``net == gross`` WITHOUT touching any rate argument —
    the caller passes zeros on that arm), else the cost-bearing stack under
    the SAME permanent ADVERSE (ceiling) levy rounding. Every rate / refusal
    decision is resolved by the CALLER (Python stays the single authoritative
    rate source; the refusal order STT → ETC → NSE-IPFT → stamp raises there,
    never here). The body is the byte-identical extraction of the inline
    arithmetic of :func:`compute_futures_trade_cost` under main's #1825 law
    (re-derived at the club-R1 merge-reconcile — main wins structurally) and
    stays the permanent oracle for the Rust twin
    (``fno_math.exec_futures_charge_stack``).

    Divergences from the options stack, preserved verbatim: gross =
    ``sell_notional − buy_notional`` for BOTH directions (the fill law
    pre-assigns the legs — no sign branch); brokerage =
    :func:`_brokerage_leg_paisa` PER LEG (Groww ``min(flat, turnover)`` /
    Zerodha flat) instead of the options flat round-trip.
    """
    buy_fill_paisa, sell_fill_paisa, realized_slip_per_unit = apply_futures_fill(
        side="long" if is_long else "short",
        entry_high_paisa=entry_high_paisa,
        entry_low_paisa=entry_low_paisa,
        exit_high_paisa=exit_high_paisa,
        exit_low_paisa=exit_low_paisa,
    )
    n_shares = n_lots * lot_size
    buy_notional_paisa = buy_fill_paisa * n_shares
    sell_notional_paisa = sell_fill_paisa * n_shares
    # Both directions reduce to (sell − buy) over the ADVERSE fills: a long
    # buys the entry leg and sells the exit leg; a short sells the entry leg
    # and buys (covers) the exit leg — apply_futures_fill already assigned the
    # legs, so ONE formula covers both (no sign branch to get wrong).
    gross_pnl_paisa = sell_notional_paisa - buy_notional_paisa
    slippage_paisa = realized_slip_per_unit * n_shares

    if cost_free:
        # INDEX futures: signal-only, ZERO statutory/broker charges
        # (DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001). net == gross.
        return ChargeStack(
            buy_fill_paisa=buy_fill_paisa,
            sell_fill_paisa=sell_fill_paisa,
            n_shares=n_shares,
            buy_notional_paisa=buy_notional_paisa,
            sell_notional_paisa=sell_notional_paisa,
            brokerage_paisa=0,
            stt_paisa=0,
            exch_paisa=0,
            sebi_paisa=0,
            ipft_paisa=0,
            stamp_paisa=0,
            gst_paisa=0,
            slippage_paisa=slippage_paisa,
            total_charges_paisa=0,
            gross_pnl_paisa=gross_pnl_paisa,
            net_pnl_paisa=gross_pnl_paisa,
            realized_slip_per_share=realized_slip_per_unit,
        )

    # Every rounded charge below rounds ADVERSELY (ceiling) — the permanent
    # adverse-levy-rounding law (DEC-FILL-WORST-CASE-ONLY-001).
    # Brokerage: min(₹20, 0.03% × turnover) per executed order (Groww law) /
    # flat per order (Zerodha) — parameters caller-resolved.
    brokerage_paisa = _brokerage_leg_paisa(
        buy_notional_paisa, brokerage_flat_paisa, brokerage_turnover_bps_x100
    ) + _brokerage_leg_paisa(sell_notional_paisa, brokerage_flat_paisa, brokerage_turnover_bps_x100)
    # STT: sell-side turnover, statutory rupee rounding.
    stt_paisa = _round_levy_rupee((sell_notional_paisa * stt_rate) // 10_000_000)
    # ETC: both sides on turnover, adverse-ceiled to the paisa PER LEG then summed.
    exch_paisa = _round_levy_paisa(buy_notional_paisa * etc_rate) + _round_levy_paisa(
        sell_notional_paisa * etc_rate
    )
    # SEBI: both sides (COMMON verified rate).
    sebi_paisa = _round_levy_paisa(buy_notional_paisa * sebi_rate) + _round_levy_paisa(
        sell_notional_paisa * sebi_rate
    )
    # IPFT: both sides.
    ipft_paisa = _round_levy_paisa(buy_notional_paisa * ipft_rate) + _round_levy_paisa(
        sell_notional_paisa * ipft_rate
    )
    # Stamp: buy side, statutory rupee rounding.
    stamp_paisa = _round_levy_rupee((buy_notional_paisa * stamp_rate) // 10_000_000)
    # GST 18% on services only, rounded ONCE on the total (DEC-COST-001).
    gst_base_paisa = brokerage_paisa + exch_paisa + sebi_paisa + ipft_paisa
    gst_paisa = _round_levy_rupee((gst_base_paisa * gst_rate) // 10_000_000)

    total_charges_paisa = (
        brokerage_paisa + stt_paisa + exch_paisa + sebi_paisa + ipft_paisa + stamp_paisa + gst_paisa
    )
    return ChargeStack(
        buy_fill_paisa=buy_fill_paisa,
        sell_fill_paisa=sell_fill_paisa,
        n_shares=n_shares,
        buy_notional_paisa=buy_notional_paisa,
        sell_notional_paisa=sell_notional_paisa,
        brokerage_paisa=brokerage_paisa,
        stt_paisa=stt_paisa,
        exch_paisa=exch_paisa,
        sebi_paisa=sebi_paisa,
        ipft_paisa=ipft_paisa,
        stamp_paisa=stamp_paisa,
        gst_paisa=gst_paisa,
        slippage_paisa=slippage_paisa,
        total_charges_paisa=total_charges_paisa,
        gross_pnl_paisa=gross_pnl_paisa,
        net_pnl_paisa=gross_pnl_paisa - total_charges_paisa,
        realized_slip_per_share=realized_slip_per_unit,
    )


def compute_futures_trade_cost(trade: FuturesTradeInput) -> FuturesCostBreakdown:
    """Compute the full charge breakdown for one round-trip futures trade.

    Pure, deterministic, O(1) — the futures twin of
    :func:`brutex.costs.calculator.compute_trade_cost`, sharing its rounding
    rules (statutory STT/stamp/GST → nearest rupee; ETC/SEBI/IPFT → nearest
    paisa; GST 18% rounded ONCE on the total per DEC-COST-001).

    COST SCOPE: an INDEX future (:func:`is_cost_free_future_underlying`) returns
    an ALL-ZERO charge stack (``cost_free=True``, ``net == gross`` — the
    signal-only decree); a COST-BEARING future runs the verified stack and
    REFUSES on any unverified regime window (see the module verification
    ledger) — today the futures-ETC refusal makes every cost-bearing date
    refuse, so this completes only under a synthetic-cited test fixture until a
    real futures circular lands.

    Parameters
    ----------
    trade
        The round-trip specification.

    Returns
    -------
    FuturesCostBreakdown
        Per-component charges + roll-ups, all int64 paisa.

    Raises
    ------
    UnverifiedCostRegimeError
        When a COST-BEARING trade's entry date lands on any refusal window
        (futures STT pre-2024-10-01; futures ETC / NSE IPFT / stamp — every
        window today). The executor prechecks the ETC accessor and
        skips-and-counts BEFORE calling here; this raise is the
        defence-in-depth choke point for direct callers.
    """
    # The FuturesTradeInput validated all four extremes non-None at
    # construction; the COMMON `require_bar_extremes` seam narrows the
    # Optional typing (a None here is a broken dataclass invariant, not a
    # reachable state — it raises the explicit AssertionError, never a bare
    # `assert` per bandit B101).
    entry_high_paisa, entry_low_paisa, exit_high_paisa, exit_low_paisa = require_bar_extremes(
        entry_bar_high_paisa=trade.entry_bar_high_paisa,
        entry_bar_low_paisa=trade.entry_bar_low_paisa,
        exit_bar_high_paisa=trade.exit_bar_high_paisa,
        exit_bar_low_paisa=trade.exit_bar_low_paisa,
    )
    # COST SCOPE resolved FIRST (pure frozenset membership — never a raise):
    # an INDEX future never consults ANY regime table, so a 2021 index trade
    # prices fine (pinned). The stack receives zero rates on that arm.
    cost_free = is_cost_free_future_underlying(trade.instrument)
    if cost_free:
        stt_rate = exch_rate = ipft_rate = stamp_rate = 0
    else:
        entry_date = trade.entry_dt_ist.date()
        # Rate RESOLUTION in the PINNED refusal order — STT → ETC → NSE-IPFT →
        # stamp (DEC-ETC-REGIME-HISTORY-001; every raise happens HERE, before
        # any arithmetic — the stack itself is total):
        # STT raises on the pre-2024-10-01 refusal window.
        stt_rate = stt_futures_rate_bps_x100(entry_date)
        # ETC raises for EVERY window today (all-refusal tables; reachable
        # past this line only under a synthetic-cited fixture).
        exch_rate = futures_exchange_charge_bps_x100(trade.instrument, entry_date)
        # IPFT — NSE refuses today; BSE is the assumed-0 options stance.
        ipft_rate = ipft_futures_bps_x100(trade.instrument, entry_date)
        # Stamp — refuses today.
        stamp_rate = stamp_futures_rate_bps_x100(entry_date)

    # Broker parameter resolution (validated in __post_init__ — never raises):
    # Groww = min(flat ₹20, 0.03% × turnover) per leg; Zerodha = flat only.
    if trade.broker == "zerodha":
        brokerage_flat_paisa = ZERODHA_BROKERAGE_PAISA
        brokerage_turnover_bps_x100: int | None = None
    else:
        brokerage_flat_paisa = GROWW_BROKERAGE_PAISA
        brokerage_turnover_bps_x100 = GROWW_FUTURES_BROKERAGE_TURNOVER_BPS_X100

    # --- exec-rust dispatch (slice 7 / P8, DEC-EXEC-RUST-CORE-001) ---
    # Lazy import (the calculator-site cycle law; see compute_trade_cost).
    # Default-OFF (`BRUTEX_EXEC_RUST`): the seam returns None unless the
    # kernel serves; the Python core below stays the permanent oracle arm.
    from brutex import exec_rust

    stack = exec_rust.charge_stack_futures(
        entry_high_paisa,
        entry_low_paisa,
        exit_high_paisa,
        exit_low_paisa,
        is_long=trade.side == "long",
        lot_size=trade.lot_size,
        n_lots=trade.n_lots,
        cost_free=cost_free,
        brokerage_flat_paisa=brokerage_flat_paisa,
        brokerage_turnover_bps_x100=brokerage_turnover_bps_x100,
        stt_rate=stt_rate,
        etc_rate=exch_rate,
        sebi_rate=SEBI_FEE_BPS_X100,
        ipft_rate=ipft_rate,
        stamp_rate=stamp_rate,
        gst_rate=GST_TOTAL_BPS_X100,
    )
    # --- end exec-rust dispatch ---
    if stack is None:
        stack = _futures_charge_stack_py(
            entry_high_paisa,
            entry_low_paisa,
            exit_high_paisa,
            exit_low_paisa,
            is_long=trade.side == "long",
            lot_size=trade.lot_size,
            n_lots=trade.n_lots,
            cost_free=cost_free,
            brokerage_flat_paisa=brokerage_flat_paisa,
            brokerage_turnover_bps_x100=brokerage_turnover_bps_x100,
            stt_rate=stt_rate,
            etc_rate=exch_rate,
            sebi_rate=SEBI_FEE_BPS_X100,
            ipft_rate=ipft_rate,
            stamp_rate=stamp_rate,
            gst_rate=GST_TOTAL_BPS_X100,
        )
    return FuturesCostBreakdown(
        n_shares_per_lot=trade.lot_size,
        n_shares=stack.n_shares,
        buy_fill_paisa=stack.buy_fill_paisa,
        sell_fill_paisa=stack.sell_fill_paisa,
        buy_notional_paisa=stack.buy_notional_paisa,
        sell_notional_paisa=stack.sell_notional_paisa,
        brokerage_paisa=stack.brokerage_paisa,
        stt_paisa=stack.stt_paisa,
        exch_paisa=stack.exch_paisa,
        sebi_paisa=stack.sebi_paisa,
        ipft_paisa=stack.ipft_paisa,
        stamp_paisa=stack.stamp_paisa,
        gst_paisa=stack.gst_paisa,
        slippage_paisa=stack.slippage_paisa,
        total_charges_paisa=stack.total_charges_paisa,
        gross_pnl_paisa=stack.gross_pnl_paisa,
        net_pnl_paisa=stack.net_pnl_paisa,
        cost_free=cost_free,
    )
