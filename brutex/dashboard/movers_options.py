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

## Deferred — the ``expiry`` (Near/Next/Far) filter (honest scope, Golden Rule #10)

The ``/movers`` Options tab also exposes a ``?expiry=`` Near/Next/Far selector.
This milestone **IGNORES it** — it ranks ALL option contracts in the lake (every
strike × CE/PE × expiry) and the payload ``note`` says so honestly. Filtering by
expiry tier is a follow-up (a separate decision; not authorized here).

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
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from brutex.dashboard.movers_futures import (
    _prev_day_oi,
    _read_future_session,
    _underlying_spot_ltp,
)
from brutex.dashboard.movers_snapshot import (
    _DAILY_MIN_FLOOR,
    _IST,
    _cutoff_minute,
    _prev_day_close,
    interval_minutes,
    parse_snapshot_date,
)
from brutex.data.spot_features import pct_change
from brutex.money import format_inr, round_money, rupees_to_paisa

__all__ = [
    "DEFAULT_OPTIONS_INTERVAL",
    "DEFAULT_OPTIONS_TOP_N",
    "OptionMover",
    "build_options_payload",
    "compute_option_mover",
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
DEFAULT_OPTIONS_TOP_N: Final[int] = 50

#: The FNO segment every option (and futures) contract partition lives under in
#: the lake (``bars/<exch>/FNO/<contract>/<interval>/...``). Mirrors the
#: instrument-master backtesting segment (CLAUDE.md §6).
_FNO_SEGMENT: Final[str] = "FNO"

#: The two option-type suffixes — Call / Put. A contract groww_symbol ends with
#: one of these (``EXCH-UNDERLYING-DDMmmYY-STRIKE-CE|PE``); a FUT ends ``-FUT``
#: and is excluded from the options universe.
_OPTION_TYPES: Final[frozenset[str]] = frozenset({"CE", "PE"})

#: The minimum hyphen-split arity of an option contract groww_symbol
#: (``EXCH-UNDERLYING-DDMmmYY-STRIKE-CE|PE`` → 5 parts; a vendor symbol uses the
#: hyphen only as the field separator).
_MIN_OPTION_PARTS: Final[int] = 5

#: The vendor expiry-token format inside the contract symbol (``30Jun25``): a
#: 2-digit day, a title-case 3-letter month, a 2-digit year. The single parse
#: spec (CLAUDE.md §11 COMMON — re-derived locally rather than importing a
#: private from the Futures reader).
_EXPIRY_FORMAT: Final[str] = "%d%b%y"


def parse_option_symbol(symbol: str) -> tuple[str, date_cls, Decimal, str] | None:
    """Parse an option contract groww_symbol into its parts, or ``None``.

    The contract groww_symbol is ``EXCH-UNDERLYING-DDMmmYY-STRIKE-CE|PE``
    (hyphen-joined), e.g. ``"NSE-NIFTY-30Jun25-24000-CE"`` →
    ``("NIFTY", date(2025, 6, 30), Decimal("24000"), "CE")``. Returns ``None``
    for any non-option symbol — a future ends ``-FUT`` so its last part is not
    ``CE`` / ``PE`` — and for any junk / unparseable expiry / non-numeric strike,
    so the caller simply skips it (never raising — Golden Rule #1).

    Parameters
    ----------
    symbol
        A candidate contract groww_symbol from the lake's FNO datasets.

    Returns
    -------
    tuple[str, datetime.date, decimal.Decimal, str] | None
        ``(underlying, expiry_date, strike, option_type)`` when *symbol* is a
        well-formed option contract, else ``None``.
    """
    parts = symbol.split("-")
    # Need at least EXCH, UNDERLYING, expiry, STRIKE, CE|PE — and the last part
    # must be an option type (a future's last part is FUT, so it is excluded).
    if len(parts) < _MIN_OPTION_PARTS or parts[-1] not in _OPTION_TYPES:
        return None
    underlying = parts[1]
    opt_type = parts[-1]
    expiry = _parse_expiry_token(parts[-3])
    strike = _parse_strike(parts[-2])
    if not underlying or expiry is None or strike is None:
        return None
    return underlying, expiry, strike, opt_type


def _parse_expiry_token(token: str) -> date_cls | None:
    """Parse a ``DDMmmYY`` expiry token (``30Jun25``) into a date, or ``None``.

    Uses :meth:`datetime.datetime.strptime` with the vendor :data:`_EXPIRY_FORMAT`
    (``%d%b%y``) — any other shape, an unknown month, or an out-of-range day
    raises :class:`ValueError`, caught and reported as ``None`` so the caller
    skips the malformed contract rather than raising (Golden Rule #1).
    """
    try:
        return datetime.strptime(token, _EXPIRY_FORMAT).date()
    except ValueError:
        return None


def _parse_strike(token: str) -> Decimal | None:
    """Parse a strike token (``24000`` / ``24000.5``) into a Decimal, or ``None``.

    A non-numeric token yields :class:`decimal.InvalidOperation`, caught and
    reported as ``None`` so the caller skips the malformed contract.
    """
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


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

    @property
    def name(self) -> str:
        """The contract's human display name, e.g. ``"NIFTY 30Jun25 24000 CE"``.

        Joins the underlying, the ``DDMmmYY`` expiry token, the strike (rendered
        without a trailing ``.0`` for a whole number), and the option type — the
        markets-screen Options row label.
        """
        expiry_token = self.expiry.strftime(_EXPIRY_FORMAT)
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
) -> OptionMover:
    """Reduce one option contract's reads to an :class:`OptionMover` (pure).

    *ltp* / *volume* / *value_paisa* / *oi* are the contract's session reduction
    (last close, cumulative volume, cumulative traded value, last OI).
    *prev_close* is the contract's prior 1-day close (``None`` ⇒ no % change).
    *spot_ltp* is the underlying's spot LTP (``None`` ⇒ no spot column). *prev_oi*
    is the contract's prior 1-day OI. The money columns are snapped; the % / OI-%
    metrics keep full precision (CLAUDE.md §4).

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
    }


def build_options_payload(
    movers: Sequence[OptionMover],
    *,
    interval: str,
    date: str,
    time: str | None,
    top_n: int,
    skipped: int,
) -> dict[str, Any]:
    """Assemble the markets-screen Options ``/api/v1/movers`` JSON from computed movers.

    Builds one ranked list **per options category** (``highest_oi`` /
    ``oi_gainers`` / ``oi_losers`` / ``volume`` / ``value`` / ``price_gainers`` /
    ``price_losers`` — the Options tab's pills), keyed by the category name so the
    page's ``j[category]`` lookup picks the active one. Adds the echo of the
    chosen ``date`` / ``interval`` / ``time`` and an honest ``note`` (which states
    that the ``expiry`` Near/Next/Far filter is deferred — ALL contracts are
    ranked). Every value is JSON-native (money is a ``₹`` string, statistical
    metrics are full-precision strings, OI / volume are ints) so Starlette's
    ``JSONResponse`` never chokes on a ``Decimal``.

    Parameters
    ----------
    movers
        The computed option-contract rows (one per strike × CE/PE × expiry).
    interval, date, time, top_n
        Echoed snapshot coordinates.
    skipped
        Count of contracts dropped (no session bar on the selected day/time) —
        reported honestly, never hidden (Golden Rule #1).

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
    instrument_count = len(movers) + skipped
    if instrument_count == 0:
        note = (
            "No options in the lake yet — pull the F&O options first on the Coverage "
            "page, then refresh. (Reference-only lake view, CLAUDE.md §3.)"
        )
    elif not any(ranked.values()):
        note = (
            f"{instrument_count} option contract(s) in the lake, but none had a bar at "
            f"{date} {time or '(daily)'} on {interval} — pick another time/day, or pull more."
        )
    else:
        note = (
            f"As-of {date} {time or '(daily)'} · {interval} — reference-only lake view "
            "(CLAUDE.md §3); ALL expiries ranked (the Near/Next/Far filter is deferred); "
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

    base = lake_root / "bars" / exchange / _FNO_SEGMENT / symbol / "1minute"
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return 0
    df = pl.read_parquet(base.as_posix() + "/**/*.parquet")
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


def _discover_options(lake_root: Path) -> list[tuple[str, str, str, date_cls, Decimal, str]]:
    """List the lake's option contracts as ``(symbol, exchange, underlying, expiry, strike, type)``.

    Walks :func:`~brutex.dashboard.db_view.list_candle_datasets`, keeps ONLY the
    ``FNO`` ``1minute`` datasets whose symbol ends ``-CE`` / ``-PE`` (a future
    ends ``-FUT`` and is excluded), parses each into its contract coordinates (a
    non-parsing symbol is skipped), and returns one tuple per contract — every
    strike × CE/PE × expiry. Returns ``[]`` when no options are present.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).

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
        symbol = dataset["symbol"]
        parsed = parse_option_symbol(symbol)
        if parsed is None:
            continue
        underlying, expiry, strike, opt_type = parsed
        contracts.append((symbol, dataset["exchange"], underlying, expiry, strike, opt_type))
    return contracts


def run_options_snapshot(
    *,
    lake_root: Path,
    interval: str,
    date: str | None,
    time: str | None,
    top_n: int,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compute the options movers snapshot over the lake → the ``/api/v1/movers`` payload.

    Discovers EVERY option contract in the lake (every strike × CE/PE × expiry —
    NOT collapsed per underlying; an options screen lists individual contracts),
    reads each contract's selected-day session (raw 1-minute, OI + cumulative
    value preserved) up to *time*, takes the contract's prior 1-day close + OI and
    its underlying spot's LTP, computes the per-contract :class:`OptionMover`, and
    ranks them into the per-category payload. An empty / options-less lake (or an
    unknown interval) yields an honest "pull the F&O options first" payload —
    never a 500, never a fabricated mover (Golden Rule #1).

    The ``expiry`` (Near/Next/Far) filter is DEFERRED (see the module docstring) —
    ALL expiries are ranked and the payload note says so.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    interval, date, time, top_n
        The snapshot coordinates (mirrors :func:`run_movers_snapshot`).
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
    minutes = interval_minutes(interval)
    day = parse_snapshot_date(date)
    date_str = day.isoformat()
    cutoff_min = None if (minutes is None or minutes >= _DAILY_MIN_FLOOR) else _cutoff_minute(time)
    resolved_names = names or {}
    contracts = _discover_options(lake_root)
    if minutes is None or not contracts:
        return build_options_payload(
            [], interval=interval, date=date_str, time=time, top_n=top_n, skipped=0
        )

    movers: list[OptionMover] = []
    skipped = 0
    for symbol, exchange, underlying, expiry, strike, opt_type in contracts:
        read = _read_future_session(
            lake_root,
            exchange=exchange,
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
            lake_root, exchange=exchange, symbol=symbol, day=day, cutoff_min=cutoff_min
        )
        prev_close = _prev_day_close(
            lake_root, exchange=exchange, segment=_FNO_SEGMENT, symbol=symbol, day=day
        )
        prev_oi = _prev_day_oi(
            lake_root, exchange=exchange, segment=_FNO_SEGMENT, symbol=symbol, day=day
        )
        # The underlying spot is probed under the PARSED underlying (NIFTY); the
        # row's display name keeps the parsed underlying (the strike/type are in
        # `OptionMover.name`), optionally renamed via `names`.
        spot_ltp = _underlying_spot_ltp(
            lake_root, exchange=exchange, underlying=underlying, day=day, cutoff_min=cutoff_min
        )
        movers.append(
            compute_option_mover(
                underlying=resolved_names.get(underlying, underlying),
                expiry=expiry,
                strike=strike,
                opt_type=opt_type,
                symbol=symbol,
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

    return build_options_payload(
        movers, interval=interval, date=date_str, time=time, top_n=top_n, skipped=skipped
    )
