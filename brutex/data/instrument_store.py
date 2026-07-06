"""DuckDB-backed temporal instrument master store.

THE single source of truth for every tradable symbol BruteX knows
about. Every downstream phase (expiries / contracts / candles /
strategies / sweep / execution / dashboard) reads from this store
instead of re-parsing the vendor instrument master CSV or holding its own copy.

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

**Uniqueness** \u2014 primary key
``(security_id, exch_id, segment, snapshot_date)``. ``segment`` is
PART of the key because the vendor's instrument identity is the pair
``(securityId, exchangeSegment)`` (DEC-INSTRUMENT-PK-SEGMENT-001): a
``security_id`` is unique only WITHIN an ``(exch_id, segment)`` \u2014 the
same numeric id can recur across NSE vs BSE AND across CASH vs FNO, so
omitting ``segment`` would collapse two genuinely distinct instruments
into one row. DuckDB's B+tree enforces it; duplicate inserts are
physically impossible. Two snapshots of the same instrument on the
same day upsert to one row, but two snapshots on different days live
as two rows (that is the temporal history).

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

import functools
import hashlib
import threading
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

import structlog

from brutex.config.hardware_envelope import apply_duckdb_thread_bound
from brutex.data.coverage_ledger import CoverageRecord
from brutex.data.errors import CatalogError
from brutex.data.fno_candle_lake import register_fno_candle_lake
from brutex.execution.types import InstrumentMeta as ExecutionInstrumentMeta
from brutex.models import (
    Exchange,
    Instrument,
    InstrumentType,
    Segment,
)

#: Store logger — surfaces a silent identity collapse at the write boundary
#: (two DISTINCT securities sharing one ``(security_id, exch_id, segment)`` — the
#: observed "13, NSE, CASH" case) so a dropped row is visible in /logs + counted
#: on the :class:`UpsertResult`, never silently last-wins (Golden Rule #1).
_LOG = structlog.get_logger("brutex.data.instrument_store")

#: Bump when the schema changes.
#:
#: v2 (DEC-COVERAGE-LEDGER-001): added the ``coverage_ledger`` table — one
#: row per ``(exch_id, underlying_symbol, year)`` recording how many
#: expiries/contracts were pulled + the cell's completeness status.
#:
#: v3 (ADR-0003 — native instrument-master schema rename): the
#: ``instruments`` / ``expiries`` / ``contracts`` / ``coverage_ledger``
#: tables were renamed to vendor-doc lower_snake column names (KEEPING
#: BruteX's engine-ready VALUES); identity moved to ``(security_id,
#: exch_id)``; the old ``vendor_symbol`` / ``internal_trading_symbol`` /
#: ``buy_allowed`` / ``sell_allowed`` columns were dropped and
#: ``option_type`` / ``expiry_flag`` added. This is a NON-additive shape
#: change: :meth:`InstrumentStore.open` DROPs + recreates an older-version
#: store (the instrument master is rebuildable from one CSV pull).
#:
#: v4 (DEC-INDEX-SEGMENT-PK-FIX-001, 2026-06-02): indices now persist with
#: ``segment='INDEX'`` instead of ``'CASH'`` so they no longer collide on
#: the ``(security_id, exch_id, segment, snapshot_date)`` PK with a cash
#: equity sharing a numeric SECURITY_ID. The bump forces any v3 store
#: (which may hold mis-segmented index rows) to DROP + rebuild from the CSV.
#:
#: v5 (DEC-EXPIRED-OPTIONS-TABLE-001, 2026-06-02): the empty/redundant
#: ``expiries`` + ``contracts`` DuckDB tables were dropped — the dedicated
#: expired-options Parquet table (``<lake>/options/<series>/``) is now the
#: single source of truth for option series, with the rolling-option
#: addressing promoted from the partition path into real columns. The bump
#: forces any v4 store to DROP the two tables (``DROP TABLE IF EXISTS`` is
#: kept in the migration list so existing DBs shed them).
#:
#: v6 (DEC-NIFTY-TOTAL-MARKET-REFTAG-001, 2026-06-06): added the
#: ``reference_only BOOLEAN`` column to ``instruments`` — the explicit
#: REFERENCE_ONLY scope tier (Stage 2b's P0 fix from the hostile review: stop
#: overloading ``in_scope`` to imply the engine tier). Today it is True only
#: for India VIX (unifying its ``UnderlyingSpec.reference_only`` flag onto the
#: row); Stage-2b's later sub-PRs set it for the admitted non-F&O constituents.
#: ``instruments`` is already in the drop+recreate list (rebuildable from the
#: CSV), so any v5 store rebuilds at the new shape on open.
#:
#: v7 (DEC-LIVE-MANIFEST-FEED-FORMAT-001, 2026-06-15): added the
#: ``feed_token VARCHAR`` column — the RAW Groww ``exchange_token`` string from
#: the instrument CSV (e.g. ``"NIFTY"`` / ``"1"`` / ``"2885"``). The numeric
#: ``security_id`` HASHES a non-numeric index token into a surrogate, so the live
#: websocket feed (``subscribe_index_value`` / ``subscribe_ltp``) — which needs
#: the literal CSV token, ``docs/groww/api-reference/07-feed.md:54,132`` — cannot
#: be addressed from ``security_id`` for string-token indices. ``feed_token``
#: preserves the ground-truth token so the dashboard live-manifest emits the
#: exact Groww feed row. Append-only column → drop+recreate rebuilds it from CSV.
SCHEMA_VERSION: Final[int] = 7

#: Default DuckDB file name inside the configured lake root.
DEFAULT_DB_NAME: Final[str] = "instruments.duckdb"

#: Sha256 hex length (used by :class:`InstrumentRun` validation).
SHA256_HEX_LEN: Final[int] = 64


# ────────────────────────────────────────────────────────────────────────
# DDL
# ────────────────────────────────────────────────────────────────────────

_DDL_INSTRUMENTS: Final[str] = """
CREATE TABLE IF NOT EXISTS instruments (
    security_id            BIGINT  NOT NULL,
    exch_id                VARCHAR NOT NULL,
    segment                VARCHAR NOT NULL,
    isin                   VARCHAR,
    instrument             VARCHAR NOT NULL,
    underlying_security_id BIGINT,
    underlying_symbol      VARCHAR,
    symbol_name            VARCHAR NOT NULL,
    display_name           VARCHAR,
    series                 VARCHAR,
    lot_size               INTEGER NOT NULL,
    sm_expiry_date         DATE,
    strike_price           DECIMAL(18, 4),
    option_type            VARCHAR,
    tick_size              DECIMAL(18, 4) NOT NULL,
    expiry_flag            VARCHAR,
    asm_gsm_flag           BOOLEAN NOT NULL DEFAULT FALSE,
    sm_freeze_qty          INTEGER,

    snapshot_date          DATE NOT NULL,
    in_scope               BOOLEAN NOT NULL,
    raw_hash               VARCHAR NOT NULL,
    fetched_at             TIMESTAMP WITH TIME ZONE NOT NULL,
    run_id                 VARCHAR NOT NULL,
    csv_source_sha256      VARCHAR NOT NULL,
    -- REFERENCE_ONLY scope tier (v6, DEC-NIFTY-TOTAL-MARKET-REFTAG-001): True
    -- for rows fetched-for-context but never brute-forced (India VIX today;
    -- the non-F&O NIFTY-Total-Market constituents in later Stage-2b sub-PRs).
    -- DEFAULT FALSE so the column is back-fill-safe on the drop+recreate.
    reference_only         BOOLEAN NOT NULL DEFAULT FALSE,

    -- RAW Groww feed token (v7, DEC-LIVE-MANIFEST-FEED-FORMAT-001): the literal
    -- instrument-CSV ``exchange_token`` string (e.g. "NIFTY"/"1"/"2885"). The
    -- numeric ``security_id`` hashes a non-numeric index token into a surrogate,
    -- so the live websocket feed (which needs the literal token, 07-feed.md) is
    -- addressed from THIS column, not ``security_id``. NULLable (legacy rows /
    -- non-feed instruments). Appended last so the column-order invariant across
    -- _INSTR_COLUMNS / _instrument_to_params / _row_to_instrument is shift-free.
    feed_token             VARCHAR,

    -- Vendor identity is (securityId, exchangeSegment) — a security_id is
    -- unique only WITHIN an (exch_id, segment); the same id recurs across
    -- NSE/BSE and CASH/FNO, so segment is PART of the PK
    -- (DEC-INSTRUMENT-PK-SEGMENT-001 / ADR-0003).
    PRIMARY KEY (security_id, exch_id, segment, snapshot_date)
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

_DDL_COVERAGE_LEDGER: Final[str] = """
CREATE TABLE IF NOT EXISTS coverage_ledger (
    exch_id           VARCHAR NOT NULL,
    underlying_symbol VARCHAR NOT NULL,
    year              INTEGER NOT NULL,

    expiries_pulled   INTEGER NOT NULL,
    contracts_pulled  INTEGER NOT NULL,
    status            VARCHAR NOT NULL,   -- pending | complete | failed
    last_run_id       VARCHAR NOT NULL,
    last_fetched_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    error_message     VARCHAR,

    PRIMARY KEY (exch_id, underlying_symbol, year)
);
"""

_DDL_SCHEMA_META: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);
"""

#: Tables dropped when an older-``schema_version`` store is opened (ADR-0003).
#: The v3 rename is a non-additive shape change; these tables are rebuildable
#: from one CSV pull, so they are recreated at the current shape.
#: ``instrument_runs`` + ``schema_meta`` (pure bookkeeping) are NOT dropped.
#:
#: ``expiries`` + ``contracts`` are dropped UNCONDITIONALLY (v5 /
#: DEC-EXPIRED-OPTIONS-TABLE-001): they are retired tables, never recreated —
#: the ``DROP TABLE IF EXISTS`` here is what sheds them from any existing v4
#: store, and ``open`` no longer issues their CREATE.
_DDL_DROP_RENAMED: Final[tuple[str, ...]] = (
    "DROP TABLE IF EXISTS instruments;",
    "DROP TABLE IF EXISTS expiries;",
    "DROP TABLE IF EXISTS contracts;",
    "DROP TABLE IF EXISTS coverage_ledger;",
)

_DDL_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_instr_segment ON instruments (segment, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_instr_underlying "
    "ON instruments (underlying_symbol, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_instr_type ON instruments (instrument, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_instr_expiry ON instruments (sm_expiry_date, snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_instr_in_scope ON instruments (in_scope, snapshot_date);",
    # Coverage ledger: the /coverage page asks "every incomplete cell"
    # (DEC-COVERAGE-LEDGER-001) — an indexed status scan, never a full table scan.
    "CREATE INDEX IF NOT EXISTS idx_cov_status ON coverage_ledger (status);",
)

#: ``instruments`` column order — used by :func:`_instrument_to_params`,
#: :func:`_row_to_instrument`, the Arrow bulk-upsert, and ``_build_insert_sql``.
#: Mirrors the ``_DDL_INSTRUMENTS`` declaration order exactly.
_INSTR_COLUMNS: Final[tuple[str, ...]] = (
    "security_id",
    "exch_id",
    "segment",
    "isin",
    "instrument",
    "underlying_security_id",
    "underlying_symbol",
    "symbol_name",
    "display_name",
    "series",
    "lot_size",
    "sm_expiry_date",
    "strike_price",
    "option_type",
    "tick_size",
    "expiry_flag",
    "asm_gsm_flag",
    "sm_freeze_qty",
    "snapshot_date",
    "in_scope",
    "raw_hash",
    "fetched_at",
    "run_id",
    "csv_source_sha256",
    "reference_only",
    "feed_token",
)

#: Positional index of ``snapshot_date`` in a ``SELECT *`` row (mirrors the
#: ``_INSTR_COLUMNS`` / table-DDL order). Used by the O(1) point-lookup index
#: builder to read the temporal key straight off a row without re-querying
#: (DEC-INSTRUMENTSTORE-O1-001); derived from the column list so it can never
#: drift from a column re-order.
_SNAPSHOT_DATE_COL_INDEX: Final[int] = _INSTR_COLUMNS.index("snapshot_date")


# ────────────────────────────────────────────────────────────────────────
# Supporting dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InstrumentRun:
    """One Phase A run's metadata row.

    Written to ``instrument_runs`` table by
    :meth:`InstrumentStore.record_run` after the upsert completes.
    Lets the dashboard and audit layer reconstruct the full history
    of ingest attempts without replaying the vendor instrument master CSV.
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
    #: How many rows were dropped by the write-boundary identity dedup because a
    #: DISTINCT security (a different ``symbol_name``) shared their
    #: ``(security_id, exch_id, segment)`` identity — the "13, NSE, CASH"
    #: collision. ``0`` for a clean master. Each drop is also logged at WARNING.
    #: A same-identity re-list with the SAME ``symbol_name`` is NOT counted —
    #: only the name is compared, so two same-name rows differing in economics
    #: (lot/tick) still collapse last-wins silently by design.
    security_id_collisions: int = 0


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
        str(instrument.security_id),
        str(instrument.exch_id),
        str(instrument.segment),
        str(instrument.isin),
        str(instrument.instrument),
        str(instrument.underlying_security_id),
        str(instrument.underlying_symbol),
        instrument.symbol_name,
        str(instrument.display_name),
        str(instrument.series),
        str(instrument.lot_size),
        str(instrument.sm_expiry_date),
        _canonical_decimal(instrument.strike_price),
        str(instrument.option_type),
        _canonical_decimal(instrument.tick_size),
        str(instrument.expiry_flag),
        str(instrument.asm_gsm_flag),
        str(instrument.sm_freeze_qty),
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

    * **No duplicates** \u2014 every ``symbol_name`` appears exactly once.
      Violation raises :class:`CatalogError` before the snapshot
      becomes visible.
    * **Non-empty** \u2014 a caller who loads an empty snapshot gets a
      valid (but empty) object; no exceptions.
    * **Thread-safety** \u2014 the snapshot is frozen. All internal
      maps are built once in ``__init__`` and never mutated, so
      any number of reader threads / processes / Ray workers can
      share it without locking.

    ## Attributes

    by_symbol
        The master dict. Every :class:`Instrument` the snapshot
        covers, keyed by its ``symbol_name``.
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
        :class:`CatalogError` on duplicate ``symbol_name``.
        """
        by_symbol: dict[str, Instrument] = {}
        for instr in instruments:
            if instr.symbol_name in by_symbol:
                raise CatalogError(
                    "InstrumentSnapshot: duplicate symbol_name "
                    f"{instr.symbol_name!r} in input (uniqueness invariant)"
                )
            by_symbol[instr.symbol_name] = instr

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
            by_type.setdefault(instr.instrument, []).append(instr)
            if instr.underlying_symbol is not None:
                by_underlying.setdefault(instr.underlying_symbol, []).append(instr)
            meta_by_symbol[instr.symbol_name] = ExecutionInstrumentMeta(
                symbol=instr.symbol_name,
                segment=instr.segment,
                instrument_type=instr.instrument,
                lot_size=instr.lot_size,
                freeze_quantity=instr.sm_freeze_qty,
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

    def get(self, symbol_name: str) -> Instrument | None:
        """O(1) lookup by ``symbol_name`` \u2014 the hot-path entry point."""
        return self._by_symbol.get(symbol_name)

    def __contains__(self, symbol_name: object) -> bool:
        """Membership test \u2014 also O(1)."""
        return symbol_name in self._by_symbol

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

    def meta(self, symbol_name: str) -> ExecutionInstrumentMeta | None:
        """Return the :class:`InstrumentMeta` for *symbol_name*, O(1)."""
        return self._meta_by_symbol.get(symbol_name)

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
# Thread-safety (DEC-STORE-THREADSAFE-001)
# ────────────────────────────────────────────────────────────────────────
#
# The dashboard shares ONE InstrumentStore (one DuckDB connection) across the
# uvicorn request threads AND the background universe-pull thread
# (DEC-AUTO-UNIVERSE-001 deliberately reuses the single writer). A DuckDB
# connection has ONE result cursor, so two threads interleaving
# ``execute()`` + ``fetch*()`` corrupt each other's rows — the operator hit a
# ``ValueError: not enough values to unpack (expected 26, got 1)`` 500 on
# ``/coverage`` when a poll read ``instruments`` mid-``refresh()``. We serialize
# every public method behind a per-store re-entrant lock so each
# execute→fetch is atomic; reads from the background pull and the request
# handlers can no longer overlap on the connection.


def _synchronized(method: Any) -> Any:
    """Wrap a public store method so its whole body runs under ``self._lock``."""

    @functools.wraps(method)
    def _wrapper(self: InstrumentStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return _wrapper


class _StoreMeta(type):
    """Metaclass that serializes every PUBLIC :class:`InstrumentStore` method.

    Wraps each callable whose name does not start with ``_`` (and which is not
    a ``property`` / ``staticmethod`` / ``classmethod``) in :func:`_synchronized`
    at class-creation time, so the per-store ``threading.RLock`` is acquired for
    the full execute→fetch of every read/write — no per-method decorator churn,
    and a future method added to the class is thread-safe by default.
    """

    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> _StoreMeta:
        for attr_name, attr in list(namespace.items()):
            if (
                callable(attr)
                and not attr_name.startswith("_")
                and not isinstance(attr, staticmethod | classmethod | property)
            ):
                namespace[attr_name] = _synchronized(attr)
        return super().__new__(mcs, name, bases, namespace)


class _LockedReadConn:
    """Read-only DuckDB connection view that serializes ``execute → fetch``.

    Returned by :attr:`InstrumentStore.connection` so whitebox readers (the
    ``/db`` viewer in :mod:`brutex.dashboard.db_view`) hold the SAME store lock
    for the full ``execute()`` → ``description`` → ``fetch*()`` window and thus
    cannot race the background pull's writes (DEC-STORE-THREADSAFE-001). It
    supports exactly the read pattern db_view uses; mutation still goes through
    the store's typed, locked methods.
    """

    def __init__(self, conn: Any, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock
        self._result: Any = None
        # Tracks whether THIS view currently holds the store lock, so release is
        # idempotent — a caller that reads only ``description`` (no fetch) can
        # free the lock via ``close()`` / a ``with`` block instead of leaking it
        # and deadlocking the background writer (DEC-STORE-LOCK-LEAK-FIX-001).
        self._held = False

    def execute(self, sql: str, parameters: Any = None) -> _LockedReadConn:
        """Acquire the store lock and execute *sql*; lock is held until fetch/close."""
        # Drop any lock still held from a PRIOR execute on this same view (a
        # caller that re-executed without fetching) before re-acquiring, so a
        # re-execute can never leak an extra acquisition — at most one hold per
        # view, always balanced by the next fetch/close (DEC-STORE-LOCK-LEAK-FIX-001).
        self._release()
        self._lock.acquire()
        self._held = True
        try:
            self._result = (
                self._conn.execute(sql)
                if parameters is None
                else self._conn.execute(sql, parameters)
            )
        except BaseException:
            self._release()
            raise
        return self

    @property
    def description(self) -> Any:
        """Column metadata of the last executed statement (lock still held)."""
        return self._result.description

    def fetchone(self) -> Any:
        """Fetch one row, then release the store lock."""
        try:
            return self._result.fetchone()
        finally:
            self._release()

    def fetchall(self) -> Any:
        """Fetch all rows, then release the store lock."""
        try:
            return self._result.fetchall()
        finally:
            self._release()

    def _release(self) -> None:
        """Release the store lock iff this view holds it (idempotent)."""
        if self._held:
            self._held = False
            self._lock.release()

    def close(self) -> None:
        """Release the store lock if still held (idempotent, fetch-or-not safe).

        Lets a description-only reader free the lock without a fetch, so the
        fragile ``execute → fetch`` contract can never leak into a deadlock.
        """
        self._release()

    def __enter__(self) -> _LockedReadConn:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._release()


# ────────────────────────────────────────────────────────────────────────
# InstrumentStore
# ────────────────────────────────────────────────────────────────────────


class InstrumentStore(metaclass=_StoreMeta):
    """Temporal instrument master on DuckDB.

    Context-manager shaped for symmetry with
    :class:`~brutex.data.catalog.Catalog`. Open once per process,
    share the handle across phases, close on shutdown.

    Thread-safe (DEC-STORE-THREADSAFE-001): every public WRITE / metadata method
    is serialized behind a per-store re-entrant write lock via :class:`_StoreMeta`.
    Read/write split (DEC-STORE-READ-WRITE-SPLIT-001): the dashboard's raw
    ``store.connection`` reads run on a dedicated child READ cursor under a
    SEPARATE read lock, so an uncached ``/db`` scan never WAITS behind the
    background universe-pull writer — DuckDB MVCC hands the reader a consistent
    committed snapshot even mid-write. The writer stays single-flight at the job
    level; this only frees concurrent READERS from waiting on it.
    """

    def __init__(self, *, conn: Any, path: Path) -> None:
        # The WRITE lock — re-entrant so a locked method may call another locked
        # method on the same thread without self-deadlock. Guards self._conn (the
        # writer connection) + every synchronized public method.
        self._lock = threading.RLock()
        self._conn = conn
        # Read/write split (DEC-STORE-READ-WRITE-SPLIT-001): a dedicated child
        # READ cursor (a separate DuckDB connection sharing the same catalog) +
        # its own lock, so a dashboard `store.connection` read runs on a DIFFERENT
        # connection under a DIFFERENT lock and NEVER waits behind the writer's
        # RLock. DuckDB MVCC gives the reader a consistent committed snapshot even
        # while a write transaction is open on self._conn (empirically verified on
        # 1.5.3); a second `read_only=True` connection cannot be opened while this
        # process holds the file read-write, so the child cursor is the mechanism.
        self._read_conn = conn.cursor()
        # RLock (not a plain Lock) to satisfy _LockedReadConn's contract + as a
        # safe superset: reads never re-enter (the view balances acquire/release
        # via its _held flag), so re-entrancy is unused but harmless.
        self._read_lock = threading.RLock()
        self._path = path
        self._closed = False
        # O(1) point-lookup index (DEC-INSTRUMENTSTORE-O1-001). Maps
        # ``symbol_name`` → its snapshots NEWEST-first as ``(snapshot_date,
        # Instrument)`` pairs, so :meth:`get` serves a point read from memory
        # in O(1) (``as_of=None``) / O(snapshots-per-symbol) (``as_of``) instead
        # of the prior O(n) DuckDB ``SEQ_SCAN`` per call (CLAUDE.md §5 O(1)
        # discipline). Built LAZILY on the first :meth:`get`; rebuilt from the
        # SAME ``instruments`` table the query read, and INVALIDATED (set back to
        # ``None``) by every method that mutates that table (:meth:`upsert_many`,
        # :meth:`prune_to_latest_snapshot`) so it can never serve a stale row.
        # Space is O(rows) — bounded by the lean ~750-underlying pruned universe
        # (the legs are discarded per DEC-INSTRUMENT-UNIVERSE-FAST-001), so the
        # index is a small, O(1)-bounded footprint, NOT an unbounded cache.
        self._symbol_index: dict[str, list[tuple[date, Instrument]]] | None = None

    @property
    def connection(self) -> Any:
        """Read-only view of the lake on a dedicated child READ cursor.

        Exposed for whitebox consumers (the ``/db`` viewer) that need raw read
        access. Returns a :class:`_LockedReadConn` over the child read cursor
        (``self._read_conn``) guarded by the SEPARATE ``self._read_lock``
        (DEC-STORE-READ-WRITE-SPLIT-001) — NOT the writer's RLock. So a ``/db``
        read holds the read lock across its own ``execute → fetch`` (still
        serialized against other concurrent reads, which share the one cursor)
        but NEVER waits behind the background pull's writer connection; DuckDB
        MVCC hands it a consistent committed snapshot even mid-write. Per
        DEC-P0-017 (PR #39) it avoids per-call-site private-access ignores.

        Treat as read-only — any state mutation MUST go through the
        InstrumentStore's typed methods (upsert_many, verify_integrity,
        etc.) to preserve audit + idempotence guarantees.
        """
        return _LockedReadConn(self._read_conn, self._read_lock)

    @classmethod
    def open(cls, path: Path, *, threads: int | None = None) -> Self:
        """Open or create an instrument store at *path*.

        *threads* bounds this connection's DuckDB thread pool (``None`` = the
        envelope-derived
        :func:`brutex.config.hardware_envelope.duckdb_thread_budget` — DuckDB
        defaults to ALL logical cores PER CONNECTION, so the dashboard's
        concurrently-live connections would otherwise stack several full-width
        pools; DEC-THREAD-BUDGET-BOUNDS-001). Parallelism only — query results
        are byte-identical.

        The parent directory is created on first open. The full schema
        (tables + indexes + schema_meta row) is applied idempotently via
        ``CREATE ... IF NOT EXISTS`` so repeated opens converge to the same
        shape.

        **Schema-version drop+recreate (ADR-0003).** The v3 native
        rename is a NON-additive column-shape change, so a store stamped at
        an OLDER ``schema_version`` cannot be migrated in place by additive
        DDL. The instrument master is fully rebuildable from one CSV pull,
        so on an older-version store ``instruments`` / ``coverage_ledger``
        are DROPped and recreated at the current shape; the pure-bookkeeping
        ``instrument_runs`` / ``schema_meta`` tables are preserved. The
        retired ``expiries`` / ``contracts`` tables (v5 /
        DEC-EXPIRED-OPTIONS-TABLE-001) are DROPped but never recreated — the
        dedicated expired-options Parquet table superseded them. A fresh
        store (no recorded version) and an already-current store both skip
        the drop.
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
            apply_duckdb_thread_bound(conn, threads=threads)
            # schema_meta first so we can read the existing version before
            # touching the renamed tables.
            conn.execute(_DDL_SCHEMA_META)
            existing = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                ["schema_version"],
            ).fetchone()
            if existing is not None and int(existing[0]) < SCHEMA_VERSION:
                # Older shape \u2014 drop the renamed tables (rebuildable from CSV);
                # keep instrument_runs + schema_meta bookkeeping.
                for drop_sql in _DDL_DROP_RENAMED:
                    conn.execute(drop_sql)
            conn.execute(_DDL_INSTRUMENTS)
            conn.execute(_DDL_INSTRUMENT_RUNS)
            conn.execute(_DDL_COVERAGE_LEDGER)
            for index_sql in _DDL_INDEXES:
                conn.execute(index_sql)
            # Stamp the current schema version (idempotent).
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ["schema_version", str(SCHEMA_VERSION)],
            )
            # Register the F&O candle lake as a first-class, documented logical table
            # in schema_meta (DEC-FNO-CANDLE-LAKE-001). Idempotent: re-opening a store
            # re-stamps the current descriptor. The F&O candle OHLC itself lives on the
            # shared Parquet lake (bars/<exch>/FNO/<groww_symbol>/<interval>/…), NOT in
            # this master — this only records the schema so /db can present it.
            register_fno_candle_lake(conn)
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
        # A property, so _StoreMeta does not wrap it — lock explicitly so its
        # execute→fetch can't race the background pull (DEC-STORE-THREADSAFE-001).
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                ["schema_version"],
            ).fetchone()
            return int(row[0]) if row else SCHEMA_VERSION

    def set_meta(self, key: str, value: str) -> None:
        """Persist one ``schema_meta`` key/value (the generic key-value sidecar).

        Used for bounded bookkeeping summaries that ride alongside the instrument
        rows — e.g. the instrument-master integrity audit
        (DEC-INSTRUMENT-MASTER-INTEGRITY-001). Auto-locked by ``_StoreMeta``.
        """
        self._ensure_open()
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            [key, value],
        )

    def get_meta(self, key: str) -> str | None:
        """Return a ``schema_meta`` value (``None`` if the key is absent)."""
        self._ensure_open()
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            [key],
        ).fetchone()
        return str(row[0]) if row else None

    # ── Write side ────────────────────────────────────────────────

    def upsert_many(
        self,
        instruments: Sequence[Instrument],
        *,
        snapshot_date: date,
        run_id: str,
        csv_source_sha256: str,
        in_scope_of: Any = None,
        reference_only_of: Any = None,
        replace_snapshot: bool = False,
    ) -> UpsertResult:
        """Insert or replace rows for *snapshot_date*.

        ``replace_snapshot`` (DEC-LEAN-REINGEST-FIX-001): when ``True`` the
        entire *snapshot_date* is wiped before insert, so the resulting rows
        are EXACTLY the incoming set — any pre-existing rows for that day
        that are NOT in the incoming batch (e.g. FUT/CE/PE legs or non-F&O
        equities left by an older, pre-lean ingest) are removed. The
        instrument master IS the full snapshot for a day, so the
        :class:`InstrumentMasterFetcher` passes ``True`` to guarantee a
        clean lean lake on every refresh. When ``False`` (default) only the
        rows whose ``(security_id, exch_id, segment, snapshot_date)`` PK
        matches an incoming row are replaced (the historical per-key upsert
        semantics, preserved for the integration tests + any partial-batch
        caller).

        Wrapped in a single transaction so partial failures roll
        back cleanly. Re-running with the same
        ``(security_id, exch_id, segment, snapshot_date)`` replaces in place;
        the ``raw_hash`` will match on byte-for-byte identical inputs.

        Parameters
        ----------
        instruments
            Normalised :class:`Instrument` objects, typically the
            full filtered output of Phase A.
        snapshot_date
            Calendar date the vendor instrument master CSV was pulled for. Part of
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
        reference_only_of
            Optional :class:`~typing.Callable` used to populate the
            ``reference_only`` column (v6, DEC-NIFTY-TOTAL-MARKET-REFTAG-001).
            If ``None``, :func:`brutex.universe.is_reference_only` is used
            (True for India VIX today; widened to the non-F&O constituents in
            later Stage-2b sub-PRs).
        """
        self._ensure_open()
        if in_scope_of is None:
            from brutex.universe import is_in_scope

            in_scope_of = is_in_scope
        if reference_only_of is None:
            from brutex.universe import is_reference_only

            reference_only_of = is_reference_only

        fetched_at = datetime.now(UTC)

        # Defensive identity-dedup (DEC-DHAN-UNIVERSE-PK-DEDUP-001). Within one
        # snapshot the table identity is (security_id, exch_id, segment) — the
        # PRIMARY KEY minus snapshot_date. A caller that hands us two rows sharing
        # that identity (e.g. the vendor master listing one security_id under two
        # SYMBOL_NAMEs — the live "13, NSE, CASH" crash) would otherwise blow up
        # the single bulk INSERT on a PK violation. Collapse to one row per
        # identity here, O(1) per row via a dict (last-seen wins), so the store is
        # crash-proof at the write boundary regardless of caller hygiene
        # (belt-and-braces: select_universe already dedups upstream).
        identity_deduped: dict[tuple[int, Exchange, Segment], Instrument] = {}
        security_id_collisions = 0
        for instr in instruments:
            identity = (instr.security_id, instr.exch_id, instr.segment)
            existing = identity_deduped.get(identity)
            if existing is not None and existing.symbol_name != instr.symbol_name:
                # Two DISTINCT securities collapsed onto ONE
                # (security_id, exch_id, segment) identity — the observed
                # "13, NSE, CASH" case where the vendor lists one security_id under
                # two symbols. We KEEP the graceful last-wins dedup (so the bulk
                # INSERT can't hit a PK violation) but make the drop LOUD + counted
                # instead of silent (Golden Rule #1); the count rides the
                # UpsertResult so the caller/load report SEES it.
                # The BENIGN arm (not counted, not warned) is a re-list with the
                # SAME identity AND the SAME ``symbol_name`` — NOT necessarily an
                # exact duplicate: two same-name rows differing only in economics
                # (lot/tick) still collapse last-wins SILENTLY by design
                # (unchanged from main; only the name is compared here).
                security_id_collisions += 1
                _LOG.warning(
                    "instrument_store.security_id_collision",
                    security_id=instr.security_id,
                    exch_id=instr.exch_id.value,
                    segment=instr.segment.value,
                    dropped_symbol=existing.symbol_name,
                    kept_symbol=instr.symbol_name,
                )
            identity_deduped[identity] = instr
        instruments = list(identity_deduped.values())

        # Ultra-fast bulk path (DEC-INSTRUMENT-BULK-UPSERT-001). The original
        # implementation issued one DELETE + one INSERT per row (~2·N SQL
        # round-trips for an N-row master; ≈300k for the large detailed instrument-master CSV)
        # which took MINUTES. DuckDB ``executemany`` is still a per-row loop
        # internally (measured ≈320 rows/s — no better), so the real fix is
        # DuckDB's columnar Arrow path: register the incoming rows as one
        # Arrow table and ``INSERT … SELECT`` from it — a single vectorised
        # bulk load (measured ≈250k rows/s, ~0.4s for 100k rows, ~800× the
        # per-row loop). Prior semantics are preserved exactly: the
        # (security_id, exch_id, segment, snapshot_date) PK still dedups O(1); a
        # re-pull of the same symbols replaces in place (idempotent, raw_hash unchanged);
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
                reference_only=bool(reference_only_of(instr)),
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
                    if replace_snapshot:
                        # Full-snapshot replace: wipe the whole day, then insert
                        # only the incoming set. Removes stale legs / non-lean
                        # rows from a prior ingest (DEC-LEAN-REINGEST-FIX-001).
                        count_row = self._conn.execute(
                            "SELECT COUNT(*) FROM instruments WHERE snapshot_date = ?",
                            [snapshot_date],
                        ).fetchone()
                        rows_replaced = int(count_row[0]) if count_row else 0
                        self._conn.execute(
                            "DELETE FROM instruments WHERE snapshot_date = ?",
                            [snapshot_date],
                        )
                    else:
                        # Per-key upsert: count, then delete, only the
                        # pre-existing rows whose PK matches an incoming row —
                        # via a semi-join on the registered table. The match
                        # includes ``segment`` so it mirrors the full
                        # (security_id, exch_id, segment, snapshot_date) PK
                        # (DEC-INSTRUMENT-PK-SEGMENT-001): an id present in two
                        # segments is replaced per-segment, never collapsed.
                        count_row = self._conn.execute(
                            "SELECT COUNT(*) FROM instruments i "
                            "WHERE i.snapshot_date = ? AND EXISTS ("
                            "  SELECT 1 FROM _brutex_instr_bulk b "
                            "  WHERE b.security_id = i.security_id AND b.exch_id = i.exch_id "
                            "  AND b.segment = i.segment"
                            ")",
                            [snapshot_date],
                        ).fetchone()
                        rows_replaced = int(count_row[0]) if count_row else 0
                        self._conn.execute(
                            "DELETE FROM instruments i "
                            "WHERE i.snapshot_date = ? AND EXISTS ("
                            "  SELECT 1 FROM _brutex_instr_bulk b "
                            "  WHERE b.security_id = i.security_id AND b.exch_id = i.exch_id "
                            "  AND b.segment = i.segment"
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
            # The bulk path mutates nothing on rollback, but invalidate
            # defensively so a partially-applied state can never be served from a
            # stale index (DEC-INSTRUMENTSTORE-O1-001).
            self._symbol_index = None
            raise

        # Invalidate the O(1) point-lookup index: the table changed, so the next
        # get() rebuilds from the committed rows (DEC-INSTRUMENTSTORE-O1-001).
        self._symbol_index = None
        return UpsertResult(
            rows_written=len(rows),
            rows_replaced=rows_replaced,
            snapshot_date=snapshot_date,
            run_id=run_id,
            security_id_collisions=security_id_collisions,
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

    # ── Coverage ledger (DEC-COVERAGE-LEDGER-001) ──────────────────

    def upsert_coverage(self, record: CoverageRecord) -> None:
        """Insert or replace one ``coverage_ledger`` cell — O(1), idempotent.

        Keyed by ``(exchange, underlying_symbol, year)`` (the table PK), so a
        re-pull of the same cell replaces it in place. The DELETE+INSERT style
        means the latest attempt's counts + status + error always win
        deterministically.
        """
        self._ensure_open()
        self._conn.execute(
            "DELETE FROM coverage_ledger WHERE exch_id = ? AND underlying_symbol = ? AND year = ?",
            [record.exch_id, record.underlying_symbol, record.year],
        )
        self._conn.execute(
            "INSERT INTO coverage_ledger ("
            "exch_id, underlying_symbol, year, expiries_pulled, contracts_pulled, "
            "status, last_run_id, last_fetched_at, error_message"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.exch_id,
                record.underlying_symbol,
                record.year,
                record.expiries_pulled,
                record.contracts_pulled,
                record.status,
                record.last_run_id,
                record.last_fetched_at,
                record.error_message,
            ],
        )

    def get_coverage(
        self, exch_id: str, underlying_symbol: str, year: int
    ) -> CoverageRecord | None:
        """Return one cell by ``(exch_id, underlying_symbol, year)``, O(1) via PK."""
        self._ensure_open()
        row = self._conn.execute(
            "SELECT exch_id, underlying_symbol, year, expiries_pulled, "
            "contracts_pulled, status, last_run_id, last_fetched_at, error_message "
            "FROM coverage_ledger "
            "WHERE exch_id = ? AND underlying_symbol = ? AND year = ?",
            [exch_id, underlying_symbol, year],
        ).fetchone()
        return _row_to_coverage(row) if row is not None else None

    def list_coverage(self) -> list[CoverageRecord]:
        """Return every coverage cell in a stable ``(exch_id, underlying, year)`` order."""
        self._ensure_open()
        rows = self._conn.execute(
            "SELECT exch_id, underlying_symbol, year, expiries_pulled, "
            "contracts_pulled, status, last_run_id, last_fetched_at, error_message "
            "FROM coverage_ledger "
            "ORDER BY exch_id, underlying_symbol, year"
        ).fetchall()
        return [_row_to_coverage(row) for row in rows]

    def list_incomplete_coverage(self) -> list[CoverageRecord]:
        """Return every cell whose status is NOT ``'complete'`` — the re-pull set.

        Indexed status scan (``idx_cov_status``), never a full table scan
        (CLAUDE.md §5 O(1) discipline / Merge Charter §7).
        """
        self._ensure_open()
        rows = self._conn.execute(
            "SELECT exch_id, underlying_symbol, year, expiries_pulled, "
            "contracts_pulled, status, last_run_id, last_fetched_at, error_message "
            "FROM coverage_ledger "
            "WHERE status != 'complete' "
            "ORDER BY exch_id, underlying_symbol, year"
        ).fetchall()
        return [_row_to_coverage(row) for row in rows]

    # ── Point reads ────────────────────────────────────────────────

    def _build_symbol_index(self) -> dict[str, list[tuple[date, Instrument]]]:
        """Build the ``symbol_name`` → newest-first snapshots index (O(rows), once).

        Reads the WHOLE ``instruments`` table in ONE scan and groups it by
        ``symbol_name`` into ``(snapshot_date, Instrument)`` lists sorted
        NEWEST-first. Built from the SAME table :meth:`get` would have queried,
        so it is byte-identical to the per-call SQL — only amortised. Called
        lazily on the first :meth:`get` after construction or invalidation
        (DEC-INSTRUMENTSTORE-O1-001).
        """
        index: dict[str, list[tuple[date, Instrument]]] = {}
        # Deterministic tiebreak (DEC-INSTRUMENTSTORE-O1-001, NIT-1): two DISTINCT
        # instruments (different ``security_id``) can share the SAME
        # ``(symbol_name, snapshot_date)`` — ``symbol_name`` is NOT in the PK — so
        # without a stable final sort key the newest-first head of a symbol's list
        # (what ``get(as_of=None)`` returns) would be order-unspecified across DuckDB
        # plans. Appending ``security_id`` pins a total order, so a same-snapshot-date
        # collision resolves identically on every build (lowest ``security_id`` is the
        # head among same-date rows) — same-snapshot determinism, no behaviour change
        # when (the normal case) at most one row exists per (symbol_name, snapshot_date).
        for row in self._conn.execute(
            "SELECT * FROM instruments ORDER BY symbol_name, snapshot_date DESC, security_id"
        ).fetchall():
            instrument = _row_to_instrument(row)
            # ``snapshot_date`` is not a field of Instrument, so read it straight
            # off the row at its fixed column position (mirrors _row_to_instrument's
            # positional unpacking; index derived from _INSTR_COLUMNS).
            snapshot_date = row[_SNAPSHOT_DATE_COL_INDEX]
            index.setdefault(instrument.symbol_name, []).append((snapshot_date, instrument))
        # The ``ORDER BY … snapshot_date DESC`` already yields newest-first within
        # each symbol group; no per-list re-sort needed.
        return index

    def get(
        self,
        symbol_name: str,
        *,
        as_of: date | None = None,
    ) -> Instrument | None:
        """Return *symbol_name*'s state, optionally as-of *as_of*.

        Looks up by the engine symbol (``symbol_name``); the most recent
        ``snapshot_date`` (≤ *as_of* when given) wins.

        **O(1)** via an in-memory ``symbol_name`` → newest-first-snapshots index
        (DEC-INSTRUMENTSTORE-O1-001), built lazily on the first call and
        invalidated on every write. ``as_of=None`` (the dominant hot path —
        every dashboard ``store.get(symbol)``) is a single dict lookup +
        first-element read, O(1). An ``as_of`` query walks that symbol's
        snapshot list newest-first to the first snapshot ≤ *as_of*, O(snapshots
        per symbol) — bounded by the small number of distinct pull days, NOT
        O(rows). Supersedes the prior per-call O(n) DuckDB ``SEQ_SCAN`` (no
        ``symbol_name`` index existed) that the "O(1) lookup" docstring
        misclaimed (CLAUDE.md §5 O(1) discipline / Golden Rule #1 honesty).
        """
        self._ensure_open()
        if self._symbol_index is None:
            self._symbol_index = self._build_symbol_index()
        snapshots = self._symbol_index.get(symbol_name)
        if not snapshots:
            return None
        if as_of is None:
            # Newest snapshot — first element (list is sorted newest-first).
            return snapshots[0][1]
        for snapshot_date, instrument in snapshots:
            if snapshot_date <= as_of:
                return instrument
        return None

    def exists(self, symbol_name: str, *, as_of: date | None = None) -> bool:
        """Return ``True`` iff *symbol_name* has at least one row <= *as_of*."""
        return self.get(symbol_name, as_of=as_of) is not None

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

        Uses the latest-snapshot semantics: for each ``(security_id,
        exch_id)`` identity, returns the most recent snapshot <= *as_of*
        (or latest of all time if *as_of* is ``None``).
        """
        self._ensure_open()
        as_of_clause = " AND snapshot_date <= ?" if as_of is not None else ""
        extras: list[str] = []
        extras_params: list[Any] = []
        if segment is not None:
            # Qualify with the ``i`` alias: the ``latest`` subquery now also
            # selects ``segment`` (it is part of the PK identity —
            # DEC-INSTRUMENT-PK-SEGMENT-001), so an unqualified ``segment`` in
            # the outer WHERE is an ambiguous reference (ADR-0003).
            extras.append("i.segment = ?")
            extras_params.append(str(segment))
        if instrument_type is not None:
            extras.append("i.instrument = ?")
            extras_params.append(str(instrument_type))
        if underlying_symbol is not None:
            extras.append("i.underlying_symbol = ?")
            extras_params.append(underlying_symbol)
        extras_sql = (" AND " + " AND ".join(extras)) if extras else ""

        # Every value is parameter-bound; only literal clause strings from the
        # static ``extras`` list are concatenated (no SQL-injection vector).
        base_sql = (
            "SELECT i.* FROM instruments i "
            "JOIN ("
            "    SELECT security_id, exch_id, segment, MAX(snapshot_date) AS snap "
            "    FROM instruments "
        )
        sub_sql = f"    WHERE in_scope = TRUE{as_of_clause} "
        join_sql = (
            "    GROUP BY security_id, exch_id, segment"
            ") latest "
            "ON i.security_id = latest.security_id "
            "AND i.exch_id = latest.exch_id "
            "AND i.segment = latest.segment "
            "AND i.snapshot_date = latest.snap "
        )
        outer_sql = f"WHERE i.in_scope = TRUE{extras_sql} "
        order_sql = "ORDER BY i.symbol_name"
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

    def reference_only_security_ids(self, *, as_of: date | None = None) -> frozenset[int]:
        """Return the ``SECURITY_ID``s flagged ``reference_only`` (v6 column).

        DEC-NIFTY-TOTAL-MARKET-REFTAG-001: the explicit REFERENCE_ONLY tier —
        India VIX today; widened to the non-F&O NIFTY-Total-Market constituents
        in later Stage-2b sub-PRs. Latest-snapshot semantics per
        ``(security_id, exch_id, segment)`` (mirrors :meth:`list_in_scope`).
        """
        self._ensure_open()
        as_of_clause = " AND snapshot_date <= ?" if as_of is not None else ""
        # Assembled from pre-built string parts (mirrors list_in_scope): only
        # the literal clause strings concatenate; ``as_of`` is parameter-bound,
        # so this is not a SQL-injection vector.
        base_sql = (
            "SELECT i.security_id FROM instruments i "
            "JOIN ("
            "    SELECT security_id, exch_id, segment, MAX(snapshot_date) AS snap "
            "    FROM instruments "
        )
        sub_sql = f"    WHERE reference_only = TRUE{as_of_clause} "
        join_sql = (
            "    GROUP BY security_id, exch_id, segment"
            ") latest "
            "ON i.security_id = latest.security_id "
            "AND i.exch_id = latest.exch_id "
            "AND i.segment = latest.segment "
            "AND i.snapshot_date = latest.snap "
            "WHERE i.reference_only = TRUE"
        )
        sql = f"{base_sql}{sub_sql}{join_sql}"  # nosec B608
        params: list[Any] = [as_of] if as_of is not None else []
        rows = self._conn.execute(sql, params).fetchall()
        return frozenset(int(r[0]) for r in rows)

    def fno_eligible_underlyings(
        self, *, as_of: date | None = None, tradable_only: bool = False
    ) -> frozenset[tuple[Exchange, str]]:
        """Return the NSE F&O-eligible ``(exchange, underlying_symbol)`` set in SQL.

        The SQL-pushed-down equivalent of
        :func:`brutex.universe.build_fno_eligible_underlyings` (same rule:
        an underlying is eligible iff at least one NSE derivative — FUT / CE
        / PE — names it), computed with a ``DISTINCT`` aggregate so it
        returns only the ~hundreds of distinct underlyings instead of
        materialising every option/future leg as a Python object
        (DEC-INSTRUMENT-UNIVERSE-FAST-001).

        Lean-store robustness (DEC-INSTRUMENT-UNIVERSE-FAST-001): because the
        lean ingest no longer persists the FUT / CE / PE legs, scanning for
        them alone would return an empty set on a real lean store. The result
        is therefore the UNION of two equivalent sources:

        * every NSE underlying NAMED by a persisted NSE derivative leg (the
          full-master / with-legs case), AND
        * every persisted NSE ``EQ`` row's own ``(exch_id, symbol_name)``
          — which, on a lean store, is F&O-eligible *by construction* (a
          non-eligible equity is never persisted; see
          :func:`brutex.universe.is_universe_underlying`).

        On a full with-legs snapshot the second source is a subset of the
        first (every persisted EQ is F&O-eligible there too), so the union
        equals :func:`build_fno_eligible_underlyings`. An equivalence
        property test (`test_universe_query`) pins this on full-master data,
        keeping the classification rule single-source. Latest-snapshot-per-
        symbol semantics match ``load_snapshot(in_scope_only=False)``.

        Parameters
        ----------
        as_of
            Optional snapshot cutoff (latest snapshot ≤ *as_of* per symbol).
        tradable_only
            When ``True``, the EQ arm (b) additionally filters
            ``reference_only = FALSE``, so the ~539 NIFTY-Total-Market
            REFERENCE_ONLY constituents (cash names with NO option chain;
            DEC-NIFTY-TOTAL-MARKET-REFTAG-001) are EXCLUDED — the scope the
            mapping-integrity gate needs so a reference-only name never
            inflates it ~2.5× nor keeps it perpetually red
            (DEC-MAPPING-INTEGRITY-ATTEMPTED-001). The DEFAULT ``False``
            keeps the FULL universe — the ``/universe`` summary classifies a
            REFERENCE_ONLY constituent as a "stock" so the operator sees the
            whole ~750-name universe (the leg arm (a) is unaffected either
            way — a derivative leg is never ``reference_only``).
        """
        self._ensure_open()
        as_of_clause = " AND snapshot_date <= ?" if as_of is not None else ""
        # Only the static as_of_clause literal is interpolated; the ? is bound.
        latest = (
            "JOIN ("  # noqa: S608  # nosec B608 — static as_of_clause literal only; ? is bound
            "    SELECT security_id, exch_id, segment, MAX(snapshot_date) AS snap "
            f"    FROM instruments WHERE TRUE{as_of_clause} "
            "    GROUP BY security_id, exch_id, segment"
            ") latest ON i.security_id = latest.security_id "
            "AND i.exch_id = latest.exch_id "
            "AND i.segment = latest.segment "
            "AND i.snapshot_date = latest.snap "
        )
        # The optional tradable-only filter on the EQ arm (b) is a STATIC literal
        # selected by a bool, never an interpolated value — no injection vector.
        eq_reference_clause = " AND i.reference_only = FALSE" if tradable_only else ""
        sql = (
            # (a) underlyings named by a persisted NSE derivative leg
            "SELECT DISTINCT i.exch_id, i.underlying_symbol "  # noqa: S608  # nosec B608 — only the static as_of_clause literal is concatenated; all values are bound
            f"FROM instruments i {latest}"
            "WHERE i.instrument IN ('FUT', 'CE', 'PE') "
            "AND i.exch_id = 'NSE' "
            "AND i.underlying_symbol IS NOT NULL "
            "UNION "
            # (b) every persisted NSE EQ row (F&O-eligible by construction). When
            # ``tradable_only`` is set, ``reference_only = FALSE`` additionally drops the
            # ~539 NIFTY-Total-Market REFERENCE_ONLY constituents (no option chain) — the
            # scope the mapping-integrity gate needs (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).
            "SELECT DISTINCT i.exch_id, i.symbol_name "
            f"FROM instruments i {latest}"
            f"WHERE i.instrument = 'EQ' AND i.exch_id = 'NSE'{eq_reference_clause}"
        )
        params: list[Any] = []
        if as_of is not None:
            # ``latest`` (with its ``as_of`` ?) appears twice — once per UNION arm.
            params = [as_of, as_of]
        rows = self._conn.execute(sql, params).fetchall()
        return frozenset((Exchange(row[0]), row[1]) for row in rows)

    def list_index_equity_instruments(self, *, as_of: date | None = None) -> list[Instrument]:
        """Return only IDX + EQ rows (the explorer candidates) — never the legs.

        The ``/universe`` explorer shows exactly indices (IDX) and F&O-eligible
        equities (EQ); the ~tens-of-thousands of option/future legs never
        appear there. This fetches only the IDX + EQ rows (latest snapshot
        per symbol, matching ``load_snapshot(in_scope_only=False)``) so the
        dashboard materialises only those ~hundreds of rows, not the whole
        master (DEC-INSTRUMENT-UNIVERSE-FAST-001). Pair with
        :meth:`fno_eligible_underlyings` and feed both to
        :func:`brutex.dashboard.universe_query.list_underlyings` /
        ``universe_summary`` (which keep the classification rule).
        """
        self._ensure_open()
        as_of_clause = " AND snapshot_date <= ?" if as_of is not None else ""
        sql = (
            "SELECT i.* FROM instruments i "  # noqa: S608  # nosec B608 — only the static as_of_clause literal is concatenated; all values are bound
            "JOIN ("
            "    SELECT security_id, exch_id, segment, MAX(snapshot_date) AS snap "
            f"    FROM instruments WHERE TRUE{as_of_clause} "
            "    GROUP BY security_id, exch_id, segment"
            ") latest ON i.security_id = latest.security_id "
            "AND i.exch_id = latest.exch_id "
            "AND i.segment = latest.segment "
            "AND i.snapshot_date = latest.snap "
            "WHERE i.instrument IN ('IDX', 'EQ') "
            "ORDER BY i.symbol_name"
        )
        params: list[Any] = [as_of] if as_of is not None else []
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_instrument(row) for row in rows]

    def has_derivative_legs(self) -> bool:
        """Return ``True`` if any FUT/CE/PE leg is persisted — the non-lean fingerprint.

        Lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001) never persists option/
        future legs, so their presence means the lake was populated by an
        older, pre-lean build. The on-run auto-heal uses this to trigger a
        clean lean re-ingest (DEC-LEAN-REINGEST-FIX-001) instead of serving
        stale, over-counted data. One indexed ``EXISTS`` probe — O(1)-ish.
        """
        self._ensure_open()
        row = self._conn.execute(
            "SELECT EXISTS (SELECT 1 FROM instruments WHERE instrument IN ('FUT', 'CE', 'PE'))"
        ).fetchone()
        return bool(row[0]) if row else False

    def has_placeholder_stock_economics(self) -> bool:
        """Return ``True`` if any latest-snapshot **F&O** NSE equity still has placeholder economics.

        The pre-economics-backfill fingerprint (DEC-STALE-ECONOMICS-HEAL-001):
        every persisted **F&O-eligible** NSE ``EQ`` row carries, after the
        leg-derived back-fill (DEC-UNDERLYING-ECONOMICS-001), a real F&O
        ``lot_size`` (always > 1 — e.g. RELIANCE 500, the smallest real lots are
        still in the hundreds). A lake loaded BEFORE that back-fill landed
        instead carries the master's placeholder ``lot_size = 1``. This probe —
        an indexed ``EXISTS`` over the latest snapshot per symbol (matching
        ``list_index_equity_instruments`` / ``load_snapshot`` semantics) — is
        therefore ``True`` iff the lake holds stale, un-enriched F&O stock
        economics and should be re-loaded.

        **REFERENCE_ONLY equities are EXCLUDED** (DEC-NIFTY-TOTAL-MARKET-ADMIT-001,
        Stage 2b): a non-F&O constituent admitted as ``reference_only=TRUE`` has
        NO derivative leg, so its cash ``lot_size = 1`` is CORRECT, not a stale
        placeholder. Without this exclusion the auto-heal would see those rows,
        believe the lake is un-enriched, and re-load the master forever (the
        Stage-2b hostile-review P1). The ``reference_only = FALSE`` clause scopes
        the probe back to the F&O equities it was designed for.

        It is the auto-heal trigger that closes the gap the operator hit:
        ``should_reload_instrument_master`` previously reloaded only on an
        empty / index-less / non-lean lake, so a lake populated by a
        pre-back-fill build kept serving ``lot_size = 1`` forever. A clean
        re-load enriches every stock, after which this returns ``False``
        (idempotent — no perpetual reloads).

        Returns
        -------
        bool
            ``True`` iff at least one latest-snapshot NSE **F&O** ``EQ`` row
            (``reference_only = FALSE``) has ``lot_size <= 1``; ``False`` on a
            fully-enriched (or stock-less) lake. REFERENCE_ONLY equities never
            count.
        """
        self._ensure_open()
        # A master load (replace_snapshot=True) writes EVERY universe symbol for
        # ONE snapshot_date, and prune_to_latest_snapshot() collapses to it — so
        # the newest snapshot is the whole live universe and ``snapshot_date =
        # MAX(snapshot_date)`` is exactly the latest-per-symbol view here, while
        # hitting the PK index instead of a GROUP BY full scan. The
        # ``reference_only = FALSE`` clause excludes non-F&O constituents whose
        # cash lot_size=1 is correct (DEC-NIFTY-TOTAL-MARKET-ADMIT-001).
        sql = (
            "SELECT EXISTS ("
            "  SELECT 1 FROM instruments "
            "  WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM instruments) "
            "  AND instrument = 'EQ' AND exch_id = 'NSE' AND lot_size <= 1 "
            "  AND reference_only = FALSE"
            ")"
        )
        row = self._conn.execute(sql).fetchone()
        return bool(row[0]) if row else False

    def has_stale_option_eligibility(self) -> bool:
        """Return ``True`` if the latest snapshot carries pre-precise OPT/REF flags.

        The pre-DEC-OPTIONS-ELIGIBLE-PRECISE-001 fingerprint
        (DEC-ELIGIBILITY-AUTOHEAL-001): the retired id-collision tagger left
        stores where a brute-force index (NIFTY / BANKNIFTY / SENSEX — all of
        which ALWAYS carry OPTIDX legs in the real master) sits wrongly
        ``reference_only = TRUE``, and/or INDIA VIX (which has NO option chain
        and is REFERENCE ONLY by decree, CLAUDE.md §2 Rule 3) sits wrongly
        option-pullable. Either direction means the flags were written by an
        old build: the boot auto-heal then re-loads the keyless master so the
        precise tagger re-tags every row — the operator never has to remember
        the "Load instruments" click after an upgrade.

        Idempotent by the same argument as the sibling probes: a precise-tagger
        refresh clears both arms, so the reload fires at most once. (If the
        vendor master ever genuinely lost the NIFTY option chain, the reload
        would re-fire each boot — one keyless CSV fetch per boot, logged, and a
        far smaller problem than the broken vendor data itself.) One indexed
        ``EXISTS`` probe over the latest snapshot, mirroring
        :meth:`has_placeholder_stock_economics`.
        """
        self._ensure_open()
        sql = (
            "SELECT EXISTS ("
            "  SELECT 1 FROM instruments "
            "  WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM instruments) "
            "  AND instrument = 'IDX' "
            "  AND ("
            "    (symbol_name IN ('NIFTY', 'BANKNIFTY', 'SENSEX') "
            "     AND reference_only = TRUE) "
            "    OR (symbol_name = 'INDIA VIX' AND reference_only = FALSE)"
            "  )"
            ")"
        )
        row = self._conn.execute(sql).fetchone()
        return bool(row[0]) if row else False

    def prune_to_latest_snapshot(self) -> int:
        """Delete every instrument row except those of the newest ``snapshot_date``.

        The instrument master is a CURRENT-STATE surface — Track 1 wants only
        the latest ~241 universe underlyings, NOT a daily-growing pile of
        historical copies (DEC-INSTRUMENT-LEAN-SNAPSHOT-001). Each keyless
        reload stamps a new ``snapshot_date`` (the UTC pull date), and
        :meth:`upsert_many` with ``replace_snapshot=True`` only wipes the SAME
        day, so without this prune the table accumulates one full ~236-row copy
        per UTC day: the operator saw the ``instruments`` table grow 236 → 472
        after a second-day reload, which read as a broken dedup guarantee.
        Collapsing to the newest snapshot keeps the lake lean + unique and makes
        the read-only ``/db`` view show exactly the live universe.

        Latest-snapshot reads (:meth:`load_snapshot`,
        :meth:`list_index_equity_instruments`) are unaffected — they already
        select ``MAX(snapshot_date)`` per symbol, so this only removes rows that
        were already shadowed. Idempotent: a single-snapshot (already-lean)
        table deletes nothing; safe on an empty table.

        Returns
        -------
        int
            The number of stale (non-latest-snapshot) rows deleted.
        """
        self._ensure_open()
        # Single statement: DuckDB returns the deleted-row count, and a
        # correlated ``MAX(snapshot_date)`` sub-select makes this a no-op on an
        # empty table (``<> NULL`` matches nothing) and on a single-snapshot
        # lake (nothing is older than the max) — no redundant COUNT pre-scan.
        deleted = self._conn.execute(
            "DELETE FROM instruments "
            "WHERE snapshot_date <> (SELECT MAX(snapshot_date) FROM instruments)"
        ).fetchone()
        # The table may have shed rows — invalidate the O(1) point-lookup index
        # so the next get() rebuilds from the pruned rows
        # (DEC-INSTRUMENTSTORE-O1-001).
        self._symbol_index = None
        return int(deleted[0]) if deleted else 0

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
        symbol_name order.
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
        return sorted(calls + puts, key=lambda i: i.symbol_name)

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
                    "    SELECT security_id, exch_id, segment, MAX(snapshot_date) AS snap "
                    "    FROM instruments "
                    "    WHERE (? IS NULL OR snapshot_date <= ?) "
                    "    GROUP BY security_id, exch_id, segment"
                    ") latest ON i.security_id = latest.security_id "
                    "AND i.exch_id = latest.exch_id "
                    "AND i.segment = latest.segment "
                    "AND i.snapshot_date = latest.snap "
                    "ORDER BY i.symbol_name",
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

        * **Duplicate primary keys** \u2014 ``COUNT(*) vs COUNT(DISTINCT
          (security_id, exch_id, segment, snapshot_date))``. DuckDB's PK
          constraint makes this impossible via normal writes, but this
          check defends against direct-SQL tampering.
        * **Raw-hash drift** \u2014 recomputes
          :func:`compute_raw_hash` for every row and compares it
          to the stored ``raw_hash``. Any mismatch means either
          the normaliser changed (code drift) or a row was
          hand-edited.
        * **CSV source drift** \u2014 returns a per-date map of
          distinct ``csv_source_sha256`` counts. Anything other
          than ``1`` means two pulls on the same day saw
          different CSV content \u2014 vendor master CSV drift in-flight.
        """
        self._ensure_open()
        total_row = self._conn.execute("SELECT COUNT(*) FROM instruments").fetchone()
        distinct_row = self._conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT DISTINCT security_id, exch_id, segment, snapshot_date FROM instruments)"
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
            # Close the child READ cursor first, then the parent writer
            # connection (DEC-STORE-READ-WRITE-SPLIT-001).
            self._read_conn.close()
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
    reference_only: bool,
) -> list[Any]:
    """Project *instrument* into the column tuple the INSERT expects.

    Column order matches :data:`_INSTR_COLUMNS` exactly.
    """
    raw_hash = compute_raw_hash(instrument)
    return [
        instrument.security_id,
        str(instrument.exch_id),
        str(instrument.segment),
        instrument.isin,
        str(instrument.instrument),
        instrument.underlying_security_id,
        instrument.underlying_symbol,
        instrument.symbol_name,
        instrument.display_name,
        instrument.series,
        instrument.lot_size,
        instrument.sm_expiry_date,
        instrument.strike_price,
        instrument.option_type,
        instrument.tick_size,
        instrument.expiry_flag,
        instrument.asm_gsm_flag,
        instrument.sm_freeze_qty,
        snapshot_date,
        in_scope,
        raw_hash,
        fetched_at,
        run_id,
        csv_source_sha256,
        reference_only,
        instrument.feed_token,
    ]


def _row_to_instrument(row: Sequence[Any]) -> Instrument:
    """Invert :func:`_instrument_to_params` \u2014 read a DuckDB row back out.

    The SQL column order is fixed via :data:`_INSTR_COLUMNS` so the
    tuple unpacking here stays in sync with the writer.
    """
    (
        security_id,
        exch_id,
        segment,
        isin,
        instrument,
        underlying_security_id,
        underlying_symbol,
        symbol_name,
        display_name,
        series,
        lot_size,
        sm_expiry_date,
        strike_price,
        option_type,
        tick_size,
        expiry_flag,
        asm_gsm_flag,
        sm_freeze_qty,
        _snapshot_date,
        _in_scope,
        _raw_hash,
        _fetched_at,
        _run_id,
        _csv_sha,
        _reference_only,
        feed_token,
    ) = row
    return Instrument(
        exch_id=Exchange(exch_id),
        security_id=int(security_id) if security_id is not None else 0,
        symbol_name=symbol_name,
        display_name=display_name,
        instrument=InstrumentType(instrument),
        segment=Segment(segment),
        series=series,
        isin=isin,
        underlying_symbol=underlying_symbol,
        underlying_security_id=(
            int(underlying_security_id) if underlying_security_id is not None else None
        ),
        lot_size=int(lot_size),
        tick_size=Decimal(str(tick_size)),
        sm_freeze_qty=int(sm_freeze_qty) if sm_freeze_qty is not None else None,
        sm_expiry_date=sm_expiry_date,
        strike_price=(Decimal(str(strike_price)) if strike_price is not None else None),
        option_type=option_type,
        expiry_flag=expiry_flag,
        asm_gsm_flag=bool(asm_gsm_flag),
        feed_token=feed_token,
    )


def _row_to_coverage(row: Sequence[Any]) -> CoverageRecord:
    """Invert the ``coverage_ledger`` SELECT into a :class:`CoverageRecord`.

    Column order matches the SELECT list in :meth:`InstrumentStore.get_coverage`
    / :meth:`list_coverage` / :meth:`list_incomplete_coverage`.
    """
    return CoverageRecord(
        exch_id=row[0],
        underlying_symbol=row[1],
        year=int(row[2]),
        expiries_pulled=int(row[3]),
        contracts_pulled=int(row[4]),
        status=row[5],
        last_run_id=row[6],
        last_fetched_at=row[7],
        error_message=row[8],
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
    "CoverageRecord",
    "InstrumentRun",
    "InstrumentSnapshot",
    "InstrumentStore",
    "IntegrityReport",
    "UpsertResult",
    "compute_raw_hash",
    "open_instrument_store",
]
