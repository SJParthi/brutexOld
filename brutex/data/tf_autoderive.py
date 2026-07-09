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

:func:`compare_daily_freshness` is the ``1day`` sibling
(DEC-MOVERS-STORED-FRESHNESS-001 fold-in — closes the BND-5b abstain): the
derived ``1day`` follows a DIFFERENT law (the midnight-IST restamped
:func:`brutex.data.spot_daily_resample.resample_1m_to_daily`, not the
session-anchored intraday resampler), so it gets its own comparator instead
of a forced fit — same fresh/stale contract, same exact-equality honesty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date
    from pathlib import Path

    import polars as pl

__all__ = [
    "compare_daily_freshness",
    "compare_derived_freshness",
    "lake_derived_freshness_verdicts",
    "read_lake_bars_frame",
]


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


def compare_daily_freshness(
    df_1m: pl.DataFrame, df_stored_1day: pl.DataFrame, *, before: date
) -> str:
    """Compare a STORED ``1day`` table against a fresh canonical re-derivation.

    The ``1day`` arm of the sweep-preflight freshness gate
    (DEC-MOVERS-STORED-FRESHNESS-001 fold-in — closes the BND-5b abstain:
    a kill-9 mid-incremental-re-derive, or a 1m fetch of a new day with no
    prebuild, previously left a durably-stale stored ``1day`` that a
    ``1minute``/``1day``-only sweep consumed ungated into run identity).
    Re-derives from *df_1m* via
    :func:`brutex.data.spot_daily_resample.resample_1m_to_daily` — the ONE
    canonical spot daily law the pipeline prebuild writes with (midnight-IST
    restamp, forming-session drop at *before*; CLAUDE.md §11 COMMON, never a
    re-mint) — converts *df_stored_1day* through the SAME
    :func:`~brutex.data.spot_daily_resample.frame_to_candle_bars` converter
    (``open_interest=None`` on both sides by construction: spot daily bars
    carry no OI), and compares the
    ``(timestamp, open, high, low, close, volume)`` tuple lists exactly.
    Returns ``"fresh"`` on exact agreement, ``"stale"`` on ANY drift.

    The TIMESTAMP is INCLUDED — a DELIBERATE divergence from the movers
    display comparator
    :func:`brutex.dashboard.movers_snapshot._daily_stored_is_stale`, which is
    VALUE-plane only (a read-only screen serving values; legacy 09:15-stamped
    fixtures must not false-flip a display). HERE the sweep consumes the
    ``1day`` timestamps into run identity (the PDH/PDL anchor, Golden
    Rule #4) and both production stamp sources (vendor-parsed and derived)
    are midnight-IST, so a stamp drift IS real drift — fail-closed. The two
    comparators sit on different honesty planes; never "unify" them. Values
    stay EXACT (both sides 2dp-snapped at the write boundary,
    DEC-SPOT-PRICE-2DP-001) — no tolerance band.
    """
    from brutex.data.spot_daily_resample import frame_to_candle_bars, resample_1m_to_daily

    expected = resample_1m_to_daily(df_1m, before=before)
    stored = frame_to_candle_bars(df_stored_1day, absent_open_interest=None)
    if len(expected) != len(stored):
        return "stale"
    expected_key = [(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in expected]
    stored_key = [(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in stored]
    return "fresh" if expected_key == stored_key else "stale"


def read_lake_bars_frame(symbol_dir: Path, interval: str, *, source: str = "") -> pl.DataFrame:
    """Read ONE stored ``(symbol, interval)`` partition as the canonical compare frame.

    The COMMON lake read the freshness compares run on (CLAUDE.md §11 —
    DEC-SUSPICION-SWEEP-FIXES-001 G1 extracted it from the doctor's
    ``check_derived_freshness`` so the doctor probe and the sweep-preflight gate
    share ONE reader): keep-FIRST read dedup BEFORE the sort
    (DEC-LAKE-READ-UNIQUE-001 — the compare must see the same deduped bars every
    wired reader serves, never a poisoned-era physical duplicate), renamed +
    projected to the resampler's canonical ``ts_utc``/OHLCV columns, sorted.
    *source* labels the dedup log event; blank derives ``<symbol>/<interval>``.
    """
    import polars as pl

    from brutex.data.lake_read import dedup_bars_frame

    frame, _dropped = dedup_bars_frame(
        pl.read_parquet((symbol_dir / interval).as_posix() + "/**/*.parquet"),
        source=source or f"{symbol_dir.name}/{interval}",
    )
    return (
        frame.rename({"timestamp": "ts_utc"})
        .select("ts_utc", "open", "high", "low", "close", "volume")
        .sort("ts_utc")
    )


def lake_derived_freshness_verdicts(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbols: Sequence[str],
    intervals: Iterable[str],
    daily_before: date | None = None,
) -> dict[tuple[str, str], str]:
    """Freshness verdict per requested DERIVED ``(symbol, interval)`` pair with stored data.

    The sweep-preflight freshness source (DEC-SUSPICION-SWEEP-FIXES-001 G1): for
    every requested *symbol* × *interval* where the interval is a DERIVED intraday
    timeframe (:data:`~brutex.data.timeframe_materialize.INTRADAY_TF_INTERVALS`)
    AND the lake stores BOTH the symbol's ``1minute`` base and the derived
    partition, re-derive from the 1-minute via the canonical session-anchored
    resampler and record :func:`compare_derived_freshness`'s verdict (``"fresh"``
    / ``"stale"``). The derived ``1day`` is likewise keyed
    (DEC-MOVERS-STORED-FRESHNESS-001 fold-in — previously an abstain, the BND-5b
    gap): compared via :func:`compare_daily_freshness` against the canonical
    midnight-IST daily law, with the forming-session cutoff resolved ONCE per
    call — *daily_before* when injected (the deterministic test seam), else the
    production :func:`brutex.calendars.nse.daily_derive_cutoff` at
    :func:`brutex.calendars.nse.now_ist` (the prebuild's own cutoff resolver) —
    so mid-session runs exclude the forming day from the expected side exactly
    as the writer does (no false-stale on a mid-session preflight).
    HONEST abstain envelope (Golden Rule #1 — never guess):

    * ``1minute`` is never keyed — it is the fetched base, there is nothing to
      re-derive;
    * an FNO-segment ``1day`` is never keyed — the F&O daily law is the
      OI-carrying :func:`brutex.data.fno_daily_resample.resample_1m_to_fno_daily`,
      so the spot comparator would false-flag (documented abstain; an FNO-law
      comparator is the named follow-up DEC-FNO-DAILY-FRESHNESS-001, not
      silently claimed);
    * a pair with NO stored derived partition is never keyed — the ZERO-bar
      slice preflight (:func:`~brutex.engine.sweep_preflight.preflight_slices`)
      already owns the missing-data refusal;
    * a symbol with NO stored ``1minute`` base is never keyed — the verdict is
      unknowable without the base (abstain, mirroring the doctor's skip).

    The per-pair 1-minute frame is read ONCE per symbol (shared by the intraday
    AND the ``1day`` arms). O(stored bars) per launch — a deliberate one-time
    preflight cost against consuming a stale derived table into run identity
    (the ``1day`` partition itself is intrinsically tiny: one bar per session).
    """
    from brutex.data.timeframe_materialize import INTRADAY_TF_INTERVALS
    from brutex.data.timeframes import parse_label

    interval_to_label = {v: k for k, v in INTRADAY_TF_INTERVALS.items()}
    verdicts: dict[tuple[str, str], str] = {}
    interval_list = list(intervals)
    requested = [i for i in interval_list if i in interval_to_label]
    # The FNO ``1day`` abstain: a different daily law (OI-carrying) — see above.
    want_daily = "1day" in interval_list and segment != "FNO"
    if not requested and not want_daily:
        return verdicts
    daily_cutoff: date | None = None
    if want_daily:
        if daily_before is not None:
            daily_cutoff = daily_before
        else:
            from brutex.calendars.nse import daily_derive_cutoff, now_ist

            daily_cutoff = daily_derive_cutoff(now_ist())
    for symbol in symbols:
        symbol_dir = lake_root / "bars" / exchange / segment / symbol
        base = symbol_dir / "1minute"
        if not (base.is_dir() and any(base.rglob("*.parquet"))):
            continue  # no 1-minute base → verdict unknowable → honest abstain
        df_1m: pl.DataFrame | None = None
        for interval in requested:
            derived_dir = symbol_dir / interval
            if not (derived_dir.is_dir() and any(derived_dir.rglob("*.parquet"))):
                continue  # nothing stored → the zero-bar preflight owns this pair
            if df_1m is None:
                df_1m = read_lake_bars_frame(symbol_dir, "1minute")
            verdicts[(symbol, interval)] = compare_derived_freshness(
                df_1m,
                read_lake_bars_frame(symbol_dir, interval),
                parse_label(interval_to_label[interval]),
            )
        if daily_cutoff is not None:
            daily_dir = symbol_dir / "1day"
            if not (daily_dir.is_dir() and any(daily_dir.rglob("*.parquet"))):
                continue  # nothing stored → the zero-bar preflight owns this pair
            if df_1m is None:
                df_1m = read_lake_bars_frame(symbol_dir, "1minute")
            verdicts[(symbol, "1day")] = compare_daily_freshness(
                df_1m,
                read_lake_bars_frame(symbol_dir, "1day"),
                before=daily_cutoff,
            )
    return verdicts
