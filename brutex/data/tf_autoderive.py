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
of a forced fit — same fresh/stale contract, same exact-equality honesty. Its
judged set is SESSION-SCOPED to the 1-minute base's own coverage and its
cutoff law MIRRORS the prebuild writer's data-grounded DEFER-only arm (both
review-round-2 fixes of the same fold-in — see the function docstrings for
the two executed permanent-refusal repros they close).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date, datetime
    from pathlib import Path

    import polars as pl

__all__ = [
    "compare_daily_freshness",
    "compare_derived_freshness",
    "lake_derived_freshness_verdicts",
    "read_lake_bars_frame",
]

#: The IST zone name for the per-bar session-date fold (the movers modules'
#: ``_IST`` string twin — a zone NAME for :meth:`polars.Expr.dt.convert_time_zone`,
#: never a re-minted offset; the offset law itself stays owned by
#: :mod:`brutex.data.spot_daily_resample`).
_IST_TZ: Final[str] = "Asia/Kolkata"


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


def _ist_date_expr(frame: pl.DataFrame) -> pl.Expr:
    """The frame's ``ts_utc`` as IST calendar dates — naive treated as UTC.

    The ONE per-bar session-date fold both sides of
    :func:`compare_daily_freshness` use (never two conversions that could
    disagree). A tz-naive ``ts_utc`` is stamped UTC first — the lake storage
    convention, mirroring ``timeframe_materialize._last_ist_datetime``.
    """
    import polars as pl

    expr = pl.col("ts_utc")
    if getattr(frame.schema["ts_utc"], "time_zone", None) is None:
        expr = expr.dt.replace_time_zone("UTC")
    return expr.dt.convert_time_zone(_IST_TZ).dt.date()


def compare_daily_freshness(
    df_1m: pl.DataFrame,
    df_stored_1day: pl.DataFrame,
    *,
    before: date,
    grounded_before: date | None = None,
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
    restamp, forming-session drop at the cutoff; CLAUDE.md §11 COMMON, never a
    re-mint) — converts *df_stored_1day* through the SAME
    :func:`~brutex.data.spot_daily_resample.frame_to_candle_bars` converter
    (``open_interest=None`` on both sides by construction: spot daily bars
    carry no OI), and compares the
    ``(timestamp, open, high, low, close, volume)`` tuple lists exactly.
    Returns ``"fresh"`` on exact agreement, ``"stale"`` on ANY drift.

    SESSION-SCOPED judged set (review round 2 — the vendor-era
    permanent-refusal fix): only stored days the 1-minute base COVERS are
    judged. A stored ``1day`` bar on an IST session with ZERO stored
    1-minute bars — a #723-era vendor-fetched daily row whose 1m the
    pre-#1294 date-blind session filter dropped (Muhurat evenings) or a
    vendor 1m gap on a ledger-settled window — is UNVERIFIABLE from the
    base, so it ABSTAINS per-bar, mirroring the caller's no-1m-base
    whole-symbol abstain. Judging it "stale" would be PERMANENT: such a day
    is outside the prebuild's derive set and the derived-``1day`` write is a
    latest-wins MERGE that never deletes rows, so the gate's own stated
    remedy provably could not clear the refusal (executed repro, 2026-07-09
    round 2: the verdict survived a canonical re-derive over the same
    partition). Corollary: an EMPTY *df_1m* verifies nothing and returns
    ``"fresh"`` vacuously — the verdicts caller never keys such a pair (its
    no-base abstain fires first).

    DUAL SANCTIONED CUTOFF (same round — the writer-mirror fix):
    *grounded_before*, when set and different from *before*, is the
    data-grounded DEFER-only cutoff the production prebuild ACTUALLY writes
    dense (index) symbols with
    (:func:`brutex.calendars.nse.daily_derive_cutoff_grounded`,
    DEC-DAILY-CUTOFF-GROUNDED-001): the stored table is ``"fresh"`` iff it
    matches the canonical derive under EITHER cutoff. Without it, a
    truncated run day (a pull died mid-session) judged post-close flagged
    the writer's OWN grounded-deferred output "stale" — and a fresh prebuild
    grounded-defers again, so the stated remedy looped until the day's 1m
    was re-pulled complete (executed repro, same round). Because the
    grounded arm is DEFER-only, the acceptance is one-directional and
    run-day-only: it can only excuse the run day's ABSENT bar, never a value
    drift (a drifted stored bar mismatches under both cutoffs — fail-closed
    preserved).

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

    covered = df_1m.select(_ist_date_expr(df_1m).alias("ist_date")).get_column("ist_date")
    judged = df_stored_1day.filter(_ist_date_expr(df_stored_1day).is_in(covered))
    stored = frame_to_candle_bars(judged, absent_open_interest=None)
    stored_key = [(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in stored]
    cutoffs = [before]
    if grounded_before is not None and grounded_before != before:
        cutoffs.append(grounded_before)
    for cutoff in cutoffs:
        expected = resample_1m_to_daily(df_1m, before=cutoff)
        expected_key = [(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in expected]
        if expected_key == stored_key:
            return "fresh"
    return "stale"


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


def _now_ist() -> datetime:
    """Injectable IST clock seam for the production daily-cutoff resolve.

    Delegates to the ONE clock leaf :func:`brutex.calendars.nse.now_ist`;
    tests freeze THIS seam, never the leaf (the
    ``timeframe_materialize._now_ist`` doctrine, DEC-DAILY-CUTOFF-GROUNDED-001).
    """
    from brutex.calendars.nse import now_ist

    return now_ist()


def _grounded_daily_cutoff(
    df_1m: pl.DataFrame, *, now: datetime | None, clock: date
) -> date | None:
    """The writer-mirror grounded ``1day`` cutoff for ONE symbol, or ``None`` when inert.

    Mirrors the prebuild writer's data-grounded arm
    (``timeframe_materialize.materialize_lake_timeframes`` —
    DEC-DAILY-CUTOFF-GROUNDED-001) so the gate judges a symbol under the SAME
    cutoff law the writer wrote it with (the round-2 false-stale fix): active
    only on the production default-cutoff path (*now* is ``None`` ⇔ an
    explicit injected ``daily_before``, which is LAW — exactly the writer's
    ``cutoff_now`` rule) and only while the shared
    :func:`~brutex.data.timeframe_materialize.daily_cutoff_grounding_enabled`
    env gate is ON; the last-bar instant comes from the SAME extraction the
    writer feeds (``timeframe_materialize._last_ist_datetime`` — one
    extraction, one convention, §11 COMMON). Returns ``None`` when the
    grounded verdict equals the clock verdict (single-law compare — the
    common complete-session case is byte-identical to the clock arm by
    construction).

    HONEST envelope (Golden Rule #10): applied WITHOUT a per-symbol density
    check — the writer grounds only dense (index) symbols, but accepting the
    grounded-DEFERRED state for a non-dense symbol is a disclosed
    one-directional leniency (run-day-only, absent-bar-only, never a value
    drift; the pre-fold-in gate abstained on ALL of ``1day``, so this is
    still strictly tighter than what shipped before the fold-in).
    """
    if now is None:
        return None
    from brutex.calendars.nse import daily_derive_cutoff_grounded
    from brutex.data.timeframe_materialize import (  # friend access (DEC-P0-017 idiom):
        _last_ist_datetime,  # pyright: ignore[reportPrivateUsage] — the writer's OWN extraction
        daily_cutoff_grounding_enabled,
    )

    if not daily_cutoff_grounding_enabled():
        return None
    grounded = daily_derive_cutoff_grounded(now, last_bar_ist=_last_ist_datetime(df_1m))
    return None if grounded == clock else grounded


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
    midnight-IST daily law, with the forming-session CLOCK cutoff resolved ONCE
    per call — *daily_before* when injected (the deterministic test seam, which
    is LAW exactly as the writer's explicit ``daily_before`` is), else the
    production :func:`brutex.calendars.nse.daily_derive_cutoff` at the
    :func:`_now_ist` seam — so mid-session runs exclude the forming day from
    the expected side exactly as the writer does (no false-stale on a
    mid-session preflight); on the production path the per-symbol
    :func:`_grounded_daily_cutoff` writer-mirror is threaded alongside it, so
    a post-close preflight over a TRUNCATED run day accepts the writer's own
    grounded-DEFERRED output as fresh (review round 2 — see the comparator
    docstring for both executed permanent-refusal repros this pair of fixes
    closes). HONEST abstain envelope (Golden Rule #1 — never guess):

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
      unknowable without the base (abstain, mirroring the doctor's skip);
    * a stored ``1day`` bar on a session with NO 1-minute coverage abstains
      PER-BAR inside :func:`compare_daily_freshness` (the vendor-era rows the
      prebuild can neither re-derive nor delete — see its docstring).

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
    daily_now: datetime | None = None
    if want_daily:
        if daily_before is not None:
            daily_cutoff = daily_before  # injected cutoff is LAW — the writer's own rule
        else:
            from brutex.calendars.nse import daily_derive_cutoff

            daily_now = _now_ist()
            daily_cutoff = daily_derive_cutoff(daily_now)
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
                grounded_before=_grounded_daily_cutoff(df_1m, now=daily_now, clock=daily_cutoff),
            )
    return verdicts
