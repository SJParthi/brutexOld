"""The ONE read-side duplicate-collapse chokepoint for the BruteX lake (DEC-LAKE-READ-UNIQUE-001).

DEC-LAKE-WRITE-UNIQUE-001 (dup-train PR-1, MIGRATION.md #1139) made the WRITE
side duplicate-proof — but bars poisoned BEFORE that fix still sit physically
duplicated on disk, and every reader that consumed them silently was an engine
corruption vector: a forensic engine audit measured ONE duplicate bar corrupting
the condition masks of every subsequent bar (24/60 bits), flipping hit counts up
to 60×, changing trades/Sharpe on 25/60 combos, and reordering the strategy
leaderboard. This module is the COMMON (CLAUDE.md §11) read-side gate every lake
reader routes through, so a stored duplicate can never reach the engine, a
dashboard page, or an options leg price again.

## Policy — keep-FIRST, deterministic, never silent

* **keep-FIRST**: for a duplicated identity key the FIRST physical occurrence
  wins (input order for in-memory sequences / frames via
  ``unique(keep="first", maintain_order=True)``; ``ORDER BY filename,
  file_row_number`` for the DuckDB pager fragments). This matches the write
  side's heal rule (:meth:`brutex.data.parquet_writer.ParquetWriter` keep-first
  collapse, DEC-LAKE-WRITE-UNIQUE-001 / MIGRATION.md #1139) so the read gate and
  a later at-rest heal converge on the SAME bars, bit-for-bit (Golden Rule #7).
* **Dedup BEFORE sort**: a reader that sorts must collapse first — sorting can
  reorder equal-timestamp rows, which would make "first" non-deterministic.
* **Never silent** (Golden Rule #1): every collapse emits ONE aggregated
  :data:`DUPLICATES_DROPPED_EVENT` WARNING carrying the reader's *source* label,
  the ``dropped`` row count, and ``conflict_count`` (dropped rows whose values
  DIFFER from the kept row — a same-values re-statement is a benign dup, a
  conflicting one is evidence of a poisoned-era partition worth healing).

## Identity keys

* Spot / F&O candle lake bars: ``timestamp`` (one bar per instant per series).
* Expired-options rolling series rows: ``(side, timestamp)`` — the composite the
  writer itself dedups on (:mod:`brutex.data.option_parquet_writer`).

## O(1) discipline (Golden Rule #5)

Every helper is a single O(n) hash pass over the rows it is handed — O(1) per
bar, no growing rescans; the DuckDB fragments push the collapse into the same
single scan the page/count query already performs. The conflict recount is paid
ONLY when at least one duplicate was actually dropped (the clean fast path is
one ``unique``/dict pass).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, Protocol

import structlog

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    import polars as pl


class _TimestampedBar(Protocol):
    """Any frozen bar row keyed by ``.timestamp`` (CandleBar / OptionBar).

    Value equality (``==``) on the whole row decides conflict vs benign
    re-statement — both bar models are frozen dataclasses with field equality.
    """

    @property
    def timestamp(self) -> datetime:
        """The bar's identity instant (the dedup key)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


_LOG = structlog.get_logger(__name__)

#: The ONE aggregated read-side dedup warning (DEC-LAKE-READ-UNIQUE-001). Every
#: wired reader emits it via :func:`log_read_duplicates` — one event per read,
#: never one per row, so a deeply-poisoned partition cannot log-storm.
DUPLICATES_DROPPED_EVENT: Final[str] = "lake.read.duplicates_dropped"

#: Bare SQL identifier grammar for the DuckDB fragment builders — the key
#: columns are hardcoded literals at every call site (``timestamp`` / ``side``),
#: never user text; this guard makes that structurally enforced.
_SQL_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: The ``read_parquet`` source fragment a deduped pager query MUST scan from:
#: ``filename`` + ``file_row_number`` give :func:`dedup_qualify_sql` its
#: deterministic keep-FIRST physical order (earliest file, earliest row). The
#: two synthetic columns never reach the output — every consumer projects an
#: explicit column list.
DEDUP_PARQUET_SOURCE_SQL: Final[str] = (
    "read_parquet(?, union_by_name=true, filename=true, file_row_number=true)"
)

#: The ``SELECT *`` replacement for a query scanning
#: :data:`DEDUP_PARQUET_SOURCE_SQL`: excludes the two synthetic ordering columns
#: so the projected page schema stays byte-identical to a plain
#: ``read_parquet`` ``SELECT *`` (DuckDB ``EXCLUDE``, supported by the pinned
#: 1.5.x engine — integration-pinned).
DEDUP_STAR_SELECT_SQL: Final[str] = "* EXCLUDE (filename, file_row_number)"


def log_read_duplicates(*, source: str, dropped: int, conflicts: int) -> None:
    """Emit the ONE aggregated read-dedup WARNING — a no-op when nothing dropped.

    Parameters
    ----------
    source
        The reader's label (e.g. ``"catalog:NSE/INDEX/NSE-NIFTY/1minute"``) so
        the operator can locate the poisoned partition.
    dropped
        How many duplicate rows the read collapsed (``<= 0`` → no log).
    conflicts
        How many of the dropped rows carried DIFFERENT values than the kept row
        (the same-ts-different-values policy: keep-FIRST deterministic, and the
        conflict count is surfaced — consistent with the write side's heal rule,
        DEC-LAKE-WRITE-UNIQUE-001 / MIGRATION.md #1139).
    """
    if dropped <= 0:
        return
    _LOG.warning(
        DUPLICATES_DROPPED_EVENT,
        source=source,
        dropped=dropped,
        conflict_count=conflicts,
    )


def dedup_bars[BarT: _TimestampedBar](
    bars: Iterable[BarT], *, source: str
) -> tuple[list[BarT], int]:
    """Collapse duplicate-``timestamp`` bars keep-FIRST; return ``(clean, dropped)``.

    Input order is preserved (dedup happens BEFORE any caller sort — see the
    module policy); the first occurrence of each timestamp wins. A dropped bar
    whose fields differ from the kept bar counts as a conflict in the aggregated
    :data:`DUPLICATES_DROPPED_EVENT` WARNING. One O(1) dict probe per bar.

    Parameters
    ----------
    bars
        The bar sequence as read (any frozen bar type keyed by ``.timestamp``).
    source
        The reader label woven into the warning.

    Returns
    -------
    tuple[list, int]
        The de-duplicated bars (original order) and the dropped-row count.
    """
    first_by_ts: dict[datetime, BarT] = {}
    clean: list[BarT] = []
    dropped = 0
    conflicts = 0
    for bar in bars:
        kept = first_by_ts.get(bar.timestamp)
        if kept is None:
            first_by_ts[bar.timestamp] = bar
            clean.append(bar)
            continue
        dropped += 1
        if bar != kept:
            conflicts += 1
    log_read_duplicates(source=source, dropped=dropped, conflicts=conflicts)
    return clean, dropped


def bars_by_ts_first[BarT: _TimestampedBar](
    bars: Iterable[BarT], *, source: str
) -> dict[datetime, BarT]:
    """Build a keep-FIRST ``{timestamp: bar}`` map — the ``{b.timestamp: b}`` replacement.

    A plain dict comprehension over bars is LAST-wins on a duplicate timestamp
    (the silent poison path the engine audit measured in ``ts_to_bar`` /
    ``ReferenceSeries.from_bars``); this is the COMMON keep-first, warned
    replacement. Same warning contract as :func:`dedup_bars`.
    """
    clean, _dropped = dedup_bars(bars, source=source)
    return {bar.timestamp: bar for bar in clean}


def dedup_bars_frame(
    df: pl.DataFrame, *, source: str, subset: Sequence[str] = ("timestamp",)
) -> tuple[pl.DataFrame, int]:
    """Collapse duplicate-key rows in a polars frame keep-FIRST; ``(clean, dropped)``.

    ``unique(subset=…, keep="first", maintain_order=True)`` runs BEFORE any
    caller sort (the module policy), so the first PHYSICAL row wins
    deterministically. The conflict count (distinct full rows beyond distinct
    keys) is computed only when at least one row was dropped — the clean fast
    path is a single ``unique`` pass.

    Parameters
    ----------
    df
        The frame as read from the lake.
    source
        The reader label woven into the warning.
    subset
        The identity key columns — ``("timestamp",)`` for candle bars,
        ``("side", "timestamp")`` for expired-options rolling rows.

    Returns
    -------
    tuple[polars.DataFrame, int]
        The de-duplicated frame (original row order) and the dropped-row count.
    """
    clean = df.unique(subset=list(subset), keep="first", maintain_order=True)
    dropped = df.height - clean.height
    if dropped > 0:
        # Distinct full rows beyond distinct keys = keys that carried at least
        # one DIFFERING duplicate value-row (an exact re-statement collapses to
        # the same full row, contributing 0). Paid only on the poisoned path.
        conflicts = max(0, df.unique(maintain_order=True).height - clean.height)
        log_read_duplicates(source=source, dropped=dropped, conflicts=conflicts)
    return clean, dropped


def _quoted_key_columns(key_columns: Sequence[str]) -> list[str]:
    """Validate + double-quote the identity key columns for a SQL fragment.

    Raises
    ------
    ValueError
        On an empty key set or a column that is not a bare SQL identifier —
        the keys are hardcoded literals at every call site, so any other shape
        is a wiring bug, refused loudly (never interpolated).
    """
    if not key_columns:
        raise ValueError("dedup key_columns must be non-empty")
    quoted: list[str] = []
    for column in key_columns:
        if not _SQL_IDENTIFIER_RE.match(column):
            raise ValueError(f"dedup key column {column!r} is not a bare SQL identifier")
        quoted.append(f'"{column}"')
    return quoted


def dedup_qualify_sql(key_columns: Sequence[str] = ("timestamp",)) -> str:
    """Return the ``QUALIFY`` fragment that keep-FIRST dedups a pager scan.

    Appends after the ``WHERE`` clause (and before ``ORDER BY``) of a query
    whose source is :data:`DEDUP_PARQUET_SOURCE_SQL`::

        row_number() OVER (PARTITION BY <keys> ORDER BY filename, file_row_number) = 1

    — the first physical row per identity key wins (earliest file, earliest row
    within it), the deterministic keep-FIRST twin of :func:`dedup_bars_frame`.
    Verified against the pinned DuckDB (1.5.x supports ``QUALIFY`` +
    ``file_row_number``; the integration tests pin it against the real engine).

    HONEST envelope (Golden Rule #10): the QUALIFY window is an O(total physical
    rows) columnar hash-partition per query plus O(file-count) Parquet footer
    opens — NOT O(page). A ``LIMIT``/``OFFSET`` appended after the ``ORDER BY``
    bounds only the caller's Python materialisation, never the scan; this is the
    same envelope :func:`~brutex.dashboard.db_view.query_candles` accepts
    (DEC-DB-OPAGE-HONESTY-001).

    Raises
    ------
    ValueError
        Via :func:`_quoted_key_columns` on a malformed key set.
    """
    keys = ", ".join(_quoted_key_columns(key_columns))
    return (
        f" QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY filename, file_row_number) = 1"
    )


def dedup_qualify_latest_sql(key_columns: Sequence[str]) -> str:
    """Return the ``QUALIFY`` fragment that keep-LAST folds a base+delta scan.

    The keep-LAST twin of :func:`dedup_qualify_sql` — a WAL fold, provably equal
    to a Python dict fold that starts from the base and overwrites per key in
    ascending delta-filename order (the ``load_window_ledger`` fold;
    NEWER-FILE-wins, never ``merge_ledger_rows`` — demotions survive). Appends
    after the ``WHERE`` clause of a query whose source is
    :data:`DEDUP_PARQUET_SOURCE_SQL`::

        row_number() OVER (PARTITION BY <keys>
                           ORDER BY (filename = ?) ASC, filename DESC,
                                    file_row_number DESC) = 1

    Ordering terms (live-verified on the pinned DuckDB engine):

    * ``(filename = ?) ASC`` — the extra ``?`` binds the BASE file's exact
      ``as_posix()`` path; ``True`` sorts after ``False``, so the base row ranks
      LAST in every partition and deltas always beat the base — regardless of
      lexicography (a base name sorting above ``deltas/``) and regardless of the
      lake's own path containing ``deltas`` (bound equality, never ``LIKE``).
      Bind the base's ``as_posix()`` even when the base file is absent — it then
      matches nothing and the deltas order among themselves.
    * ``filename DESC`` — among deltas the newest zero-padded ``time_ns`` name
      wins (lexicographic == chronological by construction,
      DEC-LEDGER-DELTA-LOCKED-TS-001).
    * ``file_row_number DESC`` — a determinism belt only: keys are unique WITHIN
      each ledger file by construction, so this fires only on a malformed
      within-file dup, where the LAST written row wins deterministically.

    Bind order law: the ``read_parquet`` file-list ``?`` first, then the
    base-path marker ``?``. HONEST envelope (Golden Rule #10): the QUALIFY scan
    is an O(total physical rows) columnar hash-partition per query plus
    O(file-count) Parquet footer opens — NOT O(page); the same envelope
    ``query_candles`` already accepts.

    Raises
    ------
    ValueError
        Via :func:`_quoted_key_columns` on a malformed key set.
    """
    keys = ", ".join(_quoted_key_columns(key_columns))
    return (
        f" QUALIFY row_number() OVER (PARTITION BY {keys} "
        "ORDER BY (filename = ?) ASC, filename DESC, file_row_number DESC) = 1"
    )


def dedup_latest_source_sql(key_columns: Sequence[str]) -> str:
    """Return the closed latest-wins subquery source for a base+delta fold scan.

    Composes :data:`DEDUP_STAR_SELECT_SQL` over :data:`DEDUP_PARQUET_SOURCE_SQL`
    with :func:`dedup_qualify_latest_sql` into ONE parenthesised subquery, so a
    consumer's ``WHERE`` / ``GROUP BY`` / ``ORDER BY`` / ``LIMIT`` always apply
    OUTSIDE the fold (a superseded row version can never resurrect through a
    filter — ghost-row safe by construction). The ``EXCLUDE`` keeps the outer
    ``SELECT *`` schema byte-identical to a plain ``read_parquet`` read.

    Parameter-order law: binds follow the SQL TEXT's placeholder order. The
    subquery precedes ``WHERE``/``ORDER``/``LIMIT``, so the source binds
    (``[files_list, base_marker]``) come FIRST in consumers whose only OTHER
    placeholders live in those trailing clauses — but a SELECT-list predicate
    textually precedes the ``FROM`` and binds BEFORE the source (e.g.
    ``db_view._window_counts_total_scoped``'s scope-classification ``CASE``,
    DEC-UNIVERSE-INDEX-ONLY-001 #1934); never "normalize" such a consumer to
    source-first — on the delta shape that binds the base-path marker into a
    predicate placeholder. HONEST envelope (Golden Rule #10): one such query is an O(total
    physical rows) columnar hash-partition plus O(file-count) footer opens per
    execution — NOT O(page); callers memoise per content stamp.

    Raises
    ------
    ValueError
        Via :func:`_quoted_key_columns` on a malformed key set.
    """
    return (
        f"(SELECT {DEDUP_STAR_SELECT_SQL} FROM {DEDUP_PARQUET_SOURCE_SQL}"  # noqa: S608  # nosec B608 — module-constant fragments + identifier-validated keys; every value is bound
        f"{dedup_qualify_latest_sql(key_columns)})"
    )


def distinct_count_sql(key_columns: Sequence[str] = ("timestamp",)) -> str:
    """Return the ``COUNT(DISTINCT …)`` select expression for a deduped page total.

    The count twin of :func:`dedup_qualify_sql`: a pager's ``total`` must equal
    the number of DISTINCT identity keys (what the deduped page actually
    serves), not the physical row count a poisoned partition inflates. A single
    key renders ``COUNT(DISTINCT "timestamp")``; a composite key renders the
    row-value form ``COUNT(DISTINCT ("side", "timestamp"))``.

    Raises
    ------
    ValueError
        Via :func:`_quoted_key_columns` on a malformed key set.
    """
    quoted = _quoted_key_columns(key_columns)
    if len(quoted) == 1:
        return f"COUNT(DISTINCT {quoted[0]})"
    return f"COUNT(DISTINCT ({', '.join(quoted)}))"


__all__ = [
    "DEDUP_PARQUET_SOURCE_SQL",
    "DEDUP_STAR_SELECT_SQL",
    "DUPLICATES_DROPPED_EVENT",
    "bars_by_ts_first",
    "dedup_bars",
    "dedup_bars_frame",
    "dedup_latest_source_sql",
    "dedup_qualify_latest_sql",
    "dedup_qualify_sql",
    "distinct_count_sql",
    "log_read_duplicates",
]
