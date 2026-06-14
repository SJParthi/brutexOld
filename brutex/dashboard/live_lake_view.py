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

#: The two browsable live tables and their on-disk subdirectory + stable sort key.
_TABLES: Final[dict[str, tuple[str, str]]] = {
    "live_ticks": ("ticks", '"seq"'),
    "live_candles_1m": ("candles_1m", '"timestamp"'),
}
#: Public list of browsable table names (newest-first when rendered).
LIVE_TABLES: Final[tuple[str, ...]] = tuple(_TABLES)

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
    source = "read_parquet(?, union_by_name=true)"
    conn = duckdb.connect(":memory:")
    try:
        # COUNT(*) always yields exactly one row → fetchall()[0][0] (no None branch).
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM {source}", [glob]).fetchall()[0][0]  # noqa: S608  # nosec B608 - bound parquet path
        )
        cursor = conn.execute(
            f"SELECT * FROM {source} ORDER BY {sort} DESC LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - fixed sort col + bound path/values
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


def live_tables_summary(lake_root: str | Path) -> list[dict[str, Any]]:
    """Return ``[{table, row_count}]`` for every live table (the nav counts)."""
    root = Path(lake_root)
    return [{"table": t, "row_count": live_table(root, t, limit=1)["total"]} for t in LIVE_TABLES]


__all__ = [
    "LIVE_TABLES",
    "clamp_limit",
    "clamp_offset",
    "live_table",
    "live_tables_summary",
]
