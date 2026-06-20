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
import threading
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import duckdb
import structlog

from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
)
from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.data.fno_candle_lake import (
    FNO_CANDLE_COLUMNS,
    FNO_CANDLE_LAKE_SEGMENT,
)
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.partition import (
    VALID_EXCHANGES,
    VALID_INTERVALS,
    VALID_SEGMENTS,
)
from brutex.data.session import format_ist_clock
from brutex.data.spot_features import pct_change
from brutex.data.window_ledger import EMPTY_TERMINAL_THRESHOLD

#: Structured logger — surfaces the otherwise-silent coverage-read degradation
#: (a single corrupt / version-incompatible Parquet file is SKIPPED, logged as
#: ``coverage.parquet_skip``, instead of 500-ing the whole endpoint) into the
#: same structlog stream the operator reads on ``/logs``
#: (DEC-COVERAGE-PARQUET-RESILIENT-001).
_LOG = structlog.get_logger("brutex.dashboard.db_view")

#: The ONLY tables the viewer may touch — the real DuckDB tables created by
#: :meth:`brutex.data.instrument_store.InstrumentStore.open`. A
#: caller-supplied table name is checked against this allowlist before
#: it is interpolated into any SQL string (SQL-injection guard). The retired
#: ``expiries`` / ``contracts`` tables were dropped in
#: DEC-EXPIRED-OPTIONS-TABLE-001 — the expired-options Parquet lake
#: (``<lake>/options/…``, browsed via :func:`list_option_datasets` +
#: :func:`query_option_candles`) is their replacement.
KNOWN_TABLES: Final = (
    "instruments",
    "instrument_runs",
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

#: The 18 columns every expired-option Parquet partition carries
#: (``brutex/data/option_parquet_writer.py`` ``_render_df`` schema,
#: DEC-EXPIRED-OPTIONS-TABLE-001): the 7 series-addressing columns promoted
#: from the partition path (``exch_id`` / ``exchange_segment`` /
#: ``security_id`` / ``underlying_symbol`` / ``expiry_flag`` / ``expiry_code``
#: / ``strike_offset``) PLUS the per-bar ``side`` and the OHLCV+OI value set
#: PLUS the per-bar option stamps ``strike`` / ``spot`` / ``iv`` (the
#: rolling-option endpoint surface). Used to validate an option-candle column
#: selection without touching disk.
OPTION_CANDLE_COLUMNS: Final = (
    "exch_id",
    "exchange_segment",
    "security_id",
    "underlying_symbol",
    "expiry_flag",
    "expiry_code",
    "side",
    "strike_offset",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "strike",
    "spot",
    "iv",
)

#: The subdirectory under the lake root holding the expired-options table
#: (``<lake>/options/<series>/<YYYY>-<MM>.parquet``). Mirrors
#: ``brutex.data.option_parquet_writer._OPTIONS_SUBDIR`` (kept in lockstep) so
#: the read-side viewer never traverses outside ``<lake>/options``.
OPTIONS_DIR_NAME: Final = "options"

#: The F&O expiries+contracts REGISTRY cache file
#: (:class:`brutex.data.registry_store.RegistryStore`,
#: DEC-DERIV-METADATA-HISTORICAL-SPLIT-001) — a SIBLING of the lean instrument
#: master (``instruments.duckdb``), so it lives in the SAME directory (the lake
#: root). :func:`registry_path_for` resolves it from the master's on-disk path
#: the same way the candle/option readers resolve the lake root, keeping the
#: ``/db`` page's path-resolution discipline in one place (CLAUDE.md §11 COMMON).
REGISTRY_DB_NAME: Final = "registry.duckdb"

#: The 7 browsable columns surfaced for the registry ``expiries`` table — the
#: ``expiries`` schema minus ``last_run_id`` (an opaque blake3 run id, noise for
#: the operator), in a stable display order. ``year`` is the derived Phase-1 cell
#: key (:attr:`brutex.data.registry_store.ExpiryRow.year`).
REGISTRY_EXPIRY_COLUMNS: Final = (
    "exch",
    "underlying",
    "expiry_date",
    "year",
    "fetched_at",
)

#: The 8 browsable columns surfaced for the registry ``contracts`` table — the
#: structured chain view the operator asked for (expiry / strike / CE-PE /
#: per-contract historical ``candle_status``), ``last_run_id`` again omitted.
REGISTRY_CONTRACT_COLUMNS: Final = (
    "exch",
    "underlying",
    "expiry_date",
    "contract_symbol",
    "strike",
    "side",
    "candle_status",
    "fetched_at",
)

#: A filesystem-safe option series segment is the writer's ``_safe_segment``
#: output: a ``|``-joined series key with ``|`` rewritten to ``__``. The real
#: parts are alnum + ``.`` + ``-`` + ``+`` (vendor coordinates) joined by
#: ``__``; we allow exactly that grammar so a discovered directory name can
#: never carry a traversal/separator character into the read glob (``..``
#: rejected separately, matching the writer's own guard).
#:
#: ``+`` is admitted because the writer stores it verbatim
#: (DEC-DB-OPTION-DATASET-DECODE-001): the upper half of every strike chain
#: uses the rolling-option ``ATM+N`` selector (``option_universe_ingest``
#: emits ``ATM+1`` … ``ATM+10``), and ``_safe_segment`` only rewrites ``|`` —
#: so the lake holds ``…__ATM+3__CALL`` directories. Before this fix those
#: series were silently SKIPPED by :func:`list_option_datasets` AND refused by
#: :func:`query_option_candles` (the read allowlist dropped what the write
#: path stores). ``+`` is neither a path separator nor a DuckDB-glob
#: metacharacter (``* ? [ ] { }``), so admitting it cannot widen the bound
#: ``read_parquet`` glob or traverse the tree — the same allowlist-consistency
#: class as DEC-PREOPEN-AMPERSAND-001 (``&``) and DEC-PREOPEN-SPACE-SYMBOL-001
#: (space) on the spot side (CLAUDE.md §11 COMMON).
_OPTION_SEGMENT_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-]*$")

#: A candle symbol must match this allowlist before it is ever joined into
#: a lake path / ``read_parquet`` glob — defence-in-depth against path
#: traversal even though discovered symbols come from our own directory
#: scan. Vendor symbols are alnum + ``-`` + ``_`` + ``.`` + ``&`` (e.g.
#: ``NSE-NIFTY-30Jan25-28500-CE``, ``M&M``); ``..`` is rejected separately.
#:
#: ``&`` is admitted because real NSE/BSE underlyings legitimately contain it —
#: Mahindra & Mahindra (``M&M`` / ``M&MFIN``) and GE Vernova T&D (``GVT&D``) are
#: F&O-eligible spot symbols the ingest writes verbatim (the partition layer's
#: filesystem denylist in ``brutex.data.partition`` permits ``&``, so the lake
#: holds a ``…/M&M/…`` directory). ``&`` is neither a path separator nor a
#: DuckDB-glob metacharacter (``* ? [ ] { }``), so it cannot widen the bound
#: ``read_parquet`` glob or traverse the tree — admitting it keeps this read
#: allowlist consistent with what the write path stores (CLAUDE.md §11 COMMON)
#: WITHOUT loosening the path-traversal / glob-injection guard. Before this fix
#: the pre-open report 500'd on ``M&M`` (``illegal candle symbol 'M&M'``).
#: DEC-PREOPEN-AMPERSAND-001.
#:
#: A literal **space** is admitted for the same reason (DEC-PREOPEN-SPACE-SYMBOL-001):
#: real INDEX underlyings the lake stores verbatim contain spaces — ``INDIA VIX``,
#: ``NIFTY 50``, ``NIFTY TOTAL MKT``, ``NIFTY MIDCAP 150`` — and a space is neither a
#: path separator nor a DuckDB-glob metacharacter (``* ? [ ] { }``). The symbol is
#: joined via ``pathlib`` into ``<lake>/bars/<exch>/<seg>/<symbol>/<interval>`` and
#: handed to ``read_parquet(?)`` as a **bound parameter** (never string-interpolated),
#: so the space is an inert literal path component — it cannot widen the glob, inject
#: SQL, or traverse (``..`` still rejected). Before this fix the pre-open ``.zip``
#: 500'd on ``INDIA VIX`` (``illegal candle symbol 'INDIA VIX'``), and one bad symbol
#: sank the whole multi-symbol export; admitting the space keeps the read allowlist
#: consistent with what the write path stores (CLAUDE.md §11 COMMON) so no stored
#: data is dropped.
_SYMBOL_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.&\- ]*$")

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

#: The daily-bar partition interval — its bars render DATE-ONLY (no noise
#: ``00:00:00`` suffix) in the candle viewer (DEC-DB-CANDLE-DERIVED-001).
_DAILY_INTERVAL: Final[str] = "1day"

#: The 1-minute base partition — the source of the previous-day close BASELINE
#: for the derived %-change / gap columns. The engine is 1-minute-based, so the
#: baseline is the prior trading day's **last 1-minute close** (the 15:29 IST
#: bar), NOT the 1-day official close — this keeps ``change_pct`` / ``open_gap_pct``
#: internally consistent with the 1-minute feed the engine actually sweeps
#: (DEC-SPOT-PREVCLOSE-1MIN-001; operator 2026-06-03). The 1-day official close
#: (= the NSE settlement / "previous close") is reserved for the separate NSE
#: pre-open Excel report, never the engine store.
_INTRADAY_BASE_INTERVAL: Final[str] = "1minute"

#: 09:15:00 IST — the regular NSE/BSE session open. The overnight ``open_gap_pct``
#: derived column (DEC-DB-CANDLE-DERIVED-001) is computed ONLY for the bar at
#: this instant; mirrors ``brutex.data.spot_features._SESSION_OPEN`` (§11 COMMON).
_SESSION_OPEN_IST: Final[time] = time(9, 15)

#: The two server-computed derived columns appended to every spot candle page
#: (DEC-DB-CANDLE-DERIVED-001): previous-day % change + the 09:15 overnight gap.
#: They are NOT real Parquet columns (never selectable in the column picker);
#: the viewer computes them per bar from the symbol's prior-day last 1-minute
#: close (DEC-SPOT-PREVCLOSE-1MIN-001).
SPOT_DERIVED_COLUMNS: Final = ("change_pct", "open_gap_pct")


def _format_candle_timestamp(value: Any, interval: str) -> Any:
    """Render a candle bar timestamp as a clean IST wall-clock string.

    Drops the ``+05:30`` offset suffix (operator: "it should be clean") — a
    bar stored as ``2026-05-29T03:30:00Z`` renders ``2026-05-29 09:00:00``
    (IST), never ``2026-05-29 09:00:00+05:30``. Converting to :data:`_IST`
    explicitly keeps the output identical whether the DuckDB session runs in
    UTC (CI) or Asia/Kolkata (the operator's box). Non-datetime cells (a
    column subset that excludes ``timestamp`` never reaches here) fall back to
    :func:`_json_safe`.

    For the ``"1day"`` *interval* the bar is rendered **DATE-ONLY**
    (``2026-05-29``) — the ``00:00:00`` time component is noise for a daily bar
    (DEC-DB-CANDLE-DERIVED-001); every intraday interval keeps the full
    ``YYYY-MM-DD HH:MM:SS`` wall-clock.
    """
    if isinstance(value, datetime):
        ist = value.astimezone(_IST) if value.tzinfo is not None else value
        fmt = "%Y-%m-%d" if interval == _DAILY_INTERVAL else "%Y-%m-%d %H:%M:%S"
        return ist.strftime(fmt)
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


def list_fno_candle_datasets(lake_root: Path) -> list[dict[str, Any]]:
    """Discover the F&O candle datasets, each decoded to its contract coordinates.

    The F&O analog of :func:`list_candle_datasets`: keeps ONLY the ``segment="FNO"``
    partition sets — the futures + options the Groww historical pull writes to
    ``bars/<exch>/FNO/<groww_symbol>/<interval>/…`` (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) —
    and enriches each with the contract identity decoded from its ``groww_symbol`` via
    :func:`~brutex.data.fno_symbol.parse_fno_symbol` (underlying / expiry / strike /
    instrument_type / option_type). This is the first-class "F&O candle lake" surface
    the ``/db`` browser renders, distinct from (and no longer conflated with) the spot
    candle list — the honesty fix of DEC-FNO-CANDLE-LAKE-001: an F&O contract bar is
    NOT spot.

    A dataset whose directory name does NOT decode as a contract symbol is STILL listed
    (with ``None`` identity fields) rather than hidden — the lake is the single source
    of truth and a present-but-undecodable partition is surfaced honestly, never dropped
    (Golden Rule #1). The ``(exchange, segment, symbol, interval)`` coordinates are the
    SAME keys :func:`query_candles` takes, so the viewer queries an F&O contract through
    the identical ``segment="FNO"`` candle path — zero new query surface.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).

    Returns
    -------
    list[dict[str, Any]]
        One entry per ``(exchange, FNO, groww_symbol, interval)`` set, in
        :func:`list_candle_datasets` order, each shaped::

            {"kind": "fno", "exchange", "segment", "symbol", "interval",
             "underlying": str|None, "expiry": "YYYY-MM-DD"|None,
             "strike": str|None, "instrument_type": str|None, "option_type": str|None}
    """
    out: list[dict[str, Any]] = []
    for dataset in list_candle_datasets(lake_root):
        if dataset["segment"] != FNO_CANDLE_LAKE_SEGMENT:
            continue
        parts = parse_fno_symbol(dataset["symbol"])
        out.append(
            {
                "kind": "fno",
                "exchange": dataset["exchange"],
                "segment": dataset["segment"],
                "symbol": dataset["symbol"],
                "interval": dataset["interval"],
                "underlying": parts.underlying if parts else None,
                "expiry": parts.expiry.isoformat() if parts else None,
                "strike": str(parts.strike) if parts and parts.strike is not None else None,
                "instrument_type": parts.instrument_type if parts else None,
                "option_type": parts.option_type if parts else None,
            }
        )
    return out


def fno_candle_lake_schema() -> list[dict[str, Any]]:
    """Return the F&O candle logical-table schema (the documented column set).

    A JSON-friendly projection of
    :data:`brutex.data.fno_candle_lake.FNO_CANDLE_COLUMNS` — the documented columns
    (the 7 physical OHLCV columns + the partition / symbol identity columns) — the
    programmatic twin of the ``schema_meta`` registration written at store-open
    (DEC-FNO-CANDLE-LAKE-001). Served on the candle-datasets endpoint for API clients;
    the operator-facing surface of the same schema is the browsable ``schema_meta``
    table row in ``/db``. Pure projection — no disk access.
    """
    return [
        {
            "name": c.name,
            "dtype": c.dtype,
            "source": c.source,
            "nullable": c.nullable,
            "doc": c.doc,
        }
        for c in FNO_CANDLE_COLUMNS
    ]


def _aggregate_parquet_resilient(
    conn: Any, directory: Path, sql: str, *, source: str
) -> list[tuple[Any, ...]]:
    """Run *sql* — one ``read_parquet(?, union_by_name=true)`` aggregate over the
    ``directory`` glob — and return its rows, **resilient to an unreadable file**.

    Fast path (steady state): bind ``<directory>/**/*.parquet`` to the single
    ``?`` and ``fetchall()`` — byte-identical to a direct query, one scan.

    Degraded path: a single corrupt / version-incompatible Parquet file in the
    lake (seen in the wild as DuckDB ``don't know what type: \\r``) makes the
    whole-glob scan raise :class:`duckdb.Error`, which would 500 a coverage
    endpoint that documents *"never a 500"*. So on that error we probe each
    matched file, **skip** the unreadable one(s) — each logged as
    ``coverage.parquet_skip`` with its path + DuckDB reason so the operator can
    quarantine it — and re-run *sql* over the readable subset (bound as a LIST).
    When NO file is readable we log ``coverage.parquet_unreadable`` and return
    ``[]`` (an empty summary, never an exception). Honest envelope (Golden Rule
    #10): a skipped file is OMITTED from coverage, never silently counted.

    The per-file probing is paid ONLY when a read actually fails; the happy path
    stays a single query (Golden Rule #5 O(1) steady-state).
    """
    glob = directory.as_posix() + "/**/*.parquet"
    try:
        return list(conn.execute(sql, [glob]).fetchall())
    except duckdb.Error as whole_glob_error:
        readable: list[str] = []
        for path in sorted(directory.rglob("*.parquet")):
            candidate = path.as_posix()
            try:
                conn.execute("SELECT 1 FROM read_parquet(?) LIMIT 1", [candidate]).fetchone()
            except duckdb.Error as skip_reason:
                _LOG.warning(
                    "coverage.parquet_skip",
                    source=source,
                    path=candidate,
                    error=str(skip_reason),
                )
            else:
                readable.append(candidate)
        if not readable:
            _LOG.error("coverage.parquet_unreadable", source=source, error=str(whole_glob_error))
            return []
        return list(conn.execute(sql, [readable]).fetchall())


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
    stored, NOT whether the full ``2020 → today`` range is complete.
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
        # The glob is built from list_candle_datasets coordinates (each already
        # validated against the partition allowlists during discovery), never
        # free user text; the path is given to read_parquet as a bound parameter.
        # Resilient read: a single unreadable Parquet file degrades this dataset
        # to its readable subset (or 0 bars), never a 500 (DEC-COVERAGE-PARQUET-
        # RESILIENT-001).
        agg_rows = _aggregate_parquet_resilient(
            conn,
            lake_root / "bars" / exchange / segment / symbol / interval,
            'SELECT COUNT(*), MIN("timestamp"), MAX("timestamp") '
            "FROM read_parquet(?, union_by_name=true)",
            source=f"spot:{exchange}/{segment}/{symbol}/{interval}",
        )
        agg = agg_rows[0] if agg_rows else None
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


def summarize_option_datasets(conn: Any, lake_root: Path) -> list[dict[str, Any]]:
    """Summarise the expired-options lake per underlying for the coverage page.

    The options-side analog of :func:`summarize_spot_datasets`
    (DEC-OPTIONS-COVERAGE-LAKE-001): reads ONE aggregate per
    ``(exch_id, underlying_symbol)`` straight from the expired-options Parquet
    lake (``<lake>/options/**/*.parquet``) via DuckDB ``read_parquet`` — the
    number of distinct series, total bars, and first/last bar date. The inner
    GROUP BY collapses each ``(expiry_flag, expiry_code, strike_offset, side,
    strike)`` series; the outer GROUP BY rolls those up per underlying, so
    ``series`` is a plain ``COUNT(*)`` of the inner rows. Read-only by
    construction — a single aggregate query, no bar rows materialised. A missing
    / empty ``options`` directory yields ``[]`` (mirrors :func:`list_option_series`).

    **Honest envelope (Golden Rule #10):** a series PRESENT in the lake counts
    as *pulled*; ``series`` / ``bars`` / ``first_date`` / ``last_date`` describe
    WHAT is stored, NOT whether the full ``2020 → today`` range is complete —
    partial-range detection is an explicit follow-up, mirroring the spot side.

    Parameters
    ----------
    conn
        A DuckDB connection.
    lake_root
        Top-level lake directory (the parent of ``options/``).

    Returns
    -------
    list[dict[str, Any]]
        One row per underlying, sorted by ``(exchange, underlying)``::

            {"underlying", "exchange", "series": <int>, "bars": <int>,
             "first_date": <YYYY-MM-DD|None>, "last_date": <YYYY-MM-DD|None>}
    """
    options = lake_root / OPTIONS_DIR_NAME
    if not options.is_dir() or not any(options.rglob("*.parquet")):
        return []
    # read_parquet path is parameter-bound; the column list is the fixed
    # addressing set (no free user text). Inner query = one row per series;
    # the outer rolls up per underlying, so COUNT(*) is the series count.
    # Resilient read: one corrupt Parquet file is SKIPPED (logged), never a 500
    # (DEC-COVERAGE-PARQUET-RESILIENT-001).
    rows = _aggregate_parquet_resilient(
        conn,
        options,
        "SELECT exch_id, underlying_symbol, COUNT(*) AS series, "  # nosec B608 - static SQL, parameter-bound path
        "SUM(bars) AS bars, MIN(first_ts) AS first_ts, MAX(last_ts) AS last_ts FROM ("
        "  SELECT exch_id, underlying_symbol, expiry_flag, expiry_code, strike_offset, "
        '         side, strike, COUNT(*) AS bars, MIN("timestamp") AS first_ts, '
        '         MAX("timestamp") AS last_ts '
        "  FROM read_parquet(?, union_by_name=true) "
        "  GROUP BY exch_id, underlying_symbol, expiry_flag, expiry_code, strike_offset, side, strike"
        ") GROUP BY exch_id, underlying_symbol ORDER BY exch_id, underlying_symbol",
        source="options",
    )
    return [
        {
            "underlying": row[1],
            "exchange": row[0],
            "series": int(row[2]),
            "bars": int(row[3]),
            "first_date": _ist_date(row[4]),
            "last_date": _ist_date(row[5]),
        }
        for row in rows
    ]


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


#: The on-disk separator the option writer substitutes for the series key's
#: ``|`` (``option_parquet_writer._SAFE``, kept in lockstep like
#: :data:`OPTIONS_DIR_NAME`).
_OPTION_SEGMENT_SEP: Final = "__"

#: The option series directory segment's part arity: the writer's partition
#: key is ``<exchange_segment>|<security_id>|<expiry_flag>|<expiry_code>|
#: <strike>|<drv_option_type>`` (``option_sink.append_option_bars``), so the
#: on-disk segment splits into exactly 6 parts on ``__``.
_OPTION_SERIES_ARITY: Final = 6


def _decode_option_dataset(series: str, symbol_for_id: Mapping[str, str] | None) -> str | None:
    """Decode one on-disk option series segment into a PRECISE display label.

    The dataset-picker twin of :func:`_decode_option_window`
    (DEC-DB-OPTION-DATASET-DECODE-001 — the #629 tracked follow-up): the /db
    expired-options picker rendered the raw directory segment
    (``NSE_FNO__25__MONTH__1__ATM-10__CALL`` — the numeric vendor id, not the
    operator's symbol). Returns a ``"BANKNIFTY · MONTH#1 ATM-10 CALL · 1m"``-style
    label, or ``None`` for any shape that does not parse (wrong part arity, an
    empty part) — the caller keeps the raw segment as the label: fallback,
    never an exception, never a hidden dataset. An unknown id keeps the numeric
    id so the entry is still distinguishable. Display-only: the ``series`` KEY
    the queries address is never touched.
    """
    parts = series.split(_OPTION_SEGMENT_SEP)
    if len(parts) != _OPTION_SERIES_ARITY or not all(parts):
        return None
    _segment, sid, flag, code, strike, opt_type = parts
    label = (symbol_for_id or {}).get(sid, sid)
    return f"{label} · {flag}#{code} {strike} {opt_type} · 1m"


def list_option_datasets(
    lake_root: Path,
    *,
    symbol_for_id: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
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

    Each entry additionally carries a display-only ``label``
    (DEC-DB-OPTION-DATASET-DECODE-001): the series decoded to the operator's
    coordinates (``BANKNIFTY · MONTH#1 ATM-10 CALL · 1m``) via
    :func:`_decode_option_dataset`, falling back to the RAW segment for any
    non-parsing shape — a dataset is never hidden and the ``series`` KEY stays
    byte-unchanged (queries keep addressing the on-disk segment).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``options/``).
    symbol_for_id
        Optional ``str(SECURITY_ID) → engine symbol`` map (the
        DEC-WINDOW-OPTION-DECODE-001 pattern) so the label shows the
        underlying's symbol (``BANKNIFTY``) instead of the vendor id the
        segment carries (``25``). ``None`` / a missing id keeps the numeric
        id — display-only, never a data change.

    Returns
    -------
    list[dict[str, str]]
        ``[{"kind": "option", "series", "interval", "label"}]`` sorted by
        series — the candle-viewer's option-dataset picker entries.
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
                    "label": _decode_option_dataset(series_dir.name, symbol_for_id)
                    or series_dir.name,
                }
            )
    return found


def list_option_series(
    conn: Any,
    lake_root: Path,
    *,
    underlying_symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Return the DISTINCT expired-option series in the lake (DEC-EXPIRED-OPTIONS-TABLE-001).

    Replaces the dropped ``expiries`` / ``contracts`` DuckDB tables as the
    UI's option-series source: reads the expired-options Parquet lake
    (``<lake>/options/**/*.parquet``) and returns one entry per distinct
    ``(exchange, underlying_symbol, expiry_flag, expiry_code, strike_offset,
    side, strike)`` tuple, derived from the real addressing columns now
    promoted onto every bar. Filters by *underlying_symbol* when supplied.

    A missing / empty ``options`` directory yields ``[]`` (never raises),
    mirroring :func:`list_option_datasets`.

    Parameters
    ----------
    conn
        A DuckDB connection.
    lake_root
        Top-level lake directory (the parent of ``options/``).
    underlying_symbol
        Restrict to one underlying (e.g. ``"NIFTY"``), or ``None`` for all.

    Returns
    -------
    list[dict[str, Any]]
        ``[{"exchange", "underlying_symbol", "expiry_flag", "expiry_code",
        "strike_offset", "side", "strike"}]`` sorted for a stable UI order.
    """
    options = lake_root / OPTIONS_DIR_NAME
    if not options.is_dir() or not any(options.rglob("*.parquet")):
        return []
    glob = options.as_posix() + "/**/*.parquet"
    where_sql = ""
    params: list[Any] = [glob]
    if underlying_symbol is not None:
        where_sql = " WHERE underlying_symbol = ?"
        params.append(underlying_symbol)
    # read_parquet path + every value parameter-bound; the SELECT column list
    # is the fixed addressing-column set (no free user text) — no injection.
    rows = conn.execute(
        "SELECT DISTINCT exch_id, underlying_symbol, expiry_flag, expiry_code, "  # noqa: S608  # nosec B608 - bound path + fixed columns
        "strike_offset, side, strike "
        "FROM read_parquet(?, union_by_name=true)"
        f"{where_sql} "
        "ORDER BY underlying_symbol, expiry_flag, expiry_code, strike_offset, side, strike",
        params,
    ).fetchall()
    return [
        {
            "exchange": row[0],
            "underlying_symbol": row[1],
            "expiry_flag": row[2],
            "expiry_code": int(row[3]) if row[3] is not None else None,
            "strike_offset": row[4],
            "side": row[5],
            "strike": float(row[6]) if row[6] is not None else None,
        }
        for row in rows
    ]


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
        empty_cols = list(columns) if columns is not None else list(CANDLE_COLUMNS)
        return {
            "symbol": symbol,
            "interval": interval,
            # The two derived %-change columns (DEC-DB-CANDLE-DERIVED-001) are
            # ALWAYS appended so the viewer's column set is stable across an
            # empty vs populated partition.
            "columns": [*empty_cols, *SPOT_DERIVED_COLUMNS],
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
    # Fetch ONCE: the rendered rows AND the derived %-change columns are both
    # built from the same page (DEC-DB-CANDLE-DERIVED-001).
    raw_rows = cursor.fetchall()
    # Render the bar timestamp as a clean IST wall-clock (no +05:30 suffix,
    # DATE-ONLY for 1day); every other cell goes through the generic JSON
    # coercion (shared helper).
    rendered = _render_candle_rows(raw_rows, out_columns, interval)
    # Derive the %-change columns WITHIN the shown interval's OWN basis — never
    # mix two price scales (DEC-DB-CANDLE-PERINTERVAL-001): a 1-day view compares
    # against the prior **1-day** close (so a demerger/split-adjusted daily series
    # stays self-consistent), a 1-minute view against the prior day's last
    # **1-minute** close (DEC-SPOT-PREVCLOSE-1MIN-001). Mixing them is what made
    # adjustment-mismatched symbols (e.g. TMPV — adjusted daily ~112.9 vs raw
    # intraday ~186.6) read garbage. The engine store is unaffected — this is the
    # read-only viewer. O(1) per-bar lookup once the map is built (CLAUDE.md §5).
    if interval == _DAILY_INTERVAL:
        prev_close = _prev_trading_close(
            _daily_close_by_date(conn, lake_root, exchange=exchange, segment=segment, symbol=symbol)
        )
    else:
        prev_close = _prev_trading_close(
            _intraday_last_close_by_date(
                conn, lake_root, exchange=exchange, segment=segment, symbol=symbol
            )
        )
    derived = _spot_derived_rows(raw_rows, out_columns, prev_close, interval=interval)
    rows = [
        [*rendered_row, change, gap]
        for rendered_row, (change, gap) in zip(rendered, derived, strict=True)
    ]
    return {
        "symbol": symbol,
        "interval": interval,
        "columns": [*out_columns, *SPOT_DERIVED_COLUMNS],
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


def _render_candle_rows(
    raw_rows: list[tuple[Any, ...]], out_columns: list[str], interval: str
) -> list[list[Any]]:
    """Render already-fetched candle/option rows with a clean IST ``timestamp``.

    The ``timestamp`` cell (if projected) is rendered as a clean IST wall-clock
    string (no ``+05:30`` suffix) via :func:`_format_candle_timestamp` — DATE-ONLY
    for the ``"1day"`` *interval* (DEC-DB-CANDLE-DERIVED-001); every other cell
    goes through the generic :func:`_json_safe` coercion. *raw_rows* is the
    already-fetched cursor result (the caller fetches once so the spot reader can
    also derive the %-change columns from the same rows). Shared by
    :func:`query_candles` (spot) and :func:`query_option_candles` (options).
    """
    ts_idx = out_columns.index("timestamp") if "timestamp" in out_columns else -1
    return [
        [
            _format_candle_timestamp(cell, interval) if i == ts_idx else _json_safe(cell)
            for i, cell in enumerate(row)
        ]
        for row in raw_rows
    ]


def _to_decimal(value: Any) -> Decimal:
    """Coerce a DuckDB price cell to an exact :class:`~decimal.Decimal`.

    Spot OHLC is stored Parquet ``Float64`` (DuckDB returns ``float``); a
    pre-existing ``Decimal`` passes through untouched. Used only for the
    derived %-change metrics — full precision, never money-rounded (CLAUDE.md §4).
    """
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _bar_ist_date(ts: datetime) -> date:
    """Return the IST calendar date of *ts* (the bar's trading day).

    IST-anchored (not UTC) so it aligns intraday bars (09:15–15:30 IST) with the
    1d daily bars regardless of whether the daily bar is stamped at IST midnight
    or at a session instant (DEC-DB-CANDLE-DERIVED-001). A naive datetime
    (defensive — DuckDB returns tz-aware) is read as-is.
    """
    return (ts.astimezone(_IST) if ts.tzinfo is not None else ts).date()


def _bar_ist_time(ts: datetime) -> time:
    """Return the IST wall-clock time-of-day of *ts* (for the 09:15 gap anchor)."""
    return (ts.astimezone(_IST) if ts.tzinfo is not None else ts).time()


def _intraday_last_close_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> dict[date, Decimal]:
    """Read the symbol's LAST 1-minute close per day into a ``{ist_date: close}`` dict.

    The %-change / gap baseline is the prior trading day's **last 1-minute close**
    (the 15:29 IST bar), NOT the 1-day official close — the engine is 1-minute-based,
    so anchoring on the 1-minute feed keeps ``change_pct`` / ``open_gap_pct``
    internally consistent with the bars actually swept (DEC-SPOT-PREVCLOSE-1MIN-001;
    operator 2026-06-03).

    Reads ``<lake>/bars/<exch>/<seg>/<symbol>/1minute/**/*.parquet`` via a bound
    DuckDB ``read_parquet`` glob (the coordinates are already validated by
    :func:`_validate_candle_coords` in the caller — no free user text) and
    aggregates with ``arg_max(close, timestamp)`` grouped by the bar DATE — ONE
    columnar pass returns ~one row per trading day, so the per-bar lookup
    downstream is O(1) (CLAUDE.md §5). Session-filtered intraday bars (09:15–15:29
    IST) never cross the IST/UTC date boundary, so ``CAST(timestamp AS DATE)`` IS
    the IST trading date (matches :func:`_bar_ist_date` used on the viewed bars).
    Returns ``{}`` when the symbol has no 1-minute partition yet, so the derived
    columns degrade to blank rather than raising (DEC-DB-CANDLE-DERIVED-001).
    """
    base = lake_root / "bars" / exchange / segment / symbol / _INTRADAY_BASE_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    glob = base.as_posix() + "/**/*.parquet"
    # glob is built from validated coordinates (no free user text); the path is
    # given to read_parquet as a bound parameter. arg_max(close, timestamp) is
    # the close of the day's LAST bar (the 15:29 IST close).
    cursor = conn.execute(
        'SELECT CAST("timestamp" AS DATE) AS d, arg_max(close, "timestamp") AS last_close '
        "FROM read_parquet(?, union_by_name=true) GROUP BY d",
        [glob],
    )
    return {day: _to_decimal(last_close) for day, last_close in cursor.fetchall()}


def _prev_trading_close(last_close: dict[date, Decimal]) -> dict[date, Decimal]:
    """Map each trading date to the PREVIOUS trading day's last 1-minute close.

    Built once from the ``{date: last_1min_close}`` dict by walking the dates in
    sorted order, so a per-bar lookup is O(1) (DEC-DB-CANDLE-DERIVED-001). The
    earliest date has no predecessor and is therefore absent from the result
    (its bars get a blank %-change).
    """
    out: dict[date, Decimal] = {}
    prev: date | None = None
    for day in sorted(last_close):
        if prev is not None:
            out[day] = last_close[prev]
        prev = day
    return out


def _daily_close_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> dict[date, Decimal]:
    """Read the symbol's 1-DAY official close per IST date → ``{ist_date: close}``.

    The same-basis baseline for the ``/db`` **1-day** view (and the NSE pre-open
    report's "previous close") — the prior 1-day close, NOT the 1-minute close
    (DEC-DB-CANDLE-PERINTERVAL-001), so a demerger/split-adjusted daily series
    stays self-consistent. Reads ``<lake>/bars/<exch>/<seg>/<symbol>/1day/**``
    via a bound ``read_parquet`` glob; keyed by the bar's IST date (a 1-day bar
    may be stamped at IST midnight or a session instant, so :func:`_bar_ist_date`
    — never a UTC ``CAST`` — is the trading day). ``{}`` when the 1-day partition
    is absent.
    """
    _validate_candle_coords(exchange, segment, symbol, _DAILY_INTERVAL)
    base = lake_root / "bars" / exchange / segment / symbol / _DAILY_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    glob = base.as_posix() + "/**/*.parquet"
    cursor = conn.execute(
        'SELECT "timestamp", close FROM read_parquet(?, union_by_name=true)',
        [glob],
    )
    return {_bar_ist_date(ts): _to_decimal(close) for ts, close in cursor.fetchall()}


def _daily_open_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> dict[date, Decimal]:
    """Read the symbol's 1-DAY official open per IST date → ``{ist_date: open}``.

    The day's official opening price — on NSE the pre-open auction's indicative
    equilibrium price (IEP) *becomes* the day's 1-day open — used as the IEP for
    the pre-open report (DEC-PREOPEN-REPORT-1DAY-001), on the SAME 1-day basis as
    the report's previous close so the two never mix price scales. Reads the
    1-day partition keyed by :func:`_bar_ist_date`; ``{}`` when absent.
    """
    _validate_candle_coords(exchange, segment, symbol, _DAILY_INTERVAL)
    base = lake_root / "bars" / exchange / segment / symbol / _DAILY_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    glob = base.as_posix() + "/**/*.parquet"
    cursor = conn.execute(
        'SELECT "timestamp", open FROM read_parquet(?, union_by_name=true)',
        [glob],
    )
    return {_bar_ist_date(ts): _to_decimal(open_) for ts, open_ in cursor.fetchall()}


def _intraday_open_by_date(
    conn: Any, lake_root: Path, *, exchange: str, segment: str, symbol: str
) -> dict[date, Decimal]:
    """Read the symbol's 09:15 IST session-open 1-minute OPEN per day → ``{ist_date: open}``.

    The **minute-basis IEP** for the pre-open report (DEC-PREOPEN-MINUTE-BASIS-001):
    the day's FIRST 1-minute bar IS the 09:15:00 IST session open (the intraday
    partition is session-filtered 09:15–15:29 IST), so ``arg_min(open, timestamp)``
    grouped by the bar DATE is the 09:15 open — the true pre-open opening price the
    report header names, on the SAME 1-minute basis as the minute prev-close (the
    prior day's last 1-minute close, :func:`_intraday_last_close_by_date` +
    :func:`_prev_trading_close`). Mirrors :func:`_intraday_last_close_by_date`: a
    bound ``read_parquet`` glob over validated coordinates, ONE columnar pass
    (~one row per trading day → O(1) per-day lookup, CLAUDE.md §5); session bars
    never cross the IST/UTC date boundary so ``CAST(timestamp AS DATE)`` IS the IST
    trading date. ``{}`` when the symbol has no 1-minute partition.
    """
    _validate_candle_coords(exchange, segment, symbol, _INTRADAY_BASE_INTERVAL)
    base = lake_root / "bars" / exchange / segment / symbol / _INTRADAY_BASE_INTERVAL
    if not (base.is_dir() and any(base.rglob("*.parquet"))):
        return {}
    glob = base.as_posix() + "/**/*.parquet"
    # glob built from validated coordinates (no free user text); path is a bound
    # parameter. arg_min(open, timestamp) = the open of the day's FIRST (09:15) bar.
    cursor = conn.execute(
        'SELECT CAST("timestamp" AS DATE) AS d, arg_min(open, "timestamp") AS first_open '
        "FROM read_parquet(?, union_by_name=true) GROUP BY d",
        [glob],
    )
    return {day: _to_decimal(first_open) for day, first_open in cursor.fetchall()}


def _spot_derived_rows(
    raw_rows: list[tuple[Any, ...]],
    out_columns: list[str],
    prev_close_by_date: dict[date, Decimal],
    *,
    interval: str = _INTRADAY_BASE_INTERVAL,
) -> list[tuple[Any, Any]]:
    """Compute the per-bar ``(change_pct, open_gap_pct)`` derived pair.

    For each bar (DEC-DB-CANDLE-DERIVED-001), baselined on *prev_close_by_date* —
    the prior trading day's close **on the shown interval's own basis**
    (DEC-DB-CANDLE-PERINTERVAL-001): the prior 1-day close for a ``"1day"`` view,
    the prior day's last 1-minute close for an intraday view
    (DEC-SPOT-PREVCLOSE-1MIN-001). The two are never mixed, so an
    adjustment-mismatched symbol (e.g. TMPV) stays precise:

    * ``change_pct = (close − prev_close) / prev_close × 100``;
    * ``open_gap_pct = (open − prev_close) / prev_close × 100`` — for a ``"1day"``
      view computed on EVERY bar (the open-vs-prior-close gap); for an intraday
      view computed ONLY for the day's 09:15:00 IST market-open bar (the overnight
      gap), ``None`` on every other intraday bar.

    Both reuse the COMMON :func:`brutex.data.spot_features.pct_change` primitive
    (CLAUDE.md §11) and stay full precision — statistical metrics, never
    money-rounded (CLAUDE.md §4); the 2dp is a *display*-only concern in
    ``db.html``. A bar whose IST date is absent from *prev_close_by_date* (the
    first trading day, or a symbol with no baseline lake) gets ``(None, None)``;
    a zero previous close yields ``None`` (no divide-by-zero, via ``pct_change``).
    """
    ts_idx = out_columns.index("timestamp") if "timestamp" in out_columns else -1
    open_idx = out_columns.index("open") if "open" in out_columns else -1
    close_idx = out_columns.index("close") if "close" in out_columns else -1
    is_daily = interval == _DAILY_INTERVAL
    derived: list[tuple[Any, Any]] = []
    for row in raw_rows:
        change: Decimal | None = None
        gap: Decimal | None = None
        ts = row[ts_idx] if ts_idx >= 0 else None
        if isinstance(ts, datetime):
            prev = prev_close_by_date.get(_bar_ist_date(ts))
            if prev is not None:
                if close_idx >= 0:
                    change = pct_change(_to_decimal(row[close_idx]), prev)
                # 1-day: the open-vs-prior-close gap on every bar; intraday: only
                # the 09:15 IST session-open bar (DEC-DB-CANDLE-PERINTERVAL-001).
                if open_idx >= 0 and (is_daily or _bar_ist_time(ts) == _SESSION_OPEN_IST):
                    gap = pct_change(_to_decimal(row[open_idx]), prev)
        derived.append((_json_safe(change), _json_safe(gap)))
    return derived


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
    # Options roll a 1-minute table, so the bar always renders full HH:MM:SS (no
    # date-only collapse); options carry no spot derived %-columns.
    rows = _render_candle_rows(cursor.fetchall(), out_columns, "1minute")
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


#: The SPOT fetch-window market namespace. MUST MATCH what the Groww spot ingest
#: actually persists — ``GROWW_COVERAGE_MARKET`` (``"groww"``,
#: ``brutex/adapters/groww/ingest.py:106`` ``_MARKET = GROWW_COVERAGE_MARKET``), NOT
#: the legacy vendor-neutral ``COVERAGE_MARKET`` (``"spot"``). Before
#: DEC-COVERAGE-GROWW-NAMESPACE-001 this pointed at ``"spot"``, so
#: :func:`spot_window_rollup`'s EXACT-match filter skipped every real ``"groww"``
#: window and the /coverage spot chips rendered EMPTY after a live pull. The
#: ``_window_market_bucket`` catch-all still folds any other namespace into "spot".
_SPOT_WINDOW_MARKET: Final[str] = GROWW_COVERAGE_MARKET

#: The unified ledger's option-window namespace — MUST MATCH what the Groww option
#: ingest persists: ``GROWW_OPTION_COVERAGE_MARKET`` (``"groww-option"``,
#: ``option_ingest.py:179`` default). Before DEC-COVERAGE-GROWW-NAMESPACE-001 this
#: was the legacy ``"spot-option"`` and never matched a real Groww option window,
#: so options were mislabelled into the "spot" bucket.
_OPTION_WINDOW_MARKET: Final[str] = GROWW_OPTION_COVERAGE_MARKET

#: The unified ledger's FUTURE-window namespace — ``GROWW_FUTURE_COVERAGE_MARKET``
#: (``"groww-future"``, DEC-GROWW-FUTURE-INGEST-001). Without this, ``groww-future``
#: windows fell into the "spot" catch-all (mislabelled); they now get their own
#: "futures" bucket (DEC-COVERAGE-GROWW-NAMESPACE-001).
_FUTURE_WINDOW_MARKET: Final[str] = GROWW_FUTURE_COVERAGE_MARKET

#: The option coverage key's folded-interval arity:
#: ``OPT|<flag>|<code>|<strike>|<type>`` (option_ingest._coverage_key).
_OPTION_INTERVAL_ARITY = 5


def _decode_option_window(
    record: Any, symbol_for_id: Mapping[str, str] | None
) -> tuple[str, str] | None:
    """Decode one option-window ledger row into PRECISE display coordinates.

    The option window key folds the whole series into the reusable
    ``WindowKey`` (DEC-WINDOW-LEDGER-001): ``security_id = "<id>|<segment>"``
    and ``interval = "OPT|<flag>|<code>|<strike>|<type>"``. Rendered raw those
    read ``25|NSE_FNO`` / ``OPT|WEEK|1|ATM+3|CALL`` — vendor ids, not the
    operator's symbols — which is exactly the live /coverage/windows complaint
    (DEC-WINDOW-OPTION-DECODE-001). Returns
    ``("BANKNIFTY · NSE_FNO", "WEEK#1 ATM+3 CALL · 1m")``-style display pairs,
    or ``None`` for any shape that does not parse (the caller keeps the raw
    row — fallback, never an exception; an unknown id keeps the numeric id so
    the row is still distinguishable).
    """
    sid, sep, segment = str(record.security_id).partition("|")
    parts = str(record.interval).split("|")
    if not sep or len(parts) != _OPTION_INTERVAL_ARITY:
        return None
    _opt, flag, code, strike, opt_type = parts
    label = (symbol_for_id or {}).get(sid, sid)
    return (f"{label} · {segment}", f"{flag}#{code} {strike} {opt_type} · 1m")


def _window_row(
    record: Any,
    *,
    first_seen_at: str | None,
    symbol_for_id: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project one per-window record to the ``/coverage/windows`` JSON row (ONE shape).

    Shared by both sources (the unified ledger + the legacy status log) so the two
    can never drift — CLAUDE.md §11 COMMON. Both record types expose the same
    field names; only the ledger carries ``first_seen_at`` (``None`` for the
    legacy log). Option windows (``_OPTION_WINDOW_MARKET``) are decoded to the operator's
    PRECISE coordinates — underlying symbol + ``FLAG#code strike type`` — via
    :func:`_decode_option_window` (DEC-WINDOW-OPTION-DECODE-001); spot rows are
    byte-unchanged.
    """
    symbol = record.security_id
    interval = record.interval
    if record.market == _OPTION_WINDOW_MARKET:
        decoded = _decode_option_window(record, symbol_for_id)
        if decoded is not None:
            symbol, interval = decoded
    return {
        "market": record.market,
        "symbol": symbol,
        "interval": interval,
        "window_start": record.window_start,
        "window_end": record.window_end,
        "status": record.status,
        "bars": record.bars,
        "attempts": record.attempts,
        # The genuine-EMPTY count (window_ledger R1) — surfaces WHY an EMPTY row
        # at attempts 5-6 is not yet SKIPPED: promotion needs `empty_threshold`
        # genuine EMPTYs, and an interleaved FAILED (429/timeout) now PRESERVES
        # the streak rather than resetting it (a rate-limit is not evidence of
        # data — DEC-EMPTY-STREAK-RATELIMIT-001), so only a SUCCESS resets it
        # (DEC-EMPTY-STREAK-VISIBLE-001). The legacy status log has no streak —
        # getattr keeps ONE row shape.
        "empty_streak": getattr(record, "empty_streak", 0),
        # Vendor fetches in the LAST pull (DEC-WINDOW-INPULL-FETCHES-001): >1 means the
        # in-pull empty/transient retry fired — the count that was previously DEBUG-only,
        # so the page now SHOWS that "attempts 1" already meant up to 3 fetches. The
        # legacy status log has no such field — getattr keeps ONE row shape (CLAUDE.md §11).
        "in_pull_fetches": getattr(record, "in_pull_fetches", 0),
        "last_error": record.last_error,
        # Display the stored UTC stamps as clean IST (CLAUDE.md §6 store-UTC /
        # display-IST; §11 COMMON via `format_ist_clock`) so /coverage/windows
        # reads like the candle viewer, not raw `...+00:00`.
        "first_seen_at": format_ist_clock(first_seen_at),
        "last_attempt_at": format_ist_clock(record.last_attempt_at),
    }


#: Hard cap on rows returned by :func:`build_window_payload` (hostile C3,
#: DEC-OPTION-WINDOW-PLAN-001): a pre-planned full-universe options pull holds
#: ~841k ledger rows — the un-capped projection measured 247 MB of JSON and
#: ~22 s per request against the page's 4 s poll (threadpool starvation, tab
#: death). Counts are ALWAYS complete; only the row list is capped.
WINDOW_ROWS_CAP: int = 20_000

#: Severity-first display order for the capped row list: actionable rows
#: (FAILED, then EMPTY) surface before the bulk statuses, so the cap can never
#: hide what the operator must act on.
_WINDOW_STATUS_ORDER: Final[dict[str, int]] = {
    "FAILED": 0,
    "EMPTY": 1,
    "SUCCESS": 2,
    "SKIPPED": 3,
    "PLANNED": 4,
}

#: mtime-keyed single-entry ledger-records cache (hostile C3): the windows page
#: polls every 4 s and the coverage hub hits the same endpoint; re-reading an
#: 841k-row Parquet per poll measured ~13.6 s. Keyed by (path, mtime_ns) so any
#: flush invalidates it; guarded by a lock (route runs on threadpool workers).
_WINDOW_CACHE_LOCK = threading.Lock()
_WINDOW_CACHE: dict[str, tuple[int, tuple[Any, ...]]] = {}


def _ledger_records_cached(lake_root: Path) -> tuple[Any, ...]:
    """Load the ledger's records with an mtime-keyed cache (C3 read path)."""
    from brutex.data.window_ledger_store import ledger_path, load_window_ledger

    path = ledger_path(lake_root)
    mtime_ns = path.stat().st_mtime_ns
    key = str(path)
    with _WINDOW_CACHE_LOCK:
        hit = _WINDOW_CACHE.get(key)
        if hit is not None and hit[0] == mtime_ns:
            return hit[1]
    records = tuple(load_window_ledger(lake_root).records())
    with _WINDOW_CACHE_LOCK:
        _WINDOW_CACHE[key] = (mtime_ns, records)
    return records


def _window_market_bucket(market: str) -> str:
    """Map a ledger ``market`` namespace to the page's display/filter bucket.

    Three buckets (DEC-COVERAGE-GROWW-NAMESPACE-001): ``"options"``
    (``groww-option``), ``"futures"`` (``groww-future`` — previously mislabelled),
    and ``"spot"`` (the ``groww`` spot namespace + any other/legacy catch-all).
    """
    if market == _OPTION_WINDOW_MARKET:
        return "options"
    if market == _FUTURE_WINDOW_MARKET:
        return "futures"
    return "spot"


#: mtime-keyed single-entry cache for the spot completeness GROUPING — the
#: /coverage spot endpoint is polled every 4 s by two pages, and grouping a
#: universe-scale (~1M-row) ledger per poll would be an O(rows) Python loop per
#: request. Same invalidation rule as ``_WINDOW_CACHE`` (any flush bumps the
#: Parquet mtime), same lock (DEC-SPOT-COVERAGE-COMPLETENESS-001).
_SPOT_ROLLUP_CACHE: dict[str, tuple[int, dict[tuple[str, str], tuple[int, int, int]]]] = {}


def spot_window_rollup(lake_root: Path) -> dict[tuple[str, str], tuple[int, int, int]]:
    """Group the spot fetch-window ledger into ``(symbol, interval) → (resolved, pending, failed)``.

    The completeness source for the /coverage spot chips
    (DEC-SPOT-COVERAGE-COMPLETENESS-001 → DEC-COMPLETENESS-HONEST-EMPTY-001): counts come
    ONLY from the unified window ledger — ``resolved`` is the ledger's SKIP/SETTLE
    authority (:data:`~brutex.data.window_ledger.SETTLED_STATUSES` = SUCCESS+SKIPPED:
    a window with real data, OR a TERMINALLY-empty window — one promoted to SKIPPED
    because it was seen empty ``EMPTY_TERMINAL_THRESHOLD`` times or fast-settled as an
    old pre-listing gap), ``failed`` is ``FAILED``, and ``pending`` is ``PLANNED`` **OR
    a NON-terminal ``EMPTY``** — both are outstanding work a re-pull WILL attempt.

    DEC-COMPLETENESS-HONEST-EMPTY-001 (supersedes the chip half of
    DEC-EMPTY-RESOLVED-REPORTING-001): a NON-terminal ``EMPTY`` (status still ``EMPTY``,
    below the terminal threshold — typically a RECENT daily/option window the vendor
    has not published yet, which a later pull DID fill) now counts ``pending``, NOT
    resolved, so the chip never reads "full" while recent data is genuinely missing.
    An all-empty PRE-LISTING dataset still reads complete because its old windows
    fast-settle to ``SKIPPED`` (terminal → resolved). The LIVE progress-bar numerator
    (``RESOLVED_STATUSES``, window_ledger.py — still SUCCESS+SKIPPED+EMPTY so the bar
    never freezes mid-pull) is deliberately UNCHANGED: completion-chip honesty and
    progress-bar liveness are intentionally different authorities. The SKIP authority
    (``SETTLED_STATUSES``) is also unchanged — a below-threshold EMPTY is still
    re-fetched next pull; only what the CHIP counts changed. Option rows
    (``market == _OPTION_WINDOW_MARKET``) are excluded by the EXACT spot-market filter.

    A lake with NO ledger Parquet returns ``{}`` (every fetch-interval chip
    renders "no ledger record") — deliberately NO ``window_status.json``
    fallback, unlike ``build_window_payload``: that sidecar is a frozen,
    write-retired snapshot (DEC-STATUS-SIDECAR-WRITE-RETIRE-001) and a stale
    completeness claim is worse than an honest unknown.

    O(rows) once per ledger flush (mtime-keyed cache); O(1) per cached call.
    """
    from brutex.data.window_ledger import EMPTY, FAILED, SETTLED_STATUSES
    from brutex.data.window_ledger_store import ledger_path

    path = ledger_path(lake_root)
    if not path.is_file():
        return {}
    mtime_ns = path.stat().st_mtime_ns
    key = str(path)
    with _WINDOW_CACHE_LOCK:
        hit = _SPOT_ROLLUP_CACHE.get(key)
        if hit is not None and hit[0] == mtime_ns:
            return hit[1]
    counts: dict[tuple[str, str], list[int]] = {}
    for record in _ledger_records_cached(lake_root):
        if record.market != _SPOT_WINDOW_MARKET:
            continue
        cell = counts.setdefault((record.security_id, record.interval), [0, 0, 0])
        if record.status in SETTLED_STATUSES:
            cell[0] += 1  # SUCCESS + SKIPPED — terminally settled (real data, or genuinely-no-data)
        elif record.status == FAILED:
            cell[2] += 1
        elif record.status == EMPTY:
            # DEC-COMPLETENESS-HONEST-EMPTY-001: a NON-terminal EMPTY (status still
            # EMPTY, i.e. below the terminal threshold — typically a recent window the
            # vendor has not published yet) is OUTSTANDING work the next pull WILL
            # re-fetch, so the completeness CHIP counts it PENDING, never "full". This
            # is the chip fix ONLY; the live progress-bar numerator (RESOLVED_STATUSES,
            # which still counts EMPTY as progress so the bar never freezes mid-pull)
            # is deliberately UNCHANGED. A genuinely-OLD empty fast-settles to SKIPPED
            # (terminal → resolved above), so an all-empty PRE-LISTING dataset still
            # reads complete — only a recent maybe-late empty reads pending (the fix).
            cell[1] += 1
        else:  # PLANNED only — genuinely-unfetched work a re-pull WILL attempt
            cell[1] += 1
    rollup = {k: (v[0], v[1], v[2]) for k, v in counts.items()}
    with _WINDOW_CACHE_LOCK:
        _SPOT_ROLLUP_CACHE[key] = (mtime_ns, rollup)
    return rollup


def build_window_payload(
    lake_root: Path,
    *,
    symbol_for_id: Mapping[str, str] | None = None,
    market: str | None = None,
    status: str | None = None,
    limit: int = WINDOW_ROWS_CAP,
) -> dict[str, Any]:
    """Project the per-window state for ``/coverage/windows`` — the unified ledger if present.

    DEC-WINDOW-LEDGER-001 (PR-3). Reads the durable ledger Parquet
    (``<lake>/window_ledger/ledger.parquet``) when it exists — surfacing EVERY
    window with its full status (``PLANNED`` / ``SUCCESS`` / ``EMPTY`` / ``FAILED``
    / ``SKIPPED``) + ``first_seen_at`` — and otherwise FALLS BACK to the legacy
    ``window_status.json`` sidecar so the page keeps working before the
    ingest-switch slice (PR-4) writes the ledger. READ-ONLY; never raises (both
    loaders degrade to an empty record set). The ``source`` field tells the UI
    which backing answered.

    Server-side scale guards (DEC-OPTION-WINDOW-PLAN-001, hostile C3 — measured
    at the pre-planned universe scale of ~841k rows): ``counts`` (status ×
    spot/options/futures bucket — DEC-COVERAGE-GROWW-NAMESPACE-001) are ALWAYS
    computed over EVERY record; the ROW list is
    filtered (*market*/*status*), ordered severity-first
    (:data:`_WINDOW_STATUS_ORDER` — FAILED/EMPTY can never be hidden by the
    cap), and truncated to *limit*. ``PLANNED`` rows are returned ONLY when
    explicitly requested via ``status="PLANNED"`` — they are the bulk of a
    freshly-planned pull and carry no per-row information beyond their count.
    The ledger read is cached keyed on the Parquet's mtime (any flush
    invalidates), so the 4 s page poll stops re-paying the full load.

    Parameters
    ----------
    lake_root
        The lake directory holding the ledger / legacy sidecar.
    symbol_for_id
        Optional ``str(SECURITY_ID) → engine symbol`` map
        (DEC-WINDOW-OPTION-DECODE-001) so option windows display the
        underlying's PRECISE symbol (``BANKNIFTY``) instead of the vendor id
        the coverage key carries (``25|NSE_FNO``). ``None`` / a missing id
        keeps the raw coordinate — display-only, never a data change.
    market
        ``"spot"`` / ``"options"`` / ``"futures"`` row filter (``None`` = all).
    status
        Exact status row filter (``"FAILED"`` …). ``"PLANNED"`` is the ONLY way
        PLANNED rows enter the row list.
    limit
        Row-list cap (clamped to ``[1, WINDOW_ROWS_CAP]``).

    Returns
    -------
    dict[str, Any]
        ``{"source", "count", "counts", "empty_threshold", "matched",
        "returned", "capped", "windows"}`` — ``count`` is the TOTAL recorded
        (back-compat), ``matched`` the post-filter row count, ``returned`` the
        rows shipped, ``empty_threshold`` the R1 EMPTY→SKIPPED promotion bound.
    """
    from brutex.data.window_ledger_store import ledger_path

    limit = max(1, min(int(limit), WINDOW_ROWS_CAP))
    if ledger_path(lake_root).is_file():
        source = "ledger"
        records: tuple[Any, ...] = _ledger_records_cached(lake_root)
    else:
        from brutex.data.window_status_store import load_window_status

        source = "status_log"
        records = tuple(load_window_status(lake_root).records())

    counts: dict[str, dict[str, int]] = {"spot": {}, "options": {}, "futures": {}}
    for r in records:
        bucket = counts[_window_market_bucket(r.market)]
        bucket[r.status] = bucket.get(r.status, 0) + 1

    wanted_status = status.upper() if status else None
    selected = [
        r
        for r in records
        if (market is None or _window_market_bucket(r.market) == market)
        and (wanted_status == r.status if wanted_status else r.status != "PLANNED")
    ]
    selected.sort(key=lambda r: _WINDOW_STATUS_ORDER.get(r.status, 9))
    rows = [
        # The legacy status log has no first_seen_at — getattr keeps ONE shape.
        _window_row(r, first_seen_at=getattr(r, "first_seen_at", None), symbol_for_id=symbol_for_id)
        for r in selected[:limit]
    ]
    return {
        "source": source,
        "count": len(records),
        "counts": counts,
        # The R1 promotion threshold (EMPTY → SKIPPED after this many
        # CONSECUTIVE clean EMPTYs) so the page can render each EMPTY row's
        # `empty_streak`/threshold progress without hardcoding the constant.
        "empty_threshold": EMPTY_TERMINAL_THRESHOLD,
        "matched": len(selected),
        "returned": len(rows),
        "capped": len(selected) > limit,
        "windows": rows,
    }


# ── F&O expiries+contracts REGISTRY browser (DEC-DERIV-REGISTRY-DB-VIEW-001) ──
# The registry is a SEPARATE `registry.duckdb` cache (PR-1,
# DEC-DERIV-METADATA-HISTORICAL-SPLIT-001) holding the actual expiry DATES +
# contract SYMBOLS the lean instrument master discards. Surfacing it here makes a
# pulled chain browsable on /db, structured by expiry / strike / CE-PE — the
# structured-table view the operator wants. It is EMPTY until a later PR
# populates it; a missing file degrades to an honest empty result + note (never
# an invented row — Golden Rule #1), mirroring the expired-options "empty" path.

#: The honest note returned when no ``registry.duckdb`` exists yet — the registry
#: is written by the (separate) "Pull expiries + contracts" button, so before the
#: first metadata pull there is simply no cache to browse.
_REGISTRY_MISSING_NOTE: Final = "no registry yet — pull expiries + contracts first"


def registry_path_for(store_path: Path) -> Path:
    """Resolve the ``registry.duckdb`` path as a SIBLING of the instrument master.

    The registry cache lives in the SAME directory as ``instruments.duckdb`` (the
    lean master) — a sibling file, NOT inside it (DEC-DERIV-METADATA-HISTORICAL-
    SPLIT-001). Given the master's on-disk path (the dashboard wires it as
    ``store.path``), the registry is ``<that dir>/registry.duckdb``. Keeping this
    one resolver here means the ``/db`` routes never re-derive the path
    (CLAUDE.md §11 COMMON).

    Parameters
    ----------
    store_path
        The instrument store's on-disk path (``<lake_root>/instruments.duckdb``).

    Returns
    -------
    Path
        The sibling ``<lake_root>/registry.duckdb``.
    """
    return store_path.parent / REGISTRY_DB_NAME


def _expiry_grid_row(row: Any) -> list[Any]:
    """Project one :class:`~brutex.data.registry_store.ExpiryRow` to a JSON-safe grid row.

    Columns follow :data:`REGISTRY_EXPIRY_COLUMNS`: the ``expiry_date`` is rendered
    as an ISO ``YYYY-MM-DD`` string and ``fetched_at`` as clean IST (CLAUDE.md §6
    store-UTC / display-IST); ``year`` is the derived Phase-1 cell key.
    """
    return [
        row.exch,
        row.underlying,
        row.expiry_date.isoformat(),
        row.year,
        format_ist_clock(row.fetched_at.isoformat()),
    ]


def _contract_grid_row(row: Any) -> list[Any]:
    """Project one :class:`~brutex.data.registry_store.ContractRow` to a grid row.

    Columns follow :data:`REGISTRY_CONTRACT_COLUMNS`: the structured chain view
    (expiry / strike / CE-PE / per-contract historical ``candle_status``). The
    ``strike`` stays full numeric precision (it is not money — never rounded,
    CLAUDE.md §4) and is ``None`` for a ``FUT`` leg; ``expiry_date`` is an ISO
    string, ``fetched_at`` clean IST.
    """
    return [
        row.exch,
        row.underlying,
        row.expiry_date.isoformat(),
        row.contract_symbol,
        _json_safe(row.strike),
        row.side,
        row.candle_status,
        format_ist_clock(row.fetched_at.isoformat()),
    ]


def registry_counts(registry_path: Path) -> dict[str, Any]:
    """Return ``{"expiries", "contracts", "exists", "note"}`` for the registry cache.

    A MISSING ``registry.duckdb`` degrades honestly to ``{"expiries": 0,
    "contracts": 0, "exists": False, "note": <missing note>}`` — never opens /
    creates a file just to count it (so a count never side-effects a brand-new
    empty cache) and never invents rows (Golden Rule #1). When the file exists the
    counts are read read-only via :class:`~brutex.data.registry_store.RegistryStore`
    (which is closed before returning), and ``note`` is ``None``.

    Parameters
    ----------
    registry_path
        The ``registry.duckdb`` path (see :func:`registry_path_for`).

    Returns
    -------
    dict[str, Any]
        ``{"expiries": <int>, "contracts": <int>, "exists": <bool>,
        "note": <str|None>}``.
    """
    if not registry_path.is_file():
        return {"expiries": 0, "contracts": 0, "exists": False, "note": _REGISTRY_MISSING_NOTE}
    from brutex.data.registry_store import RegistryStore

    store = RegistryStore.open(registry_path)
    try:
        return {
            "expiries": store.count_expiries(),
            "contracts": store.count_contracts(),
            "exists": True,
            "note": None,
        }
    finally:
        store.close()


def list_registry_expiries(
    registry_path: Path,
    *,
    exch: str | None = None,
    underlying: str | None = None,
) -> dict[str, Any]:
    """List the registry's persisted expiry DATES as a browsable grid (``/db``).

    Reads the SEPARATE ``registry.duckdb`` cache read-only via
    :meth:`~brutex.data.registry_store.RegistryStore.list_expiries` (static
    ``(? IS NULL OR col = ?)`` filters — parameterized values only) and returns the
    grid shape the ``/db`` page renders (``{columns, rows, total}``), each row
    JSON-safe (``expiry_date`` ISO string, ``fetched_at`` clean IST). A MISSING
    registry file degrades to an empty grid plus an honest ``note`` (never an
    invented row — Golden Rule #1), mirroring the expired-options "empty" path.

    Parameters
    ----------
    registry_path
        The ``registry.duckdb`` path (see :func:`registry_path_for`).
    exch, underlying
        Optional exact-match filters (``None`` = no filter), passed straight to
        the store's parameterized SELECT.

    Returns
    -------
    dict[str, Any]
        ``{"columns": REGISTRY_EXPIRY_COLUMNS, "rows": [...], "total": <int>,
        "note": <str|None>}`` — ``note`` is the missing-registry message when the
        cache file does not exist yet, else ``None``.
    """
    columns = list(REGISTRY_EXPIRY_COLUMNS)
    if not registry_path.is_file():
        return {"columns": columns, "rows": [], "total": 0, "note": _REGISTRY_MISSING_NOTE}
    from brutex.data.registry_store import RegistryStore

    store = RegistryStore.open(registry_path)
    try:
        rows = [_expiry_grid_row(r) for r in store.list_expiries(exch=exch, underlying=underlying)]
    finally:
        store.close()
    return {"columns": columns, "rows": rows, "total": len(rows), "note": None}


def list_registry_contracts(
    registry_path: Path,
    *,
    exch: str | None = None,
    underlying: str | None = None,
    expiry_date: str | None = None,
) -> dict[str, Any]:
    """List the registry's persisted contract SYMBOLS as a browsable grid (``/db``).

    The structured chain view the operator asked for: one row per contract leg
    (expiry / strike / CE-PE / per-contract historical ``candle_status``), read
    read-only via :meth:`~brutex.data.registry_store.RegistryStore.list_contracts`
    (static parameterized filters). *expiry_date*, when given, is a strict ISO
    ``YYYY-MM-DD`` string (a ``<input type="date">`` value), parsed via the shared
    :func:`_parse_iso_date` (a malformed bound raises ``ValueError`` → HTTP 400 at
    the route). A MISSING registry file degrades to an empty grid plus an honest
    ``note`` (never an invented row — Golden Rule #1).

    Parameters
    ----------
    registry_path
        The ``registry.duckdb`` path (see :func:`registry_path_for`).
    exch, underlying
        Optional exact-match filters (``None`` = no filter).
    expiry_date
        Optional ISO ``YYYY-MM-DD`` expiry filter (``None`` = no filter); a
        malformed value raises ``ValueError``.

    Returns
    -------
    dict[str, Any]
        ``{"columns": REGISTRY_CONTRACT_COLUMNS, "rows": [...], "total": <int>,
        "note": <str|None>}``.

    Raises
    ------
    ValueError
        On a malformed *expiry_date* bound (not strict ISO ``YYYY-MM-DD``).
    """
    columns = list(REGISTRY_CONTRACT_COLUMNS)
    # Parse the date BEFORE the file check so a malformed bound is rejected
    # consistently whether or not the cache exists yet (no silent empty page).
    parsed_expiry = _parse_iso_date(expiry_date, "expiry_date") if expiry_date is not None else None
    if not registry_path.is_file():
        return {"columns": columns, "rows": [], "total": 0, "note": _REGISTRY_MISSING_NOTE}
    from brutex.data.registry_store import RegistryStore

    store = RegistryStore.open(registry_path)
    try:
        rows = [
            _contract_grid_row(r)
            for r in store.list_contracts(
                exch=exch, underlying=underlying, expiry_date=parsed_expiry
            )
        ]
    finally:
        store.close()
    return {"columns": columns, "rows": rows, "total": len(rows), "note": None}


__all__ = [
    "ALLOWED_OPERATORS",
    "CANDLE_COLUMNS",
    "KNOWN_TABLES",
    "OPTIONS_DIR_NAME",
    "OPTION_CANDLE_COLUMNS",
    "REGISTRY_CONTRACT_COLUMNS",
    "REGISTRY_DB_NAME",
    "REGISTRY_EXPIRY_COLUMNS",
    "SPOT_DERIVED_COLUMNS",
    "build_window_payload",
    "clamp_limit",
    "clamp_offset",
    "fno_candle_lake_schema",
    "list_candle_datasets",
    "list_columns",
    "list_fno_candle_datasets",
    "list_option_datasets",
    "list_option_series",
    "list_registry_contracts",
    "list_registry_expiries",
    "query_candles",
    "query_option_candles",
    "query_table",
    "registry_counts",
    "registry_path_for",
    "sample_rows",
    "spot_window_rollup",
    "summarize_option_datasets",
    "summarize_spot_datasets",
    "table_summary",
]
