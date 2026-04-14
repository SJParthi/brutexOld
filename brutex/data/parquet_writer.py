"""Parquet writer for canonical OHLCV bars.

Uses ``polars`` under the hood (per ADR-0012: no pandas in hot paths).
``polars`` is imported **lazily** inside :meth:`ParquetWriter.write`
so that unit tests and CLI code that never touch the writer can run
without installing the P2 data-lake dependency group.

## Idempotence (NFR-DET-001, NFR-REL-001)

Re-writing the same ``(symbol, interval, year, month)`` partition with
identical bar content produces a byte-for-byte identical file. We
enforce this by:

1. Sorting bars by timestamp before writing (same input → same order).
2. Using zstd compression with level 3 (deterministic for identical
   byte streams under any supported zstd version).
3. Disabling polars' random row-group ID (``use_pyarrow=False`` and
   ``row_group_size`` fixed).
4. Computing ``sha256`` of the written bytes and returning it in the
   result so callers can record the raw_hash in the catalog.

If the destination already exists and its content hash differs from
the hash of the new file, we raise :class:`WriterError`. Callers
must first delete the old file or resolve the conflict explicitly.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from brutex.data.errors import WriterError
from brutex.data.models import CandleBar, CatalogEntry
from brutex.data.partition import (
    build_partition_path,
    partition_for_timestamp,
)

if TYPE_CHECKING:  # pragma: no cover
    pass


#: Fixed row-group size for parquet writes. Small enough to stream
#: a single month in one group, large enough to amortise page overhead.
ROW_GROUP_SIZE: Final[int] = 100_000

#: zstd compression level — deterministic across zstd >= 1.5.
ZSTD_LEVEL: Final[int] = 3


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Outcome of a successful :meth:`ParquetWriter.write` call.

    Attributes
    ----------
    path
        The partition path the bars were written to.
    row_count
        Number of bars in the file.
    raw_hash
        sha256 hex digest of the file bytes (64 chars).
    first_ts, last_ts
        Timestamp bounds of the bars written.
    bytes_written
        Size of the final Parquet file in bytes.
    """

    path: Path
    row_count: int
    raw_hash: str
    first_ts: datetime
    last_ts: datetime
    bytes_written: int


class ParquetWriter:
    """Writes canonical OHLCV bars to the partition layout from FR-CAT-001.

    One writer instance is cheap — it holds no open handles. You can
    instantiate one per ingest worker or one per process; both work.

    Parameters
    ----------
    lake_root
        Top-level data-lake directory. Must be a :class:`pathlib.Path`.
        The directory is created on first write.
    """

    def __init__(self, *, lake_root: Path) -> None:
        self._lake_root = lake_root

    @property
    def lake_root(self) -> Path:
        return self._lake_root

    def write(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: list[CandleBar],
    ) -> WriteResult:
        """Write *bars* to their canonical partition file.

        All bars must belong to the same ``(year, month)`` partition —
        the writer does not split across partitions. The caller is
        responsible for grouping bars by partition first (see
        :func:`partition_for_timestamp`).

        Parameters
        ----------
        exchange, segment, symbol, interval
            Partition coordinates.
        bars
            Non-empty list of :class:`CandleBar` instances to write.

        Returns
        -------
        WriteResult
            Describes the file that was created / verified.

        Raises
        ------
        WriterError
            If *bars* is empty, spans multiple partitions, or the
            destination exists with conflicting content.
        """
        if not bars:
            raise WriterError(
                "ParquetWriter.write called with an empty bars list"
            )

        # Group by (year, month) — must all be in one partition.
        partitions = {partition_for_timestamp(b.timestamp) for b in bars}
        if len(partitions) != 1:
            raise WriterError(
                f"bars span multiple partitions {sorted(partitions)}; "
                f"caller must group by (year, month) before writing"
            )
        (year, month) = next(iter(partitions))

        sorted_bars = sorted(bars, key=lambda b: b.timestamp)
        first_ts = sorted_bars[0].timestamp
        last_ts = sorted_bars[-1].timestamp

        dest = build_partition_path(
            lake_root=self._lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            year=year,
            month=month,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)

        content_bytes = self._render_bytes(sorted_bars)
        new_hash = hashlib.sha256(content_bytes).hexdigest()

        if dest.exists():
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
            if existing_hash != new_hash:
                raise WriterError(
                    f"partition {dest} exists with different content "
                    f"(existing_hash={existing_hash}, new_hash={new_hash}); "
                    f"re-ingesting the same partition with different bars "
                    f"is not allowed — resolve the conflict and retry"
                )
            # Idempotent rewrite — bytes match. No-op.
        else:
            self._atomic_write(dest, content_bytes)

        return WriteResult(
            path=dest,
            row_count=len(sorted_bars),
            raw_hash=new_hash,
            first_ts=first_ts,
            last_ts=last_ts,
            bytes_written=len(content_bytes),
        )

    def make_catalog_entry(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        result: WriteResult,
    ) -> CatalogEntry:
        """Project a :class:`WriteResult` into a :class:`CatalogEntry`.

        Convenience helper so the ingest adapter doesn't have to repeat
        the same field-wiring in every call site.
        """
        year, month = partition_for_timestamp(result.first_ts)
        return CatalogEntry(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            year=year,
            month=month,
            raw_hash=result.raw_hash,
            row_count=result.row_count,
            first_ts=result.first_ts,
            last_ts=result.last_ts,
            fetched_at=datetime.now(UTC),
        )

    # ── Private ────────────────────────────────────────────────────

    @staticmethod
    def _render_bytes(sorted_bars: list[CandleBar]) -> bytes:
        """Serialise *sorted_bars* to a Parquet byte blob.

        ``polars`` is imported lazily so callers that never write don't
        need the dep. Raises :class:`WriterError` with a clear message
        if the import fails.
        """
        try:
            import polars as pl  # noqa: PLC0415 — lazy on purpose
        except ImportError as exc:
            raise WriterError(
                "ParquetWriter requires 'polars' — install with "
                "'pip install -e .[data]' to enable the P2 data lake"
            ) from exc

        # Build columns from the bars. Keep Decimal → float conversion
        # explicit so the cast is visible in audit logs.
        rows = {
            "timestamp": [b.timestamp for b in sorted_bars],
            "open": [float(b.open) for b in sorted_bars],
            "high": [float(b.high) for b in sorted_bars],
            "low": [float(b.low) for b in sorted_bars],
            "close": [float(b.close) for b in sorted_bars],
            "volume": [b.volume for b in sorted_bars],
            "open_interest": [b.open_interest for b in sorted_bars],
        }
        df = pl.DataFrame(
            rows,
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "open_interest": pl.Int64,
            },
        )

        buffer: list[bytes] = []

        # polars write_parquet → file-like. Use an in-memory BytesIO so
        # we can compute the hash before touching the filesystem.
        import io  # noqa: PLC0415

        bio = io.BytesIO()
        df.write_parquet(
            bio,
            compression="zstd",
            compression_level=ZSTD_LEVEL,
            row_group_size=ROW_GROUP_SIZE,
            use_pyarrow=False,
            statistics=False,  # stats are non-deterministic; skip for reproducibility
        )
        buffer.append(bio.getvalue())
        return b"".join(buffer)

    @staticmethod
    def _atomic_write(dest: Path, content: bytes) -> None:
        """Atomically write *content* to *dest* via tempfile + rename.

        Prevents a half-written parquet file being left on disk if the
        process is killed mid-write. Uses the same directory so the
        rename is always on one filesystem.
        """
        fd, tmp_path = tempfile.mkstemp(
            dir=str(dest.parent), prefix=".brutex-write-", suffix=".parquet.tmp"
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)
        except Exception:
            # Clean up on any error path.
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise
