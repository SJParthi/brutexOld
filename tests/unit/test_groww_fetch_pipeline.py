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
    _shard_index,
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
    """The env-driven default-ON + kill-switch + size normalisation (fail-safe to default)."""

    @pytest.mark.parametrize(
        "flag", [None, "", "garbage", "1", "true", "TRUE", "Yes", "on", " on "]
    )
    def test_unset_or_non_falsy_flag_enables_default_size(self, flag: str | None) -> None:
        """DEFAULT ON: unset / blank / unrecognised / truthy → the pipeline is enabled."""
        assert resolve_fetch_pipeline_size(raw=None, flag=flag) == 4

    @pytest.mark.parametrize("flag", ["0", "false", "FALSE", "no", "No", "off", "OFF", " off "])
    def test_falsy_flag_kill_switches(self, flag: str) -> None:
        """KILL-SWITCH: only an explicit falsy token disables the pipeline (sync path)."""
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

    def test_environment_on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An UNSET env flag now resolves ON (the default-flip)."""
        monkeypatch.delenv(FETCH_PIPELINE_ENV, raising=False)
        monkeypatch.delenv(FETCH_PIPELINE_SIZE_ENV, raising=False)
        assert resolve_fetch_pipeline_size() == 4

    def test_environment_kill_switch_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ``=off`` kill-switch disables the pipeline via the environment."""
        monkeypatch.setenv(FETCH_PIPELINE_ENV, "off")
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

    def test_tail_transient_folded_and_surfaced(self) -> None:
        """A tail-side transient defer is counted as processed + surfaced (MAJOR-2 fix)."""
        pipeline = FetchPipeline(workers=2)
        pipeline.fold_totals(bars_appended=10, conflicts=0)
        pipeline.fold_tail_transient()
        pipeline.fold_tail_transient()
        assert pipeline.tail_transients == 2
        # A transient is still a processed window (it advanced + ledger-recorded FAILED).
        processed, bars, conflicts = pipeline.totals
        assert (processed, bars, conflicts) == (3, 10, 0)
        # A transient does NOT trip the pool-wide stop — drain is clean (no re-raise).
        pipeline.drain()

    def test_per_series_totals_accumulate_by_key(self) -> None:
        """The completion-count fix: bars/conflicts/transients accumulate per (symbol, interval)."""
        pipeline = FetchPipeline(workers=2)
        # No series_key → pull-level only, NOT attributed to any series.
        pipeline.fold_totals(bars_appended=7, conflicts=0)
        # Per-series folds for two distinct series + a repeat on the first.
        pipeline.fold_totals(bars_appended=10, conflicts=1, series_key=("13", "1day"))
        pipeline.fold_totals(bars_appended=5, conflicts=0, series_key=("13", "1day"))
        pipeline.fold_totals(bars_appended=3, conflicts=0, series_key=("25", "1minute"))
        pipeline.fold_tail_transient(("25", "1minute"))
        per_series = pipeline.per_series_totals()
        assert per_series[("13", "1day")].bars_appended == 15
        assert per_series[("13", "1day")].conflicts == 1
        assert per_series[("13", "1day")].transient == 0
        assert per_series[("25", "1minute")].bars_appended == 3
        assert per_series[("25", "1minute")].transient == 1
        # The keyless fold is in the pull-level totals but NOT attributed to any series.
        assert sum(t.bars_appended for t in per_series.values()) == 18  # 15 + 3, not the keyless 7
        pipeline.drain()

    def test_per_series_transient_is_last_write_wins_not_cumulative(self) -> None:
        """MAJOR-1: an early-round transient CLEARED by a later durable success → transient==0.

        DEC-FNO-ASYNC-PIPELINE-WIRE-001 MAJOR-1 regression. The per-series ``transient`` flag is
        LAST-WRITE-WINS, NOT a cumulative count: a contract that tail-transient'd in until-clean
        round 1 (0 bars) but DURABLY SUCCEEDED in round 2 (50 bars) must read ``transient == 0``
        (resolved), so the reconcile does NOT falsely report it still-failing with its bars
        already in the lake. The cumulative bug left ``transient == 1`` forever.
        """
        pipeline = FetchPipeline(workers=1)
        key = ("ABC", "1minute")
        # Round 1: the window deferred on a PartitionLockTimeout (0 bars).
        pipeline.fold_tail_transient(key)
        assert pipeline.per_series_totals()[key].transient == 1
        # Round 2: the SAME window is re-pulled and durably succeeds (50 bars).
        pipeline.fold_totals(bars_appended=50, conflicts=0, series_key=key)
        per_series = pipeline.per_series_totals()
        assert per_series[key].transient == 0  # CLEARED — not a stale cumulative 1
        assert per_series[key].bars_appended == 50  # bars still accumulate (durable)
        pipeline.drain()

    def test_per_series_transient_success_then_transient_stays_failing(self) -> None:
        """MAJOR-1 (the other direction): a success FOLLOWED by a transient stays still-failing.

        The robust fix must NOT use the naive ``still = transient if bars==0 else 0`` shortcut —
        that would FALSE-mark-complete a series whose FINAL fold was a transient just because an
        earlier fold had bars. LAST-WRITE-WINS keeps ``transient == 1`` when the newest outcome is
        a transient, even with accumulated bars (DEC-FNO-ASYNC-PIPELINE-WIRE-001 MAJOR-1).
        """
        pipeline = FetchPipeline(workers=1)
        key = ("ABC", "1minute")
        pipeline.fold_totals(bars_appended=50, conflicts=0, series_key=key)  # earlier success
        pipeline.fold_tail_transient(key)  # newest outcome: a transient
        per_series = pipeline.per_series_totals()
        assert per_series[key].transient == 1  # still failing (last fold wins)
        assert per_series[key].bars_appended == 50  # the earlier bars still accumulate
        pipeline.drain()


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

    def test_blocked_producer_release_on_stop_accounts_in_flight(self) -> None:
        """A producer blocked on a FULL queue that a peer halt releases must NOT leave a
        phantom in-flight window (DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996).

        The window is counted ``_submitted`` BEFORE the (blocking) put; the ``_draining``
        drop branch already calls ``_mark_completed`` but the ``_stopped`` re-raise branch
        did NOT — leaving ``in_flight > 0`` after a halt and firing one SPURIOUS
        ``fetch_pipeline.stalled`` WARNING during teardown. This asserts the re-raise path
        now accounts the window so in-flight returns to 0 and the stall stays honest.
        """
        release = threading.Event()
        first_in = threading.Event()

        def _proc(_tail: FetchTail) -> None:
            first_in.set()
            release.wait(timeout=5.0)  # wedge the consumer so the queue fills behind it

        pipeline = FetchPipeline(workers=1, queue_maxsize=1)
        # 1st is pulled by the consumer (which wedges); 2nd fills the single queue slot.
        pipeline.submit(_result("s", _proc))
        assert first_in.wait(timeout=2.0)
        pipeline.submit(_result("s", _proc))  # queue now FULL — the next submit BLOCKS

        blocked = threading.Event()
        raised = threading.Event()

        def _blocking_submit() -> None:
            blocked.set()
            try:
                pipeline.submit(_result("s", _proc))  # blocks on the full queue
            except LakeWriteHalt:
                raised.set()  # released by the peer halt re-raise (the fixed path)

        t = threading.Thread(target=_blocking_submit, daemon=True)
        t.start()
        assert blocked.wait(timeout=2.0)
        assert not raised.wait(timeout=0.3)  # still blocked while the queue is full
        # A peer consumer trips the pool-wide stop → the blocked producer's full-queue
        # recheck re-raises AND marks its window completed.
        pipeline._trip(LakeWriteHalt("disk_full"))
        assert raised.wait(timeout=5.0), "blocked producer was not released on stop"
        t.join(timeout=5.0)
        # The blocked window was counted submitted then accounted on the re-raise: every
        # submitted window is matched by a completion now that the consumer can drain.
        release.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with pipeline._progress_lock:
                if pipeline._submitted == pipeline._completed:
                    break
            time.sleep(0.02)
        with pipeline._progress_lock:
            in_flight = pipeline._submitted - pipeline._completed
        assert in_flight == 0, f"phantom in-flight window after halt: {in_flight}"
        # No spurious stall: with in-flight == 0, the watchdog never fires.
        stalls: list[float] = []
        pipeline._on_stall = stalls.append
        pipeline._check_stall()
        assert stalls == []
        with pytest.raises(LakeWriteHalt):
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

    def test_drain_does_not_hang_when_queue_full_and_consumer_wedged(self) -> None:
        """BLOCKER regression: a FULL queue + a wedged consumer must NOT hang ``drain()``.

        1 consumer, queue_maxsize=2, a wedged sink, 3 same-shard submits → the queue fills
        while the consumer is stuck inside ``sink.append``. The OLD code's UNBOUNDED
        ``q.put(poison)`` blocked FOREVER on the full queue, so the bounded join was never
        reached and the pull's ``finally`` hung (ledger never persisted, no signal). The fix
        delivers the poison via a BOUNDED put (falling through to the join on ``queue.Full``)
        + a ``_draining`` flag the consumer's timed get also checks, so ``drain()`` returns
        BOUNDED and REPORTS the wedged consumer.
        """
        release = threading.Event()
        first_in = threading.Event()

        def _wedged(_tail: FetchTail) -> None:
            first_in.set()
            release.wait(timeout=30.0)  # wedged inside sink.append, queue fills behind it

        pipeline = FetchPipeline(workers=1, queue_maxsize=2, drain_join_timeout_seconds=0.3)
        pipeline.submit(_result("s", _wedged))  # consumer pulls + wedges
        assert first_in.wait(timeout=2.0)
        pipeline.submit(_result("s", _wedged))  # queue slot 1
        pipeline.submit(_result("s", _wedged))  # queue slot 2 → queue now FULL
        assert pipeline._queues[0].full()
        # Drain from a side thread and prove it RETURNS within a bound (would hang pre-fix).
        returned = threading.Event()

        def _do_drain() -> None:
            pipeline.drain()
            returned.set()

        t = threading.Thread(target=_do_drain, daemon=True)
        t.start()
        assert returned.wait(timeout=5.0), "drain() hung on a full queue with a wedged consumer"
        t.join(timeout=5.0)
        # The wedged consumer is still alive (reported, not joined) — the poison could not be
        # enqueued on the full queue; the bounded join reported it.
        assert pipeline._threads[0].is_alive()
        # Release so the daemon thread exits cleanly afterwards.
        release.set()

    def test_watchdog_alive_across_wedged_drain_surfaces_stall(self) -> None:
        """The stall watchdog stays ALIVE across a wedged drain join (BLOCKER companion).

        The old code set ``_watchdog_stop`` BEFORE the drain join, so a wedge during the
        (bounded-but-real) join was never surfaced. The fix stops the watchdog only AFTER the
        join, so an in-flight wedge during drain still fires ``on_stall``.
        """
        fake_now = [0.0]
        stalls: list[float] = []
        release = threading.Event()
        first_in = threading.Event()

        def _wedged(_tail: FetchTail) -> None:
            first_in.set()
            release.wait(timeout=30.0)

        pipeline = FetchPipeline(
            workers=1,
            queue_maxsize=2,
            stall_watchdog_seconds=10.0,
            on_stall=stalls.append,
            drain_join_timeout_seconds=2.0,
            monotonic=lambda: fake_now[0],
        )
        pipeline.submit(_result("s", _wedged))
        assert first_in.wait(timeout=2.0)
        # Advance the fake clock past the stall threshold so the watchdog SHOULD fire while the
        # consumer is wedged. Drain in a side thread: the watchdog must still be polling.
        fake_now[0] = 100.0
        drained = threading.Event()
        threading.Thread(target=lambda: (pipeline.drain(), drained.set()), daemon=True).start()
        deadline = time.monotonic() + 5.0
        while not stalls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stalls, "watchdog did not surface the stall during the wedged drain"
        release.set()
        assert drained.wait(timeout=5.0)


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

    def test_blocked_submit_released_when_peer_consumer_trips_stop(self) -> None:
        """B1 liveness fix: a producer blocked in submit()'s put on a WEDGED-FOREVER consumer
        is RELEASED the moment a PEER consumer trips the pool-wide stop.

        Regression for the SUBMIT-path liveness gap (distinct from the drain() poison-put fix):
        before the fix, ``submit``'s ``put`` was UNBOUNDED with no ``_stopped`` recheck, so a
        producer back-pressured on a full shard queue whose consumer was wedged forever would
        wedge the fetch thread FOREVER even after the pull halted ("looks alive but stuck").
        This test HANGS against the old unbounded put and PASSES after the bounded, stop-aware
        retry. workers=2 / queue_maxsize=1 with a deterministic shard split.
        """
        # Route a known key to shard 0 (the wedged consumer) and another to shard 1 (the peer
        # that will trip the stop) so the split is deterministic, not luck.
        wedge_key = next(k for k in (f"w{i}" for i in range(1000)) if _shard_index(k, 2) == 0)
        peer_key = next(k for k in (f"p{i}" for i in range(1000)) if _shard_index(k, 2) == 1)

        wedged_in = threading.Event()
        never = threading.Event()  # never set → the wedged consumer hangs forever

        def _wedge(_tail: FetchTail) -> None:
            wedged_in.set()
            never.wait(timeout=30.0)

        def _halt(_tail: FetchTail) -> None:
            raise LakeWriteHalt("disk_full")

        pipeline = FetchPipeline(workers=2, queue_maxsize=1)
        # Consumer 0 pulls this and hangs forever inside the tail.
        pipeline.submit(_result(wedge_key, _wedge))
        assert wedged_in.wait(timeout=3.0)
        # Fill shard 0's bounded queue (maxsize=1) so the NEXT submit to shard 0 blocks.
        pipeline.submit(_result(wedge_key, _wedge))

        entered = threading.Event()
        released = threading.Event()
        outcome: list[BaseException | None] = []

        def _blocked_producer() -> None:
            entered.set()
            try:
                pipeline.submit(_result(wedge_key, _wedge))
                outcome.append(None)
            except BaseException as exc:  # a stop re-raise IS a valid release
                outcome.append(exc)
            released.set()

        producer = threading.Thread(target=_blocked_producer, daemon=True)
        producer.start()
        assert entered.wait(timeout=3.0)
        # The producer must be BLOCKED (back-pressure) while the pull is live + the queue is full.
        assert not released.wait(timeout=0.6), "producer should block before the stop"
        # Trip the pool-wide stop via the PEER consumer (shard 1).
        pipeline.submit(_result(peer_key, _halt))
        # The blocked producer is RELEASED within a bound (the B1 fix) — never wedged forever.
        assert released.wait(timeout=10.0), "blocked producer was NOT released by the stop (B1)"
        producer.join(timeout=5.0)
        assert not producer.is_alive()
        # It was released by the captured halt re-raising on the producer thread (not a silent
        # drop) — the stop reaches the spine's _GLOBAL_STOP_ERRORS path.
        assert isinstance(outcome[0], LakeWriteHalt)
        never.set()
        with pytest.raises(LakeWriteHalt):
            pipeline.drain()

    def test_blocked_submit_dropped_when_draining(self) -> None:
        """B1 fix drain branch: a producer blocked in submit()'s put when a pull-end DRAIN is
        signalled (its shard consumer wedged, queue full) DROPS the window fail-fast and returns
        — the window re-fetches next pull (idempotent keep-first), never an unbounded wedge.
        """
        wedged_in = threading.Event()
        never = threading.Event()

        def _wedge(_tail: FetchTail) -> None:
            wedged_in.set()
            never.wait(timeout=30.0)

        pipeline = FetchPipeline(workers=1, queue_maxsize=1)
        pipeline.submit(_result("s", _wedge))  # consumer pulls + hangs forever
        assert wedged_in.wait(timeout=3.0)
        pipeline.submit(_result("s", _wedge))  # fills the queue (maxsize=1)

        entered = threading.Event()
        released = threading.Event()

        def _blocked_producer() -> None:
            entered.set()
            pipeline.submit(_result("s", _wedge))  # blocks in the put
            released.set()

        producer = threading.Thread(target=_blocked_producer, daemon=True)
        producer.start()
        assert entered.wait(timeout=3.0)
        assert not released.wait(timeout=0.6), "producer should block before draining"
        # Signal drain → the blocked producer's full-queue retry sees _draining and DROPS the
        # window (returns), instead of wedging forever on the wedged consumer.
        pipeline._draining.set()
        assert released.wait(timeout=10.0), "blocked producer was NOT released by drain (B1)"
        producer.join(timeout=5.0)
        assert not producer.is_alive()
        never.set()
        # A clean drain (no consumer failure) — the wedged consumer is REPORTED, never hangs.
        pipeline.drain()
