"""DuckDB-backed catalog for the BruteX data lake.

Implements ``FR-CAT-002`` (catalog row shape), ``FR-CAT-003``
(``get_bars`` Arrow zero-copy reader) and ``FR-CAT-004``
(``data_snapshot_hash`` rollup).

``duckdb``, ``polars`` and ``pyarrow`` are imported lazily so unit
tests that touch only the pure-Python pieces don't need the P2
data-lake extras installed.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self

import structlog

from brutex.data.errors import CatalogError
from brutex.data.models import CatalogEntry
from brutex.data.partition import build_partition_path

if TYPE_CHECKING:  # pragma: no cover
    import polars as pl
    import pyarrow as pa

_LOG = structlog.get_logger("brutex.data.catalog")

#: Short back-off between a torn read and its single retry (the concurrent-writer
#: inode-straddle window; the ``dashboard/db_view.py`` torn-read-retry scale,
#: DEC-WINDOW-TORN-READ-RETRY-001). Injectable so tests never sleep.
_PARTITION_READ_RETRY_DELAY_S: Final[float] = 0.05

#: The canonical bar columns every readable partition must carry (mirrors
#: :func:`_empty_bars_table`). A partition missing any of these is a foreign /
#: old-schema file and is UNREADABLE (never silently skipped, Golden Rule #4).
_CANONICAL_BAR_COLUMNS: Final[tuple[str, ...]] = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
)


#: DDL for the single ``partitions`` table. Keep this in sync with
#: :class:`CatalogEntry`. The primary key matches FR-CAT-002.
_DDL: str = """
CREATE TABLE IF NOT EXISTS partitions (
    exchange VARCHAR NOT NULL,
    segment VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    interval VARCHAR NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    raw_hash VARCHAR NOT NULL,
    row_count BIGINT NOT NULL,
    first_ts TIMESTAMP WITH TIME ZONE NOT NULL,
    last_ts TIMESTAMP WITH TIME ZONE NOT NULL,
    fetched_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (exchange, segment, symbol, interval, year, month)
);
"""

#: Additive per-partition STAT-CACHE columns (DEC-SWEEP-RECONCILE-STATCACHE-001).
#: These persist a partition file's ``(st_mtime_ns, st_size, st_ino)`` freshness
#: token OUTSIDE the frozen 11-field :class:`~brutex.data.models.CatalogEntry`
#: model and the shared ``upsert``/``get``/``list_partitions`` 11-column SELECTs,
#: so the sweep-launch reconcile can skip the full-bytes ``sha256`` + timestamp
#: re-read of a byte-unchanged partition on an EXACT token match. ``st_ino`` is
#: mandatory: every lake writer publishes via ``os.replace`` (a fresh inode), so
#: an inode change defeats a same-size in-place rewrite AND a coarse-mtime
#: external-SSD collision. A legacy pre-migration row reads ``NULL`` for all
#: three ⇒ an explicit cache MISS (full reconcile, byte-identical to today, that
#: backfills the token) — ``NULL`` is NEVER treated as a match (no COALESCE-to-0).
_STAT_CACHE_COLUMNS: Final[tuple[str, ...]] = ("file_mtime_ns", "file_size", "file_ino")

#: One ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` per column (DuckDB adds a
#: single column per statement), run in :meth:`Catalog.open` after ``_DDL`` so a
#: pre-existing ``partitions`` table gains the columns in place; each is
#: idempotent, so re-opening an already-migrated catalog is a clean no-op.
_STAT_CACHE_DDL: Final[tuple[str, ...]] = tuple(
    f"ALTER TABLE partitions ADD COLUMN IF NOT EXISTS {column} BIGINT"
    for column in _STAT_CACHE_COLUMNS
)


class Catalog:
    """Thin DuckDB wrapper over the catalog partitions table.

    Use as a context manager::

        with Catalog.open(Path("lake/catalog.duckdb")) as cat:
            cat.upsert(entry)
            row = cat.get(exchange="NSE", segment="FNO", symbol="NSE-NIFTY",
                          interval="1minute", year=2024, month=1)

    Thread-safety (DEC-DHAN-PARALLEL-INGEST-001). The DuckDB connection is NOT
    safe for concurrent use, so when a catalog is threaded into the parallel
    spot fan-out (the live spot path passes ``catalog=None`` today, but tests
    wire one) a :class:`threading.Lock` serialises ``upsert`` and the reads
    (``get`` / ``list_partitions`` / ``data_snapshot_hash`` / ``count``). Each
    critical section is one bounded DuckDB statement (the ``upsert`` is a single
    DELETE+INSERT transaction); the lock is re-used internally via the
    ``*_locked`` helpers so a read composed of another read (e.g.
    ``data_snapshot_hash`` over ``list_partitions``) does not self-deadlock.
    Future-proofing: it makes the catalog correct under the fan-out the moment a
    caller wires one.
    """

    def __init__(self, *, conn: object, path: Path) -> None:
        self._conn = conn
        self._path = path
        self._closed = False
        #: Serialises DuckDB access under the parallel fan-out
        #: (DEC-DHAN-PARALLEL-INGEST-001). The connection is not concurrency-safe.
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path, *, require_exists: bool = False) -> Self:
        """Open or create the catalog DB at *path*.

        Parameters
        ----------
        path
            The DuckDB file path (conventionally ``<lake_root>/catalog.duckdb``).
        require_exists
            When ``True``, refuse a *path* that does not already exist instead
            of creating it — the guard for READ-ONLY callers, who must never
            conjure a fresh empty lake/catalog out of a typo'd path
            (DEC-CLI-USER-ERROR-HYGIENE-001). The default ``False`` preserves
            the historical create-on-open behaviour byte-identically (the
            ``fetch`` write path + dashboard boot).

        Raises
        ------
        CatalogError
            If duckdb is not installed, ``require_exists=True`` and *path*
            does not exist, or the DB cannot be opened.
        """
        try:
            import duckdb
        except ImportError as exc:
            raise CatalogError(
                "Catalog requires 'duckdb' — install with "
                "'pip install -e .[data]' to enable the P2 data lake"
            ) from exc

        if require_exists and not path.exists():
            raise CatalogError(
                f"catalog not found at {path} (require_exists=True — refusing to create one)"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = duckdb.connect(str(path))
            conn.execute(_DDL)
            # DEC-SWEEP-RECONCILE-STATCACHE-001: migrate a legacy 11-column
            # `partitions` table in place (idempotent `ADD COLUMN IF NOT EXISTS`).
            for stat_cache_ddl in _STAT_CACHE_DDL:
                conn.execute(stat_cache_ddl)
        except Exception as exc:  # pragma: no cover — defensive
            raise CatalogError(f"failed to open catalog at {path}: {exc}") from exc
        return cls(conn=conn, path=path)

    @property
    def path(self) -> Path:
        """Return the on-disk path of the underlying DuckDB file."""
        return self._path

    def upsert(self, entry: CatalogEntry) -> None:
        """Insert or replace *entry* by its six-coordinate primary key.

        DuckDB doesn't support ``ON CONFLICT DO UPDATE`` for all types
        until 0.9+, so we implement upsert as DELETE + INSERT inside
        a transaction. This keeps the catalog consistent even if the
        writer is interrupted between statements.
        """
        if self._closed:
            raise CatalogError("Catalog is closed")
        # Serialise the DELETE+INSERT transaction — the DuckDB connection is not
        # concurrency-safe (DEC-DHAN-PARALLEL-INGEST-001).
        with self._lock:
            try:
                self._conn.execute("BEGIN")  # type: ignore[attr-defined]
                self._conn.execute(  # type: ignore[attr-defined]
                    """
                    DELETE FROM partitions
                    WHERE exchange = ? AND segment = ? AND symbol = ?
                      AND interval = ? AND year = ? AND month = ?
                    """,
                    [
                        entry.exchange,
                        entry.segment,
                        entry.symbol,
                        entry.interval,
                        entry.year,
                        entry.month,
                    ],
                )
                self._conn.execute(  # type: ignore[attr-defined]
                    """
                    INSERT INTO partitions
                    (exchange, segment, symbol, interval, year, month,
                     raw_hash, row_count, first_ts, last_ts, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        entry.exchange,
                        entry.segment,
                        entry.symbol,
                        entry.interval,
                        entry.year,
                        entry.month,
                        entry.raw_hash,
                        entry.row_count,
                        entry.first_ts,
                        entry.last_ts,
                        entry.fetched_at,
                    ],
                )
                self._conn.execute("COMMIT")  # type: ignore[attr-defined]
            except Exception:
                self._conn.execute("ROLLBACK")  # type: ignore[attr-defined]
                raise

    def get(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        year: int,
        month: int,
    ) -> CatalogEntry | None:
        """Return the catalog row for the given partition, or None if absent."""
        if self._closed:
            raise CatalogError("Catalog is closed")
        with self._lock:
            row = self._conn.execute(  # type: ignore[attr-defined]
                """
                SELECT exchange, segment, symbol, interval, year, month,
                       raw_hash, row_count, first_ts, last_ts, fetched_at
                FROM partitions
                WHERE exchange = ? AND segment = ? AND symbol = ?
                  AND interval = ? AND year = ? AND month = ?
                """,
                [exchange, segment, symbol, interval, year, month],
            ).fetchone()
        if row is None:
            return None
        return CatalogEntry(
            exchange=row[0],
            segment=row[1],
            symbol=row[2],
            interval=row[3],
            year=row[4],
            month=row[5],
            raw_hash=row[6],
            row_count=row[7],
            first_ts=row[8],
            last_ts=row[9],
            fetched_at=row[10],
        )

    def delete_partition(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        year: int,
        month: int,
    ) -> bool:
        """DELETE the row for one partition; ``True`` iff a row existed.

        DEC-LAKE-HEAL-001: a heal can quarantine-MOVE a partition file away (a
        wholly-foreign file) or merge a drifted legacy dir out of existence — a
        dangling catalog row would make :meth:`get_bars` raise on the missing
        file and poison ``data_snapshot_hash`` (Golden Rule #4), so the healer
        deletes the row explicitly. Runs the single ``DELETE ... RETURNING``
        statement under the internal lock; idempotent (a second delete of the
        same coordinates returns ``False``).

        Parameters
        ----------
        exchange, segment, symbol, interval, year, month
            The six-coordinate primary key of the row to delete.

        Returns
        -------
        bool
            ``True`` iff a row existed (and was deleted).
        """
        if self._closed:
            raise CatalogError("Catalog is closed")
        with self._lock:
            deleted = self._conn.execute(  # type: ignore[attr-defined]
                """
                DELETE FROM partitions
                WHERE exchange = ? AND segment = ? AND symbol = ?
                  AND interval = ? AND year = ? AND month = ?
                RETURNING 1
                """,
                [exchange, segment, symbol, interval, year, month],
            ).fetchall()
        return len(deleted) > 0

    def get_partition_stat_tokens(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
    ) -> dict[tuple[int, int], tuple[int, int, int] | None]:
        """Return every ``(year, month) -> stat token`` for one ``(symbol, interval)``.

        The reconcile stat-cache warm path (DEC-SWEEP-RECONCILE-STATCACHE-001)
        bulk-loads a whole series' tokens in ONE indexed query, so the per-file
        freshness check is an ``os.stat`` + an O(1) dict lookup with ZERO Parquet
        reads. A token is the partition file's ``(st_mtime_ns, st_size,
        st_ino)``; a row whose stat columns are ``NULL`` (a legacy row written
        before the migration, or one whose token write failed) maps to ``None``
        — an explicit cache MISS, never a match.

        Parameters
        ----------
        exchange, segment, symbol, interval
            The four series coordinates. All concrete.

        Returns
        -------
        dict
            ``{(year, month): (mtime_ns, size, ino)}``, with a ``None`` value for
            any row that has not yet been stamped with a full token.
        """
        if self._closed:
            raise CatalogError("Catalog is closed")
        with self._lock:
            rows = self._conn.execute(  # type: ignore[attr-defined]
                """
                SELECT year, month, file_mtime_ns, file_size, file_ino
                FROM partitions
                WHERE exchange = ? AND segment = ? AND symbol = ? AND interval = ?
                """,
                [exchange, segment, symbol, interval],
            ).fetchall()
        tokens: dict[tuple[int, int], tuple[int, int, int] | None] = {}
        for year, month, mtime_ns, size, ino in rows:
            if mtime_ns is None or size is None or ino is None:
                tokens[(int(year), int(month))] = None
            else:
                tokens[(int(year), int(month))] = (int(mtime_ns), int(size), int(ino))
        return tokens

    def set_partition_stat(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        year: int,
        month: int,
        file_mtime_ns: int,
        file_size: int,
        file_ino: int,
    ) -> None:
        """Backfill the stat-cache token columns for one already-upserted partition.

        Kept SEPARATE from :meth:`upsert` so the frozen
        :class:`~brutex.data.models.CatalogEntry` model + its shared 11-column
        ``INSERT`` stay untouched (DEC-SWEEP-RECONCILE-STATCACHE-001). The
        reconcile stat-cache calls this immediately after the row's ``upsert``, so
        the row always exists; a DuckDB fault (e.g. ``ENOSPC``) is wrapped into
        :class:`~brutex.data.errors.CatalogError` and swallowed best-effort by the
        caller (a torn stat write never fails the launch — the token stays ``NULL``
        and the file is simply recomputed next launch).

        Parameters
        ----------
        exchange, segment, symbol, interval, year, month
            The six-coordinate primary key of the row to stamp.
        file_mtime_ns, file_size, file_ino
            The partition file's ``os.stat`` freshness token.

        Raises
        ------
        CatalogError
            If the catalog is closed, or the underlying ``UPDATE`` fails.
        """
        if self._closed:
            raise CatalogError("Catalog is closed")
        with self._lock:
            try:
                self._conn.execute(  # type: ignore[attr-defined]
                    """
                    UPDATE partitions
                    SET file_mtime_ns = ?, file_size = ?, file_ino = ?
                    WHERE exchange = ? AND segment = ? AND symbol = ?
                      AND interval = ? AND year = ? AND month = ?
                    """,
                    [
                        file_mtime_ns,
                        file_size,
                        file_ino,
                        exchange,
                        segment,
                        symbol,
                        interval,
                        year,
                        month,
                    ],
                )
            except Exception as exc:
                raise CatalogError(f"failed to write partition stat cache: {exc}") from exc

    def list_partitions(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CatalogEntry]:
        """Return every partition whose ``[first_ts, last_ts]`` overlaps
        ``[start, end]`` for the given coordinates.

        The result is sorted ascending by ``(year, month)`` so repeated
        calls are deterministic — the ordering is the contract the
        :meth:`data_snapshot_hash` rollup relies on.

        ``start`` / ``end`` may be ``None`` to mean "no bound on that
        side". Both must be timezone-aware if supplied.
        """
        if self._closed:
            raise CatalogError("Catalog is closed")
        _require_tz_aware(start, name="start")
        _require_tz_aware(end, name="end")

        clauses: list[str] = [
            "exchange = ?",
            "segment = ?",
            "symbol = ?",
            "interval = ?",
        ]
        params: list[Any] = [exchange, segment, symbol, interval]
        # A partition intersects the query window iff last_ts >= start
        # AND first_ts <= end. Skipping either bound drops the clause.
        if start is not None:
            clauses.append("last_ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("first_ts <= ?")
            params.append(end)

        # Every user-supplied value flows through DuckDB parameter
        # binding (the ``?`` placeholders). The only string
        # concatenation is over the static whitelist of ``clauses``
        # literals defined above, not over any untrusted input — so
        # this is not a SQL-injection vector.
        where = " AND ".join(clauses)
        base_sql = (
            "SELECT exchange, segment, symbol, interval, year, month, "
            "raw_hash, row_count, first_ts, last_ts, fetched_at "
            "FROM partitions"
        )
        sql = f"{base_sql} WHERE {where} ORDER BY year ASC, month ASC"  # nosec B608
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()  # type: ignore[attr-defined]
        return [
            CatalogEntry(
                exchange=row[0],
                segment=row[1],
                symbol=row[2],
                interval=row[3],
                year=row[4],
                month=row[5],
                raw_hash=row[6],
                row_count=row[7],
                first_ts=row[8],
                last_ts=row[9],
                fetched_at=row[10],
            )
            for row in rows
        ]

    def get_bars(
        self,
        *,
        lake_root: Path,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        retry_sleep: Callable[[float], None] | None = None,
    ) -> pa.Table:
        """Return a :class:`pyarrow.Table` of bars in ``[start, end]``.

        Implements ``FR-CAT-003``. The catalog is consulted to find
        every ``(year, month)`` partition whose timestamp range
        intersects the query window; each matching Parquet file is
        read via :func:`polars.read_parquet` and converted to Arrow
        with zero-copy (``polars.DataFrame.to_arrow``). The individual
        tables are then concatenated and filtered to the exact
        ``[start, end]`` window.

        The return type is a ``pyarrow.Table`` — the shared lingua
        franca for zero-copy interop with the data lake layer.
        Columns are::

            timestamp (timestamp[us, tz=UTC])
            open / high / low / close (float64)
            volume (int64)
            open_interest (int64, nullable)

        Parameters
        ----------
        lake_root
            Top-level lake directory. Used to resolve each catalog
            row to its on-disk Parquet file.
        exchange, segment, symbol, interval
            Partition coordinates. All four must be concrete — this
            method reads bars for one series at a time. Cross-symbol
            queries should call :meth:`get_bars` per symbol.
        start, end
            Inclusive window. Must be timezone-aware.
        retry_sleep
            Optional ``sleep(seconds)`` injected into the per-partition
            torn-read retry (see :func:`_read_partition_frame`) — defaults to
            :func:`time.sleep`; tests pass a no-op so no wall-clock time is
            spent. Additive: existing callers are unchanged.

        Returns
        -------
        pa.Table
            Bars sorted ascending by timestamp. Empty table (with the
            canonical schema) when no partitions match.

        Raises
        ------
        CatalogError
            If ``polars`` or ``pyarrow`` is not installed, if a referenced
            partition file is missing from disk, or if a referenced partition
            is **UNREADABLE** — corrupt/torn bytes surviving one retry, or a
            foreign / wrong-dtype schema (:func:`_read_partition_frame`). A
            corrupt partition is NEVER silently skipped: a dropped file would
            hole the ``data_snapshot_hash`` inputs and silently change run
            identity (Golden Rule #4), so the read refuses LOUDLY naming the
            file.
        """
        if self._closed:
            raise CatalogError("Catalog is closed")
        if start.tzinfo is None or end.tzinfo is None:
            raise CatalogError("get_bars: start and end must be timezone-aware")
        if end < start:
            raise CatalogError(f"get_bars: end ({end}) must be >= start ({start})")

        try:
            import polars as pl
            import pyarrow as pa
        except ImportError as exc:
            raise CatalogError(
                "get_bars requires 'polars' + 'pyarrow' — install with "
                "'pip install -e .[data]' to enable the P2 data lake"
            ) from exc

        entries = self.list_partitions(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
        )
        if not entries:
            return _empty_bars_table(pa)

        frames: list[pl.DataFrame] = []
        for entry in entries:
            path = build_partition_path(
                lake_root=lake_root,
                exchange=entry.exchange,
                segment=entry.segment,
                symbol=entry.symbol,
                interval=entry.interval,
                year=entry.year,
                month=entry.month,
            )
            if not path.exists():
                raise CatalogError(
                    f"catalog references missing partition file {path} "
                    f"— run 'brutex data verify' to re-sync the catalog"
                )
            frames.append(_read_partition_frame(path, retry_sleep=retry_sleep))

        combined = pl.concat(frames, how="vertical_relaxed")
        # Read-side dedup gate (DEC-LAKE-READ-UNIQUE-001): a partition poisoned
        # BEFORE the write-side uniqueness fix (DEC-LAKE-WRITE-UNIQUE-001) can
        # physically carry duplicate timestamps; collapse them keep-FIRST — the
        # frames are concatenated in ascending (year, month) order, so the first
        # physical row matches what a write-side heal keeps — BEFORE the sort
        # (sorting could reorder equal timestamps and make "first" ambiguous).
        # This is the engine's load-bearing input mouth: every ReplayFeed /
        # sweep / cascade bar stream flows through here, so a duplicate bar can
        # never reach the Stage-1A condition masks again. The collapse warns
        # (`lake.read.duplicates_dropped`), never silent; a clean partition is
        # byte-identical (unique() drops nothing).
        from brutex.data.lake_read import dedup_bars_frame

        combined, _dropped = dedup_bars_frame(
            combined, source=f"catalog:{exchange}/{segment}/{symbol}/{interval}"
        )
        # Filter to the exact window and keep only the canonical column
        # order. polars' filter is zero-copy slice-level, not row-level.
        filtered = combined.filter(
            (pl.col("timestamp") >= start) & (pl.col("timestamp") <= end)
        ).sort("timestamp")
        return filtered.to_arrow()

    def data_snapshot_hash(
        self,
        *,
        exchange: str,
        segment: str,
        symbols: Sequence[str],
        interval: str,
        start: datetime,
        end: datetime,
    ) -> str:
        """Return the ``data_snapshot_hash`` for a ``(universe, interval, range)``.

        Implements ``FR-CAT-004``. The hash is the sha256 of the
        concatenated ``raw_hash`` values of every partition that
        intersects the query window, ordered canonically so the
        result is stable across invocations::

            sorted_entries = sort_by(symbol, year, month)
            concat = "".join(e.raw_hash for e in sorted_entries)
            snapshot = sha256(concat).hexdigest()

        An empty slice (no partitions match) returns the sha256 of the
        empty string — deterministic by construction.

        Parameters
        ----------
        exchange, segment, interval
            Shared partition coordinates.
        symbols
            Iterable of trading symbols in the universe. Duplicates are
            de-duped and the final ordering is alphabetical so the
            caller does not need to pre-sort.
        start, end
            Inclusive window. Must be timezone-aware.

        Returns
        -------
        str
            64-character sha256 hex digest.
        """
        if self._closed:
            raise CatalogError("Catalog is closed")
        if start.tzinfo is None or end.tzinfo is None:
            raise CatalogError("data_snapshot_hash: start and end must be timezone-aware")
        if end < start:
            raise CatalogError(f"data_snapshot_hash: end ({end}) must be >= start ({start})")

        canonical_symbols = sorted(set(symbols))
        hasher = hashlib.sha256()
        for symbol in canonical_symbols:
            entries = self.list_partitions(
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
            )
            # list_partitions already returns rows sorted by (year, month).
            for entry in entries:
                hasher.update(entry.raw_hash.encode("ascii"))
        return hasher.hexdigest()

    def count(self) -> int:
        """Return the number of partition rows recorded in the catalog."""
        if self._closed:
            raise CatalogError("Catalog is closed")
        with self._lock:
            result = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) FROM partitions"
            ).fetchone()
        return int(result[0]) if result else 0

    def close(self) -> None:
        """Close the underlying DuckDB connection. Idempotent."""
        if self._closed:
            return
        try:
            self._conn.close()  # type: ignore[attr-defined]
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


@contextmanager
def open_catalog(path: Path) -> Generator[Catalog, None, None]:
    """Context-manager sugar for :meth:`Catalog.open`."""
    cat = Catalog.open(path)
    try:
        yield cat
    finally:
        cat.close()


def _require_tz_aware(ts: datetime | None, *, name: str) -> None:
    """Raise :class:`CatalogError` if *ts* is a naive (tz-less) datetime."""
    if ts is not None and ts.tzinfo is None:
        raise CatalogError(f"{name} must be timezone-aware, got naive datetime {ts!r}")


def _read_partition_frame(
    path: Path,
    *,
    retry_sleep: Callable[[float], None] | None = None,
) -> pl.DataFrame:
    """Read ONE partition Parquet into a schema-validated frame — retry-once then fail LOUD.

    The engine's load-bearing input mouth (:meth:`Catalog.get_bars`) NEVER
    silently skips a corrupt partition: a dropped file would hole the
    ``data_snapshot_hash`` inputs and silently change run identity (Golden
    Rule #4).

    A **torn read** — a :func:`~brutex.data.errors.parquet_read_skip_errors`
    fault from a concurrent writer's ``os.replace`` straddling a multi-open
    ``pl.read_parquet`` (footer + column-chunk reads on fresh handles →
    transient ``ComputeError``), the ``dashboard/db_view.py`` torn-read-retry
    precedent (DEC-WINDOW-TORN-READ-RETRY-001) — is retried ONCE after a short
    sleep. A SECOND failure raises the typed :class:`CatalogError` naming the
    **UNREADABLE** partition (the same verdict vocabulary the lake census's
    ``lake.invariant.scan_skip`` / ``catalog.reconcile.parquet_skip`` warnings
    use), so a genuinely corrupt lake refuses the launch loudly rather than
    computing a snapshot hash over partial data.

    Parameters
    ----------
    path
        The partition Parquet file (already confirmed to exist by the caller).
    retry_sleep
        ``sleep(seconds)`` used between the tear and its single retry;
        defaults to :func:`time.sleep`. Injectable so tests never sleep.
    """
    import polars as pl

    from brutex.data.errors import parquet_read_skip_errors

    sleep = retry_sleep if retry_sleep is not None else time.sleep
    skip_errors = parquet_read_skip_errors()
    try:
        frame = pl.read_parquet(str(path))
    except skip_errors as first:
        # Torn read (concurrent writer). Retry ONCE — the writer's os.replace is
        # atomic, so a second open sees one consistent inode on a healthy lake.
        _LOG.warning("catalog.read.parquet_retry", path=str(path), error=str(first))
        sleep(_PARTITION_READ_RETRY_DELAY_S)
        try:
            frame = pl.read_parquet(str(path))
        except skip_errors as second:
            raise CatalogError(
                f"UNREADABLE partition file {path}: {second} — a corrupt partition "
                f"is NEVER silently skipped (it would hole the data_snapshot_hash "
                f"inputs, Golden Rule #4); quarantine/heal it "
                f"('brutex lake audit-unique') or re-pull the month, then re-run"
            ) from second
    _validate_partition_schema(frame, path)
    return frame


def _validate_partition_schema(frame: pl.DataFrame, path: Path) -> None:
    """Refuse a foreign / wrong-dtype partition frame as **UNREADABLE** (no row scan).

    An O(columns) schema introspection (never a row scan, never a cast): the
    canonical bar columns of :func:`_empty_bars_table` must be present, the four
    prices numeric, ``timestamp`` temporal, and ``volume`` integer. A violation
    raises :class:`CatalogError` naming the file + the offending columns, so a
    ``timestamp``-only foreign parquet (C3) or a ``close`` stored as ``Utf8``
    (C4) can never reach the Stage-1A condition masks or the snapshot hash.
    """
    schema = frame.schema
    missing = [col for col in _CANONICAL_BAR_COLUMNS if col not in schema]
    if missing:
        raise CatalogError(
            f"UNREADABLE partition file {path}: missing required bar column(s) "
            f"{missing} (foreign / old-schema parquet) — quarantine/heal it "
            f"('brutex lake audit-unique') or re-pull the month, then re-run"
        )
    bad_dtypes: list[str] = []
    for col in ("open", "high", "low", "close"):
        if not schema[col].is_numeric():
            bad_dtypes.append(f"{col}={schema[col]} (want numeric)")
    if not schema["timestamp"].is_temporal():
        bad_dtypes.append(f"timestamp={schema['timestamp']} (want temporal)")
    if not schema["volume"].is_integer():
        bad_dtypes.append(f"volume={schema['volume']} (want integer)")
    if bad_dtypes:
        raise CatalogError(
            f"UNREADABLE partition file {path}: wrong column dtype(s) "
            f"{bad_dtypes} — quarantine/heal it ('brutex lake audit-unique') or "
            f"re-pull the month, then re-run"
        )


def _empty_bars_table(pa_mod: Any) -> Any:
    """Return an empty ``pyarrow.Table`` matching the canonical bar schema.

    Centralised so both the empty-slice return path in
    :meth:`Catalog.get_bars` and any future "no partitions matched"
    caller emit the exact same schema object.
    """
    schema = pa_mod.schema(
        [
            pa_mod.field("timestamp", pa_mod.timestamp("us", tz="UTC")),
            pa_mod.field("open", pa_mod.float64()),
            pa_mod.field("high", pa_mod.float64()),
            pa_mod.field("low", pa_mod.float64()),
            pa_mod.field("close", pa_mod.float64()),
            pa_mod.field("volume", pa_mod.int64()),
            pa_mod.field("open_interest", pa_mod.int64()),
        ]
    )
    return pa_mod.table(
        {
            "timestamp": [],
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
            "open_interest": [],
        },
        schema=schema,
    )
