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
byte-stable across runs. A WITHIN-BATCH duplicate timestamp (a corrupt
vendor payload repeating a minute inside ONE response) rides the SAME
per-``merge_mode`` rule on BOTH branches: the merge branch collapses it via
its timestamp-keyed dict, and the FRESH-partition branch de-duplicates the
batch before rendering (:meth:`ParquetWriter._dedup_batch`) — so a first
write can never land two rows for one timestamp
(DEC-FRESH-WRITE-BATCH-DEDUP-001).

## Write-side uniqueness invariant (DEC-LAKE-WRITE-UNIQUE-001)

A vendor payload can physically carry DUPLICATE same-timestamp rows (measured:
135 duplicate 1-minute rows in one ADANIENT January window, reproduced
deterministically across two pulls). The merge branch always deduped against
the STORED bars, and the fresh branch now dedups the batch per merge_mode
(:meth:`ParquetWriter._dedup_batch`, DEC-FRESH-WRITE-BATCH-DEDUP-001 above) —
but a lake POISONED before that fix never self-healed (the ``added == 0``
merge path returned the poisoned bytes forever), and every collapse was
SILENT. This layer completes the invariant:

1. **Observability** — the duplicate collapse is COUNTED per write batch and
   reported as one aggregated ``lake.vendor_duplicate_rows`` WARNING
   (symbol/interval/window/count) on BOTH branches — what the vendor sent is
   never silently normalised away (Golden Rule #1).
2. **Merge self-heal** — :meth:`ParquetWriter._merge_write` compares the
   stored file's PHYSICAL row count (Parquet footer, O(footer)) against its
   unique-timestamp count and forces a deduped rewrite even when the merge
   itself added nothing (``lake.partition.dedup_healed``), so an
   already-poisoned closed-month file heals on the next touch.
3. **Keep-first read collapse** — :meth:`ParquetWriter._read_existing_bars`
   collapses stored duplicates keep-FIRST (the first physical row wins,
   honouring DEC-LAKE-CONFLICT-KEEPFIRST-001) and logs
   ``lake.partition.duplicate_collapsed`` (previously a silent last-wins).
4. **Tripwire** (:meth:`ParquetWriter._assert_strictly_increasing`) — after
   dedup, timestamps must be STRICTLY increasing before render; a violation
   raises a loud :class:`~brutex.data.errors.WriterError` naming the
   partition (impossible post-dedup — a genuine invariant assert).

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
import itertools
import os
import time
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_via, lake_write_guard
from brutex.data.errors import WriterError, parquet_read_skip_errors
from brutex.data.lake_manifest import invalidate_manifest, record_dataset
from brutex.data.lake_stats_manifest import (
    KIND_BARS,
    partition_stats_tracked,
    record_partition_stats,
    remove_partition_stats,
    stats_manifest_enabled,
)
from brutex.data.models import GREEKS_OVERLAY_FIELDS, CandleBar, CatalogEntry, FnoGreeksBar
from brutex.data.partition import (
    BAR_DIR_NAME,
    PARTITION_PATH_COMPONENTS,
    build_partition_path,
    partition_for_timestamp,
)
from brutex.data.partition_lock import BoundedLockRegistry, partition_write_lock
from brutex.money import round_money

if TYPE_CHECKING:  # pragma: no cover
    import polars as pl


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
#: LEVEL is per-writer (DEC-PREBUILD-LOG-FLOOD-001): the LIVE-PULL writers keep
#: the INFO default (rare + valuable — one vendor window per write), while the
#: pipeline PREBUILD's derived-TF writers construct with
#: ``partition_log_level="debug"`` — the prebuild materialises ~66k contracts ×
#: 20 TFs of tiny derived partitions, and at INFO those 205,609 lines (~132/s)
#: rolled the 10 MiB × 5 rotation over in ~26 min, WIPING the 15.7 h fetch
#: phase's rate/429/window-settle audit trail (measured, pull 2026-07-04/05
#: pull_id 22fc7739f719 — an undocumented DEC-LAKE-WRITE-INSTRUMENT-001 ×
#: DEC-PIPELINE-AUTOPREBUILD-001 interaction). The telemetry is NOT dropped:
#: the fields survive in aggregate via the per-symbol
#: ``lake.derived.write_summary`` INFO (timeframe_materialize) and per-partition
#: at DEBUG for a deliberately-verbose session.
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


#: Price column names snapped to the 2dp tick grid on the merge READ
#: (DEC-LAKE-WRITE-VECTORIZE-001). The OHLC fields are the only Decimal-rupee
#: columns; volume / open_interest / timestamp are untouched.
_PRICE_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")


#: The intermediate Decimal scale used to parse the float's ``str()`` repr LOSSLESSLY
#: (DEC-LAKE-WRITE-VECTORIZE-001). A Python ``str(float)`` carries at most 17 SIGNIFICANT
#: digits, so 17 FRACTIONAL digits can never truncate the repr — whatever the integer
#: part. With ``precision=38`` that leaves room for up to 21 integer digits (≫ any real
#: price), and the cast is byte-exact equal to ``Decimal(str(x))`` across the realistic
#: price range. A *smaller* scale (the original ``scale=10``) silently ROUNDS an 11-17
#: digit repr to 10 places BEFORE the half-up step — a double-rounding the legacy
#: ``Decimal(str(x))`` never performs — which flips the 2dp answer for long-tail floats
#: near a ``.xx5`` boundary (e.g. ``0.00499999999`` → wrong ``0.01`` vs legacy ``0.00``).
#: That is exactly the class of value a legacy raw-float partition carries, so the full
#: scale is load-bearing, not cosmetic. A value so large that ``x * 100`` exhausts the
#: precision-38 headroom (``|x| ≳ 1e19`` — empirically the cast overflows between 1e18 and
#: 1e19, still ~10^15x above any tradable price) raises a polars error, caught by the
#: per-row fallback in :meth:`ParquetWriter._read_existing_bars` (correctness over speed).
_SNAP_PARSE_SCALE: Final[int] = 17


def _snap_price_expr(column: str) -> pl.Expr:
    """Return a polars expression that snaps *column* to the 2dp tick grid, byte-exact.

    This is the COLUMNAR, GIL-releasing replacement (DEC-LAKE-WRITE-VECTORIZE-001)
    for the per-row ``round_money(Decimal(str(float)))`` reconstruction. It reproduces
    the EXACT byte contract of :func:`~brutex.money.round_money` applied to
    ``Decimal(str(float))`` — ``ROUND_HALF_UP`` (away from zero), never truncation
    (DEC-MONEY-2DP-001 / DEC-SPOT-PRICE-2DP-001) — entirely inside the polars C engine:

    1. ``cast(String)`` materialises the float's shortest round-trip decimal repr,
       which carries the same NUMERIC VALUE Python's ``str(float)`` yields for the
       realistic price range — the seed the legacy ``Decimal(str(op))`` parsed.
       (The two are not always the same DIGIT STRING — polars emits ``"0.00001"``
       where Python emits ``"1e-05"`` — but ``Decimal("0.00001") == Decimal("1e-05")``,
       so the parsed value is identical; no realistic price is in scientific notation
       anyway.) It is then cast to ``Decimal(scale=_SNAP_PARSE_SCALE)`` — a scale
       (17) wide enough to hold EVERY fractional digit of a ``str(float)`` (≤ 17
       significant digits) WITHOUT truncation, so the parsed value equals
       ``Decimal(str(op))`` EXACTLY with no binary-float drift AND no intermediate
       rounding (a direct ``Float64 -> Decimal`` cast would carry the binary
       representation, e.g. ``100.005`` -> ``100.00499…`` -> the WRONG half-up answer;
       and a too-SMALL scale would round an 11-17-digit repr like ``0.00499999999``
       up to ``0.005`` BEFORE the half-up step → the WRONG ``0.01`` — the ``str``
       round-trip at FULL scale is what keeps the half boundary on the legacy side).
    2. Half-away-from-zero to 2dp via integer arithmetic on the Decimal column:
       ``floor(d*100 + 0.5)/100`` for ``d >= 0`` and ``ceil(d*100 - 0.5)/100`` for
       ``d < 0``. ``floor``/``ceil`` (not a ``cast(Int64)``, which ROUNDS) give exact
       truncation toward the boundary, so ``x.xx5`` always rounds away from zero —
       byte-identical to ``Decimal.quantize(Decimal("0.01"), ROUND_HALF_UP)``.

    The result is a ``Decimal(scale=2)`` column whose Python ``Decimal`` scalars are
    numerically EQUAL to the legacy ``round_money(Decimal(str(op)))`` (so
    :meth:`ParquetWriter._bar_values_equal` sees no false conflict) and whose
    ``float(...)`` (the value :meth:`ParquetWriter._render_bytes` writes) is the SAME
    float (so the stored Parquet bytes are byte-identical for every NON-NEGATIVE price —
    the only kind a bar carries). Proven across the realistic price range incl. exact
    ``.005`` half-boundaries, long-tail floats near a boundary (``0.00499999999`` …),
    negatives, zeros, large strikes, and 4dp vendor noise in
    ``tests/golden/test_writer_vectorized_snap_parity.py``.

    Known, unreachable edges (honest envelope, Golden Rule #10):

    * **Signed zero.** A NEGATIVE value that rounds to zero yields ``Decimal("0.00")``
      here vs the legacy ``Decimal("-0.00")`` (polars Decimal has no signed zero), so
      the re-rendered float is ``+0.0`` vs the legacy ``-0.0`` — a 1-bit sign
      difference. UNREACHABLE: a stored bar price is never negative (the vendor sends
      positive OHLC; nothing writes a negative price). The numeric ``Decimal``/``float``
      values still compare EQUAL.
    * **Cast overflow.** A value whose ``x * 100`` exhausts the ``precision=38`` headroom
      (``|x| ≳ 1e19`` — empirically between 1e18 and 1e19) raises a polars error;
      :meth:`ParquetWriter._read_existing_bars` catches it and falls back to the exact
      per-row ``Decimal(str(x))`` reconstruction. UNREACHABLE for any tradable price.
    """
    import polars as pl

    decimal_hi = (
        pl.col(column).cast(pl.String).cast(pl.Decimal(precision=38, scale=_SNAP_PARSE_SCALE))
    )
    scaled = decimal_hi * pl.lit(Decimal(100))
    snapped = (
        pl.when(scaled >= pl.lit(Decimal(0)))
        .then((scaled + pl.lit(Decimal("0.5"))).floor())
        .otherwise((scaled - pl.lit(Decimal("0.5"))).ceil())
    ) / pl.lit(Decimal(100))
    return snapped.cast(pl.Decimal(precision=38, scale=2)).alias(column)


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

    A Greeks-enriched :class:`~brutex.data.models.FnoGreeksBar`
    (`DEC-GREEKS-GENERATION-TIME-001`, plan §15.3) is snapped TYPE-PRESERVINGLY:
    its OHLC plus ``spot_at_bar`` (the ONE overlay column that IS a price) ride
    the same 2dp grid, while the Greeks / stamp fields pass through at FULL
    precision (DEC-MONEY-2DP-001 forbids rounding statistical values).
    """
    if isinstance(bar, FnoGreeksBar):
        return FnoGreeksBar(
            timestamp=bar.timestamp,
            open=round_money(bar.open),
            high=round_money(bar.high),
            low=round_money(bar.low),
            close=round_money(bar.close),
            volume=bar.volume,
            open_interest=bar.open_interest,
            iv=bar.iv,
            delta=bar.delta,
            gamma=bar.gamma,
            theta=bar.theta,
            vega=bar.vega,
            rho=bar.rho,
            spot_at_bar=None if bar.spot_at_bar is None else round_money(bar.spot_at_bar),
            t_years_used=bar.t_years_used,
            rate_used=bar.rate_used,
            greeks_provenance_id=bar.greeks_provenance_id,
        )
    return CandleBar(
        timestamp=bar.timestamp,
        open=round_money(bar.open),
        high=round_money(bar.high),
        low=round_money(bar.low),
        close=round_money(bar.close),
        volume=bar.volume,
        open_interest=bar.open_interest,
    )


def _overlay_values(bar: CandleBar) -> tuple[object, ...]:
    """A bar's Greeks-overlay tuple in canonical field order (all-``None`` for a plain bar).

    ``getattr(..., None)`` so a plain spot :class:`CandleBar` reads as the
    all-``None`` overlay — the identity that keeps the legacy merge semantics
    byte-unchanged (two plain bars always compare overlay-equal).
    """
    return tuple(getattr(bar, name, None) for name in GREEKS_OVERLAY_FIELDS)


def _overlay_equal(a: CandleBar, b: CandleBar) -> bool:
    """True when two bars carry an identical Greeks overlay.

    Deliberately SEPARATE from :meth:`ParquetWriter._bar_values_equal` (which
    stays OHLCV+OI-only — plan §15.3): a Greek difference at the same timestamp
    must never trip the keep-first conflict machinery. In ``latest-wins`` mode
    an overlay difference triggers an overwrite (the enrich-heal contract); in
    ``keep-first`` mode the STORED overlay is kept silently (a vendor re-pull's
    plain bar never strips a stored Greek).
    """
    return _overlay_values(a) == _overlay_values(b)


@dataclass(frozen=True, slots=True)
class BarConflict:
    """A single vendor re-statement detected during merge-on-write.

    When the vendor returns a DIFFERENT OHLCV/OI for a timestamp ALREADY stored
    in the lake the writer records the discrepancy here rather than raising
    (DEC-LAKE-CONFLICT-KEEPFIRST-001). The STORED bar always wins
    (keep-first — never overwritten, preserving reproducibility per
    CLAUDE.md Golden Rule #7). The caller logs the conflict and continues
    the pull, then reports exactly what the vendor changed. A WITHIN-BATCH
    duplicate timestamp with differing values on a FRESH partition write is
    recorded the same way (DEC-FRESH-WRITE-BATCH-DEDUP-001): ``stored`` is
    then the KEPT first occurrence in sorted order (the bar that lands in
    the lake), ``vendor`` the discarded duplicate.

    Parameters
    ----------
    timestamp
        The UTC timestamp of the conflicting bar.
    stored
        The bar currently in the lake — or, for a within-batch duplicate on
        a fresh write, the kept first occurrence (the authoritative value —
        keep-first means this is never replaced).
    vendor
        The bar the vendor just returned (the re-statement that
        was silently discarded after logging).
    """

    timestamp: datetime
    stored: CandleBar
    vendor: CandleBar


def max_conflict_close_delta(conflicts: Iterable[BarConflict]) -> Decimal | None:
    """The max absolute vendor-vs-stored CLOSE delta across *conflicts* — exact Decimal.

    DEC-VENDOR-CONFLICT-SURFACE-001: the ONE derivation of a conflict batch's
    "how far did the vendor's re-stated close drift" figure, shared by the sink
    fold, both ingest spines, and the pipeline headline (CLAUDE.md §11 COMMON).
    Prices are Decimal rupees (CLAUDE.md §6 price-unit row — NEVER float), so the
    delta stays exact through every fold. Returns ``None`` for an empty batch —
    honestly distinct from a genuine ``Decimal("0")`` delta (a conflict whose
    close matched while another OHLCV/OI field differed).
    """
    delta: Decimal | None = None
    for c in conflicts:
        delta = fold_max_close_delta(delta, abs(c.vendor.close - c.stored.close))
    return delta


def fold_max_close_delta(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    """MAX-fold two optional close deltas — ``None`` is the identity (DEC-VENDOR-CONFLICT-SURFACE-001).

    The pure fold every aggregation level uses (window → series → phase →
    pipeline headline): commutative, associative, ``None``-identity, and exact
    (Decimal comparison — no float round-trip, CLAUDE.md §4 money rule). Two
    ``None``\\s fold to ``None`` (no conflict observed anywhere), never a
    fabricated zero (Golden Rule #1).
    """
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


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
        DISTINCT-timestamp incoming bar (a within-batch duplicate collapses to one,
        DEC-FRESH-WRITE-BATCH-DEDUP-001); a merge adds only the timestamps not
        already stored (a re-pull of bars already in the lake adds ``0``); a
        same-timestamp/different-value keep-first conflict adds ``0`` (the stored
        bar is kept). Distinct from :attr:`row_count` (the partition's total size
        after the write) and from the INPUT batch size — this is the honest "how
        many new bars did this write contribute" count the sink reports as
        ``bars_appended``.
    conflicts
        Vendor re-statements detected during this write: timestamps whose
        stored bar differed from the incoming bar (DEC-LAKE-CONFLICT-KEEPFIRST-001)
        — including a WITHIN-BATCH duplicate timestamp with differing values on a
        fresh keep-first write (DEC-FRESH-WRITE-BATCH-DEDUP-001). The stored /
        first-occurrence bar was kept (keep-first, never overwritten). Empty tuple
        for a duplicate-free first write or a clean re-pull with no value changes.
    vendor_dup_rows_dropped
        WITHIN-BATCH duplicate-timestamp rows the vendor physically sent in THIS
        write's batch and the writer collapsed (DEC-VENDOR-CONFLICT-SURFACE-001)
        — the SAME count the aggregated ``lake.vendor_duplicate_rows`` WARNING
        reports, now carried on the result so the pull can SUM it end-to-end
        instead of the count dying in the log. Covers BOTH branches (the fresh
        ``_dedup_batch`` collapse AND the merge branch's timestamp-keyed-dict
        collapse — the count is the batch's ``len − len(unique timestamps)``
        either way). STORED-poison heals (``lake.partition.dedup_healed`` — rows
        a PRIOR write landed) are deliberately NOT counted here: they are a heal
        surface, not this batch's vendor behaviour.
    file_rewritten
        ``True`` iff THIS call RENDERED and atomically replaced the partition's
        bytes (DEC-LAKE-STATS-MANIFEST-001): a fresh write, a merge that added
        bars, a merge that RESTATED bytes without adding (the latest-wins
        Greeks-enrich / dedup-heal rewrite where ``bars_added == 0``), or a
        heal rewrite. ``False`` ONLY for the merge NO-OP fast path (every
        incoming bar already stored identically — the file was not touched).
        The stats-manifest write hook keys on THIS flag — bytes RENDERED,
        never ``bars_added`` — so a no-op merge stays zero-cost while every
        byte change re-stamps the shard's stat token.
    """

    path: Path
    row_count: int
    raw_hash: str
    first_ts: datetime
    last_ts: datetime
    bytes_written: int
    bars_added: int = 0
    conflicts: tuple[BarConflict, ...] = ()
    vendor_dup_rows_dropped: int = 0
    file_rewritten: bool = False


@dataclass(frozen=True, slots=True)
class PartitionHealResult:
    """Outcome of one :meth:`ParquetWriter.heal_partition` call (DEC-LAKE-HEAL-001).

    Attributes
    ----------
    path
        The partition file the heal targeted.
    changed
        ``False`` when the file was left byte-untouched — the idempotence
        oracle: a second heal of a healed partition changes nothing. Also
        ``False`` on a footer-probe failure (the #1067 "never guess" pin —
        poison persists, logged, re-found on the next scan).
    kept
        Rows remaining in the partition after the heal (``0`` when the file
        was quarantine-moved away).
    dropped_duplicates
        Physically-duplicated stored rows collapsed keep-FIRST.
    dropped_removed
        ``remove_timestamps`` entries actually present and dropped (the
        foreign-row reassignment hook).
    raw_hash_before
        sha256 of the ORIGINAL stored bytes (what the quarantine copy holds).
    raw_hash_after
        sha256 of the rewritten bytes; equal to :attr:`raw_hash_before` when
        unchanged; ``None`` iff the file was quarantine-MOVED (emptied).
    write
        The rewrite's :class:`WriteResult` (feeds
        :meth:`ParquetWriter.make_catalog_entry` unmodified); ``None`` when
        unchanged or quarantine-moved.
    """

    path: Path
    changed: bool
    kept: int
    dropped_duplicates: int
    dropped_removed: int
    raw_hash_before: str
    raw_hash_after: str | None
    write: WriteResult | None


class ParquetWriter:
    """Writes canonical OHLCV bars to the partition layout from FR-CAT-001.

    One writer instance is cheap — it holds no open handles. You can
    instantiate one per ingest worker or one per process; both work.

    Parameters
    ----------
    lake_root
        Top-level data-lake directory. Must be a :class:`pathlib.Path`.
        The directory is created on first write.
    partition_log_level
        The level of the per-partition ``lake.partition.write`` telemetry event
        (DEC-PREBUILD-LOG-FLOOD-001): ``"info"`` (default — the LIVE-PULL
        vendor-write path, rare + valuable) or ``"debug"`` (the pipeline
        prebuild's DERIVED-timeframe writers, whose ~10⁵ per-partition lines at
        INFO rotated the whole fetch-phase audit trail away in ~26 minutes).
        Event NAME + FIELDS are identical at either level — only the level
        moves; any other value raises :class:`WriterError` (fail-closed).
    """

    def __init__(self, *, lake_root: Path, partition_log_level: str = "info") -> None:
        if partition_log_level not in ("info", "debug"):
            raise WriterError(
                f"unknown partition_log_level {partition_log_level!r}; expected 'info' or 'debug'"
            )
        self._lake_root = lake_root
        self._partition_log = _LOG.debug if partition_log_level == "debug" else _LOG.info

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
        O(1) (one event per partition write, NOT per bar) — emitted at the
        writer's ``partition_log_level`` (INFO on the live-pull writers, DEBUG
        on the prebuild's derived-TF writers — DEC-PREBUILD-LOG-FLOOD-001, so
        the derived fan-out can never rotate the fetch-phase audit trail away;
        same event name + fields at either level).
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
        # Duplicate-timestamp COUNT for the aggregated observability WARNING below
        # (DEC-LAKE-WRITE-UNIQUE-001): a vendor payload can physically repeat a
        # timestamp (measured live — 135 duplicate 1-minute rows in one window).
        # BOTH branches collapse the duplicates — the fresh branch via
        # `_dedup_batch` (DEC-FRESH-WRITE-BATCH-DEDUP-001), the merge branch via
        # its timestamp-keyed dict — but neither reported the COUNT, so the
        # collapse was silent. One O(batch) set sizes it for either branch.
        duplicates_in_batch = len(sorted_bars) - len({b.timestamp for b in sorted_bars})
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
        # a never-seen timestamp is added, an identical one is a no-op, and a
        # SAME timestamp with DIFFERENT OHLCV (real vendor drift) is resolved
        # per merge_mode — the default "keep-first" KEEPS the stored bar and
        # RECORDS a BarConflict on the returned WriteResult (it does NOT raise;
        # the pull always finishes — DEC-LAKE-CONFLICT-KEEPFIRST-001, surfaced
        # downstream as the per-window `ingest.conflict.summary` WARNING +
        # per-bar DEBUG log), while "latest-wins" (the derived 1day partition
        # only) overwrites it. This makes incremental month-filling + re-pulls
        # idempotent (DEC-LAKE-MERGE-WRITE-001; comment reconciled to the real
        # behaviour by DEC-RESAMPLER-INPUT-GUARD-001's doc-drift audit — the
        # prior "→ raise" wording described the retired rule).
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
        dataset_created = False
        with contextlib.ExitStack() as stack:
            stack.enter_context(_PARTITION_LOCKS.acquire(dest))
            stack.enter_context(partition_write_lock(self._lake_root, dest))
            lock_wait_ms = (time.perf_counter() - lock_acquire_start) * 1000.0

            write_start = time.perf_counter()
            if dest.exists():
                # The merge branch needs NO batch pre-dedup: its timestamp-keyed
                # dict already collapses a within-batch duplicate under the SAME
                # per-merge_mode rule (the first occurrence lands in `merged`, a
                # later duplicate hits the `prev is not None` branches) — and it
                # must see the RAW batch so a same-ts restate keeps conflicting
                # against the STORED bar, byte-unchanged.
                result = self._merge_write(dest, sorted_bars, merge_mode=merge_mode)
            else:
                # FRESH partition: de-duplicate the batch by timestamp FIRST
                # (DEC-FRESH-WRITE-BATCH-DEDUP-001). Without this, a corrupt
                # vendor payload repeating a minute landed BOTH rows in the lake
                # on the first write — and a settled past month is never
                # rewritten, so the duplicate persisted. The dedup applies the
                # SAME per-merge_mode collision rule `_merge_write` uses, so a
                # fresh write + a re-pull of the same batch stay bit-idempotent.
                # NEW-dataset probe (DEC-LAKE-MANIFEST-O1-001): a dataset is
                # CREATED by this write iff its interval dir holds zero
                # ``*.parquet`` BEFORE the file lands (the ``.parquet.tmp``
                # temp never matches the glob). Probed only on the FRESH
                # branch — a new month of an EXISTING dataset short-circuits
                # on its first stored file, and the hot existing-partition
                # merge path never runs this at all (zero manifest cost per
                # bar append). ``dest.parent.parent`` is the interval dir
                # (``…/<interval>/<yyyy>/<mm>.parquet``).
                dataset_created = not any(dest.parent.parent.rglob("*.parquet"))
                fresh_bars, batch_conflicts = self._dedup_batch(sorted_bars, merge_mode=merge_mode)
                # P9 tripwire (DEC-LAKE-WRITE-UNIQUE-001): post-dedup the batch MUST be
                # strictly increasing; a violation is a loud WriterError, never a
                # silently-poisoned partition.
                self._assert_strictly_increasing(dest, fresh_bars)
                content_bytes = self._render_bytes(fresh_bars)
                self._atomic_write(dest, content_bytes)
                result = WriteResult(
                    path=dest,
                    row_count=len(fresh_bars),
                    raw_hash=hashlib.sha256(content_bytes).hexdigest(),
                    first_ts=first_ts,
                    last_ts=last_ts,
                    bytes_written=len(content_bytes),
                    bars_added=len(fresh_bars),
                    conflicts=tuple(batch_conflicts),
                    file_rewritten=True,  # a fresh write always renders bytes
                )
            append_ms = (time.perf_counter() - write_start) * 1000.0
            if result.file_rewritten:
                # Stats-manifest upsert (DEC-LAKE-STATS-MANIFEST-001): the write
                # RENDERED new partition bytes, so the dataset's stats shard —
                # when one exists (lazy accretion: an absent shard costs one
                # failed ``os.stat`` and nothing else) — is re-stamped with the
                # file's fresh stat token, distinct-timestamp count, bounds and
                # raw_hash. Runs INSIDE the still-held partition locks so no
                # concurrent replace can slip between the ``os.replace`` above
                # and the ``os.stat`` here (the token is the durable file's).
                # Best-effort by contract — it can never raise into the pull.
                # The merge NO-OP fast path (``file_rewritten=False``) touches
                # nothing: an existing shard row is still stat-valid.
                self._record_stats_hook(dest, result)

        # Emit the per-partition write telemetry AFTER the locks are released (the
        # log call is not itself part of the serialised critical section). One
        # event per successful partition write — the throughput-binding cost the
        # pull-log forensic could only INFER, now a measurable fact. INFO on the
        # live-pull writers, DEBUG on the prebuild's derived-TF writers
        # (DEC-PREBUILD-LOG-FLOOD-001 — the per-writer `partition_log_level`).
        self._partition_log(
            "lake.partition.write",
            append_ms=round(append_ms, 3),
            lock_wait_ms=round(lock_wait_ms, 3),
            rows=len(sorted_bars),
            partition_rows=result.row_count,
            partition=str(dest),
        )
        if dataset_created:
            # Write-time manifest upsert (DEC-LAKE-MANIFEST-O1-001): this write
            # CREATED the (exchange, segment, symbol, interval) dataset, so the
            # durable manifest gains its row — O(1) per NEW dataset, best-effort
            # (record_dataset never raises into the pull), taken AFTER the
            # partition locks are released (lock order: partition → manifest,
            # never the reverse — no cross-lock deadlock).
            record_dataset(
                self._lake_root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                interval=interval,
            )
        if duplicates_in_batch:
            # ONE aggregated WARNING per write when the vendor sent physically
            # duplicate rows (DEC-LAKE-WRITE-UNIQUE-001) — the batch was collapsed
            # per the merge_mode rule (fresh: `_dedup_batch`, merge: the
            # timestamp-keyed dict), so nothing corrupt was stored; this names
            # what the vendor did (never silent, Golden Rule #1). Emitted outside
            # the locks like the write telemetry (logging is not part of the
            # serialised critical section).
            _LOG.warning(
                "lake.vendor_duplicate_rows",
                symbol=symbol,
                interval=interval,
                window_start=first_ts.isoformat(),
                window_end=last_ts.isoformat(),
                dropped=duplicates_in_batch,
                partition=str(dest),
            )
            # Carry the SAME count on the result (DEC-VENDOR-CONFLICT-SURFACE-001)
            # so the sink → spine → phase → headline fold can SUM it — the count
            # no longer dies in the log. ONE stamp site covers BOTH branches (the
            # fresh `_dedup_batch` collapse and the merge dict collapse both drop
            # exactly the batch's within-batch duplicate timestamps counted above).
            # The zero-duplicate path returns the branch result untouched
            # (byte-identical fast path, no extra allocation).
            result = replace(result, vendor_dup_rows_dropped=duplicates_in_batch)
        return result

    def _merge_write(
        self, dest: Path, new_bars: list[CandleBar], *, merge_mode: str = "keep-first"
    ) -> WriteResult:
        """Union *new_bars* into the existing partition at *dest*, idempotently.

        Reads the stored bars, merges the incoming ones keyed by timestamp
        (O(1) per bar), and rewrites the full month only if the stored content
        changed. A timestamp already present with identical OHLCV is a silent
        no-op — UNLESS the stored file itself physically carries duplicate
        timestamps (a partition poisoned before DEC-LAKE-WRITE-UNIQUE-001), in
        which case the merge FORCES a deduped rewrite even when it adds nothing
        (``lake.partition.dedup_healed``), so a poisoned closed month self-heals
        on the next touch instead of returning the poisoned bytes forever.

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
        # Dedup self-heal (DEC-LAKE-WRITE-UNIQUE-001): a partition poisoned BEFORE the
        # write-side uniqueness invariant landed physically carries duplicate
        # timestamps. `_read_existing_bars` collapses them keep-FIRST, so comparing the
        # stored file's PHYSICAL row count (Parquet footer — O(footer), no column
        # decode) against the unique-timestamp count detects the poison; a positive
        # delta FORCES a deduped rewrite below even when the merge itself adds nothing
        # (the old no-op path returned the poisoned bytes forever). Runs under the
        # partition locks `write()` already holds on this path — never re-acquired.
        stored_unique = len(merged)
        stored_physical = self._stored_physical_rows(dest, existing_bytes)
        heal_dropped = 0 if stored_physical is None else stored_physical - stored_unique
        # Wrap the in-Python merge-dict build + the final ``sorted()`` in ``lake_write_guard``
        # (DEC-LAKE-MERGE-OOM-HALT-001): a deep month partition merged with a large incoming
        # chunk grows ``merged`` (and materialises a sorted copy) in memory, so a ``MemoryError``
        # HERE — between the already-guarded read (`_read_existing_bytes`) and the already-guarded
        # render/write (`_render_bytes`/`_atomic_write`) — must map to a `LakeWriteHalt(
        # "out_of_memory")` pull-wide stop (a member of both spines' `_GLOBAL_STOP_ERRORS`)
        # instead of crashing the whole pull RAW as an "escaped code bug". This closes the ONE
        # unguarded OOM site in the merge path; every other allocation here is already guarded
        # (DEC-LAKE-WRITE-HALT-001 — "every allocation/failure site is guarded").
        with lake_write_guard():
            # ADR-0020 PR-3 (DEC-LAKE-RUST-MERGE-DEDUP-001): the OPTIONAL
            # Rust merge/dedup DECISION seam — default OFF; when
            # BRUTEX_LAKE_RUST_MERGE is truthy the stored + batch bars cross
            # to the brutex_lake crate as 17-column decision frames
            # (`_decision_frame` → `_ipc_bytes`) and the returned per-row
            # action codes are materialized back into `merged` PYTHON-side,
            # guarded by the facade's fail-closed layers (import/version
            # guards, refuse-poison gate, burn-in bit-compare + autorevert).
            # EVERY guarded/degraded path runs the verbatim `_merge_fold`
            # below on this same dict, and the routing stays INSIDE
            # lake_write_guard so a MemoryError anywhere maps to
            # LakeWriteHalt exactly as before (DEC-LAKE-MERGE-OOM-HALT-001).
            # Import is lazy: flag-OFF pays one dict probe and stays
            # byte-identical (never-routes spy-pinned).
            from brutex.data import lake_rust_merge

            if lake_rust_merge.lake_rust_merge_enabled():
                merged, added, rewrite, conflicts = lake_rust_merge.merge_fold(
                    merged,
                    new_bars,
                    merge_mode=merge_mode,
                    python_fold=self._merge_fold,
                    decision_ipc=lambda bars: _ipc_bytes(_decision_frame(bars)),
                )
            else:
                added, rewrite, conflicts = self._merge_fold(
                    merged, new_bars, merge_mode=merge_mode
                )
            merged_sorted = sorted(merged.values(), key=lambda b: b.timestamp)
        if added == 0 and not rewrite and heal_dropped == 0:
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
                file_rewritten=False,  # NO-OP: the stored bytes were not touched
            )

        if heal_dropped > 0:
            # The stored file physically carried duplicate timestamps — the rewrite
            # below persists the keep-first collapsed rows (DEC-LAKE-WRITE-UNIQUE-001).
            _LOG.warning(
                "lake.partition.dedup_healed",
                partition=str(dest),
                kept=stored_unique,
                dropped=heal_dropped,
            )
        # P9 tripwire (DEC-LAKE-WRITE-UNIQUE-001): the merged month is a dict keyed by
        # timestamp sorted ascending, so this can never fire — a genuine invariant assert.
        self._assert_strictly_increasing(dest, merged_sorted)
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
            # Bytes RENDERED — including the ``added == 0`` latest-wins restate
            # and the dedup self-heal rewrite (DEC-LAKE-STATS-MANIFEST-001).
            file_rewritten=True,
        )

    def _merge_fold(
        self,
        merged: dict[datetime, CandleBar],
        new_bars: list[CandleBar],
        *,
        merge_mode: str,
    ) -> tuple[int, bool, list[BarConflict]]:
        """The verbatim timestamp-keyed merge fold — mutates *merged* IN PLACE.

        Extracted token-for-token from the :meth:`_merge_write` loop body
        (ADR-0020 PR-3, DEC-LAKE-RUST-MERGE-DEDUP-001) so the flag-OFF
        branch runs it directly (byte-identical to the pre-extraction
        in-line loop) and the ``lake_rust_merge`` facade can run it as the
        live oracle on every degrade path and the burn-in compare. Returns
        ``(added, rewrite, conflicts)``; the caller sorts + renders.
        """
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
            elif not _overlay_equal(prev, bar):
                # Identical OHLCV+OI but a DIFFERENT Greeks overlay
                # (DEC-GREEKS-GENERATION-TIME-001, plan §15.3): in
                # latest-wins mode the incoming overlay WINS (the enrich
                # pass stamping / correcting Greeks onto stored bars); in
                # keep-first mode the STORED overlay is kept SILENTLY —
                # never a BarConflict (a vendor re-pull's plain bar must
                # not strip a stored Greek; Greeks are a re-derivable
                # overlay, not vendor source truth — `_bar_values_equal`
                # stays OHLCV+OI-only by design).
                if merge_mode == "latest-wins":
                    merged[bar.timestamp] = bar
                    rewrite = True
        return added, rewrite, conflicts

    def _dedup_batch(
        self, sorted_bars: list[CandleBar], *, merge_mode: str
    ) -> tuple[list[CandleBar], list[BarConflict]]:
        """Collapse WITHIN-BATCH duplicate timestamps before a FRESH-partition render.

        Forensic-audit finding #11 (DEC-FRESH-WRITE-BATCH-DEDUP-001): the fresh
        branch rendered ``sorted_bars`` directly, so a corrupt vendor payload
        carrying the SAME timestamp twice stored BOTH rows on a first write —
        and a settled past month is never rewritten, so the duplicate persisted.
        :meth:`_merge_write` was already safe (its timestamp-keyed dict collapses
        a within-batch duplicate against the stored partition); this applies the
        SAME per-*merge_mode* collision rule to the fresh batch, one duplicate
        pair at a time, so BOTH branches see de-duplicated input consistently:

        * an IDENTICAL duplicate (same OHLCV+OI, same overlay) is a silent drop
          (mirrors the merge no-op);
        * a DIFFERENT-value duplicate keeps the FIRST occurrence in sorted order
          under ``"keep-first"`` and records a :class:`BarConflict`
          (``stored`` = the kept first occurrence — the existing observability
          surface, logged downstream via ``ingest.conflict.summary``), or is
          OVERWRITTEN by the later occurrence under ``"latest-wins"`` (no
          conflict — the intentional restate rule);
        * a same-OHLCV / DIFFERENT-overlay duplicate keeps the first overlay
          silently under ``"keep-first"`` and takes the later one under
          ``"latest-wins"`` (mirrors the merge overlay rule, plan §15.3).

        Mirroring the merge rule per mode (not keep-first unconditionally) is
        load-bearing for Golden Rule #7: a fresh ``latest-wins`` write followed
        by a re-pull of the SAME batch must converge on the same bytes the merge
        path would produce. A duplicate-free batch passes through unchanged, so
        the rendered bytes are byte-identical to the pre-fix output. O(1) dict
        lookup per bar; the loop allocates O(batch), so it runs inside
        ``lake_write_guard`` like the merge loop (DEC-LAKE-MERGE-OOM-HALT-001 —
        an OOM here halts gracefully, never crashes the pull raw).
        """
        deduped: list[CandleBar] = []
        slot_by_ts: dict[datetime, int] = {}
        conflicts: list[BarConflict] = []
        with lake_write_guard():
            for bar in sorted_bars:
                slot = slot_by_ts.get(bar.timestamp)
                if slot is None:
                    slot_by_ts[bar.timestamp] = len(deduped)
                    deduped.append(bar)
                elif not self._bar_values_equal(deduped[slot], bar):
                    if merge_mode == "latest-wins":
                        # The later restate wins (no conflict recorded) — the
                        # same intentional-overwrite rule the merge applies.
                        deduped[slot] = bar
                    else:
                        # Keep the FIRST occurrence (keep-first); record the
                        # dropped duplicate for the caller's conflict logging.
                        conflicts.append(
                            BarConflict(timestamp=bar.timestamp, stored=deduped[slot], vendor=bar)
                        )
                elif not _overlay_equal(deduped[slot], bar):
                    # Identical OHLCV+OI but a DIFFERENT Greeks overlay: the
                    # later overlay wins ONLY under latest-wins; keep-first
                    # keeps the first silently — never a BarConflict (mirrors
                    # the merge overlay rule, plan §15.3).
                    if merge_mode == "latest-wins":
                        deduped[slot] = bar
        return deduped, conflicts

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

    def read_partition_bars(self, dest: Path) -> dict[datetime, CandleBar]:
        """Read one stored partition into ``{timestamp: CandleBar}`` under the write locks.

        DEC-LAKE-HEAL-001: the healer's consistent-read entry — the reassignment
        and drift-merge heals need the stored bars of a SOURCE partition before
        moving them home through :meth:`write`. Takes the SAME two locks in the
        SAME order as :meth:`write` (in-process partition lock, then the
        cross-process flock) so the read can never interleave a concurrent
        writer's read-modify-write, and delegates to the existing
        overlay-preserving decode (:meth:`_read_existing_bars` — a 17-column
        Greeks-enriched option file round-trips intact, and a physically
        duplicated stored timestamp collapses keep-FIRST, logged). A vanished /
        unreadable file raises the quarantinable
        :class:`~brutex.data.errors.WriterError` the read privates already map.

        Parameters
        ----------
        dest
            The partition file to read.

        Returns
        -------
        dict[datetime, CandleBar]
            The stored bars keyed by timestamp (keep-first collapsed).
        """
        with contextlib.ExitStack() as stack:
            stack.enter_context(_PARTITION_LOCKS.acquire(dest))
            stack.enter_context(partition_write_lock(self._lake_root, dest))
            existing_bytes = self._read_existing_bytes(dest)
            return self._read_existing_bars(dest, existing_bytes)

    def heal_partition(
        self,
        dest: Path,
        *,
        remove_timestamps: AbstractSet[datetime] = frozenset(),
        quarantine_to: Path | None = None,
    ) -> PartitionHealResult:
        """Heal ONE stored partition in place: keep-FIRST dedup + row removal (DEC-LAKE-HEAL-001).

        The public heal entry the lake healer (:mod:`brutex.data.lake_heal`)
        drives — a SETTLED month nobody re-pulls never takes a merge touch, so
        the #1139 self-heal-on-touch cannot reach it; this method applies the
        SAME mode-independent keep-FIRST collapse through the SAME in-module
        privates (zero duplication: locks in :meth:`write`'s order, the
        overlay-preserving :meth:`_read_existing_bars` decode, the P9
        :meth:`_assert_strictly_increasing` tripwire, the deterministic
        :meth:`_render_bytes` + :meth:`_atomic_write`). STRICTLY row-removing —
        kept rows keep their exact stored values (never value-editing).

        Steps: read the stored bytes → keep-first collapse → footer row-count
        probe (a probe failure returns ``changed=False`` — poison persists,
        logged, NEVER guessed, the #1067 pin) → drop *remove_timestamps* (the
        foreign-row reassignment hook) → if nothing changed, return with the
        file byte-untouched → quarantine-copy the ORIGINAL bytes to
        *quarantine_to* (MANDATORY before any rewrite; via
        :func:`~brutex.data.atomic_io.atomic_write_via` so an ENOSPC/OOM during
        the copy raises :class:`~brutex.data.errors.LakeWriteHalt` with the
        original partition untouched) → rewrite, or — when EVERY row was
        removed (a wholly-foreign file) — quarantine-MOVE it (``unlink`` +
        prune the now-empty ``<yyyy>/`` parent; the writer NEVER renders an
        empty partition, :meth:`write`'s own law).

        Parameters
        ----------
        dest
            The partition file to heal.
        remove_timestamps
            Stored timestamps to DROP (whole rows) — the reassignment heal
            moves them to their canonical month file first, then removes them
            here. Absent timestamps are ignored.
        quarantine_to
            Where the ORIGINAL bytes are copied before any mutation. Required
            whenever the heal would change the file (``WriterError`` otherwise
            — never rewrite without the safety copy).

        Returns
        -------
        PartitionHealResult
            The heal outcome; ``result.write`` feeds
            :meth:`make_catalog_entry` so the rotated ``raw_hash`` reaches the
            catalog + ``data_snapshot_hash`` (Golden Rule #4).

        Raises
        ------
        WriterError
            Vanished/corrupt file, or a changing heal with no *quarantine_to*.
        LakeWriteHalt
            Disk-full / OOM during the quarantine copy or the rewrite.
        PartitionLockTimeout
            The cross-process lock could not be acquired in time (a transient
            defer — the finding is re-found on the next scan).
        """
        with contextlib.ExitStack() as stack:
            stack.enter_context(_PARTITION_LOCKS.acquire(dest))
            stack.enter_context(partition_write_lock(self._lake_root, dest))
            return self._heal_partition_locked(
                dest, remove_timestamps=remove_timestamps, quarantine_to=quarantine_to
            )

    def _heal_partition_locked(
        self,
        dest: Path,
        *,
        remove_timestamps: AbstractSet[datetime],
        quarantine_to: Path | None,
    ) -> PartitionHealResult:
        """The :meth:`heal_partition` body, run under BOTH partition locks."""
        existing_bytes = self._read_existing_bytes(dest)
        raw_hash_before = hashlib.sha256(existing_bytes).hexdigest()
        merged = self._read_existing_bars(dest, existing_bytes)
        stored_unique = len(merged)
        physical = self._stored_physical_rows(dest, existing_bytes)
        if physical is None:
            # The #1067 pin: a failed footer probe SKIPS the heal — the
            # poison persists (already logged) rather than being guessed at.
            return PartitionHealResult(
                path=dest,
                changed=False,
                kept=stored_unique,
                dropped_duplicates=0,
                dropped_removed=0,
                raw_hash_before=raw_hash_before,
                raw_hash_after=raw_hash_before,
                write=None,
            )
        dropped_duplicates = physical - stored_unique
        dropped_removed = 0
        for ts in sorted(remove_timestamps):
            if ts in merged:
                del merged[ts]
                dropped_removed += 1
        if dropped_duplicates == 0 and dropped_removed == 0:
            return PartitionHealResult(
                path=dest,
                changed=False,
                kept=stored_unique,
                dropped_duplicates=0,
                dropped_removed=0,
                raw_hash_before=raw_hash_before,
                raw_hash_after=raw_hash_before,
                write=None,
            )
        if quarantine_to is None:
            raise WriterError(
                f"heal of {dest} requires a quarantine destination — "
                f"refusing to rewrite without the safety copy"
            )
        # Quarantine-copy the ORIGINAL bytes BEFORE any mutation. A full disk
        # here raises LakeWriteHalt with dest untouched (graceful halt).
        with lake_write_guard():
            quarantine_to.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_via(quarantine_to, lambda tmp: tmp.write_bytes(existing_bytes))
        if not merged:
            # Every row removed (a wholly-foreign file): quarantine-MOVE —
            # the writer never renders an empty partition (write()'s law).
            dest.unlink()
            with contextlib.suppress(OSError):
                dest.parent.rmdir()  # prune the now-empty <yyyy>/ dir
            # A file REMOVAL can empty a dataset, so the durable dataset
            # manifest may now over-list — invalidate it (best-effort, O(1));
            # the next discovery walks + rebuilds (DEC-LAKE-MANIFEST-O1-001).
            invalidate_manifest(self._lake_root)
            # Stats-manifest twin (DEC-LAKE-STATS-MANIFEST-001): the moved
            # file's row leaves the dataset's stats shard beside the manifest
            # invalidation (best-effort; the LAST row's removal unlinks the
            # whole shard — an empty dataset has no shard). Still under both
            # held partition locks.
            self._remove_stats_hook(dest)
            return PartitionHealResult(
                path=dest,
                changed=True,
                kept=0,
                dropped_duplicates=dropped_duplicates,
                dropped_removed=dropped_removed,
                raw_hash_before=raw_hash_before,
                raw_hash_after=None,
                write=None,
            )
        merged_sorted = sorted(merged.values(), key=lambda b: b.timestamp)
        # P9 tripwire — unreachable post-dedup by construction, a genuine
        # invariant assert exactly as on the write paths.
        self._assert_strictly_increasing(dest, merged_sorted)
        content_bytes = self._render_bytes(merged_sorted)
        self._atomic_write(dest, content_bytes)
        if dropped_duplicates > 0:
            # Reuse the PINNED heal event (same fields as the merge self-heal).
            _LOG.warning(
                "lake.partition.dedup_healed",
                partition=str(dest),
                kept=stored_unique,
                dropped=dropped_duplicates,
            )
        write = WriteResult(
            path=dest,
            row_count=len(merged_sorted),
            raw_hash=hashlib.sha256(content_bytes).hexdigest(),
            first_ts=merged_sorted[0].timestamp,
            last_ts=merged_sorted[-1].timestamp,
            bytes_written=len(content_bytes),
            bars_added=0,  # a heal never adds bars (Golden Rule #1)
            conflicts=(),
            file_rewritten=True,  # the heal rendered + replaced the bytes
        )
        # Stats-manifest re-stamp (DEC-LAKE-STATS-MANIFEST-001): the heal
        # rewrite rendered new bytes, so the shard row is upserted under the
        # SAME held locks — it carries the ROTATED raw_hash, the post-removal
        # distinct count and a fresh stat token (the shard and the catalog
        # therefore agree). The probe-failure ABSTAIN + unchanged branches
        # above return earlier and touch neither the file nor the shard.
        self._record_stats_hook(dest, write)
        return PartitionHealResult(
            path=dest,
            changed=True,
            kept=len(merged_sorted),
            dropped_duplicates=dropped_duplicates,
            dropped_removed=dropped_removed,
            raw_hash_before=raw_hash_before,
            raw_hash_after=write.raw_hash,
            write=write,
        )

    # ── Private ────────────────────────────────────────────────────

    def _record_stats_hook(self, dest: Path, result: WriteResult) -> None:
        """Best-effort stats-manifest upsert for a just-rendered partition.

        DEC-LAKE-STATS-MANIFEST-001 write hook: called by :meth:`write` (both
        branches, when ``result.file_rewritten``) and by the heal rewrite —
        always INSIDE the already-held partition locks, so the ``os.stat``
        taken here is the durable file's content token (no concurrent
        ``os.replace`` is possible). LAZY ACCRETION, probed FIRST
        (:func:`~brutex.data.lake_stats_manifest.partition_stats_tracked`,
        review round 2): an unviewed dataset's write costs exactly ONE cheap
        O(1) probe — a shard-path key digest + one failed ``os.stat`` — and
        never even stats *dest*. When the shard exists, delegates to
        :func:`~brutex.data.lake_stats_manifest.record_partition_stats`,
        which is best-effort by contract (a shard failure logs + unlinks +
        returns — it NEVER raises into the pull; a
        :class:`~brutex.data.errors.LakeWriteHalt` from ``.locks/`` creation
        on a genuinely full disk deliberately propagates). The kill switch
        (``BRUTEX_LAKE_STATS_MANIFEST=off``) makes this a pure no-op — not
        even the probe runs (byte-identical legacy write behaviour). A *dest*
        outside the canonical ``bars/`` layout or a vanished-file stat is
        logged and skipped — never an error into the write path.
        """
        if not stats_manifest_enabled():
            return
        key = self._stats_key_for_dest(dest)
        if key is None:
            _LOG.warning("lake.stats_manifest.key_underivable", partition=str(dest))
            return
        if not partition_stats_tracked(self._lake_root, KIND_BARS, key):
            # Lazy accretion, probe-FIRST (review round 2): an unviewed
            # dataset pays only this probe — the os.stat(dest) below never
            # runs; seeding stays read-side only.
            return
        try:
            stat = os.stat(dest)
        except OSError as exc:
            _LOG.warning(
                "lake.stats_manifest.stat_failed",
                partition=str(dest),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return
        record_partition_stats(
            self._lake_root,
            kind=KIND_BARS,
            key=key,
            dest=dest,
            rows=result.row_count,
            min_ts=result.first_ts,
            max_ts=result.last_ts,
            raw_hash=result.raw_hash,
            stat=stat,
        )

    def _remove_stats_hook(self, dest: Path) -> None:
        """Best-effort stats-manifest row removal for a quarantine-MOVED file.

        DEC-LAKE-STATS-MANIFEST-001 heal hook: called by the
        :meth:`_heal_partition_locked` quarantine-MOVE branch beside
        ``invalidate_manifest``, under the SAME held partition locks.
        Delegates to
        :func:`~brutex.data.lake_stats_manifest.remove_partition_stats`
        (lazy + best-effort — never raises into the heal; the last row's
        removal unlinks the whole shard). Kill switch off ⇒ pure no-op.
        """
        if not stats_manifest_enabled():
            return
        key = self._stats_key_for_dest(dest)
        if key is None:
            _LOG.warning("lake.stats_manifest.key_underivable", partition=str(dest))
            return
        remove_partition_stats(self._lake_root, kind=KIND_BARS, key=key, dest=dest)

    def _stats_key_for_dest(self, dest: Path) -> dict[str, str] | None:
        """Derive the stats-manifest dataset key VERBATIM from *dest*'s path.

        The ONE derivation both hook sites share (CLAUDE.md §11 COMMON): a
        canonical partition path is
        ``<lake_root>/bars/<exchange>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet``,
        so the key is the four dataset components exactly as they appear ON
        DISK (a #911-drift dir stays its own dataset — the shard-key verbatim
        rule). Returns ``None`` for any other shape (outside the lake, outside
        ``bars/``, wrong depth) — the hooks abstain rather than guess. O(1).
        """
        try:
            rel = dest.relative_to(self._lake_root / BAR_DIR_NAME)
        except ValueError:
            return None
        parts = rel.parts
        # PARTITION_PATH_COMPONENTS counts the leading ``bars`` component,
        # which ``relative_to`` above already consumed.
        if len(parts) != PARTITION_PATH_COMPONENTS - 1:
            return None
        return {
            "exchange": parts[0],
            "segment": parts[1],
            "symbol": parts[2],
            "interval": parts[3],
        }

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
    def _assert_strictly_increasing(dest: Path, sorted_bars: list[CandleBar]) -> None:
        """P9 write-invariant tripwire: refuse to render a non-strictly-increasing batch.

        DEC-LAKE-WRITE-UNIQUE-001: every render site runs AFTER the keep-first
        dedup, so the timestamps handed to :meth:`_render_bytes` are unique and
        ascending BY CONSTRUCTION — this assert can only fire on a genuine code
        regression (a future edit bypassing the dedup), in which case it raises
        a loud :class:`~brutex.data.errors.WriterError` NAMING the partition
        instead of silently persisting a poisoned file. O(n) over the batch —
        strictly cheaper than the Parquet render it guards.
        """
        for prev, cur in itertools.pairwise(sorted_bars):
            if cur.timestamp <= prev.timestamp:
                raise WriterError(
                    f"write invariant violated for partition {dest}: timestamps not "
                    f"strictly increasing after dedup "
                    f"({cur.timestamp.isoformat()} <= {prev.timestamp.isoformat()}) — "
                    f"refusing to render a poisoned partition"
                )

    @staticmethod
    def _stored_physical_rows(dest: Path, existing_bytes: bytes) -> int | None:
        """The stored partition's PHYSICAL row count, from the Parquet footer.

        DEC-LAKE-WRITE-UNIQUE-001: the dedup self-heal needs the file's physical
        row count to compare against the unique-timestamp count
        :meth:`_read_existing_bars` returns. The footer's ``num_rows`` is
        authoritative and O(footer) — no column decode, so the steady-state
        no-op merge (the re-pull hot path) pays only a metadata read.

        ``None`` when the footer probe fails (defensive — polars just parsed
        these same bytes, so a pyarrow footer failure is essentially
        unreachable): the caller then SKIPS the heal check for this write
        rather than guessing, and the probe failure is logged
        (``lake.partition.rowcount_probe_failed``) so it is never silent. The
        heal re-runs on the next touch (honest envelope: on that exotic edge
        the poison persists until the probe succeeds — never mis-healed).
        """
        import io

        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover — ships with polars in the [data] extra
            raise WriterError(
                "ParquetWriter requires 'pyarrow' — install with "
                "'pip install -e .[data]' to enable the P2 data lake"
            ) from exc

        try:
            parquet_file = pq.ParquetFile(  # type: ignore[no-untyped-call, unused-ignore]
                io.BytesIO(existing_bytes)
            )
            return int(parquet_file.metadata.num_rows)
        except (pa.ArrowException, OSError) as exc:
            _LOG.warning(
                "lake.partition.rowcount_probe_failed",
                partition=str(dest),
                error=str(exc),
            )
            return None

    @staticmethod
    def _read_existing_bars(dest: Path, existing_bytes: bytes) -> dict[datetime, CandleBar]:
        """Parse a stored partition's *existing_bytes* into ``{timestamp: CandleBar}``.

        A physically-duplicated stored timestamp (a partition poisoned before
        DEC-LAKE-WRITE-UNIQUE-001) is collapsed keep-FIRST — the first physical
        row wins, honouring DEC-LAKE-CONFLICT-KEEPFIRST-001 — and the collapse
        is logged (``lake.partition.duplicate_collapsed``), never silent. (The
        old code collapsed last-wins with no log.) The merge caller compares
        this map's size against the footer row count to drive the dedup
        self-heal.

        Prices are snapped to the 2dp tick grid by the VECTORIZED, GIL-releasing
        :func:`_snap_price_expr` (DEC-LAKE-WRITE-VECTORIZE-001) — a polars-native
        columnar expression that reproduces the EXACT byte contract of the legacy
        per-row ``round_money(Decimal(str(float)))`` (``ROUND_HALF_UP``, never
        truncation; DEC-MONEY-2DP-001 / DEC-SPOT-PRICE-2DP-001). So a stored bar still
        compares equal to a freshly-rounded incoming bar even if the partition predates
        DEC-SPOT-PRICE-2DP-001 (any 4-decimal remnant rounds to the same 2dp), keeping
        re-pulls idempotent and converging the lake to clean 2dp — and the rebuilt
        ``{timestamp: CandleBar}`` map is byte-identical to the prior per-row path
        (proven in ``tests/golden/test_writer_vectorized_snap_parity.py``,
        CLAUDE.md Golden Rule #7).

        Resilience (GAP #1): the existing-partition DECODE is wrapped in a narrow ``try``
        over :func:`~brutex.data.errors.parquet_read_skip_errors` (a
        ``polars.exceptions.PolarsError`` on a corrupt / version-incompatible file, or an
        ``OSError`` fault). A CORRUPT existing partition therefore raises a QUARANTINABLE
        :class:`~brutex.data.errors.WriterError` naming the bad path instead of crashing
        the whole pull — the ingest spine quarantines that one symbol's window and
        proceeds; every other symbol still pulls. ``MemoryError`` / ENOSPC stay mapped to
        ``LakeWriteHalt`` by the guard (a pull-wide stop, never demoted to a per-symbol
        quarantine).

        Performance (DEC-LAKE-WRITE-VECTORIZE-001): the heavy
        ``Decimal(str(float))`` + half-up reconstruction — previously a pure-Python,
        partly-GIL-held per-row loop (the single biggest write cost on a deep month
        partition) — now runs entirely as ONE columnar polars ``select`` (compiled C,
        GIL released for the math), ~1.8x lower wall-time on a ~7,500-row partition and
        ~10x lower on the price columns in isolation. Only the final
        ``{timestamp: CandleBar}`` dict build stays in Python (inherent to the merge
        structure); the double ``CandleBar`` construction + ``_tick_round`` of the old
        path is gone.

        Correctness fallback: the vectorized snap is BYTE-EXACT for every realistic price
        (see :func:`_snap_price_expr`), but a value whose ``x * 100`` overflows the
        intermediate ``Decimal(precision=38)`` (``|x| ≳ 1e19``, unreachable for a bar
        price) raises a ``PolarsError`` from the snap. That is NOT corruption, so it is
        NOT quarantined — it falls back to the exact per-row ``Decimal(str(x))`` +
        ``_tick_round`` reconstruction (:meth:`_reconstruct_bars_per_row`), the proven
        legacy path. A genuinely-corrupt NON-FINITE (inf/NaN) price likewise lands on the
        per-row path, where it raises a quarantinable ``WriterError`` (it can never be a
        valid 2dp price). So correctness wins over the micro-optimization, and the
        change never silently corrupts a long-tail value.
        """
        try:
            import polars as pl
        except ImportError as exc:  # pragma: no cover — same guard as _render_bytes
            raise WriterError(
                "ParquetWriter requires 'polars' — install with "
                "'pip install -e .[data]' to enable the P2 data lake"
            ) from exc

        import io

        # The DECODE is the steady-state re-pull's largest allocation, so it stays inside
        # `lake_write_guard`: a `MemoryError` here becomes a `LakeWriteHalt` pull-wide
        # stop (DEC-LAKE-WRITE-HALT-001, unchanged). A corrupt-file `PolarsError` /
        # non-ENOSPC `OSError` (GAP #1) is caught and re-raised as a quarantinable
        # `WriterError` naming the path — `MemoryError` is NOT in the skip set, so an OOM
        # still reaches the guard and halts (never demoted to a quarantine).
        with lake_write_guard():
            try:
                df = pl.read_parquet(io.BytesIO(existing_bytes))
                # Snap OHLC to the 2dp tick grid columnarly (byte-exact, GIL-released);
                # timestamp / volume / open_interest pass through untouched. A value
                # whose repr overflows precision-38 (unreachable for a price) raises a
                # PolarsError here → fall back to the exact per-row reconstruction.
                # An ENRICHED partition's `spot_at_bar` (the ONE overlay column that
                # is a price — plan §15.3) rides the SAME snap; the Greeks columns
                # pass through at FULL precision (DEC-MONEY-2DP-001).
                snap_columns: list[str] = list(_PRICE_COLUMNS)
                if "spot_at_bar" in df.columns:
                    snap_columns.append("spot_at_bar")
                snapped: pl.DataFrame | None
                try:
                    snapped = df.with_columns(*(_snap_price_expr(c) for c in snap_columns))
                except pl.exceptions.PolarsError:
                    snapped = None
            except parquet_read_skip_errors() as exc:
                raise WriterError(f"corrupt existing partition {dest}: {exc}") from exc

        if snapped is None:
            # Overflow / non-finite fallback: exact per-row Decimal(str(x)) path.
            return ParquetWriter._reconstruct_bars_per_row(dest, df)

        # COLUMN-wise extraction (one Series.to_list per column). The OHLC columns are
        # already snapped to `Decimal(scale=2)`, so `to_list` yields Python `Decimal`
        # scalars equal to the legacy `round_money(Decimal(str(op)))` — no per-row
        # Decimal parse / `_tick_round` remains (DEC-LAKE-WRITE-VECTORIZE-001).
        timestamps = snapped["timestamp"].to_list()
        opens = snapped["open"].to_list()
        highs = snapped["high"].to_list()
        lows = snapped["low"].to_list()
        closes = snapped["close"].to_list()
        volumes = snapped["volume"].to_list()
        ois = snapped["open_interest"].to_list()
        # SCHEMA-TOLERANT overlay read-back (DEC-GREEKS-GENERATION-TIME-001, plan
        # §15.3 — "the single biggest correctness trap"): a LEGACY 7-column
        # partition has NO overlay columns → plain CandleBars are rebuilt and the
        # merge round-trip re-renders the byte-identical 7-column blob (no
        # KeyError, NO migration). An ENRICHED partition (any overlay column
        # present) rebuilds FnoGreeksBars, each ABSENT overlay column reading as
        # a None-filled series — so its overlay survives every future merge.
        overlay_lists = ParquetWriter._overlay_column_lists(snapped, len(timestamps))
        out: dict[datetime, CandleBar] = {}
        try:
            for idx, (ts, op, hi, lo, cl, vol, oi) in enumerate(
                zip(timestamps, opens, highs, lows, closes, volumes, ois, strict=True)
            ):
                if ts in out:
                    # Keep-FIRST collapse of a physically-duplicated stored timestamp
                    # (DEC-LAKE-WRITE-UNIQUE-001): the FIRST physical row wins —
                    # honouring DEC-LAKE-CONFLICT-KEEPFIRST-001 — where the old
                    # last-assignment silently let the LAST row win. Counted + logged
                    # below, never silent.
                    continue
                bar: CandleBar
                if overlay_lists is None:
                    bar = CandleBar(
                        timestamp=ts,
                        open=op,
                        high=hi,
                        low=lo,
                        close=cl,
                        volume=int(vol),
                        open_interest=None if oi is None else int(oi),
                    )
                else:
                    bar = FnoGreeksBar(
                        timestamp=ts,
                        open=op,
                        high=hi,
                        low=lo,
                        close=cl,
                        volume=int(vol),
                        open_interest=None if oi is None else int(oi),
                        iv=overlay_lists["iv"][idx],
                        delta=overlay_lists["delta"][idx],
                        gamma=overlay_lists["gamma"][idx],
                        theta=overlay_lists["theta"][idx],
                        vega=overlay_lists["vega"][idx],
                        rho=overlay_lists["rho"][idx],
                        spot_at_bar=overlay_lists["spot_at_bar"][idx],
                        t_years_used=overlay_lists["t_years_used"][idx],
                        rate_used=overlay_lists["rate_used"][idx],
                        greeks_provenance_id=overlay_lists["greeks_provenance_id"][idx],
                    )
                out[bar.timestamp] = bar
        except ValueError as exc:
            # A parquet-readable but VALUE-corrupt partition (e.g. a NaN Greek —
            # FnoGreeksBar's finite-or-None validation refuses it, mirroring the
            # per-row path's contract): a quarantinable WriterError naming the
            # path, never a raw mid-merge crash (GAP #1 semantics preserved).
            raise WriterError(f"corrupt existing partition {dest}: {exc}") from exc
        ParquetWriter._log_duplicate_collapse(dest, physical=len(timestamps), unique=len(out))
        return out

    @staticmethod
    def _overlay_column_lists(df: pl.DataFrame, height: int) -> dict[str, list[Any]] | None:
        """Extract the Greeks-overlay columns of a stored partition, tolerantly.

        ``None`` when NO overlay column is physically present (a legacy /
        plain partition — the caller rebuilds plain :class:`CandleBar` rows so
        the merge round-trip stays byte-identical). Otherwise one list per
        :data:`~brutex.data.models.GREEKS_OVERLAY_FIELDS` name, an ABSENT
        column filled with ``None`` (never a ``KeyError`` — plan §15.3's
        schema-tolerance requirement). ``spot_at_bar`` was already 2dp-snapped
        to ``Decimal`` by the caller's vectorized snap.
        """
        if not any(name in df.columns for name in GREEKS_OVERLAY_FIELDS):
            return None
        out: dict[str, list[Any]] = {}
        for name in GREEKS_OVERLAY_FIELDS:
            out[name] = df[name].to_list() if name in df.columns else [None] * height
        return out

    @staticmethod
    def _reconstruct_bars_per_row(dest: Path, df: object) -> dict[datetime, CandleBar]:
        """Exact per-row ``Decimal(str(float))`` + ``_tick_round`` reconstruction.

        The proven legacy 2dp-snap path (DEC-WRITER-COLUMNAR-4A-001), kept as the
        CORRECTNESS fallback for the vectorized :func:`_snap_price_expr`
        (DEC-LAKE-WRITE-VECTORIZE-001): it is invoked ONLY when the columnar snap raises
        (a value whose ``x * 100`` overflows the intermediate ``Decimal(precision=38)``,
        ``|x| ≳ 1e19`` — unreachable for a real price). Column-wise ``Series.to_list``
        extraction (one list per column, no per-row dict) keeps it allocation-lean.

        A genuinely-corrupt NON-FINITE (inf/NaN) price reaches this path too: building a
        ``CandleBar`` from it raises ``ValueError`` (the ``low <= open <= high`` invariant
        fails for ``inf``) — or ``decimal.InvalidOperation`` if the snap raises first — and
        EITHER is re-raised as a quarantinable :class:`~brutex.data.errors.WriterError`
        naming the bad path (a non-finite / out-of-order value can never be a valid bar, so
        it is treated as corruption — the pull quarantines that one symbol's window and
        proceeds, never crashing the whole pull).
        """
        from decimal import InvalidOperation

        timestamps = df["timestamp"].to_list()  # type: ignore[index]
        opens = df["open"].to_list()  # type: ignore[index]
        highs = df["high"].to_list()  # type: ignore[index]
        lows = df["low"].to_list()  # type: ignore[index]
        closes = df["close"].to_list()  # type: ignore[index]
        volumes = df["volume"].to_list()  # type: ignore[index]
        ois = df["open_interest"].to_list()  # type: ignore[index]
        columns: list[str] = list(df.columns)  # type: ignore[attr-defined]
        overlay: dict[str, list[Any]] | None = None
        if any(name in columns for name in GREEKS_OVERLAY_FIELDS):
            # Schema-tolerant overlay extraction (mirrors `_overlay_column_lists`):
            # an enriched partition landing on this fallback keeps its overlay.
            overlay = {
                name: (
                    df[name].to_list()  # type: ignore[index]
                    if name in columns
                    else [None] * len(timestamps)
                )
                for name in GREEKS_OVERLAY_FIELDS
            }
        out: dict[datetime, CandleBar] = {}
        try:
            for idx, (ts, op, hi, lo, cl, vol, oi) in enumerate(
                zip(timestamps, opens, highs, lows, closes, volumes, ois, strict=True)
            ):
                if ts in out:
                    # Keep-FIRST collapse, mirroring the columnar path
                    # (DEC-LAKE-WRITE-UNIQUE-001) — counted + logged below.
                    continue
                base: CandleBar
                if overlay is None:
                    base = CandleBar(
                        timestamp=ts,
                        open=Decimal(str(op)),
                        high=Decimal(str(hi)),
                        low=Decimal(str(lo)),
                        close=Decimal(str(cl)),
                        volume=int(vol),
                        open_interest=None if oi is None else int(oi),
                    )
                else:
                    spot_raw = overlay["spot_at_bar"][idx]
                    base = FnoGreeksBar(
                        timestamp=ts,
                        open=Decimal(str(op)),
                        high=Decimal(str(hi)),
                        low=Decimal(str(lo)),
                        close=Decimal(str(cl)),
                        volume=int(vol),
                        open_interest=None if oi is None else int(oi),
                        iv=overlay["iv"][idx],
                        delta=overlay["delta"][idx],
                        gamma=overlay["gamma"][idx],
                        theta=overlay["theta"][idx],
                        vega=overlay["vega"][idx],
                        rho=overlay["rho"][idx],
                        spot_at_bar=None if spot_raw is None else Decimal(str(spot_raw)),
                        t_years_used=overlay["t_years_used"][idx],
                        rate_used=overlay["rate_used"][idx],
                        greeks_provenance_id=overlay["greeks_provenance_id"][idx],
                    )
                bar = _tick_round(base)
                out[bar.timestamp] = bar
        except (InvalidOperation, ValueError) as exc:
            raise WriterError(f"corrupt existing partition {dest}: {exc}") from exc
        ParquetWriter._log_duplicate_collapse(dest, physical=len(timestamps), unique=len(out))
        return out

    @staticmethod
    def _log_duplicate_collapse(dest: Path, *, physical: int, unique: int) -> None:
        """Log a stored-duplicate keep-first collapse — never silent (DEC-LAKE-WRITE-UNIQUE-001).

        Emitted by BOTH partition-decode paths (columnar + the per-row fallback)
        when the stored file physically carried more rows than unique timestamps.
        The merge caller separately decides the self-heal rewrite off the footer
        row count; this event makes the READ-side collapse itself visible (the
        old code collapsed silently, last-wins).
        """
        if physical > unique:
            _LOG.warning(
                "lake.partition.duplicate_collapsed",
                partition=str(dest),
                physical_rows=physical,
                unique_timestamps=unique,
                collapsed=physical - unique,
            )

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
            rows: dict[str, list[object]] = {
                "timestamp": [b.timestamp for b in sorted_bars],
                "open": [float(b.open) for b in sorted_bars],
                "high": [float(b.high) for b in sorted_bars],
                "low": [float(b.low) for b in sorted_bars],
                "close": [float(b.close) for b in sorted_bars],
                "volume": [b.volume for b in sorted_bars],
                "open_interest": [b.open_interest for b in sorted_bars],
            }
            schema: dict[str, pl.DataType | type[pl.DataType]] = {
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "open_interest": pl.Int64,
            }
            # Greeks-enriched branch (DEC-GREEKS-GENERATION-TIME-001, plan §15.3):
            # ONLY when the batch carries at least one FnoGreeksBar are the ten
            # nullable overlay columns appended after `open_interest` — a plain
            # spot / derived-1day batch still renders the byte-identical 7-column
            # blob (legacy partitions untouched, NO migration). A plain CandleBar
            # merged INTO an enriched partition reads its overlay as None via
            # `getattr` (the schema-tolerant identity `_overlay_values` uses).
            # `spot_at_bar` is the ONE snapped price; the Greeks stay FULL
            # precision (never through `_snap_price_expr` / `_PRICE_COLUMNS`).
            if any(isinstance(b, FnoGreeksBar) for b in sorted_bars):
                for name in GREEKS_OVERLAY_FIELDS:
                    if name == "greeks_provenance_id":
                        rows[name] = [getattr(b, name, None) for b in sorted_bars]
                        schema[name] = pl.Int32
                    elif name == "spot_at_bar":
                        rows[name] = [
                            None if (v := getattr(b, name, None)) is None else float(v)
                            for b in sorted_bars
                        ]
                        schema[name] = pl.Float64
                    else:
                        rows[name] = [getattr(b, name, None) for b in sorted_bars]
                        schema[name] = pl.Float64
            df = pl.DataFrame(rows, schema=schema)

            def _python_render() -> bytes:
                # polars write_parquet → file-like. Use an in-memory BytesIO
                # so we can compute the hash before touching the filesystem.
                bio = io.BytesIO()
                df.write_parquet(
                    bio,
                    compression="zstd",
                    compression_level=ZSTD_LEVEL,
                    row_group_size=ROW_GROUP_SIZE,
                    use_pyarrow=False,
                    statistics=False,  # stats are non-deterministic; skip for reproducibility
                )
                return bio.getvalue()

            # ADR-0020 PR-1 (DEC-RUST-DATALAKE-IO-ROADMAP-001): the OPTIONAL
            # Rust render seam — default OFF; when BRUTEX_LAKE_RUST_RENDER is
            # truthy the frame crosses to the version-lockstepped brutex_lake
            # crate as uncompressed Arrow IPC, guarded by the facade's three
            # fail-closed layers (import guard / version guard / burn-in +
            # autorevert). EVERY guarded/degraded path returns the verbatim
            # _python_render() bytes, and the routing stays INSIDE
            # lake_write_guard so a MemoryError anywhere maps to LakeWriteHalt
            # exactly as before. Import is lazy: flag-OFF pays one dict probe
            # and stays byte-identical.
            from brutex.data import lake_rust_render

            if lake_rust_render.lake_rust_render_enabled():
                return lake_rust_render.render_parquet_bytes(
                    ipc_bytes_factory=lambda: _ipc_bytes(df),
                    python_render=_python_render,
                    polars_version=pl.__version__,
                    # The burn-in SHAPE gate's key (review-round fold): every
                    # distinct frame schema (7-col spot / 17-col Greeks
                    # overlay) is byte-compared at least once before crate
                    # bytes ship for it — the count budget alone would let a
                    # spot-only burn-in license a first-ever Greeks shape.
                    schema_token=str(df.schema),
                )
            return _python_render()

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


def _ipc_bytes(df: pl.DataFrame) -> bytes:
    """Serialise *df* to UNCOMPRESSED Arrow IPC bytes — the Rust-render handoff.

    DEC-RUST-DATALAKE-IO-ROADMAP-001: a THIN delegating alias of the COMMON
    :func:`brutex.data.lake_rust_render.ipc_handoff_bytes` (the body was
    HOISTED there in PR-2 when ``option_parquet_writer`` became the second
    caller — §11 COMMON; this alias preserves the PR-1 module's documented
    shape and its test surface). All the byte-surface + memory-honesty
    documentation lives on the common helper. Reached ONLY from the flag-ON
    path inside :meth:`ParquetWriter._render_bytes`'s ``lake_write_guard``
    block, so an allocation failure maps to ``LakeWriteHalt`` like every
    other render allocation.
    """
    from brutex.data import lake_rust_render

    return lake_rust_render.ipc_handoff_bytes(df)


def _decision_frame(bars: list[CandleBar]) -> pl.DataFrame:
    """Build the ALWAYS-17-column merge-DECISION frame (ADR-0020 PR-3).

    The ``brutex_lake`` merge/dedup decision kernel
    (DEC-LAKE-RUST-MERGE-DEDUP-001) compares rows by column NAME across this
    FIXED schema, so the render frame's 7-col/17-col branching is
    deliberately NOT mirrored here — this frame is a decision handoff,
    never rendered, and the all-null overlay columns a plain spot batch
    carries cost nothing byte-parity-wise. A plain :class:`CandleBar` reads
    its overlay as ``None`` via ``getattr`` — the SAME schema-tolerant
    identity :func:`_overlay_values` uses — so the kernel's overlay compare
    is the ``_overlay_equal`` law over frames. Prices cross as the SAME
    ``float(Decimal)`` conversion the render frame applies (values are
    already 2dp-snapped at the top of ``write()``; the kernel NEVER
    re-rounds); ``greeks_provenance_id`` crosses as Int64 (comparison-only
    — the render schema's Int32 range law is untouched).
    """
    import polars as pl

    rows: dict[str, list[object]] = {
        "timestamp": [b.timestamp for b in bars],
        "open": [float(b.open) for b in bars],
        "high": [float(b.high) for b in bars],
        "low": [float(b.low) for b in bars],
        "close": [float(b.close) for b in bars],
        "volume": [b.volume for b in bars],
        "open_interest": [b.open_interest for b in bars],
    }
    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
        "open_interest": pl.Int64,
    }
    for name in GREEKS_OVERLAY_FIELDS:
        if name == "greeks_provenance_id":
            rows[name] = [getattr(b, name, None) for b in bars]
            schema[name] = pl.Int64
        elif name == "spot_at_bar":
            rows[name] = [None if (v := getattr(b, name, None)) is None else float(v) for b in bars]
            schema[name] = pl.Float64
        else:
            rows[name] = [getattr(b, name, None) for b in bars]
            schema[name] = pl.Float64
    return pl.DataFrame(rows, schema=schema)
