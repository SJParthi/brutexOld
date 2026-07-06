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

import structlog

from brutex.config.deadline import run_with_deadline
from brutex.config.hardware_envelope import apply_duckdb_thread_bound
from brutex.data.store_write_deadline import (
    STORE_CLOSE_SKIPPED_EVENT,
    StoreWriteDeadlineGuard,
    resolve_store_write_deadline_seconds,
)
from brutex.models import BruteXError

_LOG = structlog.get_logger(__name__)

#: The three write statements — module-level constants so the batch methods and the
#: single-row delegates share ONE SQL string each (DEC-STORE-WRITE-DEADLINE-001;
#: CLAUDE.md §11 COMMON — zero duplication, byte-identical statements).
_SQL_UPSERT_EXPIRY: Final[str] = (
    "INSERT OR REPLACE INTO expiries "
    "(exch, underlying, expiry_date, last_run_id, fetched_at) VALUES (?, ?, ?, ?, ?)"
)
_SQL_UPSERT_CONTRACT: Final[str] = (
    "INSERT OR REPLACE INTO contracts "
    "(exch, underlying, expiry_date, contract_symbol, strike, side, "
    "candle_status, last_run_id, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SQL_SET_CANDLE_STATUS: Final[str] = (
    "UPDATE contracts SET candle_status = ? "
    "WHERE exch = ? AND underlying = ? AND contract_symbol = ?"
)

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
        # Wall-clock deadline + wedge fail-fast latch for every DuckDB write batch
        # (DEC-STORE-WRITE-DEADLINE-001). The guard runs ONLY raw ``self._conn``
        # statements on its daemon thread — ``self._lock`` stays caller-held, so a
        # breach releases it deterministically on the OWNING thread.
        self._write_guard = StoreWriteDeadlineGuard(store_label="registry")

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

        The whole connect + DDL + ``schema_meta`` stamp — a REAL disk write on every
        open — runs under the store-write wall-clock deadline
        (DEC-STORE-WRITE-DEADLINE-001), so a NEXT pull opening a fresh store on a
        still-wedged volume fails LOUDLY with the terminal :class:`RegistryError`
        after one deadline instead of freezing the job/request thread forever.
        """
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - duckdb is a [data] extra
            raise RegistryError(
                "RegistryStore requires 'duckdb' — install with 'pip install -e .[data]'"
            ) from exc

        path.parent.mkdir(parents=True, exist_ok=True)

        def _connect() -> Any:
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
            return conn

        try:
            conn = run_with_deadline(
                _connect,
                resolve_store_write_deadline_seconds(),
                what="registry open",
                thread_name="registry-open-deadline",
            )
        except Exception as exc:
            # Defensive open failure — a hung open's DeadlineExceeded lands here too
            # (terminal by design: with no store there is no defer unit; the job
            # fails loudly + honestly instead of freezing).
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

    def _ensure_not_wedged(self) -> None:
        """Raise :class:`RegistryError` while an abandoned write still wedges the conn.

        DEC-STORE-WRITE-DEADLINE-001: after a write-deadline breach the zombie write
        still holds the DuckDB connection's internal mutex, so a read would block on
        it unboundedly. Reads therefore fail FAST + LOUD with the terminal
        :class:`RegistryError`. Caller honesty (Golden Rule #10): the dashboard
        read-side callers (``movers_options.load_registry_expiries``,
        ``registry_strikes``) catch it and DEGRADE gracefully; the pull-path
        callers deliberately do NOT — the metadata-pull SEED reads fail the job
        fast + loud at pull start (intended fail-fast, MIGRATION.md #1493), and
        the composite's phase-(b) re-read is gated UPSTREAM on
        ``MetadataPullReport.store_write_deferrals`` (phase (b) is skipped with an
        honest note, so a phase-(a) wedge never reaches this raise as a whole-job
        crash). The check self-heals the moment the zombie completes (the guard's
        token-matched latch clears). One boolean check on the healthy path.
        """
        if self._write_guard.wedged:
            raise RegistryError(
                f"RegistryStore write wedged ({self._path}); read refused until the volume recovers"
            )

    def close(self) -> None:
        """Close the underlying DuckDB connection (idempotent).

        When a write-deadline breach left a zombie write wedged inside the
        connection, the raw ``conn.close()`` is SKIPPED (closing under a
        mid-``execute`` zombie is the one call that could turn a bounded block into
        a crash) and one :data:`~brutex.data.store_write_deadline.STORE_CLOSE_SKIPPED_EVENT`
        WARN is emitted; the store is still marked closed, so the ``with`` exit can
        never freeze (DEC-STORE-WRITE-DEADLINE-001). The orphaned connection dies
        with the process (the zombie is a daemon thread).
        """
        with self._lock:
            if not self._closed:
                if self._write_guard.wedged:
                    _LOG.warning(STORE_CLOSE_SKIPPED_EVENT, store="registry", path=str(self._path))
                else:
                    self._conn.close()
                self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── writes ──────────────────────────────────────────────────────────────
    #
    # Every write runs as ONE deadline-guarded BATCH (DEC-STORE-WRITE-DEADLINE-001):
    # the caller thread holds ``self._lock`` for the whole batch exactly as before
    # (same serialization, byte-identical statements/order/parameters), while the raw
    # ``self._conn.execute`` loop runs on the guard's daemon thread bounded by the
    # wall-clock deadline. A wedged volume raises the TRANSIENT
    # :class:`~brutex.data.errors.StoreWriteDeadlineExceeded` — the metadata/historical
    # catch sites defer the underlying to the next pull. O(1) guard cost per BATCH
    # (one thread + one Event), never per row.

    def upsert_expiries(
        self,
        exch: str,
        underlying: str,
        expiry_dates: Sequence[date],
        *,
        run_id: str,
        fetched_at: datetime,
    ) -> int:
        """Persist a batch of discovered expiry dates (idempotent on the PK).

        Returns the number of rows written (``len(expiry_dates)``). One
        deadline-guarded write batch; an empty batch executes zero statements.

        Raises
        ------
        StoreWriteDeadlineExceeded
            When the batch blows the store-write deadline (transient — the caller
            defers the underlying's cell to the next pull) or the store is wedged
            by a prior breach (``latched=True`` fail-fast).
        """
        _require_tzaware(fetched_at, "fetched_at")
        with self._lock:
            self._ensure_open()

            def _write() -> None:
                for expiry_date in expiry_dates:
                    self._conn.execute(
                        _SQL_UPSERT_EXPIRY, [exch, underlying, expiry_date, run_id, fetched_at]
                    )

            self._write_guard.run(_write, op="upsert_expiries")
        return len(expiry_dates)

    def upsert_expiry(
        self, exch: str, underlying: str, expiry_date: date, *, run_id: str, fetched_at: datetime
    ) -> None:
        """Persist one discovered expiry date (idempotent on the PK).

        A thin delegate to :meth:`upsert_expiries` (ONE statement + validation
        shape — CLAUDE.md §11 COMMON); signature + behaviour byte-identical.
        """
        self.upsert_expiries(exch, underlying, [expiry_date], run_id=run_id, fetched_at=fetched_at)

    def upsert_contracts(
        self,
        exch: str,
        underlying: str,
        expiry_date: date,
        legs: Sequence[tuple[str, str, float | None]],
        *,
        run_id: str,
        fetched_at: datetime,
        candle_status: str = CANDLE_STATUS_NONE,
    ) -> int:
        """Persist a batch of contract legs for one expiry (idempotent on the PK).

        *legs* is a sequence of ``(contract_symbol, side, strike)`` triples. Every
        leg's ``side`` (:data:`CONTRACT_SIDES`), the shared ``candle_status``
        (:data:`CANDLE_STATUSES`) and the ``fetched_at`` tz-awareness are validated
        UPFRONT — before the lock and the guard — so an invalid mid-list leg
        persists NOTHING instead of a prefix (a sanctioned micro-difference vs the
        retired per-row interleaving on the ERROR path only: either way the job
        fails + re-pulls idempotently; Golden Rule #1 — never store an
        un-vocabularised value). Returns ``len(legs)``.

        Raises
        ------
        RegistryError
            On an invalid leg ``side`` / ``candle_status`` / naive ``fetched_at``
            (terminal validation, raised before any statement executes).
        StoreWriteDeadlineExceeded
            When the batch blows the store-write deadline (transient — the caller's
            Phase-2 job fails + re-pulls next run) or the store is wedged by a
            prior breach (``latched=True`` fail-fast).
        """
        for _symbol, side, _strike in legs:
            if side not in CONTRACT_SIDES:
                raise RegistryError(f"side must be one of {sorted(CONTRACT_SIDES)}, got {side!r}")
        if candle_status not in CANDLE_STATUSES:
            raise RegistryError(
                f"candle_status must be one of {sorted(CANDLE_STATUSES)}, got {candle_status!r}"
            )
        _require_tzaware(fetched_at, "fetched_at")
        with self._lock:
            self._ensure_open()

            def _write() -> None:
                for contract_symbol, side, strike in legs:
                    self._conn.execute(
                        _SQL_UPSERT_CONTRACT,
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

            self._write_guard.run(_write, op="upsert_contracts")
        return len(legs)

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
        an un-vocabularised value). A thin delegate to :meth:`upsert_contracts`
        (ONE statement + validation shape — CLAUDE.md §11 COMMON); signature +
        behaviour byte-identical.
        """
        self.upsert_contracts(
            exch,
            underlying,
            expiry_date,
            [(contract_symbol, side, strike)],
            run_id=run_id,
            fetched_at=fetched_at,
            candle_status=candle_status,
        )

    def mark_candle_statuses(self, rows: Sequence[tuple[str, str, str]], candle_status: str) -> int:
        """Update a batch of contracts' ``candle_status`` in ONE guarded write batch.

        *rows* is a sequence of ``(exch, underlying, contract_symbol)`` PK triples.
        The historical pull's completion stamp — O(1) guard cost for the WHOLE loop
        (DEC-STORE-WRITE-DEADLINE-001), not per contract. Returns ``len(rows)``
        (the rows processed; an unknown contract's UPDATE is an honest 0-row no-op,
        never an invented row).

        Raises
        ------
        RegistryError
            On an invalid ``candle_status`` (terminal validation, before any
            statement executes).
        StoreWriteDeadlineExceeded
            When the batch blows the store-write deadline (transient — the caller
            defers; the unmarked contracts re-mark idempotently on the next pull)
            or the store is wedged by a prior breach (``latched=True``).
        """
        if candle_status not in CANDLE_STATUSES:
            raise RegistryError(
                f"candle_status must be one of {sorted(CANDLE_STATUSES)}, got {candle_status!r}"
            )
        with self._lock:
            self._ensure_open()

            def _write() -> None:
                for exch, underlying, contract_symbol in rows:
                    self._conn.execute(
                        _SQL_SET_CANDLE_STATUS,
                        [candle_status, exch, underlying, contract_symbol],
                    )

            self._write_guard.run(_write, op="mark_candle_statuses")
        return len(rows)

    def set_candle_status(
        self, exch: str, underlying: str, contract_symbol: str, candle_status: str
    ) -> int:
        """Update one contract's ``candle_status`` (the historical pull's marker).

        Returns the number of rows updated (``0`` if the contract is unknown — an
        honest no-op, never an invented row). The UPDATE + its verification count
        run as ONE guarded write batch (same-thread pair, byte-identical
        statements; DEC-STORE-WRITE-DEADLINE-001).
        """
        if candle_status not in CANDLE_STATUSES:
            raise RegistryError(
                f"candle_status must be one of {sorted(CANDLE_STATUSES)}, got {candle_status!r}"
            )
        with self._lock:
            self._ensure_open()

            def _write() -> int:
                self._conn.execute(
                    _SQL_SET_CANDLE_STATUS,
                    [candle_status, exch, underlying, contract_symbol],
                )
                return self._scalar(
                    "SELECT count(*) FROM contracts "
                    "WHERE exch = ? AND underlying = ? AND contract_symbol = ? "
                    "AND candle_status = ?",
                    [exch, underlying, contract_symbol, candle_status],
                )

            return self._write_guard.run(_write, op="set_candle_status")

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
            self._ensure_not_wedged()
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
            self._ensure_not_wedged()
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
            self._ensure_not_wedged()
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
            self._ensure_not_wedged()
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
            self._ensure_not_wedged()
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
