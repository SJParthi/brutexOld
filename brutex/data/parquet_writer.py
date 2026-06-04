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

## Merge-on-write (DEC-LAKE-MERGE-WRITE-001)

A fetch chunk (30/90 days) straddles month boundaries, so one month
partition is written by several chunks, each carrying only part of the
month. Rather than reject a partial re-write as a conflict, :meth:`write`
UNIONS the incoming bars into the existing partition keyed by timestamp
(O(1) dedup per bar): a new timestamp is appended, an identical one is a
no-op, and a SAME timestamp with DIFFERENT OHLCV is recorded as a
:class:`BarConflict` (keep-first — the STORED bar wins, never overwritten)
and returned in :attr:`WriteResult.conflicts` so the caller can log and
continue (DEC-LAKE-CONFLICT-KEEPFIRST-001). Incremental month-filling and
full re-pulls are therefore idempotent — the completed partition is
byte-stable across runs.

## Per-partition write serialisation (DEC-DHAN-PARALLEL-WINDOWS-001)

Merge-on-write is a read-modify-write (read the stored month → union the
incoming bars → atomic rename), so two threads writing the **same**
``(symbol, interval, year, month)`` partition at once would race a
lost-update: both read the old file, each unions only its own bars, and the
later ``os.replace`` wins — silently dropping the other's bars. This bites
the moment the per-window spot fetch is parallelised, because adjacent
90-day windows straddle a month boundary, so two windows of ONE series share
a month partition (e.g. windows ``[Apr 1, Jun 30)`` and ``[Jun 30, Sep 28)``
both touch June). A :class:`threading.Lock` **per destination path** (interned
in :data:`_PARTITION_LOCKS` under a meta-lock) serialises only the writers
that target the SAME file; writers to DIFFERENT partitions never contend, so
the fan-out keeps its parallelism. The merge itself is already order-
independent (it unions by timestamp), so serialising the two writers yields
the same bytes regardless of which window lands first — completion order
never affects the stored data (CLAUDE.md §2 Rule 7).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

from brutex.data.errors import WriterError
from brutex.data.models import CandleBar, CatalogEntry
from brutex.data.partition import (
    build_partition_path,
    partition_for_timestamp,
)
from brutex.money import round_money

if TYPE_CHECKING:  # pragma: no cover
    pass


#: Fixed row-group size for parquet writes. Small enough to stream
#: a single month in one group, large enough to amortise page overhead.
ROW_GROUP_SIZE: Final[int] = 100_000

#: zstd compression level — deterministic across zstd >= 1.5.
ZSTD_LEVEL: Final[int] = 3

#: Process-wide registry of per-partition write locks (DEC-DHAN-PARALLEL-WINDOWS-001).
#: Keyed by the resolved destination ``Path`` so two threads writing the SAME
#: ``(symbol, interval, year, month)`` file serialise their read-modify-write
#: merge, while writers to DIFFERENT partitions never contend. Interned lazily so
#: the map only ever holds locks for partitions actually written. It is keyed by
#: PATH (not writer instance) on purpose: the production fan-out may build one
#: :class:`ParquetWriter` per worker (the docstring blesses that), so the lock
#: MUST be shared across instances that resolve to the same on-disk file.
_PARTITION_LOCKS: dict[Path, threading.Lock] = {}

#: Guards :data:`_PARTITION_LOCKS` itself — held only for the O(1)
#: get-or-create of a path's lock, never across a write.
_PARTITION_LOCKS_GUARD: threading.Lock = threading.Lock()


def _partition_lock(dest: Path) -> threading.Lock:
    """Return the shared write lock for partition *dest* (interned, O(1)).

    The first caller for a given path creates the lock under
    :data:`_PARTITION_LOCKS_GUARD`; every later caller (any thread, any
    :class:`ParquetWriter` instance) gets the SAME lock object, so concurrent
    merge-writes to one partition file are serialised. The guard is held only
    for the dict get-or-create — never across the write itself — so distinct
    partitions are looked up without contention.
    """
    with _PARTITION_LOCKS_GUARD:
        lock = _PARTITION_LOCKS.get(dest)
        if lock is None:
            lock = threading.Lock()
            _PARTITION_LOCKS[dest] = lock
        return lock


def _tick_round(bar: CandleBar) -> CandleBar:
    """Snap a bar's OHLC to the 2-decimal rupee tick grid (DEC-SPOT-PRICE-2DP-001).

    Dhan returns float-noise prices (``14020.7999``) but the real index / equity
    tick is 2dp (``14020.80``). The spot lake rounds at the write boundary with
    the SAME ``round_money`` (``ROUND_HALF_UP``, never truncation) the option
    writer already uses — so the spot and option lakes match and ``/db`` shows
    clean 2dp everywhere. Applied to BOTH incoming and stored-read bars, so a
    re-pull compares grid-vs-grid and stays bit-idempotent. Rounding is monotone,
    so ``low <= open, close <= high`` is preserved (no ``CandleBar`` violation).
    Volume / OI / timestamp are untouched.
    """
    return CandleBar(
        timestamp=bar.timestamp,
        open=round_money(bar.open),
        high=round_money(bar.high),
        low=round_money(bar.low),
        close=round_money(bar.close),
        volume=bar.volume,
        open_interest=bar.open_interest,
    )


@dataclass(frozen=True, slots=True)
class BarConflict:
    """A single vendor re-statement detected during merge-on-write.

    When Dhan returns a DIFFERENT OHLCV/OI for a timestamp ALREADY stored
    in the lake the writer records the discrepancy here rather than raising
    (DEC-LAKE-CONFLICT-KEEPFIRST-001). The STORED bar always wins
    (keep-first — never overwritten, preserving reproducibility per
    CLAUDE.md Golden Rule #7). The caller logs the conflict and continues
    the pull, then reports exactly what the vendor changed.

    Parameters
    ----------
    timestamp
        The UTC timestamp of the conflicting bar.
    stored
        The bar currently in the lake (the authoritative value —
        keep-first means this is never replaced).
    vendor
        The bar the vendor just returned (the re-statement that
        was silently discarded after logging).
    """

    timestamp: datetime
    stored: CandleBar
    vendor: CandleBar


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
    conflicts
        Vendor re-statements detected during this write: timestamps whose
        stored bar differed from the incoming bar (DEC-LAKE-CONFLICT-KEEPFIRST-001).
        The stored bar was kept (keep-first, never overwritten). Empty tuple
        for a first write or a clean re-pull with no value changes.
    """

    path: Path
    row_count: int
    raw_hash: str
    first_ts: datetime
    last_ts: datetime
    bytes_written: int
    conflicts: tuple[BarConflict, ...] = ()


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
        """Return the configured top-level lake directory path."""
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
            destination already stores the SAME timestamp with DIFFERENT
            OHLCV (a true data conflict — see merge-on-write above).
        """
        if not bars:
            raise WriterError("ParquetWriter.write called with an empty bars list")

        # Snap every incoming bar to the 2dp tick grid first, so the lake stores
        # clean 2dp prices AND a re-pull compares grid-vs-grid (idempotent — no
        # false conflict against already-rounded stored bars). DEC-SPOT-PRICE-2DP-001.
        bars = [_tick_round(b) for b in bars]

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

        # MERGE-on-write: a 30/90-day fetch chunk straddles month boundaries,
        # so a single month partition is touched by more than one chunk — each
        # carrying only PART of the month. The old "byte-hash must match or
        # raise" rule rejected every such partial write as a conflict (the
        # BAD_REQUEST flood on re-pull). Instead, UNION the incoming bars into
        # the existing partition keyed by timestamp (O(1) dict dedup per bar):
        # a never-seen timestamp is added, an identical one is a no-op, and only
        # a SAME timestamp with DIFFERENT OHLCV is a true conflict (real vendor
        # drift) → raise. This makes incremental month-filling + re-pulls
        # idempotent (DEC-LAKE-MERGE-WRITE-001).
        #
        # The existence-check + merge/create below is a read-modify-write, so it
        # runs under this partition's shared lock (DEC-DHAN-PARALLEL-WINDOWS-001):
        # when the per-window spot fetch is parallelised, two adjacent 90-day
        # windows of ONE series share a month partition (they straddle a month
        # boundary), and without serialisation the later os.replace would drop
        # the other window's bars (lost update). The lock is PER PATH, so writers
        # to different partitions never contend — the fan-out keeps its
        # parallelism; only same-file writers queue. The merge unions by
        # timestamp, so the stored bytes are identical regardless of which window
        # acquires the lock first (order-independent, Rule #7).
        with _partition_lock(dest):
            if dest.exists():
                return self._merge_write(dest, sorted_bars)

            content_bytes = self._render_bytes(sorted_bars)
            self._atomic_write(dest, content_bytes)
            return WriteResult(
                path=dest,
                row_count=len(sorted_bars),
                raw_hash=hashlib.sha256(content_bytes).hexdigest(),
                first_ts=first_ts,
                last_ts=last_ts,
                bytes_written=len(content_bytes),
            )

    def _merge_write(self, dest: Path, new_bars: list[CandleBar]) -> WriteResult:
        """Union *new_bars* into the existing partition at *dest*, idempotently.

        Reads the stored bars, merges the incoming ones keyed by timestamp
        (O(1) per bar), and rewrites the full month only if at least one new
        timestamp was added. A timestamp already present with identical OHLCV
        is a silent no-op; a same-timestamp/different-value bar records a
        :class:`BarConflict` — the STORED bar is kept (keep-first, never
        overwritten, DEC-LAKE-CONFLICT-KEEPFIRST-001 / CLAUDE.md Golden Rule
        #7), the conflict is collected and returned to the caller for logging,
        and the merge continues so the pull always finishes.
        """
        merged = self._read_existing_bars(dest)
        added = 0
        conflicts: list[BarConflict] = []
        for bar in new_bars:
            prev = merged.get(bar.timestamp)
            if prev is None:
                merged[bar.timestamp] = bar
                added += 1
            elif not self._bar_values_equal(prev, bar):
                # Keep the STORED bar (keep-first — prev stays in `merged`).
                # Record the discrepancy; do NOT increment `added`.
                conflicts.append(BarConflict(timestamp=bar.timestamp, stored=prev, vendor=bar))

        merged_sorted = sorted(merged.values(), key=lambda b: b.timestamp)
        if added == 0:
            # Every incoming bar is already stored identically or is a conflict
            # (kept-first) → no rewrite needed; the stored file is unchanged.
            return WriteResult(
                path=dest,
                row_count=len(merged_sorted),
                raw_hash=hashlib.sha256(dest.read_bytes()).hexdigest(),
                first_ts=merged_sorted[0].timestamp,
                last_ts=merged_sorted[-1].timestamp,
                bytes_written=dest.stat().st_size,
                conflicts=tuple(conflicts),
            )

        content_bytes = self._render_bytes(merged_sorted)
        self._atomic_write(dest, content_bytes)
        return WriteResult(
            path=dest,
            row_count=len(merged_sorted),
            raw_hash=hashlib.sha256(content_bytes).hexdigest(),
            first_ts=merged_sorted[0].timestamp,
            last_ts=merged_sorted[-1].timestamp,
            bytes_written=len(content_bytes),
            conflicts=tuple(conflicts),
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
    def _read_existing_bars(dest: Path) -> dict[datetime, CandleBar]:
        """Read a stored partition back into ``{timestamp: CandleBar}``.

        Decimals are reconstructed via ``Decimal(str(float))`` and then snapped
        to the 2dp tick grid (``_tick_round``) — so a stored bar compares equal
        to a freshly-rounded incoming bar even if the partition predates
        DEC-SPOT-PRICE-2DP-001 (any 4-decimal remnant rounds to the same 2dp),
        keeping re-pulls idempotent and converging the lake to clean 2dp.
        """
        try:
            import polars as pl
        except ImportError as exc:  # pragma: no cover — same guard as _render_bytes
            raise WriterError(
                "ParquetWriter requires 'polars' — install with "
                "'pip install -e .[data]' to enable the P2 data lake"
            ) from exc

        df = pl.read_parquet(dest)
        out: dict[datetime, CandleBar] = {}
        for row in df.iter_rows(named=True):
            oi = row["open_interest"]
            bar = _tick_round(
                CandleBar(
                    timestamp=row["timestamp"],
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["volume"]),
                    open_interest=None if oi is None else int(oi),
                )
            )
            out[bar.timestamp] = bar
        return out

    @staticmethod
    def _bar_values_equal(a: CandleBar, b: CandleBar) -> bool:
        """True if two bars carry identical OHLCV+OI (the timestamp is the key).

        Decimal ``==`` is numeric, so ``Decimal("12187.30") == Decimal("12187.3")``
        — a reconstructed price equals the parser's value with/without trailing
        zeros.
        """
        return (
            a.open == b.open
            and a.high == b.high
            and a.low == b.low
            and a.close == b.close
            and a.volume == b.volume
            and a.open_interest == b.open_interest
        )

    @staticmethod
    def _render_bytes(sorted_bars: list[CandleBar]) -> bytes:
        """Serialise *sorted_bars* to a Parquet byte blob.

        ``polars`` is imported lazily so callers that never write don't
        need the dep. Raises :class:`WriterError` with a clear message
        if the import fails.
        """
        try:
            import polars as pl
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
        import io

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
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)
            raise
