"""Day-scoped lake partition resolution — the movers cold-path read bound (DEC-MOVERS-O1-LATENCY-001).

The point-in-time ``/movers`` readers want ONE IST trading day's bars for a
symbol, but the lake stores a symbol's whole history under
``<lake>/bars/<exch>/<seg>/<symbol>/<interval>/<yyyy>/<mm>.parquet`` (one Parquet
file per UTC ``(year, month)`` — :func:`brutex.data.partition.partition_for_timestamp`).
Reading the *entire* partition (``…/**/*.parquet``) just to slice one day is
``O(all-bars)`` per symbol, which on a multi-year 1-minute series is millions of
rows × every screened instrument, every interaction — the lag this module fixes.

## Why a single month file is exactly one IST day's 1-minute bars

The NSE/BSE regular session is **09:15–15:30 IST** (CLAUDE.md §7). IST is a fixed
``UTC+05:30`` (no DST), so the session in UTC is **03:45–10:00** — it NEVER
crosses UTC midnight. Therefore every intraday session bar's UTC calendar date
equals its IST calendar date, and so its UTC ``(year, month)`` partition equals
its IST ``(year, month)``. **All** of an IST trading day's 1-minute bars live in
exactly one file: ``<interval>/<IST_year>/<IST_month>.parquet``. Reading that one
file (then filtering to the day, identical to before) is byte-equivalent to
reading the whole partition and filtering — but ``O(one month of bars)``.

## NOT valid for the derived ``1day`` partition

The derived daily bar is stamped at **midnight IST** (``00:00 IST = 18:30 UTC the
prior day`` — :mod:`brutex.data.spot_daily_resample`), so an IST day's 1-day bar
can sit in the PRIOR UTC month's file (e.g. IST ``2026-07-01`` ⇒ ``2026-06``).
This helper is therefore used ONLY by the **intraday 1-minute** session readers;
the ``1day`` prior-close / daily-snapshot readers keep their whole-partition read
(a daily series is tiny — at most ``years × ~250`` rows — so it is not a hot
path). Mis-applying this to ``1day`` would silently drop month-boundary days, so
the contract is documented here and the callers are 1-minute only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date as date_cls
    from pathlib import Path

    import polars as pl

__all__ = ["day_month_file", "read_day_month_frame"]


def day_month_file(base: Path, day: date_cls) -> Path | None:
    """Return ``<base>/<yyyy>/<mm>.parquet`` for *day*'s month, or ``None`` if absent.

    *base* is an interval partition directory (``…/<symbol>/1minute``). The file
    is the single monthly Parquet holding every bar of *day*'s ``(year, month)``;
    for an intraday 1-minute series that is exactly *day*'s bars (see the module
    docstring — the IST session never crosses a UTC month boundary). Returns
    ``None`` when the file does not exist (a never-pulled month), so the caller
    degrades to "no bars that day" honestly, never raising (Golden Rule #1).

    Parameters
    ----------
    base
        The interval partition directory (``<lake>/bars/<exch>/<seg>/<symbol>/<interval>``).
    day
        The IST trading day whose monthly partition file is wanted.

    Returns
    -------
    pathlib.Path | None
        The monthly Parquet file path when it exists, else ``None``.
    """
    candidate = base / f"{day.year:04d}" / f"{day.month:02d}.parquet"
    return candidate if candidate.is_file() else None


def read_day_month_frame(base: Path, day: date_cls) -> pl.DataFrame | None:
    """Read *day*'s month Parquet under *base* into a polars frame, or ``None``.

    Thin reader over :func:`day_month_file`: when the monthly file exists it is
    read whole (it spans only that one month) and returned; when it is absent
    ``None`` is returned. The caller applies its own IST-day / cutoff filter to
    the returned frame exactly as it did over a whole-partition read — the result
    is identical because the day's bars are entirely within this one file.

    Parameters
    ----------
    base
        The interval partition directory (1-minute series — see the module
        docstring for why this is 1-minute-only).
    day
        The IST trading day to read.

    Returns
    -------
    polars.DataFrame | None
        The month's bars (all columns as stored, incl. ``open_interest``), or
        ``None`` when the month file is absent.
    """
    import polars as pl

    path = day_month_file(base, day)
    if path is None:
        return None
    return pl.read_parquet(path.as_posix())
