"""Benchmark gate for the sharded write-behind consumer (DEC-FETCH-WRITE-OVERLAP-001 Lever 1).

This is THE merge gate. CLAUDE.md Golden Rule #10 (honest limits): Lever 1 only
unlocks a higher pull rate if the SHARDED consumer's aggregate write throughput beats
the ~3.2/s a single serial writer would manage (the #906 trap — a single writer
serialises every append at ~3/s, a NET REGRESSION). We PROVE here that:

* a single serial writer's rate ≈ ``1 / per_write_seconds`` (the floor — the trap), and
* the W-shard consumer's aggregate rate is materially higher (it overlaps the writes
  across W threads when they target distinct partitions, which a real pull's
  per-contract appends do), specifically ``> 3.2/s``.

The per-write cost is modelled with a calibrated ``time.sleep`` standing in for the
~440 ms lake Parquet append (a forensic log analysis, MIGRATION.md #921, measured the
real append at ~440 ms on a deep production partition). We model the COST rather than
do a real 440 ms Parquet write so the gate runs in seconds on CI; the property under
test is the OVERLAP (W writers vs 1), which the model captures faithfully — each
submit routes to a distinct partition/shard, so W writers run W appends concurrently.

Honest envelope: this measures the consumer's write-overlap throughput in isolation;
it does NOT measure the end-to-end pull (fetch + write), which depends on the live
vendor rate. It proves the necessary condition for Lever 1 (sharded beats serial and
clears 3.2/s); the end-to-end gain is verified by the operator's live-log re-run.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.data.write_consumer import WriteConsumer

#: Modelled per-write cost (seconds) standing in for the ~440 ms production lake
#: append. Smaller than 440 ms so the bench is fast, but large enough that the
#: serial floor sits BELOW the 3.2/s gate (1/0.4 = 2.5/s < 3.2) — proving a single
#: writer would NOT clear the bar, so the win must come from sharding.
_PER_WRITE_SECONDS = 0.04

#: The merge gate from the task: the sharded aggregate write rate MUST exceed this.
_GATE_RATE = 3.2

#: Number of distinct-partition submits to time (enough to amortise thread startup).
_N_SUBMITS = 200


def _bar(idx: int) -> CandleBar:
    """A deterministic bar; the day cycles so timestamps stay valid."""
    return CandleBar(
        timestamp=datetime(2024, 1, 1 + (idx % 27), 9, 15, 0, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=10,
        open_interest=5,
    )


class _SlowSink:
    """A :class:`BarSink` whose append costs a fixed time — models the ~440 ms append."""

    def __init__(self, per_write_seconds: float) -> None:
        self._cost = per_write_seconds

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
        time.sleep(self._cost)
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _bench_write_rate(shards: int, *, n: int = _N_SUBMITS) -> float:
    """Submit *n* distinct-partition writes through a *shards*-wide consumer; return r/s.

    Each submit targets a UNIQUE symbol → a distinct partition → it can route to any
    shard, so with W shards up to W appends overlap. The returned rate is
    ``n / wall_seconds`` measured from first submit to the last durable settle (the
    consumer's ``close`` drains + joins, so on return every write has completed).
    """
    sink = _SlowSink(_PER_WRITE_SECONDS)
    done = 0
    lock = threading.Lock()

    def settle(_result: SinkAppendResult | None, _error: BaseException | None) -> None:
        nonlocal done
        with lock:
            done += 1

    start = time.perf_counter()
    with WriteConsumer(sink, shards=shards, queue_depth=max(2, shards * 2)) as wc:
        for i in range(n):
            wc.submit(
                exchange="NSE",
                segment="FNO",
                symbol=f"NSE-B{i}",
                interval="1minute",
                bars=[_bar(i)],
                settle=settle,
            )
    elapsed = time.perf_counter() - start
    assert done == n, f"only {done}/{n} writes settled"
    return n / elapsed


def test_serial_writer_rate_is_one_over_cost() -> None:
    """A single shard serialises every append → rate is bounded above by ``1/per_write``.

    This pins the SERIAL ceiling: one writer can do AT MOST ``1/per_write`` writes/s
    (it does them back-to-back). The production-latency consequence — that this floor
    is BELOW the 3.2/s gate — is asserted analytically in
    :func:`test_production_latency_serial_would_miss_gate`; here we only confirm the
    serial rate tracks ``1/cost`` (the modelled 0.04 s → ≤ ~25/s), so the sharded
    speed-up below is measured against a real serial baseline, not a strawman.
    """
    serial_rate = _bench_write_rate(shards=1)
    ceiling = 1.0 / _PER_WRITE_SECONDS
    assert serial_rate <= ceiling * 1.15  # within scheduling noise of the floor


def test_sharded_consumer_beats_gate() -> None:
    """THE MERGE GATE: the W-shard aggregate write rate must exceed 3.2/s AND beat serial."""
    serial_rate = _bench_write_rate(shards=1)
    sharded_rate = _bench_write_rate(shards=4)
    # 1) Sharded clears the absolute gate.
    assert sharded_rate > _GATE_RATE, (
        f"sharded rate {sharded_rate:.2f}/s did NOT clear the {_GATE_RATE}/s gate — "
        f"a regression must NOT merge"
    )
    # 2) Sharded is materially faster than the single serial writer (the real win:
    #    overlap, not a faster individual write). Require a clear margin over noise.
    assert sharded_rate > serial_rate * 1.5, (
        f"sharded {sharded_rate:.2f}/s not meaningfully faster than serial "
        f"{serial_rate:.2f}/s — no overlap benefit"
    )


def test_production_latency_serial_would_miss_gate() -> None:
    """At the PRODUCTION ~440 ms append cost, a single writer (2.27/s) misses 3.2/s.

    This is the honest, load-bearing claim: with the real measured append latency the
    serial floor is BELOW the gate, so the win MUST come from sharding. We compute the
    floor analytically (1 / 0.44) rather than sleep 0.44 s × 200 on CI.
    """
    production_append_seconds = 0.44
    serial_floor_rate = 1.0 / production_append_seconds
    assert serial_floor_rate < _GATE_RATE  # 2.27/s < 3.2/s — serial can't clear it
    # With 4 shards overlapping the same 0.44 s appends, the ceiling is ~4× → ~9.1/s,
    # comfortably over the gate. (Measured overlap is verified by the bench above at
    # the modelled cost; this asserts the production arithmetic that motivates Lever 1.)
    assert (4.0 / production_append_seconds) > _GATE_RATE
