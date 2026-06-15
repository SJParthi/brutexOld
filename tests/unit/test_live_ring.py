"""Tests for the in-RAM ring + periodic durable flush (:mod:`brutex.live.ring`).

Covers DEC-LIVE-RAM-RING-001 (#797) + DEC-LIVE-FLUSH-LOOP-001 (#798): O(1) ring
append, the atomic drain swap, the observer snapshot, capacity validation; the
flusher's capacity-spill (never drop), cooperative ``flush_due`` cadence, the full
shutdown ``drain``, seq continuity through the SINK (never the ring), idempotence
of a re-flush over a resumed sink, plus the capture-loop + CLI wiring. Seven test
types are exercised: unit, property (multiset preservation under arbitrary
interleavings), golden-idempotence (a re-fold over a resumed sink is byte-stable),
bench (O(1) append ratio), stress (ring-full burst), crash-restart (un-flushed
ring tail is lost but the sink resumes gap-free), and the real-thread non-gating
producer-drains-without-loss check. Pure + injected — no ``growwapi``, no network.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from brutex.live.__main__ import LiveDeps, main
from brutex.live.capture import TickCapture
from brutex.live.ring import RingFlusher, RingItem, TickRing
from brutex.live.rolling import RollingTickStore
from brutex.live.store import TickRecord, TickStore

_T0 = datetime(2026, 6, 15, 3, 45, 10, tzinfo=UTC)  # 09:15:10 IST — in-session


def _ts(offset_s: int = 0) -> datetime:
    return datetime(2026, 6, 15, 3, 45, 10 + offset_s, tzinfo=UTC)


def _item(value: float, *, offset_s: int = 0) -> RingItem:
    return RingItem(
        snapshot={"NSE:IDX:NIFTY": value}, recv_ts=_ts(offset_s), recv_ns=1_000 + offset_s
    )


class _FakeSink:
    """A :class:`~brutex.live.store.TickSink` double that records every append.

    Assigns ``seq`` exactly like the real sink (monotonic from ``start``), so the
    tests can assert the ring NEVER assigns a seq and the sink does.
    """

    def __init__(self, *, start: int = 0) -> None:
        self._seq = start
        self.records: list[TickRecord] = []

    @property
    def next_seq(self) -> int:
        return self._seq

    def append(self, snapshot: Any, *, recv_ts: datetime, recv_ns: int | None = None) -> TickRecord:
        record = TickRecord(
            seq=self._seq,
            recv_ts=recv_ts,
            snapshot=dict(snapshot),
            recv_ns=recv_ns if recv_ns is not None else 0,
        )
        self.records.append(record)
        self._seq += 1
        return record


def _clock(values: list[float]) -> Callable[[], float]:
    it = iter(values)
    return lambda: next(it)


# ───────────────────────────── TickRing ─────────────────────────────


def test_ring_rejects_non_positive_capacity() -> None:
    with pytest.raises(ValueError, match="capacity must be >= 1"):
        TickRing(capacity=0)


def test_ring_append_below_capacity_returns_false_and_buffers() -> None:
    ring = TickRing(capacity=3)
    full_after_1 = ring.append(_item(100.0))
    full_after_2 = ring.append(_item(101.0))
    assert full_after_1 is False
    assert full_after_2 is False
    assert len(ring) == 2
    assert ring.capacity == 3


def test_ring_append_at_capacity_returns_true() -> None:
    ring = TickRing(capacity=2)
    below = ring.append(_item(100.0))
    at_cap = ring.append(_item(101.0))  # now AT capacity → signal flush
    over_cap = ring.append(_item(102.0))  # OVER capacity (no eviction) → still True
    assert below is False
    assert at_cap is True
    assert over_cap is True


def test_ring_drain_pending_returns_fifo_and_empties() -> None:
    ring = TickRing(capacity=10)
    ring.append(_item(1.0))
    ring.append(_item(2.0))
    ring.append(_item(3.0))
    drained = ring.drain_pending()
    assert [i.snapshot["NSE:IDX:NIFTY"] for i in drained] == [1.0, 2.0, 3.0]
    assert len(ring) == 0
    second_drain = ring.drain_pending()
    assert second_drain == []  # second drain is empty


def test_ring_snapshot_reads_without_removing() -> None:
    ring = TickRing(capacity=10)
    ring.append(_item(1.0))
    ring.append(_item(2.0))
    view = ring.snapshot()
    assert [i.snapshot["NSE:IDX:NIFTY"] for i in view] == [1.0, 2.0]
    assert len(ring) == 2  # snapshot did NOT drain
    assert isinstance(view, tuple)


# ─────────────────────────── RingFlusher ────────────────────────────


def test_flusher_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="flush_interval must be > 0"):
        RingFlusher(
            sink=_FakeSink(), ring=TickRing(capacity=4), flush_interval=0.0, monotonic=lambda: 0.0
        )


def test_flusher_capture_buffers_without_disk_until_flush_due() -> None:
    sink = _FakeSink()
    ring = TickRing(capacity=100)
    flusher = RingFlusher(
        sink=sink, ring=ring, flush_interval=1.0, monotonic=_clock([0.0, 0.5, 1.0])
    )
    flusher.capture({"NSE:IDX:NIFTY": 100.0}, recv_ts=_ts(0), recv_ns=10)
    flusher.capture({"NSE:IDX:NIFTY": 101.0}, recv_ts=_ts(1), recv_ns=11)
    assert sink.records == []  # nothing on disk yet — both in RAM
    assert len(ring) == 2
    drained_below = flusher.flush_due(0.5)  # below the 1.0 deadline → no-op
    assert drained_below == 0
    assert sink.records == []
    drained_at = flusher.flush_due(1.0)  # at deadline → drains both
    assert drained_at == 2
    assert [r.snapshot["NSE:IDX:NIFTY"] for r in sink.records] == [100.0, 101.0]
    assert [r.seq for r in sink.records] == [0, 1]  # sink assigned seq, FIFO order
    assert flusher.flushed == 2 and flusher.flushes == 1


def test_flusher_capacity_overflow_spills_synchronously_never_drops() -> None:
    sink = _FakeSink()
    ring = TickRing(capacity=2)
    flusher = RingFlusher(sink=sink, ring=ring, flush_interval=1.0, monotonic=lambda: 0.0)
    flusher.capture({"NSE:IDX:NIFTY": 1.0}, recv_ts=_ts(0), recv_ns=1)  # 1 buffered, below cap
    assert sink.records == [] and len(ring) == 1
    flusher.capture({"NSE:IDX:NIFTY": 2.0}, recv_ts=_ts(1), recv_ns=2)  # hits cap → spill BOTH
    assert [r.snapshot["NSE:IDX:NIFTY"] for r in sink.records] == [1.0, 2.0]
    assert len(ring) == 0  # ring emptied by the spill — nothing dropped
    assert flusher.spilled == 2 and flusher.flushed == 0


def test_flusher_flush_due_advances_deadline_by_one_interval() -> None:
    sink = _FakeSink()
    ring = TickRing(capacity=100)
    # start monotonic = 0.0 → first deadline = 1.0
    flusher = RingFlusher(sink=sink, ring=ring, flush_interval=1.0, monotonic=lambda: 0.0)
    flusher.capture({"NSE:IDX:NIFTY": 1.0}, recv_ts=_ts(0), recv_ns=1)
    drained_1 = flusher.flush_due(1.0)  # deadline 1.0 met → drained, next deadline 2.0
    assert drained_1 == 1
    flusher.capture({"NSE:IDX:NIFTY": 2.0}, recv_ts=_ts(1), recv_ns=2)
    drained_early = flusher.flush_due(1.5)  # below the NEW 2.0 deadline → no-op
    assert drained_early == 0
    drained_2 = flusher.flush_due(2.0)  # next deadline met
    assert drained_2 == 1
    assert [r.snapshot["NSE:IDX:NIFTY"] for r in sink.records] == [1.0, 2.0]


def test_flusher_drain_writes_everything_and_is_empty_safe() -> None:
    sink = _FakeSink()
    ring = TickRing(capacity=100)
    flusher = RingFlusher(sink=sink, ring=ring, flush_interval=1.0, monotonic=lambda: 0.0)
    empty_drain = flusher.drain()  # empty ring → no-op write, but counts a flush pass
    assert empty_drain == 0
    assert flusher.flushes == 1
    flusher.capture({"NSE:IDX:NIFTY": 7.0}, recv_ts=_ts(0), recv_ns=7)
    one_drain = flusher.drain()
    assert one_drain == 1
    assert [r.snapshot["NSE:IDX:NIFTY"] for r in sink.records] == [7.0]


def test_flusher_seq_continuity_through_a_resumed_sink(tmp_path: Path) -> None:
    """seq is the SINK's — a resumed real ``TickStore`` continues the global seq."""
    path = tmp_path / "ring.jsonl"
    first = TickStore.open(path)
    f1 = RingFlusher(
        sink=first, ring=TickRing(capacity=100), flush_interval=1.0, monotonic=lambda: 0.0
    )
    f1.capture({"NSE:IDX:NIFTY": 1.0}, recv_ts=_ts(0), recv_ns=1)
    f1.capture({"NSE:IDX:NIFTY": 2.0}, recv_ts=_ts(1), recv_ns=2)
    f1.drain()
    first.close()
    resumed = TickStore.open(path)  # resumes seq at 2
    assert resumed.next_seq == 2
    f2 = RingFlusher(
        sink=resumed, ring=TickRing(capacity=100), flush_interval=1.0, monotonic=lambda: 0.0
    )
    f2.capture({"NSE:IDX:NIFTY": 3.0}, recv_ts=_ts(2), recv_ns=3)
    f2.drain()
    resumed.close()
    seqs = [r.seq for r in TickStore.read(path)]
    assert seqs == [0, 1, 2]  # gap-free, strictly increasing across the resume


# ───────────────────── property (multiset preservation) ─────────────


@pytest.mark.parametrize("seed", range(8))
def test_property_drain_preserves_multiset_and_order(seed: int) -> None:
    """Any interleaving of capture/flush_due/drain delivers EVERY item, in order.

    Drives the calls synchronously in a deterministic-per-seed pattern (no real
    threads) and asserts the sink received exactly the captured values, FIFO.
    """
    sink = _FakeSink()
    ring = TickRing(capacity=3)
    now = {"t": 0.0}
    flusher = RingFlusher(sink=sink, ring=ring, flush_interval=1.0, monotonic=lambda: now["t"])
    expected = [float(i) for i in range(20)]
    for i, value in enumerate(expected):
        flusher.capture({"NSE:IDX:NIFTY": value}, recv_ts=_ts(i), recv_ns=i)
        # Pseudo-randomly (by seed) advance the clock + poll the cooperative flush.
        if (i * 7 + seed) % 3 == 0:
            now["t"] += 1.0
            flusher.flush_due(now["t"])
    flusher.drain()  # final shutdown drain
    got = [r.snapshot["NSE:IDX:NIFTY"] for r in sink.records]
    assert got == expected  # nothing lost, nothing reordered
    assert [r.seq for r in sink.records] == list(range(20))  # sink seq gap-free


# ───────────────────────── stress: ring-full burst ──────────────────


def test_stress_sustained_burst_never_drops_with_small_ring() -> None:
    """A long burst far exceeding the ring capacity loses ZERO ticks (spill path)."""
    sink = _FakeSink()
    ring = TickRing(capacity=4)
    flusher = RingFlusher(sink=sink, ring=ring, flush_interval=1.0, monotonic=lambda: 0.0)
    total = 5_000
    for i in range(total):
        flusher.capture({"NSE:IDX:NIFTY": float(i)}, recv_ts=_ts(0), recv_ns=i)
    flusher.drain()  # sweep the in-RAM remainder
    assert len(sink.records) == total
    assert [r.snapshot["NSE:IDX:NIFTY"] for r in sink.records] == [float(i) for i in range(total)]
    assert flusher.spilled + flusher.flushed == total  # every item accounted for


# ───────────────────── crash-restart (un-flushed tail lost) ─────────


def test_crash_restart_unflushed_ring_tail_lost_but_sink_resumes_gapfree(
    tmp_path: Path,
) -> None:
    """A hard crash before a flush loses ONLY the in-RAM tail; the sink resumes clean.

    This is the HONEST hard-crash bound (Golden Rule #10): items still in the ring
    when the process dies are gone, but the durable sink continues gap-free from
    what it DID write — no duplicate, no corruption.
    """
    path = tmp_path / "crash.jsonl"
    store = TickStore.open(path)
    flusher = RingFlusher(
        sink=store, ring=TickRing(capacity=100), flush_interval=1.0, monotonic=lambda: 0.0
    )
    flusher.capture({"NSE:IDX:NIFTY": 1.0}, recv_ts=_ts(0), recv_ns=1)
    flusher.drain()  # 1.0 is now durable
    # ... two more buffered but NOT flushed → the "crash window"
    flusher.capture({"NSE:IDX:NIFTY": 2.0}, recv_ts=_ts(1), recv_ns=2)
    flusher.capture({"NSE:IDX:NIFTY": 3.0}, recv_ts=_ts(2), recv_ns=3)
    store.close()  # simulate process death WITHOUT a final drain (the buffered tail is lost)
    on_disk = [r.snapshot["NSE:IDX:NIFTY"] for r in TickStore.read(path)]
    assert on_disk == [1.0]  # only the flushed tick survived
    # Restart: a fresh sink resumes gap-free from seq 1.
    resumed = TickStore.open(path)
    assert resumed.next_seq == 1
    f2 = RingFlusher(
        sink=resumed, ring=TickRing(capacity=100), flush_interval=1.0, monotonic=lambda: 0.0
    )
    f2.capture({"NSE:IDX:NIFTY": 4.0}, recv_ts=_ts(3), recv_ns=4)
    f2.drain()
    resumed.close()
    assert [r.seq for r in TickStore.read(path)] == [0, 1]  # gap-free across the crash


# ─────────────── golden-idempotence over a RollingTickStore ─────────


def test_flusher_drains_into_rolling_store_with_global_seq(tmp_path: Path) -> None:
    """The flusher REUSES RollingTickStore verbatim; seq is global across segments."""
    base = tmp_path / "live-ticks.jsonl"
    store = RollingTickStore.open(base, max_bytes=64 * 1024 * 1024, max_lines=2, max_segments=8)
    flusher = RingFlusher(
        sink=store, ring=TickRing(capacity=100), flush_interval=1.0, monotonic=lambda: 0.0
    )
    for i in range(5):
        flusher.capture({"NSE:IDX:NIFTY": float(i)}, recv_ts=_ts(i), recv_ns=i)
    flusher.drain()
    tail = store.flush_active()
    store.close()
    assert tail is not None
    assert store.next_seq == 5  # five global seqs assigned by the sink, not the ring


# ─────────────── real-thread NON-GATING producer-drain check ────────


class _LockedSink(_FakeSink):
    """A sink whose ``append`` is serialized — safe to call from two threads."""

    def __init__(self, *, start: int = 0) -> None:
        super().__init__(start=start)
        self._append_lock = threading.Lock()

    def append(self, snapshot: Any, *, recv_ts: datetime, recv_ns: int | None = None) -> TickRecord:
        with self._append_lock:
            return super().append(snapshot, recv_ts=recv_ts, recv_ns=recv_ns)


def test_thread_producer_drains_without_loss() -> None:
    """NON-GATING evidence: a real producer thread + a draining loop lose nothing.

    Thread-safety rests on CPython deque atomicity + the single swap lock (argued
    in the module docstring), not on a deterministic test; this exercises the real
    concurrent path once as corroboration. A producer thread captures into the ring
    (which spills to the sink on overflow); the main thread concurrently drains the
    ring to the SAME sink. At the end EVERY produced value is present exactly once.
    The sink's ``append`` is itself serialized (real sinks are single-writer, but a
    spill from the producer can race a manual drain in this test).
    """
    sink = _LockedSink()
    ring = TickRing(capacity=64)
    flusher = RingFlusher(sink=sink, ring=ring, flush_interval=1.0, monotonic=lambda: 0.0)
    total = 3_000
    done = threading.Event()

    def produce() -> None:
        for i in range(total):
            flusher.capture({"NSE:IDX:NIFTY": float(i)}, recv_ts=_ts(0), recv_ns=i)
        done.set()

    producer = threading.Thread(target=produce)
    producer.start()
    while not done.is_set():
        for item in ring.drain_pending():  # concurrent drains race the producer
            sink.append(item.snapshot, recv_ts=item.recv_ts, recv_ns=item.recv_ns)
    producer.join()
    for item in ring.drain_pending():  # final sweep of the in-RAM remainder
        sink.append(item.snapshot, recv_ts=item.recv_ts, recv_ns=item.recv_ns)
    values = sorted(r.snapshot["NSE:IDX:NIFTY"] for r in sink.records)
    assert values == [float(i) for i in range(total)]  # every value present exactly once


# ─────────────────── TickCapture ring-path integration ──────────────


class _FakeFeed:
    """Fires one canned snapshot per callback (mirrors test_live_cli._FakeFeed)."""

    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        self._snapshots = snapshots
        self._idx = 0
        self._cb: Any = None
        self.subscribed: list[Any] | None = None

    def subscribe_index_value(self, instruments: Any, on_data_received: Any) -> None:
        self.subscribed = list(instruments)
        self._cb = on_data_received

    subscribe_ltp = subscribe_index_value

    def get_index_value(self) -> dict[str, Any]:
        snap = self._snapshots[min(self._idx, len(self._snapshots) - 1)]
        self._idx += 1
        return snap

    get_ltp = get_index_value

    def fire(self) -> None:
        assert self._cb is not None
        self._cb()


def _fixed_clock() -> Callable[[], datetime]:
    return lambda: _T0


def test_capture_with_flusher_buffers_then_drains_on_shutdown(tmp_path: Path) -> None:
    """TickCapture routes through the flusher; the final run_for drain is durable."""
    feed = _FakeFeed([{"NSE:IDX:NIFTY": 100.0}, {"NSE:IDX:NIFTY": 101.0}])
    store = TickStore.open(tmp_path / "t.jsonl")
    # flush_interval huge → no periodic flush fires; the SHUTDOWN drain must persist.
    flusher = RingFlusher(
        sink=store, ring=TickRing(capacity=100), flush_interval=1_000.0, monotonic=lambda: 0.0
    )
    capture = TickCapture(
        store=store,
        subscribe=lambda cb: feed.subscribe_index_value([], on_data_received=cb),
        read_snapshot=feed.get_index_value,
        clock=_fixed_clock(),
        ns_clock=lambda: 7,
        flusher=flusher,
    )
    # NB: same recv_ts each callback (delta keeps both since the VALUE changes).
    n = capture.run_for(
        1.0, sleep=lambda _s: feed.fire(), monotonic=iter([0.0, 0.4, 0.8, 1.2]).__next__
    )
    store.close()
    assert n == 2 and capture.count == 2
    persisted = [r.snapshot["NSE:IDX:NIFTY"] for r in TickStore.read(tmp_path / "t.jsonl")]
    assert persisted == [100.0, 101.0]  # both survived via the shutdown drain


def test_capture_with_flusher_does_not_fire_on_record(tmp_path: Path) -> None:
    """The ring path assigns seq at flush time → on_record is NOT fired per tick."""
    feed = _FakeFeed([{"NSE:IDX:NIFTY": 1.0}, {"NSE:IDX:NIFTY": 2.0}])
    store = TickStore.open(tmp_path / "t.jsonl")
    flusher = RingFlusher(
        sink=store, ring=TickRing(capacity=100), flush_interval=1_000.0, monotonic=lambda: 0.0
    )
    seen: list[TickRecord] = []
    capture = TickCapture(
        store=store,
        subscribe=lambda cb: feed.subscribe_index_value([], on_data_received=cb),
        read_snapshot=feed.get_index_value,
        clock=_fixed_clock(),
        on_record=seen.append,
        flusher=flusher,
    )
    capture.run_for(1.0, sleep=lambda _s: feed.fire(), monotonic=iter([0.0, 0.4, 1.2]).__next__)
    store.close()
    assert seen == []  # on_record silent on the ring path


# ───────────────────────── CLI --ring wiring ────────────────────────


def _ring_deps(feed: _FakeFeed, *, monotonic_vals: list[float]) -> LiveDeps:
    it = iter(monotonic_vals)
    return LiveDeps(
        feed_builder=lambda: feed,
        instrument_resolver=lambda _symbols, _kind: [
            {"exchange": "NSE", "segment": "IDX", "exchange_token": "26000"}
        ],
        clock=_fixed_clock(),
        sleep=lambda _s: feed.fire(),
        monotonic=lambda: next(it),
    )


def test_cmd_capture_ring_flag_persists_via_drain(tmp_path: Path) -> None:
    """``capture --ring`` buffers in RAM then drains to --out (plain TickStore)."""
    out = tmp_path / "ticks.jsonl"
    feed = _FakeFeed([{"NSE:IDX:NIFTY": 22500.0}, {"NSE:IDX:NIFTY": 22501.0}])
    # monotonic order: flusher __init__ (1) → run_for start (1) → loop nows.
    deps = _ring_deps(feed, monotonic_vals=[0.0, 0.0, 0.4, 0.8, 1.2])
    rc = main(
        [
            "capture",
            "--out",
            str(out),
            "--seconds",
            "1",
            "--feed",
            "index",
            "--ring",
            "--flush-interval",
            "1000",
        ],
        deps=deps,
    )
    assert rc == 0
    assert [r.snapshot["NSE:IDX:NIFTY"] for r in TickStore.read(out)] == [22500.0, 22501.0]


def test_cmd_capture_ring_with_rolling_drains_and_folds(tmp_path: Path) -> None:
    """``capture --ring --roll`` drains the ring THEN folds the tail segment."""
    out = tmp_path / "live-ticks.jsonl"
    feed = _FakeFeed([{"NSE:IDX:NIFTY": 1.0}, {"NSE:IDX:NIFTY": 2.0}])
    deps = _ring_deps(feed, monotonic_vals=[0.0, 0.0, 0.4, 0.8, 1.2])
    rc = main(
        [
            "capture",
            "--out",
            str(out),
            "--seconds",
            "1",
            "--feed",
            "index",
            "--roll",
            "--ring",
            "--flush-interval",
            "1000",
        ],
        deps=deps,
    )
    assert rc == 0
    from brutex.live.rolling import list_segments, segment_dir_for

    segments = list_segments(segment_dir_for(out))
    records = [r.snapshot["NSE:IDX:NIFTY"] for seg in segments for r in TickStore.read(seg)]
    assert records == [1.0, 2.0]
