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
    from brutex.data.lake_rust_resample import daily_restamp_via_rust, lake_rust_resample_enabled
    from brutex.data.tf_resampler import resample_1m_to_tf

    if df_1m.is_empty():
        return []

    daily = resample_1m_to_tf(df_1m, DAILY_TF_MINUTES)
    if lake_rust_resample_enabled():
        # DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 (the ADR-0020 row-14 port): the
        # daily FILTER + midnight-IST RE-STAMP frame stage routes through the
        # brutex_lake crate (``keep_oi=True`` — the F&O arm carries the
        # resampler's carry-last OI through), with the SHARED spot frame
        # pipeline as the live oracle. The verbatim per-row loop BELOW then
        # materialises the bars from the restamped frame — its own
        # ``astimezone(_IST)`` re-stamp and ``>= before`` filter are
        # IDEMPOTENT over an already-restamped frame (midnight IST maps to
        # the same IST date; every surviving row already passed the filter),
        # so the ``Decimal(str(x))`` repr edges + the stdlib-UTC stamp stay
        # Python-side and attribute-identical on BOTH arms (the row-14 law:
        # output objects stay Python-side).
        daily = daily_restamp_via_rust(
            daily, before, keep_oi=True, oracle=_fno_daily_restamp_oracle
        )
    return _daily_frame_to_fno_bars(daily, before)


def _fno_daily_restamp_oracle(daily: pl.DataFrame, before: date) -> pl.DataFrame:
    """The F&O binding of the ONE shared daily restamp frame pipeline.

    ``spot_daily_resample._daily_restamp_frame_python`` with ``keep_oi=True``
    (CLAUDE.md §11 COMMON — one pipeline, two arms), shaped to the seam's
    positional ``(frame, before)`` oracle signature.
    """
    # Friend import (DEC-P0-017 — deliberate cross-module reuse, never a copy).
    from brutex.data.spot_daily_resample import (
        _daily_restamp_frame_python,  # pyright: ignore[reportPrivateUsage]
    )

    return _daily_restamp_frame_python(daily, before, keep_oi=True)


def _daily_frame_to_fno_bars(daily: pl.DataFrame, before: date) -> list[CandleBar]:
    """The verbatim pre-seam per-row materialisation loop (Slice-0-style).

    Extracted token-for-token from :func:`resample_1m_to_fno_daily` by
    DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 so the public fn can gate the Rust
    frame stage while the OFF (default) path stays byte-identical. The loop
    is RESTAMP- and FILTER-IDEMPOTENT: fed the raw resampled daily frame
    (the OFF arm) it filters + re-stamps exactly as before; fed the
    already-restamped frame (the ON arm) every row passes the filter and
    re-stamps to the same midnight-IST instant — so both arms build
    attribute-identical bars through this ONE retained loop.
    """
    from brutex.data.models import CandleBar
    from brutex.data.tf_resampler import OPEN_INTEREST_COLUMN

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
