"""F&O expiries + contracts REGISTRY cache — a SEPARATE DuckDB store.

DEC-DERIV-METADATA-HISTORICAL-SPLIT-001: the metadata-only "Pull expiries +
contracts" button persists the actual expiry DATES + contract SYMBOLS here, in a
SEPARATE ``registry.duckdb`` cache, so the lean instrument master
(DEC-INSTRUMENT-UNIVERSE-FAST-001) STAYS lean — the ~89k FUT/CE/PE legs it
discards are now *cached* (not *mastered*) here, recoverable by re-pull.

This is the missing ``store.list_contracts()`` the dormant two-phase engine
(:mod:`brutex.orchestrator.universe_phases`, DEC-UNIVERSE-PULL-PHASED-001) assumed
but never had: :meth:`RegistryStore.list_contract_jobs` feeds
:func:`~brutex.orchestrator.universe_phases.settled_contract_jobs`, so Phase-2's
incremental skip — *don't re-pull an expired expiry's immutable contract set* —
finally has a backing store. Historical OHLC is NOT stored here (that stays on the
verified ``bars/`` candle lake, behind the separate "Pull historical" button); this
registry is **metadata only**.

## Why a separate file (not the master, not coverage_ledger)

* The instrument master (``instruments.duckdb``) stays lean by decree — re-adding
  the ~89k contract rows there would reverse DEC-INSTRUMENT-UNIVERSE-FAST-001. The
  registry is a NEW cache DB (a sibling file), so the master's PK / scope / the
  brute-force engine are untouched.
* ``coverage_ledger`` (in the master) keeps the per-``(underlying, year)`` COUNT +
  status the ``/coverage`` page reads; this registry keeps the actual dates +
  symbols it only counts. The status MODEL is shared
  (:mod:`brutex.data.coverage_ledger`, CLAUDE.md §11 COMMON) — not re-implemented.

The registry is a **rebuildable cache**: on a schema-version mismatch it is
dropped + recreated (a re-pull restores it), so a stale file never blocks a boot
(CLAUDE.md §7 idempotence — same inputs rebuild the same rows).
"""

from __future__ import annotations

import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Self

from brutex.config.hardware_envelope import apply_duckdb_thread_bound
from brutex.models import BruteXError

#: Registry schema version — bump on any non-additive shape change (the cache is
#: rebuildable, so an older version is simply dropped + recreated on open).
REGISTRY_SCHEMA_VERSION: Final[int] = 1

#: The sides a stored derivative leg may carry: option Call/Put or a future.
CONTRACT_SIDES: Final[frozenset[str]] = frozenset({"CE", "PE", "FUT"})

#: Per-contract HISTORICAL-candle coverage (set by the separate "Pull historical"
#: button, NOT the metadata pull): ``none`` (metadata only, no OHLC yet),
#: ``partial`` (some windows pulled), ``complete`` (all windows pulled).
CANDLE_STATUS_NONE: Final[str] = "none"
CANDLE_STATUS_PARTIAL: Final[str] = "partial"
CANDLE_STATUS_COMPLETE: Final[str] = "complete"
CANDLE_STATUSES: Final[frozenset[str]] = frozenset(
    {CANDLE_STATUS_NONE, CANDLE_STATUS_PARTIAL, CANDLE_STATUS_COMPLETE}
)


class RegistryError(BruteXError):
    """A registry-store open / write / read failure (terminal)."""


_DDL_SCHEMA_META: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);
"""

_DDL_EXPIRIES: Final[str] = """
CREATE TABLE IF NOT EXISTS expiries (
    exch        VARCHAR NOT NULL,
    underlying  VARCHAR NOT NULL,
    expiry_date DATE    NOT NULL,
    last_run_id VARCHAR NOT NULL,
    fetched_at  TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (exch, underlying, expiry_date)
);
"""

_DDL_CONTRACTS: Final[str] = """
CREATE TABLE IF NOT EXISTS contracts (
    exch            VARCHAR NOT NULL,
    underlying      VARCHAR NOT NULL,
    expiry_date     DATE    NOT NULL,
    contract_symbol VARCHAR NOT NULL,
    strike          DOUBLE,
    side            VARCHAR NOT NULL,
    candle_status   VARCHAR NOT NULL DEFAULT 'none',
    last_run_id     VARCHAR NOT NULL,
    fetched_at      TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (exch, underlying, contract_symbol)
);
"""

_DDL_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_exp_underlying ON expiries (exch, underlying);",
    "CREATE INDEX IF NOT EXISTS idx_con_expiry ON contracts (exch, underlying, expiry_date);",
    "CREATE INDEX IF NOT EXISTS idx_con_candle ON contracts (candle_status);",
)

#: Dropped when an OLDER ``schema_version`` store is opened — the registry is a
#: rebuildable cache, so a non-additive shape change just drops + recreates.
_DDL_DROP: Final[tuple[str, ...]] = (
    "DROP TABLE IF EXISTS expiries;",
    "DROP TABLE IF EXISTS contracts;",
)

#: Static filtered SELECTs — a ``None`` filter passes through via ``? IS NULL`` so the
#: query text is CONSTANT (never caller-built) and every filter is a bind parameter.
_SELECT_EXPIRIES: Final[str] = (
    "SELECT exch, underlying, expiry_date, last_run_id, fetched_at FROM expiries "
    "WHERE (? IS NULL OR exch = ?) AND (? IS NULL OR underlying = ?) "
    "ORDER BY exch, underlying, expiry_date"
)
_SELECT_CONTRACTS: Final[str] = (
    "SELECT exch, underlying, expiry_date, contract_symbol, strike, side, "
    "candle_status, last_run_id, fetched_at FROM contracts "
    "WHERE (? IS NULL OR exch = ?) AND (? IS NULL OR underlying = ?) "
    "AND (? IS NULL OR expiry_date = ?) "
    "ORDER BY exch, underlying, expiry_date, contract_symbol"
)
#: Page suffix appended (a STATIC literal, never caller-built) when a ``limit`` is
#: requested — keeps the registry ``/db`` tabs O(page) instead of loading every leg
#: (DEC-DB-VIEWER-PAGINATE-ALL-001). ``limit`` / ``offset`` are bind parameters.
_PAGE_SUFFIX: Final[str] = " LIMIT ? OFFSET ?"
#: FILTERED counts — the same ``(? IS NULL OR col = ?)`` pass-through as the SELECTs,
#: so a paged tab's ``total`` reflects the rows matching its filter, not the whole table.
_COUNT_EXPIRIES: Final[str] = (
    "SELECT count(*) FROM expiries WHERE (? IS NULL OR exch = ?) AND (? IS NULL OR underlying = ?)"
)
_COUNT_CONTRACTS: Final[str] = (
    "SELECT count(*) FROM contracts WHERE (? IS NULL OR exch = ?) AND (? IS NULL OR underlying = ?) "
    "AND (? IS NULL OR expiry_date = ?)"
)


@dataclass(frozen=True, slots=True)
class ExpiryRow:
    """One persisted expiry date for an underlying (mirrors the ``expiries`` table)."""

    exch: str
    underlying: str
    expiry_date: date
    last_run_id: str
    fetched_at: datetime

    @property
    def year(self) -> int:
        """The calendar year of the expiry (the Phase-1 cell key)."""
        return self.expiry_date.year


@dataclass(frozen=True, slots=True)
class ContractRow:
    """One persisted contract leg (mirrors the ``contracts`` table)."""

    exch: str
    underlying: str
    expiry_date: date
    contract_symbol: str
    strike: float | None
    side: str
    candle_status: str
    last_run_id: str
    fetched_at: datetime


def _require_tzaware(value: datetime, field: str) -> None:
    """Raise :class:`RegistryError` if *value* is not timezone-aware."""
    if value.tzinfo is None:
        raise RegistryError(f"{field} must be timezone-aware, got naive {value!r}")


class RegistryStore:
    """A separate DuckDB cache of F&O expiry dates + contract symbols.

    Thread-safe via a single re-entrant lock (every public method runs under it),
    matching the :class:`~brutex.data.instrument_store.InstrumentStore` discipline.
    Upserts are PK ``INSERT OR REPLACE`` (idempotent — a re-pull of the same
    expiry/contract is a no-op), so the registry is rerun-safe (CLAUDE.md §7).
    """

    def __init__(self, *, conn: Any, path: Path) -> None:
        self._conn = conn
        self._path = path
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(cls, path: Path, *, threads: int | None = None) -> Self:
        """Open or create the registry cache at *path*.

        *threads* bounds this connection's DuckDB thread pool (``None`` = the
        envelope-derived
        :func:`brutex.config.hardware_envelope.duckdb_thread_budget`;
        DEC-THREAD-BUDGET-BOUNDS-001 — parallelism only, query results
        byte-identical).

        Applies the schema idempotently (``CREATE ... IF NOT EXISTS``). An older
        recorded ``schema_version`` drops + recreates the data tables (the cache
        is rebuildable from a re-pull, so the drop is safe); a fresh or
        already-current store skips the drop.
        """
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - duckdb is a [data] extra
            raise RegistryError(
                "RegistryStore requires 'duckdb' — install with 'pip install -e .[data]'"
            ) from exc

        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = duckdb.connect(str(path))
            apply_duckdb_thread_bound(conn, threads=threads)
            conn.execute(_DDL_SCHEMA_META)
            existing = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?", ["schema_version"]
            ).fetchone()
            if existing is not None and int(existing[0]) < REGISTRY_SCHEMA_VERSION:
                for drop_sql in _DDL_DROP:
                    conn.execute(drop_sql)
            conn.execute(_DDL_EXPIRIES)
            conn.execute(_DDL_CONTRACTS)
            for index_sql in _DDL_INDEXES:
                conn.execute(index_sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ["schema_version", str(REGISTRY_SCHEMA_VERSION)],
            )
        except Exception as exc:  # pragma: no cover - defensive open failure
            raise RegistryError(f"failed to open RegistryStore at {path}: {exc}") from exc
        return cls(conn=conn, path=path)

    @property
    def path(self) -> Path:
        """The on-disk DuckDB file path for this registry."""
        return self._path

    def _ensure_open(self) -> None:
        """Raise :class:`RegistryError` if the store has been closed."""
        if self._closed:
            raise RegistryError("RegistryStore is closed")

    def close(self) -> None:
        """Close the underlying DuckDB connection (idempotent)."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── writes ──────────────────────────────────────────────────────────────

    def upsert_expiry(
        self, exch: str, underlying: str, expiry_date: date, *, run_id: str, fetched_at: datetime
    ) -> None:
        """Persist one discovered expiry date (idempotent on the PK)."""
        _require_tzaware(fetched_at, "fetched_at")
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "INSERT OR REPLACE INTO expiries "
                "(exch, underlying, expiry_date, last_run_id, fetched_at) VALUES (?, ?, ?, ?, ?)",
                [exch, underlying, expiry_date, run_id, fetched_at],
            )

    def upsert_contract(
        self,
        exch: str,
        underlying: str,
        expiry_date: date,
        contract_symbol: str,
        *,
        side: str,
        strike: float | None,
        run_id: str,
        fetched_at: datetime,
        candle_status: str = CANDLE_STATUS_NONE,
    ) -> None:
        """Persist one discovered contract leg (idempotent on the PK).

        ``side`` must be one of :data:`CONTRACT_SIDES`; ``candle_status`` one of
        :data:`CANDLE_STATUSES` — both are validated (Golden Rule #1: never store
        an un-vocabularised value).
        """
        if side not in CONTRACT_SIDES:
            raise RegistryError(f"side must be one of {sorted(CONTRACT_SIDES)}, got {side!r}")
        if candle_status not in CANDLE_STATUSES:
            raise RegistryError(
                f"candle_status must be one of {sorted(CANDLE_STATUSES)}, got {candle_status!r}"
            )
        _require_tzaware(fetched_at, "fetched_at")
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "INSERT OR REPLACE INTO contracts "
                "(exch, underlying, expiry_date, contract_symbol, strike, side, "
                "candle_status, last_run_id, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    exch,
                    underlying,
                    expiry_date,
                    contract_symbol,
                    strike,
                    side,
                    candle_status,
                    run_id,
                    fetched_at,
                ],
            )

    def set_candle_status(
        self, exch: str, underlying: str, contract_symbol: str, candle_status: str
    ) -> int:
        """Update one contract's ``candle_status`` (the historical pull's marker).

        Returns the number of rows updated (``0`` if the contract is unknown — an
        honest no-op, never an invented row).
        """
        if candle_status not in CANDLE_STATUSES:
            raise RegistryError(
                f"candle_status must be one of {sorted(CANDLE_STATUSES)}, got {candle_status!r}"
            )
        with self._lock:
            self._ensure_open()
            self._conn.execute(
                "UPDATE contracts SET candle_status = ? "
                "WHERE exch = ? AND underlying = ? AND contract_symbol = ?",
                [candle_status, exch, underlying, contract_symbol],
            )
            return self._scalar(
                "SELECT count(*) FROM contracts "
                "WHERE exch = ? AND underlying = ? AND contract_symbol = ? AND candle_status = ?",
                [exch, underlying, contract_symbol, candle_status],
            )

    # ── reads ───────────────────────────────────────────────────────────────

    def _scalar(self, sql: str, params: Sequence[object]) -> int:
        """Run *sql* (a ``count(*)``-style query) and return its single int result."""
        row = self._conn.execute(sql, list(params)).fetchone()
        if row is None:  # pragma: no cover - count(*) always returns exactly one row
            return 0
        return int(row[0])

    def count_expiries(self, *, exch: str | None = None, underlying: str | None = None) -> int:
        """Number of persisted expiry dates — total, or matching the given filter.

        With no filter this is the whole-table total (the nav-badge count); with an
        ``exch`` / ``underlying`` filter it is the FILTERED total a paged tab needs
        (DEC-DB-VIEWER-PAGINATE-ALL-001). Static ``(? IS NULL OR col = ?)`` pass-through.
        """
        with self._lock:
            self._ensure_open()
            return self._scalar(_COUNT_EXPIRIES, [exch, exch, underlying, underlying])

    def count_contracts(
        self,
        *,
        exch: str | None = None,
        underlying: str | None = None,
        expiry_date: date | None = None,
    ) -> int:
        """Number of persisted contract legs — total, or matching the given filter.

        With no filter the whole-table total (nav badge); with a filter the FILTERED
        total for a paged tab (DEC-DB-VIEWER-PAGINATE-ALL-001).
        """
        with self._lock:
            self._ensure_open()
            return self._scalar(
                _COUNT_CONTRACTS, [exch, exch, underlying, underlying, expiry_date, expiry_date]
            )

    def list_expiries(
        self,
        *,
        exch: str | None = None,
        underlying: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ExpiryRow]:
        """List persisted expiries, optionally filtered by exchange / underlying.

        Filters use a STATIC ``(? IS NULL OR col = ?)`` predicate — a ``None`` filter
        is a pass-through — so the SQL text is constant (never caller-built) and every
        value is a bind parameter. When *limit* is given the read is SQL-bounded to one
        ``LIMIT``/``OFFSET`` page (DEC-DB-VIEWER-PAGINATE-ALL-001) instead of materialising
        every row; ``limit=None`` keeps the legacy "all rows" behaviour.
        """
        params: list[object] = [exch, exch, underlying, underlying]
        sql = _SELECT_EXPIRIES
        if limit is not None:
            sql = _SELECT_EXPIRIES + _PAGE_SUFFIX
            params = [*params, limit, offset]
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(sql, params).fetchall()
        return [ExpiryRow(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    def list_contracts(
        self,
        *,
        exch: str | None = None,
        underlying: str | None = None,
        expiry_date: date | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ContractRow]:
        """List persisted contracts, optionally filtered by exch / underlying / expiry.

        Same static ``(? IS NULL OR col = ?)`` pass-through filtering as
        :meth:`list_expiries` — constant SQL, parameterized values only — and the same
        optional ``LIMIT``/``OFFSET`` page bound (DEC-DB-VIEWER-PAGINATE-ALL-001).
        """
        params: list[object] = [exch, exch, underlying, underlying, expiry_date, expiry_date]
        sql = _SELECT_CONTRACTS
        if limit is not None:
            sql = _SELECT_CONTRACTS + _PAGE_SUFFIX
            params = [*params, limit, offset]
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(sql, params).fetchall()
        return [ContractRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows]

    def list_contract_jobs(self) -> list[tuple[str, str, date]]:
        """Distinct ``(exch, underlying, expiry_date)`` triples — the Phase-2 *settled* set.

        Feeds :func:`~brutex.orchestrator.universe_phases.settled_contract_jobs`: a
        persisted contract for an expiry means that expiry's leg-set was pulled, so
        ``contract_jobs_to_pull`` skips it once it has expired (immutable). This is
        the ``store.list_contracts()`` the dormant engine assumed but never had.
        """
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT DISTINCT exch, underlying, expiry_date FROM contracts "
                "ORDER BY exch, underlying, expiry_date"
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]


@contextmanager
def open_registry(path: Path) -> Generator[RegistryStore, None, None]:
    """Context-manager open: yield a :class:`RegistryStore`, close on exit."""
    store = RegistryStore.open(path)
    try:
        yield store
    finally:
        store.close()


__all__ = [
    "CANDLE_STATUSES",
    "CANDLE_STATUS_COMPLETE",
    "CANDLE_STATUS_NONE",
    "CANDLE_STATUS_PARTIAL",
    "CONTRACT_SIDES",
    "REGISTRY_SCHEMA_VERSION",
    "ContractRow",
    "ExpiryRow",
    "RegistryError",
    "RegistryStore",
    "open_registry",
]
