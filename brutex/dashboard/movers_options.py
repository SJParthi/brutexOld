"""Point-in-time "movers" snapshot core for the 🎯 **Options** tab (DEC-MOVERS-OPTIONS-001).

Milestone 3 of the redesigned ``/movers`` page: the Options twin of the Spot
(:mod:`brutex.dashboard.movers_snapshot`) + Futures
(:mod:`brutex.dashboard.movers_futures`) readers. It answers the same single
question — *"who are the movers as of one date, on one timeframe, at one
time?"* — but over the **individual option contracts** the operator pulled into
the lake. Unlike the Futures reader (which collapses each underlying to ONE
near-month row), an options screen lists **one row per CONTRACT** — every strike
× CE/PE × expiry found in the lake — exactly as the broker markets-screen
Options tab renders them.

## Reuse, not re-implementation (CLAUDE.md §11 COMMON)

The session-time vocabulary + the raw-1-minute / prior-day readers live in
exactly ONE place each. This module IMPORTS:

* from :mod:`brutex.dashboard.movers_snapshot` —
  :func:`~brutex.dashboard.movers_snapshot.interval_minutes`,
  :func:`~brutex.dashboard.movers_snapshot.parse_snapshot_date`,
  :func:`~brutex.dashboard.movers_snapshot._cutoff_minute`,
  :data:`~brutex.dashboard.movers_snapshot._IST`,
  :data:`~brutex.dashboard.movers_snapshot._DAILY_MIN_FLOOR`, and the prior-day
  1-day close reader :func:`~brutex.dashboard.movers_snapshot._prev_day_close`
  (its canonical home — the Futures reader re-imports the same one);
* from :mod:`brutex.dashboard.movers_futures` — the GENERIC raw-1-minute
  session reader :func:`~brutex.dashboard.movers_futures._read_future_session`
  (it returns ``(ltp, volume, oi)`` for ANY FNO contract, futures OR options —
  reused verbatim for an option contract), the prior-day OI reader
  :func:`~brutex.dashboard.movers_futures._prev_day_oi`, and the underlying spot
  resolver :func:`~brutex.dashboard.movers_futures._underlying_spot_ltp`.

It adds only what options need: the contract-symbol parser (strike + CE/PE), the
per-contract value reader, the options ranking (highest OI / OI gainers / OI
losers / volume / value / price gainers / price losers), and the options payload.

## The locked calculation rules (operator-confirmed 2026-06-13)

For each option contract (strike × CE/PE × expiry) found in the lake:

* **LTP / volume / OI** come from the contract's own session bars (raw 1-minute,
  sliced to the selected ``time`` by the SAME minute math the Spot reader uses).
  ``ltp`` is the last bar's close (2dp money), ``volume`` is the cumulative
  fresh-session quantity, ``oi`` is the last bar's open interest (``None``-safe).
* **Value (₹)** is the cumulative traded value over those session bars
  (``Σ round_money(close) × volume``), kept as int64 paisa via
  :mod:`brutex.money` — mirrors ``movers_snapshot.MoverSnapshot.value_paisa``.
* **% change** is vs the contract's OWN previous trading-day 1-day close
  (``None`` when there is no prior day).
* **OI change** is ``oi − prev_oi`` (a signed int) and ``oi_chg_pct`` is the
  full-precision pct change vs the contract's prior 1-day open interest
  (``None`` when ``oi`` / ``prev_oi`` is missing or ``prev_oi`` is zero).
* **Spot** is the UNDERLYING's spot LTP (its INDEX or CASH 1-minute bars,
  resolved the SAME way the Futures reader resolves it). ``None`` when absent.

## The ``expiry`` (Near/Next/Far) filter (DEC-MOVERS-OPTIONS-EXPIRY-001)

The Options tab's ``?expiry=`` selector keeps ONLY the contracts on the chosen
expiry, resolved PER underlying as-of the snapshot date: ``near`` = the front
(closest) live expiry ``>= date``, ``next`` = the one after, ``far`` = the third
(:func:`_tier_expiry_by_underlying`). So picking a date shows that underlying's
nearest-relevant expiry, and a near-dated vs a far-dated ``24000 CE`` never mix in
one ranking. An underlying without that serial expiry simply drops out (honest —
that month is not listed yet); a blank / unknown selector falls back to ``near``.

## The ``track`` (All / Monthly / Weekly) cadence (DEC-MOVERS-OPTIONS-TRACK-001)

The ``?track=`` selector chooses the cadence the Near/Next/Far tier ranks WITHIN:
``all`` (default — every live expiry, the prior behaviour), ``monthly`` (only each
underlying's last expiry per calendar month — the monthly settlement), or
``weekly`` (the rest). The split is purely STRUCTURAL — ``monthly`` = the
last-in-calendar-month expiry for that underlying (:func:`_monthly_expiries`),
derived from the dates actually in the lake, NEVER from a hardcoded NSE
expiry-weekday rule (Golden Rule #1). That is what makes it handle every
permutation: a single stock or a weekly-less index has one expiry per month (all
``monthly``, an empty ``weekly`` track), an index with weeklies has several per
month (the last is the monthly, the earlier ones the weeklies), and an NSE cadence
change (weekday shift, an index gaining/losing weeklies) just changes which dates
appear — no code change. A blank / unknown selector falls back to ``all``.

## Honesty (Golden Rule #1 / #10)

REFERENCE_ONLY over the **ingested lake** (CLAUDE.md §3) — never a live feed
(Track-1 has none — CLAUDE.md §1). Money is 2dp (``DEC-MONEY-2DP-001``); the
% / OI-% metrics keep FULL precision (a statistical metric, never money-rounded
— CLAUDE.md §4). An empty / options-less lake (or an unknown interval) yields an
honest "pull the F&O options first" payload — never a 500, never a fabricated
mover.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date as date_cls
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from brutex.dashboard.movers_futures import (
    _prev_day_oi,
    _read_future_session,
    _underlying_spot_ltp,
)
from brutex.dashboard.movers_resilient import log_movers_skip, movers_read_skip_errors
from brutex.dashboard.movers_snapshot import (
    _DAILY_MIN_FLOOR,
    _IST,
    _cutoff_minute,
    _prev_day_close,
    interval_minutes,
    parse_snapshot_date,
)
from brutex.data.fno_symbol import EXPIRY_DATE_FORMAT, parse_fno_symbol
from brutex.data.moneyness import Moneyness, derive_strike_step, moneyness_view
from brutex.data.spot_features import pct_change
from brutex.money import format_inr, round_money, rupees_to_paisa

__all__ = [
    "DEFAULT_OPTIONS_INTERVAL",
    "DEFAULT_OPTIONS_TOP_N",
    "OptionMover",
    "OptionsCandidates",
    "build_options_payload",
    "compute_option_mover",
    "compute_options_candidates",
    "normalize_options_selectors",
    "parse_option_symbol",
    "rank_highest_oi",
    "rank_oi_gainers",
    "rank_oi_losers",
    "rank_price_gainers",
    "rank_price_losers",
    "rank_value",
    "rank_volume",
    "run_options_snapshot",
]

#: The Options tab opens on the coarse 1-day timeframe (cheapest, always
#: populated if ANY option was pulled) and a generous depth — mirrors the Spot /
#: Futures tab defaults (CLAUDE.md §11 COMMON keeps the pages aligned).
DEFAULT_OPTIONS_INTERVAL: Final[str] = "1day"
#: 100 default (DEC-MOVERS-COMPLETENESS-001 — operator-directed "minimum 100 always");
#: mirrors the Spot / Futures tab defaults (CLAUDE.md §11 COMMON keeps the pages aligned).
DEFAULT_OPTIONS_TOP_N: Final[int] = 100

#: The FNO segment every option (and futures) contract partition lives under in
#: the lake (``bars/<exch>/FNO/<contract>/<interval>/...``). Mirrors the
#: instrument-master backtesting segment (CLAUDE.md §6).
_FNO_SEGMENT: Final[str] = "FNO"

#: The ``?expiry=`` selector → the 0-based rank of the chosen expiry among an
#: underlying's DISTINCT live (``>= snapshot date``) expiries, sorted ascending:
#: ``near`` = the front (closest) expiry, ``next`` = the one after, ``far`` = the
#: third. An unknown / blank selector falls back to ``near``.
_EXPIRY_TIERS: Final[dict[str, int]] = {"near": 0, "next": 1, "far": 2}

#: The ``?track=`` selector → which expiry CADENCE the Near/Next/Far tier ranks
#: WITHIN (DEC-MOVERS-OPTIONS-TRACK-001): ``all`` (default — every live expiry,
#: the prior behaviour), ``monthly`` (only each underlying's last expiry per
#: calendar month — the monthly settlement), or ``weekly`` (the rest — the
#: non-terminal weeklies). The classification is purely STRUCTURAL — derived from
#: the expiry dates actually present in the lake (:func:`_monthly_expiries`),
#: NEVER from a hardcoded NSE expiry-weekday rule (Golden Rule #1). So a stock or
#: a weekly-less index simply has one expiry per month (all ``monthly``, an empty
#: ``weekly`` track) and an NSE cadence change only changes which dates appear —
#: no code change. An unknown / blank selector falls back to ``all``.
_EXPIRY_TRACKS: Final[frozenset[str]] = frozenset({"all", "weekly", "monthly"})


def parse_option_symbol(symbol: str) -> tuple[str, date_cls, Decimal, str] | None:
    """Parse an OPTION contract groww_symbol into its parts, or ``None``.

    A thin options-only adapter over the canonical
    :func:`brutex.data.fno_symbol.parse_fno_symbol` (CLAUDE.md §11 COMMON — one
    parser, shared with the ``/db`` F&O candle browser): the contract groww_symbol
    is ``EXCH-UNDERLYING-DDMmmYY-STRIKE-CE|PE`` (hyphen-joined), e.g.
    ``"NSE-NIFTY-30Jun25-24000-CE"`` → ``("NIFTY", date(2025, 6, 30),
    Decimal("24000"), "CE")``. Returns ``None`` for any non-option symbol — a
    FUTURE (``-FUT``) is excluded here even though the shared parser decodes it —
    and for any junk / unparseable expiry / non-positive strike, so the caller
    simply skips it (never raising — Golden Rule #1).

    Parameters
    ----------
    symbol
        A candidate contract groww_symbol from the lake's FNO datasets.

    Returns
    -------
    tuple[str, datetime.date, decimal.Decimal, str] | None
        ``(underlying, expiry_date, strike, option_type)`` when *symbol* is a
        well-formed OPTION contract, else ``None``.
    """
    parts = parse_fno_symbol(symbol)
    if parts is None or parts.option_type is None or parts.strike is None:
        return None
    return parts.underlying, parts.expiry, parts.strike, parts.option_type


@dataclass(frozen=True, slots=True)
class OptionMover:
    """One option contract's as-of-snapshot metrics (the locked rules, full precision).

    Attributes
    ----------
    underlying
        The underlying symbol (``NIFTY`` / ``RELIANCE``).
    expiry
        The contract's expiry date.
    strike
        The contract's strike price.
    opt_type
        ``"CE"`` (Call) or ``"PE"`` (Put).
    symbol
        The contract groww_symbol (``NSE-NIFTY-30Jun25-24000-CE``) — the
        deterministic tiebreak key, never shown.
    exchange, segment
        The contract's lake partition coordinates (segment is always ``FNO``);
        used only for the stable ``(symbol, exchange, segment)`` tiebreak.
    ltp
        The contract's last traded price — the selected bar's close, 2dp money.
    spot
        The underlying's spot LTP (2dp money), or ``None`` when the underlying
        spot is absent from the lake.
    pct_change
        ``(ltp − prev_close) / prev_close × 100`` at FULL precision (a statistical
        metric, never money-rounded — CLAUDE.md §4), or ``None`` when there is no
        prior day.
    oi
        The selected bar's open interest, or ``None`` when the bar omits it.
    oi_chg
        ``oi − prev_oi`` (a signed int), or ``None`` when ``oi`` / ``prev_oi`` is
        missing.
    oi_chg_pct
        OI % change vs the contract's prior 1-day open interest at full precision,
        or ``None`` when there is no prior day / a zero-or-missing prior OI / a
        missing current OI.
    volume
        Cumulative fresh-session traded quantity 09:15 → the selected bar.
    value_paisa
        Cumulative traded value over those session bars as int64 paisa
        (``Σ round_money(close) × volume``).
    moneyness
        The contract's moneyness vs the underlying spot as of the snapshot
        (:class:`~brutex.data.moneyness.Moneyness` — ITM/ATM/OTM, or UNKNOWN when
        the spot is absent). DERIVED, never stored (DEC-FNO-MONEYNESS-001).
    atm_offset
        The signed ATM±N strike-steps from spot (``0`` = the at-the-money rung,
        ``+N`` above, ``-N`` below), or ``None`` when the spot / strike grid is
        unknown.
    moneyness_label
        The single trader-facing string the screen shows — bucket + signed depth:
        ``"ITM -1"`` / ``"OTM +10"`` / ``"ATM"`` · ``"ITM"`` / ``"OTM"`` (no grid) ·
        ``"—"`` (a future) · ``"?"`` (spot absent). DEC-FNO-MONEYNESS-001.
    """

    underlying: str
    expiry: date_cls
    strike: Decimal
    opt_type: str
    symbol: str
    exchange: str
    segment: str
    ltp: Decimal
    spot: Decimal | None
    pct_change: Decimal | None
    oi: int | None
    oi_chg: int | None
    oi_chg_pct: Decimal | None
    volume: int
    value_paisa: int
    moneyness: Moneyness
    atm_offset: int | None
    moneyness_label: str

    @property
    def name(self) -> str:
        """The contract's human display name, e.g. ``"NIFTY 30Jun25 24000 CE"``.

        Joins the underlying, the ``DDMmmYY`` expiry token, the strike (rendered
        without a trailing ``.0`` for a whole number), and the option type — the
        markets-screen Options row label.
        """
        expiry_token = self.expiry.strftime(EXPIRY_DATE_FORMAT)
        strike = self.strike.normalize()
        # `normalize()` can yield exponent form (``2.4E+4``) for a whole strike —
        # `quantize` back to an integer when there is no fractional part so the
        # label reads ``24000`` not ``2.4E+4``.
        if strike == strike.to_integral_value():
            strike = strike.quantize(Decimal(1))
        return f"{self.underlying} {expiry_token} {strike} {self.opt_type}"

    @property
    def ltp_inr(self) -> str:
        """The contract's LTP as a 2dp ``₹`` display string."""
        return format_inr(rupees_to_paisa(self.ltp))

    @property
    def spot_inr(self) -> str | None:
        """The underlying spot LTP as a 2dp ``₹`` string, or ``None`` if absent."""
        return format_inr(rupees_to_paisa(self.spot)) if self.spot is not None else None


def _sort_key(mover: OptionMover) -> tuple[str, str, str]:
    """Deterministic tiebreak so equal metrics never reorder run-to-run (Rule #4)."""
    return (mover.symbol, mover.exchange, mover.segment)


def rank_highest_oi(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts by open interest (``oi`` present), descending."""
    pool = [m for m in movers if m.oi is not None]
    pool.sort(key=lambda m: (-(m.oi or 0), _sort_key(m)))
    return pool[:top_n]


def rank_oi_gainers(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts with ``oi_chg_pct > 0``, descending (stable tiebreak)."""
    pool = [m for m in movers if m.oi_chg_pct is not None and m.oi_chg_pct > 0]
    pool.sort(key=lambda m: (-(m.oi_chg_pct or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_oi_losers(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts with ``oi_chg_pct < 0``, ascending (most negative first)."""
    pool = [m for m in movers if m.oi_chg_pct is not None and m.oi_chg_pct < 0]
    pool.sort(key=lambda m: ((m.oi_chg_pct or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_volume(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts by cumulative fresh-session volume, descending."""
    pool = [m for m in movers if m.volume > 0]
    pool.sort(key=lambda m: (-m.volume, _sort_key(m)))
    return pool[:top_n]


def rank_value(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts by cumulative fresh-session traded value, descending."""
    pool = [m for m in movers if m.value_paisa > 0]
    pool.sort(key=lambda m: (-m.value_paisa, _sort_key(m)))
    return pool[:top_n]


def rank_price_gainers(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts with ``pct_change > 0``, descending (stable tiebreak)."""
    pool = [m for m in movers if m.pct_change is not None and m.pct_change > 0]
    pool.sort(key=lambda m: (-(m.pct_change or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


def rank_price_losers(movers: Sequence[OptionMover], top_n: int) -> list[OptionMover]:
    """Top-``n`` contracts with ``pct_change < 0``, ascending (most negative first)."""
    pool = [m for m in movers if m.pct_change is not None and m.pct_change < 0]
    pool.sort(key=lambda m: ((m.pct_change or Decimal(0)), _sort_key(m)))
    return pool[:top_n]


#: Options category key → ranking function (the Options tab's pill set, in the
#: template's order). Mirrors ``movers_futures._FUTURES_CATEGORIES``.
_OPTIONS_CATEGORIES: Final[dict[str, Any]] = {
    "highest_oi": rank_highest_oi,
    "oi_gainers": rank_oi_gainers,
    "oi_losers": rank_oi_losers,
    "volume": rank_volume,
    "value": rank_value,
    "price_gainers": rank_price_gainers,
    "price_losers": rank_price_losers,
}


def compute_option_mover(
    *,
    underlying: str,
    expiry: date_cls,
    strike: Decimal,
    opt_type: str,
    symbol: str,
    exchange: str,
    ltp: Decimal,
    volume: int,
    value_paisa: int,
    oi: int | None,
    prev_close: Decimal | None,
    spot_ltp: Decimal | None,
    prev_oi: int | None,
    strike_step: Decimal | None = None,
) -> OptionMover:
    """Reduce one option contract's reads to an :class:`OptionMover` (pure).

    *ltp* / *volume* / *value_paisa* / *oi* are the contract's session reduction
    (last close, cumulative volume, cumulative traded value, last OI).
    *prev_close* is the contract's prior 1-day close (``None`` ⇒ no % change).
    *spot_ltp* is the underlying's spot LTP (``None`` ⇒ no spot column). *prev_oi*
    is the contract's prior 1-day OI. The money columns are snapped; the % / OI-%
    metrics keep full precision (CLAUDE.md §4).

    Moneyness (ITM/ATM/OTM + signed ATM±N) is DERIVED against *spot_ltp* at the
    snapshot instant (DEC-FNO-MONEYNESS-001) — never stored. *strike_step* is the
    DYNAMIC grid step for this ``(underlying, expiry)`` (derived by the caller from
    the screened strike ladder, so it works for EVERY F&O underlying, not just the
    3 indices); ``None`` ⇒ no ATM±N offset but the ITM/ATM/OTM bucket still resolves.
    A missing spot ⇒ :attr:`~brutex.data.moneyness.Moneyness.UNKNOWN`, never faked.

    Parameters
    ----------
    underlying, expiry, strike, opt_type
        The parsed contract coordinates (the display name's parts).
    symbol, exchange
        The contract groww_symbol + exchange (segment is always ``FNO``).
    ltp, volume, value_paisa, oi
        The session LTP (rupees), cumulative volume, cumulative value (paisa),
        and last OI (or ``None``).
    prev_close
        The contract's previous trading-day 1-day close (rupees), or ``None``.
    spot_ltp
        The underlying's spot LTP (rupees), or ``None`` when absent.
    prev_oi
        The contract's previous trading-day 1-day open interest, or ``None``.
    strike_step
        The ``(underlying, expiry)`` strike-grid step (rupees), or ``None``.

    Returns
    -------
    OptionMover
        The fully-computed row (money 2dp, statistical metrics full-precision).
    """
    snapped_ltp = round_money(ltp)
    snapped_spot = round_money(spot_ltp) if spot_ltp is not None else None
    snapped_prev = round_money(prev_close) if prev_close is not None else None
    pct = pct_change(snapped_ltp, snapped_prev) if snapped_prev is not None else None
    oi_chg = (oi - prev_oi) if (oi is not None and prev_oi is not None) else None
    oi_chg_pct = (
        pct_change(Decimal(oi), Decimal(prev_oi))
        if (oi is not None and prev_oi is not None and prev_oi != 0)
        else None
    )
    # Moneyness vs the snapshot spot (the snapped 2dp spot keeps it consistent with
    # the displayed spot/strike); UNKNOWN when the spot is absent (Golden Rule #1).
    view = moneyness_view(opt_type, strike, snapped_spot, strike_step)
    return OptionMover(
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        opt_type=opt_type,
        symbol=symbol,
        exchange=exchange,
        segment=_FNO_SEGMENT,
        ltp=snapped_ltp,
        spot=snapped_spot,
        pct_change=pct,
        oi=oi,
        oi_chg=oi_chg,
        oi_chg_pct=oi_chg_pct,
        volume=volume,
        value_paisa=value_paisa,
        moneyness=view.moneyness,
        atm_offset=view.atm_offset,
        moneyness_label=view.label,
    )


def _option_row(mover: OptionMover) -> dict[str, Any]:
    """Project one mover to a markets-screen Options row (money 2dp, metrics full-precision)."""
    return {
        "name": mover.name,
        "symbol": mover.symbol,
        "exchange": mover.exchange,
        "segment": mover.segment,
        "spot": mover.spot_inr,
        "ltp": mover.ltp_inr,
        "pct_change": str(mover.pct_change) if mover.pct_change is not None else None,
        "oi": mover.oi,
        "oi_chg": mover.oi_chg,
        "oi_chg_pct": str(mover.oi_chg_pct) if mover.oi_chg_pct is not None else None,
        "volume": mover.volume,
        # The ONE moneyness cell the screen shows — bucket + signed depth as a single
        # string ("ITM -1" / "OTM +10" / "ATM"); DEC-FNO-MONEYNESS-001. The raw bucket
        # + signed ATM±N stay in the payload for sorting / programmatic use.
        "moneyness": mover.moneyness_label,
        "moneyness_bucket": mover.moneyness.value,
        "atm_offset": mover.atm_offset,
    }


def build_options_payload(
    movers: Sequence[OptionMover],
    *,
    interval: str,
    date: str,
    time: str | None,
    top_n: int,
    skipped: int,
    expiry_tier: str = "near",
    expiry_track: str = "all",
) -> dict[str, Any]:
    """Assemble the markets-screen Options ``/api/v1/movers`` JSON from computed movers.

    Builds one ranked list **per options category** (``highest_oi`` /
    ``oi_gainers`` / ``oi_losers`` / ``volume`` / ``value`` / ``price_gainers`` /
    ``price_losers`` — the Options tab's pills), keyed by the category name so the
    page's ``j[category]`` lookup picks the active one. Adds the echo of the
    chosen ``date`` / ``interval`` / ``time`` and an honest ``note`` naming the
    selected ``expiry_tier`` (Near/Next/Far). Every value is JSON-native (money is
    a ``₹`` string, statistical metrics are full-precision strings, OI / volume are
    ints) so Starlette's ``JSONResponse`` never chokes on a ``Decimal``.

    Parameters
    ----------
    movers
        The computed option-contract rows (one per strike × CE/PE × expiry).
    interval, date, time, top_n
        Echoed snapshot coordinates.
    skipped
        Count of contracts dropped — no session bar on the selected day/time,
        OR an unreadable Parquet file (logged as ``movers.parquet_skip``;
        DEC-MOVERS-CORRUPT-SKIP-001) — reported honestly, never hidden
        (Golden Rule #1).
    expiry_tier
        The selected ``near`` / ``next`` / ``far`` expiry tier, echoed in the note.
    expiry_track
        The selected ``all`` / ``monthly`` / ``weekly`` cadence track, echoed in the
        note (appended to the tier label, e.g. ``Near Monthly``) unless ``all``.

    Returns
    -------
    dict[str, Any]
        The Options-tab payload: the seven ranked category lists + the echo +
        ``instrument_count`` / ``skipped_count`` / ``note``.
    """
    ranked: dict[str, list[dict[str, Any]]] = {
        category: [_option_row(m) for m in rank_fn(movers, top_n)]
        for category, rank_fn in _OPTIONS_CATEGORIES.items()
    }
    # The tier label, with the cadence track appended (``Near Monthly``) unless the
    # default ``all`` track (then just ``Near`` — the prior wording is preserved).
    track_suffix = "" if expiry_track == "all" else f" {expiry_track.capitalize()}"
    tier_label = f"{expiry_tier.capitalize()}{track_suffix}"
    instrument_count = len(movers) + skipped
    if instrument_count == 0:
        note = (
            f"No {tier_label}-expiry option contracts in the lake for {date} — pull the F&O "
            "options first on the Coverage page, or pick another expiry/date. "
            "(Reference-only lake view, CLAUDE.md §3.)"
        )
    elif not any(ranked.values()):
        note = (
            f"{instrument_count} {tier_label}-expiry option contract(s) in the lake, but none "
            f"had a bar at {date} {time or '(daily)'} on {interval} — pick another time/day."
        )
    else:
        note = (
            f"As-of {date} {time or '(daily)'} · {interval} · {tier_label} expiry — "
            "reference-only lake view "
            "(CLAUDE.md §3); the contract's expiry nearest the selected date "
            "(Near/Next/Far) on the chosen cadence track (All/Monthly/Weekly); "
            "% is vs the contract's previous close, OI change is vs its prior day."
        )
    return {
        "interval": interval,
        "date": date,
        "time": time,
        "top_n": top_n,
        "instrument_count": instrument_count,
        "skipped_count": skipped,
        "note": note,
        **ranked,
    }


# ────────────────────────────────────────────────────────────────────────
# Live orchestration — reads the lake (raw 1m for OI + value), snapshots
# ────────────────────────────────────────────────────────────────────────


def _session_value_paisa(
    lake_root: Path,
    *,
    exchange: str,
    symbol: str,
    day: date_cls,
    cutoff_min: int | None,
) -> int:
    """Cumulative traded value over *day*'s 1-minute bars, as int64 paisa.

    Reads the contract's ``<lake>/bars/<exchange>/FNO/<symbol>/1minute/**`` raw
    1-minute parquet DIRECTLY (the same path
    :func:`~brutex.dashboard.movers_futures._read_future_session` reads — but it
    returns only ``(ltp, volume, oi)``, NOT the cumulative ``Σ close×volume``,
    and ``movers_futures`` must not be modified). Computes
    ``Σ round_money(close) × volume`` over the kept bars (sliced to
    ``≤ cutoff_min`` for an intraday timeframe with the same Int32-cast minute
    math the Spot reader uses) and returns it as int64 paisa via
    :func:`~brutex.money.rupees_to_paisa` — mirroring
    ``movers_snapshot.MoverSnapshot.value_paisa``. Returns ``0`` when the
    directory is missing or no bar falls on the day / within the cutoff (the
    caller only reaches here for a contract that already produced a session read,
    so a missing-dir path is defensive but kept honest).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    exchange, symbol
        The option contract's partition coordinates (segment is always ``FNO``).
    day
        The IST trading day to total.
    cutoff_min
        Intraday minute-of-day cutoff, or ``None`` for the whole day.

    Returns
    -------
    int
        The cumulative traded value over the kept bars, in int64 paisa.
    """
    import polars as pl

    from brutex.data.lake_day_glob import read_day_intraday_frame

    # Read the day's bars via the symbol-aware reader — see
    # movers_futures._read_future_session: an option CONTRACT symbol is
    # session-filtered unconditionally at ingest, so it takes the fast month-bound
    # path (O(one month) not O(all-bars), DEC-MOVERS-O1-LATENCY-001); the
    # session-exempt fallback exists for consistency but never triggers for a
    # full contract groww_symbol.
    base = lake_root / "bars" / exchange / _FNO_SEGMENT / symbol / "1minute"
    df = read_day_intraday_frame(base, day, symbol=symbol)
    if df is None:
        return 0
    ist = pl.col("timestamp").dt.convert_time_zone(_IST)
    day_df = df.filter(ist.dt.date() == day).sort("timestamp")
    if cutoff_min is not None:
        # Cast to Int32 BEFORE the arithmetic — `dt.hour()`/`dt.minute()` are
        # narrow Int8 (15*60 overflows), so the cast gives the true minute-of-day
        # (mirrors movers_snapshot._read_session_bars — CLAUDE.md §11 COMMON).
        minute_of_day = ist.dt.hour().cast(pl.Int32) * 60 + ist.dt.minute().cast(pl.Int32)
        day_df = day_df.filter(minute_of_day <= cutoff_min)
    if day_df.is_empty():
        return 0
    value_rupees = Decimal(0)
    for close, volume in zip(
        day_df.get_column("close").to_list(), day_df.get_column("volume").to_list(), strict=True
    ):
        value_rupees += round_money(Decimal(str(close))) * int(volume)
    return rupees_to_paisa(value_rupees)


def _discover_options(
    lake_root: Path, *, exchange: str | None = None
) -> list[tuple[str, str, str, date_cls, Decimal, str]]:
    """List the lake's option contracts as ``(symbol, exchange, underlying, expiry, strike, type)``.

    Walks :func:`~brutex.dashboard.db_view.list_candle_datasets`, keeps ONLY the
    ``FNO`` ``1minute`` datasets whose symbol ends ``-CE`` / ``-PE`` (a future
    ends ``-FUT`` and is excluded), parses each into its contract coordinates (a
    non-parsing symbol is skipped), and returns one tuple per contract — every
    strike × CE/PE × expiry. When *exchange* is given only that exchange's
    contracts are kept (an exact per-exchange ranking). Returns ``[]`` when no
    options are present.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    exchange
        Restrict to one exchange (``NSE`` / ``BSE``), or ``None`` for all.

    Returns
    -------
    list[tuple[str, str, str, datetime.date, decimal.Decimal, str]]
        ``(contract_symbol, exchange, underlying, expiry, strike, opt_type)`` per
        option contract found in the lake.
    """
    from brutex.dashboard.db_view import list_candle_datasets

    contracts: list[tuple[str, str, str, date_cls, Decimal, str]] = []
    for dataset in list_candle_datasets(lake_root):
        if dataset["segment"] != _FNO_SEGMENT or dataset["interval"] != "1minute":
            continue
        if exchange is not None and dataset["exchange"] != exchange:
            continue
        symbol = dataset["symbol"]
        parsed = parse_option_symbol(symbol)
        if parsed is None:
            continue
        underlying, expiry, strike, opt_type = parsed
        contracts.append((symbol, dataset["exchange"], underlying, expiry, strike, opt_type))
    return contracts


def _monthly_expiries(expiries: set[date_cls]) -> set[date_cls]:
    """The subset of *expiries* that are a monthly settlement (last-in-calendar-month).

    Groups the distinct expiry dates by ``(year, month)`` and keeps the **max** of
    each group — the LAST expiry in that calendar month, which on the NSE settles
    as the monthly contract. Every other date is a (non-terminal) weekly. This is
    purely structural: it reads the dates actually present in the lake, NEVER a
    hardcoded expiry-weekday rule (Golden Rule #1), so it stays correct under any
    NSE cadence change and yields the right answer for every permutation — a stock
    or weekly-less index has one expiry per month (that one date is its monthly, an
    empty weekly track), an index with weeklies has several per month (the last is
    the monthly, the earlier ones the weeklies). An empty input yields an empty set.

    Parameters
    ----------
    expiries
        The distinct expiry dates to classify (one underlying's live expiries).

    Returns
    -------
    set[datetime.date]
        The dates that are the last expiry of their calendar month.
    """
    by_month: dict[tuple[int, int], list[date_cls]] = {}
    for expiry in expiries:
        by_month.setdefault((expiry.year, expiry.month), []).append(expiry)
    return {max(group) for group in by_month.values()}


def _track_pool(expiries: set[date_cls], track: str) -> set[date_cls]:
    """Keep only *expiries* on the chosen ``track`` (``all`` / ``monthly`` / ``weekly``).

    ``monthly`` keeps the last-in-month settlements (:func:`_monthly_expiries`);
    ``weekly`` keeps the rest (the non-terminal weeklies); ``all`` — and any other
    value — returns the set unchanged (the prior behaviour). The caller
    :func:`run_options_snapshot` already normalises an unknown selector to ``all``,
    so the catch-all return is the honest belt-and-suspenders fallback.
    """
    if track == "monthly":
        return _monthly_expiries(expiries)
    if track == "weekly":
        return expiries - _monthly_expiries(expiries)
    return expiries


def _tier_expiry_by_underlying(
    contracts: Sequence[tuple[str, str, str, date_cls, Decimal, str]],
    *,
    on_date: date_cls,
    tier: str,
    track: str = "all",
) -> dict[str, date_cls]:
    """Map each underlying → its Near/Next/Far expiry on/after *on_date*, within *track*.

    For each underlying the DISTINCT expiries that are ``>= on_date`` are first
    narrowed to the chosen ``track`` (``all`` / ``monthly`` / ``weekly`` via
    :func:`_track_pool`), then sorted ascending; the *tier* (``near`` / ``next`` /
    ``far`` via :data:`_EXPIRY_TIERS`, an unknown / blank tier falling back to
    ``near``) picks the 1st / 2nd / 3rd of THAT track. So ``monthly`` + ``next``
    is "the second monthly from the snapshot date", ``weekly`` + ``near`` is "the
    nearest weekly", etc. An underlying that has fewer live expiries on the track
    than the tier needs is OMITTED — that serial month is simply not listed yet, so
    it contributes nothing (honest, never faked — Golden Rule #1). Already-expired
    contracts (``expiry < on_date``) are excluded.

    Parameters
    ----------
    contracts
        The lake's option contracts as ``(symbol, exchange, underlying, expiry,
        strike, type)`` (the :func:`_discover_options` shape).
    on_date
        The snapshot date the expiry tier is resolved as-of.
    tier
        The ``?expiry=`` selector, lower-cased (``near`` / ``next`` / ``far``).
    track
        The ``?track=`` selector, lower-cased (``all`` / ``monthly`` / ``weekly``;
        an unknown value behaves as ``all`` via :func:`_track_pool`).

    Returns
    -------
    dict[str, datetime.date]
        ``underlying → the tier's expiry on the track`` for every underlying that
        has one.
    """
    idx = _EXPIRY_TIERS.get(tier, 0)
    live: dict[str, set[date_cls]] = {}
    for _symbol, _exchange, underlying, expiry, _strike, _opt in contracts:
        if expiry >= on_date:
            live.setdefault(underlying, set()).add(expiry)
    target: dict[str, date_cls] = {}
    for underlying, expiries in live.items():
        ordered = sorted(_track_pool(expiries, track))
        if idx < len(ordered):
            target[underlying] = ordered[idx]
    return target


def normalize_options_selectors(
    expiry_tier: str | None, expiry_track: str | None
) -> tuple[str, str]:
    """Normalise the raw ``?expiry=`` / ``?track=`` selectors to canonical tokens.

    Lower-cases both; a blank / ``None`` tier ⇒ ``near``; a blank / ``None`` /
    unknown track ⇒ ``all`` (the structural-cadence default). The single source of
    truth for the normalisation so the cache key and the computed candidate set
    agree on the canonical tier/track (CLAUDE.md §11 COMMON) — an unknown track
    such as ``"foo"`` keys identically to ``all``, never a stale split.

    Returns
    -------
    tuple[str, str]
        ``(tier, track)`` — canonical lower-case tokens.
    """
    tier = (expiry_tier or "near").lower()
    track = (expiry_track or "all").lower()
    if track not in _EXPIRY_TRACKS:
        track = "all"
    return tier, track


@dataclass(frozen=True, slots=True)
class OptionsCandidates:
    """The cacheable options candidate set for one snapshot coordinate (pre-ranking).

    The expensive, ``top_n``-independent product of one lake scan — the per-contract
    :class:`OptionMover` list + the skipped count + the normalised snapshot date +
    the canonical expiry tier/track the set was computed for. Caching THIS (keyed by
    ``(exchange, interval, date, time, tier, track)``) makes a sub-filter / ``top_n``
    change an O(1) hit + a cheap re-rank (:func:`build_options_payload`), never a
    re-scan (DEC-MOVERS-O1-LATENCY-001).

    Attributes
    ----------
    movers
        The computed option-contract rows (one per strike × CE/PE on the tier expiry).
    skipped
        Count of contracts dropped (no session bar on the selected day/time,
        or an unreadable Parquet file logged as ``movers.parquet_skip``).
    date
        The normalised ISO ``YYYY-MM-DD`` snapshot day.
    expiry_tier, expiry_track
        The canonical tier / cadence the set was computed for (echoed to the payload).
    """

    movers: tuple[OptionMover, ...]
    skipped: int
    date: str
    expiry_tier: str
    expiry_track: str


def compute_options_candidates(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    expiry_tier: str = "near",
    expiry_track: str = "all",
    exchange: str | None = None,
    names: Mapping[str, str] | None = None,
) -> OptionsCandidates:
    """Scan the lake → the :class:`OptionsCandidates` for one snapshot coordinate.

    The expensive, cacheable core extracted from :func:`run_options_snapshot`:
    discovers EVERY option contract in the lake (every strike × CE/PE × expiry —
    NOT collapsed per underlying; optionally scoped to *exchange*), keeps only the
    contracts on the selected Near/Next/Far expiry within the chosen cadence track,
    reads each contract's selected-day session (raw 1-minute, OI + cumulative value
    preserved) up to *time*, takes the contract's prior 1-day close + OI and its
    underlying spot's LTP, and computes the per-contract :class:`OptionMover`.
    Ranking is the cheap, ``top_n``-dependent step done by
    :func:`build_options_payload`. An empty / options-less lake (or an unknown
    interval) yields an empty :class:`OptionsCandidates` (Golden Rule #1).

    Parameters
    ----------
    lake_root, interval, date, time, expiry_tier, expiry_track, names
        See :func:`run_options_snapshot`.
    exchange
        Restrict the screened contracts to one exchange (``NSE`` / ``BSE``) for an
        exact per-exchange ranking; ``None`` screens every exchange.

    Returns
    -------
    OptionsCandidates
        The contract rows + skipped count + normalised date + canonical tier/track.

    Raises
    ------
    ValueError
        On a malformed *date* / *time* (the route maps it to a 400).
    """
    minutes = interval_minutes(interval)
    day = parse_snapshot_date(date)
    date_str = day.isoformat()
    cutoff_min = None if (minutes is None or minutes >= _DAILY_MIN_FLOOR) else _cutoff_minute(time)
    resolved_names = names or {}
    tier, track = normalize_options_selectors(expiry_tier, expiry_track)
    contracts = _discover_options(lake_root, exchange=exchange)
    # Keep ONLY the contracts on the selected Near/Next/Far expiry (resolved per
    # underlying as-of the snapshot day) WITHIN the chosen cadence track; an
    # underlying without that serial expiry on the track drops out honestly.
    target_expiry = _tier_expiry_by_underlying(contracts, on_date=day, tier=tier, track=track)
    contracts = [c for c in contracts if target_expiry.get(c[2]) == c[3]]
    if minutes is None or not contracts:
        return OptionsCandidates(
            movers=(), skipped=0, date=date_str, expiry_tier=tier, expiry_track=track
        )

    # Derive the DYNAMIC strike-grid step ONCE per (underlying, expiry) from the
    # screened strike ladder (CE + PE share the grid) — the min positive gap. This
    # works for EVERY F&O underlying (all stocks + indices), never a hardcoded NSE
    # rule (Golden Rule #1). O(K log K) once here; the per-contract ATM±N is O(1).
    strikes_by_key: dict[tuple[str, date_cls], list[Decimal]] = {}
    for _sym, _exch, c_underlying, c_expiry, c_strike, _opt in contracts:
        strikes_by_key.setdefault((c_underlying, c_expiry), []).append(c_strike)
    step_by_key = {key: derive_strike_step(strikes) for key, strikes in strikes_by_key.items()}

    movers: list[OptionMover] = []
    skipped = 0
    skip_errors = movers_read_skip_errors()
    for symbol, contract_exchange, underlying, expiry, strike, opt_type in contracts:
        try:
            read = _read_future_session(
                lake_root,
                exchange=contract_exchange,
                segment=_FNO_SEGMENT,
                symbol=symbol,
                day=day,
                cutoff_min=cutoff_min,
            )
            if read is None:  # no session bar on the selected day/time → skip, never faked.
                skipped += 1
                continue
            ltp, volume, oi = read
            value_paisa = _session_value_paisa(
                lake_root, exchange=contract_exchange, symbol=symbol, day=day, cutoff_min=cutoff_min
            )
            prev_close = _prev_day_close(
                lake_root, exchange=contract_exchange, segment=_FNO_SEGMENT, symbol=symbol, day=day
            )
            prev_oi = _prev_day_oi(
                lake_root, exchange=contract_exchange, segment=_FNO_SEGMENT, symbol=symbol, day=day
            )
            # The underlying spot is probed under the PARSED underlying (NIFTY); the
            # row's display name keeps the parsed underlying (the strike/type are in
            # `OptionMover.name`), optionally renamed via `names`.
            spot_ltp = _underlying_spot_ltp(
                lake_root,
                exchange=contract_exchange,
                underlying=underlying,
                day=day,
                cutoff_min=cutoff_min,
            )
        except skip_errors as exc:
            # A corrupt / unreadable Parquet for ONE option must not 500 the screen —
            # skip it (logged + counted), never fabricate (DEC-MOVERS-CORRUPT-SKIP-001).
            log_movers_skip(source="options", symbol=symbol, error=str(exc))
            skipped += 1
            continue
        movers.append(
            compute_option_mover(
                underlying=resolved_names.get(underlying, underlying),
                expiry=expiry,
                strike=strike,
                opt_type=opt_type,
                symbol=symbol,
                exchange=contract_exchange,
                ltp=ltp,
                volume=volume,
                value_paisa=value_paisa,
                oi=oi,
                prev_close=prev_close,
                spot_ltp=spot_ltp,
                prev_oi=prev_oi,
                strike_step=step_by_key.get((underlying, expiry)),
            )
        )

    return OptionsCandidates(
        movers=tuple(movers),
        skipped=skipped,
        date=date_str,
        expiry_tier=tier,
        expiry_track=track,
    )


def run_options_snapshot(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    top_n: int,
    expiry_tier: str = "near",
    expiry_track: str = "all",
    exchange: str | None = None,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compute the options movers snapshot over the lake → the ``/api/v1/movers`` payload.

    The thin compose of :func:`compute_options_candidates` (the expensive, cacheable
    scan) + :func:`build_options_payload` (the cheap, ``top_n``-dependent rank).
    Behaviour is unchanged from the pre-split version.

    The ``expiry_tier`` (``near`` / ``next`` / ``far``) keeps ONLY the contracts on
    the selected expiry — resolved PER underlying as the 1st / 2nd / 3rd distinct
    live expiry on/after the snapshot date (:func:`_tier_expiry_by_underlying`),
    WITHIN the chosen ``expiry_track`` cadence (``all`` / ``monthly`` / ``weekly``)
    — so a near-dated and a far-dated ``24000 CE`` never mix in one ranking, and
    ``monthly`` + ``near`` ranks the nearest monthly settlement only.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    interval, date, time, top_n
        The snapshot coordinates (mirrors :func:`run_movers_snapshot`).
    expiry_tier
        The ``?expiry=`` selector (``near`` / ``next`` / ``far``; blank / unknown ⇒
        ``near``) — which serial expiry, as-of *date*, to rank.
    expiry_track
        The ``?track=`` selector (``all`` / ``monthly`` / ``weekly``; blank /
        unknown ⇒ ``all``) — the cadence the Near/Next/Far tier ranks within. The
        classification is structural (last-in-month = monthly), so it needs no NSE
        weekday rule (Golden Rule #1).
    exchange
        Restrict the screened contracts to one exchange (``NSE`` / ``BSE``); ``None``
        screens every exchange.
    names
        Optional ``underlying → display name`` map; defaults to the underlying
        symbol itself when absent (the contract name keeps the strike + type).

    Returns
    -------
    dict[str, Any]
        The Options-tab ``/api/v1/movers`` payload.

    Raises
    ------
    ValueError
        On a malformed *date* / *time* (the route maps it to a 400).
    """
    candidates = compute_options_candidates(
        lake_root=lake_root,
        interval=interval,
        date=date,
        time=time,
        expiry_tier=expiry_tier,
        expiry_track=expiry_track,
        exchange=exchange,
        names=names,
    )
    return build_options_payload(
        candidates.movers,
        interval=interval,
        date=candidates.date,
        time=time,
        top_n=top_n,
        skipped=candidates.skipped,
        expiry_tier=candidates.expiry_tier,
        expiry_track=candidates.expiry_track,
    )
