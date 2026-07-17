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
exact equality is the honest comparison. The 1-minute BASE side is read with
the SAME bad-tick screen the prebuild's read runs
(DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001 — screened-vs-screened, one
``BRUTEX_BAD_TICK_SCREEN`` switch), so a legitimate stored in-session spike
the screen exists for can never read a healthy lake as permanently stale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    import polars as pl

__all__ = [
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


def read_lake_bars_frame(
    symbol_dir: Path, interval: str, *, source: str = "", screen_bad_ticks: bool = False
) -> pl.DataFrame:
    """Read ONE stored ``(symbol, interval)`` partition as the canonical compare frame.

    The COMMON lake read the freshness compares run on (CLAUDE.md §11 —
    DEC-SUSPICION-SWEEP-FIXES-001 G1 extracted it from the doctor's
    ``check_derived_freshness`` so the doctor probe and the sweep-preflight gate
    share ONE reader): keep-FIRST read dedup BEFORE the sort
    (DEC-LAKE-READ-UNIQUE-001 — the compare must see the same deduped bars every
    wired reader serves, never a poisoned-era physical duplicate), renamed +
    projected to the resampler's canonical ``ts_utc``/OHLCV columns, sorted.
    *source* labels the dedup log event; blank derives ``<symbol>/<interval>``.

    *screen_bad_ticks* (DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001): ``True`` opts
    the read into the SAME bad-tick screen the prebuild's 1-minute read runs
    (:func:`~brutex.data.bad_tick.screen_frame_bad_ticks`, REUSED — never
    copied), gated by the SAME ``BRUTEX_BAD_TICK_SCREEN`` kill switch
    (:func:`~brutex.data.bad_tick.bad_tick_screen_enabled` checked HERE, at the
    read site — exactly mirroring
    :func:`~brutex.data.timeframe_materialize.make_lake_read_1m`'s gating, so
    the ONE switch flips every screened boundary together; switch OFF ⇒ the
    raw deduped frame, byte-identical to the pre-parity read). The freshness
    consumers set it on their **1-minute BASE read only** — the stored DERIVED
    partition is the prebuild's screened OUTPUT and must never be re-screened
    (a derived bucket's legitimate coarse move could false-trip the in-session
    ratio test). Default ``False`` keeps every other read byte-identical.
    ``BRUTEX_BAD_TICK_SCREEN`` is resolved PER-PROCESS (read at call time from
    THIS process's environment) — a prebuild and a later gate run executed
    under DIFFERENT env settings can disagree, and converge via the printed
    re-prebuild remedy (a transient ``"stale"``, never a permanent wedge).
    """
    import polars as pl

    from brutex.data.bad_tick import bad_tick_screen_enabled, screen_frame_bad_ticks
    from brutex.data.lake_read import dedup_bars_frame

    resolved_source = source or f"{symbol_dir.name}/{interval}"
    frame, _dropped = dedup_bars_frame(
        pl.read_parquet((symbol_dir / interval).as_posix() + "/**/*.parquet"),
        source=resolved_source,
    )
    frame = (
        frame.rename({"timestamp": "ts_utc"})
        .select("ts_utc", "open", "high", "low", "close", "volume")
        .sort("ts_utc")
    )
    if screen_bad_ticks and bad_tick_screen_enabled():
        # DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001: compare screened-vs-screened.
        # The prebuild writes derived TFs from the SCREENED 1-minute
        # (make_lake_read_1m, DEC-READ-GUARD-PARITY-001), so a freshness
        # re-derivation from the UNSCREENED raw 1m read a lake with ONE
        # legitimate self-consistent >=4x in-session spike as PERMANENTLY
        # "stale" — a false sweep/doctor wedge no prebuild re-run could ever
        # clear. Runs AFTER the sort (the screen's documented precondition);
        # clean frames return the input object unchanged (zero-copy).
        frame = screen_frame_bad_ticks(frame, ts_column="ts_utc", source=resolved_source)
    return frame


def lake_derived_freshness_verdicts(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbols: Sequence[str],
    intervals: Iterable[str],
) -> dict[tuple[str, str], str]:
    """Freshness verdict per requested DERIVED ``(symbol, interval)`` pair with stored data.

    The sweep-preflight freshness source (DEC-SUSPICION-SWEEP-FIXES-001 G1): for
    every requested *symbol* × *interval* where the interval is a DERIVED intraday
    timeframe (:data:`~brutex.data.timeframe_materialize.INTRADAY_TF_INTERVALS`)
    AND the lake stores BOTH the symbol's ``1minute`` base and the derived
    partition, re-derive from the 1-minute via the canonical session-anchored
    resampler and record :func:`compare_derived_freshness`'s verdict (``"fresh"``
    / ``"stale"``). HONEST abstain envelope (Golden Rule #1 — never guess):

    * a NON-derived interval (``1minute`` / ``1day``) is never keyed — there is
      nothing to re-derive (the daily partition derives via the midnight-IST
      resample, a different law — deliberately out of this gate's scope, exactly
      as the doctor probe scopes it);
    * a pair with NO stored derived partition is never keyed — the ZERO-bar
      slice preflight (:func:`~brutex.engine.sweep_preflight.preflight_slices`)
      already owns the missing-data refusal;
    * a symbol with NO stored ``1minute`` base is never keyed — the verdict is
      unknowable without the base (abstain, mirroring the doctor's skip).

    Screen parity (DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001): the 1-minute BASE
    read opts into the SAME bad-tick screen the prebuild's read runs
    (``screen_bad_ticks=True`` on :func:`read_lake_bars_frame` — the one
    ``BRUTEX_BAD_TICK_SCREEN`` kill switch honored at the read site), so the
    re-derivation compares screened-vs-screened; switch OFF compares
    unscreened-vs-unscreened (the pre-parity behavior). The stored DERIVED
    partition read stays unscreened — it is the prebuild's screened output.
    HONEST residual (the ghost-bucket class, named follow-up
    DEC-DERIVED-GHOST-BUCKET-PRUNE-001): a stored derived bucket whose SOLE
    1-minute source is screened away survives even a forced full re-prebuild
    (the derived writers are latest-wins and never delete), so such a lake
    honestly reads ``"stale"`` — the stored table genuinely serves a bucket
    the screened base disowns; the escape is deleting the symbol's derived TF
    partitions and re-running the prebuild (see
    :meth:`~brutex.engine.sweep_preflight.FreshnessReport.remediation`).

    The per-pair 1-minute frame is read ONCE per symbol. O(stored bars) per
    launch — a deliberate one-time preflight cost against consuming a stale
    derived table into run identity.
    """
    from brutex.data.timeframe_materialize import INTRADAY_TF_INTERVALS
    from brutex.data.timeframes import parse_label

    interval_to_label = {v: k for k, v in INTRADAY_TF_INTERVALS.items()}
    verdicts: dict[tuple[str, str], str] = {}
    requested = [i for i in intervals if i in interval_to_label]
    if not requested:
        return verdicts
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
                df_1m = read_lake_bars_frame(symbol_dir, "1minute", screen_bad_ticks=True)
            verdicts[(symbol, interval)] = compare_derived_freshness(
                df_1m,
                read_lake_bars_frame(symbol_dir, interval),
                parse_label(interval_to_label[interval]),
            )
    return verdicts
