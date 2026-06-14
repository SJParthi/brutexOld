"""``/db`` browse backend for the live-feed lake tables (DEC-LIVE-DB-VIEW-001).

Surfaces ``live_ticks`` + ``live_candles_1m`` (written by :mod:`brutex.live.lake`)
as the SAME ``{table, columns, rows, total, limit, offset}`` payload the rest of
the ``/db`` viewer uses, so the dashboard's LIVE FEED section renders them with
the existing grid. Read-only and bounded (page-clamped via the shared
:func:`brutex.dashboard.db_view.clamp_limit` / :func:`~clamp_offset`).

It REUSES the lake readers (:func:`brutex.live.lake.read_live_ticks` /
:func:`~read_live_candles`) rather than building a new ``read_parquet`` glob, so
there is no SQL/path-injection surface here — the lake module owns the paths
(CLAUDE.md §11 COMMON).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import polars as pl

from brutex.dashboard.db_view import clamp_limit, clamp_offset
from brutex.live.lake import read_live_candles, read_live_ticks

#: The browsable live tables → their lake reader.
LIVE_TABLES: Final[dict[str, Callable[[str | Path], pl.DataFrame]]] = {
    "live_ticks": read_live_ticks,
    "live_candles_1m": read_live_candles,
}


def _json_safe(cell: Any) -> Any:
    """Render a polars cell JSON-safe (datetimes → ISO; everything else as-is)."""
    if isinstance(cell, (datetime, date)):
        return cell.isoformat()
    return cell


def query_live_table(
    lake_root: str | Path, table: str, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """Return one page of an allowlisted live table in the ``/db`` payload shape.

    Parameters
    ----------
    lake_root
        The lake root holding the ``live/`` subtree.
    table
        Must be a key of :data:`LIVE_TABLES` (``live_ticks`` / ``live_candles_1m``).
    limit, offset
        Page size + start; clamped to the shared ``[1, 500]`` / ``>= 0`` bounds.

    Returns
    -------
    dict[str, Any]
        ``{"table", "columns", "rows", "total", "limit", "offset"}`` — identical
        in shape to :func:`brutex.dashboard.db_view.sample_table`.

    Raises
    ------
    ValueError
        If *table* is not an allowlisted live table.
    """
    if table not in LIVE_TABLES:
        raise ValueError(f"unknown live table {table!r}; allowed are {sorted(LIVE_TABLES)}")
    frame = LIVE_TABLES[table](lake_root)
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)
    page = frame.slice(safe_offset, safe_limit)
    rows = [[_json_safe(cell) for cell in row] for row in page.iter_rows()]
    return {
        "table": table,
        "columns": frame.columns,
        "rows": rows,
        "total": frame.height,
        "limit": safe_limit,
        "offset": safe_offset,
    }


__all__ = ["LIVE_TABLES", "query_live_table"]
