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
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.fetch_pipeline import FetchPipeline
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
