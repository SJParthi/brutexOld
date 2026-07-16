"""Track-1 spot derived-timeframe resampler.

The lake holds **21 timeframes** per spot instrument (CLAUDE.md §1:
"1m base + 19 derived intraday + 1d"). Only **1-minute** spot data is fetched
from the vendor's intraday endpoint; the **1-day** anchor comes from
the vendor's historical endpoint. Every **other** timeframe is *derived* from the 1m
base — never fetched (DEC-DHAN-1M-ONLY-001) — and this is spot-only (the
brute-force engine + reference indexes; options are a separate surface).

:func:`derive_intraday_tfs` is that derivation: it resamples a 1-minute spot
DataFrame to the **19 derived intraday** timeframes (``2m … 15m`` + ``30m,
60m, 120m, 180m, 240m``) via the canonical
:func:`~brutex.data.tf_resampler.resample_all_21_tfs`, then drops the two that
are NOT derived from 1m — the ``1m`` base (the source) and the ``1d`` daily
(fetched, not resampled). Output is keyed by the canonical TF label
(:data:`~brutex.data.timeframes.TF_LABELS`) ready for the per-TF lake write.

Pure (no I/O): a sorted 1m :class:`polars.DataFrame` in, a ``{label: df}``
mapping out — so it is 100% unit-testable and trivially composes into the
universe-ingest persist step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brutex.data.lake_rust_resample import derive_frames_via_rust, lake_rust_resample_enabled
from brutex.data.tf_resampler import resample_all_21_tfs
from brutex.data.timeframes import ALL_TF_MINUTES, DAILY_TF_MINUTES, TF_LABELS

if TYPE_CHECKING:
    import polars as pl

#: The 1-minute base TF (the resample *source*, never a derived output).
_BASE_TF_MINUTES: int = 1

#: The 19 DERIVED intraday TFs (the 21 minus the ``1m`` base + the ``1d``
#: daily) in ``ALL_TF_MINUTES`` order — EXACTLY the key set + ORDER the
#: Python arm's dict comprehension over ``resample_all_21_tfs`` yields, so
#: the two arms' output dicts iterate identically.
DERIVED_INTRADAY_TF_MINUTES: tuple[int, ...] = tuple(
    minutes for minutes in ALL_TF_MINUTES if minutes not in (_BASE_TF_MINUTES, DAILY_TF_MINUTES)
)


def derive_intraday_tfs(df_1m: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Resample a 1m spot DataFrame to the 19 derived intraday timeframes.

    When — and ONLY when — ``BRUTEX_LAKE_RUST_RESAMPLE`` is set truthy
    (DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001, the ADR-0020 row-12 fold of this
    wrapper into the crate's entry points), the 19-TF derive routes through
    the ``brutex_lake`` batch face (ONE Arrow-IPC crossing) with the
    verbatim per-TF Python resample body as the live parity oracle; flag OFF
    (the default) is byte-identical to today (the seam is never touched —
    bomb-spy-pinned). Validation stays Python on BOTH arms.

    Parameters
    ----------
    df_1m
        A 1-minute spot :class:`polars.DataFrame` (columns
        ``ts_utc, open, high, low, close, volume``; ``ts_utc`` Datetime,
        sorted ascending) — validated by the underlying resampler.

    Returns
    -------
    dict[str, polars.DataFrame]
        ``{TF label → resampled DataFrame}`` for the **19 derived intraday**
        timeframes only — the ``1m`` base (source) and the ``1d`` daily
        (fetched from ``/charts/historical``, not resampled) are excluded.
    """
    if lake_rust_resample_enabled():
        return _derive_intraday_tfs_rust(df_1m)
    return _derive_intraday_tfs_python(df_1m)


def _derive_intraday_tfs_python(df_1m: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """The verbatim pre-seam derive body (the Slice-0-style decomposition).

    Extracted token-for-token from :func:`derive_intraday_tfs` by
    DEC-LAKE-RUST-RESAMPLE-SIBLINGS-001 so the public fn can gate the Rust
    seam while the OFF (default) path stays byte-identical.
    """
    all_tfs = resample_all_21_tfs(df_1m)
    return {
        TF_LABELS[minutes]: frame
        for minutes, frame in all_tfs.items()
        if minutes not in (_BASE_TF_MINUTES, DAILY_TF_MINUTES)
    }


def _derive_intraday_tfs_rust(df_1m: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """The flag-ON arm: ONE batch crossing over the 19 derived intraday TFs.

    VALIDATION STAYS PYTHON (the seam contract): the SAME
    ``tf_resampler._validate_input`` gate the Python arm hits (via its first
    ``resample_1m_to_tf`` call) runs FIRST, so a schema/ordering defect
    raises the identical ``TypeError``/``ValueError``/``ResampleInputError``
    on both arms — the crate never sees an unvalidated frame. Every degrade
    path inside :func:`~brutex.data.lake_rust_resample.derive_frames_via_rust`
    serves the verbatim per-TF Python oracle, so the returned mapping never
    depends on the wheel being present or correct.
    """
    # Friend imports of the resampler's validation gate + pre-seam body (the
    # DEC-P0-017 convention — deliberate cross-module reuse, never a copy).
    from brutex.data.tf_resampler import (
        _resample_1m_to_tf_python,  # pyright: ignore[reportPrivateUsage]
        _validate_input,  # pyright: ignore[reportPrivateUsage]
    )

    _validate_input(df_1m)
    frames = derive_frames_via_rust(
        df_1m, DERIVED_INTRADAY_TF_MINUTES, oracle=_resample_1m_to_tf_python
    )
    return {TF_LABELS[minutes]: frames[minutes] for minutes in DERIVED_INTRADAY_TF_MINUTES}


__all__ = [
    "DERIVED_INTRADAY_TF_MINUTES",
    "derive_intraday_tfs",
]
