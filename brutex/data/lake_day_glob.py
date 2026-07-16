"""Day-scoped lake partition resolution — the movers cold-path read bound (DEC-MOVERS-O1-LATENCY-001).

The point-in-time ``/movers`` readers want ONE IST trading day's bars for a
symbol, but the lake stores a symbol's whole history under
``<lake>/bars/<exch>/<seg>/<symbol>/<interval>/<yyyy>/<mm>.parquet`` (one Parquet
file per UTC ``(year, month)`` — :func:`brutex.data.partition.partition_for_timestamp`).
Reading the *entire* partition (``…/**/*.parquet``) just to slice one day is
``O(all-bars)`` per symbol, which on a multi-year 1-minute series is millions of
rows × every screened instrument, every interaction — the lag this module fixes.

## Why a single month file is exactly one IST day's 1-minute bars (SESSION-FILTERED symbols)

The NSE/BSE regular session is **09:15–15:30 IST** (CLAUDE.md §7). IST is a fixed
``UTC+05:30`` (no DST), so the session in UTC is **03:45–10:00** — it NEVER
crosses UTC midnight. Therefore every intraday session bar's UTC calendar date
equals its IST calendar date, and so its UTC ``(year, month)`` partition equals
its IST ``(year, month)``. **All** of a SESSION-FILTERED IST trading day's
1-minute bars live in exactly one file: ``<interval>/<IST_year>/<IST_month>.parquet``.
Reading that one file (then filtering to the day, identical to before) is
byte-equivalent to reading the whole partition and filtering — but
``O(one month of bars)``.

## SESSION-EXEMPT symbols (GIFT Nifty) — whole-partition fallback

A handful of symbols are **exempt** from the 09:15–15:30 IST session filter and
keep their full-day intraday series verbatim — :data:`brutex.data.session.SESSION_EXEMPT_SYMBOLS`
= ``{"GIFTNIFTY"}`` (GIFT Nifty trades ~21h on NSE IX,
``DEC-DHAN-GIFTNIFTY-SESSION-001``). For such a symbol an IST day's bars CAN cross
a UTC midnight (e.g. an IST ``02:00`` bar is ``18:30–03:45 UTC of the prior IST
day``), so on the **1st of an IST month** they land in the PRIOR UTC month's file
— the single-month read would drop them. :func:`read_day_intraday_frame` therefore
falls back to the **whole partition** for session-exempt symbols (the old
``O(all-bars)`` read, restoring exact byte-equivalence); GIFT Nifty is rare /
not-yet-admitted, so the slow path is never on the hot ``NIFTY``/``BANKNIFTY``/
``SENSEX`` set. The bare ``read_day_month_frame`` makes NO exemption check — only
the symbol-aware :func:`read_day_intraday_frame` does, and the callers go through it.

## NOT valid for the derived ``1day`` partition

The derived daily bar is stamped at **midnight IST** (``00:00 IST = 18:30 UTC the
prior day`` — :mod:`brutex.data.spot_daily_resample`), so an IST day's 1-day bar
can sit in the PRIOR UTC month's file (e.g. IST ``2026-07-01`` ⇒ ``2026-06``).
This helper is therefore used ONLY by the **intraday** session readers — the
1-minute base AND the prebuilt derived intraday-TF partitions
(``2minute`` … ``4hour``, whose bucket labels share the bar's own IST date for
a session-filtered symbol by the fixed-arithmetic session anchor,
DEC-RESAMPLE-ANCHOR-002 — but NOT always the bar's UTC **month**: a label
before 05:30 IST maps to the prior UTC day, so on the 1st of an IST month a
derived-TF bucket — e.g. a stray 09:00 pre-open bar's 05:15-IST 4hour label —
is FILED under the prior UTC month's file (DEC-MOVERS-STORED-FRESHNESS-001
REFUTER ROUND 2 defect B2; the earlier "the same one-month-file argument
holds" wording here was falsified by that executed shape). The stored-TF
consumer (:func:`brutex.dashboard.movers_stored_read.read_stored_day_frame` —
DEC-PIPELINE-AUTOPREBUILD-001 chunk 2's stored-first movers read) therefore
ALSO probes the prior month's file exactly on that month-edge; the 1-minute
base itself is unaffected — a session-filtered symbol's bar TIMES
(~09:00–19:15 IST) never precede 05:30 IST, so the one-month-file argument
stays exact for 1-minute). The ``1day`` prior-close / daily-snapshot
readers keep their whole-partition read (a daily series is tiny — at most
``years × ~250`` rows — so it is not a hot path). Mis-applying this to ``1day``
would silently drop month-boundary days, so the contract is documented here and
the callers are intraday-interval only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brutex.data.session import is_session_exempt

if TYPE_CHECKING:
    from datetime import date as date_cls
    from pathlib import Path

    import polars as pl

__all__ = ["day_month_file", "read_day_intraday_frame", "read_day_month_frame"]


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
    from brutex.data.guarded_read import read_parquet_regular
    from brutex.data.lake_read import dedup_bars_frame

    path = day_month_file(base, day)
    if path is None:
        return None
    # Read-side dedup gate (DEC-LAKE-READ-UNIQUE-001): every /movers session/day
    # reader flows through here — a poisoned-era duplicated month file would
    # otherwise double-count session volume/turnover. Keep-FIRST, warned.
    # DEC-LAKE-READ-INODE-GUARD-001 defense (a): fd-validated literal read —
    # a FIFO/device planted at the month file folds into the callers'
    # existing movers skip arms (NotRegularFileError is an OSError) instead
    # of hanging the /movers route.
    frame, _dropped = dedup_bars_frame(
        read_parquet_regular(path), source=f"lake_day:{path.as_posix()}"
    )
    return frame


def read_day_intraday_frame(base: Path, day: date_cls, *, symbol: str) -> pl.DataFrame | None:
    """Read *day*'s 1-minute bars for *symbol*, month-bounded OR whole-partition.

    The symbol-aware entry point every intraday ``/movers`` session reader uses
    (DEC-MOVERS-O1-LATENCY-001). For a **session-filtered** symbol (the norm) the
    day's bars are entirely within ``<base>/<yyyy>/<mm>.parquet`` (the IST session
    never crosses a UTC month boundary), so ONLY that month file is read —
    ``O(one month)`` (:func:`read_day_month_frame`). For a **session-exempt**
    symbol (:func:`brutex.data.session.is_session_exempt` — GIFT Nifty, ~21h
    trading) the day's bars can spill into the PRIOR UTC month's file, so the
    WHOLE partition is read to stay byte-equivalent with the pre-optimisation
    read (see the module docstring). ``None`` when there are no bars (missing
    month file / empty partition). The caller still filters to the exact IST day,
    so a whole-partition read returns the same rows it always did.

    Parameters
    ----------
    base
        The 1-minute interval partition directory
        (``<lake>/bars/<exch>/<seg>/<symbol>/1minute``).
    day
        The IST trading day to read.
    symbol
        The lake symbol — its session-exemption decides month-bound vs
        whole-partition (a session-exempt symbol takes the whole-partition path).

    Returns
    -------
    polars.DataFrame | None
        The day's candidate bars (all columns as stored), or ``None`` when none.
    """
    if is_session_exempt(symbol):
        # GIFT Nifty (and any future exempt symbol): its full-day series can cross
        # a UTC month boundary, so read the whole partition (the old O(all-bars)
        # path) to stay byte-equivalent — the caller's IST-day filter is unchanged.
        from brutex.data.guarded_read import read_parquet_glob_regular
        from brutex.data.lake_read import dedup_bars_frame

        if not (base.is_dir() and any(base.rglob("*.parquet"))):
            return None
        # SAME keep-FIRST read gate as the month-bounded path
        # (DEC-LAKE-READ-UNIQUE-001) — warned, never silent; the glob read is
        # per-file fd-validated (DEC-LAKE-READ-INODE-GUARD-001 defense (a)).
        frame, _dropped = dedup_bars_frame(
            read_parquet_glob_regular(base.as_posix() + "/**/*.parquet"),
            source=f"lake_day:{base.as_posix()}",
        )
        return frame
    return read_day_month_frame(base, day)
