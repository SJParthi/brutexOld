"""Derive **F&O 1-day** bars from stored **F&O 1-minute**, OI-correct (DEC-FNO-TF-DERIVE-OI-001).

The F&O twin of :func:`brutex.data.spot_daily_resample.resample_1m_to_daily`.

Why a separate function
-----------------------
:func:`~brutex.data.spot_daily_resample.resample_1m_to_daily` hardcodes
``open_interest=None`` — exactly right for spot / index daily bars (cash
instruments carry no open interest), but WRONG for F&O futures + options,
whose 1-minute bars carry a real ``open_interest`` column. This function is
byte-identical to the spot daily derive EXCEPT it carries the **last-of-day**
open interest instead of ``None``: it reuses the SAME canonical
:func:`~brutex.data.tf_resampler.resample_1m_to_tf` (now OI-aware — it carries
the bucket's last source bar's OI through when the column is present) for the
OHLCV+OI aggregation, the SAME midnight-IST re-stamp (so a derived F&O daily
bar is byte-identical in stamp/schema to a vendor-parsed ``00:00:00 IST`` daily
bar), and the SAME forming-session ``before`` guard (today's incomplete session
is dropped — no look-ahead). The spot module's daily contract is left textually
frozen (CLAUDE.md §11 COMMON — no copy of the bucketing math; only the per-row
OI read differs).

OI convention (operator-confirmed default — DEC-FNO-TF-DERIVE-OI-001)
--------------------------------------------------------------------
The derived daily OI is the **last 1-minute bar of the session's** open
interest (last-of-day SNAPSHOT). This is the standard in-repo convention
(:func:`brutex.feed.resampler._aggregate` + ``movers_futures._read_future_session``
both carry last-of-bucket OI) AND the only OI derivable from 1-minute candles.
True exchange end-of-session **settlement** OI is a SEPARATE vendor field we do
NOT fetch and is OUT OF SCOPE — it is NEVER fabricated here (Golden Rule #1).
HONEST edge: if the LAST 1-minute bar of a session carries a null OI (a vendor
glitch at the close), the derived daily OI is null even though earlier bars
carried real OI — consistent with the carry-last rule, never papered over.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from brutex.data.lake_rust_resample_daily import (
    lake_rust_resample_daily_enabled,
    resample_daily_frame_via_rust,
)

# Reuse the spot module's ONE fixed +05:30 IST offset (CLAUDE.md §11 COMMON — one
# source of truth; do NOT redefine the same constant here). The friend import is the
# established convention (pyproject reportPrivateUsage="warning" + per-site ignore at
# deliberate cross-module reuse sites — DEC-P0-017).
from brutex.data.spot_daily_resample import _IST  # pyright: ignore[reportPrivateUsage]
from brutex.data.timeframes import DAILY_TF_MINUTES

if TYPE_CHECKING:
    from datetime import date

    import polars as pl

    from brutex.data.models import CandleBar

__all__ = ["resample_1m_to_fno_daily", "resample_1m_to_fno_daily_positional"]


def resample_1m_to_fno_daily(df_1m: pl.DataFrame, *, before: date) -> list[CandleBar]:
    """Resample F&O 1-minute bars to **midnight-IST** daily bars, carrying last-of-day OI.

    The F&O analogue of :func:`brutex.data.spot_daily_resample.resample_1m_to_daily`:
    identical OHLCV aggregation, midnight-IST stamp, and forming-session guard, but
    it carries the LAST 1-minute bar of each complete IST session's
    ``open_interest`` (a last-of-day snapshot) instead of hardcoding ``None``. The
    OI itself is computed by the shared OI-aware
    :func:`~brutex.data.tf_resampler.resample_1m_to_tf` (it appends a carry-last
    ``open_interest`` column when the input frame carries one); this function only
    reads that aggregated value per row.

    Parameters
    ----------
    df_1m
        The contract's stored F&O 1-minute bars as a polars frame with the
        resampler's required OHLCV columns (``ts_utc, open, high, low, close,
        volume``) and — for OI to be carried — an ``open_interest`` column, sorted
        ascending by ``ts_utc``. An empty frame yields an empty list. When the
        ``open_interest`` column is ABSENT (a hand-built frame / the 1-minute
        no-op branch), the derived daily OI is honestly ``None`` (defensive read).
    before
        The forming-session cutoff (today IST): any session whose IST date is
        ``>= before`` is DROPPED so a still-forming session is never emitted as a
        final daily bar (no look-ahead, no partial bar).

    Returns
    -------
    list[CandleBar]
        One midnight-IST-stamped daily bar per COMPLETE IST trading day present in
        ``df_1m``, ascending by timestamp, each carrying the last-of-day open
        interest (``None`` when that bar's OI was null / the column was absent).
        Prices stay full-precision ``Decimal`` (the lake-write sink snaps to the
        2-dp tick grid, DEC-SPOT-PRICE-2DP-001).
    """
    # Local import keeps polars out of this module's import graph for callers that
    # only need the dataclass (CLAUDE.md §11 lazy dep, mirrors the spot module).
    from brutex.data.models import CandleBar
    from brutex.data.tf_resampler import OPEN_INTEREST_COLUMN, resample_1m_to_tf

    if df_1m.is_empty():
        return []

    if lake_rust_resample_daily_enabled():
        # The T11 daily seam (DEC-LAKE-RUST-RESAMPLE-DAILY-001): the crate
        # runs aggregate + forming-session drop + midnight-IST restamp as
        # ONE frame call (oi_aware=True — carry-last OI by presence);
        # validation runs FIRST (the seam law) and every degrade serves the
        # verbatim frame composite `_fno_daily_frame_python`. The rows
        # arriving here are ALREADY filtered + restamped, so the
        # materialization below is the OFF arm's loop MINUS those two
        # steps — same `Decimal(str(x))` / `int(oi)` / null-stays-None law
        # (CandleBar-level ON≡OFF equality is parity-corpus-pinned).
        from brutex.data.tf_resampler import validate_resample_frame

        validate_resample_frame(df_1m, DAILY_TF_MINUTES)
        stamped = resample_daily_frame_via_rust(
            df_1m, before, oi_aware=True, oracle=_fno_daily_frame_python
        )
        stamped_has_oi = OPEN_INTEREST_COLUMN in stamped.columns
        rust_bars: list[CandleBar] = []
        for row in stamped.iter_rows(named=True):
            raw_oi = row[OPEN_INTEREST_COLUMN] if stamped_has_oi else None
            rust_bars.append(
                CandleBar(
                    timestamp=row["ts_utc"],
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["volume"]),
                    open_interest=None if raw_oi is None else int(raw_oi),
                )
            )
        return rust_bars

    daily = resample_1m_to_tf(df_1m, DAILY_TF_MINUTES)
    # Defensive: the OHLCV-aware resampler only appends the OI column when the
    # input carried one. A hand-built test frame (or the 1m no-op branch) can omit
    # it, so guard the read — a bare ``row["open_interest"]`` would KeyError.
    has_oi = OPEN_INTEREST_COLUMN in daily.columns

    bars: list[CandleBar] = []
    for row in daily.iter_rows(named=True):
        ist_date = row["ts_utc"].astimezone(_IST).date()
        if ist_date >= before:
            # Still-forming / incomplete session — never written as a final bar.
            continue
        # Re-stamp to midnight IST (== 18:30 UTC of the prior calendar day) so the
        # UTC instant is byte-identical to a vendor-parsed "00:00:00 IST" daily bar.
        stamp_utc = datetime(ist_date.year, ist_date.month, ist_date.day, tzinfo=_IST).astimezone(
            UTC
        )
        raw_oi = row[OPEN_INTEREST_COLUMN] if has_oi else None
        bars.append(
            CandleBar(
                timestamp=stamp_utc,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                open_interest=None if raw_oi is None else int(raw_oi),
            )
        )
    return bars


def _fno_daily_frame_python(df_1m: pl.DataFrame, before: date) -> pl.DataFrame:
    """The F&O daily FRAME composite — the Rust daily seam's live parity oracle.

    DEC-LAKE-RUST-RESAMPLE-DAILY-001: the frame-level twin of the per-row
    OFF-arm body — 375-minute aggregation (the shared
    :func:`~brutex.data.tf_resampler.resample_1m_to_tf`) → drop sessions
    ``>= before`` → re-stamp to midnight IST (the spot module's vectorized
    law: ``(_ist_date.cast(Datetime("us")).replace_time_zone("UTC")) −
    offset``, so the output ``ts_utc`` is always ``Datetime(us, UTC)``) →
    select ``ts_utc`` + OHLCV + ``open_interest`` when present (the ten
    Greeks-overlay columns are DROPPED — the daily materializer never read
    them; the crate face mirrors this select). Served on every seam
    degrade AND compared against the crate frame by the burn-in; naive
    input timestamps are treated as UTC (the lake convention — the same
    fixed-offset arithmetic the spot vectorized body uses).
    """
    import polars as pl

    from brutex.data.tf_resampler import OPEN_INTEREST_COLUMN, resample_1m_to_tf

    daily = resample_1m_to_tf(df_1m, DAILY_TF_MINUTES)
    ist_offset = _IST.utcoffset(None)  # timedelta(+05:30) — the ONE IST definition
    columns = ["ts_utc", "open", "high", "low", "close", "volume"]
    if OPEN_INTEREST_COLUMN in daily.columns:
        columns.append(OPEN_INTEREST_COLUMN)
    frame: pl.DataFrame = (
        daily.with_columns((pl.col("ts_utc") + ist_offset).dt.date().alias("_ist_date"))
        .filter(pl.col("_ist_date") < before)
        .with_columns(
            (
                pl.col("_ist_date").cast(pl.Datetime("us")).dt.replace_time_zone("UTC") - ist_offset
            ).alias("ts_utc")
        )
        .select(*columns)
    )
    return frame


def resample_1m_to_fno_daily_positional(df_1m: pl.DataFrame, before: date) -> list[CandleBar]:
    """Positional ``(frame, before)`` adapter over :func:`resample_1m_to_fno_daily`.

    The F&O binding for the
    :func:`brutex.data.spot_daily_resample.fill_daily_from_minute` ``resample_fn``
    seam, whose uniform signature is positional ``(frame, before) -> list[CandleBar]``
    (mirrors :func:`brutex.data.spot_daily_resample.resample_1m_to_daily_default`).
    Injecting THIS makes the SAME shared daily driver carry last-of-day OI for F&O
    (DEC-FNO-TF-DERIVE-OI-001).
    """
    return resample_1m_to_fno_daily(df_1m, before=before)
