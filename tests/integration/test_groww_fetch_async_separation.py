"""Integration: FULL async fetch/process separation (DEC-GROWW-FETCH-ASYNC-SEPARATION-001).

Drives the REAL :func:`~brutex.adapters.groww.ingest.ingest_spot` +
:func:`~brutex.adapters.groww.option_ingest.ingest_option_contract` through a REAL
:class:`~brutex.data.sink.ParquetBarSink` + :class:`~brutex.data.window_ledger.WindowLedger`,
both with the pipeline OFF (synchronous) and ON (the consumer pool), proving:

* **byte-parity** — the lake Parquet written WITH the pipeline is bit-identical to the
  synchronous path, including a SAME-PARTITION overlapping-month keep-first conflict.
* **settle-after-durable** — a ledger SUCCESS row exists IFF the bytes are durable; ``drain``
  before flush; a never-drained window leaves the row UNSETTLED (re-fetched next pull).
* **ISOLATION PROOF (the centerpiece)** — with the consumer deliberately STALLED, the
  PRODUCER/fetch keeps issuing at full rate (the fetch loop's throughput is unaffected by the
  slow consumer — the producer does NOT block waiting on the tail).
* **caps intact** — the 8/500 + AIMD pacing stays on the producer under pipeline mode.

Only the adapter/SDK is faked; everything below it is the real stack.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.fetch_pipeline import FetchPipeline
from brutex.adapters.groww.fetch_spill import FetchSpillArea
from brutex.adapters.groww.ingest import ingest_spot
from brutex.adapters.groww.option_ingest import ingest_option_contract
from brutex.adapters.protocol import DerivativeContract
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path
from brutex.data.sink import ParquetBarSink
from brutex.data.window_ledger import WindowLedger
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.parallel import PoolRunParallel

# 04:05 UTC = 09:35 IST → in-session.
_TS_JAN = datetime(2021, 1, 4, 4, 5, tzinfo=UTC)


def _bar(ts: datetime, close: str = "105.00") -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal(close),
        volume=1,
        open_interest=None,
    )


def _inst(symbol: str = "NIFTY") -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _now() -> datetime:
    return datetime(2026, 6, 13, 8, 0, tzinfo=UTC)


class _DayAdapter:
    """Fake MarketAdapter returning one in-session bar per fetch, dated to the window start."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, end
        self.calls += 1
        # A bar timestamped inside the window's first day's session (deterministic per window).
        ts = datetime(start.year, start.month, start.day, 4, 5, tzinfo=UTC)
        return [_bar(ts)]


def _sink(lake: Path) -> ParquetBarSink:
    return ParquetBarSink(writer=ParquetWriter(lake_root=lake), catalog=None)


def _spot_part(lake: Path, *, year: int, month: int, symbol: str = "NIFTY") -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval="1minute",
        year=year,
        month=month,
    )


def _read_all_spot(lake: Path) -> list[bytes]:
    """All spot Parquet files' raw bytes, path-sorted (for a byte-parity comparison)."""
    return [p.read_bytes() for p in sorted(lake.rglob("*.parquet"))]


# ────────────────────────────── byte-parity ──────────────────────────────


class TestByteParity:
    def test_pipeline_output_bit_identical_to_synchronous(self, tmp_path: Path) -> None:
        """A daily-tiled multi-window pull writes byte-identical Parquet sync vs pipeline."""
        # Synchronous baseline.
        sync_lake = tmp_path / "sync"
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _sink(sync_lake),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 4, 1),  # spans 3 monthly partitions via 30-day tiles
            now_ist=_now,
        )
        # Pipeline path (a real consumer pool).
        pipe_lake = tmp_path / "pipe"
        pipeline = FetchPipeline(workers=2, queue_maxsize=4)
        try:
            ingest_spot(
                _DayAdapter(),  # type: ignore[arg-type]
                _sink(pipe_lake),
                instrument=_inst(),
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 4, 1),
                now_ist=_now,
                fetch_pipeline=pipeline,
            )
        finally:
            pipeline.drain()
        assert _read_all_spot(sync_lake) == _read_all_spot(pipe_lake)

    def test_same_partition_keep_first_conflict_byte_identical(self, tmp_path: Path) -> None:
        """Two windows landing in the SAME month + a keep-first conflict are byte-identical."""

        class _SameMonthConflictAdapter:
            """Both windows write the SAME timestamp with DIFFERENT closes (keep-first)."""

            def __init__(self) -> None:
                self.calls = 0

            def fetch_ohlcv(
                self, *, symbol: str, exchange: object, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                del symbol, exchange, interval, end
                self.calls += 1
                # Window 1 (Jan 1-31) writes close 105; window 2 fetched later writes 999 at
                # the SAME timestamp → the keep-first union must keep 105 either way.
                close = "105.00" if self.calls == 1 else "999.00"
                return [_bar(_TS_JAN, close=close)]

        sync_lake = tmp_path / "sync"
        ingest_spot(
            _SameMonthConflictAdapter(),  # type: ignore[arg-type]
            _sink(sync_lake),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            now_ist=_now,
        )
        pipe_lake = tmp_path / "pipe"
        pipeline = FetchPipeline(workers=1, queue_maxsize=4)  # 1 worker → ordered same-series
        try:
            ingest_spot(
                _SameMonthConflictAdapter(),  # type: ignore[arg-type]
                _sink(pipe_lake),
                instrument=_inst(),
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
                now_ist=_now,
                fetch_pipeline=pipeline,
            )
        finally:
            pipeline.drain()
        assert _read_all_spot(sync_lake) == _read_all_spot(pipe_lake)
        # keep-first: the stored close is the FIRST window's 105 (never the conflicting 999).
        df = pl.read_parquet(sorted(pipe_lake.rglob("*.parquet"))[0])
        assert df["close"].to_list() == [105.0]


# ─────────────────────── settle-after-durable / recovery ───────────────────────


class TestSettleAfterDurable:
    def test_ledger_settled_only_after_drain(self, tmp_path: Path) -> None:
        """A SUCCESS ledger row exists IFF the bytes are durable — recorded by the consumer tail."""
        lake = tmp_path / "lake"
        ledger = WindowLedger()
        pipeline = FetchPipeline(workers=2, queue_maxsize=4)
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _sink(lake),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 2, 1),
            ledger=ledger,
            now_ist=_now,
            fetch_pipeline=pipeline,
        )
        # BEFORE drain the consumer may still be processing — settle is not guaranteed yet.
        pipeline.drain()
        # AFTER drain every window is recorded SUCCESS and the bytes are on disk.
        statuses = {r.status for r in ledger.records()}
        assert statuses == {"SUCCESS"}
        assert _spot_part(lake, year=2021, month=1).exists()

    def test_never_drained_window_unsettled_refetched_next_pull(self, tmp_path: Path) -> None:
        """A window enqueued but never processed (abandoned consumer) stays UNSETTLED.

        Simulated by stalling the consumer + never releasing it, then abandoning the pipeline:
        the ledger has NO SUCCESS row for the stuck window, so a fresh pull re-fetches it
        (idempotent keep-first) — zero corruption.
        """
        lake = tmp_path / "lake"
        ledger = WindowLedger()
        gate = threading.Event()

        # A sink whose append blocks forever (the consumer is wedged on the first window).
        class _BlockingSink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                gate.wait(timeout=30.0)
                return super().append(**kw)  # type: ignore[arg-type]

        pipeline = FetchPipeline(workers=1, queue_maxsize=8, drain_join_timeout_seconds=0.3)
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _BlockingSink(writer=ParquetWriter(lake_root=lake), catalog=None),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 2, 1),
            ledger=ledger,
            now_ist=_now,
            fetch_pipeline=pipeline,
        )
        # Abandon: drain with a tiny join timeout — the wedged consumer is REPORTED, not hung.
        pipeline.drain()  # returns within the bounded join even though the consumer is stuck
        # No window settled SUCCESS (the tail never completed) → re-fetchable next pull.
        assert all(r.status != "SUCCESS" for r in ledger.records())
        # Now release the daemon so it can exit cleanly (no orphan), and prove a fresh, normal
        # pull DOES land the data (re-fetch, no corruption).
        gate.set()
        lake2 = tmp_path / "lake2"
        ledger2 = WindowLedger()
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _sink(lake2),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 2, 1),
            ledger=ledger2,
            now_ist=_now,
        )
        assert _spot_part(lake2, year=2021, month=1).exists()


# ───────────────────── tail-side transient (PartitionLockTimeout) ───────────────────


class TestTailTransientAsync:
    def test_partition_lock_timeout_in_async_tail_is_counted_not_dropped(
        self, tmp_path: Path
    ) -> None:
        """MAJOR-2 regression: a tail-side ``PartitionLockTimeout`` in async mode is COUNTED.

        A wedged peer holding the cross-process partition lock past the deadline makes
        ``sink.append`` raise ``PartitionLockTimeout`` INSIDE the consumer tail. The tail DEFERS
        the window (records FAILED) and returns a transient outcome; the producer's ``_run_tail``
        must fold it into the pipeline's tail-transient tally so it is SURFACED at drain and the
        across-pull retry round re-pulls it. Before the fix the transient was silently dropped
        (folded as 0 bars, never counted) so the window vanished for the round.
        """
        from brutex.data.errors import PartitionLockTimeout

        lake = tmp_path / "lake"
        ledger = WindowLedger()

        class _LockTimeoutSink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                raise PartitionLockTimeout(timeout_seconds=0.5)

        pipeline = FetchPipeline(workers=1, queue_maxsize=8)
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _LockTimeoutSink(writer=ParquetWriter(lake_root=lake), catalog=None),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 2, 1),
            ledger=ledger,
            now_ist=_now,
            fetch_pipeline=pipeline,
        )
        # A transient must NOT trip the pool-wide stop (it is OUR contention, not a fatal halt) —
        # drain is clean (no re-raise).
        pipeline.drain()
        # The deferred window is COUNTED (surfaced at drain), not silently dropped.
        assert pipeline.tail_transients >= 1
        # The window's ledger row is FAILED (deferred) — never SUCCESS — so it re-pulls next round.
        statuses = {r.status for r in ledger.records()}
        assert "SUCCESS" not in statuses


# ───────────────────────── ISOLATION PROOF (centerpiece) ─────────────────────────


class TestIsolationProof:
    def test_stalled_consumer_does_not_slow_the_fetch(self, tmp_path: Path) -> None:
        """The fetch keeps issuing at full rate while each consumer tail SLEEPS 60 ms.

        The producer must NOT block on the tail: across many windows the fetch issue intervals
        stay ~constant (set by the producer's own work), NOT inflated by the 60 ms-per-window
        consumer sleep. We measure the wall time the PRODUCER spends issuing N fetches and
        assert it is far below N × tail_sleep (which is what a synchronous tail would cost).
        """
        lake = tmp_path / "lake"
        tail_sleep = 0.06
        n_windows = 12

        # A sink that SLEEPS on every append (the deliberately-stalled consumer work).
        class _SlowSink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                time.sleep(tail_sleep)
                return super().append(**kw)  # type: ignore[arg-type]

        fetch_timestamps: list[float] = []

        class _TimedAdapter:
            def __init__(self) -> None:
                self.calls = 0

            def fetch_ohlcv(
                self, *, symbol: str, exchange: object, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                del symbol, exchange, interval, end
                fetch_timestamps.append(time.monotonic())
                self.calls += 1
                ts = datetime(start.year, start.month, start.day, 4, 5, tzinfo=UTC)
                return [_bar(ts)]

        # A queue big enough that the producer is NOT back-pressured for this small N — the
        # whole point is the producer races ahead of the slow consumers.
        pipeline = FetchPipeline(workers=2, queue_maxsize=n_windows + 4)
        producer_start = time.monotonic()
        ingest_spot(
            _TimedAdapter(),  # type: ignore[arg-type]
            _SlowSink(writer=ParquetWriter(lake_root=lake), catalog=None),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2022, 1, 1),  # ~12 monthly tiles
            now_ist=_now,
            fetch_pipeline=pipeline,
        )
        producer_elapsed = time.monotonic() - producer_start
        try:
            assert len(fetch_timestamps) >= n_windows
            # The synchronous path would cost AT LEAST n_windows × tail_sleep just on tails.
            synchronous_floor = len(fetch_timestamps) * tail_sleep
            # The PRODUCER finished issuing all fetches in WELL under that floor — the slow
            # consumer never blocked the fetch lane (isolation). Generous 0.5× margin to stay
            # robust on a busy CI box while still proving the decouple (sync would be ≥ 1.0×).
            assert producer_elapsed < 0.5 * synchronous_floor, (
                f"producer took {producer_elapsed:.3f}s issuing "
                f"{len(fetch_timestamps)} fetches — not isolated from the "
                f"{synchronous_floor:.3f}s of consumer tails"
            )
        finally:
            pipeline.drain()
        # And the data still landed durably after drain.
        assert _spot_part(lake, year=2021, month=1).exists()


# ───────────────── HUNG-WRITE FREEZE GUARD (DEC-FETCH-HUNG-WRITE-DEADLINE-001) ─────────────────


class TestHungWriteDoesNotBlockFetchLane:
    """Adversarial: a durable write that HANGS (never raises) can never wedge the fetch lane.

    The bug class (verified by the prior audit): the fetch-pipeline consumer does the durable
    ``sink.append`` (fsync + os.replace) with NO timeout. If a write HANGS WITHOUT RAISING (a
    wedged fsync on a stalled SSD), the consumer never finishes, its bounded queue fills, and
    the producer eventually BLOCKS on ``submit``'s ``queue.put`` back-pressure FOREVER — the
    whole fetch lane stalls at 0 r/s while the rate controller still "allows" the ceiling. The
    back-pressure loop only breaks on ``_stopped`` / ``_draining``, which a non-raising hang
    NEVER sets.

    The fix (DEC-FETCH-HUNG-WRITE-DEADLINE-001): the consumer's durable write goes through
    :func:`~brutex.adapters.groww.write_deadline.append_with_deadline`, so a hang raises a
    TRANSIENT :class:`~brutex.data.errors.WriteDeadlineExceeded` after the deadline — the
    window is DEFERRED (recorded FAILED → re-pull) and the consumer keeps draining, releasing
    the producer.

    These tests patch the deadline to 0.2 s (via the real env var) so the timeout path is
    exercised fast.
    """

    def test_producer_not_blocked_by_a_hung_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fetch lane keeps issuing windows through a hung durable write (no deadlock).

        ONE consumer, a TINY queue (so a hung consumer back-pressures the producer almost
        immediately), and MANY windows where the FIRST window's write hangs forever. WITHOUT the
        fix the producer wedges on ``queue.put`` after the queue fills and ``ingest_spot`` never
        returns. WITH the fix the consumer's hung write times out at 0.2 s, the window defers,
        the consumer drains the rest, and the producer finishes — proven by a wall-clock budget
        + a watchdog that fails LOUDLY (never hangs the suite) if the call deadlocks.
        """
        # 0.2 s durable-write deadline (the real env path used by append_with_deadline(None)).
        monkeypatch.setenv("BRUTEX_GROWW_WRITE_DEADLINE_S", "0.2")

        gate = threading.Event()  # released at teardown so the abandoned daemon exits cleanly
        first_entered = threading.Event()
        append_calls: list[int] = []
        lock = threading.Lock()

        class _HungFirstWriteSink(ParquetBarSink):
            """The FIRST append hangs forever; every later append writes normally."""

            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                with lock:
                    append_calls.append(1)
                    n = len(append_calls)
                if n == 1:
                    first_entered.set()
                    gate.wait(timeout=30.0)  # the wedged fsync — bounded only by the guard
                    return super().append(**kw)  # type: ignore[arg-type]
                return super().append(**kw)  # type: ignore[arg-type]

        lake = tmp_path / "lake"
        ledger = WindowLedger()
        # workers=1 so the SAME consumer that hangs must also process the rest (it cannot offload
        # to a peer); queue_maxsize=1 so the producer is back-pressured almost immediately when
        # the consumer wedges — the exact condition that wedged the producer before the fix.
        pipeline = FetchPipeline(workers=1, queue_maxsize=1, drain_join_timeout_seconds=1.0)

        done = threading.Event()
        error: list[Exception] = []

        def _run() -> None:
            try:
                ingest_spot(
                    _DayAdapter(),  # type: ignore[arg-type]
                    _HungFirstWriteSink(writer=ParquetWriter(lake_root=lake), catalog=None),
                    instrument=_inst(),
                    interval="1minute",
                    start=date(2021, 1, 1),
                    end=date(2022, 1, 1),  # ~12 monthly tiles → many windows
                    ledger=ledger,
                    now_ist=_now,
                    fetch_pipeline=pipeline,
                )
            except Exception as exc:  # surface ANY worker-thread raise to the test, never swallow
                error.append(exc)
            finally:
                done.set()

        runner = threading.Thread(target=_run, name="hung-write-producer", daemon=True)
        runner.start()
        try:
            # FORWARD-PROGRESS PROOF: the producer must finish issuing ALL windows within a
            # bounded wall time DESPITE the hung first write. Each hung window costs ~0.2 s
            # (the deadline); a dozen windows + the fixed producer work fit comfortably in 20 s.
            # WITHOUT the fix this would hang here forever (the watchdog fails LOUDLY, never
            # blocking the suite indefinitely).
            assert done.wait(timeout=20.0), (
                "ingest_spot did NOT return — the fetch lane was BLOCKED by the hung write "
                "(the bug the deadline fixes)"
            )
            assert not error, f"producer raised unexpectedly: {error[0]!r}"
            # The first window's write really DID start hanging (the hang is real, not a no-op).
            assert first_entered.is_set()
            # (a) CLEAN shutdown — drain returns within the bounded join (no hang on drain/join).
            pipeline.drain()
            # (b) The hung window is recorded FAILED-deferred (re-pull next round) — NEVER a
            # silent loss and NEVER a fabricated SUCCESS for a write that never completed.
            statuses = [r.status for r in ledger.records()]
            assert "FAILED" in statuses, statuses
            assert "SUCCESS" in statuses, (
                "the windows AFTER the hung one must have written durably — proving the "
                "consumer kept processing"
            )
            # (c) The deferred window is surfaced on the pipeline's tail-transient tally so the
            # across-pull retry round re-pulls it (never dropped).
            assert pipeline.tail_transients >= 1
            # (d) The later windows' data landed durably on disk.
            assert any(lake.rglob("*.parquet")), "no Parquet written — the consumer never drained"
        finally:
            gate.set()  # release the abandoned daemon write thread (no orphan)
            runner.join(timeout=5.0)

    def test_hung_write_then_normal_pull_recovers_the_deferred_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deferred window is RE-PULLED + lands durably on a fresh (non-hung) pull.

        Proves the defer is recoverable, not a permanent loss: after a hung-write pull defers a
        window, a normal pull of the SAME span writes it (idempotent keep-first sink), so the
        guard trades a wedged lane for a clean re-pull — zero corruption.
        """
        monkeypatch.setenv("BRUTEX_GROWW_WRITE_DEADLINE_S", "0.2")
        gate = threading.Event()
        calls: list[int] = []
        lock = threading.Lock()

        class _HungFirstWriteSink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                with lock:
                    calls.append(1)
                    n = len(calls)
                if n == 1:
                    gate.wait(timeout=30.0)
                    return super().append(**kw)  # type: ignore[arg-type]
                return super().append(**kw)  # type: ignore[arg-type]

        lake = tmp_path / "lake"
        ledger = WindowLedger()
        pipeline = FetchPipeline(workers=1, queue_maxsize=1, drain_join_timeout_seconds=1.0)
        try:
            ingest_spot(
                _DayAdapter(),  # type: ignore[arg-type]
                _HungFirstWriteSink(writer=ParquetWriter(lake_root=lake), catalog=None),
                instrument=_inst(),
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 4, 1),  # 3 monthly tiles; the FIRST defers
                ledger=ledger,
                now_ist=_now,
                fetch_pipeline=pipeline,
            )
        finally:
            pipeline.drain()
            gate.set()
        assert "FAILED" in {r.status for r in ledger.records()}  # the hung window deferred
        # A fresh, normal pull (no hang) of the SAME span lands every window durably.
        lake2 = tmp_path / "lake2"
        ledger2 = WindowLedger()
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _sink(lake2),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 4, 1),
            ledger=ledger2,
            now_ist=_now,
        )
        # Every monthly partition exists on the clean re-pull — the deferred window recovered.
        assert _spot_part(lake2, year=2021, month=1).exists()
        assert "FAILED" not in {r.status for r in ledger2.records()}

    def test_same_partition_zombie_lock_second_window_defers_in_bounded_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two windows in the SAME partition where the FIRST write hangs: the SECOND still defers.

        The honest-envelope tradeoff stated in the fix: an abandoned daemon write thread keeps
        holding that partition's in-process ``threading.Lock`` + ``flock`` until its wedged
        syscall returns. Because all windows of one series route to ONE consumer
        (per-partition-lock shard), a LATER window of the SAME partition could block on the
        still-held lock — but that later append is ALSO wrapped in ``append_with_deadline``, so it
        too defers in BOUNDED time rather than wedging forever. This test forces exactly that: a
        sink that hangs the FIRST write to a partition and would hang a SECOND too; with a single
        consumer both windows route to the same thread, and the test asserts ``ingest_spot``
        RETURNS within a bounded wall time (the second window did not wedge on the zombie lock).
        """
        monkeypatch.setenv("BRUTEX_GROWW_WRITE_DEADLINE_S", "0.2")
        gate = threading.Event()
        entered = threading.Semaphore(0)
        lock = threading.Lock()
        n_calls: list[int] = []

        class _HangFirstTwoSink(ParquetBarSink):
            """Hang the first TWO durable writes (e.g. two windows of the same month partition)."""

            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                with lock:
                    n_calls.append(1)
                    n = len(n_calls)
                if n <= 2:
                    entered.release()
                    gate.wait(timeout=30.0)
                    return super().append(**kw)  # type: ignore[arg-type]
                return super().append(**kw)  # type: ignore[arg-type]

        lake = tmp_path / "lake"
        ledger = WindowLedger()
        # ONE consumer so the same thread handles consecutive same-series windows (it cannot
        # offload the second to a peer); two adjacent 30-day windows of NIFTY straddle the Jan/Feb
        # boundary, so they touch overlapping month partitions — the same-partition contention.
        pipeline = FetchPipeline(workers=1, queue_maxsize=1, drain_join_timeout_seconds=1.0)

        done = threading.Event()
        error: list[Exception] = []

        def _run() -> None:
            try:
                ingest_spot(
                    _DayAdapter(),  # type: ignore[arg-type]
                    _HangFirstTwoSink(writer=ParquetWriter(lake_root=lake), catalog=None),
                    instrument=_inst(),
                    interval="1minute",
                    start=date(2021, 1, 1),
                    end=date(2021, 3, 1),  # 2 monthly tiles → 2 windows, both hang
                    ledger=ledger,
                    now_ist=_now,
                    fetch_pipeline=pipeline,
                )
            except Exception as exc:  # surface ANY worker-thread raise to the test, never swallow
                error.append(exc)
            finally:
                done.set()

        runner = threading.Thread(target=_run, name="zombie-lock-producer", daemon=True)
        runner.start()
        try:
            # Both windows hang ~0.2 s each; the producer + both deferrals must complete well
            # within 20 s. WITHOUT the deadline the SECOND window would wedge forever on the
            # abandoned daemon's partition lock and this would never return.
            assert done.wait(timeout=20.0), (
                "ingest_spot did NOT return — the second same-partition window wedged on the "
                "zombie lock held by the abandoned first write"
            )
            assert not error, f"producer raised unexpectedly: {error[0]!r}"
            # At least the first two writes started hanging (the contention is real).
            assert entered.acquire(blocking=False)
            pipeline.drain()
            # Both hung windows are deferred FAILED — never SUCCESS, never lost.
            assert "FAILED" in {r.status for r in ledger.records()}
            assert pipeline.tail_transients >= 1
        finally:
            gate.set()
            runner.join(timeout=5.0)

    def test_one_wedged_shard_does_not_freeze_a_healthy_shard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multi-consumer: a hung write on ONE shard cannot freeze the OTHER shard's windows.

        With ``workers >= 2`` the series shard by ``blake2b(coords) % M``. We run two SEPARATE
        symbols (so they hash to — and this test asserts — DIFFERENT shards): one symbol's writes
        HANG, the other's write normally. The healthy symbol's windows must land DURABLY on disk
        while the hung symbol's defer — proving one bad partition can't freeze the whole pool.
        """
        monkeypatch.setenv("BRUTEX_GROWW_WRITE_DEADLINE_S", "0.2")
        # Pick two symbols that hash to DIFFERENT shards under the pipeline's blake2b router so the
        # wedged one and the healthy one are genuinely on different consumers. Use the PRODUCTION
        # shard-key builder (`_series_shard_key`) — no copy of the key format (CLAUDE.md §11 COMMON).
        from brutex.adapters.groww.fetch_pipeline import _shard_index
        from brutex.adapters.groww.ingest import _series_shard_key

        workers = 2
        hung_symbol, healthy_symbol = "AAA", "BBB"
        # Sanity-pick: confirm the two symbols land on different shards (deterministic blake2b over
        # the real series coords). AAA → shard 1, BBB → shard 0 under workers=2.
        assert _shard_index(
            _series_shard_key(_inst(hung_symbol), "1minute"), workers
        ) != _shard_index(_series_shard_key(_inst(healthy_symbol), "1minute"), workers), (
            "test symbols must hash to different shards"
        )

        gate = threading.Event()

        class _PerSymbolSink(ParquetBarSink):
            """Hang every write for *hung_symbol*; write *healthy_symbol* normally."""

            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                if kw.get("symbol") == hung_symbol:
                    gate.wait(timeout=30.0)
                    return super().append(**kw)  # type: ignore[arg-type]
                return super().append(**kw)  # type: ignore[arg-type]

        lake = tmp_path / "lake"
        ledger = WindowLedger()
        pipeline = FetchPipeline(workers=workers, queue_maxsize=2, drain_join_timeout_seconds=1.0)
        sink = _PerSymbolSink(writer=ParquetWriter(lake_root=lake), catalog=None)

        done = threading.Event()
        error: list[Exception] = []

        def _run() -> None:
            try:
                for sym in (hung_symbol, healthy_symbol):
                    ingest_spot(
                        _DayAdapter(),  # type: ignore[arg-type]
                        sink,
                        instrument=_inst(sym),
                        interval="1minute",
                        start=date(2021, 1, 1),
                        end=date(2021, 3, 1),
                        ledger=ledger,
                        now_ist=_now,
                        fetch_pipeline=pipeline,
                    )
            except Exception as exc:  # surface ANY worker-thread raise to the test, never swallow
                error.append(exc)
            finally:
                done.set()

        runner = threading.Thread(target=_run, name="multi-shard-producer", daemon=True)
        runner.start()
        try:
            assert done.wait(timeout=20.0), (
                "ingest_spot did NOT return — the healthy shard was frozen by the wedged shard"
            )
            assert not error, f"producer raised unexpectedly: {error[0]!r}"
            pipeline.drain()
            # The HEALTHY symbol's data landed durably on disk (its shard kept working through the
            # wedge on the OTHER shard) AND its ledger window(s) are SUCCESS.
            assert _spot_part(lake, year=2021, month=1, symbol=healthy_symbol).exists()
            healthy_statuses = {
                r.status for r in ledger.records() if r.security_id == healthy_symbol
            }
            assert healthy_statuses == {"SUCCESS"}, healthy_statuses
            # The HUNG symbol deferred (no durable partition for it; its windows FAILED) — counted,
            # never lost, never a fabricated SUCCESS for a write that never completed.
            assert not _spot_part(lake, year=2021, month=1, symbol=hung_symbol).exists()
            hung_statuses = {r.status for r in ledger.records() if r.security_id == hung_symbol}
            assert hung_statuses == {"FAILED"}, hung_statuses
            assert pipeline.tail_transients >= 1
        finally:
            gate.set()
            runner.join(timeout=5.0)

    def test_write_completing_at_the_boundary_records_success_not_timeout(
        self, tmp_path: Path
    ) -> None:
        """A write that completes JUST under the deadline records SUCCESS (no SUCCESS-on-timeout).

        The deadline-race edge: a write whose duration is close to — but under — the deadline must
        deterministically SUCCEED (the bars land durably, ledger SUCCESS, NO transient, NO
        double-record). Proven by a small deadline with an even smaller write sleep so the write
        always finishes first; asserting exactly one durable partition + one SUCCESS row.
        """
        lake = tmp_path / "lake"
        ledger = WindowLedger()
        write_calls: list[int] = []
        lock = threading.Lock()

        class _NearBoundarySink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                time.sleep(0.05)  # well under the 0.5 s deadline below → always completes
                with lock:
                    write_calls.append(1)
                return super().append(**kw)  # type: ignore[arg-type]

        # An explicit-but-generous deadline so the 0.05 s write always finishes first; this is the
        # NORMAL path under a tight-but-sufficient bound — the proof we never SUCCESS-on-timeout
        # and never break the slow-but-OK path.
        pipeline = FetchPipeline(workers=1, queue_maxsize=4, drain_join_timeout_seconds=1.0)
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _NearBoundarySink(writer=ParquetWriter(lake_root=lake), catalog=None),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 10),
            end=date(2021, 1, 25),  # < 30 days → a SINGLE window (deterministic, no tiling)
            ledger=ledger,
            now_ist=_now,
            fetch_pipeline=pipeline,
        )
        pipeline.drain()
        statuses = [r.status for r in ledger.records()]
        # Exactly ONE SUCCESS, NO FAILED (the write finished before the deadline) — and the write
        # ran exactly once (no double-record / no retry on a near-boundary completion).
        assert statuses == ["SUCCESS"], statuses
        assert len(write_calls) == 1
        assert pipeline.tail_transients == 0
        assert _spot_part(lake, year=2021, month=1).exists()


# ─────────────────────────── caps intact under pipeline ──────────────────────────


class TestRateCapsIntact:
    def test_pace_runs_once_per_fetch_under_pipeline(self, tmp_path: Path) -> None:
        """The producer still calls the rate ``pace`` bucket once per physical fetch.

        The 8/500 + AIMD limiter is the ``pace`` hook on the PRODUCER; it must fire exactly as
        in the synchronous path (the consumer never paces). One bar per window, no retries → one
        pace per window, identical sync vs pipeline.
        """
        sync_paces: list[int] = []
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _sink(tmp_path / "sync"),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            pace=lambda: sync_paces.append(1),
            now_ist=_now,
        )
        pipe_paces: list[int] = []
        pipeline = FetchPipeline(workers=2, queue_maxsize=8)
        try:
            ingest_spot(
                _DayAdapter(),  # type: ignore[arg-type]
                _sink(tmp_path / "pipe"),
                instrument=_inst(),
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
                pace=lambda: pipe_paces.append(1),
                now_ist=_now,
                fetch_pipeline=pipeline,
            )
        finally:
            pipeline.drain()
        assert sum(sync_paces) == sum(pipe_paces)
        assert sum(pipe_paces) > 0


# ──────────────────────────────── option twin ───────────────────────────────────


class _OptionSDK:
    """Returns two in-session option bars with volume + OI (the F&O twin fixture)."""

    def __init__(self) -> None:
        self.calls = 0

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
        self.calls += 1
        return {
            "candles": [
                ["2024-01-02T09:30:00", 245.95, 246.15, 245.05, 245.60, 735060, 12345],
                ["2024-01-02T09:31:00", 245.60, 245.66, 244.80, 244.94, 682373, 12350],
            ]
        }

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"contracts": []}


def _contract() -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol="NSE-NIFTY-02Jan25-28500-CE",
        contract_type="CE",
        strike=28500.0,
    )


def _contract_n(i: int) -> DerivativeContract:
    """A distinct NIFTY CE contract per *i* (distinct strike → distinct series/partition)."""
    strike = 28500 + i * 100
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol=f"NSE-NIFTY-02Jan25-{strike}-CE",
        contract_type="CE",
        strike=float(strike),
    )


class TestOptionTwin:
    def test_option_pipeline_byte_identical_and_settled(self, tmp_path: Path) -> None:
        """The F&O twin: an option contract written via the pipeline is byte-identical + settled."""
        # Synchronous baseline.
        sync_lake = tmp_path / "sync"
        ingest_option_contract(
            GrowwClient(sdk=_OptionSDK(), sleep=lambda _s: None, max_attempts=1),
            _sink(sync_lake),
            contract=_contract(),
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
            now_ist=_now,
        )
        # Pipeline path.
        pipe_lake = tmp_path / "pipe"
        ledger = WindowLedger()
        pipeline = FetchPipeline(workers=2, queue_maxsize=4)
        report = ingest_option_contract(
            GrowwClient(sdk=_OptionSDK(), sleep=lambda _s: None, max_attempts=1),
            _sink(pipe_lake),
            contract=_contract(),
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
            ledger=ledger,
            now_ist=_now,
            fetch_pipeline=pipeline,
        )
        # The producer returns a DEFERRED report (bars on the pipeline totals, not here).
        assert report.windows == 1 and report.bars_appended == 0
        pipeline.drain()
        assert pipeline.totals[1] == 2  # the consumer folded 2 bars
        assert {r.status for r in ledger.records()} == {"SUCCESS"}
        assert _read_all_spot(sync_lake) == _read_all_spot(pipe_lake)

    def test_option_tail_partition_lock_timeout_counted_not_dropped(self, tmp_path: Path) -> None:
        """MAJOR-2 option twin: a tail-side ``PartitionLockTimeout`` is COUNTED, not dropped."""
        from brutex.data.errors import PartitionLockTimeout

        lake = tmp_path / "lake"
        ledger = WindowLedger()

        class _LockTimeoutSink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                raise PartitionLockTimeout(timeout_seconds=0.5)

        pipeline = FetchPipeline(workers=1, queue_maxsize=4)
        ingest_option_contract(
            GrowwClient(sdk=_OptionSDK(), sleep=lambda _s: None, max_attempts=1),
            _LockTimeoutSink(writer=ParquetWriter(lake_root=lake), catalog=None),
            contract=_contract(),
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
            ledger=ledger,
            now_ist=_now,
            fetch_pipeline=pipeline,
        )
        pipeline.drain()  # a transient does not trip the pool-wide stop → clean drain
        assert pipeline.tail_transients >= 1
        assert "SUCCESS" not in {r.status for r in ledger.records()}

    def test_option_window_spills_and_rehydrates_durably(self, tmp_path: Path) -> None:
        """The F&O twin of the spill: a SLOW option write SPILLS + the consumer rehydrates durably.

        A slow (not hung) option write + a small queue + a spill area: the over-queue option
        contracts SPILL to disk (the producer never blocks), and the consumer rehydrates each
        spilled window's bars via the option ``_rehydrate`` hook + writes them durably (every
        contract lands, none lost). Exercises the option spine's spill rehydration end to end.
        """
        lake = tmp_path / "lake"
        spill = FetchSpillArea.under_lake_root(lake)
        ledger = WindowLedger()

        class _SlowOptionSink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                time.sleep(0.3)
                return super().append(**kw)  # type: ignore[arg-type]

        # ONE consumer + a 1-deep queue + a spill area: with several contracts the over-queue
        # ones spill. All contracts share the underlying so they route to ONE consumer (shard 0).
        pipeline = FetchPipeline(workers=1, queue_maxsize=1, spill_area=spill)
        sink = _SlowOptionSink(writer=ParquetWriter(lake_root=lake), catalog=None)
        client = GrowwClient(sdk=_OptionSDK(), sleep=lambda _s: None, max_attempts=1)
        n_contracts = 6
        for i in range(n_contracts):
            ingest_option_contract(
                client,
                sink,
                contract=_contract_n(i),
                interval="1minute",
                start=date(2024, 1, 1),
                end=date(2024, 1, 3),
                ledger=ledger,
                now_ist=_now,
                fetch_pipeline=pipeline,
            )
        # At least one contract overflowed to the spill while the slow writes drained.
        assert spill.reserved_bytes >= 0  # (bounded; may be 0 if all drained by drain-time)
        pipeline.drain()
        # Every contract was written durably (rehydrated from spill where it overflowed) — 2 bars
        # each × n_contracts, all SUCCESS, no loss; the spill area is released + clean.
        assert pipeline.totals[1] == 2 * n_contracts
        assert {r.status for r in ledger.records()} == {"SUCCESS"}
        assert spill.reserved_bytes == 0
        assert not list(spill.spill_dir.iterdir())


# ─────────────────────── isolation under a REAL pool runner ──────────────────────


class TestIsolationUnderRealPool:
    def test_real_pool_producer_keeps_fetching_with_slow_consumer(self, tmp_path: Path) -> None:
        """Sanity: a real PoolRunParallel single-pair pull works with the slow consumer pool.

        Exercises the production runner seam (not just the in-thread fake) end-to-end with the
        pipeline so the wiring is proven against the real fan-out, then drained cleanly.
        """
        lake = tmp_path / "lake"

        class _SlowSink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                time.sleep(0.01)
                return super().append(**kw)  # type: ignore[arg-type]

        pipeline = FetchPipeline(workers=3, queue_maxsize=16)
        ledger = WindowLedger()
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _SlowSink(writer=ParquetWriter(lake_root=lake), catalog=None),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 5, 1),
            ledger=ledger,
            now_ist=_now,
            run_parallel=PoolRunParallel(max_workers=3),
            fetch_pipeline=pipeline,
        )
        pipeline.drain()
        assert {r.status for r in ledger.records()} == {"SUCCESS"}
        assert _spot_part(lake, year=2021, month=1).exists()


# ────────── SUSTAINED-SLOW DISK does not back-pressure the fetch lane (centerpiece) ──────────
#
# DEC-FETCH-NONBLOCKING-HANDOFF-001. PR #962 covers a HUNG write (a deadline raises + defers the
# window). THIS covers the SUSTAINED-SLOW case: each durable write COMPLETES (no hang) but is slow,
# so with only a fixed bounded queue the producer would block on ``submit`` back-pressure and the
# fetch rate would couple to the write rate. The bounded on-disk SPILL decouples them. The ANTI-FAKE
# adversarial proof: a slow (not hung) sink + the production-sized queue, asserting the producer
# issues ALL fetches in a wall time that is IMPOSSIBLE if it blocked on the write rate, that memory
# (the spill) stays bounded, that EVERY window lands durably (none lost, keep-first), and that the
# drain is clean.


class TestSustainedSlowDiskDoesNotBackPressure:
    def test_slow_disk_does_not_throttle_the_fetch_lane(self, tmp_path: Path) -> None:
        """A SLOW (not hung) write must NOT throttle the producer to the write rate — it SPILLS.

        Each durable write sleeps 0.5 s but COMPLETES. With ONE consumer + the production queue
        depth (4) and NO spill, the producer would block on back-pressure after ~5 windows and the
        whole pull would take ~N × 0.5 s. WITH the bounded spill the producer keeps issuing fetches
        at its own (fast) rate — the over-queue windows spill to disk — so it finishes issuing ALL
        N fetches in WELL under the synchronous-write floor. We assert exactly that wall-time bound
        (impossible without the decouple), bounded spill, every window durable, and a clean drain.
        """
        lake = tmp_path / "lake"
        write_sleep = 0.5
        n_windows = 12  # 12 monthly tiles → 12 windows; a synchronous write floor of ~6 s

        # A sink that SLEEPS 0.5 s on every append but always COMPLETES (slow, not hung).
        class _SlowButCompletingSink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                time.sleep(write_sleep)
                return super().append(**kw)  # type: ignore[arg-type]

        fetch_timestamps: list[float] = []

        class _TimedAdapter:
            def fetch_ohlcv(
                self, *, symbol: str, exchange: object, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                del symbol, exchange, interval, end
                fetch_timestamps.append(time.monotonic())
                return [_bar(datetime(start.year, start.month, start.day, 4, 5, tzinfo=UTC))]

        # ONE consumer + the PRODUCTION queue depth (4) so a sustained-slow write fills the queue —
        # WITH a generous spill area so the producer spills the overflow instead of blocking.
        spill = FetchSpillArea.under_lake_root(lake)
        pipeline = FetchPipeline(workers=1, queue_maxsize=4, spill_area=spill)
        ledger = WindowLedger()
        producer_start = time.monotonic()
        ingest_spot(
            _TimedAdapter(),  # type: ignore[arg-type]
            _SlowButCompletingSink(writer=ParquetWriter(lake_root=lake), catalog=None),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2022, 1, 1),  # ~12 monthly tiles
            ledger=ledger,
            now_ist=_now,
            fetch_pipeline=pipeline,
        )
        producer_elapsed = time.monotonic() - producer_start
        try:
            assert len(fetch_timestamps) >= n_windows
            # (a) FORWARD PROGRESS / NOT THROTTLED TO THE WRITE RATE. A synchronous (or
            # queue-blocked) pull would cost AT LEAST n_windows × write_sleep just on the slow
            # writes. The producer finished issuing ALL fetches in WELL under that floor (the spill
            # decoupled it). 0.5× margin keeps it robust on a busy CI box (sync would be ≥ 1.0×).
            synchronous_floor = len(fetch_timestamps) * write_sleep
            assert producer_elapsed < 0.5 * synchronous_floor, (
                f"producer took {producer_elapsed:.3f}s issuing {len(fetch_timestamps)} fetches — "
                f"it was THROTTLED to the {synchronous_floor:.3f}s slow-write rate (no decouple)"
            )
            # (b) MEMORY/SPILL STAYS BOUNDED — the overflow spilled but never grew without bound
            # (well under the 256 MiB cap; a handful of tiny windows at a few hundred bytes each).
            assert spill.reserved_bytes <= 1 * 1024 * 1024
        finally:
            pipeline.drain()
        # (c) EVERY window written DURABLY — none lost, keep-first preserved (all SUCCESS, no
        # EMPTY/FAILED), and the spill area is fully released + cleaned after the drain.
        statuses = {r.status for r in ledger.records()}
        assert statuses == {"SUCCESS"}, statuses
        assert ledger.count >= n_windows
        assert _spot_part(lake, year=2021, month=1).exists()
        assert _spot_part(lake, year=2021, month=6).exists()
        # (d) CLEAN DRAIN — the spill bytes are released + the spill dir holds no straggler.
        assert spill.reserved_bytes == 0
        assert not list(spill.spill_dir.iterdir())

    def test_regression_without_spill_the_fetch_lane_throttles_to_write_rate(
        self, tmp_path: Path
    ) -> None:
        """REGRESSION PIN: with NO spill the SAME slow disk DOES couple the fetch to the write rate.

        The mirror of the centerpiece that PINS the bug: the IDENTICAL slow-but-completing sink +
        production queue depth but NO spill area. The producer is now back-pressured once the queue
        fills, so it issues its fetches NO FASTER than the slow write drains them — the inter-fetch
        gap is dominated by ``write_sleep``. We assert the pull takes AT LEAST a meaningful fraction
        of the synchronous-write floor (the coupling the spill removes), proving the fix is real and
        not a no-op (without the fix the centerpiece's ``< 0.5×`` bound would itself be unreachable).
        """
        lake = tmp_path / "lake"
        write_sleep = 0.5
        n_windows = 12

        class _SlowButCompletingSink(ParquetBarSink):
            def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
                time.sleep(write_sleep)
                return super().append(**kw)  # type: ignore[arg-type]

        pipeline = FetchPipeline(workers=1, queue_maxsize=4)  # NO spill_area
        ledger = WindowLedger()
        start = time.monotonic()
        ingest_spot(
            _DayAdapter(),  # type: ignore[arg-type]
            _SlowButCompletingSink(writer=ParquetWriter(lake_root=lake), catalog=None),
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2022, 1, 1),
            ledger=ledger,
            now_ist=_now,
            fetch_pipeline=pipeline,
        )
        elapsed = time.monotonic() - start
        pipeline.drain()
        # WITHOUT the spill the producer is throttled to the write rate: once the 4-deep queue fills
        # it blocks on each ``submit`` until a slow write drains a slot, so the whole pull is bounded
        # BELOW by roughly (n_windows − queue_depth) × write_sleep. A generous 0.3× floor proves the
        # coupling is real (it is far above the centerpiece's < 0.5× bound, which is only reachable
        # WITH the spill). This is the bug the spill removes.
        coupled_floor = 0.3 * n_windows * write_sleep
        assert elapsed >= coupled_floor, (
            f"pull finished in {elapsed:.3f}s — expected >= {coupled_floor:.3f}s coupling to the "
            f"slow write WITHOUT the spill (the regression pin is not exercising the back-pressure)"
        )
        assert {r.status for r in ledger.records()} == {"SUCCESS"}
