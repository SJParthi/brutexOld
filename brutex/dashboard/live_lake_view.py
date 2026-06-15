"""Read-only browse of the live Parquet lake (``live_ticks`` + ``live_candles_1m``).

The dashboard Live section surfaces the SEPARATE ``live/`` lake the capture writes
(``DEC-LIVE-LAKE-SCHEMA-001``) **without importing** :mod:`brutex.live` — it opens
the Parquet globs directly via DuckDB (bound-parameter read, mirroring
:mod:`brutex.dashboard.db_view`), so ``brutex/live/`` stays imported-by-nothing and
portable to TickVault / AWS (``DEC-LIVE-DECOUPLE-001``). Read-only: no orders, no
mutation — it only renders rows the capture subprocess wrote.

Layout (the lake contract): ``<lake_root>/live/ticks/dt=*.parquet`` and
``<lake_root>/live/candles_1m/dt=*.parquet``.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import duckdb

from brutex.dashboard.db_view import (
    _build_order_by,
    _build_where,
    _select_list,
    format_clean_ist_millis,
)

#: The two browsable live tables and their on-disk subdirectory + stable sort key.
_TABLES: Final[dict[str, tuple[str, str]]] = {
    "live_ticks": ("ticks", '"seq"'),
    "live_candles_1m": ("candles_1m", '"timestamp"'),
}
#: Public list of browsable table names (newest-first when rendered).
LIVE_TABLES: Final[tuple[str, ...]] = tuple(_TABLES)

#: The live-table timestamp columns that render as **clean IST** (no ``+05:30``
#: offset suffix, trimmed to milliseconds) instead of the raw ``isoformat()`` the
#: operator saw — ``live_ticks.recv_ts`` / ``.exchange_ts`` + ``live_candles_1m
#: .timestamp`` (DEC-LIVE-DASH-PROBE-001). DISPLAY-ONLY: the lake storage stays UTC
#: ``Datetime("us","UTC")`` (CLAUDE.md §6 "stored UTC, displayed IST"). Reuses the
#: shared :func:`brutex.dashboard.db_view.format_clean_ist_millis` formatter (COMMON,
#: CLAUDE.md §11) — the spot candle viewer's own clean formatting is untouched.
_IST_TIMESTAMP_COLUMNS: Final[frozenset[str]] = frozenset({"recv_ts", "exchange_ts", "timestamp"})

#: The bound-parameter ``read_parquet`` source (the glob is always bound, never
#: interpolated — no path-traversal / glob-injection surface).
_SOURCE: Final = "read_parquet(?, union_by_name=true)"

_LIMIT_MIN: Final = 1
_LIMIT_MAX: Final = 500


def clamp_limit(n: int) -> int:
    """Clamp a requested row limit to ``[1, 500]`` (mirrors ``db_view``)."""
    return max(_LIMIT_MIN, min(_LIMIT_MAX, n))


def clamp_offset(n: int) -> int:
    """Clamp a requested page offset to ``>= 0``."""
    return max(0, n)


def _subdir(table: str) -> str:
    """Return the on-disk subdirectory for *table* (raises on an unknown name)."""
    if table not in _TABLES:
        raise ValueError(f"unknown live table {table!r}; expected one of {LIVE_TABLES}")
    return _TABLES[table][0]


def _glob(lake_root: Path, table: str) -> str:
    """The ``read_parquet`` glob for *table* under *lake_root* (a bound parameter)."""
    return (lake_root / "live" / _subdir(table)).as_posix() + "/dt=*.parquet"


def _has_partitions(lake_root: Path, table: str) -> bool:
    """True if *table* has at least one ``dt=*.parquet`` partition on disk."""
    base = lake_root / "live" / _subdir(table)
    return base.is_dir() and any(base.glob("dt=*.parquet"))


def _jsonify(cell: object) -> Any:
    """Coerce a DuckDB cell to a JSON-safe value (datetimes/Decimals → str/float)."""
    if cell is None or isinstance(cell, (str, int, float, bool)):
        return cell
    if isinstance(cell, Decimal):
        return float(cell)
    if isinstance(cell, (_dt.datetime, _dt.date)):
        return cell.isoformat()
    return str(cell)


def _jsonify_for_column(cell: object, column: str) -> Any:
    """JSON-safe a cell, rendering the live-table timestamp columns as clean IST.

    For a :data:`_IST_TIMESTAMP_COLUMNS` column holding a ``datetime`` (the lake's
    UTC ``recv_ts`` / ``exchange_ts`` / ``timestamp``) the value renders as the clean
    IST wall-clock ``2026-06-15 15:34:41.335`` — no ``+05:30`` offset suffix, trimmed
    to milliseconds (DEC-LIVE-DASH-PROBE-001) — via the shared
    :func:`brutex.dashboard.db_view.format_clean_ist_millis`. A NULL cell in such a
    column (e.g. ``exchange_ts`` null when the leaf carried no usable ``tsInMillis``)
    stays ``None``, and any non-datetime falls through to :func:`_jsonify`. Every other
    column is unchanged. DISPLAY-ONLY — the stored UTC value is never mutated.
    """
    if column in _IST_TIMESTAMP_COLUMNS and isinstance(cell, _dt.datetime):
        return format_clean_ist_millis(cell)
    return _jsonify(cell)


def _render_rows(columns: list[str], raw_rows: list[Any]) -> list[list[Any]]:
    """Render DuckDB *raw_rows* to JSON-safe rows, clean-IST per column.

    Pairs each cell with its column name so the timestamp columns route through
    :func:`_jsonify_for_column` (clean IST) while every other cell uses the plain
    :func:`_jsonify` — one place the row coercion lives (CLAUDE.md §11 COMMON), shared
    by :func:`live_table` + :func:`query_live_table`.
    """
    return [
        [_jsonify_for_column(cell, col) for cell, col in zip(row, columns, strict=True)]
        for row in raw_rows
    ]


def live_table(
    lake_root: str | Path, table: str, *, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    """Read one page of a live lake table, newest-first.

    Returns the SAME ``{table, columns, rows, total, limit, offset}`` shape the
    rest of ``/db`` uses. An absent/empty partition set yields an empty page (never
    a ``read_parquet`` "No files found" error). The glob is built from a fixed
    allow-listed table name (no free user text) and handed to ``read_parquet`` as a
    BOUND parameter, so there is no path-traversal / glob-injection surface.
    """
    root = Path(lake_root)
    safe_limit, safe_offset = clamp_limit(limit), clamp_offset(offset)
    sort = _TABLES[table][1] if table in _TABLES else _subdir(table)  # validates table
    if not _has_partitions(root, table):
        return {
            "table": table,
            "columns": [],
            "rows": [],
            "total": 0,
            "limit": safe_limit,
            "offset": safe_offset,
        }
    glob = _glob(root, table)
    conn = duckdb.connect(":memory:")
    try:
        # COUNT(*) always yields exactly one row → fetchall()[0][0] (no None branch).
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM {_SOURCE}", [glob]).fetchall()[0][0]  # noqa: S608  # nosec B608 - bound parquet path
        )
        cursor = conn.execute(
            f"SELECT * FROM {_SOURCE} ORDER BY {sort} DESC LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - fixed sort col + bound path/values
            [glob, safe_limit, safe_offset],
        )
        # A SELECT always carries a description (column metadata) → cast, no guard.
        columns = [str(col[0]) for col in cast("list[Any]", cursor.description)]
        rows = _render_rows(columns, cursor.fetchall())
    finally:
        conn.close()
    return {
        "table": table,
        "columns": columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


def _parquet_columns(conn: Any, glob: str) -> list[str]:
    """Read a live partition set's column names (a zero-row ``LIMIT 0`` probe)."""
    cursor = conn.execute(
        f"SELECT * FROM {_SOURCE} LIMIT 0",  # noqa: S608  # nosec B608 - bound parquet path
        [glob],
    )
    columns = [str(col[0]) for col in cast("list[Any]", cursor.description)]
    cursor.fetchall()  # free the cursor (mirrors db_view.list_columns)
    return columns


def live_columns(lake_root: str | Path, table: str) -> list[str]:
    """Return a live table's column names (the search/sort column picker).

    Empty when the table has no partitions yet. Raises ``ValueError`` on an
    unknown table name (``_subdir`` validates the allowlist).
    """
    root = Path(lake_root)
    _subdir(table)  # validate the allowlisted table name
    if not _has_partitions(root, table):
        return []
    conn = duckdb.connect(":memory:")
    try:
        return _parquet_columns(conn, _glob(root, table))
    finally:
        conn.close()


def query_live_table(
    lake_root: str | Path,
    table: str,
    *,
    columns: tuple[str, ...] | None = None,
    filters: tuple[tuple[str, str, str], ...] = (),
    order_by: str | None = None,
    descending: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Structured, read-only **search / sort / filter** of a live lake table.

    The power-user surface behind the Live DB browser — the live-lake twin of
    :func:`brutex.dashboard.db_view.query_table`, reusing its **validated**
    builders (``_select_list`` / ``_build_where`` / ``_build_order_by``): pick
    *columns*, filter rows with *filters* (``(column, op, value)`` where ``op`` is
    an :data:`~brutex.dashboard.db_view.ALLOWED_OPERATORS` token), *order_by* a
    column (defaults to newest-first by the table's stable key), and page with
    *limit* / *offset*. Safe by construction: the table is allowlist-checked,
    every column/order/filter identifier is validated against the partition's
    REAL columns (an unknown/injection-shaped name raises before any SQL runs),
    operators come from the fixed map, and the glob + all values are bound.
    ``total`` reflects the filtered count so the pager is correct.
    """
    root = Path(lake_root)
    safe_limit, safe_offset = clamp_limit(limit), clamp_offset(offset)
    default_sort = _TABLES[table][1] if table in _TABLES else _subdir(table)  # validates table
    if not _has_partitions(root, table):
        return {
            "table": table,
            "columns": [],
            "rows": [],
            "total": 0,
            "limit": safe_limit,
            "offset": safe_offset,
        }
    glob = _glob(root, table)
    conn = duckdb.connect(":memory:")
    try:
        valid = frozenset(_parquet_columns(conn, glob))
        select = _select_list(columns, valid)
        where_sql, where_params = _build_where(filters, valid)
        order_sql = _build_order_by(order_by, descending, valid) or f" ORDER BY {default_sort} DESC"
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {_SOURCE}{where_sql}",  # noqa: S608  # nosec B608 - bound path + validated identifiers + bound values
                [glob, *where_params],
            ).fetchall()[0][0]
        )
        cursor = conn.execute(
            f"SELECT {select} FROM {_SOURCE}{where_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - validated select/where/order + bound path/values
            [glob, *where_params, safe_limit, safe_offset],
        )
        out_columns = [str(col[0]) for col in cast("list[Any]", cursor.description)]
        rows = _render_rows(out_columns, cursor.fetchall())
    finally:
        conn.close()
    return {
        "table": table,
        "columns": out_columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


def live_tables_summary(lake_root: str | Path) -> list[dict[str, Any]]:
    """Return ``[{table, row_count}]`` for every live table (the nav counts)."""
    root = Path(lake_root)
    return [{"table": t, "row_count": live_table(root, t, limit=1)["total"]} for t in LIVE_TABLES]


#: The honest empty-state note when the live-tick lake has no partitions yet.
TICKS_EMPTY_NOTE: Final[str] = "no ticks yet — turn the Live feed ON to start capturing"

#: A feed/instrument is "fresh" while its last tick is within this many seconds
#: (``age <= FRESH_MAX_AGE_S``); once the gap EXCEEDS it the instrument reads "stale".
#: The Groww live feed delivers ~1+ updates/sec per subscribed instrument
#: (DEC-LIVE-PROBE-CONCURRENT-001 cadence classes), so a gap past 60s means the feed
#: has stopped flowing for that instrument (closed market, dropped connection, or OFF).
FRESH_MAX_AGE_S: Final[float] = 60.0


def _latest_tick_partition(lake_root: Path) -> Path | None:
    """Return the NEWEST ``live/ticks/dt=<YYYY-MM-DD>.parquet`` partition, or ``None``.

    The writer (:func:`brutex.live.lake._partition_path`) names every partition
    ``dt=<session_date>.parquet`` with ``session_date`` a ``date`` → always an ISO
    ``YYYY-MM-DD`` calendar date. We pick the newest by PARSING that date and taking
    the max, **skipping any name that is not a valid ISO date** — so a stray /
    hand-crafted ``dt=garbage.parquet`` (which would otherwise sort lexicographically
    last) can never be chosen as "newest", keeping the freshness scan both BOUNDED
    (Golden Rule #5 — only the latest day's one file is read) and HONEST (it reads a
    real session partition, never a malformed file).
    """
    base = lake_root / "live" / _subdir("live_ticks")
    if not base.is_dir():
        return None
    dated: list[tuple[_dt.date, Path]] = []
    for path in base.glob("dt=*.parquet"):
        try:
            session = _dt.date.fromisoformat(path.stem.removeprefix("dt="))
        except ValueError:
            continue  # a non-ISO partition name → not a real session file, skipped
        dated.append((session, path))
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[0])[1]


def _age_seconds(last: _dt.datetime | None, now: _dt.datetime) -> float | None:
    """Age of *last* relative to *now* in seconds, to one decimal (``None`` if null).

    Both are UTC instants from the lake; the gap is rounded to a tenth of a second
    (e.g. ``30.0``). A negative computed age (a tick stamped slightly ahead of the
    reader's clock) is clamped to ``0.0`` so the indicator never shows a nonsensical
    negative freshness.
    """
    if last is None:
        return None
    return max(0.0, round((now - last).total_seconds(), 1))


def ticks_flowing(lake_root: str | Path, *, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Per-instrument "are ticks FLOWING?" freshness from the live-tick lake.

    Reads ONLY the newest ``live/ticks`` partition (:func:`_latest_tick_partition` —
    a BOUNDED, O(1)-in-history read; DuckDB further skips row-groups via the Parquet
    min/max stats) and returns, per ``(instrument_key, groww_symbol)``: the latest
    ``recv_ts`` (OUR receive clock) + ``exchange_ts`` (the vendor exchange clock,
    null when the leaf carried no usable ``tsInMillis``), the tick count in that
    partition, and the **last-tick age** in seconds vs *now*. A feed is ``fresh`` iff
    its last tick is within :data:`FRESH_MAX_AGE_S`.

    This reconciles the operator-observed "``/live`` shows TICKS CAPTURED 0 while
    ``/db`` shows tens of thousands of rows" mismatch: the old counter counts JSONL
    write-ahead-log LINES (often 0 once folded/rotated), whereas THIS reads the ACTUAL
    Parquet lake the ``/db`` tables show — so the indicator reflects real lake state.

    **Honest envelope (Golden Rule #1 / #10):** an absent / empty tick lake yields
    ``flowing=False`` + :data:`TICKS_EMPTY_NOTE` and an empty instrument list — never a
    fabricated flow. The dashboard reads the lake directly (DuckDB, bound-parameter
    glob) and never imports :mod:`brutex.live` (``DEC-LIVE-DECOUPLE-001``).

    Parameters
    ----------
    lake_root
        The lake root (the parent of ``live/``).
    now
        The reference instant for the age (defaults to ``datetime.now(UTC)``);
        injected in tests for a deterministic age.

    Returns
    -------
    dict[str, Any]
        ``{"flowing": bool, "total_ticks": int, "fresh_instruments": int,
        "instruments": [{"instrument_key", "groww_symbol", "ticks",
        "last_recv_ts", "last_exchange_ts", "last_tick_age_s", "fresh"}],
        "session_date": <YYYY-MM-DD|None>, "note": <str|None>}`` — sorted by freshest
        first (smallest age). ``flowing`` is True iff at least one instrument is fresh.
    """
    reference = now if now is not None else _dt.datetime.now(_dt.UTC)
    root = Path(lake_root)
    partition = _latest_tick_partition(root)
    if partition is None:
        return {
            "flowing": False,
            "total_ticks": 0,
            "fresh_instruments": 0,
            "instruments": [],
            "session_date": None,
            "note": TICKS_EMPTY_NOTE,
        }
    # dt=<YYYY-MM-DD>.parquet → the session date (display only).
    session_date = partition.stem.removeprefix("dt=")
    conn = duckdb.connect(":memory:")
    try:
        # One bounded aggregate over the newest partition: max receive / exchange clock
        # + count per instrument. The path is a BOUND parameter (no glob/SQL-injection
        # surface) and the column list is the fixed lake schema (no free user text).
        cursor = conn.execute(
            "SELECT instrument_key, groww_symbol, COUNT(*) AS ticks, "  # noqa: S608  # nosec B608 - static SQL, bound path
            'MAX("recv_ts") AS last_recv_ts, MAX("exchange_ts") AS last_exchange_ts '
            f"FROM {_SOURCE} "
            "GROUP BY instrument_key, groww_symbol "
            "ORDER BY last_recv_ts DESC",
            [partition.as_posix()],
        )
        raw = cursor.fetchall()
    finally:
        conn.close()
    instruments: list[dict[str, Any]] = []
    total_ticks = 0
    fresh_count = 0
    for instrument_key, groww_symbol, ticks, last_recv_ts, last_exchange_ts in raw:
        count = int(ticks)
        total_ticks += count
        age = _age_seconds(last_recv_ts, reference)
        fresh = age is not None and age <= FRESH_MAX_AGE_S
        if fresh:
            fresh_count += 1
        instruments.append(
            {
                "instrument_key": instrument_key,
                "groww_symbol": groww_symbol,
                "ticks": count,
                "last_recv_ts": _jsonify_for_column(last_recv_ts, "recv_ts"),
                "last_exchange_ts": _jsonify_for_column(last_exchange_ts, "exchange_ts"),
                "last_tick_age_s": age,
                "fresh": fresh,
            }
        )
    return {
        "flowing": fresh_count > 0,
        "total_ticks": total_ticks,
        "fresh_instruments": fresh_count,
        "instruments": instruments,
        "session_date": session_date,
        "note": None if instruments else TICKS_EMPTY_NOTE,
    }


__all__ = [
    "FRESH_MAX_AGE_S",
    "LIVE_TABLES",
    "TICKS_EMPTY_NOTE",
    "clamp_limit",
    "clamp_offset",
    "live_columns",
    "live_table",
    "live_tables_summary",
    "query_live_table",
    "ticks_flowing",
]
