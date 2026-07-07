"""Expired-option Parquet table writer — the concrete `OptionPartitionWriter`.

The physical lake layer the :class:`~brutex.data.option_sink.OptionBarSink`
delegates to: persists a deduped batch of
:class:`~brutex.data.models.OptionBar` rows to a Parquet partition under
``<lake_root>/options/<series>/<YYYY>-<MM>.parquet``. One series (one
``partition_key``) per call; bars spanning month boundaries are split into
their ``(year, month)`` partitions (:func:`~brutex.data.partition.partition_for_timestamp`).

**Idempotent merge-on-write (O(1) per bar amortised).** A 30-day option pull
straddles months and may be re-pulled, so each write **unions** the incoming
bars into the existing partition keyed by ``(side, timestamp)`` — a never-seen
key is added, an identical one is a silent no-op. The hash-keyed
``polars.unique`` makes the merge O(1) amortised per row (the upstream
:class:`~brutex.data.option_dedup.OptionDedupIndex` already guarantees O(1)
content dedup; this is the durable belt-and-braces across index resets).

``write`` returns the count of **newly-written** rows (what the sink reports
as "written"); a pure re-pull returns 0 and rewrites nothing.

Honest envelope (Golden Rule #10): per-bar work is O(1) amortised, but a flush
reads the whole month partition (O(month rows)) to union — inherent to a
month-partition lake (identical to the spot
:class:`~brutex.data.parquet_writer.ParquetWriter`). Vendor-drift detection
(same ``(side, ts)`` with different OHLCV) is deferred — existing rows win on
merge; the dedup index prevents in-run drift, cross-run drift is a tracked
follow-up.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from brutex.data.atomic_io import atomic_write_via, lake_write_guard
from brutex.data.errors import parquet_read_skip_errors
from brutex.data.lake_stats_manifest import (
    KIND_OPTIONS,
    partition_stats_tracked,
    record_partition_stats,
    stats_manifest_enabled,
)
from brutex.data.models import OptionBar
from brutex.data.partition import partition_for_timestamp
from brutex.data.partition_lock import BoundedLockRegistry, partition_write_lock
from brutex.models import BruteXError
from brutex.money import round_money

if TYPE_CHECKING:
    import polars as pl

_LOG = structlog.get_logger("brutex.data.option_parquet_writer")

#: Subdirectory under the lake root holding the expired-options table.
_OPTIONS_SUBDIR: str = "options"


@dataclass(frozen=True, slots=True)
class OptionSeriesCoordinate:
    """The series addressing one rolling-option pull resolves to.

    DEC-EXPIRED-OPTIONS-TABLE-001: the rolling-option request coordinate is
    promoted from the partition *path* into real, self-describing columns on
    every persisted bar. The physical partition path is unchanged
    (``options/<series>/<YYYY>-<MM>.parquet``) — these fields are simply
    written alongside each bar so the expired-options table is queryable
    without decoding the path.

    Attributes
    ----------
    exch_id
        Exchange id (e.g. ``"NSE"``).
    exchange_segment
        Vendor ``exchangeSegment`` (e.g. ``"NSE_FNO"``).
    security_id
        The **underlying** vendor ``SECURITY_ID`` (numeric).
    underlying_symbol
        The underlying's engine symbol (e.g. ``"NIFTY"``).
    expiry_flag
        ``"WEEK"`` / ``"MONTH"``.
    expiry_code
        The expiry selector (Near/Next/Far).
    strike_offset
        The **requested** relative strike selector label (``"ATM"``,
        ``"ATM+3"``, ``"ATM-2"``) — distinct from the per-bar resolved
        absolute ``strike``.
    """

    exch_id: str
    exchange_segment: str
    security_id: int
    underlying_symbol: str
    expiry_flag: str
    expiry_code: int
    strike_offset: str


#: Characters in a partition key replaced to make a filesystem-safe segment.
_UNSAFE: str = "|"
_SAFE: str = "__"

#: Process-wide registry of per-partition write locks (DEC-OPTION-PARTITION-LOCK-001).
#: Mirrors the spot :class:`~brutex.data.parquet_writer.ParquetWriter` registry
#: (DEC-DHAN-PARALLEL-WINDOWS-001). Keyed by the resolved destination ``Path`` so two
#: threads writing the SAME ``options/<series>/<YYYY>-<MM>.parquet`` file serialise their
#: read-modify-write merge, while writers to DIFFERENT partitions never contend. Interned
#: lazily so the map only ever holds locks for partitions actually written. It is keyed by
#: PATH (not writer instance) on purpose: the parallel rolling-option fetch
#: (DEC-DHAN-PARALLEL-INGEST-001) may build one :class:`OptionParquetWriter` per worker, so
#: the lock MUST be shared across instances that resolve to the same on-disk file. This is
#: belt-and-braces below the coarse :class:`~brutex.data.option_sink.OptionBarSink` audit
#: lock: it makes the writer correct on its OWN, so the option write can be parallelised
#: out from under that global serialisation without a lost-update race. The intern is
#: CAPACITY-BOUNDED (:class:`~brutex.data.partition_lock.BoundedLockRegistry`, LRU eviction
#: of idle locks) so the huge option chain (every strike × CE/PE × month) can no longer
#: accumulate a ``Lock`` object per partition forever (GAP #19); an in-flight (refcounted)
#: write keeps its lock, so correctness is independent of the cap.
_PARTITION_LOCKS: BoundedLockRegistry = BoundedLockRegistry()


class OptionWriterError(BruteXError):
    """An expired-option Parquet write failed or got malformed input."""


def _safe_segment(partition_key: str) -> str:
    """Map a ``|``-joined series key to one filesystem-safe path segment.

    Rejects path-traversal / separator characters so a vendor-supplied series
    coordinate can never escape the options table directory.
    """
    if not partition_key:
        raise OptionWriterError("partition_key must be non-empty")
    if "/" in partition_key or "\\" in partition_key or ".." in partition_key:
        raise OptionWriterError(f"unsafe partition_key {partition_key!r}")
    return partition_key.replace(_UNSAFE, _SAFE)


@dataclass(frozen=True, slots=True)
class OptionHealResult:
    """Outcome of one :meth:`OptionParquetWriter.heal_partition` call (DEC-LAKE-HEAL-001).

    Attributes
    ----------
    path
        The options-tree partition file the heal targeted.
    changed
        ``False`` when the file was left byte-untouched (already unique —
        the idempotence oracle).
    kept
        Rows remaining after the keep-first ``(side, timestamp)`` collapse.
    dropped_duplicates
        Physically-duplicated stored rows dropped.
    quarantined_original
        Where the ORIGINAL bytes were copied before the rewrite, or ``None``
        for an unchanged file.
    """

    path: Path
    changed: bool
    kept: int
    dropped_duplicates: int
    quarantined_original: Path | None


class OptionParquetWriter:
    """Writes :class:`OptionBar` rows to the partitioned expired-options table.

    Parameters
    ----------
    lake_root
        Top-level data-lake directory; the options table lives under
        ``<lake_root>/options/``. Created on first write.
    """

    def __init__(self, *, lake_root: Path) -> None:
        self._lake_root = lake_root

    @property
    def lake_root(self) -> Path:
        """Return the configured top-level lake directory."""
        return self._lake_root

    def write(
        self,
        *,
        partition_key: str,
        coordinate: OptionSeriesCoordinate,
        bars: Sequence[OptionBar],
    ) -> int:
        """Persist *bars* for one series; return the count of newly-written rows.

        Bars are split into ``(year, month)`` partitions and each group is
        merged into its partition file keyed by ``(side, timestamp)``. A pure
        re-pull (every bar already stored) writes nothing and returns 0.

        *coordinate* (DEC-EXPIRED-OPTIONS-TABLE-001) carries the series
        addressing fields that are written as real columns on every bar, so
        the expired-options table is self-describing without decoding the
        partition path.
        """
        if not bars:
            return 0
        segment = _safe_segment(partition_key)
        groups: dict[tuple[int, int], list[OptionBar]] = {}
        for bar in bars:
            groups.setdefault(partition_for_timestamp(bar.timestamp), []).append(bar)

        added_total = 0
        for (year, month), group in sorted(groups.items()):
            dest = self._lake_root / _OPTIONS_SUBDIR / segment / f"{year:04d}-{month:02d}.parquet"
            added_total += self._merge_partition(dest, coordinate, group)
        return added_total

    def _merge_partition(
        self, dest: Path, coordinate: OptionSeriesCoordinate, new_bars: list[OptionBar]
    ) -> int:
        """Union *new_bars* into *dest* keyed by ``(side, timestamp)``; return new DISTINCT keys.

        The return is the count of genuinely NEW ``(side, timestamp)`` keys —
        post-``unique`` distinct minus the EXISTING file's distinct count,
        never a physical-row delta (review round 3: the physical baseline
        under-counted on a physically-duplicated partition and silently
        dropped new bars). ``added == 0`` (pure re-pull) writes nothing.

        The existence-check → read → union → atomic-rename below is a
        read-modify-write, so it runs under this partition's shared lock
        (DEC-OPTION-PARTITION-LOCK-001, mirroring the spot writer's
        DEC-DHAN-PARALLEL-WINDOWS-001): when the rolling-option fetch is
        parallelised (DEC-DHAN-PARALLEL-INGEST-001), two windows of ONE series can
        target the SAME month partition (a 30-day option pull straddles a month
        boundary), and without serialisation the later ``os.replace`` would drop the
        other window's bars (lost update). The lock is PER PATH, so writers to
        different partitions never contend; the merge unions by ``(side, timestamp)``,
        so the stored bytes are identical regardless of which window acquires the lock
        first (order-independent — CLAUDE.md §2 Rule 7).
        """
        import polars as pl

        # The render + merge READ + concat are the option lake-write allocation sites
        # (the merge read loads the WHOLE existing partition — the steady-state re-pull
        # case), so they run inside lake_write_guard: a MemoryError / ENOSPC anywhere
        # in the read-modify-write becomes a LakeWriteHalt pull-wide stop, not a raw
        # mid-merge crash (DEC-LAKE-WRITE-HALT-001). The partition lock still serialises
        # same-path writers (DEC-OPTION-PARTITION-LOCK-001), and partition_write_lock
        # extends that serialisation ACROSS PROCESSES (a concurrent CLI fetch + a
        # dashboard pull sharing the lake) so the same month partition can't lost-update
        # (DEC-LAKE-PARTITION-FLOCK-001); the flock is taken BEFORE the write guard so a
        # disk-full at lock creation also maps to LakeWriteHalt.
        with (
            _PARTITION_LOCKS.acquire(dest),
            partition_write_lock(self._lake_root, dest),
            lake_write_guard(),
        ):
            new_df = _render_df(coordinate, new_bars)  # validates the polars import
            if dest.exists():
                # GAP #1: a CORRUPT existing partition (a polars PolarsError, or an
                # OSError fault) must quarantine THIS one series — raising a
                # quarantinable OptionWriterError naming the bad path — not crash the
                # whole option pull. lake_write_guard (above) still maps MemoryError /
                # ENOSPC to a pull-wide LakeWriteHalt, so an OOM here is NOT demoted to a
                # per-contract quarantine; only an unreadable-FILE fault is.
                try:
                    existing = pl.read_parquet(dest)
                except parquet_read_skip_errors() as exc:
                    raise OptionWriterError(f"corrupt existing partition {dest}: {exc}") from exc
                # ``before`` counts DISTINCT ``(side, timestamp)`` keys — NEVER
                # the physical height: on a pre-uniqueness-era poisoned
                # partition (physical duplicate keys) the physical count
                # exceeds the distinct count, so the old
                # ``combined.height - physical_before`` went non-positive even
                # when genuinely NEW keys arrived and the merge silently
                # SKIPPED the write — dropping the new bars for that pull
                # (review round 3; a clean partition has physical == distinct,
                # byte-identical). When new keys DO arrive on a poisoned file,
                # the rewrite keep-first-collapses the physical dups exactly as
                # ``heal_partition`` would (same ``unique`` key + keep rule).
                # int() coercion keeps the value concretely int even in the CI
                # type-check env where polars is NOT installed (`[dev]`-only env, without the `[data]` extra)
                # — there the polars return is Any (ignore_missing_imports) and an
                # un-coerced value would trip mypy --strict's no-any-return.
                before = int(existing.n_unique(subset=["side", "timestamp"]))
                combined = (
                    pl.concat([existing, new_df])
                    .unique(subset=["side", "timestamp"], keep="first", maintain_order=True)
                    .sort(["side", "timestamp"])
                )
            else:
                before = 0
                combined = new_df.unique(
                    subset=["side", "timestamp"], keep="first", maintain_order=True
                ).sort(["side", "timestamp"])
            added = int(combined.height) - before
            if added > 0:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_parquet(dest, combined)
                # Stats-manifest upsert (DEC-LAKE-STATS-MANIFEST-001): bytes
                # were RENDERED, so the shard — when one exists (lazy
                # accretion) — is re-stamped under the still-held lock stack.
                # The ``added == 0`` pure re-pull writes nothing and therefore
                # touches no shard (the existing row stays stat-valid).
                _record_option_stats(self._lake_root, dest=dest, frame=combined)
            return added

    def heal_partition(self, dest: Path, *, quarantine_to: Path | None = None) -> OptionHealResult:
        """Heal ONE options-tree partition: keep-FIRST ``(side, timestamp)`` dedup (DEC-LAKE-HEAL-001).

        The options twin of :meth:`brutex.data.parquet_writer.ParquetWriter.heal_partition`,
        under this writer's OWN established lock stack + ``lake_write_guard``
        (the :meth:`_merge_partition` discipline): read the stored frame,
        collapse to unique ``(side, timestamp)`` keys keeping the FIRST physical
        row (``polars.unique(keep="first", maintain_order=True)`` — the writer's
        own merge key), and — only when rows were dropped — quarantine-copy the
        ORIGINAL bytes then atomically rewrite. No P9 tripwire: options files
        sort by ``(side, timestamp)``, not strictly-increasing timestamps.
        STRICTLY row-removing — kept rows keep their stored values.

        Parameters
        ----------
        dest
            The ``options/<series>/<YYYY>-<MM>.parquet`` file to heal.
        quarantine_to
            Where the ORIGINAL bytes are copied before the rewrite. Required
            whenever the heal would change the file
            (:class:`OptionWriterError` otherwise).

        Returns
        -------
        OptionHealResult
            The heal outcome (``changed=False`` = byte-untouched).

        Raises
        ------
        OptionWriterError
            Vanished/corrupt file, or a changing heal with no *quarantine_to*.
        LakeWriteHalt
            Disk-full / OOM during the quarantine copy or the rewrite.
        PartitionLockTimeout
            The cross-process lock could not be acquired in time.
        """
        import io

        import polars as pl

        with (
            _PARTITION_LOCKS.acquire(dest),
            partition_write_lock(self._lake_root, dest),
            lake_write_guard(),
        ):
            try:
                existing_bytes = dest.read_bytes()
                existing = pl.read_parquet(io.BytesIO(existing_bytes))
            except parquet_read_skip_errors() as exc:
                raise OptionWriterError(f"corrupt existing partition {dest}: {exc}") from exc
            physical = int(existing.height)
            deduped = existing.unique(
                subset=["side", "timestamp"], keep="first", maintain_order=True
            ).sort(["side", "timestamp"])
            kept = int(deduped.height)
            if kept == physical:
                return OptionHealResult(
                    path=dest,
                    changed=False,
                    kept=kept,
                    dropped_duplicates=0,
                    quarantined_original=None,
                )
            if quarantine_to is None:
                raise OptionWriterError(
                    f"heal of {dest} requires a quarantine destination — "
                    f"refusing to rewrite without the safety copy"
                )
            quarantine_to.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_via(quarantine_to, lambda tmp: tmp.write_bytes(existing_bytes))
            _atomic_write_parquet(dest, deduped)
            # Stats-manifest re-stamp (DEC-LAKE-STATS-MANIFEST-001): the heal
            # rewrite rendered new bytes — upsert the row (post-dedup distinct
            # count + fresh stat token) under the SAME held locks. The
            # unchanged branch above returns earlier and touches nothing.
            _record_option_stats(self._lake_root, dest=dest, frame=deduped)
            return OptionHealResult(
                path=dest,
                changed=True,
                kept=kept,
                dropped_duplicates=physical - kept,
                quarantined_original=quarantine_to,
            )


def _money(value: Decimal) -> float:
    """Round a money :class:`~decimal.Decimal` to 2dp (DEC-MONEY-2DP-001) → float."""
    return float(round_money(value))


def _opt_money(value: Decimal | None) -> float | None:
    """2dp-round an optional money field; ``None`` stays ``None``."""
    return None if value is None else _money(value)


def _render_df(coordinate: OptionSeriesCoordinate, bars: Sequence[OptionBar]) -> pl.DataFrame:
    """Build a typed polars DataFrame from *coordinate* + *bars*.

    The 7 series-addressing columns (``exch_id`` / ``exchange_segment`` /
    ``security_id`` / ``underlying_symbol`` / ``expiry_flag`` / ``expiry_code``
    / ``strike_offset``) are promoted from the partition path onto every bar
    (DEC-EXPIRED-OPTIONS-TABLE-001), constant within a partition file.

    Money fields (``open``/``high``/``low``/``close``/``strike``/``spot``) are
    rounded to **2 decimals** (DEC-MONEY-2DP-001, CLAUDE.md §4). ``iv`` is a
    volatility metric, **NOT money** — it keeps full precision (rounding it
    would corrupt downstream analytics, per the same rule).
    """
    try:
        import polars as pl
    except ImportError as exc:  # pragma: no cover — exercised only without the data extra
        raise OptionWriterError(
            "OptionParquetWriter requires 'polars' — install '.[data]' for the lake"
        ) from exc

    count = len(bars)
    rows = {
        # ── series addressing (promoted from the partition path) ──
        "exch_id": [coordinate.exch_id] * count,
        "exchange_segment": [coordinate.exchange_segment] * count,
        "security_id": [coordinate.security_id] * count,
        "underlying_symbol": [coordinate.underlying_symbol] * count,
        "expiry_flag": [coordinate.expiry_flag] * count,
        "expiry_code": [coordinate.expiry_code] * count,
        "side": [b.side for b in bars],
        "strike_offset": [coordinate.strike_offset] * count,
        # ── per-bar value fields ──
        "timestamp": [b.timestamp for b in bars],
        "open": [_money(b.open) for b in bars],
        "high": [_money(b.high) for b in bars],
        "low": [_money(b.low) for b in bars],
        "close": [_money(b.close) for b in bars],
        "volume": [b.volume for b in bars],
        "open_interest": [b.open_interest for b in bars],
        "strike": [_opt_money(b.strike) for b in bars],
        "spot": [_opt_money(b.spot) for b in bars],
        "iv": [None if b.iv is None else float(b.iv) for b in bars],  # NOT money — full precision
    }
    # polars infers timestamp→Datetime(us, UTC), prices→Float64, side→Utf8.
    # An all-None batch infers a column as Null, so pin the nullable fields to
    # their stable lake dtypes (cast is a no-op when already typed).
    return pl.DataFrame(rows).with_columns(
        pl.col("security_id").cast(pl.Int64),
        pl.col("expiry_code").cast(pl.Int64),
        pl.col("open_interest").cast(pl.Int64),
        pl.col("strike").cast(pl.Float64),
        pl.col("spot").cast(pl.Float64),
        pl.col("iv").cast(pl.Float64),
    )


def _record_option_stats(lake_root: Path, *, dest: Path, frame: pl.DataFrame) -> None:
    """Best-effort stats-manifest upsert after an options-tree partition render.

    DEC-LAKE-STATS-MANIFEST-001 write hook (the options twin of
    ``ParquetWriter._record_stats_hook``): called by
    :meth:`OptionParquetWriter._merge_partition` (inside its ``added > 0``
    branch) and by the :meth:`OptionParquetWriter.heal_partition` rewrite —
    always INSIDE the still-held partition lock stack, so the ``os.stat``
    token is the durable file's. ``rows`` is *frame*'s height — already the
    post-``unique`` DISTINCT ``(side, timestamp)`` count — with bounds from
    the rendered frame and ``raw_hash=None`` (the options writer computes
    none; forensics-only field). The dataset key is the on-disk series dir
    name VERBATIM (``{"series": dest.parent.name}``). LAZY ACCRETION, probed
    FIRST (:func:`~brutex.data.lake_stats_manifest.partition_stats_tracked`,
    review round 2): an unviewed series' write costs exactly ONE cheap O(1)
    probe — a shard-path key digest + one failed ``os.stat`` — and never runs
    the O(frame) bounds fold or the ``os.stat(dest)``. When the shard exists,
    delegates to the best-effort
    :func:`~brutex.data.lake_stats_manifest.record_partition_stats` — a shard
    failure can never raise into the pull (a
    :class:`~brutex.data.errors.LakeWriteHalt` from ``.locks/`` creation on a
    genuinely full disk deliberately propagates). Kill switch off ⇒ pure
    no-op (not even the probe). A frame whose timestamp bounds are not
    datetimes (defensive — an empty/null column) or a vanished-file stat is
    logged and skipped.
    """
    if not stats_manifest_enabled():
        return
    key = {"series": dest.parent.name}
    if not partition_stats_tracked(lake_root, KIND_OPTIONS, key):
        # Lazy accretion, probe-FIRST (review round 2): an unviewed series
        # pays only this probe — the O(frame) min/max fold and the
        # os.stat(dest) below never run; seeding stays read-side only.
        return
    min_ts = frame["timestamp"].min()
    max_ts = frame["timestamp"].max()
    if not isinstance(min_ts, datetime) or not isinstance(max_ts, datetime):
        _LOG.warning("lake.stats_manifest.bounds_underivable", partition=str(dest))
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
        lake_root,
        kind=KIND_OPTIONS,
        key=key,
        dest=dest,
        rows=int(frame.height),
        min_ts=min_ts,
        max_ts=max_ts,
        raw_hash=None,
        stat=stat,
    )


def _atomic_write_parquet(dest: Path, df: pl.DataFrame) -> None:
    """Write *df* to *dest* atomically via the shared atomic_io helper.

    Delegates to :func:`~brutex.data.atomic_io.atomic_write_via` (CLAUDE.md §11
    COMMON — one tmp→fsync→``os.replace``→cleanup implementation shared with the spot
    :class:`~brutex.data.parquet_writer.ParquetWriter`): the tmp file is ``fsync``-ed
    to stable storage BEFORE the rename (``os.replace`` orders only the metadata, not
    the page cache, so a crash between the two could otherwise leave ``dest`` pointing
    at a truncated parquet). The helper additionally maps a full disk (``ENOSPC``) or
    an OOM (``MemoryError`` from ``df.write_parquet``) to a
    :class:`~brutex.data.errors.LakeWriteHalt` pull-wide stop (DEC-LAKE-WRITE-HALT-001).
    """
    atomic_write_via(dest, df.write_parquet)


__all__ = [
    "OptionHealResult",
    "OptionParquetWriter",
    "OptionSeriesCoordinate",
    "OptionWriterError",
]
