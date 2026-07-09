"""Point-in-time "movers" snapshot core for the 🔮 **Futures** tab (DEC-MOVERS-FUTURES-001).

Milestone 2 of the redesigned ``/movers`` page: the Futures twin of the Spot
reader (:mod:`brutex.dashboard.movers_snapshot`). It answers the same single
question — *"who are the movers as of one date, on one timeframe, at one
time?"* — but over the **near-month index / stock FUTURES** the operator pulled
into the lake, computing the futures-specific columns (premium / discount vs the
underlying spot, open-interest change) the markets-screen Futures tab renders.

## Reuse, not re-implementation (CLAUDE.md §11 COMMON)

The session-time vocabulary lives in exactly ONE place — the Spot reader. This
module IMPORTS :func:`~brutex.dashboard.movers_snapshot.interval_minutes`,
:func:`~brutex.dashboard.movers_snapshot.session_bar_times`,
:func:`~brutex.dashboard.movers_snapshot.parse_snapshot_date`,
:func:`~brutex.dashboard.movers_snapshot._cutoff_minute`, and the prior-day
1-day-close reader :func:`~brutex.dashboard.movers_snapshot.resolve_prev_close`. It
adds only what futures need: the contract-symbol parser, the serial-expiry tier
picker (Near / Next / Far), the raw-1-minute reader that PRESERVES
``open_interest`` (the shared ``make_lake_read_1m`` path drops OI), the prior-day
OI reader, and the futures ranking + payload.

## The locked calculation rules (operator-confirmed 2026-06-13)

For each underlying that has a future in the lake (one row per underlying — its
contract at the selected serial-expiry tier):

* **Expiry tier** = the live contracts (expiry ``>= snapshot_date``) sorted
  earliest-first; ``near`` selects the front month, ``next`` the second serial
  month, ``far`` the third (the operator's Near / Next / Far selector). An
  underlying with no live contract at the requested tier's rank is SKIPPED (no
  fabricated or wrong-tier row — honest, Golden Rule #1), never faked.
* **LTP / volume / value / OI** come from the future's own session bars (raw
  1-minute, resampled by the same minute math the Spot reader uses, sliced to the
  selected ``time``). ``ltp`` is the last bar's close (2dp money), ``volume`` is the
  cumulative session quantity, ``value`` is the HLC3 typical-price × volume turnover
  (the "Top Value" metric, DEC-MOVERS-VWAP-VALUE-001), ``oi`` is the last bar's
  open interest.
* **% change** is vs the future's OWN previous trading-day 1-day close.
* **Premium / discount** = ``ltp − underlying_spot_ltp`` (the underlying's spot
  read the SAME way — its INDEX or CASH 1-minute bars). ``None`` when the
  underlying spot is absent. ``premium > 0`` → the *premium* bucket;
  ``premium < 0`` → the *discount* bucket.
* **OI % change** is vs the future's prior 1-day bar's open interest.

## Honesty (Golden Rule #1 / #10)

REFERENCE_ONLY over the **ingested lake** (CLAUDE.md §3) — never a live feed
(Track-1 has none — CLAUDE.md §1). Money is 2dp (``DEC-MONEY-2DP-001``); the
% / OI-% metrics keep FULL precision (a statistical metric, never money-rounded
— CLAUDE.md §4). An empty / futures-less lake (or an unknown interval) yields an
honest "pull the F&O futures first" payload — never a 500, never a fabricated
mover.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date as date_cls
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from brutex.dashboard.instrument_loader import resolve_fno_index_aliases
from brutex.dashboard.movers_resilient import log_movers_skip, movers_read_skip_errors
from brutex.dashboard.movers_snapshot import (
    _DAILY_MIN_FLOOR,
    _IST,
    _calendar_exchange,
    _cutoff_minute,
    _NoncontiguousWarnState,
    _prior_session_last_from_minute,
    filter_to_ist_minute,
    interval_minutes,
    make_expected_session_resolver,
    normalize_min_volume,
    parse_snapshot_date,
    prev_baseline_provenance,
    resolve_candle_datasets,
    resolve_prev_close,
    session_traded_value_paisa,
    value_crore_str,
    value_meets_volume_floor,
    warn_prev_baseline_noncontiguous,
)
from brutex.dashboard.movers_underlying_class import (
    KIND_UNKNOWN,
    class_echo,
    class_note_suffix,
    filter_by_underlying_class,
    fno_kind_from_underlying,
    normalize_underlying_class,
)
from brutex.data.fno_symbol import expiry_year_in_window
from brutex.data.partition import SPOT_PROBE_SEGMENTS
from brutex.data.spot_features import pct_change
from brutex.money import format_inr, round_money, rupees_to_paisa

__all__ = [
    "DEFAULT_FUTURES_INTERVAL",
    "DEFAULT_FUTURES_TOP_N",
    "FuturesCandidates",
    "FuturesMover",
    "PrevOiBaseline",
    "build_futures_payload",
    "compute_futures_candidates",
    "compute_futures_mover",
    "format_contract_date",
    "normalize_futures_tier",
    "parse_future_symbol",
    "resolve_prev_oi",
    "run_futures_snapshot",
    "select_expiry_tier",
    "select_near_month",
]

#: The Futures tab opens on the coarse 1-day timeframe (cheapest, always
#: populated if ANY future was pulled) and a generous depth — mirrors the Spot
#: tab defaults (CLAUDE.md §11 COMMON keeps the two pages aligned).
DEFAULT_FUTURES_INTERVAL: Final[str] = "1day"
#: 100 default (DEC-MOVERS-COMPLETENESS-001 — operator-directed "minimum 100 always");
#: mirrors the Spot tab default (CLAUDE.md §11 COMMON keeps the pages aligned).
DEFAULT_FUTURES_TOP_N: Final[int] = 100

#: The FNO segment every futures (and options) contract partition lives under in
#: the lake (``bars/<exch>/FNO/<contract>/<interval>/...``). Mirrors the
#: instrument-master backtesting segment (CLAUDE.md §6).
_FNO_SEGMENT: Final[str] = "FNO"

#: The two spot segments an underlying's cash/index series can live under — an
#: index future's underlying rides ``INDEX``; a stock future's rides ``CASH``. Tried in
#: this order when resolving the underlying spot LTP for premium. The shared
#: :data:`brutex.data.partition.SPOT_PROBE_SEGMENTS` is the ONE source of truth for the
#: order (the ``/db`` per-bar moneyness join uses the same; CLAUDE.md §11 COMMON).
_SPOT_SEGMENTS: Final[tuple[str, ...]] = SPOT_PROBE_SEGMENTS

#: A FUT contract groww_symbol ends with this suffix (``EXCH-UNDERLYING-DDMmmYY-FUT``).
#: Options share the FNO segment but their symbol carries a strike + ``-CE`` /
#: ``-PE`` suffix, so this anchor keeps options out of the futures universe.
_FUT_SUFFIX: Final[str] = "-FUT"

#: Three-letter month abbreviation → month number, for the ``DDMmmYY`` expiry
#: token (``30Sep25`` → 2025-09-30). The single parse table (CLAUDE.md §11).
_MONTHS: Final[dict[str, int]] = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

#: The two-digit year in the expiry token is in the 2000s (``25`` → 2025).
_CENTURY: Final[int] = 2000

#: Month number → three-letter abbreviation, the inverse of :data:`_MONTHS`. Used
#: by :func:`format_contract_date` so the human ``dd-Mon-YYYY`` contract label is
#: DETERMINISTIC across machines — ``datetime.strftime("%b")`` is LOCALE-dependent
#: (a non-English locale would render ``"déc."`` etc.), so the displayed contract
#: name must never go through it (CLAUDE.md §4 deterministic display).
_MONTH_ABBR: Final[dict[int, str]] = {number: abbr for abbr, number in _MONTHS.items()}


def format_contract_date(value: date_cls) -> str:
    """Format an expiry date as a deterministic, locale-stable ``DD-Mon-YYYY`` string.

    Renders e.g. ``date(2025, 9, 30)`` → ``"30-Sep-2025"`` using the fixed English
    :data:`_MONTH_ABBR` table rather than ``strftime("%b")`` (which is
    locale-dependent), so the displayed Futures / Options contract name reads
    identically on every machine (CLAUDE.md §4 / §11 COMMON — the ONE contract-date
    formatter, shared by both derivative movers tabs). The day is zero-padded to two
    digits and the year is the full four digits.

    Parameters
    ----------
    value
        The expiry (or any) date to render.

    Returns
    -------
    str
        The ``DD-Mon-YYYY`` label (``30-Sep-2025``).
    """
    return f"{value.day:02d}-{_MONTH_ABBR[value.month]}-{value.year:04d}"


#: The expiry token's fixed character length (``DDMmmYY`` = 2 + 3 + 2).
_EXPIRY_TOKEN_LEN: Final[int] = 7

#: The minimum hyphen-split arity of a FUT contract groww_symbol
#: (``EXCH-UNDERLYING-DDMmmYY-FUT`` → 4 parts). NOTE (review round 1): an
#: underlying CAN itself contain hyphens (``BAJAJ-AUTO`` — the vendor keeps the
#: spot spelling's interior hyphen), so the parse is right-ANCHORED and joins the
#: interior tokens back (mirroring :func:`~brutex.data.fno_symbol.parse_fno_symbol`);
#: the prior "a stock name never hyphenates" claim here was FALSE.
_MIN_FUT_PARTS: Final[int] = 4

#: The three serial-month futures expiry TIERS → the 0-based rank of the live
#: contract (sorted earliest-expiry-first) the tier selects: ``near`` = the
#: front month (next to expire), ``next`` = the second serial month, ``far`` =
#: the third. NSE lists three serial monthly futures at any time; an underlying
#: with fewer live contracts than the requested tier index is honestly dropped
#: (Golden Rule #1 — never a fabricated row). The ONE source of truth for the
#: tier→index mapping (CLAUDE.md §11 COMMON).
_TIER_INDEX: Final[dict[str, int]] = {"near": 0, "next": 1, "far": 2}

#: The default tier when none is selected — the front month (mirrors the prior
#: near-month-only behaviour, so the warmer + every un-tiered caller is
#: byte-equivalent).
_DEFAULT_TIER: Final[str] = "near"


def parse_future_symbol(symbol: str) -> tuple[str, date_cls] | None:
    """Parse a FUT contract groww_symbol into ``(underlying, expiry_date)`` or ``None``.

    The contract groww_symbol is ``EXCH-UNDERLYING-DDMmmYY-FUT`` (hyphen-joined),
    e.g. ``"NSE-NIFTY-30Sep25-FUT"`` → ``("NIFTY", date(2025, 9, 30))``. Returns
    ``None`` for any non-future symbol (an option symbol ends ``-CE`` / ``-PE``,
    so its last hyphen part is not ``FUT``) and for any junk / unparseable expiry
    token — the caller then skips it, never raising (Golden Rule #1).

    Right-ANCHORED, mirroring the canonical
    :func:`~brutex.data.fno_symbol.parse_fno_symbol` (CLAUDE.md §11 COMMON): the
    underlying is every token between the exchange (token ``0``) and the expiry,
    joined through any interior hyphens, so a hyphenated underlying is preserved
    intact — ``"NSE-BAJAJ-AUTO-30Sep25-FUT"`` → ``("BAJAJ-AUTO", …)``. Review
    round 1 fix: the prior ``parts[1]`` read TRUNCATED it to ``"BAJAJ"``,
    mis-keying the grouping / display name / underlying-spot probe AND the
    underlying-class lookup (a sanctioned correction, ledgered in
    ``MIGRATION.md`` #1631).

    Parameters
    ----------
    symbol
        A candidate contract groww_symbol from the lake's FNO datasets.

    Returns
    -------
    tuple[str, datetime.date] | None
        ``(underlying_symbol, expiry_date)`` when *symbol* is a well-formed
        future, else ``None``.
    """
    parts = symbol.split("-")
    # Need at least EXCH, UNDERLYING, expiry, FUT — and the last part must be FUT.
    if len(parts) < _MIN_FUT_PARTS or parts[-1] != "FUT":
        return None
    # Right-anchored: expiry/FUT from the END, the underlying is EVERYTHING between
    # (interior hyphens re-joined — the parse_fno_symbol idiom, review round 1).
    underlying = "-".join(parts[1:-2])
    expiry = _parse_expiry_token(parts[-2])
    if not underlying or expiry is None:
        return None
    return underlying, expiry


def _parse_expiry_token(token: str) -> date_cls | None:
    """Parse a ``DDMmmYY`` expiry token (``30Sep25``) into a date, or ``None``.

    The token is exactly 7 chars: a 2-digit day, a 3-letter month abbreviation
    (title-case, in :data:`_MONTHS`), and a 2-digit year (``20YY``). Any other
    shape, an unknown month, or an out-of-range day yields ``None`` so the
    caller skips the malformed contract rather than raising. The resolved year
    must also pass the SHARED :func:`~brutex.data.fno_symbol.expiry_year_in_window`
    sanity window (DEC-FNO-EXPIRY-YEAR-PIVOT-001) — ``30Jun75`` is junk, NOT a
    2075 expiry — so this shape-stricter twin can never diverge from the
    canonical :func:`~brutex.data.fno_symbol.parse_expiry_token` on which
    resolved years are accepted (CLAUDE.md §11 COMMON).
    """
    if len(token) != _EXPIRY_TOKEN_LEN:
        return None
    day_str, month_abbr, year_str = token[:2], token[2:5], token[5:]
    if not (day_str.isdigit() and year_str.isdigit()):
        return None
    month = _MONTHS.get(month_abbr)
    if month is None:
        return None
    year = _CENTURY + int(year_str)
    if not expiry_year_in_window(year):
        return None
    try:
        return date_cls(year, month, int(day_str))
    except ValueError:
        # Day out of range for the month (e.g. 31Feb) — malformed, skip honestly.
        return None


def normalize_futures_tier(expiry: str | None) -> str:
    """Normalise the raw ``?expiry=`` selector to a canonical futures tier token.

    Lower-cases the value; a blank / ``None`` / unknown tier ⇒ ``near`` (the
    front month). The single source of truth for the normalisation so the cache
    key and the computed candidate set agree on the canonical tier (CLAUDE.md §11
    COMMON) — the futures twin of
    :func:`~brutex.dashboard.movers_options.normalize_options_selectors` (futures
    carry no All/Monthly/Weekly track, only the Near/Next/Far tier). An unknown
    value such as ``"foo"`` normalises to ``near``, never a stale split.

    Parameters
    ----------
    expiry
        The raw ``?expiry=`` query value (``Near`` / ``Next`` / ``Far`` from the
        page, any case; blank / ``None`` / unknown ⇒ ``near``).

    Returns
    -------
    str
        A canonical lower-case tier token — one of ``near`` / ``next`` / ``far``.
    """
    tier = (expiry or _DEFAULT_TIER).lower()
    return tier if tier in _TIER_INDEX else _DEFAULT_TIER


def select_expiry_tier(
    items: Iterable[tuple[str, date_cls]], *, on_date: date_cls, tier: str = _DEFAULT_TIER
) -> str | None:
    """Return the *tier*-th live contract symbol for *on_date*, or ``None``.

    *items* are ``(symbol, expiry)`` pairs (the FUT contracts of ONE underlying).
    The live contracts are those whose expiry is ``>= on_date`` (a contract
    expiring today is still live), sorted earliest-expiry-first; ``near`` selects
    the front month, ``next`` the second serial month, ``far`` the third (the
    :data:`_TIER_INDEX` mapping; an unknown tier falls back to ``near``). When
    there is no live contract at the tier's rank — every contract expired, or the
    underlying lists fewer serial months than the requested tier — the underlying
    is dropped (``None``, Golden Rule #1, honest — never a fabricated or
    wrong-tier row). Ties on the expiry date break on the symbol string so the
    pick is deterministic (Rule #4).

    Parameters
    ----------
    items
        ``(symbol, expiry_date)`` pairs for one underlying's FUT contracts.
    on_date
        The snapshot date the tier is selected as-of.
    tier
        The serial expiry tier — ``near`` / ``next`` / ``far`` (default ``near``;
        an unknown value falls back to ``near`` via :data:`_TIER_INDEX`).

    Returns
    -------
    str | None
        The chosen contract symbol, or ``None`` when no live contract exists at
        the requested tier's rank.
    """
    live = sorted((expiry, symbol) for symbol, expiry in items if expiry >= on_date)
    index = _TIER_INDEX.get(tier, _TIER_INDEX[_DEFAULT_TIER])
    if index >= len(live):
        return None
    return live[index][1]


def select_near_month(items: Iterable[tuple[str, date_cls]], *, on_date: date_cls) -> str | None:
    """Return the front-month contract symbol for *on_date*, or ``None``.

    The ``near``-tier special case of :func:`select_expiry_tier` (kept as a named
    helper for the front-month callers + back-compat). See that function for the
    live-contract + tie-break rules.

    Parameters
    ----------
    items
        ``(symbol, expiry_date)`` pairs for one underlying's FUT contracts.
    on_date
        The snapshot date the near-month is selected as-of.

    Returns
    -------
    str | None
        The chosen contract symbol, or ``None`` when all contracts are expired.
    """
    return select_expiry_tier(items, on_date=on_date, tier=_DEFAULT_TIER)


@dataclass(frozen=True, slots=True)
class FuturesMover:
    """One near-month future's as-of-snapshot metrics (the locked rules, full precision).

    Attributes
    ----------
    underlying
        The underlying symbol (``NIFTY`` / ``RELIANCE``).
    expiry
        The contract's expiry date (parsed from :attr:`symbol`), or ``None`` when
        the symbol could not be parsed — the :attr:`name` label then falls back to
        the raw symbol / underlying (never blank, Golden Rule #1).
    symbol
        The near-month contract groww_symbol (``NSE-NIFTY-30Sep25-FUT``) — the
        deterministic tiebreak key + the raw-identity fallback for :attr:`name`.
    exchange, segment
        The future's lake partition coordinates (segment is always ``FNO``);
        used only for the stable ``(symbol, exchange, segment)`` tiebreak.
    ltp
        The future's last traded price — the selected bar's close, 2dp money.
    spot
        The underlying's spot LTP (2dp money), or ``None`` when the underlying
        spot is absent from the lake.
    premium
        ``ltp − spot`` (2dp money, may be negative), or ``None`` when *spot* is
        ``None``. Positive ⇒ the future trades at a premium, negative ⇒ discount.
    pct_change
        ``(ltp − prev_close) / prev_close × 100`` at FULL precision (a statistical
        metric, never money-rounded — CLAUDE.md §4), or ``None`` when there is no
        prior day.
    oi
        The selected bar's open interest, or ``None`` when the bar omits it.
    oi_chg_pct
        OI % change vs the future's prior 1-day open interest at full precision,
        or ``None`` when there is no prior day / a zero-or-missing prior OI / a
        missing current OI.
    volume
        Cumulative fresh-session traded quantity 09:15 → the selected bar.
    value_paisa
        Cumulative traded value (turnover) over the session bars as int64 paisa —
        ``Σ round_money((high+low+close)/3) × volume`` (the per-bar HLC3 typical
        price × volume VWAP-proxy turnover, DEC-MOVERS-VWAP-VALUE-001), shared with
        the spot + options screens via ``session_traded_value_paisa``.
    prev_close_session, prev_close_contiguous
        Calendar-contiguity provenance of the ``pct_change`` baseline
        (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001) — the ISO session date the prior
        close actually came from + whether it IS the immediately-preceding NSE
        session (``None`` = no baseline / calendar abstained). Additive +
        defaulted; the row projector emits the pair ONLY on ``False``.
    oi_baseline_session, oi_baseline_contiguous
        The INDEPENDENT OI twin — a null-OI 1-day bar makes the OI baseline
        fall to the minute fallback on a possibly DIFFERENT day than the close
        baseline, so the two flags never share a verdict.
    underlying_kind
        The resolved index-vs-stock class of this future's underlying
        (``index`` / ``stock`` / ``unknown``, DEC-MOVERS-UNDERLYING-CLASS-001) —
        looked up in the stored-master kind map. Additive + defaulted (``unknown``);
        consumed ONLY by the rank-time ``All | Indices | Stocks`` filter, never
        emitted in a row, so the default payload is byte-identical.
    """

    underlying: str
    expiry: date_cls | None
    symbol: str
    exchange: str
    segment: str
    ltp: Decimal
    spot: Decimal | None
    premium: Decimal | None
    pct_change: Decimal | None
    oi: int | None
    oi_chg_pct: Decimal | None
    volume: int
    value_paisa: int
    prev_close_session: str | None = None
    prev_close_contiguous: bool | None = None
    oi_baseline_session: str | None = None
    oi_baseline_contiguous: bool | None = None
    underlying_kind: str = KIND_UNKNOWN

    @property
    def name(self) -> str:
        """The contract's human display name, e.g. ``"NIFTY 30-Sep-2025 FUT"``.

        Joins the underlying, the deterministic ``DD-Mon-YYYY`` expiry
        (:func:`format_contract_date`), and the ``FUT`` suffix — the markets-screen
        Futures row label (the mockup intent). When :attr:`expiry` is ``None`` (the
        contract symbol could not be parsed), it falls back to the raw
        :attr:`symbol` if present, else the bare :attr:`underlying` — so the cell is
        NEVER blank and never crashes on an unparseable contract (Golden Rule #1).
        """
        if self.expiry is None:
            return self.symbol or self.underlying
        return f"{self.underlying} {format_contract_date(self.expiry)} FUT"

    @property
    def ltp_inr(self) -> str:
        """The future's LTP as a 2dp ``₹`` display string."""
        return format_inr(rupees_to_paisa(self.ltp))

    @property
    def value_crore(self) -> str:
        """Cumulative traded value rendered in ₹ crore (2dp) — shares the COMMON formatter."""
        return value_crore_str(self.value_paisa)

    @property
    def spot_inr(self) -> str | None:
        """The underlying spot LTP as a 2dp ``₹`` string, or ``None`` if absent."""
        return format_inr(rupees_to_paisa(self.spot)) if self.spot is not None else None

    @property
    def premium_inr(self) -> str | None:
        """The premium as a 2dp ``₹`` string (can be negative), or ``None`` if absent."""
        return format_inr(rupees_to_paisa(self.premium)) if self.premium is not None else None


def _sort_key(mover: FuturesMover) -> tuple[str, str, str]:
    """Deterministic tiebreak so equal metrics never reorder run-to-run (Rule #4)."""
    return (mover.symbol, mover.exchange, mover.segment)


def rank_price_gainers(movers: Sequence[FuturesMover], top_n: int) -> list[FuturesMover]:
    """Top-``n`` futures with ``pct_change > 0``, descending (stable tiebreak)."""
    pool = [m for m in movers if m.pct_change is not None and m.pct_change > 0]
    pool.sort(key=lambda m: (-(m.pct_change or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_price_losers(movers: Sequence[FuturesMover], top_n: int) -> list[FuturesMover]:
    """Top-``n`` futures with ``pct_change < 0``, ascending (most negative first)."""
    pool = [m for m in movers if m.pct_change is not None and m.pct_change < 0]
    pool.sort(key=lambda m: ((m.pct_change or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_premium(movers: Sequence[FuturesMover], top_n: int) -> list[FuturesMover]:
    """Top-``n`` futures trading at a PREMIUM (``premium > 0``), descending."""
    pool = [m for m in movers if m.premium is not None and m.premium > 0]
    pool.sort(key=lambda m: (-(m.premium or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_discount(movers: Sequence[FuturesMover], top_n: int) -> list[FuturesMover]:
    """Top-``n`` futures trading at a DISCOUNT (``premium < 0``), most negative first."""
    pool = [m for m in movers if m.premium is not None and m.premium < 0]
    pool.sort(key=lambda m: ((m.premium or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_volume(
    movers: Sequence[FuturesMover], top_n: int, *, min_volume: int = 0
) -> list[FuturesMover]:
    """Top-``n`` futures by cumulative fresh-session volume, descending.

    With the default ``min_volume == 0`` the pool is byte-identical to the prior
    ``volume > 0`` filter; a positive OPT-IN floor (DEC-MOVERS-MIN-VOLUME-001) drops
    contracts below it, via the COMMON
    :func:`~brutex.dashboard.movers_snapshot.value_meets_volume_floor` predicate.
    """
    pool = [m for m in movers if value_meets_volume_floor(m.volume, min_volume)]
    pool.sort(key=lambda m: (-m.volume, _sort_key(m)))
    return pool[:top_n]


def rank_value(movers: Sequence[FuturesMover], top_n: int) -> list[FuturesMover]:
    """Top-``n`` futures by cumulative fresh-session traded value (turnover), descending.

    Ranks the HLC3 typical-price turnover (``value_paisa``, DEC-MOVERS-VWAP-VALUE-001)
    — the futures twin of the spot/options "Top Value" ranking; a zero-turnover future
    is excluded (mirrors :func:`rank_volume`).
    """
    pool = [m for m in movers if m.value_paisa > 0]
    pool.sort(key=lambda m: (-m.value_paisa, _sort_key(m)))
    return pool[:top_n]


def rank_oi_gainers(movers: Sequence[FuturesMover], top_n: int) -> list[FuturesMover]:
    """Top-``n`` futures with ``oi_chg_pct > 0``, descending (stable tiebreak)."""
    pool = [m for m in movers if m.oi_chg_pct is not None and m.oi_chg_pct > 0]
    pool.sort(key=lambda m: (-(m.oi_chg_pct or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_oi_losers(movers: Sequence[FuturesMover], top_n: int) -> list[FuturesMover]:
    """Top-``n`` futures with ``oi_chg_pct < 0``, ascending (most negative first)."""
    pool = [m for m in movers if m.oi_chg_pct is not None and m.oi_chg_pct < 0]
    pool.sort(key=lambda m: ((m.oi_chg_pct or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


#: Futures category key → ranking function (the Futures tab's pill set, in the
#: template's order). Mirrors ``movers_snapshot._SPOT_CATEGORIES``.
_FUTURES_CATEGORIES: Final[dict[str, Any]] = {
    "price_gainers": rank_price_gainers,
    "price_losers": rank_price_losers,
    "premium": rank_premium,
    "discount": rank_discount,
    "volume": rank_volume,
    "value": rank_value,
    "oi_gainers": rank_oi_gainers,
    "oi_losers": rank_oi_losers,
}


def compute_futures_mover(
    *,
    underlying: str,
    symbol: str,
    exchange: str,
    ltp: Decimal,
    volume: int,
    value_paisa: int,
    oi: int | None,
    prev_close: Decimal | None,
    spot_ltp: Decimal | None,
    prev_oi: int | None,
) -> FuturesMover:
    """Reduce one near-month future's reads to a :class:`FuturesMover` (pure).

    *ltp* / *volume* / *oi* are the future's session reduction (last close,
    cumulative volume, last OI). *prev_close* is the future's prior 1-day close
    (``None`` ⇒ no % change). *spot_ltp* is the underlying's spot LTP (``None`` ⇒
    no premium). *prev_oi* is the future's prior 1-day OI. The premium uses
    money-snapped prices; the % / OI-% metrics keep full precision (CLAUDE.md §4).

    Parameters
    ----------
    underlying
        Underlying symbol (the display name).
    symbol, exchange
        The future's contract symbol + exchange (segment is always ``FNO``).
    ltp, volume, value_paisa, oi
        The future's session LTP (rupees), cumulative volume, traded value (turnover,
        int64 paisa — DEC-MOVERS-VWAP-VALUE-001), and last OI (or ``None``).
    prev_close
        The future's previous trading-day 1-day close (rupees), or ``None``.
    spot_ltp
        The underlying's spot LTP (rupees), or ``None`` when absent.
    prev_oi
        The future's previous trading-day 1-day open interest, or ``None``.

    Returns
    -------
    FuturesMover
        The fully-computed row (money 2dp, statistical metrics full-precision).
    """
    snapped_ltp = round_money(ltp)
    snapped_spot = round_money(spot_ltp) if spot_ltp is not None else None
    premium = (snapped_ltp - snapped_spot) if snapped_spot is not None else None
    snapped_prev = round_money(prev_close) if prev_close is not None else None
    pct = pct_change(snapped_ltp, snapped_prev) if snapped_prev is not None else None
    oi_chg_pct = (
        pct_change(Decimal(oi), Decimal(prev_oi))
        if (oi is not None and prev_oi is not None and prev_oi != 0)
        else None
    )
    # Derive the expiry from the contract symbol for the dated display name; an
    # unparseable symbol yields ``None`` → the name falls back to the raw symbol /
    # underlying (never crashes, never blank — Golden Rule #1).
    parsed = parse_future_symbol(symbol)
    expiry = parsed[1] if parsed is not None else None
    return FuturesMover(
        underlying=underlying,
        expiry=expiry,
        symbol=symbol,
        exchange=exchange,
        segment=_FNO_SEGMENT,
        ltp=snapped_ltp,
        spot=snapped_spot,
        premium=premium,
        pct_change=pct,
        oi=oi,
        oi_chg_pct=oi_chg_pct,
        volume=volume,
        value_paisa=value_paisa,
    )


def _futures_row(mover: FuturesMover) -> dict[str, Any]:
    """Project one mover to a markets-screen Futures row (money 2dp, metrics full-precision)."""
    row: dict[str, Any] = {
        # The FULL dated contract label ("NIFTY 30-Sep-2025 FUT") so the Futures tab
        # disambiguates Near/Next/Far contracts of the same underlying; falls back to
        # the raw symbol / underlying on an unparseable symbol (DEC-MOVERS-FULL-CONTRACT-NAME-001).
        "name": mover.name,
        "symbol": mover.symbol,
        "exchange": mover.exchange,
        "segment": mover.segment,
        "ltp": mover.ltp_inr,
        "spot": mover.spot_inr,
        "premium": mover.premium_inr,
        "pct_change": str(mover.pct_change) if mover.pct_change is not None else None,
        "oi": mover.oi,
        "oi_chg_pct": str(mover.oi_chg_pct) if mover.oi_chg_pct is not None else None,
        "volume": mover.volume,
        "value_cr": mover.value_crore,
    }
    # EMIT-ON-FALSE, each baseline INDEPENDENT (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001):
    # the additive provenance keys appear ONLY when the calendar PROVED that
    # baseline non-contiguous — contiguous and abstained rows stay byte-identical.
    if mover.prev_close_contiguous is False:
        row["prev_close_session"] = mover.prev_close_session
        row["prev_close_contiguous"] = mover.prev_close_contiguous
    if mover.oi_baseline_contiguous is False:
        row["oi_baseline_session"] = mover.oi_baseline_session
        row["oi_baseline_contiguous"] = mover.oi_baseline_contiguous
    return row


def build_futures_payload(
    movers: Sequence[FuturesMover],
    *,
    interval: str,
    date: str,
    time: str | None,
    top_n: int,
    skipped: int,
    expiry_tier: str = _DEFAULT_TIER,
    min_volume: int | None = None,
    latest_time: str | None = None,
    underlying_class: str | None = None,
) -> dict[str, Any]:
    """Assemble the markets-screen Futures ``/api/v1/movers`` JSON from computed movers.

    Builds one ranked list **per futures category** (``price_gainers`` /
    ``price_losers`` / ``premium`` / ``discount`` / ``volume`` / ``value`` /
    ``oi_gainers`` / ``oi_losers`` — the Futures tab's pills), keyed by the category name so the
    page's ``j[category]`` lookup picks the active one. Adds the echo of the
    chosen ``date`` / ``interval`` / ``time`` and an honest ``note``. Every value
    is JSON-native (money is a ``₹`` string, statistical metrics are
    full-precision strings, OI / volume are ints) so Starlette's ``JSONResponse``
    never chokes on a ``Decimal``.

    Parameters
    ----------
    movers
        The computed future rows for the selected tier (one per live underlying).
    interval, date, time, top_n
        Echoed snapshot coordinates.
    expiry_tier
        The serial expiry tier these rows were screened at (``near`` / ``next`` /
        ``far``) — echoed so the page can confirm the active selector.
    min_volume
        The OPT-IN "Top Volume" floor (DEC-MOVERS-MIN-VOLUME-001) applied at RANK
        time to the ``volume`` ranking only; ``None`` / ``0`` ⇒ the byte-identical
        no-floor default.
    underlying_class
        The ``All | Indices | Stocks`` filter (DEC-MOVERS-UNDERLYING-CLASS-001)
        applied at RANK time over the SAME candidate set; ``None`` / blank / unknown
        ⇒ ``all`` (byte-identical — no filtering, no added payload key). A non-``all``
        class drops non-matching + unclassified rows and echoes the active class +
        the ``excluded_unclassified`` count.
    latest_time
        The ADDITIVE display-only echo of the resolved latest intraday minute
        (``"HH:MM"`` IST, DEC-MOVERS-ASOF-LATEST-DEFAULT-001) — shown as
        ``latest (HH:MM)`` while the default cut stays whole-day; ``None`` for an
        explicit *time* / daily interval / empty lake. Never affects a number.
    skipped
        Count of underlyings/contracts dropped — no live contract at the tier,
        no session bars, OR an unreadable Parquet file (logged as
        ``movers.parquet_skip``; DEC-MOVERS-CORRUPT-SKIP-001) — reported honestly,
        never hidden (Golden Rule #1).

    Returns
    -------
    dict[str, Any]
        The Futures-tab payload: the eight ranked category lists + the echo +
        ``instrument_count`` / ``skipped_count`` / ``note``.
    """
    floor = normalize_min_volume(min_volume)
    # DEC-MOVERS-UNDERLYING-CLASS-001: RANK-time `All | Indices | Stocks` filter over
    # the SAME cached candidate set — never a re-scan (the DEC-MOVERS-MIN-VOLUME-001
    # template). `all` (the default) is byte-identical.
    wanted_class = normalize_underlying_class(underlying_class)
    ranked_movers, excluded_unclassified = filter_by_underlying_class(movers, wanted_class)
    ranked: dict[str, list[dict[str, Any]]] = {
        category: [
            _futures_row(m)
            for m in (
                rank_volume(ranked_movers, top_n, min_volume=floor)
                if category == "volume"
                else rank_fn(ranked_movers, top_n)
            )
        ]
        for category, rank_fn in _FUTURES_CATEGORIES.items()
    }
    class_note = class_note_suffix(wanted_class, excluded_unclassified)
    instrument_count = len(ranked_movers) + skipped
    lake_population = len(movers) + skipped
    if lake_population == 0:
        note = (
            "No futures in the lake yet — pull the F&O futures first on the Coverage "
            "page, then refresh. (Reference-only lake view, CLAUDE.md §3.)" + class_note
        )
    elif not any(ranked.values()):
        note = (
            f"{instrument_count} future(s) in the lake, but none had a bar at "
            f"{date} {time or '(daily)'} on {interval} — pick another time/day, or pull more."
            + class_note
        )
    else:
        note = (
            f"As-of {date} {time or '(daily)'} · {interval} — reference-only lake view "
            "(CLAUDE.md §3); % is vs the future's previous close, premium is vs the spot."
            + (f" Min volume ≥ {floor:,}." if floor > 0 else "")
            + class_note
        )
    return {
        "interval": interval,
        "date": date,
        "time": time,
        "top_n": top_n,
        "expiry_tier": expiry_tier,
        "min_volume": floor,
        "instrument_count": instrument_count,
        "skipped_count": skipped,
        "latest_time": latest_time,
        "note": note,
        **class_echo(wanted_class, excluded_unclassified),
        **ranked,
    }


# ────────────────────────────────────────────────────────────────────────
# Live orchestration — reads the lake (raw 1m for OI), snapshots (futures)
# ────────────────────────────────────────────────────────────────────────


def _read_future_session(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    day: date_cls,
    cutoff_min: int | None,
) -> tuple[Decimal, int, int, int | None, float | None, float | None] | None:
    """Read *day*'s raw 1-minute bars for one series, preserving open interest.

    Reads ``<lake>/bars/<exchange>/<segment>/<symbol>/1minute/**/*.parquet`` with
    polars DIRECTLY (NOT the shared ``make_lake_read_1m`` reader, which selects
    only OHLCV and DROPS ``open_interest`` — the futures tab needs OI). Filters to
    the IST trading *day*, sorts, and for an intraday timeframe keeps bars whose
    IST minute-of-day ``≤ cutoff_min`` (the Int32-cast minute math the Spot
    reader uses — ``dt.hour()`` / ``dt.minute()`` are narrow Int8 and overflow
    otherwise). A daily timeframe passes ``cutoff_min=None`` ⇒ no cutoff. From
    the kept bars: the LTP is the last close (2dp money), the volume is the sum,
    the VALUE (turnover) is the HLC3 typical-price × volume sum via the shared
    :func:`~brutex.dashboard.movers_snapshot.session_traded_value_paisa`
    (DEC-MOVERS-VWAP-VALUE-001), and the OI is the last bar's ``open_interest``
    (``None`` when the column is null). Returns ``None`` when the directory is
    missing or no bar falls on the day / within the cutoff (the caller counts it
    skipped, never fabricated — Golden Rule #1).

    This is an **as-of cutoff SNAPSHOT** (cumulative session-to-cutoff: last close /
    summed volume / last OI ≤ cutoff), NOT a single resampled TF bucket — the honest
    partial-as-of of corrected_point #1b (DEC-FNO-TF-DERIVE-OI-001). A TRUE per-TF
    resampled F&O bar (with the trailing forming-bucket dropped, corrected_point #1a)
    is the COMMON :func:`brutex.data.fno_resample.resample_session_to_tf`, which carries
    last-of-bucket OI via the SAME OI-aware resampler this snapshot's OI rule mirrors.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    exchange, segment, symbol
        Partition coordinates of the series to read (a future under ``FNO`` or
        an underlying spot under ``INDEX`` / ``CASH``).
    day
        The IST trading day to snapshot.
    cutoff_min
        Intraday minute-of-day cutoff, or ``None`` for the whole day.

    Returns
    -------
    tuple[Decimal, int, int, int | None, float | None, float | None] | None
        ``(ltp, volume, value_paisa, oi, iv, delta)`` for the kept bars, or
        ``None`` when there are none. ``iv`` / ``delta`` are the LAST kept bar's
        STORED Greeks overlay values (DEC-GREEKS-ENGINE-PR3B-001 — read from the
        SAME frame, zero extra I/O, never recomputed at render): the Phase-2b
        enrichment (DEC-GREEKS-ENGINE-PR2B-001) writes them only for OPTION
        contracts, so a future / spot / legacy pre-Greeks partition (whose frame
        lacks the columns) and a stamped-NULL bar (plan §15.8 honest NA) both
        read ``None`` — never a fabricated Greek (Golden Rule #1), and never
        backfilled from an earlier bar (the same as-of last-bar rule LTP/OI use).
    """
    import polars as pl

    from brutex.data.lake_day_glob import read_day_intraday_frame

    # Read the day's bars via the symbol-aware reader: ONLY the day's month
    # Parquet (1minute/<yyyy>/<mm>.parquet) for a session-filtered symbol — an IST
    # day's 1-minute bars all live in that one file (the session never crosses a
    # UTC month boundary), so this is byte-equivalent to the prior whole-partition
    # read but O(one month) not O(all-bars) (DEC-MOVERS-O1-LATENCY-001); a
    # session-exempt underlying spot (GIFT Nifty) falls back to the whole
    # partition there. FNO contract symbols are session-filtered unconditionally
    # at ingest, so they always take the fast month-bound path.
    base = lake_root / "bars" / exchange / segment / symbol / "1minute"
    df = read_day_intraday_frame(base, day, symbol=symbol)
    if df is None:
        return None
    ist = pl.col("timestamp").dt.convert_time_zone(_IST)
    day_df = df.filter(ist.dt.date() == day).sort("timestamp")
    if cutoff_min is not None:
        # The ONE COMMON raw-1m as-of cut shared with the spot reader
        # (movers_snapshot.filter_to_ist_minute — DEC-HARDENING-LEFTOVERS-001,
        # CLAUDE.md §11 COMMON; byte-identical to the prior inline expression).
        day_df = filter_to_ist_minute(day_df, cutoff_min, ts_col="timestamp")
    if day_df.is_empty():
        return None
    last = day_df.tail(1)
    ltp = round_money(Decimal(str(last.get_column("close")[0])))
    volume = int(day_df.get_column("volume").sum())
    # DEC-MOVERS-VWAP-VALUE-001: turnover is the per-bar HLC3 typical-price × volume sum,
    # via the ONE shared helper (spot + options + futures share it — CLAUDE.md §11 COMMON).
    value_paisa = session_traded_value_paisa(
        (Decimal(str(high)), Decimal(str(low)), Decimal(str(close)), int(vol))
        for high, low, close, vol in zip(
            day_df.get_column("high").to_list(),
            day_df.get_column("low").to_list(),
            day_df.get_column("close").to_list(),
            day_df.get_column("volume").to_list(),
            strict=True,
        )
    )
    raw_oi = last.get_column("open_interest")[0]
    oi = None if raw_oi is None else int(raw_oi)
    # Stored Greeks overlay, LAST kept bar (DEC-GREEKS-ENGINE-PR3B-001): read off
    # the SAME frame — zero extra I/O. Schema-tolerant: a legacy pre-Greeks /
    # spot / futures frame lacks the columns → None; a stamped-NULL last bar
    # (§15.8) stays None — honest NA, never backfilled (Golden Rule #1).
    iv = _last_bar_overlay(last, "iv")
    delta = _last_bar_overlay(last, "delta")
    return ltp, volume, value_paisa, oi, iv, delta


def _last_bar_overlay(last: Any, column: str) -> float | None:
    """Return the single-row frame *last*'s stored overlay *column* as a float, or ``None``.

    The tolerant per-column extractor for the session snapshot's Greeks cells
    (DEC-GREEKS-ENGINE-PR3B-001): ``None`` when the frame does not carry the
    column at all (a legacy pre-Greeks partition / a never-enriched future or
    spot series) OR when the stored cell is NULL (a plan-§15.8 stamped-NA bar) —
    an honest absent value in both cases, never a fabricated Greek (Golden
    Rule #1). *last* is a one-row polars frame (typed ``Any`` — polars is a
    lazy function-local import in this module).
    """
    if column not in last.columns:
        return None
    raw = last.get_column(column)[0]
    return None if raw is None else float(raw)


@dataclass(frozen=True, slots=True)
class PrevOiBaseline:
    """A resolved previous-day OI baseline + the session it actually came from.

    DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 — the OI twin of
    :class:`~brutex.dashboard.movers_snapshot.PrevDayBaseline`: ``value`` is the
    served baseline (byte-identical to the historical ``_prev_day_oi`` answer)
    and ``found_date`` is the IST session date the OI bar belongs to, judged by
    :func:`~brutex.dashboard.movers_snapshot.prev_baseline_provenance`
    INDEPENDENTLY of the close baseline (a null-OI 1-day bar makes the OI fall
    to the minute fallback on a possibly DIFFERENT day). ``(None, None)`` when
    no prior-session OI resolved.
    """

    value: int | None
    found_date: date_cls | None


def resolve_prev_oi(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, day: date_cls
) -> PrevOiBaseline:
    """Return the previous trading day's open interest before *day* + its session date.

    The OI twin of :func:`~brutex.dashboard.movers_snapshot.resolve_prev_close`:
    reads the symbol's stored ``1day`` partitions and returns the
    ``open_interest`` of the last bar whose IST trading date is strictly before
    *day*. ``PrevOiBaseline(None, None)`` when there is no 1-day data, no prior
    day, or neither source yields an OI — the OI % change is then an honest
    ``None``, never faked. The served VALUE is byte-identical to the historical
    ``_prev_day_oi`` in every branch (the value-seam law) — this resolver ADDS
    only the found session date. The 1-day OI scan physically stays HERE
    (movers_futures) so the DEC-LAKE-READ-UNIQUE-001 bypass-gate allowlist keeps
    its wired dedup entry for this module.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    exchange, segment, symbol
        The future's partition coordinates (segment ``FNO``).
    day
        The snapshot day; the prior 1-day bar is strictly before it.

    Returns
    -------
    PrevOiBaseline
        The prior trading day's open interest + its IST session date.
    """
    import polars as pl

    from brutex.data.lake_read import dedup_bars_frame

    base = lake_root / "bars" / exchange / segment / symbol / "1day"
    if base.is_dir() and any(base.rglob("*.parquet")):
        # Keep-FIRST read dedup (DEC-LAKE-READ-UNIQUE-001): the `.tail(1)` below
        # would otherwise pick a poisoned-era duplicate re-statement over the
        # first stored bar — warned, never silent.
        df, _dropped = dedup_bars_frame(
            pl.read_parquet(base.as_posix() + "/**/*.parquet"),
            source=f"movers_futures_oi:{exchange}/{segment}/{symbol}",
        )
        prior = df.filter(pl.col("timestamp").dt.convert_time_zone(_IST).dt.date() < day).sort(
            "timestamp"
        )
        if not prior.is_empty():
            last = prior.tail(1)
            raw_oi = last.get_column("open_interest")[0]
            if raw_oi is not None:
                found = last.get_column("timestamp").dt.convert_time_zone(_IST).dt.date()[0]
                return PrevOiBaseline(value=int(raw_oi), found_date=found)
    # Fallback: derived ``1day`` partition absent (or no prior 1-day OI) — recover the
    # prior trading day's OI from the stored 1-MINUTE bars (the OI twin of the
    # ``resolve_prev_close`` fallback, the ONE shared bounded helper) so the OI %-change
    # works on an F&O lake whose 1-day derive never ran — the OI Gainers/Losers fix.
    recovered = _prior_session_last_from_minute(
        lake_root, exchange=exchange, segment=segment, symbol=symbol, day=day
    )
    if recovered is None:
        return PrevOiBaseline(value=None, found_date=None)
    return PrevOiBaseline(value=recovered[1], found_date=recovered[2])


def _prev_day_oi(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, day: date_cls
) -> int | None:
    """Return the previous trading day's 1-day open interest before *day*, or ``None``.

    The byte-identical value seam over :func:`resolve_prev_oi`
    (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001): every historical caller/pin of the
    plain-``int`` contract keeps its exact answer; the enriched resolver
    additionally carries the found session date for the contiguity stamping.
    """
    return resolve_prev_oi(
        lake_root, exchange=exchange, segment=segment, symbol=symbol, day=day
    ).value


def _underlying_spot_ltp(
    lake_root: Path, *, exchange: str, underlying: str, day: date_cls, cutoff_min: int | None
) -> Decimal | None:
    """Return the underlying's spot LTP for *day* (try ``INDEX`` then ``CASH``), or ``None``.

    An index future's underlying spot lives under ``INDEX``; a stock future's
    under ``CASH``. We probe the segments in :data:`_SPOT_SEGMENTS` order and
    return the first that yields a session read (its last close, 2dp money).
    ``None`` when the underlying spot is absent from both — the premium is then an
    honest ``None`` (Golden Rule #1).
    """
    for segment in _SPOT_SEGMENTS:
        read = _read_future_session(
            lake_root,
            exchange=exchange,
            segment=segment,
            symbol=underlying,
            day=day,
            cutoff_min=cutoff_min,
        )
        if read is not None:
            return read[0]
    return None


def _discover_futures(
    lake_root: Path,
    *,
    exchange: str | None = None,
    datasets: list[dict[str, str]] | None = None,
) -> dict[str, list[tuple[str, str, date_cls]]]:
    """Group the lake's FUT datasets by underlying → ``[(symbol, exchange, expiry)]``.

    Walks the lake's candle datasets (the memoised list when the route threads one
    in via *datasets* — :func:`~brutex.dashboard.movers_snapshot.resolve_candle_datasets`,
    the O(1)-latency seam — else a fresh :func:`~brutex.dashboard.db_view.list_candle_datasets`
    scan), keeps ONLY the ``FNO`` ``1minute`` datasets whose symbol ends ``-FUT``
    (options share the FNO segment but carry a strike + ``-CE`` / ``-PE`` suffix),
    parses each into ``(underlying, expiry)`` (a non-parsing symbol is skipped), and
    buckets by underlying. When *exchange* is given only that exchange's contracts
    are kept (an exact per-exchange ranking). Returns ``{}`` when no futures present.
    """
    by_underlying: dict[str, list[tuple[str, str, date_cls]]] = {}
    for dataset in resolve_candle_datasets(lake_root, datasets):
        if dataset["segment"] != _FNO_SEGMENT or dataset["interval"] != "1minute":
            continue
        if exchange is not None and dataset["exchange"] != exchange:
            continue
        symbol = dataset["symbol"]
        if not symbol.endswith(_FUT_SUFFIX):
            continue
        parsed = parse_future_symbol(symbol)
        if parsed is None:
            continue
        underlying, expiry = parsed
        by_underlying.setdefault(underlying, []).append((symbol, dataset["exchange"], expiry))
    return by_underlying


@dataclass(frozen=True, slots=True)
class FuturesCandidates:
    """The cacheable futures candidate set for one snapshot coordinate (pre-ranking).

    The expensive, ``top_n``-independent product of one lake scan — the
    selected-tier :class:`FuturesMover` per live underlying + the skipped count +
    the normalised snapshot date + the canonical expiry tier. Caching THIS (keyed
    by ``(exchange, interval, date, time, expiry_tier)`` so Near / Next / Far
    cache independently) makes a sub-filter / ``top_n`` change an O(1) hit + a
    cheap re-rank (:func:`build_futures_payload`), never a re-scan
    (DEC-MOVERS-O1-LATENCY-001 / DEC-MOVERS-FUTURES-TIER-001).

    Attributes
    ----------
    movers
        The computed future rows for the selected tier (one per live underlying).
    skipped
        Count of underlyings/contracts dropped (no live contract at the tier, no
        session bars, or an unreadable Parquet file logged as
        ``movers.parquet_skip``).
    date
        The normalised ISO ``YYYY-MM-DD`` snapshot day.
    expiry_tier
        The canonical serial expiry tier these candidates were screened at
        (``near`` / ``next`` / ``far``) — echoed so the payload + page agree.
    """

    movers: tuple[FuturesMover, ...]
    skipped: int
    date: str
    expiry_tier: str = _DEFAULT_TIER


def compute_futures_candidates(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    exchange: str | None = None,
    names: Mapping[str, str] | None = None,
    datasets: list[dict[str, str]] | None = None,
    expiry_tier: str = _DEFAULT_TIER,
    underlying_kinds: dict[str, str] | None = None,
) -> FuturesCandidates:
    """Scan the lake → the :class:`FuturesCandidates` for one snapshot coordinate.

    The expensive, cacheable core extracted from :func:`run_futures_snapshot`:
    discovers each underlying's FUT contract at the selected serial expiry *tier*
    (``near`` / ``next`` / ``far`` as-of *date*, optionally scoped to *exchange*),
    reads each future's selected-day session (raw 1-minute, OI preserved) up to
    *time*, takes the future's prior 1-day close + OI and the underlying spot's
    LTP, and computes the per-underlying :class:`FuturesMover`. Ranking is the
    cheap, ``top_n``-dependent step done by :func:`build_futures_payload`. An empty
    / futures-less lake (or an unknown interval) yields an empty
    :class:`FuturesCandidates` (Golden Rule #1).

    Parameters
    ----------
    lake_root, interval, date, time, names
        See :func:`run_futures_snapshot`.
    exchange
        Restrict the screened futures to one exchange (``NSE`` / ``BSE``) for an
        exact per-exchange ranking; ``None`` screens every exchange.
    expiry_tier
        The serial expiry tier to screen — ``near`` (front month, default) /
        ``next`` / ``far`` (:func:`normalize_futures_tier` canonicalises it; an
        unknown value falls back to ``near``). It is part of the candidate cache
        key, so Near / Next / Far each cache + rank independently (never a stale
        Near row served for a Far request).
    underlying_kinds
        The stored-master ``{underlying → kind}`` map
        (:func:`~brutex.dashboard.movers_underlying_class.build_underlying_kind_map`,
        DEC-MOVERS-UNDERLYING-CLASS-001) used to stamp each future's
        ``underlying_kind`` for the RANK-time ``All | Indices | Stocks`` filter.
        ``None`` (a direct/test caller) leaves every kind ``unknown`` — inert for
        the default ``all`` class. NOT part of the cache key (the class filters the
        shared set at rank time, so one cached set serves all three classes).

    Returns
    -------
    FuturesCandidates
        The selected-tier future rows + skipped count + normalised date + the
        canonical ``expiry_tier`` echoed.

    Raises
    ------
    ValueError
        On a malformed *date* / *time* (the route maps it to a 400).
    """
    tier = normalize_futures_tier(expiry_tier)
    minutes = interval_minutes(interval)
    day = parse_snapshot_date(date)
    date_str = day.isoformat()
    cutoff_min = None if (minutes is None or minutes >= _DAILY_MIN_FLOOR) else _cutoff_minute(time)
    resolved_names = names or {}
    by_underlying = _discover_futures(lake_root, exchange=exchange, datasets=datasets)
    if minutes is None or not by_underlying:
        return FuturesCandidates(movers=(), skipped=0, date=date_str, expiry_tier=tier)

    movers: list[FuturesMover] = []
    skipped = 0
    skip_errors = movers_read_skip_errors()
    warn_state = _NoncontiguousWarnState()
    expected_session_for = make_expected_session_resolver(day)
    # The spot→F&O index alias map for the class lookup, resolved ONCE per compute
    # call (an env parse + dict copy) and threaded into every per-candidate
    # `fno_kind_from_underlying` — the lookup stays O(1)/candidate (review round 1).
    class_aliases = resolve_fno_index_aliases()
    for underlying, contracts in by_underlying.items():
        chosen = select_expiry_tier(
            [(sym, exp) for sym, _exch, exp in contracts], on_date=day, tier=tier
        )
        if chosen is None:  # no live contract at this tier's rank → honest skip.
            skipped += 1
            continue
        exchange = next(exch for sym, exch, _exp in contracts if sym == chosen)
        try:
            read = _read_future_session(
                lake_root,
                exchange=exchange,
                segment=_FNO_SEGMENT,
                symbol=chosen,
                day=day,
                cutoff_min=cutoff_min,
            )
            if read is None:  # no session bar on the selected day/time → skip, never faked.
                skipped += 1
                continue
            # Futures take only the price/volume/OI slice — FUT legs are never
            # Greeks-enriched (DEC-GREEKS-ENGINE-PR2B-001), so the trailing
            # iv/delta of the shared read are always None here (options consume
            # them — DEC-GREEKS-ENGINE-PR3B-001).
            ltp, volume, value_paisa, oi = read[:4]
            close_baseline = resolve_prev_close(
                lake_root, exchange=exchange, segment=_FNO_SEGMENT, symbol=chosen, day=day
            )
            oi_baseline = resolve_prev_oi(
                lake_root, exchange=exchange, segment=_FNO_SEGMENT, symbol=chosen, day=day
            )
            spot_ltp = _underlying_spot_ltp(
                lake_root, exchange=exchange, underlying=underlying, day=day, cutoff_min=cutoff_min
            )
        except skip_errors as exc:
            # A corrupt / unreadable Parquet for ONE future must not 500 the screen —
            # skip it (logged + counted), never fabricate (DEC-MOVERS-CORRUPT-SKIP-001).
            log_movers_skip(source="futures", symbol=chosen, error=str(exc))
            skipped += 1
            continue
        mover = compute_futures_mover(
            underlying=resolved_names.get(underlying, underlying),
            symbol=chosen,
            exchange=exchange,
            ltp=ltp,
            volume=volume,
            value_paisa=value_paisa,
            oi=oi,
            prev_close=close_baseline.value,
            spot_ltp=spot_ltp,
            prev_oi=oi_baseline.value,
        )
        # Baseline calendar-contiguity provenance, close + OI judged INDEPENDENTLY
        # (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001) — a null-OI 1-day bar sends the OI
        # baseline to the minute fallback on a possibly DIFFERENT day. Served
        # values are untouched; each strictly-False verdict gets ONE capped WARN.
        cal_exchange = _calendar_exchange(exchange)
        expected_session = expected_session_for(cal_exchange)
        close_iso, close_contig = prev_baseline_provenance(
            close_baseline.found_date, day, exchange=cal_exchange, expected_session=expected_session
        )
        oi_iso, oi_contig = prev_baseline_provenance(
            oi_baseline.found_date, day, exchange=cal_exchange, expected_session=expected_session
        )
        mover = replace(
            mover,
            prev_close_session=close_iso,
            prev_close_contiguous=close_contig,
            oi_baseline_session=oi_iso,
            oi_baseline_contiguous=oi_contig,
            # DEC-MOVERS-UNDERLYING-CLASS-001: resolve the index-vs-stock class from
            # the stored-master kind map. The map is keyed by primary_fno_underlying
            # FORMS (BAJAJAUTO) while the PARSED token is hyphen-preserving
            # (BAJAJ-AUTO), so the lookup normalises the token into the map's key
            # space via the SAME resolver + the hoisted alias map (review round 1).
            # Attached once so the rank-time filter never re-reads the master;
            # absent map / unrecognised underlying → KIND_UNKNOWN (Golden Rule #1).
            underlying_kind=fno_kind_from_underlying(
                underlying, underlying_kinds, aliases=class_aliases
            ),
        )
        if close_iso is not None and close_contig is False:
            warn_prev_baseline_noncontiguous(
                warn_state,
                kind="prev_close",
                exchange=exchange,
                segment=_FNO_SEGMENT,
                symbol=chosen,
                snapshot_day=day,
                found_session=close_iso,
                expected_session=expected_session,
            )
        if oi_iso is not None and oi_contig is False:
            warn_prev_baseline_noncontiguous(
                warn_state,
                kind="oi",
                exchange=exchange,
                segment=_FNO_SEGMENT,
                symbol=chosen,
                snapshot_day=day,
                found_session=oi_iso,
                expected_session=expected_session,
            )
        movers.append(mover)

    return FuturesCandidates(movers=tuple(movers), skipped=skipped, date=date_str, expiry_tier=tier)


def run_futures_snapshot(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    top_n: int,
    exchange: str | None = None,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compute the futures movers snapshot over the lake → the ``/api/v1/movers`` payload.

    The thin compose of :func:`compute_futures_candidates` (the expensive, cacheable
    scan) + :func:`build_futures_payload` (the cheap, ``top_n``-dependent rank).
    Behaviour is unchanged from the pre-split version — an empty / futures-less lake
    (or an unknown interval) yields an honest "pull the F&O futures first" payload,
    never a 500 (Golden Rule #1).

    Parameters
    ----------
    lake_root, interval, date, time, top_n, exchange, names
        See :func:`compute_futures_candidates` (plus *top_n*, the ranking depth).

    Raises
    ------
    ValueError
        On a malformed *date* / *time* (the route maps it to a 400).
    """
    candidates = compute_futures_candidates(
        lake_root=lake_root,
        interval=interval,
        date=date,
        time=time,
        exchange=exchange,
        names=names,
    )
    return build_futures_payload(
        candidates.movers,
        interval=interval,
        date=candidates.date,
        time=time,
        top_n=top_n,
        skipped=candidates.skipped,
    )
