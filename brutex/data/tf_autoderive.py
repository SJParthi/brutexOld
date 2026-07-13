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
exact equality is the honest comparison. Since
DEC-DERIVED-1DAY-FRESHNESS-001 the compare is OI-AWARE (a drifted F&O
``open_interest`` reads stale — null ≡ null) and the derived ``1day`` has
its own VALUE arm, :func:`compare_daily_freshness`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date, datetime
    from pathlib import Path

    import polars as pl

_LOG = structlog.get_logger(__name__)

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

    OI-aware (DEC-DERIVED-1DAY-FRESHNESS-001): when BOTH the re-derived
    frame (*df_1m* carried an ``open_interest`` column — the F&O lake read)
    AND *df_stored* carry the column, ``open_interest`` joins the compared
    columns (null ≡ null — polars ``equals`` treats nulls as equal), so a
    stored partition whose ONLY drift is a fabricated/stale OI reads
    ``"stale"``. Disclosed transient: a legacy pre-DEC-DERIVED-OI-NULL-
    HONEST-001 partition (OI fabricated as 0 where the base is null) reads
    stale for at most one nightly — the writer_convention stamp bump forces
    its rewrite on that SAME nightly (bounded, self-healing).
    """
    from brutex.data.tf_resampler import (
        OPEN_INTEREST_COLUMN,
        REQUIRED_COLUMNS,
        resample_1m_to_tf,
    )

    expected_full = resample_1m_to_tf(df_1m, tf_minutes)
    compare_columns: tuple[str, ...] = REQUIRED_COLUMNS
    if OPEN_INTEREST_COLUMN in expected_full.columns and OPEN_INTEREST_COLUMN in df_stored.columns:
        compare_columns = (*REQUIRED_COLUMNS, OPEN_INTEREST_COLUMN)
    expected = expected_full.select(*compare_columns)
    stored = df_stored.select(*compare_columns).sort("ts_utc")
    if expected.height != stored.height:
        return "stale"
    return "fresh" if expected.equals(stored.cast(expected.schema)) else "stale"


def _ist_date_of(ts: datetime) -> date:
    """A bar instant's IST trading date (naive instants are treated as UTC)."""
    from datetime import UTC, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return aware.astimezone(ist).date()


def compare_daily_freshness(
    df_1m: pl.DataFrame,
    df_stored_1day: pl.DataFrame,
    *,
    segment: str,
    source: str = "",
) -> str:
    """Compare a STORED derived ``1day`` table against a fresh re-derivation.

    The ``1day`` VALUE arm (DEC-DERIVED-1DAY-FRESHNESS-001 — pre-fix the
    derived ``1day``, the PDH/PDL anchor source, was checked NOWHERE): the
    stored partition is re-derived from the 1-minute base via the
    segment-routed daily law — :func:`~brutex.data.fno_daily_resample.
    resample_1m_to_fno_daily` for ``FNO`` (carries last-of-day OI), else
    :func:`~brutex.data.spot_daily_resample.resample_1m_to_daily` — and
    compared per-date: midnight-IST stamp + OHLCV + volume + ``open_interest``
    (null ≡ null; the stored side always carries the column, and the daily
    laws always emit a value — honest ``None`` for spot).

    This is deliberately a VALUE-FIDELITY check, never a RECENCY check:

    * the re-derive is bounded ``before = last stored 1day IST date + 1`` —
      bounding by the stored partition's OWN last date avoids every
      entanglement with ``daily_derive_cutoff`` / the grounded-deferral arm
      (a legitimately-DEFERRED forming session can never read stale — a
      clock-bound variant would refusal-loop on a healthy lake);
    * only the INTERSECTING dates are compared. A re-derived date ABSENT
      from the store (the 1day lagging a 1m backfill — prebuild lag) stays
      OUT of the gate (recency-by-absence is the prebuild's job, not a
      corruption verdict); a STORED date absent from the re-derive (an
      orphan row whose 1m base vanished) is an observe-only
      ``derived_1day_orphan_rows`` WARN, never a stale verdict —
      latest-wins cannot delete an orphan, so a stale verdict would be an
      un-remediable refusal loop.

    A stamp OUTSIDE the Python ``datetime`` range on EITHER side (a
    hand-planted / corrupt i64 — e.g. a year-9999 instant whose +05:30 shift
    passes ``datetime.max``, a ``2**62``-µs value that PANICS the polars
    conversion, or a far-past NEGATIVE i64 in the 1-MINUTE BASE) reads the
    SAME typed ``"stale"`` verdict fail-closed — never an uncaught raise
    escaping the sweep-preflight gate (the #1594 rounds-12–18
    isolation-escape convention). TWO arms per the COMMON sets' consumption
    law: the STORED-1day reduction rides the FULL
    :func:`~brutex.data.errors.datetime_out_of_range_errors` set (its sole
    Python-side failure mode is the stored-timestamp conversion); the
    1-minute-base RE-DERIVATION (the daily law + the expected-bar stamp
    build — refuter round 1: it previously ran OUTSIDE the guard and a
    corrupt base escaped as an uncaught ``PanicException``, while the
    polars-only intraday twin degraded gracefully) rides the
    ValueError-EXCLUDING
    :func:`~brutex.data.errors.datetime_out_of_range_escape_errors` set,
    because the law call spans arbitrary code where a ``ValueError`` can be
    a genuine bug (e.g. the R5 negative-volume-wrap ``CandleBar`` refusal)
    that must keep propagating.

    A NULL or WRONG-DTYPE stored stamp fail-closes the same way (refuter
    round 3): the out-of-range sets vet i64-RANGE faults only, so a stored
    ``ts_utc`` cell that is ``None`` (a null Datetime cell) or not a
    ``datetime`` at all (a hand-planted Int64 / Date / Utf8 column —
    ``ts.tzinfo`` → ``AttributeError``, in NEITHER COMMON set) previously
    ESCAPED the stored-stamp reduction as an uncaught raise through the
    sweep-preflight gate, while the polars-only intraday twin read
    ``"stale"`` gracefully on the identical corruption. The reduction now
    carries its own NARROW ``(AttributeError, TypeError)`` clause — the
    span's sole failure mode is the stored-stamp conversion, so the #1594
    consumption law holds — reading the typed ``"stale"`` verdict + ONE
    ``derived_1day_corrupt_stamp`` WARN.

    A corrupt stored VALUE fail-closes the same way (refuter rounds 2 + 3):
    the stamp guards vet STAMPS only, so a null / NaN- / inf-unconvertible
    stored OHLCV / volume / ``open_interest`` value (a hand-planted /
    corrupt partition — ``float(None)`` / ``int(None)`` → ``TypeError``,
    ``int(NaN)`` → ``ValueError``, ``int(inf)`` → ``OverflowError``)
    previously ESCAPED the compare loop's bare conversions as an uncaught
    raise through the sweep-preflight gate, while the polars-only intraday
    twin read ``"stale"`` gracefully (``.equals``) on the identical
    corruption. The per-row stored-key build now rides its own NARROW
    ``(TypeError, ValueError, OverflowError)`` guard — spanning ONLY the
    stored-row conversions, whose sole failure mode is an unconvertible
    stored value, so the #1594 consumption law holds (a genuine code bug
    elsewhere keeps propagating) — and reads the typed ``"stale"`` verdict
    + ONE ``derived_1day_corrupt_value`` WARN.

    A corrupt 1-MINUTE-BASE VALUE fail-closes the same way (refuter
    round 4): the base re-derive guard's escape set deliberately EXCLUDES
    ``ValueError`` (the law spans arbitrary derive code), so a corrupt /
    hand-planted base VALUE — a negative volume or an inconsistent OHLC row
    whose daily aggregate breaks the ``CandleBar`` ordering law
    (``ValueError``), or a NaN price whose ``Decimal("NaN")`` comparison
    raises ``decimal.InvalidOperation`` — previously ESCAPED the daily
    law's validating bar build as an uncaught raise through the
    sweep-preflight gate, while the polars-only intraday twin (no
    ``CandleBar`` build) stayed graceful on the identical base. The base
    frame is therefore VALUE-SCREENED here — ONE vectorized polars pass
    BEFORE the law runs: price sanity via the COMMON
    :func:`~brutex.data.price_sanity.price_violation_expr` (null /
    non-positive / broken-ordering prices) + negative / non-finite
    volume & ``open_interest`` (exactly the refusal plane of the law's
    ``CandleBar`` build) — reading the typed ``"stale"`` verdict + ONE
    ``derived_1day_corrupt_base`` WARN; a column dtype so hostile the probe
    expressions themselves raise (a hand-planted Utf8/List numeric column —
    ``polars.exceptions.PolarsError``, a NARROW span whose sole failure
    mode is that hostile dtype, the #1594 consumption law) fail-closes the
    same way. Disclosed narrowing (corrupt-lake-only): the screen judges
    the WHOLE base frame (including rows past the ``before`` bound), and a
    value-corrupt base that previously happened to compare ``"fresh"``
    (the corruption laundered into BOTH sides) or drift-``"stale"`` now
    short-circuits to the same ``"stale"`` with the honest corrupt-base
    WARN — the prebuild's own read boundary (#1792 price-sanity + the
    writer's ``CandleBar`` refusal) refuses to DERIVE from such a base
    anyway, so remediation is heal / re-pull, exactly what the WARN names.
    Base COLUMN faults outside the probed value plane (e.g. a wrong-dtype
    ``ts_utc``) keep the documented propagation envelope (the adjudicated
    round-4 MINOR — the pre-existing intraday twin raises identically on
    that class).

    HONEST cost (Golden Rule #10): the per-date compare below is a
    NON-vectorized ``iter_rows`` Python loop — O(stored 1day rows) ≈ one row
    per TRADING DAY per symbol (a full-history partition is hundreds-to-low-
    thousands of rows, never the minutes-scale intraday frames), run once per
    symbol at SWEEP LAUNCH (not a poll); the 1-minute base read is shared
    with the intraday arm.

    Returns ``"fresh"`` / ``"stale"``; *source* labels the orphan WARN.
    """
    from datetime import timedelta

    from brutex.data.errors import (
        datetime_out_of_range_errors,
        datetime_out_of_range_escape_errors,
    )

    if df_stored_1day.height == 0:
        return "fresh"  # nothing stored to drift (callers key only stored partitions)

    try:
        last_stored = max(_ist_date_of(ts) for ts in df_stored_1day.get_column("ts_utc").to_list())
        before = last_stored + timedelta(days=1)
    except datetime_out_of_range_errors():
        # R4-M1 (review round 1): an out-of-range STORED stamp is an
        # unreadable-instant partition — refuse the launch with the typed
        # verdict (fail-closed), never a raw OverflowError / ValueError /
        # PanicException out of the preflight gate. The reduction above
        # materialises EVERY stored stamp (`.to_list()` + the +05:30 shift),
        # so a per-row STAMP conversion in the compare loop below can never
        # hit a stamp this guard (or the corrupt-stamp clause below) did not
        # already vet — the loop's VALUE conversions have their OWN
        # fail-closed guard (refuter rounds 2 + 3). Loud: ONE WARN per
        # partition.
        _LOG.warning(
            "derived_1day_out_of_range_stamp",
            source=source or "1day",
            side="stored-1day",
            detail=(
                "stored 1day stamp outside the Python datetime range (a corrupt/"
                "hand-planted i64) — verdict 'stale' fail-closed; heal-recall or "
                "re-derive the partition"
            ),
        )
        return "stale"
    except (AttributeError, TypeError):
        # Refuter round 3 (seat 1, MAJOR): the out-of-range guard above vets
        # i64-RANGE faults only — a NULL (None) or wrong-dtype (a
        # hand-planted Int64 / Date / Utf8 `ts_utc` column) stored stamp
        # raises AttributeError at the `_ist_date_of` `ts.tzinfo` probe
        # instead, a member of NEITHER COMMON out-of-range set, and
        # previously ESCAPED `lake_derived_freshness_verdicts` as an
        # uncaught raise through the sweep-preflight gate while the
        # polars-only intraday twin read "stale" gracefully on the identical
        # corruption. NARROW try — the reduction's sole failure mode is the
        # stored-stamp conversion, so catching (AttributeError, TypeError)
        # here honors the #1594 consumption law (a genuine code bug
        # elsewhere keeps propagating). Loud: ONE WARN per partition,
        # fail-closed.
        _LOG.warning(
            "derived_1day_corrupt_stamp",
            source=source or "1day",
            side="stored-1day",
            detail=(
                "stored 1day timestamp is null or not a datetime (a corrupt/"
                "hand-planted partition) — verdict 'stale' fail-closed; "
                "heal-recall or re-derive the partition"
            ),
        )
        return "stale"

    import polars as pl

    from brutex.data.price_sanity import PRICE_SANITY_COLUMNS, price_violation_expr

    # Refuter round 4 (seat 2, MAJOR): VALUE-SCREEN the 1-minute base BEFORE
    # the daily law runs — the escape-set guard below deliberately excludes
    # ValueError, so a corrupt/hand-planted base VALUE (negative volume /
    # inconsistent OHLC → the daily law's CandleBar refusal ValueError; a
    # NaN price → Decimal("NaN") comparison decimal.InvalidOperation)
    # previously ESCAPED lake_derived_freshness_verdicts uncaught while the
    # polars-only intraday twin stayed graceful. ONE vectorized pass over
    # exactly the CandleBar refusal plane; an unprobeable column dtype (the
    # probe expressions themselves raise PolarsError — a NARROW span whose
    # sole failure mode is that hostile dtype) fail-closes the same way.
    numeric_columns = [*PRICE_SANITY_COLUMNS, "volume"]
    sign_checks = [pl.col("volume") < 0]
    if "open_interest" in df_1m.columns:
        numeric_columns.append("open_interest")
        sign_checks.append(pl.col("open_interest") < 0)
    try:
        mask = price_violation_expr()
        for check in sign_checks:
            mask = mask | check
        for name in numeric_columns:
            # cast(strict=False) makes the finite probe dtype-agnostic (a
            # legit Int64 volume casts cleanly; null casts stay null → never
            # flagged — null volume/OI is the honest vendor-absent value).
            mask = mask | pl.col(name).cast(pl.Float64, strict=False).is_finite().not_()
        corrupt = df_1m.filter(mask)
        base_corrupt = corrupt.height > 0
        corrupt_rows = corrupt.height
        first_offender = repr(corrupt.get_column("ts_utc")[0]) if base_corrupt else ""
    except pl.exceptions.PolarsError:
        base_corrupt = True
        corrupt_rows = df_1m.height
        first_offender = "<unprobeable column dtype>"
    if base_corrupt:
        _LOG.warning(
            "derived_1day_corrupt_base",
            source=source or "1day",
            side="1minute-base",
            corrupt_rows=corrupt_rows,
            first_offender=first_offender,
            detail=(
                "1-minute base carries value-corrupt row(s) (non-positive/"
                "non-finite/misordered OHLC, or negative/non-finite volume/OI "
                "— a corrupt/hand-planted partition) — verdict 'stale' "
                "fail-closed; heal or re-pull the 1-minute base"
            ),
        )
        return "stale"
    try:
        if segment == "FNO":
            from brutex.data.fno_daily_resample import resample_1m_to_fno_daily

            bars = resample_1m_to_fno_daily(df_1m, before=before)
        else:
            from brutex.data.spot_daily_resample import resample_1m_to_daily

            bars = resample_1m_to_daily(df_1m, before=before)
        expected_by_date = {_ist_date_of(bar.timestamp): bar for bar in bars}
    except datetime_out_of_range_escape_errors():
        # Refuter round 1 (seat 1, MAJOR): a corrupt out-of-range 1-MINUTE
        # BASE stamp (far-past negative i64 — the #1594 hand-planted class)
        # previously reached the daily law's Python-datetime conversion
        # OUTSIDE the guard and ESCAPED as an uncaught PanicException through
        # the sweep-preflight gate, while the polars-only intraday twin read
        # "stale" gracefully on the SAME base. The ESCAPE set (OverflowError
        # + PanicException — ValueError deliberately EXCLUDED per the
        # consumption law: this try spans the whole daily law, where a
        # ValueError is the genuine-code-bug class that must keep
        # propagating — the corrupt-VALUE shapes that could legitimately
        # raise it, e.g. the R5 negative-volume-wrap CandleBar refusal, are
        # screened out by the round-4 base value screen ABOVE, so a
        # ValueError reaching this span is no longer attributable to a
        # corrupt partition) folds the shape into the same typed
        # fail-closed refusal. Loud: ONE WARN per partition.
        _LOG.warning(
            "derived_1day_out_of_range_stamp",
            source=source or "1day",
            side="1minute-base",
            detail=(
                "1-minute base stamp outside the Python datetime range (a corrupt/"
                "hand-planted i64) — verdict 'stale' fail-closed; heal / re-pull "
                "the 1-minute base"
            ),
        )
        return "stale"

    from datetime import UTC

    def _instant(ts: datetime) -> datetime:
        """Normalize a stamp to an aware-UTC instant (naive = UTC by contract)."""
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)

    has_oi = "open_interest" in df_stored_1day.columns
    orphans: list[str] = []
    verdict = "fresh"
    for row in df_stored_1day.sort("ts_utc").iter_rows(named=True):
        ist_date = _ist_date_of(row["ts_utc"])
        bar = expected_by_date.get(ist_date)
        if bar is None:
            orphans.append(ist_date.isoformat())
            continue
        stored_oi = row["open_interest"] if has_oi else None
        try:
            stored_key = (
                _instant(row["ts_utc"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                int(row["volume"]),
                None if stored_oi is None else int(stored_oi),
            )
        except (TypeError, ValueError, OverflowError):
            # Refuter round 2 (seat 1, MAJOR) + round 3 (seat 2, MAJOR): the
            # stamp guards above vet STAMPS only — a corrupt/hand-planted
            # stored 1day VALUE (a null OHLCV/volume →
            # `float(None)`/`int(None)` TypeError; a NaN-f64 volume/OI →
            # `int(NaN)` ValueError; an ±inf-f64 volume/OI → `int(inf)`
            # OverflowError, a subclass of NEITHER — the round-3 completion)
            # previously ESCAPED these bare conversions as an uncaught raise
            # through the sweep-preflight gate, while the polars-only
            # intraday twin read "stale" gracefully (`.equals`) on the
            # identical corruption. NARROW try — it spans ONLY the
            # stored-row conversions, whose sole failure mode is an
            # unconvertible stored value, so catching (TypeError,
            # ValueError, OverflowError) here honors the #1594 consumption
            # law (a genuine code bug elsewhere keeps propagating). Loud:
            # ONE WARN per partition, fail-closed.
            _LOG.warning(
                "derived_1day_corrupt_value",
                source=source or "1day",
                ist_date=ist_date.isoformat(),
                detail=(
                    "stored 1day OHLCV/volume/OI value is null, NaN- or "
                    "inf-unconvertible (a corrupt/hand-planted partition) — "
                    "verdict 'stale' fail-closed; heal-recall or re-derive "
                    "the partition"
                ),
            )
            return "stale"
        expected_key = (
            _instant(bar.timestamp),
            float(bar.open),
            float(bar.high),
            float(bar.low),
            float(bar.close),
            bar.volume,
            bar.open_interest,
        )
        if stored_key != expected_key:
            verdict = "stale"
    if orphans:
        _LOG.warning(
            "derived_1day_orphan_rows",
            source=source or "1day",
            orphan_dates=orphans[:20],
            orphan_count=len(orphans),
            detail=(
                "stored 1day row(s) whose 1-minute base is gone — observe-only "
                "(latest-wins cannot delete an orphan; re-pull the 1-minute or "
                "heal-recall the partition to converge)"
            ),
        )
    return verdict


def read_lake_bars_frame(symbol_dir: Path, interval: str, *, source: str = "") -> pl.DataFrame:
    """Read ONE stored ``(symbol, interval)`` partition as the canonical compare frame.

    The COMMON lake read the freshness compares run on (CLAUDE.md §11 —
    DEC-SUSPICION-SWEEP-FIXES-001 G1 extracted it from the doctor's
    ``check_derived_freshness`` so the doctor probe and the sweep-preflight gate
    share ONE reader): keep-FIRST read dedup BEFORE the sort
    (DEC-LAKE-READ-UNIQUE-001 — the compare must see the same deduped bars every
    wired reader serves, never a poisoned-era physical duplicate), renamed +
    projected to the resampler's canonical ``ts_utc``/OHLCV columns — plus
    ``open_interest`` when the stored partition carries the column
    (DEC-DERIVED-1DAY-FRESHNESS-001: the OI-aware compare needs it; every
    production partition carries it, so the compares are OI-aware end-to-end)
    — sorted. *source* labels the dedup log event; blank derives
    ``<symbol>/<interval>``.
    """
    import polars as pl

    from brutex.data.lake_read import dedup_bars_frame

    frame, _dropped = dedup_bars_frame(
        pl.read_parquet((symbol_dir / interval).as_posix() + "/**/*.parquet"),
        source=source or f"{symbol_dir.name}/{interval}",
    )
    columns = ["ts_utc", "open", "high", "low", "close", "volume"]
    if "open_interest" in frame.columns:
        columns.append("open_interest")
    return frame.rename({"timestamp": "ts_utc"}).select(*columns).sort("ts_utc")


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
    / ``"stale"``). Since DEC-DERIVED-1DAY-FRESHNESS-001 a requested ``"1day"``
    is keyed too — via the segment-routed :func:`compare_daily_freshness` VALUE
    arm — closing the hole where the derived ``1day`` (the PDH/PDL anchor
    source) was checked NOWHERE. HONEST abstain envelope (Golden Rule #1 —
    never guess):

    * ``1minute`` is never keyed — there is nothing to re-derive;
    * a pair with NO stored derived partition is never keyed — the ZERO-bar
      slice preflight (:func:`~brutex.engine.sweep_preflight.preflight_slices`)
      already owns the missing-data refusal; for ``1day`` this also keeps
      recency-by-absence OUT of the gate (a 1day lagging the 1m base is the
      prebuild's job — disclosed in :func:`compare_daily_freshness`);
    * a symbol with NO stored ``1minute`` base is never keyed — the verdict is
      unknowable without the base (abstain, mirroring the doctor's skip).

    The per-pair 1-minute frame is read ONCE per symbol (shared by the
    intraday AND 1day arms). O(stored bars) per launch — a deliberate
    one-time preflight cost against consuming a stale derived table into run
    identity. The ``1day`` arm's compare is a NON-vectorized per-row Python
    loop (O(stored 1day rows) ≈ one row per trading day per symbol —
    hundreds-to-low-thousands of rows, dwarfed by the intraday frames'
    vectorized ``equals``; see :func:`compare_daily_freshness`'s honest-cost
    note); sweep-LAUNCH only, never a poll.
    """
    from brutex.data.timeframe_materialize import INTRADAY_TF_INTERVALS
    from brutex.data.timeframes import parse_label

    interval_to_label = {v: k for k, v in INTRADAY_TF_INTERVALS.items()}
    verdicts: dict[tuple[str, str], str] = {}
    requested = [i for i in intervals if i in interval_to_label]
    want_daily = "1day" in intervals
    if not requested and not want_daily:
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
                df_1m = read_lake_bars_frame(symbol_dir, "1minute")
            verdicts[(symbol, interval)] = compare_derived_freshness(
                df_1m,
                read_lake_bars_frame(symbol_dir, interval),
                parse_label(interval_to_label[interval]),
            )
        if want_daily:
            daily_dir = symbol_dir / "1day"
            if daily_dir.is_dir() and any(daily_dir.rglob("*.parquet")):
                if df_1m is None:
                    df_1m = read_lake_bars_frame(symbol_dir, "1minute")
                verdicts[(symbol, "1day")] = compare_daily_freshness(
                    df_1m,
                    read_lake_bars_frame(symbol_dir, "1day"),
                    segment=segment,
                    source=f"{symbol}/1day",
                )
    return verdicts
