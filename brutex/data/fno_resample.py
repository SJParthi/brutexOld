"""Track-1 F&O derived-timeframe resampler (DEC-FNO-TF-DERIVE-OI-001).

The F&O twin of :mod:`brutex.data.spot_resample`. The lake holds **21
timeframes** per F&O contract (1m base + 19 derived intraday + 1d), but — exactly
like spot — only **1-minute** F&O data is fetched from Groww; every other
timeframe is DERIVED from the stored 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001).
The F&O lake is REFERENCE_ONLY (CLAUDE.md §3); this derivation never touches the
brute-force engine (NIFTY/BANKNIFTY/SENSEX).

:func:`derive_fno_intraday_tfs` is that derivation for the 19 intraday TFs: it
resamples a 1-minute F&O :class:`polars.DataFrame` via the canonical OI-aware
:func:`~brutex.data.tf_resampler.resample_all_21_tfs`, then drops the two that
are NOT derived intraday — the ``1m`` base (the source) and the ``1d`` daily
(derived by :func:`brutex.data.fno_daily_resample.resample_1m_to_fno_daily` with
the midnight-IST re-stamp). When the input frame carries an ``open_interest``
column (it does for stored F&O 1-minute), EVERY returned frame carries the
bucket's carry-last OI (snapshot semantics — never summed). Output is keyed by
the canonical TF label (:data:`~brutex.data.timeframes.TF_LABELS`).

Pure (no I/O): a sorted 1m :class:`polars.DataFrame` in, a ``{label: df}`` mapping
out — so it is 100% unit-testable and shares the SAME bucketing source of truth as
spot (CLAUDE.md §11 COMMON — no second resampler).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brutex.data.lake_rust_resample import (
    derive_frames_via_rust,
    lake_rust_resample_enabled,
    session_cutoff_via_rust,
)

# Reuse the spot module's ONE 1-minute base-TF constant + derived-TF tuple
# (CLAUDE.md §11 COMMON — one source of truth; do NOT redefine them here). The
# friend import is the established convention (pyproject
# reportPrivateUsage="warning" + per-site ignore at deliberate cross-module
# reuse sites — DEC-P0-017).
from brutex.data.spot_resample import (
    _BASE_TF_MINUTES,  # pyright: ignore[reportPrivateUsage]
    DERIVED_INTRADAY_TF_MINUTES,
)
from brutex.data.tf_resampler import resample_1m_to_tf, resample_all_21_tfs
from brutex.data.timeframes import DAILY_TF_MINUTES, TF_LABELS

if TYPE_CHECKING:
    import polars as pl

#: The lake stores its 1-minute timestamp column as ``timestamp``; the canonical
#: resampler REQUIRES ``ts_utc`` (its ``REQUIRED_COLUMNS`` / ``_validate_input``
#: raise on absence). The on-the-fly read path renames before resampling.
_LAKE_TS_COLUMN: str = "timestamp"
_RESAMPLER_TS_COLUMN: str = "ts_utc"


def derive_fno_intraday_tfs(df_1m: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Resample a 1m F&O DataFrame to the 19 derived intraday timeframes, OI-aware.

    Parameters
    ----------
    df_1m
        A 1-minute F&O :class:`polars.DataFrame` (columns
        ``ts_utc, open, high, low, close, volume`` and, for OI to be carried,
        ``open_interest``; ``ts_utc`` Datetime, sorted ascending) — validated by
        the underlying resampler.

    Returns
    -------
    dict[str, polars.DataFrame]
        ``{TF label → resampled DataFrame}`` for the **19 derived intraday**
        timeframes only — the ``1m`` base (source) and the ``1d`` daily (derived
        with the midnight-IST stamp by :mod:`brutex.data.fno_daily_resample`) are
        excluded. With an ``open_interest`` column present in ``df_1m`` every
        returned frame carries the bucket's carry-last OI.

    When ``BRUTEX_LAKE_RUST_RESAMPLE`` is set truthy
    (DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001, the ADR-0020 row-13 fold of this
    wrapper into the crate's entry points), the 19-TF derive routes through
    the ``brutex_lake`` batch face exactly like the spot twin (OI + Greeks
    overlay carried by PRESENCE — the crate's ``oi_aware`` law); flag OFF
    (the default) is byte-identical to today.
    """
    if lake_rust_resample_enabled():
        return _derive_fno_intraday_tfs_rust(df_1m)
    return _derive_fno_intraday_tfs_python(df_1m)


def _derive_fno_intraday_tfs_python(df_1m: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """The verbatim pre-seam derive body (the Slice-0-style decomposition).

    Extracted token-for-token from :func:`derive_fno_intraday_tfs` by
    DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 so the public fn can gate the Rust
    seam while the OFF (default) path stays byte-identical.
    """
    all_tfs = resample_all_21_tfs(df_1m)
    return {
        TF_LABELS[minutes]: frame
        for minutes, frame in all_tfs.items()
        if minutes not in (_BASE_TF_MINUTES, DAILY_TF_MINUTES)
    }


def _derive_fno_intraday_tfs_rust(df_1m: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """The flag-ON arm: ONE batch crossing over the 19 derived intraday TFs.

    The exact F&O twin of ``spot_resample._derive_intraday_tfs_rust`` —
    VALIDATION STAYS PYTHON (the same gate the Python arm hits first), and
    every degrade path serves the verbatim per-TF Python oracle. OI and the
    Greeks-overlay columns ride the crate's carry-last PRESENCE law, so an
    OI-less frame's output stays the OHLCV-6 verbatim on both arms.
    """
    from brutex.data.tf_resampler import (
        _resample_1m_to_tf_python,  # pyright: ignore[reportPrivateUsage]
        _validate_input,  # pyright: ignore[reportPrivateUsage]
    )

    _validate_input(df_1m)
    frames = derive_frames_via_rust(
        df_1m, DERIVED_INTRADAY_TF_MINUTES, oracle=_resample_1m_to_tf_python
    )
    return {TF_LABELS[minutes]: frames[minutes] for minutes in DERIVED_INTRADAY_TF_MINUTES}


def resample_session_to_tf(
    day_df: pl.DataFrame,
    target_tf_minutes: int,
    *,
    cutoff_min: int | None = None,
) -> pl.DataFrame:
    """Resample ONE IST session's lake 1-minute frame to *target_tf_minutes*, OI-aware.

    The on-the-fly intraday read path for ``/movers`` (and any per-TF F&O resample):
    the lake reader (:func:`brutex.data.lake_day_glob.read_day_intraday_frame`) returns
    a frame whose timestamp column is named ``timestamp``, but the canonical
    :func:`~brutex.data.tf_resampler.resample_1m_to_tf` REQUIRES ``ts_utc`` — so this
    helper RENAMES ``timestamp -> ts_utc`` before resampling (corrected_point #2;
    mirrors :func:`brutex.data.spot_daily_resample.make_lake_read_1m_window`). The
    resample carries last-of-bucket OI when the frame has an ``open_interest`` column.

    Trailing partial-bucket guard (corrected_point #1a — no look-ahead / no partial
    bar): when *cutoff_min* lands mid-bucket (e.g. cutoff 09:17 over a 5m grid yields a
    ``[09:15, 09:20)`` bucket built from only 3 of 5 minutes), the FORMING trailing
    bucket whose ``bucket_start + M`` exceeds the cutoff is DROPPED, so only fully-closed
    buckets up to the cutoff are returned. ``cutoff_min=None`` keeps every bucket (the
    whole day — used for a daily/whole-session read). The caller must pass a frame already
    filtered to the one IST day.

    Parameters
    ----------
    day_df
        ONE IST trading day's lake 1-minute bars, sorted ascending by ``timestamp``,
        with the lake columns (``timestamp, open, high, low, close, volume`` and,
        for OI, ``open_interest``). An empty frame yields an empty frame.
    target_tf_minutes
        The target timeframe in minutes (one of the 21 TFs).
    cutoff_min
        The intraday IST minute-of-day cutoff (e.g. ``09:20`` ⇒ ``560``) above which a
        forming bucket is dropped, or ``None`` for the whole session.

    Returns
    -------
    polars.DataFrame
        The resampled bars (columns ``ts_utc, open, high, low, close, volume`` + OI when
        present), ascending by ``ts_utc``, with the trailing forming bucket dropped when
        *cutoff_min* lands mid-bucket. Empty when the input is empty.
    """
    if day_df.is_empty():
        return day_df
    renamed = day_df.rename({_LAKE_TS_COLUMN: _RESAMPLER_TS_COLUMN})
    resampled: pl.DataFrame = resample_1m_to_tf(renamed, target_tf_minutes)
    if cutoff_min is None or resampled.is_empty():
        return resampled
    if lake_rust_resample_enabled():
        # DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 (the ADR-0020 row-13
        # forming-bucket-filter port): the cutoff filter routes through the
        # crate with the verbatim polars filter body below as the live
        # oracle. The resample stage ABOVE already rode the inner
        # ``resample_1m_to_tf`` seam — this is the second (filter) stage.
        return session_cutoff_via_rust(
            resampled, target_tf_minutes, cutoff_min, oracle=_session_cutoff_python
        )
    return _session_cutoff_python(resampled, target_tf_minutes, cutoff_min)


def _session_cutoff_python(
    resampled: pl.DataFrame,
    target_tf_minutes: int,
    cutoff_min: int,
) -> pl.DataFrame:
    """The verbatim pre-seam forming-bucket filter (the Slice-0-style decomposition).

    Extracted token-for-token from :func:`resample_session_to_tf` by
    DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 so the public fn can gate the Rust
    seam while the OFF (default) path stays byte-identical — AND the live
    parity oracle every seam degrade path serves.
    """
    import polars as pl

    # Drop the trailing FORMING bucket: a bucket labelled at IST minute ``s`` closes at
    # ``s + M``; keep it only when fully closed at/under the cutoff (``s + M <= cutoff``).
    # IST minute-of-day of the bucket label = (ts_utc + 05:30) hour*60 + minute, Int32-cast
    # before the arithmetic (Int8 hour/minute overflow guard — CLAUDE.md §11 COMMON).
    ist = pl.col(_RESAMPLER_TS_COLUMN) + pl.duration(hours=5, minutes=30)
    bucket_start_min = ist.dt.hour().cast(pl.Int32) * 60 + ist.dt.minute().cast(pl.Int32)
    guarded: pl.DataFrame = resampled.filter(bucket_start_min + target_tf_minutes <= cutoff_min)
    return guarded


__all__ = [
    "derive_fno_intraday_tfs",
    "resample_session_to_tf",
]
