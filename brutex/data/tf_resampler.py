"""Polars-vectorized 1m → 21-TF resampler.

Per `docs/TRACK1_BACKTEST_SPEC.md` §Strategy Specification and
spec §Workload Math (21 timeframes):
- 1m base (no resampling needed)
- 14 minute-multiple intraday: 2m, 3m, ..., 15m
- 5 multi-minute intraday: 30m, 60m, 120m, 180m, 240m
- 1 daily: 1d (full trading-day aggregation)

## Aggregation rules

For each bucket [bucket_start, bucket_start + tf):

| Field    | Rule                                           |
|----------|------------------------------------------------|
| ts_utc   | bucket_start (left-closed, left-labeled)       |
| open     | first row's open (earliest in bucket)          |
| high     | max of all rows' high                          |
| low      | min of all rows' low                           |
| close    | last row's close (latest in bucket)            |
| volume   | sum of all rows' volume                        |

## Open interest (OPTIONAL column — F&O only)

When the input frame carries an ``open_interest`` column (F&O futures /
options 1-minute bars), the bucket's open interest is the **LAST source
bar's** OI — a point-in-time SNAPSHOT (the count of outstanding contracts
at an instant), NEVER summed like the volume FLOW. This is the SAME rule
:func:`brutex.feed.resampler._aggregate` already uses
(``open_interest=last.open_interest``, "snapshot semantics"). A null OI
source bar yields a null bucket OI (preserved, NEVER fabricated as 0). When
the input frame has NO ``open_interest`` column (spot / index, which carry
no OI), the output is byte-identical to before — no OI column is added.

## Anchor

All buckets anchored at 09:15 IST (session open) by FIXED ARITHMETIC
(DEC-RESAMPLE-ANCHOR-002): ``bucket = session_open + floor((ts -
session_open)/tf)*tf``, where ``session_open`` is the bar's IST date at
09:15. The grid depends only on calendar time — a missing 09:15 bar can
NEVER shift it (the prior first-bar anchoring could). A 5m bar at
09:15:00 IST covers [09:15:00, 09:20:00).

For daily TF: one bar per trading day, covering [session_open,
session_close), labeled at the 09:15 IST session open.

## Seconds-keyed grid (DEC-TF-REGISTRY-DYNAMIC-001)

The bucket arithmetic is SECONDS-keyed over
:class:`brutex.data.timeframes.TfSpec` (``duration_s``), so any registry
spec — including a config-added minute TF like 20m, or a sub-minute spec
over second-stamped input — buckets through the ONE
:func:`resample_frame_to_spec` engine. For whole-minute-stamped input the
seconds arithmetic is BIT-IDENTICAL to the historical minute arithmetic
(``offset_s == 60 * offset_minutes`` exactly, and ``floor(60x / 60y) * 60y
== 60 * (floor(x / y) * y)`` under the same polars Int64 floor division) —
pinned by the frozen-oracle parity suite
(``tests/unit/test_tf_resampler_registry_parity.py``). HONEST GATE: no
production feed emits sub-minute bars (Groww is 1-minute-only) — the
sub-minute arm is a framework capability proven on synthetic input only.

## Polars implementation

Uses `pl.DataFrame.group_by_dynamic()` for minute-multiple TFs (Polars
native fast aggregation, all 21 TFs in seconds for a 5-year 1m
dataset).

Per Agent A research (2026-05-16): Polars 100M-row groupby completes
in 23 sec on M-series (medium.com/codastra benchmark). For Track 1
(~500K 1m bars per instrument over 5 years), groupby latency is
single-digit ms per TF.

## Determinism

Polars groupby is deterministic for sorted input. Caller MUST provide
1m DataFrame sorted ascending by `ts_utc` (this function does NOT
re-sort — `df.sort('ts_utc')` is the caller's responsibility) — and that
contract is now ENFORCED, not assumed (DEC-RESAMPLER-INPUT-GUARD-001):
`_validate_input` raises a typed
:class:`~brutex.data.errors.ResampleInputError` on a null, duplicate, or
out-of-order `ts_utc`, because a duplicate silently DOUBLE-COUNTS the
bucket's volume (10+10+5 → 25) and can pollute high/low, and an unsorted
input inverts the bucket's open/close (`first()`/`last()` trust order).
The check is vectorized (one `diff()` pass — O(1) state per row, the
polars twin of the feed resampler's `prev_ts` tracker) and raises BEFORE
any aggregation, so no partial output is ever produced.

No look-ahead — Polars buckets are computed from left-closed
intervals, so bar at time T uses only rows [T, T+tf). Property P-06 holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl

from brutex.data.errors import ResampleInputError
from brutex.data.lake_rust_resample import lake_rust_resample_enabled, resample_frame_via_rust
from brutex.data.models import GREEKS_OVERLAY_FIELDS
from brutex.data.timeframes import ALL_TF_MINUTES, TfAnchor, TfSpec, spec_for_minutes

# Required schema for input 1m DataFrame.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

#: OPTIONAL F&O column carried through with carry-last (snapshot) semantics
#: when present. Spot/index frames omit it and the output stays the OHLCV-6.
OPEN_INTEREST_COLUMN: str = "open_interest"


def _validate_input(df_1m) -> None:  # type: ignore[no-untyped-def]
    """Validate the input DataFrame's schema AND its ``ts_utc`` ordering contract.

    Schema violations keep their historical ``TypeError`` / ``ValueError``
    (back-compat); an ordering violation — a null, DUPLICATE, or OUT-OF-ORDER
    ``ts_utc`` — raises the typed
    :class:`~brutex.data.errors.ResampleInputError`
    (DEC-RESAMPLER-INPUT-GUARD-001). Before this guard the docstring CLAIMED
    sort-order validation the body never did: a duplicate timestamp silently
    double-counted the bucket's volume and could pollute high/low, and an
    unsorted input inverted the bucket's open/close. The check is one
    vectorized ``diff()`` pass (O(1) state per row — the polars twin of the
    feed resampler's ``prev_ts`` tracker) and runs BEFORE any aggregation, so
    the resampler never emits partial output from bad input.
    """
    import polars as pl

    if not isinstance(df_1m, pl.DataFrame):
        raise TypeError(f"Expected polars.DataFrame, got {type(df_1m).__name__}")
    missing = [c for c in REQUIRED_COLUMNS if c not in df_1m.columns]
    if missing:
        raise ValueError(
            f"Input DataFrame missing required columns: {missing}. "
            f"Required: {REQUIRED_COLUMNS}, got: {df_1m.columns}"
        )
    # Verify ts_utc is datetime type
    ts_dtype = df_1m.schema["ts_utc"]
    if not isinstance(ts_dtype, pl.Datetime):
        raise ValueError(
            f"ts_utc must be polars.Datetime, got {ts_dtype}. "
            f"Convert with `pl.col('ts_utc').cast(pl.Datetime)` first."
        )
    ts = df_1m.get_column("ts_utc")
    # A null timestamp cannot be bucketed (and poisons the diff check below).
    if ts.null_count() > 0:
        raise ResampleInputError(
            f"ts_utc contains {ts.null_count()} null timestamp(s) — every 1m bar "
            f"must carry a real ts_utc (a null cannot be bucketed)."
        )
    if len(ts) > 1:
        # STRICTLY-INCREASING guard (DEC-RESAMPLER-INPUT-GUARD-001): one
        # vectorized diff() pass — O(1) state per row, the polars twin of
        # feed/resampler.py's prev_ts tracker. A duplicate ts double-counts
        # the bucket's volume + pollutes high/low; an out-of-order ts inverts
        # open/close (first()/last() + maintain_order=True trust input order).
        # Raises BEFORE any aggregation — never a partial output.
        import datetime as _dt

        diffs = ts.diff().slice(1)
        violations = diffs.filter(diffs <= _dt.timedelta(0))
        if len(violations) > 0:
            offender = df_1m.with_row_index("_row").filter(
                pl.col("ts_utc").diff() <= _dt.timedelta(0)
            )
            row = int(offender.get_column("_row")[0])
            raise ResampleInputError(
                f"ts_utc must be STRICTLY increasing (sorted ascending, no "
                f"duplicates); row {row} has ts_utc "
                f"{ts[row]!r} <= previous {ts[row - 1]!r} "
                f"({len(violations)} violation(s) total). A duplicate "
                f"double-counts the bucket's volume; an out-of-order row "
                f"inverts the bucket's open/close. Sort + de-duplicate the "
                f"1m frame before resampling."
            )


def _resample_validated(df_1m, spec: TfSpec):  # type: ignore[no-untyped-def]
    """Bucket a VALIDATED frame onto *spec*'s grid — the ONE shared engine.

    Callers must run :func:`_validate_input` first. The aggregation rules
    (OHLCV + optional OI carry-last + Greeks-overlay carry-last) are
    byte-identical to the pre-registry implementation; only the bucket
    arithmetic moved from minutes to seconds (DEC-TF-REGISTRY-DYNAMIC-001).
    """
    import polars as pl

    # F&O frames carry an ``open_interest`` column; spot/index do not. Carry it
    # through with carry-last (snapshot) semantics ONLY when present, so the
    # spot output stays byte-identical to the OHLCV-6 (purely additive).
    has_oi = OPEN_INTEREST_COLUMN in df_1m.columns

    # FIXED-ARITHMETIC session anchor (DEC-RESAMPLE-ANCHOR-002, hardening
    # DEC-RESAMPLE-ANCHOR-001): each row's bucket label is its IST date's
    # 09:15 session open plus floor-of-elapsed-time-over-duration whole
    # buckets. Pure column math — the grid depends ONLY on the calendar TIME,
    # never on which bars happen to exist. The prior ``group_by_dynamic(
    # start_by="datapoint")`` anchored each session's grid to its FIRST BAR,
    # so a vendor gap at the 09:15 open silently shifted the WHOLE day's grid
    # for every timeframe (the hazard the 2026-06-11 4-agent review flagged).
    # Buckets can never straddle sessions: every label shares the bar's IST
    # trading date by construction. IST is a fixed +05:30 (no DST), so the
    # arithmetic needs no calendar module for Track 1's intraday TFs.
    ist = pl.col("ts_utc") + pl.duration(hours=5, minutes=30)
    session_open_ist = ist.dt.truncate("1d") + pl.duration(hours=9, minutes=15)

    if spec.anchor is TfAnchor.SESSION_OPEN:
        # SECONDS-keyed bucket arithmetic (DEC-TF-REGISTRY-DYNAMIC-001). For a
        # whole-minute-stamped input this is BIT-IDENTICAL to the pre-registry
        # minute arithmetic: offset_s == 60 * offset_minutes exactly, and
        # floor(60x / 60y) * 60y == 60 * (floor(x / y) * y) under the SAME
        # polars Int64 floor division on both sides — pinned by the frozen
        # oracle-parity suite (test_tf_resampler_registry_parity.py).
        offset_s = (ist - session_open_ist).dt.total_seconds()
        bucket_s = (offset_s // spec.duration_s) * spec.duration_s
        bucket_utc = (session_open_ist + pl.duration(seconds=bucket_s)) - pl.duration(
            hours=5, minutes=30
        )
    else:
        # IST_DAY (the daily TF): one bucket per IST trading day, labeled at
        # the 09:15 IST session open (NOT the first bar — same gap immunity).
        bucket_utc = session_open_ist - pl.duration(hours=5, minutes=30)

    aggregations = [
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
    ]
    if has_oi:
        # Carry-last OI: maintain_order=True over caller-sorted input makes
        # ``.last()`` deterministically the latest-timestamp bar's OI (a STOCK
        # snapshot, never the volume FLOW). A null last bar yields null OI.
        aggregations.append(pl.col(OPEN_INTEREST_COLUMN).last().alias(OPEN_INTEREST_COLUMN))
    # Greeks CLOSE-SNAPSHOT propagation (plan §15.10, semantics LOCKED by
    # DEC-GREEKS-ENGINE-PR1-001; implemented by DEC-GREEKS-ENGINE-PR3-001):
    # when the input frame carries any of the ten stored Greeks-overlay
    # columns (an ENRICHED option 1-minute read — DEC-GREEKS-ENGINE-PR2B-001),
    # each propagates by LAST-VALUE — a derived bar's iv/delta/gamma/theta/
    # vega/rho (+ inputs + provenance stamp) are EXACTLY its last constituent
    # 1-minute bar's stored values. Greeks are STATE, not FLOW: never summed,
    # never averaged, never OHLC-aggregated — the same rule ``close`` and OI
    # already obey, so it is zero-recompute, O(1) per derived bar, and
    # bit-identical across every TF. A NULL last constituent (an honest §15.8
    # stamped-NA minute) stays NULL — NEVER backfilled from an earlier
    # constituent minute (that would be a stale-spot fabrication). Spot /
    # futures / legacy frames carry none of these columns, so the output is
    # byte-identical for them (purely additive, exactly like the OI branch).
    for overlay_column in GREEKS_OVERLAY_FIELDS:
        if overlay_column in df_1m.columns:
            aggregations.append(pl.col(overlay_column).last().alias(overlay_column))

    return (
        df_1m.with_columns(bucket_utc.alias("_bucket_ts"))
        .group_by("_bucket_ts", maintain_order=True)
        .agg(*aggregations)
        .rename({"_bucket_ts": "ts_utc"})
        .sort("ts_utc")
    )


def resample_frame_to_spec(df_1m, spec: TfSpec):  # type: ignore[no-untyped-def]
    """Resample a base-granularity bar DataFrame onto *spec*'s bucket grid.

    The registry-driven entry (DEC-TF-REGISTRY-DYNAMIC-001): *spec* is the
    authority — any :class:`~brutex.data.timeframes.TfSpec` buckets here,
    including one NOT in the default 21-TF registry (a config-added ``20m``,
    or a sub-minute spec over second-stamped input). Unlike
    :func:`resample_1m_to_tf` there is NO identity shortcut — the frame is
    always bucketed onto *spec*'s grid.

    HONEST GATE (Golden Rule #10): sub-minute bucketing is a FRAMEWORK
    capability proven on synthetic second-stamped input only — no production
    feed emits sub-minute bars (Groww is 1-minute-only), so no production
    caller passes a sub-minute spec today.
    """
    _validate_input(df_1m)
    return _resample_validated(df_1m, spec)


def resample_1m_to_tf(df_1m, target_tf_minutes: int):  # type: ignore[no-untyped-def]
    """Resample a 1-minute bar DataFrame to a target timeframe.

    Args:
        df_1m: Polars DataFrame with columns [ts_utc, open, high, low,
               close, volume] and OPTIONALLY ``open_interest`` (F&O). ts_utc
               must be Datetime dtype. Caller must sort by ts_utc ascending
               before calling.
        target_tf_minutes: Target timeframe in minutes. Must be one of
            the 21 TFs in `brutex.data.timeframes.ALL_TF_MINUTES`.
            Special value 375 = daily (1d).

    Returns:
        Polars DataFrame with the same OHLCV column schema, resampled to the
        target TF. When the input carried an ``open_interest`` column it is
        carried through as the bucket's LAST source bar's OI (snapshot
        semantics, never summed — F&O only); when it did not, the output is
        byte-identical to the spot 6-column result (no OI column added).
        Bucket timestamps are left-closed, left-labeled.

    Examples:
        Two 1-minute bars at the 09:15 IST session open (03:45 UTC — IST is
        a fixed +05:30) collapse into ONE bucket at every coarser TF:

        >>> import datetime as dt
        >>> import polars as pl
        >>> df_1m = pl.DataFrame(
        ...     {
        ...         "ts_utc": [
        ...             dt.datetime(2026, 1, 5, 3, 45, tzinfo=dt.timezone.utc),
        ...             dt.datetime(2026, 1, 5, 3, 46, tzinfo=dt.timezone.utc),
        ...         ],
        ...         "open": [100.0, 101.0],
        ...         "high": [102.0, 103.0],
        ...         "low": [99.0, 100.0],
        ...         "close": [101.0, 102.0],
        ...         "volume": [10, 20],
        ...     }
        ... )
        >>> resample_1m_to_tf(df_1m, 5).height
        1
        >>> resample_1m_to_tf(df_1m, 15).height
        1
        >>> resample_1m_to_tf(df_1m, 375).height
        1

        The bucket aggregates OHLCV per the table above — close is the LAST
        bar's close, volume the SUM:

        >>> df_5m = resample_1m_to_tf(df_1m, 5)
        >>> float(df_5m.get_column("close")[0])
        102.0
        >>> int(df_5m.get_column("volume")[0])
        30
    """
    _validate_input(df_1m)
    if target_tf_minutes not in ALL_TF_MINUTES:
        raise ValueError(
            f"Unsupported target TF: {target_tf_minutes} minutes. "
            f"Must be one of {sorted(ALL_TF_MINUTES)}."
        )
    if lake_rust_resample_enabled():
        # ADR-0020 Wave-B seam (DEC-LAKE-RUST-RESAMPLE-001, default OFF):
        # route the validated frame through the brutex_lake crate, with the
        # verbatim Python body below as the LIVE parity oracle — every
        # guard/degrade/burn-in-mismatch path serves it, so resampled
        # values never depend on the wheel being present or correct.
        return resample_frame_via_rust(df_1m, target_tf_minutes, oracle=_resample_1m_to_tf_python)
    return _resample_1m_to_tf_python(df_1m, target_tf_minutes)


def _resample_1m_to_tf_python(df_1m, target_tf_minutes: int):  # type: ignore[no-untyped-def]
    """The verbatim pre-seam resample body (Slice-0-style decomposition).

    Extracted token-for-token from :func:`resample_1m_to_tf` by
    DEC-LAKE-RUST-RESAMPLE-001 so the public fn can gate the Rust seam
    while the OFF (default) path stays byte-identical — AND the live
    parity oracle every seam degrade path serves. Assumes an
    ALREADY-validated frame and an ``ALL_TF_MINUTES`` member (the public
    fn's contract; never call this directly from production code).
    """
    # 1m -> 1m is a no-op (shallow copy preserves immutability semantics) —
    # it carries WHATEVER columns the input has (OI present for F&O, absent
    # for spot), unchanged.
    if target_tf_minutes == 1:
        return df_1m.clone()

    return _resample_validated(df_1m, spec_for_minutes(target_tf_minutes))


def resample_all_21_tfs(df_1m) -> dict[int, pl.DataFrame]:  # type: ignore[no-untyped-def]
    """Resample 1m bars to all 21 timeframes in one call.

    Args:
        df_1m: 1m Polars DataFrame, sorted by ts_utc.

    Returns:
        Dict mapping `tf_minutes -> DataFrame`. Includes 1m as identity.

    Example:
        all_tfs = resample_all_21_tfs(df_1m)
        df_5m = all_tfs[5]
        df_daily = all_tfs[375]
    """
    return {tf: resample_1m_to_tf(df_1m, tf) for tf in ALL_TF_MINUTES}
