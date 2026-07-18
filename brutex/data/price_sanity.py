"""Vectorized stored-OHLC price-sanity checks (DEC-STORED-DATA-VALIDATION-001).

The ONE frame-level source of truth (CLAUDE.md §11 COMMON) for "is this stored
OHLC row sane?" — the polars twin of the per-bar
:func:`brutex.data.models._validate_price_bar` invariants:

* **positivity** — every price is non-null and ``> 0`` (a traded Indian price
  is never ``<= 0``; tick ≥ 0.05 rupees — the same market fact behind
  ``candle_fields.has_no_price``);
* **ordering** — ``low <= open, close <= high`` (``high >= low``);
* **finiteness** — every price is finite
  (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001): ``inf``/``-inf``/``NaN`` in ANY
  OHLC field is a violation. Pre-fix the vectorized mask was BLIND to
  ``high ∈ {+inf, NaN}`` — ``high >= low`` holds vacuously for ``+inf`` and
  polars' total float ordering places ``NaN`` above everything, so neither
  the ordering nor the ``<= 0`` arm ever tripped (executed-repro'd by the
  assurance session: a planted ``high = inf`` 1m bar laundered a derived
  5-minute bucket ``high = inf`` through the max aggregation). No production
  writer emits a non-finite price (the vendor parse rejects it via
  ``candle_fields.to_price``) — this arm only fires on planted/out-of-band
  corrupt stored data.

Four read surfaces consume the frame-level gate directly (and the strengthened
law flows TRANSITIVELY into every caller of these seams):

* :func:`~brutex.data.timeframe_materialize.make_lake_read_1m` (the prebuild's
  1-minute read boundary) calls :func:`validate_frame_price_sanity` so a
  corrupt 1-minute bar can never LAUNDER into a self-consistent-looking
  derived bucket across the 19 intraday TFs + the ``1day`` (the round-2 audit
  live-reproduced a ``high < low`` 1m bar emitting a derived ``low: 0.0``
  silently, while the SAME bar raises loudly on a direct 1m read). The raised
  :class:`~brutex.data.errors.ResampleInputError` is already in the prebuild's
  per-symbol quarantine set (``gated_prebuild._per_symbol_data_errors``), so
  the symbol is quarantined + NAMED and its derived TFs are NOT written —
  never a stage abort, never a silent drop/fix of rows.
* :func:`~brutex.data.spot_daily_resample.make_lake_read_1m_window` (the
  window-bounded 1-minute reader behind the post-pull ``1day`` derive, the
  heal-recall and the crossverify prebuild leg) runs the SAME
  :func:`validate_frame_price_sanity` gate on the window-cut frame
  (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001) — pre-fix it ran NO screen at
  all, so a corrupt bar yielded a CLEAN derived ``1day`` via the full
  prebuild but a POISONED ``1day`` via the window path.
* the ``/movers`` raw-1m read seam
  (``brutex.dashboard.movers_snapshot._read_cut_1m_frame``) runs
  :func:`validate_frame_price_sanity` behind the
  :func:`~brutex.data.bar_screen.screen_frame` row-drop
  (DEC-DASHBOARD-READ-SCREEN-001 + the DEC-READ-GUARD-PARITY-001 belt), and
  ``bar_screen``'s scalar path rides :func:`price_row_insane` — so the
  ``/movers`` + ``/db`` display reads inherit the non-finite arm
  transitively.
* :func:`~brutex.data.lake_invariants.scan_lake_invariants` (the census/heal
  scanner) counts violations per month file via
  :func:`count_price_violations`, so ``brutex lake audit-unique`` + the L2/L3
  heal sweeps surface price-corrupt partitions instead of a blind "0
  findings" verdict — a non-finite row now COUNTS there too
  (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001; census-pinned).

Cost honesty (Golden Rule #10): every check is ONE vectorized polars pass —
O(rows) per frame with O(1) Python state, never a per-row Python loop. The
scanner's per-file cost grows from a 1-column to a 5-column read (same file
count, same single pass — the scan stays O(files) total).

``polars`` is imported inside the functions (the repo's lazy-dep discipline —
the module stays importable wheel-free).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING, Final

from brutex.data.errors import ResampleInputError

if TYPE_CHECKING:
    import polars as pl

#: The four OHLC price columns every stored bar frame carries.
PRICE_SANITY_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")

#: Exact epoch offsets (microseconds) of ``datetime.min`` / ``datetime.max`` —
#: the Python-representable datetime range every stored ``Datetime`` value must
#: convert into. Pure integer ``timedelta`` arithmetic (never float seconds), so
#: both bounds are EXACT (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001).
_EPOCH_NAIVE: Final[datetime] = datetime(1970, 1, 1)


def _epoch_offset_us(instant: datetime) -> int:
    """*instant*'s exact epoch offset in whole microseconds (naive wall-clock)."""
    delta = instant - _EPOCH_NAIVE
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


_DATETIME_MIN_US: Final[int] = _epoch_offset_us(datetime.min)
_DATETIME_MAX_US: Final[int] = _epoch_offset_us(datetime.max)

#: Per-``polars.Datetime`` time-unit physical (i64 epoch-offset) bounds of the
#: Python-representable datetime range. ``ms`` floors ``datetime.max`` to the
#: last whole millisecond (any ms value ≤ it round-trips; ``datetime.min`` is a
#: whole day, so its floor division is exact); ``ns`` bounds exceed i64 — every
#: storable ns value is representable, the check is vacuously true there.
_TS_UNIT_BOUNDS: Final[dict[str, tuple[int, int]]] = {
    "us": (_DATETIME_MIN_US, _DATETIME_MAX_US),
    "ms": (_DATETIME_MIN_US // 1_000, _DATETIME_MAX_US // 1_000),
    "ns": (_DATETIME_MIN_US * 1_000, _DATETIME_MAX_US * 1_000),
}


def price_violation_expr() -> pl.Expr:
    """One boolean expression flagging a price-insane row (vectorized).

    ``True`` for a row where ANY price is null / non-finite / non-positive,
    or the OHLC ordering is broken (``high < low``, or ``open``/``close``
    outside ``[low, high]``). A null price makes the row ``True`` via the
    explicit ``is_null`` arm (polars' three-valued logic folds ``True | null``
    to ``True``), so the combined mask is never null. The per-column
    ``~is_finite`` arm (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001) closes the
    ``high ∈ {+inf, NaN}`` blind spot: ``+inf``/``NaN`` pass the ordering and
    ``<= 0`` checks VACUOUSLY (polars total float ordering), so the mask was
    blind to a corrupt HIGH.

    Returns
    -------
    pl.Expr
        The per-row violation mask over :data:`PRICE_SANITY_COLUMNS`.
    """
    import polars as pl

    open_, high, low, close = (pl.col(name) for name in PRICE_SANITY_COLUMNS)
    checks = [
        pl.col(name).is_null() | ~pl.col(name).is_finite() | (pl.col(name) <= 0)
        for name in PRICE_SANITY_COLUMNS
    ]
    checks.extend([high < low, open_ > high, open_ < low, close > high, close < low])
    combined = checks[0]
    for check in checks[1:]:
        combined = combined | check
    return combined


def price_row_insane(
    open_: float | None, high: float | None, low: float | None, close: float | None
) -> bool:
    """Scalar per-row twin of :func:`price_violation_expr` (positivity + ordering + finiteness).

    The ONE COMMON SCALAR price-sanity predicate (CLAUDE.md §11 COMMON) — the
    row-at-a-time twin of the vectorized :func:`price_violation_expr`, so the
    two are the SAME boolean by construction over the shared input space
    (pinned row-wise by a property test). ``True`` for an INSANE row: ANY
    price ``None`` / non-finite (``NaN`` / ``±inf``) / non-positive
    (``<= 0``), or the OHLC ordering broken (``high < low``, or ``open`` /
    ``close`` outside ``[low, high]``).

    Needed by the dashboard's scalar screening path
    (:mod:`brutex.data.bar_screen`), where the columnar
    :func:`price_violation_expr` is unavailable (a DuckDB page of Python
    tuples). ``None`` is tested FIRST so a null price short-circuits before any
    ``<=`` / ``<`` comparison (a ``None <= 0`` would raise) — mirroring the
    ``is_null`` arm of the expression.

    NON-FINITE ARM (DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001, closing the
    DEC-DASHBOARD-READ-SCREEN-001 round-2 "DELIBERATE NaN ASYMMETRY"):
    historically the scalar carried a NaN-only arm the vectorized
    :func:`price_violation_expr` lacked, and this docstring recorded the
    vectorized twin + the ``arrow_table_to_bars`` except-clause gap as the
    NAMED ENGINE-side follow-up. That follow-up is now CLOSED: BOTH planes
    flag any non-finite price (the scalar arm widened ``isnan`` →
    ``not isfinite``, so a ``+inf`` HIGH — previously sane here because
    ``inf`` passes every ordering compare — is insane too), the vectorized
    twin carries the mirrored per-column ``~is_finite`` arm, and the engine
    boundary rejects non-finite explicitly + re-wraps
    ``decimal.InvalidOperation`` instead of letting it escape. Scalar ==
    vector now holds over the FULL float plane (NaN/±inf included — the
    parity property test's input space was consciously widened).

    NON-NUMERIC GUARD (round-4 fold, scalar-only — unlike the non-finite
    arm above, which is now mirrored in BOTH planes): a
    NON-NUMERIC price cell — a ``str`` from a DuckDB ``union_by_name`` VARCHAR
    promotion over an out-of-band parquet (the lake writers never produce
    one), or a ``bool`` (``isinstance(True, int)`` is True, but a boolean
    "price" is junk — deliberately classified insane, never coerced to
    0.0/1.0) — is INSANE, never a raised ``TypeError``. The dashboard callers
    feed RAW page cells (typed ``Any`` at runtime despite this signature), and
    without this arm ``math.isnan(str)`` raised through ``screen_rows`` into a
    /db page 500 — internally inconsistent with
    :func:`brutex.data.bar_screen.screen_rows`'s ``_sortable_cell``, which
    already orders such junk cells last. Conservative in direction: DROP,
    never fabricate. The vectorized twin is untouched (its columns are typed —
    a VARCHAR-promoted column never reaches ``price_violation_expr`` as a
    numeric plane), and non-numerics are structurally outside the
    scalar==vector parity pin's input space (floats only).

    Parameters
    ----------
    open_, high, low, close
        The four OHLC prices of one bar (rupees; ``None`` for a null cell).

    Returns
    -------
    bool
        ``True`` iff the row violates price finiteness, positivity or OHLC
        ordering (a ``None`` / ``NaN`` / ``±inf`` price counts as a
        violation).
    """
    if open_ is None or high is None or low is None or close is None:
        return True
    # Non-numeric junk (str/bool — see the docstring) is insane, never a raise;
    # bool is excluded explicitly (a bool IS an int to isinstance) as junk.
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in (open_, high, low, close)
    ):
        return True
    if not all(math.isfinite(v) for v in (open_, high, low, close)):
        return True
    if open_ <= 0 or high <= 0 or low <= 0 or close <= 0:
        return True
    return high < low or open_ > high or open_ < low or close > high or close < low


def count_price_violations(df: pl.DataFrame) -> int:
    """Count the price-insane rows of *df* (0 for an empty frame).

    Parameters
    ----------
    df
        A frame carrying every :data:`PRICE_SANITY_COLUMNS` column.

    Returns
    -------
    int
        How many rows violate finiteness, positivity or OHLC ordering.
    """
    if df.height == 0:
        return 0
    return df.filter(price_violation_expr()).height


def validate_frame_price_sanity(
    df: pl.DataFrame, *, ts_column: str = "ts_utc", context: str = ""
) -> None:
    """Raise LOUDLY when *df* holds a price-insane row — else return ``None``.

    The prebuild read-boundary gate: mirrors the shape of
    ``tf_resampler._validate_input``'s ordering guard (vectorized detect →
    ONE typed :class:`~brutex.data.errors.ResampleInputError` naming the
    first offender), extended to the price plane. The error names *context*
    (the partition coordinates), the first offending bar's ``ts_column``
    value, and the violation count — never a silent drop or an in-place
    "fix" (Golden Rule #1: repairing a price would fabricate data).

    Parameters
    ----------
    df
        The 1-minute frame to validate (must carry
        :data:`PRICE_SANITY_COLUMNS`; *ts_column* is optional — an absent
        timestamp column degrades the message, never the detection).
    ts_column
        The frame's timestamp column name (the lake readers use ``ts_utc``).
    context
        Human-readable origin (e.g. ``"NSE/INDEX/NSE-NIFTY/1minute"``)
        woven into the error message.

    Raises
    ------
    ResampleInputError
        When at least one row violates price finiteness, positivity or OHLC
        ordering.
    """
    if df.height == 0:
        return
    violations = df.filter(price_violation_expr())
    if violations.height == 0:
        return
    first_ts = (
        violations.get_column(ts_column)[0] if ts_column in violations.columns else "<unknown>"
    )
    origin = f" in {context}" if context else ""
    raise ResampleInputError(
        f"stored 1-minute bars{origin} carry {violations.height} price-insane "
        f"row(s) (non-finite or non-positive price, or broken OHLC ordering); "
        f"first offender at "
        f"{first_ts!r}. Refusing to derive timeframes from corrupt input — a "
        f"corrupt 1m bar would launder into self-consistent-looking derived "
        f"buckets (DEC-STORED-DATA-VALIDATION-001). Heal or re-pull the symbol's "
        f"1-minute partition."
    )


def validate_frame_ts_datetime_range(
    df: pl.DataFrame, *, ts_column: str = "ts_utc", context: str = ""
) -> None:
    """Raise LOUDLY when *df*'s timestamp column holds an out-of-``datetime``-range value.

    The read-boundary TIMESTAMP twin of :func:`validate_frame_price_sanity`
    (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001, porting the #1594 round-18
    out-of-range law to the SPOT derive readers): a stored ``Datetime`` column
    can physically hold an i64 value no Python ``datetime`` can represent
    (e.g. a hand-planted ``2**62``-microsecond stamp — ~year 148108, far past
    ``datetime.max``). Such a value survives the vectorized resample
    (``group_by_dynamic`` never converts to Python) and then PANICS the
    Rust→Python conversion at ``Series.to_list()`` /
    ``Series.__getitem__`` as ``pyo3_runtime.PanicException`` — a
    **BaseException** that sails past every ``except Exception`` per-symbol
    quarantine (executed-repro'd: ONE poisoned symbol aborted the WHOLE
    crossverify prebuild leg, every later symbol skipped). This gate refuses
    the frame FIRST, deterministically, with the typed quarantining
    :class:`~brutex.data.errors.ResampleInputError`.

    Mechanism (one vectorized pass, no exception-shape dependence — the
    integer-bounds arm of the two #1594 round-18 idioms):
    ``Series.to_physical()`` views the column as raw i64 epoch offsets (never
    a Python ``datetime`` conversion, so the probe itself can NEVER panic) and
    compares its min/max against the EXACT Python-representable bounds for the
    column's time unit (:data:`_TS_UNIT_BOUNDS`). Nulls are skipped
    (min/max ignore them — the resampler's own null guard owns that class);
    an absent *ts_column*, an all-null column, or a non-``Datetime`` dtype
    (out-of-band shapes no lake writer produces) return without raising —
    downstream typed handling owns those. Boundary-EXACT: a stored
    ``datetime.max`` / ``datetime.min`` passes (both convert cleanly).

    Callers run this BEFORE :func:`validate_frame_price_sanity` — the price
    gate's first-offender message indexes the timestamp column
    (``violations[ts_column][0]``), which would itself panic on an
    out-of-range value.

    Parameters
    ----------
    df
        The frame to validate (any frame carrying a ``Datetime`` *ts_column*).
    ts_column
        The frame's timestamp column name (the lake readers use ``ts_utc``).
    context
        Human-readable origin (e.g. ``"NSE/INDEX/NIFTY/1minute"``) woven into
        the error message.

    Raises
    ------
    ResampleInputError
        When at least one timestamp lies outside the Python-representable
        datetime range for the column's unit.
    """
    import polars as pl

    if df.height == 0 or ts_column not in df.columns:
        return
    dtype = df.schema[ts_column]
    if not isinstance(dtype, pl.Datetime):
        return
    low, high = _TS_UNIT_BOUNDS[dtype.time_unit]
    physical = df.get_column(ts_column).to_physical()
    ts_min = physical.min()
    ts_max = physical.max()
    if not isinstance(ts_min, int) or not isinstance(ts_max, int):
        # All-null timestamps (min/max read None) — the resampler's own null
        # guard owns that class. The isinstance narrowing is also the typing
        # truth: an Int64 physical view's min/max is a Python int or None.
        return
    if low <= ts_min and ts_max <= high:
        return
    offenders = physical.filter((physical < low) | (physical > high))
    origin = f" in {context}" if context else ""
    raise ResampleInputError(
        f"stored bars{origin} carry {offenders.len()} timestamp(s) outside the "
        f"representable datetime range; first offender physical value "
        f"{offenders[0]} ({dtype.time_unit!r} since epoch). Refusing to derive "
        f"timeframes from corrupt input — an out-of-range stored timestamp "
        f"panics the Rust→Python datetime conversion (pyo3 PanicException, a "
        f"BaseException no `except Exception` guard stops) "
        f"(DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001). Heal or re-pull the "
        f"symbol's 1-minute partition."
    )


__all__ = [
    "PRICE_SANITY_COLUMNS",
    "count_price_violations",
    "price_row_insane",
    "price_violation_expr",
    "validate_frame_price_sanity",
    "validate_frame_ts_datetime_range",
]
