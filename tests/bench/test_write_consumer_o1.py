"""Benchmark gate for the write-behind consumer (DEC-FETCH-WRITE-OVERLAP-001 Lever 1).

THE HONEST OUTCOME (Golden Rule #10): a NO-GO.

This benchmark measures the ACTUAL end-to-end mechanism Lever 1 claims to improve —
the aggregate FETCH throughput (windows pulled per second) of the F&O pull — using the
REAL :class:`~brutex.data.sink.ParquetBarSink` / :class:`~brutex.data.parquet_writer.ParquetWriter`
over pre-seeded DEEP month partitions, with the network FETCH modelled as a
GIL-RELEASING ``time.sleep`` (legitimate — real network I/O releases the GIL while a
writer thread runs). It compares:

* (a) SYNCHRONOUS: N fetch workers each do ``fetch -> real-write`` serially, and
* (b) WRITE-BEHIND: N fetch workers do ONLY fetches (enqueue), a writer pool (W=1/2/4)
  drains the REAL writes off the fetch critical path.

The measured truth (this machine, deep ~9k-bar partition, 128 ms fetch model):

    SYNCHRONOUS         : ~5.7 fetch r/s
    WRITE-BEHIND W=1    : ~7.2 fetch r/s  (~1.26x)
    WRITE-BEHIND W=2    : ~6.6 fetch r/s  (~1.16x)
    WRITE-BEHIND W=4    : ~6.8 fetch r/s  (~1.19x)

and at a heavier (production-representative ~370 ms) append the gain collapses to
~1.05–1.09x with W=4 ≤ W=2 ≤ W=1.

The PURE-WRITE sharding measurement (no fetch overlap, real writer) is a NET
REGRESSION: W=4 ≈ 0.94x of W=1 — write-vs-write sharding does NOT parallelize, because
the real append is GIL-BOUND pure-Python work (``ParquetWriter._read_existing_bars``
row-decode + the keep-first ``_merge_write``), NOT a GIL-releasing ``time.sleep``. The
shards only add lock + context-switch overhead.

Conclusion (recorded in MIGRATION.md #923, corroborating the #922 ledger note that a
write-rate benchmark already REFUTED Lever 1): the SHARDING that was the feature's whole
novelty buys NOTHING under the GIL; the only real effect is a small fetch<->write
OVERLAP captured at W=1, and only while the write is not the per-contract bottleneck.
The consumer therefore stays DEFAULT-OFF (``BRUTEX_WRITE_CONSUMER`` unset) and is NOT a
merge-justifying win. This bench ASSERTS that measured truth so a future change cannot
silently re-introduce the false "sharded W>1 beats W=1" claim.

The prior version of this file modelled the lake write itself as ``time.sleep(40ms)``
(GIL-releasing), which faked perfect write-thread parallelism and produced the bogus
"W=4 = 88.8 r/s = 3.57x serial" headline. That model was the BLOCKER the hostile review
caught. This file uses the REAL writer for the write and only models the (genuinely
GIL-releasing) network fetch.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# The REAL-writer bench needs the [data] extra (polars) — the whole point is to measure
# the GIL-bound ParquetWriter append, NOT a sleep stub. The CI "Benchmarks" job installs
# `.[dev,data]` so these RUN there (and assert the NO-GO); a lean env without polars skips
# honestly rather than hard-erroring (mirrors `pytest.importorskip("duckdb")` elsewhere).
pytest.importorskip("polars", reason="real-writer write-behind bench needs the [data] extra")

from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink, SinkAppendResult
from brutex.data.write_consumer import WriteConsumer

#: Modelled per-FETCH cost (seconds) — the ~128 ms measured median network fetch. This
#: is the ONLY ``time.sleep`` in the bench, and it stands in for real network I/O, which
#: genuinely releases the GIL — so a writer thread can run during it (the overlap under
#: test). The WRITE is the REAL ``ParquetBarSink`` append (GIL-bound), never a sleep.
_FETCH_SECONDS = 0.128

#: Number of fetch workers (the production SDK connection-pool size, DEC-FETCH-WRITE-OVERLAP-001
#: Lever 2 raised ``GROWW_CLIENT_POOL_SIZE`` 2 -> 4).
_N_FETCH_WORKERS = 4

#: Distinct contracts (distinct lake partitions) timed per run. Small enough that the
#: bench finishes in a few seconds on CI (each does one GIL-releasing fetch + one real
#: append), large enough to amortise thread startup.
_N_CONTRACTS = 24

#: Depth of each pre-seeded month partition. A real F&O 1-minute month is thousands of
#: bars; this makes each append pay a realistic merge-read + re-render cost so the
#: GIL-bound nature of the write is exercised (a 1-bar partition would hide it).
_SEED_BARS = 6000

#: Bars appended per fetch (one trading day's 1-minute bars).
_APPEND_BARS = 200


def _seed_bar(idx: int) -> CandleBar:
    """A deterministic bar inside June 2024 (one deep partition per symbol)."""
    return CandleBar(
        timestamp=datetime(2024, 6, 1 + (idx // 375) % 27, 3, 45, 0, tzinfo=UTC)
        + timedelta(minutes=idx % 375),
        open=Decimal("100.05"),
        high=Decimal("110.15"),
        low=Decimal("90.25"),
        close=Decimal("105.35"),
        volume=10 + idx,
        open_interest=5 + idx,
    )


def _append_bars(offset: int) -> list[CandleBar]:
    """New (unique-timestamp) bars appended on each fetch → a real merge-read each time."""
    return [
        CandleBar(
            timestamp=datetime(2024, 6, 28, 3, 45, 0, tzinfo=UTC)
            + timedelta(minutes=offset * _APPEND_BARS + i),
            open=Decimal("101.05"),
            high=Decimal("111.15"),
            low=Decimal("91.25"),
            close=Decimal("106.35"),
            volume=20 + i,
            open_interest=10 + i,
        )
        for i in range(_APPEND_BARS)
    ]


def _make_sink(root: Path) -> ParquetBarSink:
    return ParquetBarSink(writer=ParquetWriter(lake_root=root))


def _seed(root: Path, symbols: Sequence[str]) -> None:
    """Pre-seed one deep month partition per symbol so each later append is realistic."""
    sink = _make_sink(root)
    bars = [_seed_bar(i) for i in range(_SEED_BARS)]
    for sym in symbols:
        sink.append(exchange="NSE", segment="FNO", symbol=sym, interval="1minute", bars=bars)


def _symbols() -> list[str]:
    return [f"NSE-BENCH{i:04d}" for i in range(_N_CONTRACTS)]


def _run_synchronous(root: Path, symbols: Sequence[str]) -> float:
    """N workers each fetch (GIL-released sleep) then REAL-write serially → aggregate r/s."""
    done = 0
    lock = threading.Lock()

    def worker(batch: Sequence[str]) -> None:
        nonlocal done
        sink = _make_sink(root)
        for k, sym in enumerate(batch):
            time.sleep(_FETCH_SECONDS)
            sink.append(
                exchange="NSE",
                segment="FNO",
                symbol=sym,
                interval="1minute",
                bars=_append_bars(k % 5 + 1),
            )
            with lock:
                done += 1

    batches = [list(symbols[i::_N_FETCH_WORKERS]) for i in range(_N_FETCH_WORKERS)]
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=_N_FETCH_WORKERS) as ex:
        list(ex.map(worker, batches))
    elapsed = time.perf_counter() - start
    assert done == len(symbols)
    return len(symbols) / elapsed


def _run_write_behind(root: Path, symbols: Sequence[str], *, shards: int) -> float:
    """N fetch workers fetch+enqueue; W shards drain the REAL writes → aggregate fetch r/s."""
    sink = _make_sink(root)
    fetched = 0
    lock = threading.Lock()

    def settle(_r: SinkAppendResult | None, _e: BaseException | None) -> None:
        pass

    batches = [list(symbols[i::_N_FETCH_WORKERS]) for i in range(_N_FETCH_WORKERS)]
    start = time.perf_counter()
    with WriteConsumer(sink, shards=shards, queue_depth=max(8, shards * 4)) as wc:

        def worker(batch: Sequence[str]) -> None:
            nonlocal fetched
            for k, sym in enumerate(batch):
                time.sleep(_FETCH_SECONDS)
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol=sym,
                    interval="1minute",
                    bars=_append_bars(k % 5 + 1),
                    settle=settle,
                )
                with lock:
                    fetched += 1

        with ThreadPoolExecutor(max_workers=_N_FETCH_WORKERS) as ex:
            list(ex.map(worker, batches))
        # WriteConsumer.__exit__ drains + joins → every write is durable on return.
    elapsed = time.perf_counter() - start
    assert fetched == len(symbols)
    return len(symbols) / elapsed


@pytest.mark.bench
def test_pure_write_sharding_does_not_parallelize_under_the_gil(tmp_path: Path) -> None:
    """W>1 shards do NOT speed up pure (no-fetch) REAL writes — they are a NET REGRESSION.

    This is the load-bearing NO-GO measurement: the real ``ParquetWriter`` append is
    GIL-bound pure-Python work, so feeding distinct-partition writes through a W-shard
    consumer with NO fetch overlap gives W=4 <= W=1 (the shards add lock + context-switch
    overhead, not parallelism). Asserting W=4 does NOT materially beat W=1 pins the truth
    that the SHARDING premise of Lever 1 is false under the GIL — a future change cannot
    silently re-introduce the "sharded W>1 is faster" claim. (Tolerance is generous to
    absorb CI scheduler noise; the point is the ABSENCE of a real speed-up, not an exact
    ratio.)
    """
    symbols = _symbols()

    def pure_write_rate(root: Path, *, shards: int) -> float:
        _seed(root, symbols)
        sink = _make_sink(root)
        done = 0
        lock = threading.Lock()

        def settle(_r: SinkAppendResult | None, _e: BaseException | None) -> None:
            nonlocal done
            with lock:
                done += 1

        start = time.perf_counter()
        with WriteConsumer(sink, shards=shards, queue_depth=max(8, shards * 4)) as wc:
            for k, sym in enumerate(symbols):
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol=sym,
                    interval="1minute",
                    bars=_append_bars(k % 5 + 1),
                    settle=settle,
                )
        elapsed = time.perf_counter() - start
        assert done == len(symbols)
        return len(symbols) / elapsed

    w1 = pure_write_rate(tmp_path / "pw1", shards=1)
    w4 = pure_write_rate(tmp_path / "pw4", shards=4)
    # NO real parallelism: 4 shards must NOT clear even a 1.3x bar over 1 shard. (On the
    # bench machine W=4 ≈ 0.94x of W=1 — an outright regression; 1.3x is a wide noise band
    # that still fails loudly if someone fakes a speed-up with a GIL-releasing stub.)
    assert w4 < w1 * 1.3, (
        f"pure-write W=4 ({w4:.2f}/s) unexpectedly beat W=1 ({w1:.2f}/s) by >1.3x — the "
        f"real GIL-bound writer cannot parallelize; a faked sleep-based writer can. "
        f"Investigate before claiming sharding helps."
    )


@pytest.mark.bench
def test_end_to_end_fetch_throughput_sync_vs_write_behind(tmp_path: Path) -> None:
    """End-to-end: write-behind raises fetch r/s only marginally, and shards don't help.

    Measures the REAL goal — aggregate fetch throughput — for synchronous fetch+write vs
    write-behind at W=1/2/4. The honest finding (NO-GO): write-behind gives a small
    fetch<->write overlap win (the single background writer lets a fetch worker start the
    next fetch while the previous write runs), but it is modest and W>1 does NOT improve on
    W=1 (the GIL-bound writer is the same single bottleneck regardless of shard count).

    The assertions are deliberately LOOSE (CI scheduler noise on a shared runner is large):
    they pin only the qualitative truth, never a fragile exact number —
        (1) write-behind W=1 does not REGRESS below synchronous, and
        (2) W=4 does not MATERIALLY beat W=1 (no sharding win).
    The verbatim numbers live in MIGRATION.md #923 / the PR body, measured on the M-class
    reference box, not re-asserted here (they are machine-dependent).
    """
    symbols = _symbols()

    sync_root = tmp_path / "sync"
    _seed(sync_root, symbols)
    sync_rate = _run_synchronous(sync_root, symbols)

    wb_rates: dict[int, float] = {}
    for shards in (1, 2, 4):
        root = tmp_path / f"wb{shards}"
        _seed(root, symbols)
        wb_rates[shards] = _run_write_behind(root, symbols, shards=shards)

    # (1) Write-behind never REGRESSES the pull below synchronous (a 0.85 floor absorbs
    #     noise; the measured W=1 is ~1.1–1.26x of sync, comfortably above).
    assert wb_rates[1] > sync_rate * 0.85, (
        f"write-behind W=1 ({wb_rates[1]:.2f}/s) regressed below synchronous "
        f"({sync_rate:.2f}/s) — the overlap should never make the pull slower"
    )
    # (2) Sharding (W=4) buys NO material end-to-end gain over a single writer (W=1):
    #     the GIL-bound writer is the same bottleneck. A future "sharded is faster" claim
    #     must justify itself against THIS measurement.
    assert wb_rates[4] < wb_rates[1] * 1.3, (
        f"write-behind W=4 ({wb_rates[4]:.2f}/s) materially beat W=1 ({wb_rates[1]:.2f}/s) "
        f"by >1.3x — unexpected under the GIL-bound writer; re-verify before claiming a "
        f"sharding win"
    )


def test_no_go_decision_is_recorded() -> None:
    """A plain assertion that this bench encodes the NO-GO, not a merge gate.

    Lever 1 sharding does NOT measurably raise end-to-end fetch throughput (the GIL-bound
    writer caps it); the only effect is a small W=1 overlap. The consumer stays DEFAULT-OFF.
    This test documents that the benchmark above is a NO-GO REGRESSION PIN, not a
    pass-to-merge gate — there is no '>3.2/s sharded' claim to satisfy (the old #906-style
    gate was the faked-sleep artefact). See MIGRATION.md #923.
    """
    # The decision is structural (default-off + the loose pins above), so there is nothing
    # numeric to assert here beyond the documented conclusion; this body is intentionally
    # a no-op marker that keeps the NO-GO rationale next to the bench.
    assert True


def _smoke_print_numbers() -> None:  # pragma: no cover - manual run only
    """Print the verbatim numbers (run by hand: ``python -m tests.bench...``), not in CI."""
    tmp = Path(tempfile.mkdtemp(prefix="brutex-wcbench-"))
    try:
        symbols = _symbols()
        sroot = tmp / "sync"
        _seed(sroot, symbols)
        print(f"SYNCHRONOUS: {_run_synchronous(sroot, symbols):.2f} fetch r/s")
        for w in (1, 2, 4):
            r = tmp / f"wb{w}"
            _seed(r, symbols)
            print(f"WRITE-BEHIND W={w}: {_run_write_behind(r, symbols, shards=w):.2f} fetch r/s")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
