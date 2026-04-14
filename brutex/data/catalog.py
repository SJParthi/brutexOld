"""DuckDB-backed catalog for the BruteX data lake.

Implements ``FR-CAT-002`` (catalog row shape) and a minimal upsert
API. The full ``get_bars`` API with zero-copy Arrow (``FR-CAT-003``)
and the ``data_snapshot_hash`` rollup (``FR-CAT-004``) land in later
commits — this file lays the schema and the upsert pathway so the
ingest adapter can start populating the catalog immediately.

``duckdb`` is imported lazily so unit tests that touch only the
pure-Python pieces don't need the dep installed.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Iterator, Self

from brutex.data.errors import CatalogError
from brutex.data.models import CatalogEntry

if TYPE_CHECKING:  # pragma: no cover
    pass


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


class Catalog:
    """Thin DuckDB wrapper over the catalog partitions table.

    Use as a context manager::

        with Catalog.open(Path("lake/catalog.duckdb")) as cat:
            cat.upsert(entry)
            row = cat.get(exchange="NSE", segment="FNO", symbol="NSE-NIFTY",
                          interval="1minute", year=2024, month=1)
    """

    def __init__(self, *, conn: object, path: Path) -> None:
        self._conn = conn
        self._path = path
        self._closed = False

    @classmethod
    def open(cls, path: Path) -> Self:
        """Open or create the catalog DB at *path*.

        Raises
        ------
        CatalogError
            If duckdb is not installed or the DB cannot be opened.
        """
        try:
            import duckdb  # noqa: PLC0415 — lazy on purpose
        except ImportError as exc:
            raise CatalogError(
                "Catalog requires 'duckdb' — install with "
                "'pip install -e .[data]' to enable the P2 data lake"
            ) from exc

        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = duckdb.connect(str(path))
            conn.execute(_DDL)
        except Exception as exc:  # pragma: no cover — defensive
            raise CatalogError(f"failed to open catalog at {path}: {exc}") from exc
        return cls(conn=conn, path=path)

    @property
    def path(self) -> Path:
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

    def count(self) -> int:
        """Return the number of partition rows recorded in the catalog."""
        if self._closed:
            raise CatalogError("Catalog is closed")
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
def open_catalog(path: Path) -> Iterator[Catalog]:
    """Context-manager sugar for :meth:`Catalog.open`."""
    cat = Catalog.open(path)
    try:
        yield cat
    finally:
        cat.close()
