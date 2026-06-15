"""Bounded / rolling live-tick JSONL with auto-fold to the Parquet lake.

The operator's single growing ``runs/live-ticks.jsonl`` reached GBs over a
session. PR #746 cut the per-callback bulk (a DELTA write — only changed
instruments). This module adds the SECOND layer the operator asked for ("the
live ticks should be rolling … the size is gbs"): the capture sink rotates to a
fresh **segment file** whenever the active segment crosses a size or line
threshold, so no single file ever grows unbounded, and a completed (rotated)
segment is folded into the existing live Parquet lake (REUSING
:func:`brutex.live.lake.write_live_ticks` / :func:`~brutex.live.lake.write_live_candles`
— never duplicated, Golden Rule #11 COMMON) then pruned, so disk stays bounded.

Layout — a **segment directory** (``runs/live-ticks/`` for a base of
``runs/live-ticks.jsonl``), one JSONL file per segment::

    runs/live-ticks/seg-000000.jsonl   ← folded + pruned once rotated
    runs/live-ticks/seg-000001.jsonl   ← folded + pruned once rotated
    runs/live-ticks/seg-000002.jsonl   ← ACTIVE (being appended)

Invariants (Golden Rule #7 idempotence + crash-safety):

* **Monotonic ``seq`` across segments** — the sequence number is GLOBAL; a new
  segment continues from the previous segment's ``next_seq``, so seqs are
  gap-free and strictly increasing across the whole rotation set, exactly like a
  single :class:`~brutex.live.store.TickStore`. The lake PK is
  ``(capture_run_id, seq, instrument_key)`` so a re-fold of the same segment
  unions to a byte-identical row set (no duplicate, no drop).
* **Crash-safe rotation** — rotation is the cheap, atomic act of CLOSING the
  active segment and OPENING the next; there is no rename, no merge, no
  partial-state window. A crash mid-``append`` leaves a torn LAST line in the
  active segment, which :class:`~brutex.live.store.TickStore`'s own
  ``_repair_and_count`` truncates on resume — the rolling store resumes by
  re-opening the highest-numbered segment and continuing its ``seq``.
* **Fold-then-prune is verified-before-delete** — a rotated segment is folded to
  Parquet, the write is VERIFIED (the expected seq set is present in the lake
  partition) and ONLY THEN is the raw segment deleted. A crash between fold and
  prune simply re-folds on resume (idempotent), never loses ticks.
* **Bounded ring fallback** — when no folder is wired the store keeps at most
  ``max_segments`` raw segments, deleting the oldest as it rotates (a pure ring
  buffer; the operator opted into bounded disk over full raw retention).

Read-only measurement surface (Golden Rule #3): no orders, never imported by the
engine or dashboard (DEC-LIVE-DECOUPLE-001). Pure + injected (clock/paths/folder)
→ unit-tested with fakes, no ``growwapi`` and no network.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from brutex.live.lake import (
    minute_tick_counts,
    read_live_ticks,
    write_live_candles,
    write_live_ticks,
)
from brutex.live.store import TickRecord, TickStore

_LOG = structlog.get_logger("brutex.live.rolling")

#: Segment filename, zero-padded so lexical order == numeric order: ``seg-000007.jsonl``.
_SEG_GLOB = "seg-*.jsonl"
_SEG_RE = re.compile(r"^seg-(\d{6,})\.jsonl$")
_SEG_DIGITS = 6

#: Durable GLOBAL next-``seq`` high-watermark sidecar, written on every rotation +
#: the final flush. It exists because a rotated segment is folded then **PRUNED**
#: (the production path), so on resume the segments still on disk no longer hold
#: the pruned seqs — reconstructing the global ``seq`` from on-disk record counts
#: would UNDER-count and RESET it, re-emitting seqs already in the lake and
#: silently dropping the post-crash ticks via the lake PK ``(capture_run_id, seq,
#: instrument_key)``. The watermark is the durable floor that survives pruning
#: (DEC-LIVE-AUTOROLL-001 resume fix). It is NOT a ``seg-*.jsonl`` so it is
#: invisible to :func:`list_segments` / :func:`segment_dir_for`.
_WATERMARK_NAME = ".next-seq"


def _segment_name(ordinal: int) -> str:
    """Render a zero-padded segment filename for *ordinal* (``0`` → ``seg-000000.jsonl``)."""
    return f"seg-{ordinal:0{_SEG_DIGITS}d}.jsonl"


def _read_watermark(directory: Path) -> int:
    """Read the durable global next-``seq`` watermark, or ``0`` when absent/unreadable.

    A missing sidecar (a fresh dir, or a session that predates the watermark) or a
    corrupt value degrades to ``0`` — the on-disk record seqs then carry the floor,
    so this is never worse than the pre-watermark behaviour, only better.
    """
    try:
        return int((directory / _WATERMARK_NAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_watermark(directory: Path, next_seq: int) -> None:
    """Persist the global next-``seq`` watermark ATOMICALLY (tmp + ``replace``).

    Written before a rotated/flushed segment can be pruned, so even a crash
    immediately after the prune leaves the correct floor on disk.
    """
    tmp = directory / f"{_WATERMARK_NAME}.tmp"
    tmp.write_text(str(next_seq), encoding="utf-8")
    tmp.replace(directory / _WATERMARK_NAME)


def segment_dir_for(base: str | Path) -> Path:
    """Map a base log path to its segment DIRECTORY.

    ``runs/live-ticks.jsonl`` → ``runs/live-ticks`` (the trailing ``.jsonl`` /
    any single suffix is dropped); a path with no suffix is used as-is. The
    directory holds one ``seg-NNNNNN.jsonl`` per segment.
    """
    base_path = Path(base)
    return base_path.with_suffix("") if base_path.suffix else base_path


def list_segments(directory: str | Path) -> list[Path]:
    """Return existing ``seg-*.jsonl`` segment files under *directory*, ascending.

    Sorted by the numeric ordinal embedded in the name (lexical == numeric thanks
    to the zero-padding, but the explicit ordinal sort is robust to any wider
    zero-pad a future segment might carry). Returns ``[]`` for a missing dir.
    """
    target = Path(directory)
    if not target.exists():
        return []
    pairs: list[tuple[int, Path]] = []
    for path in target.glob(_SEG_GLOB):
        match = _SEG_RE.match(path.name)
        if match is not None:
            pairs.append((int(match.group(1)), path))
    return [path for _ordinal, path in sorted(pairs)]


def _segment_ordinal(path: Path) -> int:
    """Extract the numeric ordinal from a ``seg-NNNNNN.jsonl`` path."""
    match = _SEG_RE.match(path.name)
    if match is None:  # pragma: no cover — only called on names that already matched
        raise ValueError(f"not a segment file: {path!r}")
    return int(match.group(1))


@dataclass(frozen=True, slots=True)
class FoldResult:
    """Outcome of folding ONE rotated segment into the live lake.

    Attributes
    ----------
    segment
        The raw segment file that was folded.
    records
        Number of :class:`~brutex.live.store.TickRecord` read from the segment.
    tick_paths, candle_paths
        Lake partition files the fold wrote/merged (the ``write_live_*`` returns).
    pruned
        ``True`` when the raw segment was deleted after a verified write.
    """

    segment: Path
    records: int
    tick_paths: tuple[Path, ...]
    candle_paths: tuple[Path, ...]
    pruned: bool


def fold_segment(
    segment: str | Path,
    *,
    lake_root: str | Path,
    capture_run_id: str,
    feed_kind: str = "index",
    prune: bool = True,
    build_candles: Callable[[Sequence[TickRecord]], Mapping[str, Sequence[Any]]] | None = None,
) -> FoldResult:
    """Fold one rotated *segment* into the live lake, then optionally prune it.

    REUSES :func:`brutex.live.lake.write_live_ticks` /
    :func:`~brutex.live.lake.write_live_candles` verbatim (Golden Rule #11) — this
    is the same fold the ``brutex.live lake`` CLI command does, applied to ONE
    segment so it can run incrementally as segments rotate.

    The prune is **verified-before-delete**: the segment file is removed ONLY
    after every seq it contributed is confirmed present in the lake's
    ``live_ticks`` table (a re-read), so a fold that silently wrote nothing never
    deletes the raw evidence. An empty (zero-record / empty-snapshot) segment
    writes nothing and is pruned immediately (there is nothing to verify and
    nothing to lose).

    Parameters
    ----------
    segment
        Path to the rotated segment JSONL.
    lake_root, capture_run_id, feed_kind
        Forwarded to the lake writers (``capture_run_id`` is also ``built_run_id``).
    prune
        Delete the raw segment after a verified write (default ``True``).
    build_candles
        Injected ``records -> {instrument_key: [CandleBar]}`` candle builder
        (defaults to the real
        :func:`brutex.live.candle_builder.build_minute_candles` over
        :func:`brutex.live.tick.iter_ticks`). Injected so the fold is unit
        testable without re-importing the builder graph.
    """
    builder = build_candles if build_candles is not None else _default_build_candles
    seg_path = Path(segment)
    records = TickStore.read(seg_path)
    seqs = {record.seq for record in records}

    tick_paths = (
        tuple(
            write_live_ticks(
                records, lake_root=lake_root, capture_run_id=capture_run_id, feed_kind=feed_kind
            )
        )
        if records
        else ()
    )
    candles = builder(records)
    candle_paths = (
        tuple(
            write_live_candles(
                candles,
                lake_root=lake_root,
                built_run_id=capture_run_id,
                tick_counts=minute_tick_counts(records),
            )
        )
        if candles
        else ()
    )

    pruned = False
    if prune and _fold_is_durable(lake_root, capture_run_id, seqs):
        seg_path.unlink()
        pruned = True
    _LOG.info(
        "live_segment_folded",
        segment=str(seg_path),
        records=len(records),
        tick_partitions=len(tick_paths),
        candle_partitions=len(candle_paths),
        pruned=pruned,
    )
    return FoldResult(
        segment=seg_path,
        records=len(records),
        tick_paths=tick_paths,
        candle_paths=candle_paths,
        pruned=pruned,
    )


def _default_build_candles(records: Sequence[TickRecord]) -> dict[str, list[Any]]:
    """Real candle builder: ``records -> {instrument_key: [CandleBar]}`` (lazy import)."""
    from brutex.live.candle_builder import build_minute_candles
    from brutex.live.tick import iter_ticks

    return build_minute_candles(iter_ticks(records))


def _fold_is_durable(lake_root: str | Path, capture_run_id: str, seqs: set[int]) -> bool:
    """True when every *seqs* row for *capture_run_id* is present in the lake.

    An empty *seqs* (a zero-record segment) is vacuously durable — there is
    nothing to lose, so the empty raw file is safe to prune. Otherwise we re-read
    the ``live_ticks`` table and confirm the seq set is a subset of what landed.
    """
    if not seqs:
        return True
    frame = read_live_ticks(lake_root)
    if frame.is_empty():
        return False
    present = set(frame.filter(frame["capture_run_id"] == capture_run_id)["seq"].to_list())
    return seqs <= present


class RollingTickStore:
    """Append-only tick sink that ROTATES segments + auto-folds them to the lake.

    A drop-in for :class:`~brutex.live.store.TickStore` from
    :class:`~brutex.live.capture.TickCapture`'s point of view — it exposes the
    same :attr:`next_seq` / :meth:`append` / :meth:`close` / context-manager
    surface — but each :meth:`append` checks the active segment's size and line
    count and, when either crosses its threshold, CLOSES the current segment and
    OPENS the next (carrying the global ``seq`` forward). A rotated segment is
    handed to :meth:`_handle_closed` which either folds-then-prunes it (when a
    folder is wired) or drops the oldest to keep a bounded ring.
    """

    def __init__(
        self,
        directory: Path,
        *,
        active: TickStore,
        ordinal: int,
        max_bytes: int,
        max_lines: int,
        max_segments: int,
        on_rotate: Callable[[Path], None] | None,
    ) -> None:
        """Construct directly; prefer :meth:`open`, which wires the active segment."""
        self._dir = directory
        self._active = active
        self._active_path = directory / _segment_name(ordinal)
        self._ordinal = ordinal
        self._max_bytes = max_bytes
        self._max_lines = max_lines
        self._max_segments = max_segments
        self._on_rotate = on_rotate
        # Byte size comes from the active store's in-process counter (seeded with
        # the file size at open) — no ``stat`` on the per-tick append path. The
        # line count is a ONE-TIME read at construction (off the hot path).
        self._bytes = active.bytes_written
        self._lines = len(TickStore.read(self._active_path)) if self._active_path.exists() else 0
        self._rotations = 0

    @classmethod
    def open(
        cls,
        base: str | Path,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        max_lines: int = 1_000_000,
        max_segments: int = 8,
        on_rotate: Callable[[Path], None] | None = None,
        resume: bool = True,
    ) -> RollingTickStore:
        """Open the rolling sink for the segment directory derived from *base*.

        Resumes by re-opening the HIGHEST-numbered existing segment and
        continuing its ``seq`` (the torn tail of a crash is truncated by the
        underlying :class:`~brutex.live.store.TickStore`). When the directory is
        empty (or ``resume`` is ``False``) it starts at ``seg-000000``.

        Parameters
        ----------
        base
            The operator's log path (e.g. ``runs/live-ticks.jsonl``); the segment
            directory is :func:`segment_dir_for` of it.
        max_bytes, max_lines
            Rotate the active segment once it reaches EITHER threshold (bytes of
            the file OR appended lines), whichever trips first.
        max_segments
            When no *on_rotate* folder is wired, retain at most this many raw
            segments (a bounded ring — oldest pruned on rotation). Must be ≥ 1.
        on_rotate
            Called with the just-closed segment path after each rotation (the
            fold-then-prune hook). When ``None`` the bounded ring applies instead.
        resume
            Continue an existing segment set (default) vs. start fresh numbering.
        """
        if max_bytes < 1 or max_lines < 1 or max_segments < 1:
            raise ValueError("max_bytes, max_lines and max_segments must all be >= 1")
        directory = segment_dir_for(base)
        directory.mkdir(parents=True, exist_ok=True)
        existing = list_segments(directory) if resume else []
        # The durable global next-``seq`` floor — survives rotated segments being
        # folded + PRUNED (the production path), where counting on-disk records
        # would under-count the pruned ones and RESET seq (lake-PK collision +
        # silent post-crash tick loss). DEC-LIVE-AUTOROLL-001 resume fix.
        watermark = _read_watermark(directory) if resume else 0
        if existing:
            last = existing[-1]
            ordinal = _segment_ordinal(last)
            # Reconstruct the GLOBAL next seq from the seqs the records THEMSELVES
            # carry (each line stores its global ``seq``), so a folded+pruned prior
            # segment can never under-count it. Repair the active segment's torn
            # tail first, then take ``max(every on-disk record's seq) + 1`` and the
            # durable watermark, whichever is higher (the watermark is the floor
            # when the active segment is EMPTY — a crash right after a rotation
            # whose prior was already pruned).
            TickStore.open(last, resume=True).close()  # repair torn tail in place
            seqs = [record.seq for seg in existing for record in TickStore.read(seg)]
            from_records = (max(seqs) + 1) if seqs else 0
            global_seq = max(from_records, watermark)
            active = TickStore(
                last,
                start_seq=global_seq,
                handle=last.open("a", encoding="utf-8"),
                bytes_written=last.stat().st_size,
            )
        else:
            # Empty directory: a fresh start (``resume=False`` → watermark 0) OR
            # every segment was folded + pruned on a clean shutdown — seed
            # ``seg-000000`` from the durable watermark so a resumed session never
            # re-uses a seq already in the lake.
            ordinal = 0
            seg0 = directory / _segment_name(0)
            seg0_size = seg0.stat().st_size if seg0.exists() else 0
            active = TickStore(
                seg0,
                start_seq=watermark,
                handle=seg0.open("a", encoding="utf-8"),
                bytes_written=seg0_size,
            )
        return cls(
            directory,
            active=active,
            ordinal=ordinal,
            max_bytes=max_bytes,
            max_lines=max_lines,
            max_segments=max_segments,
            on_rotate=on_rotate,
        )

    @property
    def next_seq(self) -> int:
        """The global sequence number the next :meth:`append` will assign."""
        return self._active.next_seq

    @property
    def ordinal(self) -> int:
        """The active segment's ordinal (its ``seg-NNNNNN`` number)."""
        return self._ordinal

    @property
    def active_path(self) -> Path:
        """Path of the segment currently being appended."""
        return self._active_path

    @property
    def rotations(self) -> int:
        """Number of rotations performed so far this session."""
        return self._rotations

    def append(
        self, snapshot: Mapping[str, Any], *, recv_ts: datetime, recv_ns: int | None = None
    ) -> TickRecord:
        """Append one snapshot, rotating FIRST if the active segment is full.

        The threshold is checked BEFORE the write so a segment never exceeds its
        bound by more than one in-flight line, and an empty active segment is
        never rotated (a single oversized record still lands rather than looping).
        """
        if self._lines >= self._max_lines or (self._lines > 0 and self._bytes >= self._max_bytes):
            self._rotate()
        record = self._active.append(snapshot, recv_ts=recv_ts, recv_ns=recv_ns)
        self._bytes = self._active.bytes_written  # O(1) in-process — no stat syscall per tick
        self._lines += 1
        return record

    def _rotate(self) -> None:
        """Close the active segment, open the next, and hand off the closed one."""
        self._active.close()
        closed = self._active_path
        next_seq = self._active.next_seq
        self._ordinal += 1
        self._active_path = self._dir / _segment_name(self._ordinal)
        # Carry the global seq forward: the new segment is empty on disk, so we
        # construct a store whose ``next_seq`` is the running total.
        self._active = TickStore(
            self._active_path,
            start_seq=next_seq,
            handle=self._active_path.open("a", encoding="utf-8"),
        )
        self._bytes = 0
        self._lines = 0
        self._rotations += 1
        # Persist the durable next-seq floor BEFORE the closed segment can be
        # pruned, so a crash right after the prune still resumes at the right seq
        # even though the new segment is still empty (DEC-LIVE-AUTOROLL-001).
        _write_watermark(self._dir, next_seq)
        _LOG.info("live_segment_rotated", closed=str(closed), opened=str(self._active_path))
        self._handle_closed(closed)

    def _handle_closed(self, closed: Path) -> None:
        """Fold-then-prune the closed segment, or apply the bounded-ring policy."""
        if self._on_rotate is not None:
            self._on_rotate(closed)
        else:
            self._enforce_ring()

    def _enforce_ring(self) -> None:
        """Keep at most ``max_segments`` raw segments (oldest pruned first)."""
        segments = list_segments(self._dir)
        # Never prune the active segment (it is last); cap the retained tail.
        prunable = segments[:-1] if segments and segments[-1] == self._active_path else segments
        while len(prunable) + 1 > self._max_segments:
            oldest = prunable.pop(0)
            oldest.unlink()
            _LOG.info("live_segment_ring_pruned", segment=str(oldest))

    def flush_active(self) -> Path | None:
        """Close + hand off the ACTIVE segment (end-of-session final fold).

        Returns the just-closed segment path (so the caller can confirm the tail
        folded), or ``None`` when the active segment is empty (nothing to fold).
        After this the handle is closed — call only at shutdown.
        """
        if self._lines == 0:
            return None
        # Record the final next-seq floor before the tail can be folded + pruned,
        # so a fresh `--resume` after a clean shutdown continues the global seq
        # (never re-using a seq already in the lake) — DEC-LIVE-AUTOROLL-001.
        _write_watermark(self._dir, self._active.next_seq)
        self._active.close()
        closed = self._active_path
        self._handle_closed(closed)
        return closed

    def close(self) -> None:
        """Close the active segment handle (idempotent). Does NOT fold the tail."""
        self._active.close()

    def __enter__(self) -> RollingTickStore:
        """Enter a ``with`` block, returning self."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close on ``with``-block exit (the tail is flushed explicitly, not here)."""
        self.close()


def make_fold_on_rotate(
    *,
    lake_root: str | Path,
    capture_run_id: str,
    feed_kind: str = "index",
    build_candles: Callable[[Sequence[TickRecord]], Mapping[str, Sequence[Any]]] | None = None,
) -> Callable[[Path], None]:
    """Build an ``on_rotate`` hook that folds each rotated segment to the lake.

    Wire this into :meth:`RollingTickStore.open` so that every rotation
    incrementally folds (and prunes) the just-closed segment — keeping BOTH the
    raw JSONL footprint AND the lake bounded as a long session runs.
    """

    def fold(segment: Path) -> None:
        fold_segment(
            segment,
            lake_root=lake_root,
            capture_run_id=capture_run_id,
            feed_kind=feed_kind,
            prune=True,
            build_candles=build_candles,
        )

    return fold


__all__ = [
    "FoldResult",
    "RollingTickStore",
    "fold_segment",
    "list_segments",
    "make_fold_on_rotate",
    "segment_dir_for",
]
