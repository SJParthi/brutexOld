"""Derived-timeframe FRESHNESS compare — the ``brutex doctor`` probe core.

Historical note: this module once ALSO carried the ``BRUTEX_AUTO_DERIVE_TFS``
pull-settle auto-derive planner (DEC-TF-MATERIALIZE-002 — ``autoderive_enabled``
/ ``plan_autoderive_symbols`` / ``run_autoderive``). That arm is REMOVED by
DEC-PIPELINE-AUTOPREBUILD-001: derived timeframes are written ONLY by the
pipeline's automatic FINAL prebuild phase
(:mod:`brutex.dashboard.pipeline_prebuild`), never mid-pull and never behind an
env flag. What remains here is the pure freshness compare the doctor uses.

## Freshness compare (the doctor probe core)

:func:`compare_derived_freshness` is the pure heart of the ``brutex
doctor`` derived-freshness check: re-derive a stored timeframe from the 1m
base via the canonical session-anchored resampler and compare
frame-for-frame. Any drift (a stale table left behind by a 1m backfill, a
pre-DEC-RESAMPLE-ANCHOR-002 gapped-day grid) returns ``"stale"`` — never
silently absorbed. Both sides hold lake values already snapped to the
2-decimal tick grid at the write boundary (DEC-SPOT-PRICE-2DP-001), so
exact equality is the honest comparison.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl

__all__ = ["compare_derived_freshness"]


def compare_derived_freshness(df_1m: pl.DataFrame, df_stored: pl.DataFrame, tf_minutes: int) -> str:
    """Compare a STORED derived table against a fresh re-derivation.

    Re-derives *tf_minutes* from *df_1m* via the canonical session-anchored
    resampler (DEC-RESAMPLE-ANCHOR-002) and compares against *df_stored*
    frame-for-frame over the canonical bar columns. Returns ``"fresh"`` on
    exact agreement, ``"stale"`` on ANY drift (row count, timestamps, or
    values). Lake prices on both sides are already 2dp-tick-snapped at the
    write boundary (DEC-SPOT-PRICE-2DP-001), so exact equality is the
    honest comparison — no tolerance band that could hide real drift.
    """
    from brutex.data.tf_resampler import REQUIRED_COLUMNS, resample_1m_to_tf

    expected = resample_1m_to_tf(df_1m, tf_minutes).select(*REQUIRED_COLUMNS)
    stored = df_stored.select(*REQUIRED_COLUMNS).sort("ts_utc")
    if expected.height != stored.height:
        return "stale"
    return "fresh" if expected.equals(stored.cast(expected.schema)) else "stale"
