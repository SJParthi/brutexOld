"""``run_pull_harness`` — drive the REAL pull stack against the FAKE SDK + measure.

DEC-PULL-SCALE-HARNESS-001 (MIGRATION.md #929). This is the wiring that makes the at-scale
harness HONEST: it composes the production objects (no shims, no shortcuts) and only the
SDK leaf is a test double:

    RateController(ceiling_rps, ceiling_per_min)        ← REAL adaptive limiter + governor
      → build_live_pool(size=pool_size, sdk_factory=fake)  ← REAL ClientPool, ONE CallMeter
        → GrowwMarketAdapter(client=pool, index=...)     ← REAL adapter
          → ingest_spot(..., run_parallel=PoolRunParallel)  ← REAL ingest + thread pool
            → ParquetBarSink(ParquetWriter(tmp_lake))    ← REAL lake writer

The contracts are pulled across an OUTER thread pool (one fan-out per contract) so the
ClientPool's K leaves are genuinely exercised concurrently; the meter is sampled via the
real per-window ``on_window`` hook. Wall-clock + the shared meter give the honest
achieved/allowed/slack numbers in :class:`~tests.bench.pull_harness.report.PullHarnessReport`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from brutex.adapters.call_meter import CallMeter
from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.adapters.groww.ingest import ingest_spot
from brutex.adapters.groww.instruments import load_index_from_csv
from brutex.adapters.groww.live import build_live_pool
from brutex.adapters.rate_common import RateController
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink, SinkAppendResult
from brutex.models import Exchange
from brutex.orchestrator.parallel import PoolRunParallel, workers_to_saturate

from tests.bench.pull_harness.report import PullHarnessReport, percentile
from tests.bench.pull_harness.scenario import PullScenario
from tests.fakes.fake_groww_sdk import FakeGrowwSDK, make_fake_sdk_factory

_CREDS = GrowwCredentials(api_key="harness_key", totp_secret="harness_secret")

#: A far-future clock so the harness's 2024-2025 windows are never current-day-clamped
#: nor old-gap fast-settled into SKIPPED before the fetch runs (the harness wants every
#: planned window actually pulled). A fixed tz-aware datetime in 2099.
_FAR_FUTURE_IST = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)


def _synthetic_csv(n_contracts: int) -> str:
    """Build an ``instrument.csv`` body of *n_contracts* synthetic NSE-EQ rows.

    Each row is a tradable cash-equity symbol ``SYN{i}`` (``NSE-SYN{i}``) so the real
    :class:`GrowwMarketAdapter` resolves it via ``by_exchange_symbol`` + sends the real
    ``groww_symbol`` — the exact production resolution path (DEC-GROWW-HYPHEN-SYMBOL-
    RESOLVE-001).
    """
    header = ",".join(INSTRUMENT_CSV_COLUMNS)
    lines = [header]
    for i in range(n_contracts):
        cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
        cells.update(
            exchange="NSE",
            exchange_token=str(1000 + i),
            trading_symbol=f"SYN{i}",
            groww_symbol=f"NSE-SYN{i}",
            name=f"Synthetic {i}",
            instrument_type="EQ",
            segment="CASH",
            series="EQ",
            lot_size="1",
            tick_size="0.05",
        )
        lines.append(",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS))
    return "\n".join(lines) + "\n"


class _TimedSink:
    """A :class:`~brutex.data.sink.BarSink` that wraps :class:`ParquetBarSink` + times writes.

    Delegates every ``append`` to the REAL sink (so the lake write is the genuine
    Parquet read-modify-write under the partition flock — the fetch/write overlap the
    harness measures) and records the per-call latency (ms) into a thread-safe list for
    the report's ``write_p50_ms`` / ``write_p99_ms``.
    """

    def __init__(self, inner: ParquetBarSink) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.write_ms: list[float] = []

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
        """Time + delegate the lake write (records the latency even for an empty batch)."""
        started = time.perf_counter()
        result = self._inner.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=bars,
            merge_mode=merge_mode,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            self.write_ms.append(elapsed_ms)
        return result


class _MeterSampler:
    """Samples the shared :class:`CallMeter` + controller, both per-window AND in-flight.

    The real ``ingest_spot`` fires ``on_window`` once per processed window on the worker
    thread (:meth:`ping`), which captures the controller's ALLOWED ``current_rps`` for the
    time-average. But ``on_window`` fires AFTER a window's call has already completed, so
    its ``in_flight`` reads ~0 — it cannot measure true concurrency. The honest
    concurrency gauge therefore comes from a short-interval BACKGROUND poller
    (:meth:`run_background`) that samples ``meter.snapshot()["in_flight"]`` WHILE calls are
    in flight — the only way to see the pool's K connections overlapping. Both sources are
    thread-safe; O(1) per sample.
    """

    def __init__(self, meter: CallMeter, controller: RateController) -> None:
        self._meter = meter
        self._controller = controller
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.in_flight: list[float] = []
        self.allowed_rps: list[float] = []

    def ping(self) -> None:
        """Record one ALLOWED-rate sample on the per-window hook (the controller's target)."""
        rps = self._controller.current_rps
        with self._lock:
            self.allowed_rps.append(float(rps))

    def run_background(self, *, interval_s: float = 0.002) -> None:
        """Poll the meter's live ``in_flight`` every *interval_s* until :meth:`stop`.

        Runs on its own daemon thread so it samples the TRUE concurrency while the pull's
        worker threads have calls in flight — the gauge that makes the
        pool-saturation/idle measurement honest (the 0.49-in-flight starve is invisible to
        the post-completion ``on_window`` hook).
        """
        while not self._stop.is_set():
            snap = self._meter.snapshot()
            rps = self._controller.current_rps
            with self._lock:
                self.in_flight.append(float(snap["in_flight"]))
                self.allowed_rps.append(float(rps))
            self._stop.wait(interval_s)

    def stop(self) -> None:
        """Signal the background poller to exit."""
        self._stop.set()


def run_pull_harness(scenario: PullScenario, *, lake_root: Path) -> PullHarnessReport:
    """Run *scenario* against the real pull stack into *lake_root*; return the honest report.

    Builds the REAL controller + pool (fake SDK leaves) + adapter + lake sink, pulls every
    synthetic contract across an outer thread pool (one ``ingest_spot`` fan-out each, so the
    pool's K connections are concurrently busy), samples the shared meter per window, and
    derives the achieved/allowed/slack + pool-idle + write-latency gauges.

    Parameters
    ----------
    scenario
        The synthetic universe + vendor + rate envelope (see :class:`PullScenario`).
    lake_root
        A writable directory (typically a pytest ``tmp_path``) for the real Parquet lake.

    Returns
    -------
    PullHarnessReport
        The frozen, honest measured gauges.
    """
    index = load_index_from_csv(_synthetic_csv(scenario.n_contracts))

    controller = RateController(
        ceiling_rps=scenario.ceiling_rps,
        ceiling_per_min=scenario.ceiling_per_min,
    )
    template = FakeGrowwSDK(
        seed=scenario.seed,
        min_latency_s=scenario.min_latency_s,
        max_latency_s=scenario.max_latency_s,
        candles_per_window=scenario.candles_per_window,
        empty_rate=scenario.empty_rate,
        rate_limit_call_indexes=frozenset(scenario.rate_limit_call_indexes),
        timeout_call_indexes=frozenset(scenario.timeout_call_indexes),
    )
    rate_limit_hits = _Counter()
    timeout_hits = _Counter()
    issued = _Counter()

    def _paced() -> None:
        """The shared pace wrapped to COUNT every token the controller issues.

        Production consumes the bucket TWICE per window — once for the per-window
        ``ingest_spot`` ``pace`` and once inside the client's fast-retry ``pace`` (both
        wired to this controller). Counting the true issued-token rate (vs the physical
        ``get_historical_candles`` count) is the HONEST measure of whether the rate
        controller is the BINDING limiter (CLAUDE.md §2 Rule 6): a binding bucket issues
        at ~ceiling, so the issue-rate slack is near zero even though the per-call rate is
        half that (2 paces/call). O(1) per call.
        """
        controller.pace()
        issued.inc()

    def _on_rate_limit() -> None:
        """Step the controller DOWN on a 429 + tally it (the real on_rate_limit + a count)."""
        controller.on_rate_limit()
        rate_limit_hits.inc()

    def _on_timeout() -> None:
        """Drive the controller's timeout ladder + tally it (the real on_timeout + a count)."""
        controller.on_timeout()
        timeout_hits.inc()

    pool = build_live_pool(
        _CREDS,
        size=scenario.pool_size,
        token_factory=lambda _k, _t: "HARNESS-TOKEN",
        totp_minter=lambda _s: "000000",
        sdk_factory=make_fake_sdk_factory(template),  # type: ignore[arg-type]
        pace=_paced,
        on_rate_limit=_on_rate_limit,
        on_success=controller.on_success,
        # HERMETIC token hooks (DEC-RESAMPLER-INPUT-GUARD-001 gap-2 fallout):
        # with BOTH cache hooks explicitly injected, `_resolve_token_cache_hooks`
        # takes the explicit-hook path and NEVER resolves the AWS Parameter
        # Store backend — without these, `resolve_token_store()` imports boto3
        # and the harness fails in any environment lacking the [token-broker]
        # extra (the CI bench job installs `.[dev,data,ui]` only, so this bench
        # would go RED the moment the [data] fix made it actually execute).
        # A `None` load = cache miss → the injected fake token_factory mints
        # "HARNESS-TOKEN"; the store hook is a no-op success. Offline-pure.
        token_cache_load=lambda **_kw: None,
        token_cache_store=lambda *_a, **_kw: True,
    )
    meter = pool.meter
    adapter = GrowwMarketAdapter(client=pool, index=index)

    writer = ParquetWriter(lake_root=lake_root)
    sink = _TimedSink(ParquetBarSink(writer=writer, catalog=None))
    sampler = _MeterSampler(meter, controller)

    # Size the OUTER (per-contract) pool to the rate budget so the K SDK leaves stay busy;
    # the SHARED rate bucket is still the only rate governor (Rule 6), so over-provisioning
    # workers only fills idle latency, never breaches the cap.
    outer_workers = workers_to_saturate(scenario.ceiling_rps)
    symbols = [f"SYN{i}" for i in range(scenario.n_contracts)]

    def _pull_one(symbol: str) -> int:
        instrument = index.by_exchange_symbol[(Exchange.NSE, symbol)]
        report = ingest_spot(
            adapter,
            sink,
            instrument=instrument,
            interval=scenario.interval,
            start=scenario.start,
            end=scenario.end,
            pace=_paced,
            on_rate_limit=_on_rate_limit,
            on_timeout=_on_timeout,
            on_success=controller.on_success,
            # A fresh inner per-window pool per contract — the production default sizing.
            run_parallel=PoolRunParallel(workers_to_saturate(scenario.ceiling_rps)),
            on_window=sampler.ping,
            # No real-time retry backoff in the harness (deterministic + fast); the
            # transient bursts are exercised via the in-call client retry ladder.
            retry_backoff=lambda _i: None,
            empty_retry_backoff=lambda _i: None,
            now_ist=lambda: _FAR_FUTURE_IST,
        )
        return report.bars_appended

    sampler_thread = threading.Thread(target=sampler.run_background, daemon=True)
    started = time.perf_counter()
    sampler_thread.start()
    bars_total = 0
    try:
        with ThreadPoolExecutor(max_workers=outer_workers) as executor:
            for appended in executor.map(_pull_one, symbols):
                bars_total += appended
    finally:
        sampler.stop()
        sampler_thread.join(timeout=5.0)
    wall_s = max(time.perf_counter() - started, 1e-9)

    final = meter.snapshot()
    physical_calls = sum(leaf.calls for leaf in _fake_leaves(pool))
    empty_windows = sum(leaf.empty_windows for leaf in _fake_leaves(pool))

    # The HONEST slack denominator is the ISSUED-token rate (the rate controller's
    # governed unit): a binding bucket issues at ~ceiling, so issue-rate slack ≈ 0 even
    # though the per-call rate is half (2 paces/window). ``physical_call_rps`` is the
    # second honest number — the actual vendor round-trips/sec.
    issued_rps = issued.value / wall_s
    physical_call_rps = physical_calls / wall_s
    allowed_timeavg = (
        sum(sampler.allowed_rps) / len(sampler.allowed_rps)
        if sampler.allowed_rps
        else scenario.ceiling_rps
    )
    slack_rps = max(0.0, allowed_timeavg - issued_rps)
    slack_ratio = slack_rps / allowed_timeavg if allowed_timeavg > 0.0 else 0.0
    in_flight_p50 = percentile(sampler.in_flight, 50.0)
    in_flight_max = int(max(sampler.in_flight)) if sampler.in_flight else 0
    pool_idle_pct = max(0.0, min(100.0, (1.0 - (in_flight_p50 / scenario.pool_size)) * 100.0))

    return PullHarnessReport(
        achieved_rps=round(issued_rps, 3),
        physical_call_rps=round(physical_call_rps, 3),
        allowed_rps_timeavg=round(allowed_timeavg, 3),
        slack_rps=round(slack_rps, 3),
        slack_ratio=round(slack_ratio, 4),
        in_flight_p50=round(in_flight_p50, 3),
        in_flight_max=in_flight_max,
        pool_idle_pct=round(pool_idle_pct, 2),
        pool_size=scenario.pool_size,
        rps_1s_max=int(final["rps_1s_max"]),
        rate_limit_hits=rate_limit_hits.value,
        timeout_hits=timeout_hits.value,
        empty_windows=empty_windows,
        write_p50_ms=round(percentile(sink.write_ms, 50.0), 3),
        write_p99_ms=round(percentile(sink.write_ms, 99.0), 3),
        physical_calls=physical_calls,
        bars_appended=bars_total,
        windows=len(sink.write_ms),
        wall_s=round(wall_s, 4),
        seed=scenario.seed,
    )


class _Counter:
    """A tiny thread-safe integer counter (429 / timeout tallies)."""

    __slots__ = ("_lock", "_n")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._n = 0

    def inc(self) -> None:
        with self._lock:
            self._n += 1

    @property
    def value(self) -> int:
        with self._lock:
            return self._n


def _fake_leaves(pool: object) -> list[FakeGrowwSDK]:
    """Return the pool's :class:`FakeGrowwSDK` leaves (the per-connection SDK instances).

    The :class:`~brutex.adapters.groww.pool.ClientPool` holds its leaf
    :class:`~brutex.adapters.groww.client.GrowwClient`s privately; each client's ``sdk``
    is the fake. Reaches through the ``_clients`` slot to total the real per-connection
    call counters — test-only introspection of a test-only pool.
    """
    clients = getattr(pool, "_clients", ())
    leaves: list[FakeGrowwSDK] = []
    for client in clients:
        sdk = getattr(client, "sdk", None)
        if isinstance(sdk, FakeGrowwSDK):
            leaves.append(sdk)
    return leaves


__all__ = ["run_pull_harness"]
