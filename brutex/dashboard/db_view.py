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
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from brutex.data.partition import (
    VALID_EXCHANGES,
    VALID_INTERVALS,
    VALID_SEGMENTS,
)

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

#: Structured-query operators (DEC-DB-QUERY-001). Each maps a stable UI
#: token to a (SQL operator, value-transform) pair. The SQL operator is a
#: hardcoded literal from this fixed table — NEVER caller text — and the
#: comparison value is ALWAYS parameter-bound (``?``), so the WHERE clause
#: carries zero SQL-injection surface. ``contains`` wraps the bound value
#: in ``%…%`` for a substring LIKE; ``like`` passes the operator's pattern
#: through verbatim (the operator types their own ``%``).
ALLOWED_OPERATORS: Final[dict[str, str]] = {
    "eq": "=",
    "ne": "!=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
    "like": "LIKE",
    "contains": "LIKE",
}

#: The 7 canonical OHLCV columns every candle Parquet partition carries
#: (``brutex/data/parquet_writer.py`` ``_render_bytes`` schema). Used to
#: validate a candle-viewer column selection without touching disk.
CANDLE_COLUMNS: Final = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
)

#: The 11 columns every expired-option Parquet partition carries
#: (``brutex/data/option_parquet_writer.py`` ``_render_df`` schema): the 7
#: OHLCV columns above PLUS the per-bar option stamps ``side`` / ``strike`` /
#: ``spot`` / ``iv`` (the rolling-option endpoint surface). Used to validate an
#: option-candle column selection without touching disk.
OPTION_CANDLE_COLUMNS: Final = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "side",
    "strike",
    "spot",
    "iv",
)

#: The subdirectory under the lake root holding the expired-options table
#: (``<lake>/options/<series>/<YYYY>-<MM>.parquet``). Mirrors
#: ``brutex.data.option_parquet_writer._OPTIONS_SUBDIR`` (kept in lockstep) so
#: the read-side viewer never traverses outside ``<lake>/options``.
OPTIONS_DIR_NAME: Final = "options"

#: A filesystem-safe option series segment is the writer's ``_safe_segment``
#: output: a ``|``-joined series key with ``|`` rewritten to ``__``. The real
#: parts are alnum + ``.`` + ``-`` (Dhan coordinates) joined by ``__``; we
#: allow exactly that grammar so a discovered directory name can never carry a
#: traversal/separator character into the read glob (``..`` rejected
#: separately, matching the writer's own guard).
_OPTION_SEGMENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

#: A candle symbol must match this allowlist before it is ever joined into
#: a lake path / ``read_parquet`` glob — defence-in-depth against path
#: traversal even though discovered symbols come from our own directory
#: scan. Dhan symbols are alnum + ``-`` + ``_`` + ``.`` (e.g.
#: ``NSE-NIFTY-30Jan25-28500-CE``); ``..`` is rejected separately.
_SYMBOL_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

#: Strict ISO calendar-date shape. ``date.fromisoformat`` (py3.11+) also
#: accepts ``YYYYMMDD`` and ``YYYY-Www-D`` ordinal/week forms; the candle
#: viewer's contract is the plain ``YYYY-MM-DD`` a ``<input type="date">``
#: emits, so anything else is rejected up front.
_ISO_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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


#: IST — the market wall-clock the candle viewer renders bar times in. The
#: lake stores bar time as a UTC ``TIMESTAMPTZ``; we display the Indian-market
#: local time explicitly (not the ambient DuckDB session ``TimeZone``) so the
#: rendered string is deterministic regardless of where the server runs.
_IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")


def _format_candle_timestamp(value: Any) -> Any:
    """Render a candle bar timestamp as a clean IST wall-clock string.

    Drops the ``+05:30`` offset suffix (operator: "it should be clean") — a
    bar stored as ``2026-05-29T03:30:00Z`` renders ``2026-05-29 09:00:00``
    (IST), never ``2026-05-29 09:00:00+05:30``. Converting to :data:`_IST`
    explicitly keeps the output identical whether the DuckDB session runs in
    UTC (CI) or Asia/Kolkata (the operator's box). Non-datetime cells (a
    column subset that excludes ``timestamp`` never reaches here) fall back to
    :func:`_json_safe`.
    """
    if isinstance(value, datetime):
        ist = value.astimezone(_IST) if value.tzinfo is not None else value
        return ist.strftime("%Y-%m-%d %H:%M:%S")
    return _json_safe(value)


def _ist_date(value: Any) -> str | None:
    """Render a bar timestamp as its IST calendar date (``YYYY-MM-DD``), or ``None``.

    The lake stores bar time as a UTC ``TIMESTAMPTZ``; the spot-coverage summary
    reports the Indian-market wall-clock *date* (converting to :data:`_IST`) so
    the first/last coverage date matches the trading day an operator expects —
    the date-level analog of :func:`_format_candle_timestamp`. A non-datetime
    cell (e.g. ``MIN``/``MAX`` over a partition that happens to hold zero rows
    returns SQL ``NULL`` → ``None``) yields ``None``.
    """
    if isinstance(value, datetime):
        ist = value.astimezone(_IST) if value.tzinfo is not None else value
        return ist.date().isoformat()
    return None


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


def _quote_ident(name: str) -> str:
    """Double-quote a SQL identifier, rejecting any embedded double-quote.

    The *name* is always validated against a known column set BEFORE this
    is called, so the reject branch is belt-and-braces: a column literally
    containing ``"`` could otherwise break out of the quoted identifier.
    """
    if '"' in name:
        raise ValueError(f"illegal identifier {name!r}")
    return f'"{name}"'


def list_columns(conn: Any, table: str) -> list[str]:
    """Return the column names of an allowlisted *table*.

    Issues a zero-row ``SELECT * FROM <table> LIMIT 0`` and reads the
    cursor description — no data is materialised. Powers the column
    picker in the structured-query UI.

    Parameters
    ----------
    conn
        A DuckDB connection (see :func:`table_summary`).
    table
        Must be an exact member of :data:`KNOWN_TABLES`.

    Returns
    -------
    list[str]
        Column names in declaration order.

    Raises
    ------
    ValueError
        If *table* is not in :data:`KNOWN_TABLES`.
    """
    if table not in KNOWN_TABLES:
        raise ValueError(f"unknown table {table!r}; allowed tables are {KNOWN_TABLES}")
    cursor = conn.execute(f"SELECT * FROM {table} LIMIT 0")  # noqa: S608  # nosec B608 - table is allowlist-validated above
    columns = [str(col[0]) for col in (cursor.description or [])]
    # CRITICAL (DEC-STORE-LOCK-LEAK-FIX-001): the production connection is a
    # store ``_LockedReadConn`` that holds the store lock from ``execute()``
    # until a ``fetch*()``. Reading ``.description`` alone does NOT release it —
    # so we MUST fetch (the ``LIMIT 0`` result is empty) to free the lock, or
    # the background universe-pull's writes deadlock against this read (the
    # live "Fetch all expiries" 26-minute hang). Harmless on a raw cursor.
    cursor.fetchall()
    return columns


def _build_where(
    filters: tuple[tuple[str, str, str], ...],
    valid_columns: frozenset[str],
) -> tuple[str, list[Any]]:
    """Build a parameter-bound ``WHERE`` clause from validated *filters*.

    Each filter is ``(column, op_token, value)``. The column must be in
    *valid_columns* and the op_token in :data:`ALLOWED_OPERATORS`; the
    value is always returned as a bound parameter (never interpolated).
    Returns ``("", [])`` when *filters* is empty.

    Raises
    ------
    ValueError
        On an unknown column or an unknown operator token.
    """
    if not filters:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    for column, op, value in filters:
        if column not in valid_columns:
            raise ValueError(f"unknown filter column {column!r}; allowed: {sorted(valid_columns)}")
        if op not in ALLOWED_OPERATORS:
            raise ValueError(f"unknown operator {op!r}; allowed: {sorted(ALLOWED_OPERATORS)}")
        ident = _quote_ident(column)
        if op == "contains":
            # Literal substring match: escape the LIKE metacharacters (\ % _)
            # in the value + declare ESCAPE, so a value containing % or _ is
            # matched literally rather than as a wildcard (the "like" operator
            # is the escape hatch when the operator wants raw patterns).
            esc_v = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(f"{ident} LIKE ? ESCAPE '\\'")
            params.append(f"%{esc_v}%")
        else:
            clauses.append(f"{ident} {ALLOWED_OPERATORS[op]} ?")
            params.append(value)
    return " WHERE " + " AND ".join(clauses), params


def _build_order_by(
    order_by: str | None,
    descending: bool,
    valid_columns: frozenset[str],
) -> str:
    """Build a validated ``ORDER BY`` clause, or ``""`` when not requested.

    Raises
    ------
    ValueError
        If *order_by* is given but not a member of *valid_columns*.
    """
    if order_by is None:
        return ""
    if order_by not in valid_columns:
        raise ValueError(f"unknown order column {order_by!r}; allowed: {sorted(valid_columns)}")
    direction = "DESC" if descending else "ASC"
    return f" ORDER BY {_quote_ident(order_by)} {direction}"


def _select_list(columns: tuple[str, ...] | None, valid_columns: frozenset[str]) -> str:
    """Return a quoted, validated select list — or ``"*"`` when *columns* is None.

    Raises
    ------
    ValueError
        If any requested column is not a member of *valid_columns*, or if
        *columns* is an empty tuple (a select of zero columns is invalid).
    """
    if columns is None:
        return "*"
    if not columns:
        raise ValueError("columns, when given, must name at least one column")
    for column in columns:
        if column not in valid_columns:
            raise ValueError(f"unknown column {column!r}; allowed: {sorted(valid_columns)}")
    return ", ".join(_quote_ident(c) for c in columns)


def query_table(
    conn: Any,
    table: str,
    *,
    columns: tuple[str, ...] | None = None,
    filters: tuple[tuple[str, str, str], ...] = (),
    order_by: str | None = None,
    descending: bool = False,
    limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    """Structured, read-only query of an allowlisted *table* (DEC-DB-QUERY-001).

    The power-user surface behind the ``/db`` query controls: choose which
    *columns* to show, filter rows with *filters* (``(column, op, value)``
    where ``op`` is an :data:`ALLOWED_OPERATORS` token), *order_by* a
    column, and page with *limit* / *offset*. Safe by construction: the
    table is allowlist-checked, every column / order column / filter column
    is validated against the table's REAL columns (so a non-existent or
    injection-shaped name raises before any SQL runs), operators come from a
    fixed map, and **all values are parameter-bound**. ``total`` reflects the
    filtered row count so the pager is correct.

    Parameters
    ----------
    conn
        A DuckDB connection (see :func:`table_summary`).
    table
        Must be an exact member of :data:`KNOWN_TABLES`.
    columns
        Columns to project, or ``None`` for all (``SELECT *``). An empty
        tuple is rejected.
    filters
        Zero or more ``(column, op_token, value)`` triples, AND-combined.
    order_by, descending
        Optional sort column + direction.
    limit, offset
        Page bounds; clamped via :func:`clamp_limit` / :func:`clamp_offset`.

    Returns
    -------
    dict[str, Any]
        ``{"table", "columns", "rows", "total", "limit", "offset"}`` —
        same shape as :func:`sample_rows`, with the filtered ``total``.

    Raises
    ------
    ValueError
        On an unknown table / column / order column / operator, or an empty
        explicit column tuple.
    """
    valid_columns = frozenset(list_columns(conn, table))
    select = _select_list(columns, valid_columns)
    where_sql, where_params = _build_where(filters, valid_columns)
    order_sql = _build_order_by(order_by, descending, valid_columns)
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)

    # table is allowlisted; select/where/order identifiers are all validated
    # against valid_columns above; every value is parameter-bound.
    total_row = conn.execute(
        f"SELECT COUNT(*) FROM {table}{where_sql}",  # noqa: S608  # nosec B608 - allowlisted table + validated identifiers; values bound
        where_params,
    ).fetchone()
    total = int(total_row[0]) if total_row else 0
    cursor = conn.execute(
        f"SELECT {select} FROM {table}{where_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - allowlisted table + validated identifiers; values bound
        [*where_params, safe_limit, safe_offset],
    )
    description = cursor.description or []
    out_columns = [str(col[0]) for col in description]
    rows = [[_json_safe(cell) for cell in row] for row in cursor.fetchall()]
    return {
        "table": table,
        "columns": out_columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


def _validate_candle_coords(exchange: str, segment: str, symbol: str, interval: str) -> None:
    """Validate the four candle-partition coordinates, else raise ValueError.

    Mirrors the partition-layer allowlists (``VALID_EXCHANGES`` /
    ``VALID_SEGMENTS`` / ``VALID_INTERVALS``) and applies a strict symbol
    allowlist (:data:`_SYMBOL_RE`, plus a ``..`` reject) so a coordinate can
    never traverse outside ``<lake>/bars`` when joined into the glob.
    """
    if exchange not in VALID_EXCHANGES:
        raise ValueError(f"unknown exchange {exchange!r}; allowed: {sorted(VALID_EXCHANGES)}")
    if segment not in VALID_SEGMENTS:
        raise ValueError(f"unknown segment {segment!r}; allowed: {sorted(VALID_SEGMENTS)}")
    if interval not in VALID_INTERVALS:
        raise ValueError(f"unknown interval {interval!r}; allowed: {sorted(VALID_INTERVALS)}")
    if ".." in symbol or not _SYMBOL_RE.match(symbol):
        raise ValueError(f"illegal candle symbol {symbol!r}")


def list_candle_datasets(lake_root: Path) -> list[dict[str, str]]:
    """Discover which ``(exchange, segment, symbol, interval)`` candle sets exist.

    Walks ``<lake_root>/bars/<exchange>/<segment>/<symbol>/<interval>/`` and
    returns one entry per interval directory that holds at least one
    ``.parquet`` file. Directory names that are not valid coordinates are
    skipped (a foreign / partially-written tree never raises). A missing
    ``bars`` directory yields ``[]``.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).

    Returns
    -------
    list[dict[str, str]]
        ``[{"exchange", "segment", "symbol", "interval"}]`` sorted by all
        four keys — the candle-viewer dataset picker.
    """
    bars = lake_root / "bars"
    if not bars.is_dir():
        return []
    found: list[dict[str, str]] = []
    for exch_dir in sorted(p for p in bars.iterdir() if p.is_dir()):
        if exch_dir.name not in VALID_EXCHANGES:
            continue
        for seg_dir in sorted(p for p in exch_dir.iterdir() if p.is_dir()):
            if seg_dir.name not in VALID_SEGMENTS:
                continue
            for sym_dir in sorted(p for p in seg_dir.iterdir() if p.is_dir()):
                for tf_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir()):
                    if tf_dir.name not in VALID_INTERVALS:
                        continue
                    if any(tf_dir.rglob("*.parquet")):
                        found.append(
                            {
                                "kind": "spot",
                                "exchange": exch_dir.name,
                                "segment": seg_dir.name,
                                "symbol": sym_dir.name,
                                "interval": tf_dir.name,
                            }
                        )
    return found


def summarize_spot_datasets(conn: Any, lake_root: Path) -> list[dict[str, Any]]:
    """Summarise every spot candle dataset in the lake for the coverage page.

    Discovers each ``(exchange, segment, symbol, interval)`` partition set via
    :func:`list_candle_datasets`, then reads ONE aggregate row per set
    (``COUNT(*)`` + ``MIN`` / ``MAX`` of the bar ``timestamp``) straight from the
    Parquet lake via DuckDB ``read_parquet`` over the partition glob — no data
    duplication, the lake stays the single source of truth (CLAUDE.md §11
    COMMON). Read-only by construction: a single ``SELECT`` aggregate per set, no
    write path. A click never materialises bar rows — only the three aggregate
    scalars — so cost is bounded regardless of how many years are stored
    (DuckDB skips row-groups via Parquet min/max stats).

    **Honest envelope (Golden Rule #10):** a dataset PRESENT in the lake counts
    as *pulled*; ``bars`` / ``first_date`` / ``last_date`` describe WHAT is
    stored, NOT whether the full ``2021 → today`` range is complete.
    Partial-date-range detection is an explicit follow-up, not claimed here.

    Parameters
    ----------
    conn
        A DuckDB connection (see :func:`table_summary`).
    lake_root
        Top-level lake directory (the parent of ``bars/``).

    Returns
    -------
    list[dict[str, Any]]
        One row per discovered dataset, in :func:`list_candle_datasets` order::

            {"exchange", "segment", "symbol", "interval",
             "bars": <int>, "first_date": <YYYY-MM-DD|None>,
             "last_date": <YYYY-MM-DD|None>}
    """
    rows: list[dict[str, Any]] = []
    for dataset in list_candle_datasets(lake_root):
        exchange = dataset["exchange"]
        segment = dataset["segment"]
        symbol = dataset["symbol"]
        interval = dataset["interval"]
        glob = (
            lake_root / "bars" / exchange / segment / symbol / interval
        ).as_posix() + "/**/*.parquet"
        # The glob is built from list_candle_datasets coordinates (each already
        # validated against the partition allowlists during discovery), never
        # free user text; the path is given to read_parquet as a bound parameter.
        agg = conn.execute(
            'SELECT COUNT(*), MIN("timestamp"), MAX("timestamp") '
            "FROM read_parquet(?, union_by_name=true)",
            [glob],
        ).fetchone()
        rows.append(
            {
                "exchange": exchange,
                "segment": segment,
                "symbol": symbol,
                "interval": interval,
                "bars": int(agg[0]) if agg else 0,
                "first_date": _ist_date(agg[1]) if agg else None,
                "last_date": _ist_date(agg[2]) if agg else None,
            }
        )
    return rows


def _validate_option_segment(series: str) -> None:
    """Validate a discovered option-series directory name, else raise ValueError.

    The on-disk segment is the writer's filesystem-safe ``_safe_segment`` output
    (``brutex.data.option_parquet_writer``): a ``|``-joined series coordinate
    with ``|`` → ``__``. We re-validate it against :data:`_OPTION_SEGMENT_RE`
    (plus a ``..`` reject) so a hand-crafted / foreign directory name can never
    traverse outside ``<lake>/options`` when joined into the read glob — the
    options-side analog of :func:`_validate_candle_coords`.
    """
    if ".." in series or not _OPTION_SEGMENT_RE.match(series):
        raise ValueError(f"illegal option series {series!r}")


def list_option_datasets(lake_root: Path) -> list[dict[str, str]]:
    """Discover which expired-option series exist under ``<lake>/options/``.

    Walks ``<lake_root>/options/<series>/`` (the layout
    :class:`~brutex.data.option_parquet_writer.OptionParquetWriter` writes) and
    returns one entry per series directory that holds at least one ``.parquet``
    file. Directory names that are not safe option segments are skipped (a
    foreign / partially-written tree never raises). A missing ``options``
    directory yields ``[]``.

    The expired-option lake has no ``(exchange, segment, symbol, interval)``
    coordinates the way the spot ``bars/`` tree does — one series IS one
    partition key (``exchange_segment|security_id|expiry_flag|expiry_code|
    strike|drv_option_type``, ``|`` → ``__`` on disk). The whole series rolls a
    1-minute table, so we surface the ``series`` segment as the dataset
    coordinate and stamp ``"1minute"`` as the interval for a uniform picker.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``options/``).

    Returns
    -------
    list[dict[str, str]]
        ``[{"kind": "option", "series", "interval"}]`` sorted by series — the
        candle-viewer's option-dataset picker entries.
    """
    options = lake_root / OPTIONS_DIR_NAME
    if not options.is_dir():
        return []
    found: list[dict[str, str]] = []
    for series_dir in sorted(p for p in options.iterdir() if p.is_dir()):
        if ".." in series_dir.name or not _OPTION_SEGMENT_RE.match(series_dir.name):
            continue
        if any(series_dir.glob("*.parquet")):
            found.append(
                {
                    "kind": "option",
                    "series": series_dir.name,
                    "interval": "1minute",
                }
            )
    return found


def query_candles(
    conn: Any,
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    start: str | None = None,
    end: str | None = None,
    columns: tuple[str, ...] | None = None,
    descending: bool = False,
    limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    """Read one page of OHLCV candles for a partition set (DEC-DB-QUERY-001).

    Reads the Parquet lake directly via DuckDB ``read_parquet`` over the
    partition glob ``<lake>/bars/<exch>/<seg>/<symbol>/<interval>/**/*.parquet``
    — no data duplication, the lake stays the single source of truth.
    The glob is scoped to ONE ``(symbol, interval)`` (its whole-year/month
    files only), and the page is ``LIMIT``-bounded — so a click never
    materialises more than ``limit`` rows in Python regardless of how many
    years are stored (DuckDB additionally skips row-groups via min/max stats;
    this is engine cost, not directory pruning). *start* / *end* are inclusive
    ISO ``YYYY-MM-DD`` bounds matched against the bar's UTC calendar date.
    Returns an empty
    page (never raises) when the partition set has no Parquet files yet.

    Parameters
    ----------
    conn
        A DuckDB connection.
    lake_root
        Top-level lake directory (parent of ``bars/``).
    exchange, segment, symbol, interval
        Partition coordinates; validated against the partition allowlists.
    start, end
        Inclusive ISO ``YYYY-MM-DD`` bounds on the bar timestamp, or ``None``.
    columns
        OHLCV columns to project (subset of :data:`CANDLE_COLUMNS`), or
        ``None`` for all. An empty tuple is rejected.
    descending, limit, offset
        Sort direction on ``timestamp`` + page bounds (clamped).

    Returns
    -------
    dict[str, Any]
        ``{"symbol", "interval", "columns", "rows", "total", "limit",
        "offset"}`` with every cell JSON-safe.

    Raises
    ------
    ValueError
        On an invalid coordinate, an unknown column, an empty column tuple,
        or a malformed ISO date bound.
    """
    _validate_candle_coords(exchange, segment, symbol, interval)
    valid_columns = frozenset(CANDLE_COLUMNS)
    select = _select_list(columns, valid_columns)
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)

    # Compare the bar's UTC *calendar date* — `"timestamp"` is TIMESTAMPTZ
    # (parquet_writer stores time_zone="UTC"); the shared `_date_bounds` pins
    # the wall-clock to UTC and truncates, so the bound comparison is
    # independent of the ambient DuckDB session `TimeZone` (the operator runs
    # Asia/Kolkata). Both bounds inclusive (Golden Rule #7 — same UTC date in
    # → same rows out, regardless of session tz).
    where_clauses, where_params = _date_bounds(start, end)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    glob = (
        lake_root / "bars" / exchange / segment / symbol / interval
    ).as_posix() + "/**/*.parquet"
    # Empty / not-yet-pulled partition set → return an empty page instead of
    # letting read_parquet raise "No files found".
    base = lake_root / "bars" / exchange / segment / symbol / interval
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {
            "symbol": symbol,
            "interval": interval,
            "columns": list(columns) if columns is not None else list(CANDLE_COLUMNS),
            "rows": [],
            "total": 0,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    # The glob is built from coordinates validated above (no free user text),
    # so interpolating it is safe; the source is given to read_parquet as a
    # bound parameter and every date/page value is parameter-bound.
    source = "read_parquet(?, union_by_name=true)"
    order_sql = ' ORDER BY "timestamp" ' + ("DESC" if descending else "ASC")
    total_row = conn.execute(
        f"SELECT COUNT(*) FROM {source}{where_sql}",  # noqa: S608  # nosec B608 - source is read_parquet(?) with a bound path; values bound
        [glob, *where_params],
    ).fetchone()
    total = int(total_row[0]) if total_row else 0
    cursor = conn.execute(
        f"SELECT {select} FROM {source}{where_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - validated select + bound path + bound values
        [glob, *where_params, safe_limit, safe_offset],
    )
    description = cursor.description or []
    out_columns = [str(col[0]) for col in description]
    # Render the bar timestamp as a clean IST wall-clock (no +05:30 suffix);
    # every other cell goes through the generic JSON coercion (shared helper).
    rows = _render_candle_rows(cursor, out_columns)
    return {
        "symbol": symbol,
        "interval": interval,
        "columns": out_columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


def _date_bounds(start: str | None, end: str | None) -> tuple[list[str], list[Any]]:
    """Build the inclusive UTC-date ``WHERE`` fragments shared by both readers.

    Returns ``(clauses, params)`` where each clause compares the bar's UTC
    *calendar date* (``("timestamp" AT TIME ZONE 'UTC')::DATE``) against a bound
    ISO date — independent of the ambient DuckDB session ``TimeZone`` (the
    operator runs Asia/Kolkata). Both bounds inclusive (Golden Rule #7). A
    ``None`` bound contributes nothing.

    Raises
    ------
    ValueError
        On a malformed ISO date bound (via :func:`_parse_iso_date`).
    """
    clauses: list[str] = []
    params: list[Any] = []
    if start is not None:
        clauses.append("(\"timestamp\" AT TIME ZONE 'UTC')::DATE >= ?")
        params.append(_parse_iso_date(start, "start").isoformat())
    if end is not None:
        clauses.append("(\"timestamp\" AT TIME ZONE 'UTC')::DATE <= ?")
        params.append(_parse_iso_date(end, "end").isoformat())
    return clauses, params


def _render_candle_rows(cursor: Any, out_columns: list[str]) -> list[list[Any]]:
    """Render a candle/option cursor's rows with a clean IST ``timestamp``.

    The ``timestamp`` cell (if projected) is rendered as a clean IST wall-clock
    string (no ``+05:30`` suffix) via :func:`_format_candle_timestamp`; every
    other cell goes through the generic :func:`_json_safe` coercion. Shared by
    :func:`query_candles` (spot) and :func:`query_option_candles` (options).
    """
    ts_idx = out_columns.index("timestamp") if "timestamp" in out_columns else -1
    return [
        [
            _format_candle_timestamp(cell) if i == ts_idx else _json_safe(cell)
            for i, cell in enumerate(row)
        ]
        for row in cursor.fetchall()
    ]


def query_option_candles(
    conn: Any,
    lake_root: Path,
    *,
    series: str,
    start: str | None = None,
    end: str | None = None,
    columns: tuple[str, ...] | None = None,
    descending: bool = False,
    limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    """Read one page of expired-option bars for a series (DEC-DB-QUERY-001).

    The options-lake analog of :func:`query_candles`: reads the Parquet lake
    directly via DuckDB ``read_parquet`` over the series glob
    ``<lake>/options/<series>/*.parquet`` (the layout
    :class:`~brutex.data.option_parquet_writer.OptionParquetWriter` writes) — no
    data duplication, the lake stays the single source of truth. *series* is the
    filesystem-safe segment from :func:`list_option_datasets` and is validated
    against :func:`_validate_option_segment` (NO free user text in the glob,
    same path/SQL-injection guard as the spot reader). The page is
    ``LIMIT``-bounded so a click never materialises more than *limit* rows.

    Projects the 11 :data:`OPTION_CANDLE_COLUMNS` — the OHLCV+OI set PLUS the
    per-bar option stamps ``side`` / ``strike`` / ``spot`` / ``iv``. *start* /
    *end* are inclusive ISO ``YYYY-MM-DD`` bounds matched against the bar's UTC
    calendar date. Returns an empty page (never raises) when the series has no
    Parquet files yet.

    Parameters
    ----------
    conn
        A DuckDB connection.
    lake_root
        Top-level lake directory (parent of ``options/``).
    series
        The option-series segment; validated against the safe-segment grammar.
    start, end
        Inclusive ISO ``YYYY-MM-DD`` bounds on the bar timestamp, or ``None``.
    columns
        Option columns to project (subset of :data:`OPTION_CANDLE_COLUMNS`), or
        ``None`` for all. An empty tuple is rejected.
    descending, limit, offset
        Sort direction on ``timestamp`` + page bounds (clamped).

    Returns
    -------
    dict[str, Any]
        ``{"series", "interval", "columns", "rows", "total", "limit",
        "offset"}`` with every cell JSON-safe (``iv`` kept full-precision —
        it is a volatility metric, NOT money, mirroring the writer).

    Raises
    ------
    ValueError
        On an illegal series, an unknown column, an empty column tuple, or a
        malformed ISO date bound.
    """
    _validate_option_segment(series)
    valid_columns = frozenset(OPTION_CANDLE_COLUMNS)
    select = _select_list(columns, valid_columns)
    safe_limit = clamp_limit(limit)
    safe_offset = clamp_offset(offset)

    where_clauses, where_params = _date_bounds(start, end)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    base = lake_root / OPTIONS_DIR_NAME / series
    glob = base.as_posix() + "/*.parquet"
    # Empty / not-yet-pulled series → return an empty page instead of letting
    # read_parquet raise "No files found" (mirrors query_candles).
    if not (base.is_dir() and any(base.glob("*.parquet"))):
        return {
            "series": series,
            "interval": "1minute",
            "columns": list(columns) if columns is not None else list(OPTION_CANDLE_COLUMNS),
            "rows": [],
            "total": 0,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    # The glob is built from the validated series segment (no free user text),
    # so interpolating it is safe; the source is given to read_parquet as a
    # bound parameter and every date/page value is parameter-bound.
    source = "read_parquet(?, union_by_name=true)"
    order_sql = ' ORDER BY "timestamp" ' + ("DESC" if descending else "ASC")
    total_row = conn.execute(
        f"SELECT COUNT(*) FROM {source}{where_sql}",  # noqa: S608  # nosec B608 - source is read_parquet(?) with a bound path; values bound
        [glob, *where_params],
    ).fetchone()
    total = int(total_row[0]) if total_row else 0
    cursor = conn.execute(
        f"SELECT {select} FROM {source}{where_sql}{order_sql} LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608 - validated select + bound path + bound values
        [glob, *where_params, safe_limit, safe_offset],
    )
    description = cursor.description or []
    out_columns = [str(col[0]) for col in description]
    rows = _render_candle_rows(cursor, out_columns)
    return {
        "series": series,
        "interval": "1minute",
        "columns": out_columns,
        "rows": rows,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
    }


def _parse_iso_date(value: str, field_name: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` string, re-raising with the field name.

    Rejects the looser forms ``date.fromisoformat`` accepts (``YYYYMMDD``,
    ISO week dates) so the contract matches what the date-picker emits.
    """
    if not _ISO_DATE_RE.match(value):
        raise ValueError(f"{field_name} must be ISO YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO YYYY-MM-DD, got {value!r}") from exc


__all__ = [
    "ALLOWED_OPERATORS",
    "CANDLE_COLUMNS",
    "KNOWN_TABLES",
    "OPTIONS_DIR_NAME",
    "OPTION_CANDLE_COLUMNS",
    "clamp_limit",
    "clamp_offset",
    "list_candle_datasets",
    "list_columns",
    "list_option_datasets",
    "query_candles",
    "query_option_candles",
    "query_table",
    "sample_rows",
    "summarize_spot_datasets",
    "table_summary",
]
