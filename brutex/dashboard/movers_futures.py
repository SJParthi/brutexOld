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
1-day-close reader :func:`~brutex.dashboard.movers_snapshot._prev_day_close`. It
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
from dataclasses import dataclass
from datetime import date as date_cls
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from brutex.dashboard.movers_resilient import log_movers_skip, movers_read_skip_errors
from brutex.dashboard.movers_snapshot import (
    _DAILY_MIN_FLOOR,
    _IST,
    _cutoff_minute,
    _prev_day_close,
    _prior_session_last_from_minute,
    interval_minutes,
    parse_snapshot_date,
    resolve_candle_datasets,
    session_traded_value_paisa,
    value_crore_str,
)
from brutex.data.partition import SPOT_PROBE_SEGMENTS
from brutex.data.spot_features import pct_change
from brutex.money import format_inr, round_money, rupees_to_paisa

__all__ = [
    "DEFAULT_FUTURES_INTERVAL",
    "DEFAULT_FUTURES_TOP_N",
    "FuturesCandidates",
    "FuturesMover",
    "build_futures_payload",
    "compute_futures_candidates",
    "compute_futures_mover",
    "normalize_futures_tier",
    "parse_future_symbol",
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

#: The expiry token's fixed character length (``DDMmmYY`` = 2 + 3 + 2).
_EXPIRY_TOKEN_LEN: Final[int] = 7

#: The minimum hyphen-split arity of a FUT contract groww_symbol
#: (``EXCH-UNDERLYING-DDMmmYY-FUT`` → 4 parts; a stock name never hyphenates,
#: vendor symbols use hyphen only as the field separator).
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
    underlying = parts[1]
    expiry = _parse_expiry_token(parts[-2])
    if not underlying or expiry is None:
        return None
    return underlying, expiry


def _parse_expiry_token(token: str) -> date_cls | None:
    """Parse a ``DDMmmYY`` expiry token (``30Sep25``) into a date, or ``None``.

    The token is exactly 7 chars: a 2-digit day, a 3-letter month abbreviation
    (title-case, in :data:`_MONTHS`), and a 2-digit year (``20YY``). Any other
    shape, an unknown month, or an out-of-range day yields ``None`` so the
    caller skips the malformed contract rather than raising.
    """
    if len(token) != _EXPIRY_TOKEN_LEN:
        return None
    day_str, month_abbr, year_str = token[:2], token[2:5], token[5:]
    if not (day_str.isdigit() and year_str.isdigit()):
        return None
    month = _MONTHS.get(month_abbr)
    if month is None:
        return None
    try:
        return date_cls(_CENTURY + int(year_str), month, int(day_str))
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
        The underlying symbol (``NIFTY`` / ``RELIANCE``) — the row's display name.
    symbol
        The near-month contract groww_symbol (``NSE-NIFTY-30Sep25-FUT``) — the
        deterministic tiebreak key, never shown.
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
    """

    underlying: str
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


def rank_volume(movers: Sequence[FuturesMover], top_n: int) -> list[FuturesMover]:
    """Top-``n`` futures by cumulative fresh-session volume, descending."""
    pool = [m for m in movers if m.volume > 0]
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
    return FuturesMover(
        underlying=underlying,
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
    return {
        "name": mover.underlying,
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


def build_futures_payload(
    movers: Sequence[FuturesMover],
    *,
    interval: str,
    date: str,
    time: str | None,
    top_n: int,
    skipped: int,
    expiry_tier: str = _DEFAULT_TIER,
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
    ranked: dict[str, list[dict[str, Any]]] = {
        category: [_futures_row(m) for m in rank_fn(movers, top_n)]
        for category, rank_fn in _FUTURES_CATEGORIES.items()
    }
    instrument_count = len(movers) + skipped
    if instrument_count == 0:
        note = (
            "No futures in the lake yet — pull the F&O futures first on the Coverage "
            "page, then refresh. (Reference-only lake view, CLAUDE.md §3.)"
        )
    elif not any(ranked.values()):
        note = (
            f"{instrument_count} future(s) in the lake, but none had a bar at "
            f"{date} {time or '(daily)'} on {interval} — pick another time/day, or pull more."
        )
    else:
        note = (
            f"As-of {date} {time or '(daily)'} · {interval} — reference-only lake view "
            "(CLAUDE.md §3); % is vs the future's previous close, premium is vs the spot."
        )
    return {
        "interval": interval,
        "date": date,
        "time": time,
        "top_n": top_n,
        "expiry_tier": expiry_tier,
        "instrument_count": instrument_count,
        "skipped_count": skipped,
        "note": note,
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
) -> tuple[Decimal, int, int, int | None] | None:
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
    tuple[Decimal, int, int, int | None] | None
        ``(ltp, volume, value_paisa, oi)`` for the kept bars, or ``None`` when there
        are none.
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
        # Cast to Int32 BEFORE the arithmetic — `dt.hour()`/`dt.minute()` are
        # narrow Int8 (15*60 overflows), so the cast gives the true minute-of-day
        # (mirrors movers_snapshot._read_session_bars — CLAUDE.md §11 COMMON).
        minute_of_day = ist.dt.hour().cast(pl.Int32) * 60 + ist.dt.minute().cast(pl.Int32)
        day_df = day_df.filter(minute_of_day <= cutoff_min)
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
    return ltp, volume, value_paisa, oi


def _prev_day_oi(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, day: date_cls
) -> int | None:
    """Return the previous trading day's 1-day open interest before *day*, or ``None``.

    The OI twin of :func:`~brutex.dashboard.movers_snapshot._prev_day_close`:
    reads the symbol's stored ``1day`` partitions and returns the
    ``open_interest`` of the last bar whose IST trading date is strictly before
    *day*. ``None`` when there is no 1-day data, no prior day, or the prior bar's
    OI column is null — the OI % change is then an honest ``None``, never faked.

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
    int | None
        The prior trading day's open interest, or ``None``.
    """
    import polars as pl

    base = lake_root / "bars" / exchange / segment / symbol / "1day"
    if base.is_dir() and any(base.rglob("*.parquet")):
        df = pl.read_parquet(base.as_posix() + "/**/*.parquet")
        prior = df.filter(pl.col("timestamp").dt.convert_time_zone(_IST).dt.date() < day).sort(
            "timestamp"
        )
        if not prior.is_empty():
            raw_oi = prior.tail(1).get_column("open_interest")[0]
            if raw_oi is not None:
                return int(raw_oi)
    # Fallback: derived ``1day`` partition absent (or no prior 1-day OI) — recover the
    # prior trading day's OI from the stored 1-MINUTE bars (the OI twin of the
    # ``_prev_day_close`` fallback, the ONE shared bounded helper) so the OI %-change
    # works on an F&O lake whose 1-day derive never ran — the OI Gainers/Losers fix.
    recovered = _prior_session_last_from_minute(
        lake_root, exchange=exchange, segment=segment, symbol=symbol, day=day
    )
    return None if recovered is None else recovered[1]


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

    The expensive, ``top_n``-independent product of one lake scan — the near-month
    :class:`FuturesMover` per live underlying + the skipped count + the normalised
    snapshot date. Caching THIS (keyed by ``(exchange, interval, date, time)``)
    makes a sub-filter / ``top_n`` change an O(1) hit + a cheap re-rank
    (:func:`build_futures_payload`), never a re-scan (DEC-MOVERS-O1-LATENCY-001).

    Attributes
    ----------
    movers
        The computed near-month future rows (one per live underlying).
    skipped
        Count of underlyings/contracts dropped (all-expired, no session bars,
        or an unreadable Parquet file logged as ``movers.parquet_skip``).
    date
        The normalised ISO ``YYYY-MM-DD`` snapshot day.
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
            ltp, volume, value_paisa, oi = read
            prev_close = _prev_day_close(
                lake_root, exchange=exchange, segment=_FNO_SEGMENT, symbol=chosen, day=day
            )
            prev_oi = _prev_day_oi(
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
        movers.append(
            compute_futures_mover(
                underlying=resolved_names.get(underlying, underlying),
                symbol=chosen,
                exchange=exchange,
                ltp=ltp,
                volume=volume,
                value_paisa=value_paisa,
                oi=oi,
                prev_close=prev_close,
                spot_ltp=spot_ltp,
                prev_oi=prev_oi,
            )
        )

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
