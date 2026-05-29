"""Read-only DuckDB viewer — table summaries + sample rows (DEC-DB-VIEWER-001).

Pure, FastAPI-free helpers powering the ``/db`` dashboard page so an
operator can answer "where is the DB, how can I see it, what is the
schema — every nook and corner" from the browser instead of reaching
for the ``duckdb`` CLI. Per CLAUDE.md §11 (COMMON) all real logic lives
here in exactly ONE place; the dashboard FastAPI routes in
:mod:`brutex.dashboard.app` (coverage-excluded, FastAPI-dependent) stay
thin wrappers that delegate here so this surface is 100% unit-testable
offline against a real :class:`~brutex.data.instrument_store.InstrumentStore`.

## Read-only by construction

Every query this module issues is a ``SELECT`` (a ``COUNT(*)`` or a
``SELECT * ... LIMIT ?``). There is no code path that writes, so even a
hostile caller cannot mutate the store through the viewer. The store's
audit + idempotence guarantees (CLAUDE.md §7/§8) are never touched.

## SQL-injection guard (Golden Rule #1 / §4)

DuckDB cannot parameter-bind an *identifier* (a table name), so the
table name is interpolated into the SQL string. To make that safe the
caller-supplied table name is checked for membership in
:data:`KNOWN_TABLES` (a fixed allowlist of the 5 real store tables)
*before* it is ever placed into a query. A name that is not an exact
member raises :class:`ValueError` and NO SQL is executed — so a payload
such as ``"instruments; DROP TABLE contracts"`` is rejected, never run.
The row ``limit`` IS parameter-bound (``LIMIT ?``) and additionally
clamped to ``[1, 500]`` so a hostile / malformed query string can never
trigger an unbounded read.
"""

from __future__ import annotations

import math
from typing import Any, Final

#: The ONLY tables the viewer may touch — the 5 real tables created by
#: :meth:`brutex.data.instrument_store.InstrumentStore.open`. A
#: caller-supplied table name is checked against this allowlist before
#: it is interpolated into any SQL string (SQL-injection guard).
KNOWN_TABLES: Final = (
    "instruments",
    "instrument_runs",
    "expiries",
    "contracts",
    "schema_meta",
)

#: Lower inclusive bound for the sample-row ``limit``.
_LIMIT_MIN: Final = 1

#: Upper inclusive bound for the sample-row ``limit`` (keeps a hostile
#: ``?limit=999999999`` from materialising a huge result set).
_LIMIT_MAX: Final = 500


def clamp_limit(n: int) -> int:
    """Clamp a requested row limit into the safe ``[1, 500]`` range.

    Parameters
    ----------
    n
        The caller-requested limit (may be ``0``, negative, or absurdly
        large from an untrusted query string).

    Returns
    -------
    int
        ``n`` clamped to ``[_LIMIT_MIN, _LIMIT_MAX]`` — i.e. at least
        ``1`` and at most ``500``.
    """
    return max(_LIMIT_MIN, min(_LIMIT_MAX, n))


def _json_safe(value: Any) -> Any:
    """Coerce one DuckDB cell value into a JSON-serialisable form.

    ``None`` stays ``None`` (renders as JSON ``null``); ``bool`` /
    ``int`` / ``float`` / ``str`` pass through unchanged; everything
    else (dates, datetimes, :class:`~decimal.Decimal`, ``bytes``,
    enums, …) is rendered via :func:`str` so the value is always
    transport-safe without losing human readability.

    Parameters
    ----------
    value
        A single cell value as returned by DuckDB ``fetchall``.

    Returns
    -------
    Any
        A JSON-serialisable representation of *value*.
    """
    if value is None:
        return None
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        # NaN / ±inf are not JSON-safe under Starlette's allow_nan=False;
        # stringify them so the viewer never 500s if a future migration
        # adds a DOUBLE column to an allow-listed table (hostile-review LOW).
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        # bytes are not JSON-safe; decode best-effort for display.
        return value.decode("utf-8", errors="replace")
    return str(value)


def table_summary(conn: Any) -> list[dict[str, Any]]:
    """Return ``[{"table", "row_count"}]`` for every known table.

    Runs one ``SELECT COUNT(*)`` per table in :data:`KNOWN_TABLES`. A
    table that is missing (a partially-initialised or foreign DB) is
    tolerated and reported with ``row_count == 0`` rather than raising,
    so a fresh/empty store renders cleanly.

    Parameters
    ----------
    conn
        A DuckDB connection — typically
        :attr:`brutex.data.instrument_store.InstrumentStore.connection`.

    Returns
    -------
    list[dict[str, Any]]
        One row per known table, in :data:`KNOWN_TABLES` order, each
        ``{"table": <name>, "row_count": <int>}``.
    """
    summary: list[dict[str, Any]] = []
    for name in KNOWN_TABLES:
        # name is a hardcoded allowlist literal, never caller input.
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()  # noqa: S608  # nosec B608 - name is a hardcoded KNOWN_TABLES literal, never caller input
        except Exception:
            count = 0
        else:
            count = int(row[0]) if row else 0
        summary.append({"table": name, "row_count": count})
    return summary


def clamp_offset(n: int) -> int:
    """Clamp a requested row offset to a non-negative integer.

    A hostile / malformed ``?offset=-5`` (or a Python ``int`` from a query
    string) must never produce a negative ``OFFSET`` (DuckDB would error).
    Returns ``max(0, n)``.
    """
    return max(0, n)


def sample_rows(conn: Any, table: str, *, limit: int, offset: int = 0) -> dict[str, Any]:
    """Return one *limit*-sized page of rows from an allowlisted *table*.

    Read-only and bounded: issues an indexed ``SELECT COUNT(*)`` for the
    total then a single ``SELECT * FROM <table> LIMIT ? OFFSET ?`` with the
    page parameter-bound — it NEVER materialises the whole table (a 89k-row
    legs table would be a page of at most 500 rows). *table* MUST be an exact
    member of :data:`KNOWN_TABLES`; any other value (including an injection
    payload such as ``"instruments; DROP TABLE contracts"``) raises
    :class:`ValueError` before any SQL is built or executed.

    Parameters
    ----------
    conn
        A DuckDB connection (see :func:`table_summary`).
    table
        The table to sample. Must be in :data:`KNOWN_TABLES`.
    limit
        Page size; clamped to ``[1, 500]`` via :func:`clamp_limit`.
    offset
        Page start (0-based); clamped to ``>= 0`` via :func:`clamp_offset`.

    Returns
    -------
    dict[str, Any]
        ``{"table": <name>, "columns": [<col>, ...],
        "rows": [[<cell>, ...], ...], "total": <int>, "limit": <int>,
        "offset": <int>}`` with every cell JSON-safe. ``total`` is the
        indexed full-table row count (for the pager), NOT the page size.

    Raises
    ------
    ValueError
        If *table* is not an exact member of :data:`KNOWN_TABLES`.
    """
    if table not in KNOWN_TABLES:
        raise ValueError(f"unknown table {table!r}; allowed tables are {KNOWN_TABLES}")
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)
    # `table` is validated against the KNOWN_TABLES allowlist above, so
    # interpolating it here is safe; the page bounds are parameter-bound.
    total_row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608  # nosec B608 - table is allowlist-validated against KNOWN_TABLES above
    total = int(total_row[0]) if total_row else 0
    cursor = conn.execute(
        f"SELECT * FROM {table} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - table is allowlist-validated against KNOWN_TABLES above; page bounds are parameter-bound
        [safe_limit, safe_offset],
    )
    description = cursor.description or []
    columns = [str(col[0]) for col in description]
    rows = [[_json_safe(cell) for cell in row] for row in cursor.fetchall()]
    return {
        "table": table,
        "columns": columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


__all__ = [
    "KNOWN_TABLES",
    "clamp_limit",
    "clamp_offset",
    "sample_rows",
    "table_summary",
]
