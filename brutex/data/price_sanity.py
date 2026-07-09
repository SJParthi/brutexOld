"""Vectorized stored-OHLC price-sanity checks (DEC-STORED-DATA-VALIDATION-001).

The ONE frame-level source of truth (CLAUDE.md §11 COMMON) for "is this stored
OHLC row sane?" — the polars twin of the per-bar
:func:`brutex.data.models._validate_price_bar` invariants:

* **positivity** — every price is non-null and ``> 0`` (a traded Indian price
  is never ``<= 0``; tick ≥ 0.05 rupees — the same market fact behind
  ``candle_fields.has_no_price``);
* **ordering** — ``low <= open, close <= high`` (``high >= low``).

Two read surfaces consume it:

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
* :func:`~brutex.data.lake_invariants.scan_lake_invariants` (the census/heal
  scanner) counts violations per month file via
  :func:`count_price_violations`, so ``brutex lake audit-unique`` + the L2/L3
  heal sweeps surface price-corrupt partitions instead of a blind "0
  findings" verdict.

Cost honesty (Golden Rule #10): every check is ONE vectorized polars pass —
O(rows) per frame with O(1) Python state, never a per-row Python loop. The
scanner's per-file cost grows from a 1-column to a 5-column read (same file
count, same single pass — the scan stays O(files) total).

``polars`` is imported inside the functions (the repo's lazy-dep discipline —
the module stays importable wheel-free).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from brutex.data.errors import ResampleInputError

if TYPE_CHECKING:
    import polars as pl

#: The four OHLC price columns every stored bar frame carries.
PRICE_SANITY_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")


def price_violation_expr() -> pl.Expr:
    """One boolean expression flagging a price-insane row (vectorized).

    ``True`` for a row where ANY price is null / non-positive, or the OHLC
    ordering is broken (``high < low``, or ``open``/``close`` outside
    ``[low, high]``). A null price makes the row ``True`` via the explicit
    ``is_null`` arm (polars' three-valued logic folds ``True | null`` to
    ``True``), so the combined mask is never null.

    Returns
    -------
    pl.Expr
        The per-row violation mask over :data:`PRICE_SANITY_COLUMNS`.
    """
    import polars as pl

    open_, high, low, close = (pl.col(name) for name in PRICE_SANITY_COLUMNS)
    checks = [pl.col(name).is_null() | (pl.col(name) <= 0) for name in PRICE_SANITY_COLUMNS]
    checks.extend([high < low, open_ > high, open_ < low, close > high, close < low])
    combined = checks[0]
    for check in checks[1:]:
        combined = combined | check
    return combined


def price_row_insane(
    open_: float | None, high: float | None, low: float | None, close: float | None
) -> bool:
    """Scalar per-row twin of :func:`price_violation_expr` (positivity + ordering).

    The ONE COMMON SCALAR price-sanity predicate (CLAUDE.md §11 COMMON) — the
    row-at-a-time twin of the vectorized :func:`price_violation_expr`, so the
    two are the SAME boolean by construction over the shared input space
    (pinned row-wise by a property test). ``True`` for an INSANE row: ANY
    price ``None`` / ``NaN`` / non-positive (``<= 0``), or the OHLC ordering
    broken (``high < low``, or ``open`` / ``close`` outside ``[low, high]``).

    Needed by the dashboard's scalar screening path
    (:mod:`brutex.data.bar_screen`), where the columnar
    :func:`price_violation_expr` is unavailable (a DuckDB page of Python
    tuples). ``None`` is tested FIRST so a null price short-circuits before any
    ``<=`` / ``<`` comparison (a ``None <= 0`` would raise) — mirroring the
    ``is_null`` arm of the expression.

    DELIBERATE NaN ASYMMETRY (scalar STRICTER — DEC-DASHBOARD-READ-SCREEN-001
    round-2 refuter): a ``NaN`` price is INSANE here but NOT flagged by the
    vectorized :func:`price_violation_expr` (every NaN comparison is False in
    both planes). Without this arm the dashboard would SERVE a NaN bar the
    engine REFUSES — the engine's :class:`~brutex.data.models.CandleBar`
    constructor raises ``decimal.InvalidOperation`` on a NaN price (escaping
    even ``arrow_table_to_bars``' ``except ValueError``), so outcome-parity
    holds: neither surface ever serves a NaN bar. The vectorized twin (the
    engine prebuild read path) + the ``arrow_table_to_bars`` except-clause gap
    are the NAMED ENGINE-side follow-up, deliberately not changed here.

    Parameters
    ----------
    open_, high, low, close
        The four OHLC prices of one bar (rupees; ``None`` for a null cell).

    Returns
    -------
    bool
        ``True`` iff the row violates price positivity or OHLC ordering
        (a ``None`` / ``NaN`` price counts as a violation).
    """
    if open_ is None or high is None or low is None or close is None:
        return True
    if math.isnan(open_) or math.isnan(high) or math.isnan(low) or math.isnan(close):
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
        How many rows violate positivity or OHLC ordering.
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
        When at least one row violates price positivity or OHLC ordering.
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
        f"row(s) (non-positive price or broken OHLC ordering); first offender at "
        f"{first_ts!r}. Refusing to derive timeframes from corrupt input — a "
        f"corrupt 1m bar would launder into self-consistent-looking derived "
        f"buckets (DEC-STORED-DATA-VALIDATION-001). Heal or re-pull the symbol's "
        f"1-minute partition."
    )


__all__ = [
    "PRICE_SANITY_COLUMNS",
    "count_price_violations",
    "price_row_insane",
    "price_violation_expr",
    "validate_frame_price_sanity",
]
