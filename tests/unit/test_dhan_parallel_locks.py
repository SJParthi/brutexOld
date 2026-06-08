"""Concurrency-regression tests for the parallel-ingest thread-safety locks.

DEC-DHAN-PARALLEL-INGEST-001 adds a :class:`threading.Lock` to each shared
mutable object the parallelised Dhan ingest fan-out touches concurrently. These
tests run each guarded object under a high-contention barrier-synchronised
thread storm and assert the lock holds its invariant:

* ``DhanRateController`` — concurrent ``on_rate_limit`` / ``on_success`` /
  ``pace`` / ``snapshot`` never corrupt the step-ladder state.
* ``CallMeter.record`` — every concurrent record is counted exactly once (the
  running aggregates never tear).
* ``OptionDedupIndex.add_if_new`` — a content key is admitted EXACTLY once even
  when N threads race the same key (the idempotence guard, Rule #7).
* ``FetchCoverage.mark_complete`` — every distinct window is recorded once and
  ``to_records`` never tears mid-write.
* ``Catalog.upsert`` — concurrent upserts of distinct rows all land (DuckDB is
  not concurrency-safe without the lock).

They are deterministic: a fake clock / fake sleep keeps the controller off the
wall clock, and the assertions are on exact counts, not timing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brutex.adapters.dhan.client import CallMeter
from brutex.adapters.dhan.rate_control import (
    CEILING_RPS,
    FLOOR_RPS,
    RATE_LIMIT_STORM_FLOOR_RPS,
    DhanRateController,
)
from brutex.data.catalog import Catalog
from brutex.data.fetch_coverage import FetchCoverage, WindowKey
from brutex.data.models import CatalogEntry
from brutex.data.option_dedup import OptionDedupIndex, option_bar_key


def _run_storm(fn: Callable[[int, int], None], *, threads: int, per_thread: int) -> None:
    """Run *fn(thread_idx, op_idx)* across *threads* workers, *per_thread* ops each.

    A :class:`threading.Barrier` releases all workers at once so the critical
    sections genuinely overlap (maximising the chance an unlocked race shows up).
    Any exception raised inside a worker is captured and re-raised on the main
    thread so the test fails loudly instead of hanging.
    """
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            for op in range(per_thread):
                fn(idx, op)
        except Exception as exc:  # surfaced on the main thread (worker-local capture)
            with err_lock:
                errors.append(exc)

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=30)
    if errors:
        raise errors[0]


class _Clock:
    """A controllable monotonic clock (single value; tests never advance it)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class TestDhanRateControllerConcurrency:
    """The shared GOVERNOR survives concurrent hooks without corrupting state."""

    def test_concurrent_hooks_keep_rps_in_envelope(self) -> None:
        clock = _Clock()
        ctl = DhanRateController(clock=clock, sleep=lambda _s: None)

        def op(idx: int, _op: int) -> None:
            # Mix every public hook so all four critical sections contend.
            if idx % 3 == 0:
                ctl.on_rate_limit()
            elif idx % 3 == 1:
                ctl.on_success()
            else:
                ctl.pace()
            ctl.snapshot()

        _run_storm(op, threads=12, per_thread=200)
        # The rate must always remain a valid rung in [FLOOR, CEILING] — a torn
        # step (lost/duplicated decrement) would land it outside the envelope.
        assert FLOOR_RPS <= ctl.current_rps <= CEILING_RPS
        snap = ctl.snapshot()
        current_rps = snap["current_rps"]
        recent_hits = snap["recent_hits"]
        assert isinstance(current_rps, float)
        assert isinstance(recent_hits, int)
        assert FLOOR_RPS <= current_rps <= CEILING_RPS
        assert recent_hits >= 0

    def test_concurrent_429s_step_down_to_storm_floor_atomically(self) -> None:
        # Many threads hammering 429s is a SUSTAINED storm: it must drive the rate
        # PAST the soft FLOOR_RPS (3) down to the deep RATE_LIMIT_STORM_FLOOR_RPS (1)
        # and stop there — never below (a double-step would undershoot the floor).
        # 800 hits on a frozen clock cross both the 3-hit soft-floor rungs (5→4→3)
        # and the 30-hit STORM_HITS rungs (3→2→1) (DEC-DHAN-RATE-STORM-FLOOR-001).
        clock = _Clock()
        ctl = DhanRateController(clock=clock, sleep=lambda _s: None)

        def op(_idx: int, _op: int) -> None:
            ctl.on_rate_limit()

        _run_storm(op, threads=8, per_thread=100)
        assert ctl.current_rps == RATE_LIMIT_STORM_FLOOR_RPS  # clamped, never below


class TestCallMeterConcurrency:
    """Every concurrent record is counted exactly once (no torn aggregate)."""

    def test_record_counts_every_call_exactly_once(self) -> None:
        meter = CallMeter()
        threads, per_thread = 16, 250

        def op(_idx: int, op_i: int) -> None:
            meter.record(latency_s=0.01, at=1000.0 + op_i)

        _run_storm(op, threads=threads, per_thread=per_thread)
        # The count is the canonical "did any record get lost / double-applied?"
        # oracle — an unlocked `self.count += 1` would lose increments here.
        assert meter.count == threads * per_thread
        snap = meter.snapshot()
        assert snap["calls"] == threads * per_thread


class TestOptionDedupConcurrency:
    """A content key is admitted EXACTLY once under a racing add_if_new storm."""

    def test_same_key_added_exactly_once(self) -> None:
        index = OptionDedupIndex(capacity=4096, fpr_target=1e-3)
        key = option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=1)
        wins = [0]
        win_lock = threading.Lock()

        def op(_idx: int, _op: int) -> None:
            if index.add_if_new(key):
                with win_lock:
                    wins[0] += 1

        _run_storm(op, threads=16, per_thread=50)
        # Exactly ONE caller may win the add — the check-then-act is atomic.
        assert wins[0] == 1
        assert index.seen(key) is True

    def test_distinct_keys_each_added_once(self) -> None:
        index = OptionDedupIndex(capacity=8192, fpr_target=1e-3)
        n_keys = 200
        keys = [
            option_bar_key(underlying="N", expiry="E", strike="1", side="CE", epoch=e)
            for e in range(n_keys)
        ]
        added: list[int] = [0] * n_keys
        add_lock = threading.Lock()

        def op(_idx: int, op_i: int) -> None:
            k = op_i % n_keys
            if index.add_if_new(keys[k]):
                with add_lock:
                    added[k] += 1

        # Every thread tries every key; each key must be admitted exactly once.
        _run_storm(op, threads=10, per_thread=n_keys)
        assert added == [1] * n_keys


class TestFetchCoverageConcurrency:
    """Concurrent mark_complete records every distinct window once; reads atomic."""

    def test_concurrent_marks_record_every_window(self) -> None:
        cov = FetchCoverage()
        n_windows = 300

        def _key(i: int) -> WindowKey:
            return WindowKey(
                market="dhan",
                security_id=str(i),
                interval="1minute",
                start=datetime(2021, 1, 1, tzinfo=UTC).date(),
                end=datetime(2021, 4, 1, tzinfo=UTC).date(),
            )

        def op(_idx: int, op_i: int) -> None:
            cov.mark_complete(_key(op_i % n_windows))
            # Interleave a serialising read to exercise to_records under the lock.
            cov.to_records()

        _run_storm(op, threads=12, per_thread=n_windows)
        assert cov.completed_count == n_windows
        # to_records is a clean, sorted, deduplicated snapshot.
        records = cov.to_records()
        assert len(records) == n_windows
        assert records == sorted(records)


class TestCatalogConcurrency:
    """Concurrent upserts of distinct rows all land (DuckDB needs the lock)."""

    def test_concurrent_upserts_all_persist(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.duckdb"
        cat = Catalog.open(path)
        base = datetime(2024, 1, 1, tzinfo=UTC)
        n_rows = 120

        def _entry(i: int) -> CatalogEntry:
            return CatalogEntry(
                exchange="NSE",
                segment="CASH",
                symbol=f"SYM{i}",
                interval="1minute",
                year=2024,
                month=1,
                raw_hash=f"{i:064x}",
                row_count=i,
                first_ts=base,
                last_ts=base + timedelta(minutes=1),
                fetched_at=base,
            )

        try:

            def op(_idx: int, op_i: int) -> None:
                cat.upsert(_entry(op_i % n_rows))
                cat.count()  # interleave a read under the lock

            _run_storm(op, threads=8, per_thread=n_rows)
            # All distinct rows landed exactly once (DELETE+INSERT upsert PK).
            assert cat.count() == n_rows
        finally:
            cat.close()
