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
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.atomic_io import atomic_write_via, lake_write_guard
from brutex.data.errors import WriterError, parquet_read_skip_errors
from brutex.data.models import CandleBar, CatalogEntry
from brutex.data.partition import (
    build_partition_path,
    partition_for_timestamp,
)
from brutex.data.partition_lock import BoundedLockRegistry, partition_write_lock
from brutex.money import round_money

if TYPE_CHECKING:  # pragma: no cover
    pass


#: Structured logger — emits one ``lake.partition.write`` event per successful
#: partition write so the serial lake-write cost (the throughput-binding step
#: DOWNSTREAM of fetching) is MEASURABLE in the pull log
#: (DEC-LAKE-WRITE-INSTRUMENT-001). The prior 28-hour pull-log forensic proved
#: the pull is write/lake-bound (achieved ~2.8 r/s vs ~4.6 allowed, connections
#: idle 74% of the time) but could only INFER the serial-write cost — the
#: write/lake path emitted ZERO telemetry (0 append/latency events in 343,914
#: log lines), so the ``write_consumer`` (Lever 1 of DEC-FETCH-WRITE-OVERLAP)
#: could not be sized or gated honestly. This event makes that cost a measurable
#: fact. Cheap, always-on, O(1) (one ``info`` per PARTITION write, not per bar).
_LOG = structlog.get_logger("brutex.data.parquet_writer")


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
#: MUST be shared across instances that resolve to the same on-disk file. The
#: intern is CAPACITY-BOUNDED (:class:`~brutex.data.partition_lock.BoundedLockRegistry`,
#: LRU eviction of idle locks) so a full multi-year pull touching 10^5–10^6 distinct
#: month partitions can no longer accumulate a ``Lock`` object per partition forever
#: (GAP #19); an in-flight (refcounted) write keeps its lock, so correctness is
#: independent of the cap.
_PARTITION_LOCKS: BoundedLockRegistry = BoundedLockRegistry()


def _tick_round(bar: CandleBar) -> CandleBar:
    """Snap a bar's OHLC to the 2-decimal rupee tick grid (DEC-SPOT-PRICE-2DP-001).

    The vendor returns float-noise prices (``14020.7999``) but the real index / equity
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

    When the vendor returns a DIFFERENT OHLCV/OI for a timestamp ALREADY stored
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
    bars_added
        Number of incoming bars actually ADDED to the partition by THIS write —
        the DEDUPED count (DEC-SINK-DEDUP-COUNT-001). A fresh partition adds every
        incoming bar; a merge adds only the timestamps not already stored (a re-pull
        of bars already in the lake adds ``0``); a same-timestamp/different-value
        keep-first conflict adds ``0`` (the stored bar is kept). Distinct from
        :attr:`row_count` (the partition's total size after the write) and from the
        INPUT batch size — this is the honest "how many new bars did this write
        contribute" count the sink reports as ``bars_appended``.
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
    bars_added: int = 0
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
        merge_mode: str = "keep-first",
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
        merge_mode
            How a same-timestamp/different-value collision against the stored
            partition is resolved. ``"keep-first"`` (default) keeps the STORED
            bar and records a :class:`BarConflict` — the vendor-spine identity
            (DEC-LAKE-CONFLICT-KEEPFIRST-001). ``"latest-wins"`` OVERWRITES the
            stored bar with the incoming one (no conflict recorded) — used ONLY
            by the DERIVED ``1day`` partition so a later complete-session pull
            corrects a partial-session frozen daily bar
            (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 / FIX-2). Never use latest-wins on
            the authoritative 1-minute partition; its keep-first idempotence is
            unchanged.

        Returns
        -------
        WriteResult
            Describes the file that was created / verified.

        Raises
        ------
        WriterError
            If *bars* is empty, spans multiple partitions, or *merge_mode* is
            not a recognised value.

        Notes
        -----
        On a successful write this emits exactly one ``lake.partition.write``
        structlog event (DEC-LAKE-WRITE-INSTRUMENT-001) carrying ``append_ms``
        (the wall time of the serial read-modify-write op, lock-wait EXCLUDED),
        ``lock_wait_ms`` (the wall time spent acquiring the per-partition + the
        cross-process locks), ``rows`` (the bars in THIS append batch),
        ``partition_rows`` (the total rows physically in the partition after the
        write — the full month for a merge, the real disk-write-cost signal), and
        ``partition`` (the destination path). It makes the serial lake-write cost
        — the throughput-binding step the pull-log forensic could only INFER —
        a measurable fact, so the overlap ``write_consumer`` (Lever 1 of
        DEC-FETCH-WRITE-OVERLAP) can be sized + gated honestly. Cheap, always-on,
        O(1) (one ``info`` per partition write, NOT per bar).
        """
        if merge_mode not in ("keep-first", "latest-wins"):
            raise WriterError(
                f"unknown merge_mode {merge_mode!r}; expected 'keep-first' or 'latest-wins'"
            )
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
        # First-creating a BRAND-NEW partition directory on a full volume raises a raw
        # OSError(ENOSPC) (`exist_ok=True` keeps the re-pull no-op free). Wrap it in
        # `lake_write_guard()` so a disk-full here maps to a `LakeWriteHalt("disk_full")`
        # pull-wide stop (a member of both ingest spines' `_GLOBAL_STOP_ERRORS`), instead
        # of an un-mapped OSError that the spines re-raise as an "escaped code bug" and
        # crash the whole pull RAW. The option writer guards its equivalent `mkdir` the
        # same way (DEC-LAKE-WRITE-HALT-001 — "every allocation/failure site is guarded").
        with lake_write_guard():
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
        #
        # `partition_write_lock` extends that same serialisation ACROSS PROCESSES
        # (DEC-LAKE-PARTITION-FLOCK-001): the threading lock only guards threads in
        # THIS interpreter, so a concurrent CLI `brutex fetch` + a dashboard pull
        # sharing the lake would still lost-update the same month partition. The
        # advisory `flock` (held across the read-modify-write) makes the later
        # process read AFTER the earlier one's `os.replace`, so its union includes
        # the earlier bars. Per partition + same lock ORDER as the threading lock,
        # so no cross-partition deadlock.
        #
        # Both locks are entered via an ExitStack so the lock-acquire wall time
        # (`lock_wait_ms`) is timed SEPARATELY from the write op itself
        # (`append_ms`) — the two costs the pull-log forensic needs distinguished:
        # contention waiting on a busy partition vs the serial Parquet
        # read-modify-write cost (DEC-LAKE-WRITE-INSTRUMENT-001). Entering both
        # locks inside ONE `with` keeps the SAME lock ORDER + scope as before, so
        # the serialisation semantics are byte-unchanged — only the timing
        # bookkeeping is added.
        lock_acquire_start = time.perf_counter()
        with contextlib.ExitStack() as stack:
            stack.enter_context(_PARTITION_LOCKS.acquire(dest))
            stack.enter_context(partition_write_lock(self._lake_root, dest))
            lock_wait_ms = (time.perf_counter() - lock_acquire_start) * 1000.0

            write_start = time.perf_counter()
            if dest.exists():
                result = self._merge_write(dest, sorted_bars, merge_mode=merge_mode)
            else:
                content_bytes = self._render_bytes(sorted_bars)
                self._atomic_write(dest, content_bytes)
                result = WriteResult(
                    path=dest,
                    row_count=len(sorted_bars),
                    raw_hash=hashlib.sha256(content_bytes).hexdigest(),
                    first_ts=first_ts,
                    last_ts=last_ts,
                    bytes_written=len(content_bytes),
                    bars_added=len(sorted_bars),
                )
            append_ms = (time.perf_counter() - write_start) * 1000.0

        # Emit the per-partition write telemetry AFTER the locks are released (the
        # log call is not itself part of the serialised critical section). One
        # `info` per successful partition write — the throughput-binding cost the
        # pull-log forensic could only INFER, now a measurable fact.
        _LOG.info(
            "lake.partition.write",
            append_ms=round(append_ms, 3),
            lock_wait_ms=round(lock_wait_ms, 3),
            rows=len(sorted_bars),
            partition_rows=result.row_count,
            partition=str(dest),
        )
        return result

    def _merge_write(
        self, dest: Path, new_bars: list[CandleBar], *, merge_mode: str = "keep-first"
    ) -> WriteResult:
        """Union *new_bars* into the existing partition at *dest*, idempotently.

        Reads the stored bars, merges the incoming ones keyed by timestamp
        (O(1) per bar), and rewrites the full month only if the stored content
        changed. A timestamp already present with identical OHLCV is a silent
        no-op.

        A same-timestamp/different-value collision is resolved per *merge_mode*:

        * ``"keep-first"`` (default) — the STORED bar is kept (never
          overwritten, DEC-LAKE-CONFLICT-KEEPFIRST-001 / CLAUDE.md Golden Rule
          #7); the discrepancy is recorded as a :class:`BarConflict` and
          returned for logging, and the merge continues so the pull always
          finishes.
        * ``"latest-wins"`` — the INCOMING bar OVERWRITES the stored one (no
          conflict recorded). Used ONLY by the derived ``1day`` partition
          (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 / FIX-2) so a later
          complete-session re-derive corrects a partial-session frozen daily.
        """
        # Read the stored partition's RAW bytes ONCE (inside lake_write_guard via
        # `_read_existing_bytes`), then parse the bars from those same bytes. The raw
        # bytes give us the stored file's hash + size with NO second full read, so the
        # idempotent no-op path below reuses them (GAP #11 — the old code re-read the
        # whole month with `dest.read_bytes()` + `dest.stat()` OUTSIDE the write guard
        # purely to hash a file it had ALREADY loaded, an unguarded OOM site on a deep
        # partition; this matches the option writer's no-second-read no-op).
        existing_bytes = self._read_existing_bytes(dest)
        merged = self._read_existing_bars(dest, existing_bytes)
        added = 0
        rewrite = False
        conflicts: list[BarConflict] = []
        for bar in new_bars:
            prev = merged.get(bar.timestamp)
            if prev is None:
                merged[bar.timestamp] = bar
                added += 1
            elif not self._bar_values_equal(prev, bar):
                if merge_mode == "latest-wins":
                    # Overwrite the stored bar with the corrected re-derivation;
                    # no conflict recorded (an intentional latest-wins restate).
                    merged[bar.timestamp] = bar
                    rewrite = True
                else:
                    # Keep the STORED bar (keep-first — prev stays in `merged`).
                    # Record the discrepancy; do NOT increment `added`.
                    conflicts.append(BarConflict(timestamp=bar.timestamp, stored=prev, vendor=bar))

        merged_sorted = sorted(merged.values(), key=lambda b: b.timestamp)
        if added == 0 and not rewrite:
            # Every incoming bar is already stored identically or is a conflict
            # (kept-first) → no rewrite needed; the stored file is unchanged. Reuse the
            # already-read bytes for the hash/size — no second full read of the partition.
            return WriteResult(
                path=dest,
                row_count=len(merged_sorted),
                raw_hash=hashlib.sha256(existing_bytes).hexdigest(),
                first_ts=merged_sorted[0].timestamp,
                last_ts=merged_sorted[-1].timestamp,
                bytes_written=len(existing_bytes),
                bars_added=added,  # 0 on the no-op path (every incoming bar already stored)
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
            bars_added=added,
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
    def _read_existing_bytes(dest: Path) -> bytes:
        """Read the stored partition's raw Parquet bytes once (inside the write guard).

        The merge READ is the steady-state re-pull's largest allocation (it loads
        the WHOLE existing month partition), so it runs inside ``lake_write_guard`` —
        a ``MemoryError`` here (or an ENOSPC, defensively) becomes a ``LakeWriteHalt``
        pull-wide stop, not a raw mid-merge crash (DEC-LAKE-WRITE-HALT-001). Returning
        the raw bytes lets the no-op merge path reuse them for the hash/size with NO
        second full read of the partition (GAP #11).

        GAP #1 (resilience): a non-ENOSPC ``OSError`` (e.g. the partition file vanished
        / an I/O fault mid-pull) is a per-symbol problem, NOT a pull-wide one, so it is
        re-raised as a QUARANTINABLE :class:`~brutex.data.errors.WriterError` naming the
        path — the spine quarantines that one window and the pull proceeds. ENOSPC /
        ``MemoryError`` are still mapped to ``LakeWriteHalt`` by the guard FIRST (a
        ``LakeWriteHalt`` is a ``DataLakeError``, not an ``OSError``, so it sails past
        the ``except OSError`` untouched and halts the pull).
        """
        try:
            with lake_write_guard():
                return dest.read_bytes()
        except OSError as exc:
            raise WriterError(f"unreadable existing partition {dest}: {exc}") from exc

    @staticmethod
    def _read_existing_bars(dest: Path, existing_bytes: bytes) -> dict[datetime, CandleBar]:
        """Parse a stored partition's *existing_bytes* into ``{timestamp: CandleBar}``.

        Decimals are reconstructed via ``Decimal(str(float))`` and then snapped
        to the 2dp tick grid (``_tick_round``) — so a stored bar compares equal
        to a freshly-rounded incoming bar even if the partition predates
        DEC-SPOT-PRICE-2DP-001 (any 4-decimal remnant rounds to the same 2dp),
        keeping re-pulls idempotent and converging the lake to clean 2dp.

        Resilience (GAP #1): the existing-partition parse is wrapped in a narrow
        ``try`` over :func:`~brutex.data.errors.parquet_read_skip_errors` (a
        ``polars.exceptions.PolarsError`` on a corrupt / version-incompatible file, or
        an ``OSError`` fault). A CORRUPT existing partition therefore raises a
        QUARANTINABLE :class:`~brutex.data.errors.WriterError` naming the bad path
        instead of crashing the whole pull — the ingest spine quarantines that one
        symbol's window and proceeds; every other symbol still pulls. ``MemoryError`` /
        ENOSPC stay mapped to ``LakeWriteHalt`` by the guard around the bytes read
        (a pull-wide stop, never demoted to a per-symbol quarantine).

        Performance (DEC-WRITER-COLUMNAR-4A-001): the reconstruction extracts each
        column ONCE via :meth:`polars.Series.to_list` and zips the per-row scalars,
        rather than ``df.iter_rows(named=True)`` (one Python dict allocated per row).
        ``to_list`` yields the SAME Python scalar types per cell — ``float`` for the
        OHLC columns, the same ``datetime`` (UTC ``ZoneInfo``) key, ``int`` /
        ``None`` for volume / open_interest — so the reconstructed
        ``Decimal(str(float))`` value, the ``_tick_round`` snap, and the resulting
        ``{timestamp: CandleBar}`` mapping are BYTE-IDENTICAL to the per-row path
        (idempotence preserved, CLAUDE.md Golden Rule #7). This is the
        byte-preserving cut of the GIL-bound merge READ — the per-row dict build is
        gone; the Decimal reconstruction itself is unchanged (that float→Decimal math
        is the byte contract — a switch to float math is explicitly out of scope here).
        """
        try:
            import polars as pl
        except ImportError as exc:  # pragma: no cover — same guard as _render_bytes
            raise WriterError(
                "ParquetWriter requires 'polars' — install with "
                "'pip install -e .[data]' to enable the P2 data lake"
            ) from exc

        import io

        # The parse is the steady-state re-pull's largest allocation, so it stays inside
        # `lake_write_guard`: a `MemoryError` here becomes a `LakeWriteHalt` pull-wide
        # stop (DEC-LAKE-WRITE-HALT-001, unchanged). NESTED inside it, a corrupt-file
        # `PolarsError` / non-ENOSPC `OSError` (GAP #1) is caught FIRST and re-raised as a
        # quarantinable `WriterError` naming the path — `MemoryError` is NOT in the skip
        # set, so an OOM still reaches the guard and halts (never demoted to a quarantine).
        with lake_write_guard():
            try:
                df = pl.read_parquet(io.BytesIO(existing_bytes))
            except parquet_read_skip_errors() as exc:
                raise WriterError(f"corrupt existing partition {dest}: {exc}") from exc
        # COLUMN-wise extraction (one Series.to_list per column) instead of a
        # per-row dict — the byte-preserving speed cut (DEC-WRITER-COLUMNAR-4A-001).
        timestamps = df["timestamp"].to_list()
        opens = df["open"].to_list()
        highs = df["high"].to_list()
        lows = df["low"].to_list()
        closes = df["close"].to_list()
        volumes = df["volume"].to_list()
        ois = df["open_interest"].to_list()
        out: dict[datetime, CandleBar] = {}
        for ts, op, hi, lo, cl, vol, oi in zip(
            timestamps, opens, highs, lows, closes, volumes, ois, strict=True
        ):
            bar = _tick_round(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(op)),
                    high=Decimal(str(hi)),
                    low=Decimal(str(lo)),
                    close=Decimal(str(cl)),
                    volume=int(vol),
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

        # Build columns from the bars + render to a Parquet byte blob. The polars
        # allocations (the DataFrame build + write_parquet) are the FRESH-write OOM
        # site (the merge READ in `_read_existing_bars` is guarded separately), so
        # they run inside lake_write_guard: a MemoryError here becomes a LakeWriteHalt
        # pull-wide stop instead of a raw mid-render crash (DEC-LAKE-WRITE-HALT-001).
        # Decimal → float conversion is explicit so the cast is visible in audit logs.
        import io

        with lake_write_guard():
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
        """Atomically write *content* to *dest* via the shared atomic_io helper.

        Delegates to :func:`~brutex.data.atomic_io.atomic_write_via` (CLAUDE.md §11
        COMMON — one tmp→fsync→``os.replace``→cleanup implementation), which also
        maps a full disk (``ENOSPC``) to a :class:`~brutex.data.errors.LakeWriteHalt`
        pull-wide stop instead of a raw ``OSError`` (DEC-LAKE-WRITE-HALT-001).
        Prevents a half-written parquet file being left on disk on a kill/error.
        """
        atomic_write_via(dest, lambda tmp: tmp.write_bytes(content))
