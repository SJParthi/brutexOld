"""Tests for `brutex.data.tf_autoderive` — the doctor derived-freshness compare core.

The pull-settle auto-derive arm (``autoderive_enabled`` /
``plan_autoderive_symbols`` / ``run_autoderive`` + the ``BRUTEX_AUTO_DERIVE_TFS``
opt-in of DEC-TF-MATERIALIZE-002) is REMOVED by DEC-PIPELINE-AUTOPREBUILD-001 —
derived timeframes are written ONLY by the pipeline's automatic prebuild phase
(covered in ``test_pipeline_prebuild.py`` / ``test_gated_prebuild.py``). What
remains here is the pure freshness compare the ``brutex doctor`` probe uses
(fresh / stale on value drift / stale on row-count drift) plus a pin that the
removed auto-derive surface stays removed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
from brutex.data import tf_autoderive
from brutex.data.tf_autoderive import compare_derived_freshness

_OPEN_UTC = datetime(2025, 6, 9, 3, 45, tzinfo=UTC).replace(tzinfo=None)


def _df_1m(n_bars: int = 20) -> pl.DataFrame:
    closes = [100.0 + i * 0.1 for i in range(n_bars)]
    return pl.DataFrame(
        {
            "ts_utc": [_OPEN_UTC + timedelta(minutes=i) for i in range(n_bars)],
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000] * n_bars,
        },
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


class TestFreshnessCompare:
    def test_fresh_when_stored_equals_rederivation(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _df_1m()
        stored = resample_1m_to_tf(df_1m, 5)
        assert compare_derived_freshness(df_1m, stored, 5) == "fresh"

    def test_stale_on_value_drift(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _df_1m()
        stored = resample_1m_to_tf(df_1m, 5).with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.col("close") + 1.0)
            .otherwise(pl.col("close"))
            .alias("close")
        )
        assert compare_derived_freshness(df_1m, stored, 5) == "stale"

    def test_stale_on_row_count_drift(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        df_1m = _df_1m()
        stored = resample_1m_to_tf(df_1m, 5).slice(1)  # a bucket went missing
        assert compare_derived_freshness(df_1m, stored, 5) == "stale"


class TestAutoDeriveSurfaceRemoved:
    """DEC-PIPELINE-AUTOPREBUILD-001: the pull-settle auto-derive arm stays removed."""

    def test_autoderive_symbols_gone(self) -> None:
        for gone in (
            "autoderive_enabled",
            "plan_autoderive_symbols",
            "run_autoderive",
            "AutoderiveOutcome",
            "AutoderiveReport",
            "ENV_AUTO_DERIVE_TFS",
        ):
            assert not hasattr(tf_autoderive, gone), (
                f"{gone} must stay removed — derived TFs are written only by the "
                "pipeline prebuild (DEC-PIPELINE-AUTOPREBUILD-001)"
            )
        assert tf_autoderive.__all__ == ["compare_derived_freshness"]
