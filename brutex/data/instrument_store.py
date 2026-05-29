"""DuckDB-backed temporal instrument master store.

THE single source of truth for every tradable symbol BruteX knows
about. Every downstream phase (expiries / contracts / candles /
strategies / sweep / execution / dashboard) reads from this store
instead of re-parsing the Groww instrument master CSV or holding its own copy.

## Why this exists

The Phase A\u2192B\u2192C\u2192D pipeline has four layers, all of which need
instrument metadata at runtime:

* **Phase B** (expiries) iterates every known index \u2192 needs
  ``store.list_indices()``.
* **Phase C** (contracts) per ``(underlying, expiry)`` \u2192 needs
  ``store.list_fno_by_underlying(...)``.
* **Phase D** (candles) per contract \u2192 needs ``store.get(symbol)``
  for lot size / freeze quantity / segment.
* **Strategies** size their orders via ``InstrumentMeta`` pulled
  from ``store.as_instrument_meta_map()``.
* **Execution simulator** needs lot-size / freeze / segment per
  fill \u2014 same map.
* **Dashboard** renders the filtered universe via
  ``store.list_in_scope(as_of=...)``.

Without a persistent store, Phase A's output dies with the
process and every downstream layer re-parses the CSV from disk.

## Design guarantees

**Uniqueness** \u2014 primary key ``(vendor_symbol, snapshot_date)``.
DuckDB's B+tree enforces it; duplicate inserts are physically
impossible. Two snapshots of the same symbol on the same day
upsert to one row, but two snapshots on different days live as
two rows (that is the temporal history).

**Deduplication on re-run** \u2014 :meth:`upsert_many` wraps every
write in a DELETE+INSERT inside a transaction. Pulling Phase A
100 times on the same day converges to bit-identical final state
whose ``raw_hash`` field is unchanged.

**O(1) lookup** \u2014 primary key + 5 secondary indexes. Every point
lookup is one B+tree descent (effectively constant at our scale).
Every filtered scan is a range walk over an index.

**Temporal** \u2014 implements ``FR-ING-003``. :meth:`get` and
:meth:`universe_as_of` accept an ``as_of`` date and return the
state as it existed on that day. Lot-size changes over time fall
out for free.

**Crash safety** \u2014 DuckDB is ACID. Every :meth:`upsert_many`
either commits fully or rolls back fully; no partial states are
ever visible to readers.

**Schema evolution** \u2014 additive. New columns go via
``ALTER TABLE ... ADD COLUMN`` without rewriting history.
:data:`SCHEMA_VERSION` tracks the shape; migrations run idempotent
DDL in :meth:`InstrumentStore.open`.

## Storage location

Separate DuckDB file at ``<lake_root>/instruments.duckdb`` so the
instrument master does not share a connection with
``catalog.duckdb`` (which is the Parquet-partition metadata store
and is written by a different pipeline layer).

DuckDB is imported lazily so the pure-Python surface of this
module stays importable without the ``[data]`` extras.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from brutex.data.errors import CatalogError
from brutex.execution.types import InstrumentMeta as ExecutionInstrumentMeta
from brutex.models import (
    Exchange,
    Instrument,
    InstrumentType,
    Segment,
)

#: Bump when the schema changes. New columns go in a follow-up DDL
#: block guarded by ``schema_meta.value`` comparison; never by a
#: destructive ``DROP TABLE``.
SCHEMA_VERSION: Final[int] = 1

#: Default DuckDB file name inside the configured lake root.
DEFAULT_DB_NAME: Final[str] = "instruments.duckdb"

#: Sha256 hex length (used by :class:`InstrumentRun` validation).
SHA256_HEX_LEN: Final[int] = 64


# ────────────────────────────────────────────────────────────────────────
# DDL
# ────────────────────────────────────────────────────────────────────────

_DDL_INSTRUMENTS: Final[str] = """
CREATE TABLE IF NOT EXISTS instruments (
    vendor_symbol           VARCHAR NOT NULL,
    snapshot_date          DATE NOT NULL,

    exchange               VARCHAR NOT NULL,
    exchange_token         INTEGER,
    trading_symbol         VARCHAR NOT NULL,
    name                   VARCHAR,
    instrument_type        VARCHAR NOT NULL,
    segment                VARCHAR NOT NULL,
    series                 VARCHAR,
    isin                   VARCHAR,
    underlying_symbol      VARCHAR,
    underlying_token       INTEGER,
    lot_size               INTEGER NOT NULL,
    expiry_date            DATE,
    strike_price           DECIMAL(18, 4),
    tick_size              DECIMAL(18, 4) NOT NULL,
    freeze_quantity        INTEGER,
    is_reserved            BOOLEAN NOT NULL DEFAULT FALSE,
    buy_allowed            BOOLEAN NOT NULL DEFAULT TRUE,
    sell_allowed           BOOLEAN NOT NULL DEFAULT TRUE,
    internal_trading_symbol VARCHAR,

    in_scope               BOOLEAN NOT NULL,
    raw_hash               VARCHAR NOT NULL,
    fetched_at             TIMESTAMP WITH TIME ZONE NOT NULL,
    run_id                 VARCHAR NOT NULL,
    csv_source_sha256      VARCHAR NOT NULL,

    PRIMARY KEY (vendor_symbol, snapshot_date)
);
"""

_DDL_INSTRUMENT_RUNS: Final[str] = """
CREATE TABLE IF NOT EXISTS instrument_runs (
    run_id                VARCHAR PRIMARY KEY,
    snapshot_date         DATE NOT NULL,
    fetched_at            TIMESTAMP WITH TIME ZONE NOT NULL,
    csv_source_sha256     VARCHAR NOT NULL,
    total_raw_rows        INTEGER NOT NULL,
    integrity_failures    INTEGER NOT NULL,
    in_scope_count        INTEGER NOT NULL,
    out_of_scope_count    INTEGER NOT NULL,
    index_count           INTEGER NOT NULL,
    futures_count         INTEGER NOT NULL,
    options_count         INTEGER NOT NULL,
    bse_count             INTEGER NOT NULL,
    nse_count             INTEGER NOT NULL,
    code_commit_sha       VARCHAR NOT NULL,
    schema_version        INTEGER NOT NULL
);
"""

_DDL_EXPIRIES: Final[str] = """
CREATE TABLE IF NOT EXISTS expiries (
    exchange          VARCHAR NOT NULL,
    underlying_symbol VARCHAR NOT NULL,
    expiry_date       DATE    NOT NULL,
    snapshot_date     DATE    NOT NULL,

    pulled_year       INTEGER NOT NULL,
    fetched_at        TIMESTAMP WITH TIME ZONE NOT NULL,
    run_id            VARCHAR NOT NULL,

    PRIMARY KEY (exchange, underlying_symbol, expiry_date, snapshot_date)
);
"""

_DDL_CONTRACTS: Final[str] = """
CREATE TABLE IF NOT EXISTS contracts (
    exchange          VARCHAR NOT NULL,
    underlying_symbol VARCHAR NOT NULL,
    expiry_date       DATE    NOT NULL,
    contract_symbol   VARCHAR NOT NULL,   -- full Groww trading symbol of the leg
    snapshot_date     DATE    NOT NULL,

    instrument_type   VARCHAR NOT NULL,   -- FUT | CE | PE
    strike_price      DECIMAL(18, 4),     -- NULL for FUT
    lot_size          INTEGER NOT NULL,
    freeze_quantity   INTEGER,
    tick_size         DECIMAL(18, 4) NOT NULL,
    fetched_at        TIMESTAMP WITH TIME ZONE NOT NULL,
    run_id            VARCHAR NOT NULL,

    PRIMARY KEY (contract_symbol, snapshot_date)
);
"""

_DDL_SCHEMA_META: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);
"""

_DDL_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_instr_segment ON instruments (segment, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_instr_underlying "
    "ON instruments (underlying_symbol, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_instr_type ON instruments (instrument_type, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_instr_expiry ON instruments (expiry_date, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_instr_in_scope ON instruments (in_scope, snapshot_date);",
    # Expiries fan-out: Phase C asks "every expiry for NIFTY"
    "CREATE INDEX IF NOT EXISTS idx_exp_underlying ON expiries (underlying_symbol, snapshot_date);",
    # Contracts fan-out: Phase D asks "every leg for (NIFTY, 2025-05-29)"
    "CREATE INDEX IF NOT EXISTS idx_con_underlying_expiry "
    "ON contracts (underlying_symbol, expiry_date, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_con_expiry ON contracts (expiry_date, snapshot_date);",
)

_INSTR_COLUMNS: Final[tuple[str, ...]] = (
    "vendor_symbol",
    "snapshot_date",
    "exchange",
    "exchange_token",
    "trading_symbol",
    "name",
    "instrument_type",
    "segment",
    "series",
    "isin",
    "underlying_symbol",
    "underlying_token",
    "lot_size",
    "expiry_date",
    "strike_price",
    "tick_size",
    "freeze_quantity",
    "is_reserved",
    "buy_allowed",
    "sell_allowed",
    "internal_trading_symbol",
    "in_scope",
    "raw_hash",
    "fetched_at",
    "run_id",
    "csv_source_sha256",
)


# ────────────────────────────────────────────────────────────────────────
# Supporting dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InstrumentRun:
    """One Phase A run's metadata row.

    Written to ``instrument_runs`` table by
    :meth:`InstrumentStore.record_run` after the upsert completes.
    Lets the dashboard and audit layer reconstruct the full history
    of ingest attempts without replaying the Groww instrument master CSV.
    """

    run_id: str
    snapshot_date: date
    fetched_at: datetime
    csv_source_sha256: str
    total_raw_rows: int
    integrity_failures: int
    in_scope_count: int
    out_of_scope_count: int
    index_count: int
    futures_count: int
    options_count: int
    bse_count: int
    nse_count: int
    code_commit_sha: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("InstrumentRun.run_id must be non-empty")
        if len(self.csv_source_sha256) != SHA256_HEX_LEN:
            raise ValueError(
                "InstrumentRun.csv_source_sha256 must be a 64-char sha256 hex, "
                f"got {len(self.csv_source_sha256)}"
            )
        if self.fetched_at.tzinfo is None:
            raise ValueError("InstrumentRun.fetched_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Summary of a single :meth:`InstrumentStore.upsert_many` call."""

    rows_written: int
    rows_replaced: int
    snapshot_date: date
    run_id: str


@dataclass(frozen=True, slots=True)
class ExpiryRecord:
    """One expiry row \u2014 output of Phase B per ``(exchange, underlying)``.

    Composite primary key in the table is
    ``(exchange, underlying_symbol, expiry_date, snapshot_date)`` so
    the same expiry pulled on different days is a new row (temporal
    history); pulling twice on the same day upserts in place.
    """

    exchange: str
    underlying_symbol: str
    expiry_date: date
    snapshot_date: date
    pulled_year: int
    fetched_at: datetime
    run_id: str


@dataclass(frozen=True, slots=True)
class ContractRecord:
    """One contract leg \u2014 output of Phase C per ``(underlying, expiry)``.

    Primary key is ``(contract_symbol, snapshot_date)`` \u2014
    ``contract_symbol`` is the full Groww trading symbol (e.g.
    ``NSE-NIFTY-29May25-24000-CE``) which is globally unique, and
    ``snapshot_date`` gives temporal history.
    """

    exchange: str
    underlying_symbol: str
    expiry_date: date
    contract_symbol: str
    snapshot_date: date
    instrument_type: InstrumentType
    strike_price: Decimal | None
    lot_size: int
    freeze_quantity: int | None
    tick_size: Decimal
    fetched_at: datetime
    run_id: str


# ────────────────────────────────────────────────────────────────────────
# Hash helper
# ────────────────────────────────────────────────────────────────────────


#: Quantisation used when canonicalising Decimal fields for hashing.
#: Matches the DECIMAL(18,4) column spec so pre-store and post-store
#: Decimal values produce identical hashes even when DuckDB widens
#: ``Decimal("0.05")`` to ``Decimal("0.0500")`` on round-trip.
_CANONICAL_DECIMAL_EXP: Final[Decimal] = Decimal("0.0001")


def _canonical_decimal(value: Decimal | None) -> str:
    """Return a round-trip-stable string for a nullable :class:`Decimal`.

    Used inside :func:`compute_raw_hash` so the hash is invariant
    under DuckDB's DECIMAL(18,4) widening. ``None`` becomes the
    literal ``"None"``.
    """
    if value is None:
        return "None"
    return str(value.quantize(_CANONICAL_DECIMAL_EXP))


def compute_raw_hash(instrument: Instrument) -> str:
    """Return the sha256 hex digest of *instrument*'s canonical form.

    Used for idempotence verification: the same instrument pulled N
    times must produce the same ``raw_hash``. Differences indicate
    either a real upstream change or a bug in the normaliser.

    The canonical form is ``|``-joined string fields in a fixed
    order. ``Decimal`` values are quantised to 4 decimal places via
    :func:`_canonical_decimal` so DuckDB round-trip does not alter
    the hash; ``None`` fields become the literal ``"None"``.
    """
    parts = (
        instrument.vendor_symbol,
        str(instrument.exchange),
        str(instrument.exchange_token),
        instrument.trading_symbol,
        str(instrument.name),
        str(instrument.instrument_type),
        str(instrument.segment),
        str(instrument.series),
        str(instrument.isin),
        str(instrument.underlying_symbol),
        str(instrument.underlying_exchange_token),
        str(instrument.lot_size),
        str(instrument.expiry_date),
        _canonical_decimal(instrument.strike_price),
        _canonical_decimal(instrument.tick_size),
        str(instrument.freeze_quantity),
        str(instrument.is_reserved),
        str(instrument.buy_allowed),
        str(instrument.sell_allowed),
        str(instrument.internal_trading_symbol),
    )
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────────
# InstrumentSnapshot \u2014 the HOT-PATH in-memory index
# ────────────────────────────────────────────────────────────────────────


class InstrumentSnapshot:
    """Frozen in-memory view of an instrument universe.

    This is what the **hot path** of a brute-force sweep reads from:
    strategies, execution simulator, combo runner, all of them. It
    is constructed once at sweep start via
    :meth:`InstrumentStore.load_snapshot` and then shared read-only
    across every combo.

    ## Why this exists separately from :class:`InstrumentStore`

    DuckDB's best-case lookup is ~10\u00a0\u00b5s \u2014 fine for daily ops, far
    too slow for 1M+ combos. A Python :class:`dict` lookup is ~50\u00a0ns,
    which is 200\u00d7 faster and is the only thing that meets the
    ``bench_1m_combos \u2264 300\u202fs`` SLA from FD-16.

    The snapshot is an **immutable** frozen-dict view. Every index
    lookup is:

    * :attr:`by_symbol[s]` \u2014 O(1) Python dict lookup, ~50\u00a0ns
    * :attr:`by_segment[seg]` \u2014 O(1) to get the tuple of matching
      instruments for a segment (pre-computed at load time)
    * :attr:`by_underlying[u]` \u2014 O(1) to get every derivative
      on a given underlying
    * :attr:`by_instrument_type[t]` \u2014 O(1) to get every row of
      a given type
    * :attr:`meta_by_symbol[s]` \u2014 O(1) to feed the
      :class:`~brutex.execution.types.InstrumentMeta` straight into
      the execution simulator

    ## Guarantees verified at construction

    * **No duplicates** \u2014 every ``(vendor_symbol, snapshot_date)``
      pair appears exactly once. Violation raises :class:`CatalogError`
      before the snapshot becomes visible.
    * **Non-empty** \u2014 a caller who loads an empty snapshot gets a
      valid (but empty) object; no exceptions.
    * **Thread-safety** \u2014 the snapshot is frozen. All internal
      maps are built once in ``__init__`` and never mutated, so
      any number of reader threads / processes / Ray workers can
      share it without locking.

    ## Attributes

    by_symbol
        The master dict. Every :class:`Instrument` the snapshot
        covers, keyed by its ``vendor_symbol``.
    by_segment
        Pre-indexed ``{Segment: tuple[Instrument, ...]}`` so sweep
        filters like "every FNO row" skip the per-call scan.
    by_underlying
        Pre-indexed ``{str: tuple[Instrument, ...]}``. Used by
        Phase C to enumerate option chains and by the execution
        simulator to size multi-leg positions.
    by_instrument_type
        Pre-indexed ``{InstrumentType: tuple[Instrument, ...]}``.
        ``IDX`` entry gives the 8 spot indices in one lookup.
    meta_by_symbol
        Pre-built ``{symbol: InstrumentMeta}`` for direct handoff
        to :class:`~brutex.execution.types.InstrumentMeta`\u2011accepting
        APIs (``ExecutionSimulator``, ``BacktestDriver``).
    snapshot_date
        The ``as_of`` date used to build the snapshot, or ``None``
        if the caller asked for "latest of everything".
    """

    __slots__ = (
        "_by_instrument_type",
        "_by_segment",
        "_by_symbol",
        "_by_underlying",
        "_meta_by_symbol",
        "_snapshot_date",
    )

    def __init__(
        self,
        instruments: Sequence[Instrument],
        *,
        snapshot_date: date | None = None,
    ) -> None:
        """Build a frozen snapshot from *instruments*.

        Verifies uniqueness at construction time so callers never
        observe a duplicate-bearing snapshot. Raises
        :class:`CatalogError` on duplicate ``vendor_symbol``.
        """
        by_symbol: dict[str, Instrument] = {}
        for instr in instruments:
            if instr.vendor_symbol in by_symbol:
                raise CatalogError(
                    "InstrumentSnapshot: duplicate vendor_symbol "
                    f"{instr.vendor_symbol!r} in input (uniqueness invariant)"
                )
            by_symbol[instr.vendor_symbol] = instr

        self._by_symbol: dict[str, Instrument] = by_symbol
        self._snapshot_date: date | None = snapshot_date

        # Pre-compute the secondary indexes once so every hot-path
        # lookup is O(1).
        by_segment: dict[Segment, list[Instrument]] = {}
        by_underlying: dict[str, list[Instrument]] = {}
        by_type: dict[InstrumentType, list[Instrument]] = {}
        meta_by_symbol: dict[str, ExecutionInstrumentMeta] = {}

        for instr in by_symbol.values():
            by_segment.setdefault(instr.segment, []).append(instr)
            by_type.setdefault(instr.instrument_type, []).append(instr)
            if instr.underlying_symbol is not None:
                by_underlying.setdefault(instr.underlying_symbol, []).append(instr)
            meta_by_symbol[instr.vendor_symbol] = ExecutionInstrumentMeta(
                symbol=instr.vendor_symbol,
                segment=instr.segment,
                instrument_type=instr.instrument_type,
                lot_size=instr.lot_size,
                freeze_quantity=instr.freeze_quantity,
            )

        self._by_segment: dict[Segment, tuple[Instrument, ...]] = {
            k: tuple(v) for k, v in by_segment.items()
        }
        self._by_underlying: dict[str, tuple[Instrument, ...]] = {
            k: tuple(v) for k, v in by_underlying.items()
        }
        self._by_instrument_type: dict[InstrumentType, tuple[Instrument, ...]] = {
            k: tuple(v) for k, v in by_type.items()
        }
        self._meta_by_symbol: dict[str, ExecutionInstrumentMeta] = meta_by_symbol

    # ── Public read API ──────────────────────────────────────────

    @property
    def snapshot_date(self) -> date | None:
        """Return the ``as_of`` date the snapshot was built for."""
        return self._snapshot_date

    def get(self, vendor_symbol: str) -> Instrument | None:
        """O(1) lookup by ``vendor_symbol`` \u2014 the hot-path entry point."""
        return self._by_symbol.get(vendor_symbol)

    def __contains__(self, vendor_symbol: object) -> bool:
        """Membership test \u2014 also O(1)."""
        return vendor_symbol in self._by_symbol

    def __len__(self) -> int:
        """Return the number of unique symbols in the snapshot."""
        return len(self._by_symbol)

    def __iter__(self) -> Iterator[Instrument]:
        """Iterate every :class:`Instrument` in insertion order."""
        return iter(self._by_symbol.values())

    def symbols(self) -> tuple[str, ...]:
        """Return every symbol in a stable tuple."""
        return tuple(self._by_symbol.keys())

    def by_segment(self, segment: Segment) -> tuple[Instrument, ...]:
        """Return every instrument in *segment*, O(1)."""
        return self._by_segment.get(segment, ())

    def by_underlying(self, underlying_symbol: str) -> tuple[Instrument, ...]:
        """Return every derivative on *underlying_symbol*, O(1)."""
        return self._by_underlying.get(underlying_symbol, ())

    def by_instrument_type(self, instrument_type: InstrumentType) -> tuple[Instrument, ...]:
        """Return every instrument of *instrument_type*, O(1)."""
        return self._by_instrument_type.get(instrument_type, ())

    def meta(self, vendor_symbol: str) -> ExecutionInstrumentMeta | None:
        """Return the :class:`InstrumentMeta` for *vendor_symbol*, O(1)."""
        return self._meta_by_symbol.get(vendor_symbol)

    def meta_map(self) -> dict[str, ExecutionInstrumentMeta]:
        """Return a copy of the full ``{symbol: InstrumentMeta}`` map.

        The simulator and backtest driver accept a
        ``Mapping[str, InstrumentMeta]`` in their constructors \u2014 this
        method is the direct handoff.
        """
        return dict(self._meta_by_symbol)

    def underlyings(self) -> tuple[str, ...]:
        """Return every underlying the snapshot has derivatives for."""
        return tuple(self._by_underlying.keys())


# ────────────────────────────────────────────────────────────────────────
# Verification result dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """Result of :meth:`InstrumentStore.verify_integrity`.

    Every field is either a zero-tolerance invariant (any non-zero
    means CORRUPTION) or a count for operator visibility.
    """

    total_rows: int
    distinct_pk_count: int
    pk_duplicates: int
    raw_hash_mismatches: int
    csv_source_drift: dict[date, int]

    @property
    def is_clean(self) -> bool:
        """``True`` iff every zero-tolerance invariant passed."""
        return (
            self.pk_duplicates == 0
            and self.raw_hash_mismatches == 0
            and all(count == 1 for count in self.csv_source_drift.values())
        )


# ────────────────────────────────────────────────────────────────────────
# InstrumentStore
# ────────────────────────────────────────────────────────────────────────


class InstrumentStore:
    """Temporal instrument master on DuckDB.

    Context-manager shaped for symmetry with
    :class:`~brutex.data.catalog.Catalog`. Open once per process,
    share the handle across phases, close on shutdown.
    """

    def __init__(self, *, conn: Any, path: Path) -> None:
        self._conn = conn
        self._path = path
        self._closed = False

    @property
    def connection(self) -> Any:
        """Public read-only access to the underlying DuckDB connection.

        Exposed for whitebox consumers (`brutex/dashboard/app.py` stats
        queries) that need raw connection access without touching the
        private ``_conn`` attribute. Per DEC-P0-017 (PR #39): added to
        enable narrowing pyright's `reportPrivateUsage` from "none" to
        "warning" without per-call-site ignore comments.

        Treat as read-only — any state mutation MUST go through the
        InstrumentStore's typed methods (upsert_many, verify_integrity,
        etc.) to preserve audit + idempotence guarantees.
        """
        return self._conn

    @classmethod
    def open(cls, path: Path) -> Self:
        """Open or create an instrument store at *path*.

        The parent directory is created on first open. The full
        schema (table + indexes + schema_meta row) is applied
        idempotently via ``CREATE ... IF NOT EXISTS`` so repeated
        opens converge to the same shape.
        """
        try:
            import duckdb
        except ImportError as exc:
            raise CatalogError(
                "InstrumentStore requires 'duckdb' \u2014 install with "
                "'pip install -e .[data]' to enable the P2 data lake"
            ) from exc

        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = duckdb.connect(str(path))
            conn.execute(_DDL_INSTRUMENTS)
            conn.execute(_DDL_EXPIRIES)
            conn.execute(_DDL_CONTRACTS)
            conn.execute(_DDL_INSTRUMENT_RUNS)
            conn.execute(_DDL_SCHEMA_META)
            for index_sql in _DDL_INDEXES:
                conn.execute(index_sql)
            # Record the schema version if absent. Idempotent.
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ["schema_version", str(SCHEMA_VERSION)],
            )
        except Exception as exc:  # pragma: no cover \u2014 defensive
            raise CatalogError(f"failed to open InstrumentStore at {path}: {exc}") from exc
        return cls(conn=conn, path=path)

    @property
    def path(self) -> Path:
        """Return the on-disk DuckDB file path for this store."""
        return self._path

    @property
    def schema_version(self) -> int:
        """Return the schema version recorded in ``schema_meta``."""
        self._ensure_open()
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            ["schema_version"],
        ).fetchone()
        return int(row[0]) if row else SCHEMA_VERSION

    # ── Write side ────────────────────────────────────────────────

    def upsert_many(
        self,
        instruments: Sequence[Instrument],
        *,
        snapshot_date: date,
        run_id: str,
        csv_source_sha256: str,
        in_scope_of: Any = None,
    ) -> UpsertResult:
        """Insert or replace rows for *snapshot_date*.

        Wrapped in a single transaction so partial failures roll
        back cleanly. Re-running with the same
        ``(vendor_symbol, snapshot_date)`` replaces in place; the
        ``raw_hash`` will match on byte-for-byte identical inputs.

        Parameters
        ----------
        instruments
            Normalised :class:`Instrument` objects, typically the
            full filtered output of Phase A.
        snapshot_date
            Calendar date the Groww instrument master CSV was pulled for. Part of
            the primary key.
        run_id
            Ingest run UUID. Recorded on every row for audit.
        csv_source_sha256
            sha256 of the raw CSV bytes \u2014 lets us detect drift if
            two pulls on the same day see different data.
        in_scope_of
            Optional :class:`~typing.Callable` used to populate the
            ``in_scope`` column. If ``None``, the function imported
            from :mod:`brutex.universe` is used.
        """
        self._ensure_open()
        if in_scope_of is None:
            from brutex.universe import is_in_scope

            in_scope_of = is_in_scope

        fetched_at = datetime.now(UTC)

        # Ultra-fast bulk path (DEC-INSTRUMENT-BULK-UPSERT-001). The original
        # implementation issued one DELETE + one INSERT per row (~2·N SQL
        # round-trips for an N-row master; ≈300k for the ~150k-row Groww CSV)
        # which took MINUTES. DuckDB ``executemany`` is still a per-row loop
        # internally (measured ≈320 rows/s — no better), so the real fix is
        # DuckDB's columnar Arrow path: register the incoming rows as one
        # Arrow table and ``INSERT … SELECT`` from it — a single vectorised
        # bulk load (measured ≈250k rows/s, ~0.4s for 100k rows, ~800× the
        # per-row loop). Prior semantics are preserved exactly: the
        # (vendor_symbol, snapshot_date) PK still dedups O(1); a re-pull of
        # the same symbols replaces in place (idempotent, raw_hash unchanged);
        # ``rows_replaced`` still reports the pre-existing overwritten count;
        # all wrapped in one transaction that rolls back atomically.
        rows = [
            _instrument_to_params(
                instrument=instr,
                snapshot_date=snapshot_date,
                run_id=run_id,
                csv_source_sha256=csv_source_sha256,
                fetched_at=fetched_at,
                in_scope=bool(in_scope_of(instr)),
            )
            for instr in instruments
        ]

        try:
            self._conn.execute("BEGIN")
            rows_replaced = 0
            if rows:
                # Build the incoming rows as ONE columnar Arrow table and
                # register it, then drive BOTH the replace-count/DELETE and
                # the INSERT off it via hash-JOINs — never a giant
                # ``IN (?, ?, … ×N)`` list (that is O(N²) and was the real
                # bottleneck: a 100k-symbol IN-list DELETE took tens of
                # seconds; the hash-join is O(N)). Column order is
                # _INSTR_COLUMNS, identical to the table DDL + _build_insert_sql.
                import pyarrow as pa

                columns = {name: [row[i] for row in rows] for i, name in enumerate(_INSTR_COLUMNS)}
                self._conn.register("_brutex_instr_bulk", pa.table(columns))
                try:
                    cols = ", ".join(_INSTR_COLUMNS)
                    # Count, then delete, the pre-existing rows whose PK matches
                    # an incoming row — via a semi-join on the registered table.
                    count_row = self._conn.execute(
                        "SELECT COUNT(*) FROM instruments i "
                        "WHERE i.snapshot_date = ? AND EXISTS ("
                        "  SELECT 1 FROM _brutex_instr_bulk b "
                        "  WHERE b.vendor_symbol = i.vendor_symbol"
                        ")",
                        [snapshot_date],
                    ).fetchone()
                    rows_replaced = int(count_row[0]) if count_row else 0
                    self._conn.execute(
                        "DELETE FROM instruments i "
                        "WHERE i.snapshot_date = ? AND EXISTS ("
                        "  SELECT 1 FROM _brutex_instr_bulk b "
                        "  WHERE b.vendor_symbol = i.vendor_symbol"
                        ")",
                        [snapshot_date],
                    )
                    # ONE vectorised bulk insert from the same registered table.
                    self._conn.execute(
                        f"INSERT INTO instruments ({cols}) "  # noqa: S608  # nosec B608 — cols is the fixed _INSTR_COLUMNS constant
                        f"SELECT {cols} FROM _brutex_instr_bulk"
                    )
                finally:
                    self._conn.unregister("_brutex_instr_bulk")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return UpsertResult(
            rows_written=len(instruments),
            rows_replaced=rows_replaced,
            snapshot_date=snapshot_date,
            run_id=run_id,
        )

    def record_run(self, run: InstrumentRun) -> None:
        """Insert or replace *run* in the ``instrument_runs`` table."""
        self._ensure_open()
        self._conn.execute(
            "DELETE FROM instrument_runs WHERE run_id = ?",
            [run.run_id],
        )
        self._conn.execute(
            """
            INSERT INTO instrument_runs (
                run_id, snapshot_date, fetched_at, csv_source_sha256,
                total_raw_rows, integrity_failures, in_scope_count,
                out_of_scope_count, index_count, futures_count,
                options_count, bse_count, nse_count, code_commit_sha,
                schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run.run_id,
                run.snapshot_date,
                run.fetched_at,
                run.csv_source_sha256,
                run.total_raw_rows,
                run.integrity_failures,
                run.in_scope_count,
                run.out_of_scope_count,
                run.index_count,
                run.futures_count,
                run.options_count,
                run.bse_count,
                run.nse_count,
                run.code_commit_sha,
                run.schema_version,
            ],
        )

    # ── Phase B / Phase C writers ──────────────────────────────────

    def upsert_expiries(
        self,
        expiries: Sequence[ExpiryRecord],
    ) -> int:
        """Insert or replace *expiries* \u2014 transactional, idempotent.

        Wrapped in a single transaction so partial failures roll
        back. Re-upsert of the same composite PK replaces in place.
        Returns the number of rows written.
        """
        self._ensure_open()
        try:
            self._conn.execute("BEGIN")
            for exp in expiries:
                self._conn.execute(
                    "DELETE FROM expiries "
                    "WHERE exchange = ? AND underlying_symbol = ? "
                    "AND expiry_date = ? AND snapshot_date = ?",
                    [
                        exp.exchange,
                        exp.underlying_symbol,
                        exp.expiry_date,
                        exp.snapshot_date,
                    ],
                )
                self._conn.execute(
                    "INSERT INTO expiries ("
                    "exchange, underlying_symbol, expiry_date, snapshot_date, "
                    "pulled_year, fetched_at, run_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        exp.exchange,
                        exp.underlying_symbol,
                        exp.expiry_date,
                        exp.snapshot_date,
                        exp.pulled_year,
                        exp.fetched_at,
                        exp.run_id,
                    ],
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return len(expiries)

    def upsert_contracts(
        self,
        contracts: Sequence[ContractRecord],
    ) -> int:
        """Insert or replace *contracts* \u2014 transactional, idempotent.

        Same PK-replace-in-transaction pattern as
        :meth:`upsert_expiries`.
        """
        self._ensure_open()
        try:
            self._conn.execute("BEGIN")
            for c in contracts:
                self._conn.execute(
                    "DELETE FROM contracts WHERE contract_symbol = ? AND snapshot_date = ?",
                    [c.contract_symbol, c.snapshot_date],
                )
                self._conn.execute(
                    "INSERT INTO contracts ("
                    "exchange, underlying_symbol, expiry_date, contract_symbol, "
                    "snapshot_date, instrument_type, strike_price, lot_size, "
                    "freeze_quantity, tick_size, fetched_at, run_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        c.exchange,
                        c.underlying_symbol,
                        c.expiry_date,
                        c.contract_symbol,
                        c.snapshot_date,
                        str(c.instrument_type),
                        c.strike_price,
                        c.lot_size,
                        c.freeze_quantity,
                        c.tick_size,
                        c.fetched_at,
                        c.run_id,
                    ],
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return len(contracts)

    # ── Phase B / Phase C readers ──────────────────────────────────

    def list_expiries(
        self,
        *,
        underlying_symbol: str | None = None,
        as_of: date | None = None,
    ) -> list[ExpiryRecord]:
        """Return every expiry row, optionally filtered.

        ``as_of`` is interpreted per-``(exchange, underlying, expiry)``
        triplet: for each unique triplet, the latest
        ``snapshot_date <= as_of`` is returned. Used by Phase C to
        enumerate "which expiries were known as of a given ingest
        date".
        """
        self._ensure_open()
        # Order of params matters: the subquery's ``?`` comes first
        # in the SQL, so ``as_of`` must be pushed into params BEFORE
        # the outer WHERE's ``underlying_symbol`` param.
        sub_params: list[Any] = []
        outer_where: list[str] = []
        outer_params: list[Any] = []
        as_of_clause = ""
        if as_of is not None:
            as_of_clause = " AND snapshot_date <= ?"
            sub_params.append(as_of)
        if underlying_symbol is not None:
            outer_where.append("e.underlying_symbol = ?")
            outer_params.append(underlying_symbol)
        outer_where_sql = (" WHERE " + " AND ".join(outer_where)) if outer_where else ""

        # Every user-supplied value flows through DuckDB parameter
        # binding (the ``?`` placeholders). Only ``as_of_clause`` and
        # ``outer_where_sql`` are concatenated, and those are built
        # from a fixed set of literal column names above, not from
        # user input \u2014 no SQL-injection vector here.
        base_sql = (
            "SELECT e.exchange, e.underlying_symbol, e.expiry_date, "
            "e.snapshot_date, e.pulled_year, e.fetched_at, e.run_id "
            "FROM expiries e "
            "JOIN ("
            "    SELECT exchange, underlying_symbol, expiry_date, "
            "    MAX(snapshot_date) AS snap "
            "    FROM expiries "
        )
        sub_sql = f"    WHERE 1 = 1{as_of_clause} "
        join_sql = (
            "    GROUP BY exchange, underlying_symbol, expiry_date"
            ") latest "
            "ON e.exchange = latest.exchange "
            "AND e.underlying_symbol = latest.underlying_symbol "
            "AND e.expiry_date = latest.expiry_date "
            "AND e.snapshot_date = latest.snap"
        )
        order_sql = " ORDER BY e.underlying_symbol, e.expiry_date"
        sql = f"{base_sql}{sub_sql}{join_sql}{outer_where_sql}{order_sql}"  # nosec B608
        params = sub_params + outer_params
        rows = self._conn.execute(sql, params).fetchall()
        return [
            ExpiryRecord(
                exchange=row[0],
                underlying_symbol=row[1],
                expiry_date=row[2],
                snapshot_date=row[3],
                pulled_year=int(row[4]),
                fetched_at=row[5],
                run_id=row[6],
            )
            for row in rows
        ]

    def list_contracts(
        self,
        *,
        underlying_symbol: str | None = None,
        expiry_date: date | None = None,
        as_of: date | None = None,
    ) -> list[ContractRecord]:
        """Return every contract leg, optionally filtered.

        O(log n) via the ``(underlying_symbol, expiry_date, snapshot_date)``
        composite index when both filters are supplied \u2014 exactly the
        access pattern Phase D needs when it builds candle requests.
        """
        self._ensure_open()
        # Same param-ordering discipline as ``list_expiries`` \u2014 the
        # subquery's ``?`` must come before the outer WHERE's ``?``.
        sub_params: list[Any] = []
        outer_where: list[str] = []
        outer_params: list[Any] = []
        as_of_clause = ""
        if as_of is not None:
            as_of_clause = " AND snapshot_date <= ?"
            sub_params.append(as_of)
        if underlying_symbol is not None:
            outer_where.append("c.underlying_symbol = ?")
            outer_params.append(underlying_symbol)
        if expiry_date is not None:
            outer_where.append("c.expiry_date = ?")
            outer_params.append(expiry_date)
        outer_where_sql = (" WHERE " + " AND ".join(outer_where)) if outer_where else ""

        # See ``list_expiries`` \u2014 same parameter-binding + literal
        # concat story; this is not an injection vector.
        base_sql = (
            "SELECT c.exchange, c.underlying_symbol, c.expiry_date, "
            "c.contract_symbol, c.snapshot_date, c.instrument_type, "
            "c.strike_price, c.lot_size, c.freeze_quantity, c.tick_size, "
            "c.fetched_at, c.run_id "
            "FROM contracts c "
            "JOIN ("
            "    SELECT contract_symbol, MAX(snapshot_date) AS snap "
            "    FROM contracts "
        )
        sub_sql = f"    WHERE 1 = 1{as_of_clause} "
        join_sql = (
            "    GROUP BY contract_symbol"
            ") latest "
            "ON c.contract_symbol = latest.contract_symbol "
            "AND c.snapshot_date = latest.snap"
        )
        order_sql = " ORDER BY c.contract_symbol"
        sql = f"{base_sql}{sub_sql}{join_sql}{outer_where_sql}{order_sql}"  # nosec B608
        params = sub_params + outer_params
        rows = self._conn.execute(sql, params).fetchall()
        return [
            ContractRecord(
                exchange=row[0],
                underlying_symbol=row[1],
                expiry_date=row[2],
                contract_symbol=row[3],
                snapshot_date=row[4],
                instrument_type=InstrumentType(row[5]),
                strike_price=(Decimal(str(row[6])) if row[6] is not None else None),
                lot_size=int(row[7]),
                freeze_quantity=int(row[8]) if row[8] is not None else None,
                tick_size=Decimal(str(row[9])),
                fetched_at=row[10],
                run_id=row[11],
            )
            for row in rows
        ]

    def count_expiries(self, *, as_of: date | None = None) -> int:
        """Return the total number of distinct expiries known as of *as_of*."""
        self._ensure_open()
        if as_of is None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT DISTINCT exchange, underlying_symbol, expiry_date "
                "  FROM expiries"
                ")"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT DISTINCT exchange, underlying_symbol, expiry_date "
                "  FROM expiries WHERE snapshot_date <= ?"
                ")",
                [as_of],
            ).fetchone()
        return int(row[0]) if row else 0

    def count_contracts(self, *, as_of: date | None = None) -> int:
        """Return the total number of distinct contracts known as of *as_of*."""
        self._ensure_open()
        if as_of is None:
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT contract_symbol) FROM contracts"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT contract_symbol) FROM contracts WHERE snapshot_date <= ?",
                [as_of],
            ).fetchone()
        return int(row[0]) if row else 0

    # ── Point reads ────────────────────────────────────────────────

    def get(
        self,
        vendor_symbol: str,
        *,
        as_of: date | None = None,
    ) -> Instrument | None:
        """Return *vendor_symbol*'s state, optionally as-of *as_of*.

        O(1) via the primary-key B+tree when ``as_of`` is a known
        snapshot; O(log n) when the caller asks for a historical
        state that requires a ``MAX(snapshot_date)`` lookup.
        """
        self._ensure_open()
        if as_of is None:
            row = self._conn.execute(
                "SELECT * FROM instruments "
                "WHERE vendor_symbol = ? "
                "ORDER BY snapshot_date DESC LIMIT 1",
                [vendor_symbol],
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM instruments "
                "WHERE vendor_symbol = ? AND snapshot_date <= ? "
                "ORDER BY snapshot_date DESC LIMIT 1",
                [vendor_symbol, as_of],
            ).fetchone()
        return _row_to_instrument(row) if row is not None else None

    def exists(self, vendor_symbol: str, *, as_of: date | None = None) -> bool:
        """Return ``True`` iff *vendor_symbol* has at least one row <= *as_of*."""
        return self.get(vendor_symbol, as_of=as_of) is not None

    # ── Filtered scans ────────────────────────────────────────────

    def list_in_scope(
        self,
        *,
        as_of: date | None = None,
        segment: Segment | None = None,
        instrument_type: InstrumentType | None = None,
        underlying_symbol: str | None = None,
    ) -> list[Instrument]:
        """Return every in-scope instrument matching the filters.

        Uses the latest-snapshot semantics: for each vendor_symbol,
        returns the most recent snapshot <= *as_of* (or latest of
        all time if *as_of* is ``None``).
        """
        self._ensure_open()
        as_of_clause = " AND snapshot_date <= ?" if as_of is not None else ""
        extras: list[str] = []
        extras_params: list[Any] = []
        if segment is not None:
            extras.append("segment = ?")
            extras_params.append(str(segment))
        if instrument_type is not None:
            extras.append("instrument_type = ?")
            extras_params.append(str(instrument_type))
        if underlying_symbol is not None:
            extras.append("underlying_symbol = ?")
            extras_params.append(underlying_symbol)
        extras_sql = (" AND " + " AND ".join(extras)) if extras else ""

        # Same story as ``list_expiries`` / ``list_contracts``
        # \u2014 every value is parameter-bound; only literal clause
        # strings from the static ``extras`` list are concatenated.
        base_sql = (
            "SELECT i.* FROM instruments i "
            "JOIN ("
            "    SELECT vendor_symbol, MAX(snapshot_date) AS snap "
            "    FROM instruments "
        )
        sub_sql = f"    WHERE in_scope = TRUE{as_of_clause} "
        join_sql = (
            "    GROUP BY vendor_symbol"
            ") latest "
            "ON i.vendor_symbol = latest.vendor_symbol "
            "AND i.snapshot_date = latest.snap "
        )
        outer_sql = f"WHERE i.in_scope = TRUE{extras_sql} "
        order_sql = "ORDER BY i.vendor_symbol"
        sql = f"{base_sql}{sub_sql}{join_sql}{outer_sql}{order_sql}"  # nosec B608
        params: list[Any] = []
        if as_of is not None:
            params.append(as_of)
        params.extend(extras_params)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_instrument(row) for row in rows]

    def list_indices(self, *, as_of: date | None = None) -> list[Instrument]:
        """Return every in-scope spot index (Track 1's 4 named underlyings)."""
        return self.list_in_scope(
            as_of=as_of,
            instrument_type=InstrumentType.IDX,
        )

    def list_futures(
        self,
        *,
        underlying: str | None = None,
        as_of: date | None = None,
    ) -> list[Instrument]:
        """Return every in-scope FUT row, optionally filtered by underlying."""
        return self.list_in_scope(
            as_of=as_of,
            instrument_type=InstrumentType.FUT,
            underlying_symbol=underlying,
        )

    def list_options(
        self,
        *,
        underlying: str | None = None,
        as_of: date | None = None,
    ) -> list[Instrument]:
        """Return every in-scope CE+PE row, optionally filtered by underlying.

        Results merged from two secondary-index scans (one for CE,
        one for PE) so the filtered list is a single flat list in
        vendor_symbol order.
        """
        calls = self.list_in_scope(
            as_of=as_of,
            instrument_type=InstrumentType.CE,
            underlying_symbol=underlying,
        )
        puts = self.list_in_scope(
            as_of=as_of,
            instrument_type=InstrumentType.PE,
            underlying_symbol=underlying,
        )
        return sorted(calls + puts, key=lambda i: i.vendor_symbol)

    def universe_as_of(self, as_of_date: date) -> list[Instrument]:
        """Return every in-scope instrument as it existed on *as_of_date*."""
        return self.list_in_scope(as_of=as_of_date)

    # ── Metadata ──────────────────────────────────────────────────

    def snapshot_dates(self) -> list[date]:
        """Return every distinct ``snapshot_date`` in ascending order."""
        self._ensure_open()
        rows = self._conn.execute(
            "SELECT DISTINCT snapshot_date FROM instruments ORDER BY snapshot_date ASC"
        ).fetchall()
        return [row[0] for row in rows]

    def latest_snapshot_date(self) -> date | None:
        """Return the most recent ``snapshot_date`` or ``None`` if empty."""
        self._ensure_open()
        row = self._conn.execute("SELECT MAX(snapshot_date) FROM instruments").fetchone()
        return row[0] if row and row[0] is not None else None

    def count(
        self,
        *,
        in_scope_only: bool = True,
        as_of: date | None = None,
        segment: Segment | None = None,
    ) -> int:
        """Return the number of rows matching the filters.

        Defaults: in-scope, all segments, all snapshot_dates.
        """
        self._ensure_open()
        clauses: list[str] = []
        params: list[Any] = []
        if in_scope_only:
            clauses.append("in_scope = TRUE")
        if as_of is not None:
            clauses.append("snapshot_date <= ?")
            params.append(as_of)
        if segment is not None:
            clauses.append("segment = ?")
            params.append(str(segment))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        # ``where`` is a static join of literal clause strings \u2014
        # every user value rides ``params``.
        count_sql = f"SELECT COUNT(*) FROM instruments{where}"  # nosec B608  # noqa: S608
        row = self._conn.execute(count_sql, params).fetchone()
        return int(row[0]) if row else 0

    def count_by_underlying(self, *, as_of: date | None = None) -> dict[str, int]:
        """Return ``{underlying_symbol: count}`` for in-scope derivatives."""
        self._ensure_open()
        as_of_clause = " AND snapshot_date <= ?" if as_of is not None else ""
        params: list[Any] = [as_of] if as_of is not None else []
        base = "SELECT underlying_symbol, COUNT(*) FROM instruments "
        where_sql = f"WHERE in_scope = TRUE AND underlying_symbol IS NOT NULL{as_of_clause} "
        group_sql = "GROUP BY underlying_symbol ORDER BY underlying_symbol"
        count_sql = f"{base}{where_sql}{group_sql}"  # nosec B608
        rows = self._conn.execute(count_sql, params).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    # ── Hot-path snapshot loader ─────────────────────────────────

    def load_snapshot(
        self,
        *,
        as_of: date | None = None,
        in_scope_only: bool = True,
    ) -> InstrumentSnapshot:
        """Build an :class:`InstrumentSnapshot` for the current universe.

        Single DuckDB read \u2014 one query, one tuple-to-dataclass loop,
        one freeze. The resulting snapshot is the **hot-path**
        lookup structure every sweep / strategy / execution path
        consults for the rest of the run.

        Parameters
        ----------
        as_of
            Temporal cutoff. ``None`` means "latest of everything".
        in_scope_only
            When ``True`` (default), only rows that passed the
            scope filter at ingest time are loaded. Set to
            ``False`` for drift analysis or manual inspection.
        """
        instruments = (
            self.list_in_scope(as_of=as_of)
            if in_scope_only
            else [
                _row_to_instrument(row)
                for row in self._conn.execute(
                    "SELECT i.* FROM instruments i "
                    "JOIN ("
                    "    SELECT vendor_symbol, MAX(snapshot_date) AS snap "
                    "    FROM instruments "
                    "    WHERE (? IS NULL OR snapshot_date <= ?) "
                    "    GROUP BY vendor_symbol"
                    ") latest ON i.vendor_symbol = latest.vendor_symbol "
                    "AND i.snapshot_date = latest.snap "
                    "ORDER BY i.vendor_symbol",
                    [as_of, as_of],
                ).fetchall()
            ]
        )
        return InstrumentSnapshot(instruments, snapshot_date=as_of)

    # ── Real-time integrity verification ─────────────────────────

    def verify_integrity(self) -> IntegrityReport:
        """Run every invariant check the store knows about.

        Returns an :class:`IntegrityReport` the operator can print
        or assert on. Catches:

        * **Duplicate primary keys** \u2014
          ``COUNT(*) vs COUNT(DISTINCT (vendor_symbol, snapshot_date))``.
          DuckDB's PK constraint makes this impossible via normal
          writes, but this check defends against direct-SQL
          tampering.
        * **Raw-hash drift** \u2014 recomputes
          :func:`compute_raw_hash` for every row and compares it
          to the stored ``raw_hash``. Any mismatch means either
          the normaliser changed (code drift) or a row was
          hand-edited.
        * **CSV source drift** \u2014 returns a per-date map of
          distinct ``csv_source_sha256`` counts. Anything other
          than ``1`` means two pulls on the same day saw
          different CSV content \u2014 Groww master CSV drift in-flight.
        """
        self._ensure_open()
        total_row = self._conn.execute("SELECT COUNT(*) FROM instruments").fetchone()
        distinct_row = self._conn.execute(
            "SELECT COUNT(*) FROM (  SELECT DISTINCT vendor_symbol, snapshot_date FROM instruments)"
        ).fetchone()
        total_rows = int(total_row[0]) if total_row else 0
        distinct_pk_count = int(distinct_row[0]) if distinct_row else 0
        pk_duplicates = total_rows - distinct_pk_count

        raw_hash_mismatches = 0
        for row in self._conn.execute("SELECT * FROM instruments").fetchall():
            stored_hash = row[_INSTR_COLUMNS.index("raw_hash")]
            recomputed = compute_raw_hash(_row_to_instrument(row))
            if stored_hash != recomputed:
                raw_hash_mismatches += 1

        drift_rows = self._conn.execute(
            "SELECT snapshot_date, COUNT(DISTINCT csv_source_sha256) "
            "FROM instruments GROUP BY snapshot_date"
        ).fetchall()
        csv_source_drift: dict[date, int] = {row[0]: int(row[1]) for row in drift_rows}

        return IntegrityReport(
            total_rows=total_rows,
            distinct_pk_count=distinct_pk_count,
            pk_duplicates=pk_duplicates,
            raw_hash_mismatches=raw_hash_mismatches,
            csv_source_drift=csv_source_drift,
        )

    def assert_clean(self) -> None:
        """Raise :class:`CatalogError` if :meth:`verify_integrity` finds any issue.

        Strict wrapper for ``assert_clean()`` call sites \u2014 typically
        run at Phase A completion and at process start to catch
        corruption before any downstream layer reads the store.
        """
        report = self.verify_integrity()
        if not report.is_clean:
            raise CatalogError(
                "InstrumentStore integrity check FAILED: "
                f"pk_duplicates={report.pk_duplicates}, "
                f"raw_hash_mismatches={report.raw_hash_mismatches}, "
                f"csv_source_drift={report.csv_source_drift}"
            )

    def latest_run(self) -> InstrumentRun | None:
        """Return the most recent :class:`InstrumentRun`, or ``None`` if empty."""
        self._ensure_open()
        row = self._conn.execute(
            "SELECT run_id, snapshot_date, fetched_at, csv_source_sha256, "
            "total_raw_rows, integrity_failures, in_scope_count, "
            "out_of_scope_count, index_count, futures_count, options_count, "
            "bse_count, nse_count, code_commit_sha, schema_version "
            "FROM instrument_runs "
            "ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
        return _row_to_instrument_run(row) if row else None

    # ── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying DuckDB connection. Idempotent."""
        if self._closed:
            return
        try:
            self._conn.close()
        finally:
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    # ── Private ───────────────────────────────────────────────────

    def _ensure_open(self) -> None:
        """Raise :class:`CatalogError` if the store has been closed."""
        if self._closed:
            raise CatalogError("InstrumentStore is closed")


# ────────────────────────────────────────────────────────────────────────
# Context-manager sugar
# ────────────────────────────────────────────────────────────────────────


def open_instrument_store(path: Path) -> Any:
    """Context-manager wrapper around :meth:`InstrumentStore.open`.

    Uses :func:`contextlib.closing` under the hood so callers can
    write::

        with open_instrument_store(path) as store:
            store.upsert_many(...)

    and still get a guaranteed close on exception paths, without
    the explicit ``try: yield; finally: store.close()`` generator
    pattern (which CodeQL's ``py/should-use-with`` rule doesn't
    recognise as safe).
    """
    return closing(InstrumentStore.open(path))


# ────────────────────────────────────────────────────────────────────────
# Row <-> Instrument conversion
# ────────────────────────────────────────────────────────────────────────


def _build_insert_sql() -> str:
    """Return the parametrised INSERT statement for the ``instruments`` table.

    The column list is a module-level ``tuple[str, ...]`` constant
    (:data:`_INSTR_COLUMNS`) \u2014 no user input flows into the string
    construction, so this is not a SQL-injection vector.
    """
    placeholders = ", ".join(["?"] * len(_INSTR_COLUMNS))
    cols = ", ".join(_INSTR_COLUMNS)
    return f"INSERT INTO instruments ({cols}) VALUES ({placeholders})"  # nosec B608  # noqa: S608


def _instrument_to_params(
    *,
    instrument: Instrument,
    snapshot_date: date,
    run_id: str,
    csv_source_sha256: str,
    fetched_at: datetime,
    in_scope: bool,
) -> list[Any]:
    """Project *instrument* into the column tuple the INSERT expects."""
    raw_hash = compute_raw_hash(instrument)
    return [
        instrument.vendor_symbol,
        snapshot_date,
        str(instrument.exchange),
        instrument.exchange_token,
        instrument.trading_symbol,
        instrument.name,
        str(instrument.instrument_type),
        str(instrument.segment),
        instrument.series,
        instrument.isin,
        instrument.underlying_symbol,
        instrument.underlying_exchange_token,
        instrument.lot_size,
        instrument.expiry_date,
        instrument.strike_price,
        instrument.tick_size,
        instrument.freeze_quantity,
        instrument.is_reserved,
        instrument.buy_allowed,
        instrument.sell_allowed,
        instrument.internal_trading_symbol,
        in_scope,
        raw_hash,
        fetched_at,
        run_id,
        csv_source_sha256,
    ]


def _row_to_instrument(row: Sequence[Any]) -> Instrument:
    """Invert :func:`_instrument_to_params` \u2014 read a DuckDB row back out.

    The SQL column order is fixed via :data:`_INSTR_COLUMNS` so the
    tuple unpacking here stays in sync with the writer.
    """
    (
        vendor_symbol,
        _snapshot_date,
        exchange,
        exchange_token,
        trading_symbol,
        name,
        instrument_type,
        segment,
        series,
        isin,
        underlying_symbol,
        underlying_token,
        lot_size,
        expiry_date,
        strike_price,
        tick_size,
        freeze_quantity,
        is_reserved,
        buy_allowed,
        sell_allowed,
        internal_trading_symbol,
        _in_scope,
        _raw_hash,
        _fetched_at,
        _run_id,
        _csv_sha,
    ) = row
    return Instrument(
        exchange=Exchange(exchange),
        exchange_token=int(exchange_token) if exchange_token is not None else 0,
        trading_symbol=trading_symbol,
        vendor_symbol=vendor_symbol,
        name=name,
        instrument_type=InstrumentType(instrument_type),
        segment=Segment(segment),
        series=series,
        isin=isin,
        underlying_symbol=underlying_symbol,
        underlying_exchange_token=(int(underlying_token) if underlying_token is not None else None),
        lot_size=int(lot_size),
        tick_size=Decimal(str(tick_size)),
        freeze_quantity=int(freeze_quantity) if freeze_quantity is not None else None,
        expiry_date=expiry_date,
        strike_price=(Decimal(str(strike_price)) if strike_price is not None else None),
        is_reserved=bool(is_reserved),
        buy_allowed=bool(buy_allowed),
        sell_allowed=bool(sell_allowed),
        internal_trading_symbol=internal_trading_symbol,
    )


def _row_to_instrument_run(row: Sequence[Any]) -> InstrumentRun:
    """Invert the ``instrument_runs`` INSERT into an :class:`InstrumentRun`."""
    return InstrumentRun(
        run_id=row[0],
        snapshot_date=row[1],
        fetched_at=row[2],
        csv_source_sha256=row[3],
        total_raw_rows=int(row[4]),
        integrity_failures=int(row[5]),
        in_scope_count=int(row[6]),
        out_of_scope_count=int(row[7]),
        index_count=int(row[8]),
        futures_count=int(row[9]),
        options_count=int(row[10]),
        bse_count=int(row[11]),
        nse_count=int(row[12]),
        code_commit_sha=row[13],
        schema_version=int(row[14]),
    )


__all__ = [
    "DEFAULT_DB_NAME",
    "SCHEMA_VERSION",
    "ContractRecord",
    "ExpiryRecord",
    "InstrumentRun",
    "InstrumentSnapshot",
    "InstrumentStore",
    "IntegrityReport",
    "UpsertResult",
    "compute_raw_hash",
    "open_instrument_store",
]
