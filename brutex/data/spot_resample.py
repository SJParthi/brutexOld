"""Track-1 spot derived-timeframe resampler.

The lake holds **21 timeframes** per spot instrument (CLAUDE.md §1:
"1m base + 19 derived intraday + 1d"). Only **1-minute** spot data is fetched
from Dhan ``/charts/intraday``; the **1-day** anchor comes from
``/charts/historical``. Every **other** timeframe is *derived* from the 1m
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

from brutex.data.tf_resampler import resample_all_21_tfs
from brutex.data.timeframes import DAILY_TF_MINUTES, TF_LABELS

if TYPE_CHECKING:
    import polars as pl

#: The 1-minute base TF (the resample *source*, never a derived output).
_BASE_TF_MINUTES: int = 1


def derive_intraday_tfs(df_1m: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Resample a 1m spot DataFrame to the 19 derived intraday timeframes.

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
    all_tfs = resample_all_21_tfs(df_1m)
    return {
        TF_LABELS[minutes]: frame
        for minutes, frame in all_tfs.items()
        if minutes not in (_BASE_TF_MINUTES, DAILY_TF_MINUTES)
    }


__all__ = [
    "derive_intraday_tfs",
]
