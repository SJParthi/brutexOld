"""Unit tests for ``brutex.adapters.groww.fetch_pipeline`` — the async fetch/process pool.

100% line+branch on the :class:`FetchPipeline` mechanics + the operator's HARD guards:

* STRICTLY BOUNDED — fixed-size pool (M threads, not one-per-window) + a small bounded queue;
  the producer's :meth:`submit` BLOCKS on a full queue (anti-pileup), never accumulates.
* STALL WATCHDOG — in-flight work with no completion for the threshold fires a loud
  ``fetch_pipeline.stalled`` WARNING + the ``on_stall`` hook (never silent idle).
* CLEAN DRAIN — poison-pill + BOUNDED join (a wedged consumer is REPORTED, never hangs).
* NO LEAKED THREADS — after a run + drain the active thread count returns to baseline.
* NO DEADLOCK — full queue + racing producer + shutdown always progresses within a bound.
* RECOVERY — a never-drained window leaves its ledger row UNSETTLED (re-fetched next pull).

The settle-after-durable + byte-parity + the REAL ``_ingest_one_window``/option twin drive
live in ``tests/integration/test_groww_fetch_async_separation.py``.
"""

# Whitebox test of the pipeline internals (queue depth, stop latch, watchdog) — sanctioned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
import time

import pytest
from brutex.adapters.groww.fetch_pipeline import (
    FETCH_PIPELINE_ENV,
    FETCH_PIPELINE_SIZE_ENV,
    FetchPipeline,
    FetchTail,
    RawFetchResult,
    resolve_fetch_pipeline_size,
)
from brutex.data.errors import LakeWriteHalt


def _tail(shard_key: str, process) -> FetchTail:  # type: ignore[no-untyped-def]
    """A :class:`FetchTail` with no bars (the bars are tail-internal here)."""
    return FetchTail(shard_key=shard_key, process=process)


def _result(shard_key: str, process) -> RawFetchResult:  # type: ignore[no-untyped-def]
    return RawFetchResult(tail=_tail(shard_key, process))


# ─────────────────────────── resolve_fetch_pipeline_size ────────────────────────────


class TestResolveFetchPipelineSize:
    """The env-driven on/off + size normalisation (fail-safe to the audited default)."""

    @pytest.mark.parametrize("flag", ["1", "true", "TRUE", "Yes", "on", " on "])
    def test_truthy_flag_enables_default_size(self, flag: str) -> None:
        assert resolve_fetch_pipeline_size(raw=None, flag=flag) == 4

    @pytest.mark.parametrize("flag", [None, "", "0", "false", "no", "off", "garbage"])
    def test_falsey_flag_disables(self, flag: str | None) -> None:
        assert resolve_fetch_pipeline_size(raw="4", flag=flag) is None

    def test_explicit_in_range_size(self) -> None:
        assert resolve_fetch_pipeline_size(raw="2", flag="1") == 2

    @pytest.mark.parametrize("raw", ["0", "-1", "999", "notint", "", "   "])
    def test_bad_or_out_of_range_size_falls_back_to_default(self, raw: str) -> None:
        assert resolve_fetch_pipeline_size(raw=raw, flag="1") == 4

    def test_none_size_reads_default(self) -> None:
        assert resolve_fetch_pipeline_size(raw=None, flag="1") == 4

    def test_reads_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FETCH_PIPELINE_ENV, "1")
        monkeypatch.setenv(FETCH_PIPELINE_SIZE_ENV, "3")
        assert resolve_fetch_pipeline_size() == 3

    def test_environment_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FETCH_PIPELINE_ENV, raising=False)
        assert resolve_fetch_pipeline_size() is None


# ───────────────────────────── construction guards ──────────────────────────────


class TestConstruction:
    def test_rejects_zero_workers(self) -> None:
        with pytest.raises(ValueError, match="workers must be >= 1"):
            FetchPipeline(workers=0)

    def test_rejects_zero_queue(self) -> None:
        with pytest.raises(ValueError, match="queue_maxsize must be >= 1"):
            FetchPipeline(workers=1, queue_maxsize=0)

    def test_rejects_non_positive_watchdog(self) -> None:
        with pytest.raises(ValueError, match="stall_watchdog_seconds must be > 0"):
            FetchPipeline(workers=1, stall_watchdog_seconds=0)

    def test_rejects_non_positive_join_timeout(self) -> None:
        with pytest.raises(ValueError, match="drain_join_timeout_seconds must be > 0"):
            FetchPipeline(workers=1, drain_join_timeout_seconds=0)

    def test_workers_property(self) -> None:
        pipeline = FetchPipeline(workers=2)
        try:
            assert pipeline.workers == 2
        finally:
            pipeline.drain()


# ─────────────────────────── happy path + sharding ──────────────────────────────


class TestProcessingAndSharding:
    def test_every_submitted_window_is_processed(self) -> None:
        seen: list[str] = []
        lock = threading.Lock()

        def _proc(tail: FetchTail) -> None:
            with lock:
                seen.append(tail.shard_key)

        pipeline = FetchPipeline(workers=3)
        for i in range(20):
            pipeline.submit(_result(f"series-{i}", _proc))
        pipeline.drain()
        assert sorted(seen) == sorted(f"series-{i}" for i in range(20))

    def test_same_series_routes_to_one_consumer(self) -> None:
        """All windows of ONE series land on ONE consumer (the per-partition-lock invariant)."""
        threads_seen: set[int] = set()
        lock = threading.Lock()

        def _proc(_tail: FetchTail) -> None:
            with lock:
                threads_seen.add(threading.get_ident())
            time.sleep(0.005)

        pipeline = FetchPipeline(workers=4, queue_maxsize=64)
        for _ in range(30):
            pipeline.submit(_result("one-series", _proc))
        pipeline.drain()
        assert len(threads_seen) == 1

    def test_totals_folded(self) -> None:
        def _proc(_tail: FetchTail) -> None:
            return None

        pipeline = FetchPipeline(workers=2)
        pipeline.submit(_result("s", _proc))
        pipeline.fold_totals(bars_appended=120, conflicts=2)
        pipeline.fold_totals(bars_appended=5, conflicts=0)
        pipeline.drain()
        processed, bars, conflicts = pipeline.totals
        assert (processed, bars, conflicts) == (2, 125, 2)


# ─────────────────────── anti-pileup / bounded backpressure ─────────────────────


class TestAntiPileup:
    def test_full_queue_blocks_producer_caps_at_maxsize(self) -> None:
        """Stall the single consumer → the queue caps at maxsize and submit() BLOCKS.

        With one consumer wedged on the first item, a queue of maxsize=2 holds at most 2
        more; the (maxsize+1+1)th submit must BLOCK (never accumulate beyond the bound).
        """
        release = threading.Event()
        first_in = threading.Event()

        def _proc(_tail: FetchTail) -> None:
            first_in.set()
            release.wait(timeout=5.0)

        pipeline = FetchPipeline(workers=1, queue_maxsize=2)
        # 1st is pulled by the consumer (which blocks); 2nd + 3rd fill the queue (maxsize=2).
        pipeline.submit(_result("s", _proc))
        assert first_in.wait(timeout=2.0)
        pipeline.submit(_result("s", _proc))
        pipeline.submit(_result("s", _proc))
        # The queue is now FULL — the next submit must BLOCK. Prove it from a side thread.
        blocked = threading.Event()
        returned = threading.Event()

        def _blocking_submit() -> None:
            blocked.set()
            pipeline.submit(_result("s", _proc))
            returned.set()

        t = threading.Thread(target=_blocking_submit, daemon=True)
        t.start()
        assert blocked.wait(timeout=2.0)
        # It must NOT return while the consumer is wedged (the anti-pileup guard).
        assert not returned.wait(timeout=0.3)
        # The internal queue never exceeded its bound.
        assert pipeline._queues[0].qsize() <= 2
        # Release → the wedged consumer drains, the blocked submit completes, drain joins.
        release.set()
        assert returned.wait(timeout=5.0)
        t.join(timeout=5.0)
        pipeline.drain()


# ──────────────────────────────── stall watchdog ────────────────────────────────


class TestStallWatchdog:
    def test_stall_fires_warning_and_hook(self) -> None:
        """In-flight work + no completion past the threshold → loud WARNING + on_stall hook."""
        fake_now = [0.0]
        stalls: list[float] = []
        block = threading.Event()

        def _clock() -> float:
            return fake_now[0]

        def _proc(_tail: FetchTail) -> None:
            block.wait(timeout=5.0)

        pipeline = FetchPipeline(
            workers=1,
            queue_maxsize=4,
            stall_watchdog_seconds=10.0,
            on_stall=stalls.append,
            monotonic=_clock,
        )
        try:
            pipeline.submit(_result("s", _proc))
            # Advance the fake clock past the threshold; the watchdog poll (real 1 s) sees it.
            fake_now[0] = 100.0
            deadline = time.monotonic() + 5.0
            while not stalls and time.monotonic() < deadline:
                time.sleep(0.05)
            assert stalls, "stall watchdog never fired"
            assert stalls[0] >= 10.0
        finally:
            block.set()
            pipeline.drain()

    def test_no_stall_when_idle_no_inflight(self) -> None:
        """No in-flight work → no stall even if the clock jumps (idle is not stuck)."""
        fake_now = [0.0]
        stalls: list[float] = []
        pipeline = FetchPipeline(
            workers=1,
            stall_watchdog_seconds=1.0,
            on_stall=stalls.append,
            monotonic=lambda: fake_now[0],
        )
        try:
            fake_now[0] = 1000.0
            # Give the watchdog a couple of polls — it must stay silent (nothing in flight).
            pipeline._check_stall()
            pipeline._check_stall()
            assert stalls == []
        finally:
            pipeline.drain()

    def test_stall_edge_latched_once_then_rearms(self) -> None:
        """The WARNING fires ONCE per episode (edge-latched), re-armed by the next completion."""
        fake_now = [0.0]
        stalls: list[float] = []
        gate = threading.Event()

        def _proc(_tail: FetchTail) -> None:
            gate.wait(timeout=5.0)

        pipeline = FetchPipeline(
            workers=1,
            queue_maxsize=4,
            stall_watchdog_seconds=5.0,
            on_stall=stalls.append,
            monotonic=lambda: fake_now[0],
        )
        try:
            pipeline.submit(_result("s", _proc))
            fake_now[0] = 100.0
            pipeline._check_stall()
            pipeline._check_stall()
            assert len(stalls) == 1
        finally:
            gate.set()
            pipeline.drain()

    def test_stall_warning_without_hook(self) -> None:
        """A stall with NO ``on_stall`` hook still fires the WARNING (the hook is optional)."""
        fake_now = [0.0]
        gate = threading.Event()

        def _proc(_tail: FetchTail) -> None:
            gate.wait(timeout=5.0)

        pipeline = FetchPipeline(
            workers=1,
            queue_maxsize=4,
            stall_watchdog_seconds=5.0,
            on_stall=None,
            monotonic=lambda: fake_now[0],
        )
        try:
            pipeline.submit(_result("s", _proc))
            fake_now[0] = 100.0
            # No hook → only the WARNING path; must not raise (the on_stall branch is skipped).
            pipeline._check_stall()
        finally:
            gate.set()
            pipeline.drain()

    def test_on_stall_hook_error_is_swallowed(self) -> None:
        """A throwing ``on_stall`` hook can never crash the watchdog."""
        fake_now = [0.0]

        def _bad_hook(_idle: float) -> None:
            raise RuntimeError("boom")

        gate = threading.Event()

        def _proc(_tail: FetchTail) -> None:
            gate.wait(timeout=5.0)

        pipeline = FetchPipeline(
            workers=1,
            queue_maxsize=4,
            stall_watchdog_seconds=5.0,
            on_stall=_bad_hook,
            monotonic=lambda: fake_now[0],
        )
        try:
            pipeline.submit(_result("s", _proc))
            fake_now[0] = 100.0
            pipeline._check_stall()
        finally:
            gate.set()
            pipeline.drain()


# ──────────────────────────── failure / halt handling ──────────────────────────


class TestFailureHandling:
    def test_lake_write_halt_trips_pool_and_reraises_on_submit(self) -> None:
        """A consumer disk-full trips the pool-wide stop; the next submit re-raises it."""

        def _halt(_tail: FetchTail) -> None:
            raise LakeWriteHalt("disk_full")

        pipeline = FetchPipeline(workers=1, queue_maxsize=4)
        pipeline.submit(_result("s", _halt))
        # The consumer trips the stop asynchronously; a subsequent submit re-raises it.
        deadline = time.monotonic() + 5.0
        raised: LakeWriteHalt | None = None
        while time.monotonic() < deadline:
            try:
                pipeline.submit(_result("s", _halt))
            except LakeWriteHalt as exc:
                raised = exc
                break
            time.sleep(0.02)
        assert raised is not None
        assert raised.reason == "disk_full"
        with pytest.raises(LakeWriteHalt):
            pipeline.drain()

    def test_only_first_failure_recorded(self) -> None:
        """Two consumers failing → the FIRST exception wins; the second ``_trip`` is a no-op."""
        first = LakeWriteHalt("disk_full")
        second = ValueError("later")
        pipeline = FetchPipeline(workers=2, queue_maxsize=4)
        try:
            pipeline._trip(first)
            pipeline._trip(second)
            assert pipeline._exc is first
        finally:
            with pytest.raises(LakeWriteHalt):
                pipeline.drain()

    def test_raise_if_stopped_defensive_no_exc(self) -> None:
        """Defensive: ``_stopped`` set with NO captured exc → submit does not raise (the guard)."""
        pipeline = FetchPipeline(workers=1, queue_maxsize=4)
        try:
            pipeline._stopped.set()
            # submit must NOT raise (the ``exc is not None`` guard is False) — it enqueues.
            pipeline.submit(_result("s", lambda _t: None))
        finally:
            pipeline._stopped.clear()
            pipeline.drain()

    def test_generic_consumer_failure_reraised_at_drain(self) -> None:
        """A non-halt tail failure is captured + re-raised at drain (never swallowed)."""

        def _boom(_tail: FetchTail) -> None:
            raise ValueError("tail bug")

        pipeline = FetchPipeline(workers=1, queue_maxsize=4)
        pipeline.submit(_result("s", _boom))
        with pytest.raises(ValueError, match="tail bug"):
            pipeline.drain()

    def test_after_halt_subsequent_windows_dropped(self) -> None:
        """Once stopped, later windows are DROPPED (not re-run) — re-fetched next pull."""
        processed: list[int] = []
        lock = threading.Lock()
        halted = threading.Event()

        def _proc(_tail: FetchTail) -> None:
            with lock:
                processed.append(1)
            if not halted.is_set():
                halted.set()
                raise LakeWriteHalt("out_of_memory")

        pipeline = FetchPipeline(workers=1, queue_maxsize=64)
        pipeline.submit(_result("s", _proc))
        assert halted.wait(timeout=2.0)
        # Submit more AFTER the stop is observable; they are dropped fail-fast.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                pipeline.submit(_result("s", _proc))
            except LakeWriteHalt:
                break
            time.sleep(0.01)
        with pytest.raises(LakeWriteHalt):
            pipeline.drain()
        # The first window ran once; the post-halt windows were not processed again.
        assert sum(processed) == 1


# ──────────────────────── clean drain / no leaked threads ───────────────────────


class TestDrainLifecycle:
    def test_no_leaked_threads_after_drain(self) -> None:
        """After a run + drain, the active thread count returns to baseline (no orphans)."""
        baseline = threading.active_count()

        def _proc(_tail: FetchTail) -> None:
            return None

        pipeline = FetchPipeline(workers=3)
        # The pool + watchdog raised the count above baseline.
        assert threading.active_count() > baseline
        for i in range(10):
            pipeline.submit(_result(f"s{i}", _proc))
        pipeline.drain()
        # All consumer + watchdog threads joined → back to baseline.
        deadline = time.monotonic() + 5.0
        while threading.active_count() > baseline and time.monotonic() < deadline:
            time.sleep(0.02)
        assert threading.active_count() == baseline

    def test_drain_is_idempotent(self) -> None:
        pipeline = FetchPipeline(workers=2)
        pipeline.drain()
        pipeline.drain()  # second call: no live threads → no-op (no raise, no hang)

    def test_drain_join_timeout_reported_not_hung(self) -> None:
        """A wedged consumer is REPORTED via the bounded join, never hangs the pull forever."""
        wedge = threading.Event()

        def _wedged(_tail: FetchTail) -> None:
            wedge.wait(timeout=30.0)  # held past the tiny join timeout below

        pipeline = FetchPipeline(workers=1, queue_maxsize=4, drain_join_timeout_seconds=0.2)
        pipeline.submit(_result("s", _wedged))
        # Drain must RETURN within a bound (the join times out + reports), not hang forever.
        start = time.monotonic()
        pipeline.drain()
        elapsed = time.monotonic() - start
        assert elapsed < 5.0  # bounded — did not hang on the wedged consumer
        # Release so the daemon thread can exit cleanly afterwards.
        wedge.set()


# ──────────────────────────────── no deadlock ───────────────────────────────────


class TestNoDeadlock:
    def test_full_queue_racing_producer_and_shutdown_progresses(self) -> None:
        """Many producers racing a small bounded queue + a slow consumer always completes."""
        done: list[int] = []
        lock = threading.Lock()

        def _proc(_tail: FetchTail) -> None:
            time.sleep(0.001)
            with lock:
                done.append(1)

        pipeline = FetchPipeline(workers=2, queue_maxsize=2)

        def _producer(n: int) -> None:
            for i in range(n):
                pipeline.submit(_result(f"series-{i % 5}", _proc))

        producers = [threading.Thread(target=_producer, args=(25,), daemon=True) for _ in range(4)]
        for p in producers:
            p.start()
        for p in producers:
            p.join(timeout=15.0)
            assert not p.is_alive(), "a producer deadlocked on a full queue"
        pipeline.drain()
        assert sum(done) == 100  # every window made progress (no deadlock, no loss)
