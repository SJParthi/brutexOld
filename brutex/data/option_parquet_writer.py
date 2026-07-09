"""Expired-option Parquet table writer — the concrete `OptionPartitionWriter`.

The physical lake layer for the expired-option ``options/`` tree (today driven
by the lake healer's :meth:`OptionParquetWriter.heal_partition` — the prior-vendor-era
``option_sink.OptionBarSink`` orchestration above it was DELETED as dead code by
DEC-INGEST-AUDIT-WIRING-001; the Groww F&O pull writes candles through
:class:`~brutex.data.sink.ParquetBarSink` instead): persists a deduped batch of
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

The lake healer's option-tree primitives live here too
(DEC-LAKE-HEAL-001 dedup; DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 foreign-row
reassignment): :meth:`OptionParquetWriter.heal_partition` (keep-first dedup +
RAW keyed removal + the wholly-removed quarantine-MOVE),
:meth:`OptionParquetWriter.read_partition_frame` (the healer's truthful raw
consistent read) and :meth:`OptionParquetWriter.merge_partition_frame` (the
reassignment's keep-first write-home entry) — all through the SAME lock stack
as :meth:`OptionParquetWriter.write`. ALL THREE heal-side entries — the
source read (:meth:`read_partition_frame`, round-4 fold) and both mutations —
enforce the symlink-containment rail
(:meth:`OptionParquetWriter._assert_options_containment`, hostile-review
round-2 MAJOR + round-6 MINOR folds): the RESOLVED path must stay inside
``<lake_root>/options/`` AND neither the partition file nor its series dir
may itself be a symlink (a swap whose target stays INSIDE ``options/``
passes resolved containment yet redirects the heal across series), or the
call is REFUSED with :class:`OptionWriterError` — the scan-time symlink
skip alone is bypassable (a symlinked ``options/`` root, a series dir OR
partition file swapped to a symlink between scan and heal, in-lake or out),
and only these heal-time checks close that window.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
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
    remove_partition_stats,
    stats_manifest_enabled,
)
from brutex.data.models import OptionBar
from brutex.data.partition import option_partition_stem, partition_for_timestamp
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
#: belt-and-braces that makes the writer correct on its OWN (originally below the
#: now-DELETED prior-vendor-era ``option_sink.OptionBarSink`` audit lock), so an option write
#: can be parallelised without a lost-update race. The intern is
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
        ``False`` when the file was left byte-untouched (already unique AND
        no ``remove_keys`` entry present — the idempotence oracle); ``True``
        iff ``dropped_duplicates > 0 or dropped_removed > 0``.
    kept
        Rows remaining after the keep-first ``(side, timestamp)`` collapse +
        the keyed removal (``0`` when the file was quarantine-moved away —
        every row removed, DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    dropped_duplicates
        Physically-duplicated stored rows dropped.
    quarantined_original
        Where the ORIGINAL bytes were copied before the rewrite, or ``None``
        for an unchanged file.
    dropped_removed
        ``remove_keys`` entries actually present and dropped (the foreign-row
        reassignment hook, DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    """

    path: Path
    changed: bool
    kept: int
    dropped_duplicates: int
    quarantined_original: Path | None
    dropped_removed: int = 0


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
            dest = (
                self._lake_root
                / _OPTIONS_SUBDIR
                / segment
                / f"{option_partition_stem(year, month)}.parquet"
            )
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

        Thin delegate to :meth:`_merge_frame_locked` — the render callable runs
        INSIDE ``lake_write_guard`` (its allocation keeps the ENOSPC/OOM →
        ``LakeWriteHalt`` mapping). For a UNIQUE-keyed destination — the only
        state ``write`` itself ever produces — the pre-refactor contract is
        preserved byte-for-byte (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001, golden
        byte-parity pinned). A dup-POISONED destination (plantable only by
        hand) deliberately DIVERGES from the pre-refactor behaviour: the F1
        fold baselines ``added`` on the UNIQUE stored key count and
        force-rewrites the keep-first collapse on touch (the pre-refactor
        physical-row baseline could cancel new keys — review round-2 NIT:
        the old "byte-for-byte" claim was overbroad).
        """
        return self._merge_frame_locked(dest, lambda: _render_df(coordinate, new_bars))

    def merge_partition_frame(self, dest: Path, frame: pl.DataFrame) -> int:
        """Union *frame* into *dest* keyed ``(side, timestamp)`` through the NORMAL
        locked write path; return rows actually added.

        The reassignment heal's write-home entry
        (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): keep-FIRST at the destination —
        an existing stored key WINS (a foreign overlap copy adds 0 rows and is
        thereby discarded when its source copy is removed; canonical wins), an
        absent key is added (row REASSIGNED). Rows move VERBATIM (all stored
        columns) — never re-validated, never re-rendered, never value-edited.
        The returned count is baselined on the destination's UNIQUE stored key
        set (a dup-poisoned destination can never cancel a genuinely-new moved
        key), and a poisoned destination is force-rewritten keep-first-deduped
        on touch (``lake.partition.dedup_healed`` — the bars twin's law).
        Enforces the symlink-containment rail at the mutation boundary
        (:meth:`_assert_options_containment` — heal-side entries only; the
        production :meth:`write` path is deliberately unguarded, see there).

        Raises
        ------
        OptionWriterError
            A corrupt existing destination partition, or a *dest* whose
            RESOLVED path escapes ``<lake_root>/options/`` (a symlinked
            series dir / options root — the round-2 containment rail), or a
            *dest* that is / sits behind a symlink even when its target
            stays in-lake (the round-6 swap rail).
        LakeWriteHalt
            Disk-full / OOM anywhere inside the read-modify-write.
        PartitionLockTimeout
            The cross-process lock could not be acquired in time.
        """
        return self._merge_frame_locked(dest, lambda: frame, containment=True)

    def _assert_options_containment(self, dest: Path) -> None:
        """Refuse a heal-side call whose *dest* escapes ``<lake_root>/options/`` OR is symlinked.

        Guards BOTH the two heal-side mutation entries AND the healer's source
        consistent read (:meth:`read_partition_frame`, round-4 fold — a
        followed read imports outside rows into the lake via the copy-forward
        leg even when every WRITE stays contained).

        TWO rails, both enforced at heal time under the partition locks:
        the round-2 ESCAPE rail (resolved containment, below) and the
        round-6 IN-LAKE swap rail — *dest* and its series-dir parent must
        not themselves be symlinks, because a swap whose target stays
        INSIDE ``options/`` (another series' file) passes resolved
        containment yet redirects the heal across series (cross-series
        contamination, refuter-reproduced). Together: a scan→heal swap of
        the flagged file or its series dir to ANY symlink — in-lake or
        out — is refused as the isolated :class:`OptionWriterError`.

        The mutation-boundary symlink rail (hostile-review round-2 MAJOR,
        DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): the scanner's ``is_symlink()``
        skip is scan-TIME only and bypassable two ways — a symlinked
        ``options/`` root (the series dirs inside the target are REAL dirs,
        so the per-series probe never fires) and a scan→heal TOCTOU (a real
        series dir swapped to a symlink after the scan) — both empirically
        reproduced letting the default AUTO-ON heal create/unlink parquet
        files OUTSIDE the lake root. Enforced HERE, at heal time, on the
        RESOLVED path: ``dest``'s resolved parent must sit under
        ``<resolved lake_root>/options``. The ``options`` component itself is
        deliberately NOT symlink-resolved — resolving it would re-anchor the
        containment root to wherever a swapped-in symlink points, re-opening
        the hole; a lake ROOT reached via a symlink (a legitimate mount
        shape) still passes because BOTH sides resolve consistently
        (test-pinned). Raises the quarantinable :class:`OptionWriterError`
        (a member of the healer's per-finding isolation set — the refusal is
        counted + logged, the batch continues, nothing is mutated). The
        ``resolve()`` calls themselves are GUARDED (hostile-review round-5
        MAJOR fold): on Python ≤ 3.12 ``Path.resolve(strict=False)`` raises a
        bare ``RuntimeError("Symlink loop from …")`` for a symlink-LOOP
        *dest* (3.13+ raises ``OSError`` ``ELOOP``), and ``RuntimeError`` is
        NOT in the healer's per-finding isolation set — pre-fix ONE planted
        loop as a reassignment TARGET escaped the isolation loop and aborted
        the WHOLE heal batch (no report, no stats, no alert, no audit-batch
        line; the exact isolation-escape class the round-3 zero-row fold
        eliminated). An unresolvable *dest* now takes the SAME documented
        isolated refusal as an escaping one. Honest
        envelope (Golden Rule #10): this closes the demonstrated scan→heal
        windows — the escape-the-lake vectors (rounds 2/4) AND the round-6
        in-lake swap; a sub-microsecond swap between these checks and the
        atomic rename inside the SAME call would need dirfd-relative I/O to
        close — out of scope, not claimed.
        """
        try:
            options_root = self._lake_root.resolve() / _OPTIONS_SUBDIR
            resolved_parent = dest.resolve().parent
        except (OSError, RuntimeError) as exc:
            # Symlink loop / unresolvable path: RuntimeError on Python ≤3.12,
            # OSError (ELOOP) on 3.13+ — either way the documented isolated
            # refusal, never a batch-aborting escape (round-5 MAJOR fold).
            raise OptionWriterError(
                f"cannot resolve {dest} for the options-containment check: {exc} "
                f"— refusing to touch it"
            ) from exc
        if not resolved_parent.is_relative_to(options_root):
            raise OptionWriterError(
                f"destination {dest} resolves outside the options tree "
                f"({resolved_parent} is not under {options_root}) — refusing to mutate"
            )
        if dest.is_symlink() or dest.parent.is_symlink():
            # Round-6 IN-LAKE swap rail (refuter-confirmed MINOR): the escape
            # check above cannot see a symlink whose TARGET sits INSIDE
            # options/ — a flagged partition file (or its series dir) swapped
            # post-scan to a symlink at ANOTHER series' file passed
            # containment, so the heal read the other series' rows through
            # the link and the copy-forward leg imported them into the
            # flagged series (cross-series contamination — empirically
            # reproduced). The healer only ever targets REAL partition files
            # in REAL series dirs (the scanner symlink-skips both at scan
            # time), so ANY symlink at either component is a swap — refused.
            # Ancestors ABOVE the series dir are deliberately not probed: a
            # symlinked lake ROOT (the legitimate mount shape) must keep
            # passing, and an in-lake-redirected `options` component already
            # fails the escape check (options_root pins the literal name).
            raise OptionWriterError(
                f"destination {dest} is or sits behind a symlink inside the "
                f"options tree — the healer only targets real partition files "
                f"in real series dirs; refusing to touch it"
            )

    def _merge_frame_locked(
        self, dest: Path, render: Callable[[], pl.DataFrame], *, containment: bool = False
    ) -> int:
        """The ONE read-modify-write union core (locks → guard → render → union → write).

        *containment* runs :meth:`_assert_options_containment` under the locks
        before any read/write — ``True`` for the heal-side
        :meth:`merge_partition_frame` entry, ``False`` (default) for the
        production :meth:`write` → :meth:`_merge_partition` path, which is
        deliberately unguarded: ``_safe_segment`` already rejects traversal
        characters, the pull writes only paths it itself derives, and an
        operator who symlinks the options tree to another volume on purpose
        must keep a working pull (the healer refuses instead — scan-time skip
        + this rail — so the auto-heal never mutates through that layout).

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

        Cost honesty (Golden Rule #10, review round-15 NIT): the F1
        unique-stored-key baseline costs the production merge ONE extra
        O(month-rows) ``unique()`` pass per flush on top of the union — a
        constant factor over the O(month-rows) read-modify-write this method
        already is, accepted un-benched (the merge read itself dominates).
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
            if containment:
                self._assert_options_containment(dest)
            new_df = render()  # validates the polars import on the write path
            dedup_dropped = 0
            if dest.exists():
                # GAP #1: a CORRUPT existing partition (a polars PolarsError, or an
                # OSError fault) must quarantine THIS one series — raising a
                # quarantinable OptionWriterError naming the bad path — not crash the
                # whole option pull. lake_write_guard (above) still maps MemoryError /
                # ENOSPC to a pull-wide LakeWriteHalt, so an OOM here is NOT demoted to a
                # per-contract quarantine; only an unreadable-FILE fault is.
                try:
                    # glob=False (hostile-review round-15 MAJOR fold): polars'
                    # default glob=True treats the PATH as a glob, and
                    # ``_safe_segment`` legally admits glob metachars in a
                    # series dir name (it rejects only / \ .. and maps |) — a
                    # merge into ``options/X[12]/…`` glob-matched the REAL
                    # sibling series X1 + X2 as "existing", so the rewrite
                    # PERMANENTLY DROPPED the literal partition's own stored
                    # rows (no quarantine — this leg never quarantines),
                    # IMPORTED the siblings' rows cross-series, and falsely
                    # emitted ``dedup_healed`` against them (empirically
                    # reproduced through the default AUTO-ON healer's
                    # copy-forward leg AND the production ``write()`` path —
                    # no symlink/TOCTOU needed, so the containment rail
                    # cannot see it). The literal read closes both.
                    existing = pl.read_parquet(dest, glob=False)
                except parquet_read_skip_errors() as exc:
                    raise OptionWriterError(f"corrupt existing partition {dest}: {exc}") from exc
                # `added` is baselined on the UNIQUE stored key count, NEVER the raw
                # physical row count (the bars twin's `stored_unique` law,
                # DEC-LAKE-WRITE-UNIQUE-001): a dup-POISONED destination collapses
                # inside the keep-first union below, and a physical-row baseline would
                # cancel genuinely-new keys against that collapse — `added == 0` would
                # then SKIP the rewrite and a reassignment heal's moved key would land
                # NOWHERE while the source copy is removed (silent row LOSS — the
                # DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 hostile-review BLOCKER; the same
                # defect was independently found + fixed by #1228's review round 3 —
                # `n_unique` here is its cheaper equivalent of
                # `unique(subset).height`). int() coercion keeps the return
                # concretely int even in the CI type-check env where polars is NOT
                # installed (`[dev]`-only env, without the `[data]` extra) — there
                # the polars return is Any (ignore_missing_imports) and an
                # un-coerced return would trip mypy --strict's no-any-return.
                before = int(existing.n_unique(subset=["side", "timestamp"]))
                dedup_dropped = int(existing.height) - before
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
            if dedup_dropped > 0:
                # The stored destination physically carried duplicate (side, timestamp)
                # keys — the rewrite below persists the keep-first collapse even when
                # the merge itself adds nothing (the bars `heal_dropped` forced-rewrite
                # mirror, DEC-LAKE-WRITE-UNIQUE-001), so a poisoned destination
                # self-heals on touch instead of silently returning poisoned bytes.
                # Same pinned event as the bars writer; sanctioned no-quarantine
                # self-heal (the merge is not a heal_partition call — the collapse is
                # observable here). HONEST (review round-2 NIT): in the reassignment
                # flow this collapse is the ONLY dedup a dup-poisoned DESTINATION gets
                # in that batch — option-foreign heals BEFORE option-dup, so a later
                # same-file dup finding reads changed=False → counted skipped, and NO
                # quarantine copy of the destination's pre-collapse bytes is taken
                # (rows are keep-first-dropped, never value-edited; the next scan
                # sees the file clean). Only a dup finding healed with NO same-batch
                # reassignment touching its file goes through heal_partition's
                # quarantine-copy path. partition= rides repr() (round-8
                # log-injection fold, superseding the round-7 "keeps its
                # bars-twin str(dest) shape" exclusion): dest embeds the
                # vendor-influenceable SERIES dir name (``_safe_segment``
                # strips no control chars), unlike the bars writer's
                # catalogued-coordinate dest, which stays raw. Event name +
                # field names are unchanged — only this value's escaping.
                _LOG.warning(
                    "lake.partition.dedup_healed",
                    partition=repr(str(dest)),
                    kept=before,
                    dropped=dedup_dropped,
                )
            if added > 0 or dedup_dropped > 0:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_parquet(dest, combined)
                # Stats-manifest upsert (DEC-LAKE-STATS-MANIFEST-001): bytes
                # were RENDERED, so the shard — when one exists (lazy
                # accretion) — is re-stamped under the still-held lock stack.
                # The ``added == 0`` pure re-pull writes nothing and therefore
                # touches no shard (the existing row stays stat-valid).
                _record_option_stats(self._lake_root, dest=dest, frame=combined)
            return added

    def read_partition_frame(self, dest: Path) -> pl.DataFrame:
        """Read one stored options partition RAW (physical rows preserved) under the write locks.

        The options twin of :meth:`ParquetWriter.read_partition_bars`
        (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): the reassignment heal needs the
        stored rows of a SOURCE partition before moving them home. Takes the
        SAME two locks in :meth:`write`'s order (no ``lake_write_guard`` —
        read-only) so the read can never interleave a concurrent
        read-modify-write; a vanished/corrupt file raises the quarantinable
        :class:`OptionWriterError`. Deliberately NO keep-first collapse here —
        the healer dedups explicitly, keeping this method a truthful raw read.
        Enforces the symlink-containment rail too (round-4 fold): a read is
        not a mutation, but it is the reassignment heal's INPUT — pre-fix a
        symlinked source partition FILE was followed OUTSIDE the lake and the
        copy-forward leg IMPORTED the outside rows into an in-lake canonical
        file (``dest.parent`` is a REAL series dir, so the write-leg rail
        passed); refusing at the read (:class:`OptionWriterError`, isolated
        counted error) keeps outside bytes out of the lake entirely. The
        round-6 rail extends the SAME refusal to an IN-lake swap target (a
        symlink at another series' file), keeping rows from crossing series
        through a followed read.
        """
        import io

        import polars as pl

        with (
            _PARTITION_LOCKS.acquire(dest),
            partition_write_lock(self._lake_root, dest),
        ):
            self._assert_options_containment(dest)
            try:
                return pl.read_parquet(io.BytesIO(dest.read_bytes()))
            except parquet_read_skip_errors() as exc:
                raise OptionWriterError(f"corrupt existing partition {dest}: {exc}") from exc

    def heal_partition(
        self,
        dest: Path,
        *,
        remove_keys: AbstractSet[tuple[str | None, datetime]] = frozenset(),
        quarantine_to: Path | None = None,
    ) -> OptionHealResult:
        """Heal ONE options-tree partition: keep-FIRST ``(side, timestamp)`` dedup + keyed removal.

        The options twin of :meth:`brutex.data.parquet_writer.ParquetWriter.heal_partition`
        (DEC-LAKE-HEAL-001 dedup; *remove_keys* + the wholly-removed move added
        by DEC-LAKE-HEAL-OPTIONS-FOREIGN-001), under this writer's OWN
        established lock stack + ``lake_write_guard`` (the
        :meth:`_merge_frame_locked` discipline): read the stored frame,
        collapse to unique ``(side, timestamp)`` keys keeping the FIRST physical
        row (``polars.unique(keep="first", maintain_order=True)`` — the writer's
        own merge key), drop every *remove_keys* row by RAW stored-value
        membership, and — only when rows were dropped — quarantine-copy the
        ORIGINAL bytes then atomically rewrite (or, when EVERY row was removed,
        quarantine-MOVE the file: ``unlink`` + prune a now-emptied series dir).
        No P9 tripwire: options files sort by ``(side, timestamp)``, not
        strictly-increasing timestamps. No footer probe / probe-abstain branch
        either — the bars probe exists because its overlay decode collapses
        dups before counting (the #1067 "never guess" pin); this read keeps the
        RAW frame, so ``frame.height`` IS the physical count and there is
        nothing to guess. STRICTLY row-removing — kept rows keep their stored
        values. Removal is KEYED, never positional: a row appended between the
        healer's consistent read and this removal is untouched and re-found by
        the next scan. Raw stored-value membership is immune to the
        naive-vs-aware dtype mismatch a hand-planted naive ``timestamp`` column
        would give a tz-cast join. Deliberately NO ``invalidate_manifest`` on
        the wholly-removed move — the dataset manifest records ``bars/``
        coordinates only (this module has no ``lake_manifest`` import); ``/db``
        options discovery walks ``options/`` directly and rides the
        ``on_healed`` generation bump.

        Parameters
        ----------
        dest
            The ``options/<series>/<YYYY>-<MM>.parquet`` file to heal.
        remove_keys
            Stored ``(side, timestamp)`` keys to DROP (whole rows) — the
            reassignment heal moves them to their canonical month file first,
            then removes them here. Absent keys are silently ignored (count
            only what was present). ``side`` may be ``None`` for a
            hand-planted null-side row (key payload, never routing input).
        quarantine_to
            Where the ORIGINAL bytes are copied before the rewrite. Required
            whenever the heal would change the file
            (:class:`OptionWriterError` otherwise). Honest envelope
            (round-17): this method writes the before-image WHEREVER
            *quarantine_to* points — the containment rail here guards *dest*
            only; the batch-level symlinked-``.quarantine`` refusal lives in
            :func:`brutex.data.lake_heal.heal_findings`, which derives every
            *quarantine_to* the production healer passes.

        Returns
        -------
        OptionHealResult
            The heal outcome (``changed=False`` = byte-untouched; ``kept=0``
            with ``changed=True`` = the file was quarantine-moved away).

        Raises
        ------
        OptionWriterError
            Vanished/corrupt file, a changing heal with no *quarantine_to*,
            a *dest* whose RESOLVED path escapes ``<lake_root>/options/``,
            a symlinked *dest* / series dir even when the target stays
            in-lake (:meth:`_assert_options_containment` — the round-2 +
            round-6 rails; this method unlinks/rewrites, so it must never
            follow a symlink out of the lake OR across series), or — when
            *remove_keys* is given — a re-read frame whose key columns carry
            a nested/unclassifiable dtype
            (:func:`~brutex.data.lake_invariants.option_key_dtype_error`,
            the round-13 twin of the healer's consistent-read gate: the
            keyed removal hashes raw stored values, so an ungated swapped
            frame raised bare ``TypeError`` past the per-finding isolation).
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
            self._assert_options_containment(dest)
            try:
                existing_bytes = dest.read_bytes()
                existing = pl.read_parquet(io.BytesIO(existing_bytes))
            except parquet_read_skip_errors() as exc:
                raise OptionWriterError(f"corrupt existing partition {dest}: {exc}") from exc
            physical = int(existing.height)
            deduped = existing.unique(
                subset=["side", "timestamp"], keep="first", maintain_order=True
            ).sort(["side", "timestamp"])
            dropped_duplicates = physical - int(deduped.height)
            dropped_removed = 0
            if remove_keys:
                # Round-13 fold (refuter-confirmed): this method RE-reads the
                # source under its OWN locks, so the round-12 dtype gate on the
                # healer's earlier consistent read left THIS read ungated — a
                # nested-key-dtype (List/Struct) swap landing AFTER that gated
                # read (a WIDE window: the per-month copy-forward merge legs
                # run in between) reached the Python-hash membership below and
                # raised bare ``TypeError: unhashable type`` — NOT
                # ``OptionWriterError``, so it escaped the healer's
                # per-finding isolation set and aborted the WHOLE heal batch.
                # Same gate as the scanner + the healer (§11 COMMON), applied
                # to the frame THIS method actually hashes. Function-local
                # import — ``lake_invariants`` imports nothing from this
                # module (no cycle) and stays off the writer's import path.
                from brutex.data.lake_invariants import (
                    option_key_dtype_error,
                    option_unclassifiable_message,
                )

                dtype_error = option_key_dtype_error(deduped)
                if dtype_error is not None:
                    raise OptionWriterError(option_unclassifiable_message(dest, dtype_error))
                # RAW stored-value boolean-mask removal (never a tz-cast join):
                # exact membership against the stored (side, timestamp) values.
                keyset = set(remove_keys)
                sides = deduped.get_column("side").to_list()
                stamps = deduped.get_column("timestamp").to_list()
                mask = [(s, t) not in keyset for s, t in zip(sides, stamps, strict=True)]
                if not all(mask):
                    before_remove = int(deduped.height)
                    deduped = deduped.filter(pl.Series(mask))
                    dropped_removed = before_remove - int(deduped.height)
            kept = int(deduped.height)
            if dropped_duplicates == 0 and dropped_removed == 0:
                return OptionHealResult(
                    path=dest,
                    changed=False,
                    kept=kept,
                    dropped_duplicates=0,
                    quarantined_original=None,
                    dropped_removed=0,
                )
            if quarantine_to is None:
                raise OptionWriterError(
                    f"heal of {dest} requires a quarantine destination — "
                    f"refusing to rewrite without the safety copy"
                )
            quarantine_to.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_via(quarantine_to, lambda tmp: tmp.write_bytes(existing_bytes))
            if kept == 0:
                # Every row removed (a wholly-foreign file): quarantine-MOVE —
                # the writer never renders an empty partition (write()'s law).
                # The series-dir prune is DEFENSIVE: in the reassignment flow
                # the destination file lives in the SAME series dir, so the
                # rmdir is suppressed there; a direct writer call on a
                # single-file series does prune it.
                dest.unlink()
                with contextlib.suppress(OSError):
                    dest.parent.rmdir()
                # Stats-manifest row removal (DEC-LAKE-STATS-MANIFEST-001):
                # the options twin of the bars writer's ``_remove_stats_hook``
                # at ITS quarantine-MOVE branch — the unlinked file's shard
                # row is dropped under the SAME held locks (best-effort;
                # the last row's removal unlinks the whole shard). Without
                # it the shard would fail the read-side file-set gate on
                # every consult until a reseed (safe — fallback, never a
                # wrong answer — but defeats the manifest for the series).
                _remove_option_stats(self._lake_root, dest=dest)
                return OptionHealResult(
                    path=dest,
                    changed=True,
                    kept=0,
                    dropped_duplicates=dropped_duplicates,
                    quarantined_original=quarantine_to,
                    dropped_removed=dropped_removed,
                )
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
                dropped_duplicates=dropped_duplicates,
                quarantined_original=quarantine_to,
                dropped_removed=dropped_removed,
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
    :meth:`OptionParquetWriter._merge_frame_locked` (inside its rewrite
    branch — ``added > 0`` OR the ``dedup_dropped > 0`` forced dedup
    rewrite, so a merge-touch self-heal re-stamps the row too) and by the
    :meth:`OptionParquetWriter.heal_partition` rewrite —
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


def _remove_option_stats(lake_root: Path, *, dest: Path) -> None:
    """Best-effort stats-manifest row removal after a wholly-removed unlink.

    DEC-LAKE-STATS-MANIFEST-001 heal hook — the options twin of
    ``ParquetWriter._remove_stats_hook``: called by the
    :meth:`OptionParquetWriter.heal_partition` wholly-removed
    quarantine-MOVE branch (``kept == 0`` — the file was unlinked), under
    the SAME held partition lock stack. Delegates to the lazy + best-effort
    :func:`~brutex.data.lake_stats_manifest.remove_partition_stats` (absent
    shard ⇒ no-op; the LAST row's removal unlinks the whole shard; a shard
    failure logs + never raises into the heal). Kill switch off ⇒ pure
    no-op. The dataset key is the on-disk series dir name VERBATIM
    (``{"series": dest.parent.name}`` — same derivation as
    :func:`_record_option_stats`).
    """
    if not stats_manifest_enabled():
        return
    remove_partition_stats(
        lake_root, kind=KIND_OPTIONS, key={"series": dest.parent.name}, dest=dest
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
