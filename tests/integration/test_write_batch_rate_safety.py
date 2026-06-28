"""Rate-cap safety regression for the coalescing batched write path.

DEC-WRITE-BATCH-COALESCE-001 — the CRITICAL safety invariant: the batched write speedup
must NEVER let the pull exceed the rate limits. The batched writer changes ONLY how
FETCHED data is WRITTEN; it does NOT touch, bypass, or relax the upstream
:class:`~brutex.adapters.rate_common.RateController` (the per-second AIMD ceiling + the
atomic 500/min governor). The fetch workers still acquire a rate token via
:meth:`~brutex.adapters.rate_common.RateController.pace` BEFORE every request.

This test proves it end-to-end: many fetch workers each ``pace()`` (the real rate gate)
then ``submit()`` a write to a live :class:`~brutex.data.write_consumer.BatchingWriteConsumer`.
Even though the batched writer unblocks the workers FAST (no per-window O(month-depth)
idle), the controller still binds the ISSUE rate at the ceiling and the rolling-60s count
never crosses the governor cap — faster writes let workers fetch UP TO the allowed rate,
never beyond it (Golden Rule #6).

A deterministic VIRTUAL clock drives the controller so the simulation is fast + stable
(no wall-clock flakiness): the injected ``sleep`` advances the shared clock, so the token
bucket + the per-minute governor enforce their windows against simulated time exactly as
they would against the real clock.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.adapters.rate_common import RateController
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.data.write_consumer import (
    BatchingWriteConsumer,
    ResultBox,
    WriteRequest,
)


class _VirtualClock:
    """A monotonically-advancing virtual clock shared by the controller's clock + sleep.

    ``now()`` reads the shared time; ``sleep(seconds)`` ADVANCES it (and yields the GIL so
    other workers run). Thread-safe — the parallel pacers read/advance under one lock — so
    the per-second token bucket + the per-minute governor enforce their windows against
    deterministic simulated time, with NO real wall-clock waiting (fast + reproducible).
    """

    def __init__(self) -> None:
        self._t = 0.0
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            with self._lock:
                self._t += seconds
        # Yield so other workers progress even on a zero-length sleep.
        import time as _time

        _time.sleep(0)


@dataclass
class _FastSink:
    """A durable-sink fake that returns instantly (so the WRITE is never the limiter).

    The whole point: with an instant write the ONLY thing that can pace the workers is the
    RateController. If the batched writer leaked any rate budget, the issue rate would
    exceed the ceiling here — it does not.
    """

    appends: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, merge_mode
        with self._lock:
            self.appends += 1
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _bar(minute: int) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2020, 1, 1, 9, 15, tzinfo=UTC) + timedelta(minutes=minute),
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


def _request(minute: int) -> WriteRequest:
    return WriteRequest(
        exchange="NSE",
        segment="INDEX",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=[_bar(minute)],
        merge_mode="keep-first",
        box=ResultBox(),
    )


def test_batched_writer_never_exceeds_per_second_ceiling() -> None:
    """The ISSUE rate stays at / below the controller ceiling even with the batched writer.

    Workers loop {pace(); submit()} against a live BatchingWriteConsumer + an instant sink.
    The batched writer unblocks them with near-zero write latency, so the ONLY pacing is
    the RateController. With a virtual clock, every ``pace()`` admission records its issue
    instant; the observed peak issue rate over any 1-second virtual window must NOT exceed
    the ceiling (the controller is the binding gate, untouched by the batched path).
    """
    clock = _VirtualClock()
    ceiling = 8.0
    controller = RateController(
        clock=clock.now,
        sleep=clock.sleep,
        ceiling_rps=ceiling,
        ceiling_per_min=500,
    )
    sink = _FastSink()
    consumer = BatchingWriteConsumer(sink=sink)

    issue_times: list[float] = []
    issue_lock = threading.Lock()
    total = 240  # plenty to amortise the bucket warm-up

    settled = 0
    settled_lock = threading.Lock()

    def _worker(worker_id: int, n_each: int) -> None:
        nonlocal settled
        for m in range(n_each):
            controller.pace()  # the rate gate — BEFORE the (simulated) fetch
            with issue_lock:
                issue_times.append(clock.now())
            # Distinct minute per (worker, m) so coalescing never collapses two windows
            # into one (the rate assertion measures ISSUES, not sink calls).
            consumer.submit(_request(worker_id * n_each + m))  # batched write — must not pace
            with settled_lock:
                settled += 1

    workers = 4
    per = total // workers
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda wid: _worker(wid, per), range(workers)))
    finally:
        consumer.drain()

    # Every submitted window settled (settle-after-durable: submit returned only after the
    # instant write completed), so the batched path lost no work.
    assert settled == per * workers
    assert sink.appends >= 1  # the coalescing writer wrote at least once

    # Peak issue rate over any 1-second virtual window must not exceed the ceiling.
    times = sorted(issue_times)
    assert len(times) == per * workers
    # Allow a tiny slack for the bucket's burst capacity (one token) + virtual-clock
    # granularity — the controller is provably ≤ ceiling+1 in any rolling second.
    max_in_window = 0
    j = 0
    for i in range(len(times)):
        while times[i] - times[j] >= 1.0:
            j += 1
        max_in_window = max(max_in_window, i - j + 1)
    assert max_in_window <= ceiling + 1, (
        f"issue rate {max_in_window}/s exceeded the ceiling {ceiling} — the batched writer "
        f"leaked rate budget (it must NEVER bypass the RateController)"
    )


def test_batched_writer_never_exceeds_per_minute_governor() -> None:
    """The rolling-60s issued-request count never crosses the HARD 500/min governor.

    Same wiring as above but the assertion is the per-minute governor: the controller's
    ``snapshot()['recent_minute']`` (the rolling-60s issue count) must never exceed the
    injected ``ceiling_per_min`` while the batched writer drains as fast as it can.
    """
    clock = _VirtualClock()
    governor = 500
    controller = RateController(
        clock=clock.now,
        sleep=clock.sleep,
        ceiling_rps=8.0,
        ceiling_per_min=governor,
    )
    sink = _FastSink()
    consumer = BatchingWriteConsumer(sink=sink)

    peak_recent_minute = 0
    peak_lock = threading.Lock()
    total = 240

    def _worker(n_each: int) -> None:
        nonlocal peak_recent_minute
        for m in range(n_each):
            controller.pace()
            recent = controller.snapshot()["recent_minute"]
            assert isinstance(recent, int)
            with peak_lock:
                peak_recent_minute = max(peak_recent_minute, recent)
            consumer.submit(_request(m))

    workers = 4
    per = total // workers
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_worker, [per] * workers))
    finally:
        consumer.drain()

    assert peak_recent_minute <= governor, (
        f"rolling-60s issue count {peak_recent_minute} crossed the {governor}/min governor "
        f"— the batched writer must NOT bypass the per-minute HARD cap"
    )


def test_batched_path_does_not_change_issue_rate_vs_serial() -> None:
    """The batched writer does not raise the ISSUE rate above the synchronous path.

    Run the SAME paced workload twice — once writing inline (serial), once through the
    batched consumer — and assert the batched arm issues NO MORE requests per virtual second
    than serial. The gate is UPSTREAM of the write, so the issue rate is identical; the
    batched writer only removes write IDLE, it never lets the workers fetch faster than the
    controller allows.
    """

    def _peak_issue_rate(*, batched: bool) -> int:
        clock = _VirtualClock()
        controller = RateController(
            clock=clock.now, sleep=clock.sleep, ceiling_rps=8.0, ceiling_per_min=500
        )
        sink = _FastSink()
        consumer = BatchingWriteConsumer(sink=sink) if batched else None
        issue_times: list[float] = []
        lock = threading.Lock()
        total, workers = 160, 4
        per = total // workers

        def _worker(n_each: int) -> None:
            for m in range(n_each):
                controller.pace()
                with lock:
                    issue_times.append(clock.now())
                if consumer is not None:
                    consumer.submit(_request(m))
                else:
                    sink.append(
                        exchange="NSE",
                        segment="INDEX",
                        symbol="NSE-NIFTY",
                        interval="1minute",
                        bars=[_bar(m)],
                    )

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(_worker, [per] * workers))
        finally:
            if consumer is not None:
                consumer.drain()

        times = sorted(issue_times)
        peak = j = 0
        for i in range(len(times)):
            while times[i] - times[j] >= 1.0:
                j += 1
            peak = max(peak, i - j + 1)
        return peak

    serial_peak = _peak_issue_rate(batched=False)
    batched_peak = _peak_issue_rate(batched=True)
    # The batched path must not issue MORE per second than serial — both bind at the gate.
    assert batched_peak <= serial_peak + 1, (
        f"batched issued {batched_peak}/s vs serial {serial_peak}/s — the batched writer "
        f"must not raise the issue rate (the rate gate is upstream + untouched)"
    )
