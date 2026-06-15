"""Tests for the bounded/rolling tick store + auto-fold (:mod:`brutex.live.rolling`).

Covers (DEC-LIVE-AUTOROLL-001): rotation at the size/line threshold, monotonic
``seq`` continuity across segments, fold-then-prune, re-fold idempotence, the
crash/torn-segment tolerance, the empty/zero-tick segment, the bounded ring, and
the ``roll-fold`` catch-up command. Pure + injected — no ``growwapi``, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.live.lake import read_live_candles, read_live_ticks
from brutex.live.rolling import (
    FoldResult,
    RollingTickStore,
    _read_watermark,
    _write_watermark,
    fold_segment,
    list_segments,
    make_fold_on_rotate,
    segment_dir_for,
)
from brutex.live.store import TickStore

_T0 = datetime(2026, 6, 15, 3, 45, 10, tzinfo=UTC)  # 09:15:10 IST — in-session


def _ts(offset_s: int = 0) -> datetime:
    return datetime(2026, 6, 15, 3, 45, 10 + offset_s, tzinfo=UTC)


def _snap(value: float) -> dict[str, float]:
    return {"NSE:IDX:NIFTY": value}


# ───────────────────────── pure helpers ─────────────────────────


def test_segment_dir_for_strips_suffix_and_keeps_bare() -> None:
    assert segment_dir_for("runs/live-ticks.jsonl") == Path("runs/live-ticks")
    assert segment_dir_for(Path("runs/ticks")) == Path("runs/ticks")


def test_list_segments_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_segments(tmp_path / "nope") == []


def test_list_segments_orders_by_ordinal_and_ignores_foreign(tmp_path: Path) -> None:
    (tmp_path / "seg-000002.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "seg-000000.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "seg-000010.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")  # foreign name, ignored
    # Matches the glob but NOT the ordinal regex (too few digits / non-numeric) —
    # exercises the regex-miss branch inside list_segments.
    (tmp_path / "seg-abc.jsonl").write_text("", encoding="utf-8")
    names = [p.name for p in list_segments(tmp_path)]
    assert names == ["seg-000000.jsonl", "seg-000002.jsonl", "seg-000010.jsonl"]


# ───────────────────────── rotation ─────────────────────────


def test_rotates_at_line_threshold_and_seq_is_continuous(tmp_path: Path) -> None:
    store = RollingTickStore.open(
        tmp_path / "live.jsonl", max_lines=2, max_segments=99, resume=False
    )
    seqs = [store.append(_snap(100.0 + i), recv_ts=_ts(i), recv_ns=i + 1).seq for i in range(5)]
    # The active segment is the 3rd (ordinal 2) and its path is exposed.
    assert store.ordinal == 2
    assert store.active_path.name == "seg-000002.jsonl"
    store.close()
    # Global seqs are gap-free + strictly increasing ACROSS segments.
    assert seqs == [0, 1, 2, 3, 4]
    segments = list_segments(segment_dir_for(tmp_path / "live.jsonl"))
    assert len(segments) == 3  # 2 + 2 + 1
    # Reading each segment back, the seqs concatenate without gap/dup.
    all_seqs = [rec.seq for seg in segments for rec in TickStore.read(seg)]
    assert all_seqs == [0, 1, 2, 3, 4]
    assert store.rotations == 2


def test_rotates_at_byte_threshold(tmp_path: Path) -> None:
    # One record's JSON is far larger than 1 byte, so a 1-byte cap rotates on
    # every append AFTER the first (the empty active segment is never rotated).
    store = RollingTickStore.open(
        tmp_path / "live.jsonl", max_bytes=1, max_segments=99, resume=False
    )
    for i in range(3):
        store.append(_snap(100.0 + i), recv_ts=_ts(i), recv_ns=i + 1)
    store.close()
    segments = list_segments(segment_dir_for(tmp_path / "live.jsonl"))
    assert [len(TickStore.read(s)) for s in segments] == [1, 1, 1]


def test_oversized_first_record_still_lands_no_infinite_loop(tmp_path: Path) -> None:
    # max_bytes=1 but the active segment is empty → the first record is NOT
    # rotated away (the ``_lines > 0`` guard), so it lands.
    store = RollingTickStore.open(tmp_path / "live.jsonl", max_bytes=1, resume=False)
    rec = store.append(_snap(100.0), recv_ts=_T0, recv_ns=1)
    store.close()
    assert rec.seq == 0
    assert store.rotations == 0


def test_open_rejects_non_positive_bounds(tmp_path: Path) -> None:
    for kwargs in ({"max_bytes": 0}, {"max_lines": 0}, {"max_segments": 0}):
        with pytest.raises(ValueError, match=">= 1"):
            RollingTickStore.open(tmp_path / "live.jsonl", **kwargs)  # type: ignore[arg-type]


# ───────────────────────── bounded ring ─────────────────────────


def test_bounded_ring_prunes_oldest_segments(tmp_path: Path) -> None:
    store = RollingTickStore.open(
        tmp_path / "live.jsonl", max_lines=1, max_segments=2, resume=False
    )
    for i in range(5):
        store.append(_snap(100.0 + i), recv_ts=_ts(i), recv_ns=i + 1)
    store.close()
    segments = list_segments(segment_dir_for(tmp_path / "live.jsonl"))
    # At most 2 raw segments retained — the oldest were pruned on rotation.
    assert len(segments) == 2
    kept = [rec.seq for seg in segments for rec in TickStore.read(seg)]
    assert kept == [3, 4]  # the newest survive


# ───────────────────────── fold-then-prune ─────────────────────────


def test_fold_on_rotate_writes_lake_and_prunes(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    on_rotate = make_fold_on_rotate(lake_root=lake, capture_run_id="r1", feed_kind="index")
    store = RollingTickStore.open(
        tmp_path / "live.jsonl", max_lines=2, on_rotate=on_rotate, resume=False
    )
    for i in range(5):
        store.append(_snap(100.0 + i), recv_ts=_ts(i), recv_ns=i + 1)
    tail = store.flush_active()
    store.close()
    assert tail is not None  # the active tail was flushed + folded
    # ALL 5 ticks reached the lake; every rotated + tail segment was pruned.
    assert read_live_ticks(lake).shape[0] == 5
    assert read_live_candles(lake).shape[0] >= 1
    assert list_segments(segment_dir_for(tmp_path / "live.jsonl")) == []


def test_flush_active_empty_returns_none(tmp_path: Path) -> None:
    store = RollingTickStore.open(tmp_path / "live.jsonl", resume=False)
    assert store.flush_active() is None
    store.close()


def test_fold_segment_prunes_only_after_verified_write(tmp_path: Path) -> None:
    seg = tmp_path / "seg.jsonl"
    with TickStore.open(seg) as s:
        s.append(_snap(100.0), recv_ts=_T0, recv_ns=1)
    result = fold_segment(seg, lake_root=tmp_path / "lake", capture_run_id="r1")
    assert isinstance(result, FoldResult)
    assert result.records == 1 and result.pruned is True
    assert not seg.exists()
    assert read_live_ticks(tmp_path / "lake").shape[0] == 1


def test_fold_segment_keep_does_not_prune(tmp_path: Path) -> None:
    seg = tmp_path / "seg.jsonl"
    with TickStore.open(seg) as s:
        s.append(_snap(100.0), recv_ts=_T0, recv_ns=1)
    result = fold_segment(seg, lake_root=tmp_path / "lake", capture_run_id="r1", prune=False)
    assert result.pruned is False and seg.exists()


def test_fold_segment_empty_is_pruned_vacuously(tmp_path: Path) -> None:
    seg = tmp_path / "seg.jsonl"
    seg.write_text("", encoding="utf-8")  # zero-record segment
    result = fold_segment(seg, lake_root=tmp_path / "lake", capture_run_id="r1")
    assert result.records == 0 and result.pruned is True and not seg.exists()
    assert result.tick_paths == () and result.candle_paths == ()


def test_fold_segment_not_durable_keeps_raw(tmp_path: Path) -> None:
    # A segment whose ONLY tick is out-of-session → no candle rows, but the tick
    # IS written to live_ticks (the tick lake is session-agnostic), so it IS
    # durable. To exercise the NOT-durable branch we point the verify read at an
    # EMPTY lake by stubbing the writer to a no-op via a snapshot that flattens
    # to nothing (no instrument) — then seqs is non-empty but the lake stays empty.
    seg = tmp_path / "seg.jsonl"
    with TickStore.open(seg) as s:
        s.append({"meta": "no-price-here"}, recv_ts=_T0, recv_ns=1)  # flattens to {}
    result = fold_segment(seg, lake_root=tmp_path / "lake", capture_run_id="r1")
    # The record exists (seq 0) but its snapshot has no priceable leaf, so the
    # tick frame has 0 rows → the lake is empty → durability check fails → kept.
    assert result.records == 1 and result.pruned is False and seg.exists()


# ───────────────────── re-fold idempotence (golden) ─────────────────────


def test_refold_is_byte_identical_idempotent(tmp_path: Path) -> None:
    """Golden idempotence: folding the SAME segment twice yields identical lake bytes."""
    seg = tmp_path / "seg.jsonl"
    with TickStore.open(seg) as s:
        s.append({"NSE:IDX:NIFTY": 22500.499}, recv_ts=_ts(0), recv_ns=1)
        s.append({"NSE:IDX:NIFTY": 22510.0}, recv_ts=_ts(40), recv_ns=2)
    lake = tmp_path / "lake"
    fold_segment(seg, lake_root=lake, capture_run_id="r1", prune=False)
    tick_files = sorted((lake / "live" / "ticks").glob("dt=*.parquet"))
    candle_files = sorted((lake / "live" / "candles_1m").glob("dt=*.parquet"))
    first_ticks = {p: p.read_bytes() for p in tick_files}
    first_candles = {p: p.read_bytes() for p in candle_files}
    # Re-fold the SAME segment (idempotence — the lake PK dedups).
    fold_segment(seg, lake_root=lake, capture_run_id="r1", prune=False)
    assert {p: p.read_bytes() for p in tick_files} == first_ticks
    assert {p: p.read_bytes() for p in candle_files} == first_candles
    assert read_live_ticks(lake).shape[0] == 2  # no duplicate rows


# ───────────────────── crash / torn-tail tolerance ─────────────────────


def test_resume_continues_seq_after_clean_restart(tmp_path: Path) -> None:
    base = tmp_path / "live.jsonl"
    store = RollingTickStore.open(base, max_lines=2, max_segments=99, resume=False)
    for i in range(3):
        store.append(_snap(100.0 + i), recv_ts=_ts(i), recv_ns=i + 1)
    store.close()  # simulates a process exit after 3 ticks (seg0:2, seg1:1)
    # Re-open the SAME base — it resumes the highest segment + global seq.
    store2 = RollingTickStore.open(base, max_lines=2, max_segments=99, resume=True)
    assert store2.next_seq == 3
    rec = store2.append(_snap(200.0), recv_ts=_ts(9), recv_ns=9)
    store2.close()
    assert rec.seq == 3  # no gap, no duplicate across the restart


def test_resume_after_fold_prune_continues_global_seq_no_loss(tmp_path: Path) -> None:
    """REGRESSION (DEC-LIVE-AUTOROLL-001): resume after a rotated segment was
    folded + PRUNED must continue the GLOBAL seq, never reset it.

    The pre-fix ``open`` rebuilt the seq from line counts of the segments still on
    disk; once ``seg-000000`` is folded + pruned, that under-counted and reset the
    seq, so the post-crash ticks re-used seqs already in the lake and were SILENTLY
    DROPPED by the lake PK ``(capture_run_id, seq, instrument_key)``. This asserts
    the resumed seq is exact AND every tick survives end-to-end.
    """
    base = tmp_path / "live.jsonl"
    lake = tmp_path / "lake"
    on_rotate = make_fold_on_rotate(lake_root=lake, capture_run_id="r1", feed_kind="index")
    # Session 1: 4 ticks, max_lines=2 → seg0 (seq 0,1) rotates, folds + PRUNES;
    # seg1 (seq 2,3) is the active segment on disk. Crash = close without flush.
    store = RollingTickStore.open(base, max_lines=2, on_rotate=on_rotate, resume=False)
    for i in range(4):
        store.append(_snap(100.0 + i), recv_ts=_ts(i), recv_ns=i + 1)
    store.close()
    assert read_live_ticks(lake).shape[0] == 2  # seg0's seq 0,1 folded before prune
    # Session 2: resume MUST see next_seq == 4 (NOT reset to 2 by the pruned seg0).
    store2 = RollingTickStore.open(base, max_lines=2, on_rotate=on_rotate, resume=True)
    assert store2.next_seq == 4
    rec = store2.append(_snap(200.0), recv_ts=_ts(9), recv_ns=99)
    assert rec.seq == 4
    store2.flush_active()
    store2.close()
    # Every tick reached the lake exactly once — no reset-collision, no drop.
    frame = read_live_ticks(lake)
    seqs = sorted(frame.filter(frame["capture_run_id"] == "r1")["seq"].to_list())
    assert seqs == [0, 1, 2, 3, 4]


def test_resume_empty_active_after_prune_uses_watermark(tmp_path: Path) -> None:
    """A crash right after a rotation (empty active, prior already pruned) → resume
    seeds the global seq from the DURABLE watermark, not 0 (DEC-LIVE-AUTOROLL-001).

    Reconstructs the on-disk post-rotation crash state directly: ``seg-000000`` was
    folded + pruned (gone), ``seg-000001`` is the active segment but EMPTY (no
    append landed before the crash), and the ``.next-seq`` watermark records that
    the global next seq is 5.
    """
    base = tmp_path / "live.jsonl"
    seg_dir = segment_dir_for(base)
    seg_dir.mkdir(parents=True)
    (seg_dir / "seg-000001.jsonl").write_text("", encoding="utf-8")  # empty active
    _write_watermark(seg_dir, 5)
    store = RollingTickStore.open(base, resume=True)
    assert store.next_seq == 5  # the watermark floor — NOT 0
    rec = store.append(_snap(1.0), recv_ts=_ts(0), recv_ns=1)
    store.close()
    assert rec.seq == 5


def test_resume_after_flush_active_prune_seeds_from_watermark(tmp_path: Path) -> None:
    """``flush_active`` folds + prunes the tail → empty dir; a fresh ``--resume``
    continues the global seq from the watermark, never re-using a lake seq.
    """
    base = tmp_path / "live.jsonl"
    lake = tmp_path / "lake"
    on_rotate = make_fold_on_rotate(lake_root=lake, capture_run_id="r1", feed_kind="index")
    store = RollingTickStore.open(base, max_lines=2, on_rotate=on_rotate, resume=False)
    for i in range(3):  # seq 0,1 rotate+fold+prune; seq 2 in the active tail
        store.append(_snap(100.0 + i), recv_ts=_ts(i), recv_ns=i + 1)
    store.flush_active()  # folds + prunes the tail (seq 2) → segment dir now empty
    store.close()
    assert list_segments(segment_dir_for(base)) == []  # every segment pruned
    # Resume on an EMPTY dir: the durable watermark continues the seq at 3.
    store2 = RollingTickStore.open(base, max_lines=2, on_rotate=on_rotate, resume=True)
    assert store2.next_seq == 3
    store2.append(_snap(200.0), recv_ts=_ts(9), recv_ns=99)
    store2.flush_active()
    store2.close()
    frame = read_live_ticks(lake)
    seqs = sorted(frame.filter(frame["capture_run_id"] == "r1")["seq"].to_list())
    assert seqs == [0, 1, 2, 3]  # complete, no loss, no collision


def test_read_watermark_missing_and_corrupt_degrade_to_zero(tmp_path: Path) -> None:
    """The watermark sidecar degrades to 0 when absent or unparseable (round-trips otherwise)."""
    assert _read_watermark(tmp_path) == 0  # missing file
    (tmp_path / ".next-seq").write_text("not-an-int", encoding="utf-8")
    assert _read_watermark(tmp_path) == 0  # corrupt value
    _write_watermark(tmp_path, 7)
    assert _read_watermark(tmp_path) == 7  # written value round-trips


def test_resume_repairs_torn_tail_in_active_segment(tmp_path: Path) -> None:
    base = tmp_path / "live.jsonl"
    store = RollingTickStore.open(base, max_lines=10, resume=False)
    store.append(_snap(100.0), recv_ts=_ts(0), recv_ns=1)
    store.append(_snap(101.0), recv_ts=_ts(1), recv_ns=2)
    store.close()
    # Simulate a crash mid-flush: append a torn partial line (no newline).
    active = list_segments(segment_dir_for(base))[-1]
    with active.open("a", encoding="utf-8") as h:
        h.write('{"seq":2,"recv_ts":"2026')  # torn — no terminating newline
    # Resume: the torn tail is truncated, seq continues from the 2 complete lines.
    store2 = RollingTickStore.open(base, max_lines=10, resume=True)
    assert store2.next_seq == 2
    rec = store2.append(_snap(102.0), recv_ts=_ts(2), recv_ns=3)
    store2.close()
    assert rec.seq == 2
    assert [r.seq for r in TickStore.read(active)] == [0, 1, 2]


def test_roll_fold_recovers_orphan_segments_after_crash(tmp_path: Path) -> None:
    """A crash without --roll-lake-root leaves orphan segments; roll-fold catches up."""
    base = tmp_path / "live.jsonl"
    store = RollingTickStore.open(base, max_lines=1, max_segments=99, resume=False)
    for i in range(3):
        store.append(_snap(100.0 + i), recv_ts=_ts(i), recv_ns=i + 1)
    tail = store.flush_active()
    store.close()
    assert tail is not None
    directory = segment_dir_for(base)
    assert len(list_segments(directory)) == 3  # all retained (no folder, big ring)
    lake = tmp_path / "lake"
    # Catch-up fold (what `roll-fold` does): fold every orphan, prune each.
    for seg in list_segments(directory):
        fold_segment(seg, lake_root=lake, capture_run_id="r1")
    assert read_live_ticks(lake).shape[0] == 3
    assert list_segments(directory) == []  # all pruned after verified writes


def test_context_manager_closes(tmp_path: Path) -> None:
    base = tmp_path / "live.jsonl"
    with RollingTickStore.open(base, resume=False) as store:
        store.append(_snap(100.0), recv_ts=_T0, recv_ns=1)
    # Re-opening proves the handle was closed cleanly (resume reads it back).
    reopened = RollingTickStore.open(base, resume=True)
    assert reopened.next_seq == 1
    reopened.close()
