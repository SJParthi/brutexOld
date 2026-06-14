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

from brutex.dashboard.db_view import _build_order_by, _build_where, _select_list

#: The two browsable live tables and their on-disk subdirectory + stable sort key.
_TABLES: Final[dict[str, tuple[str, str]]] = {
    "live_ticks": ("ticks", '"seq"'),
    "live_candles_1m": ("candles_1m", '"timestamp"'),
}
#: Public list of browsable table names (newest-first when rendered).
LIVE_TABLES: Final[tuple[str, ...]] = tuple(_TABLES)

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
        rows = [[_jsonify(cell) for cell in row] for row in cursor.fetchall()]
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
        f"SELECT * FROM {_SOURCE} LIMIT 0",
        [glob],  # noqa: S608  # nosec B608 - bound parquet path
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
        rows = [[_jsonify(cell) for cell in row] for row in cursor.fetchall()]
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


__all__ = [
    "LIVE_TABLES",
    "clamp_limit",
    "clamp_offset",
    "live_columns",
    "live_table",
    "live_tables_summary",
    "query_live_table",
]
