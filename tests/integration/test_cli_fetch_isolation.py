# pyright: reportPrivateUsage=none
"""The CLI ``brutex fetch`` spot pull is fetch/write-ISOLATED like the dashboard.

DEC-CLI-FETCH-ISOLATION-001. Before this, ``cmd_fetch`` called
:func:`~brutex.adapters.groww.run_ingest.run_groww_spot_ingest_with_retry` WITHOUT a
``write_pool`` / ``fetch_pipeline`` (both defaulted to ``None``), so each window's tail
(session-filter + the durable Parquet ``sink.append`` + ``ledger.record``) ran INLINE on
the fetch worker thread BETWEEN every two fetches — the dashboard had the async
fetch/process separation (DEC-GROWW-FETCH-ASYNC-SEPARATION-001) but the CLI did not.

This module proves the CLI now wires the SAME COMMON async pipeline:

* **unit (wiring + drain lifecycle)** — ``cmd_fetch`` builds + passes a NON-None
  ``fetch_pipeline`` (and ``write_pool`` when the batch flag is on) and DRAINS both in its
  ``finally`` — on a clean return AND on an exception path (a crash flushes the backlog).
* **anti-fake (ISOLATION PROOF)** — a REAL ``run_groww_spot_ingest_with_retry`` over a REAL
  ``ParquetBarSink`` whose every ``append`` SLEEPS: with the pipeline ON the CLI pull
  finishes in WELL under the synchronous floor (the slow write never blocks the fetch lane),
  and the bytes land durably after the drain.
* **regression (old behaviour pinned)** — with the ``BRUTEX_GROWW_FETCH_PIPELINE=off``
  kill-switch the CLI passes ``fetch_pipeline=None`` / ``write_pool=None`` (the
  pre-wiring fully-synchronous inline-tail path) AND the same slow-write pull is dominated by
  the serial write floor — the exact slowdown this fix removes.

Only the creds gate + the live adapter/sink build are faked (no network, no Groww keys);
everything below — the ingest spine, the pipeline, the sink, the ledger — is the real stack.
"""

from __future__ import annotations

import importlib
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.data.models import CandleBar


def _bar(start: date) -> CandleBar:
    """One in-session bar dated to the window's first day (04:05 UTC = 09:35 IST)."""
    ts = datetime(start.year, start.month, start.day, 4, 5, tzinfo=UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=1,
        open_interest=None,
    )


class _TimedAdapter:
    """A fake spot adapter that records each fetch instant + returns one bar per window."""

    def __init__(self) -> None:
        self.fetch_instants: list[float] = []

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, end
        self.fetch_instants.append(time.monotonic())
        return [_bar(start)]


def _credless(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the up-front Groww creds gate without any real keys.

    The gate resolves via ``groww_credentials_error`` (``None`` = credentials
    present; DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001).
    """
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)


def _one_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the brute-force universe to ONE synthetic index (full Instrument fields)."""
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)


# ───────────────────────── unit: wiring + drain lifecycle ─────────────────────────


class TestCmdFetchWiresAndDrainsThePipeline:
    """``cmd_fetch`` builds + passes a non-None pipeline and drains it in ``finally``."""

    def _arrange(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Fake everything network-shaped; spy on the ingest kwargs + the COMMON drains."""
        _credless(monkeypatch)
        _one_index(monkeypatch)
        monkeypatch.setattr(
            cli, "_build_live_groww_spot", lambda lake, catalog=None: (object(), object())
        )
        # The pipeline is ON by default — make sure no leaked env disables it.
        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)

        seen: dict[str, Any] = {}
        drains: list[str] = []

        # Spy the COMMON drain helpers AT THE SOURCE module (cli imports them by name into
        # its local frame at call time, so patching the source attribute is what the CLI sees).
        pull_pipeline = importlib.import_module("brutex.adapters.groww.pull_pipeline")
        real_drain_pipe = pull_pipeline.drain_fetch_pipeline
        real_drain_pool = pull_pipeline.drain_write_consumer

        def _spy_drain_pipe(pipeline: Any) -> None:
            drains.append("pipeline")
            real_drain_pipe(pipeline)

        def _spy_drain_pool(consumer: Any) -> None:
            drains.append("write_pool")
            real_drain_pool(consumer)

        monkeypatch.setattr(pull_pipeline, "drain_fetch_pipeline", _spy_drain_pipe)
        monkeypatch.setattr(pull_pipeline, "drain_write_consumer", _spy_drain_pool)
        return {"seen": seen, "drains": drains}

    def test_clean_pull_passes_pipeline_and_drains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clean pull threads a live ``fetch_pipeline`` into the ingest + drains it after."""
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        state = self._arrange(monkeypatch)
        seen, drains = state["seen"], state["drains"]
        # The batched write consumer is OFF by default → write_pool is None (the pipeline owns
        # the tail). Forcing it ON would build a write_pool too; here we assert the DEFAULT.
        monkeypatch.delenv("BRUTEX_WRITE_BATCH", raising=False)

        from brutex.adapters.groww.fetch_pipeline import FetchPipeline

        run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

        def fake_ingest(_adapter: Any, _sink: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return _clean_result()

        monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

        rc = cli.cmd_fetch(
            tmp_path,
            "2024-01-02",
            "2024-01-03",
            "1m",
            window_days=None,
            dry_run=False,
            inspect=False,
            rows_per_chunk=5,
        )
        assert rc == cli.EXIT_OK
        # The CLI now isolates the fetch from the write: a LIVE pipeline was threaded in…
        assert isinstance(seen["fetch_pipeline"], FetchPipeline)
        assert seen["write_pool"] is None  # default batch flag off → pipeline owns the tail
        # …and BOTH drains ran (pipeline FIRST, then the write consumer) in the finally.
        assert drains == ["pipeline", "write_pool"]

    def test_write_batch_flag_also_builds_and_drains_write_pool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the pipeline OFF + ``BRUTEX_WRITE_BATCH=1`` the CLI threads a live write_pool."""
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        state = self._arrange(monkeypatch)
        seen, drains = state["seen"], state["drains"]
        # Pipeline OFF (kill-switch) so the Lever-1 batched write consumer is the isolation path.
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")

        from brutex.data.write_consumer import BatchingWriteConsumer

        run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

        def fake_ingest(_adapter: Any, _sink: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return _clean_result()

        monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

        rc = cli.cmd_fetch(
            tmp_path,
            "2024-01-02",
            "2024-01-03",
            "1m",
            window_days=None,
            dry_run=False,
            inspect=False,
            rows_per_chunk=5,
        )
        assert rc == cli.EXIT_OK
        assert seen["fetch_pipeline"] is None  # kill-switch → synchronous fetch path
        assert isinstance(seen["write_pool"], BatchingWriteConsumer)
        assert drains == ["pipeline", "write_pool"]

    def test_exception_path_still_drains_in_finally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash mid-pull STILL drains the pipeline + write consumer (flush the backlog).

        The drain lives in ``cmd_fetch``'s ``finally``, so an ingest raising a non-halt error
        propagates out of ``cmd_fetch`` (the existing crash contract) but the pipeline is drained
        + joined cleanly FIRST — never leaking a live consumer pool on the crash path.
        """
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        state = self._arrange(monkeypatch)
        seen, drains = state["seen"], state["drains"]
        monkeypatch.delenv("BRUTEX_WRITE_BATCH", raising=False)

        run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

        def boom_ingest(_adapter: Any, _sink: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            raise RuntimeError("mid-pull crash")

        monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", boom_ingest)

        with pytest.raises(RuntimeError, match="mid-pull crash"):
            cli.cmd_fetch(
                tmp_path,
                "2024-01-02",
                "2024-01-03",
                "1m",
                window_days=None,
                dry_run=False,
                inspect=False,
                rows_per_chunk=5,
            )
        # The crash propagated, but the COMMON drains STILL ran in the finally (pipeline first).
        assert drains == ["pipeline", "write_pool"]

    def test_dry_run_builds_no_pipeline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--dry-run`` (no real writes) builds NEITHER pipeline nor write_pool (both None).

        The synthetic offline pull writes sample bars to a real sink, but ``_no_persist`` makes
        ``build_fetch_pipeline`` / ``build_write_consumer`` return ``None`` (mirroring
        ``--inspect``), so the pull is byte-identical to the prior fully-synchronous dry run.
        """
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)
        seen: dict[str, Any] = {}
        run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

        def fake_ingest(_adapter: Any, _sink: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return _clean_result()

        monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

        rc = cli.cmd_fetch(
            tmp_path,
            "2024-01-02",
            "2024-01-03",
            "1m",
            window_days=None,
            dry_run=True,
            inspect=False,
            rows_per_chunk=2,
        )
        assert rc == cli.EXIT_OK
        assert seen["fetch_pipeline"] is None
        assert seen["write_pool"] is None

    def test_kill_switch_threads_the_old_fully_synchronous_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION pin (kwargs): the ``=off`` kill-switch + no batch flag → BOTH None.

        With ``BRUTEX_GROWW_FETCH_PIPELINE=off`` and ``BRUTEX_WRITE_BATCH`` unset the CLI threads
        ``fetch_pipeline=None`` AND ``write_pool=None`` into the ingest — the EXACT
        pre-DEC-CLI-FETCH-ISOLATION-001 call shape, where the per-window tail runs INLINE on the
        fetch worker (no isolation). This pins that the kill-switch reproduces the old behaviour
        byte-for-byte (the timing twin is the slow-sink regression integration test).
        """
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        state = self._arrange(monkeypatch)
        seen, drains = state["seen"], state["drains"]
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
        monkeypatch.delenv("BRUTEX_WRITE_BATCH", raising=False)

        run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")

        def fake_ingest(_adapter: Any, _sink: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return _clean_result()

        monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)

        rc = cli.cmd_fetch(
            tmp_path,
            "2024-01-02",
            "2024-01-03",
            "1m",
            window_days=None,
            dry_run=False,
            inspect=False,
            rows_per_chunk=5,
        )
        assert rc == cli.EXIT_OK
        # The old fully-synchronous inline-tail call shape: NEITHER isolation primitive threaded.
        assert seen["fetch_pipeline"] is None
        assert seen["write_pool"] is None
        # The drains still run unconditionally (both no-ops on None) — the finally is uniform.
        assert drains == ["pipeline", "write_pool"]


def _clean_result() -> Any:
    """A duck-typed clean ``SpotIngestWithRetry`` (1 window, no failures, empty pairs).

    The empty ``pairs`` mapping is what the async-pipeline completion-count reconcile folds
    into — the fake ingest enqueues nothing, so the drained pipeline has no per-series totals
    and the report passes through unchanged.
    """
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class _Report:
        windows: int = 1
        bars_appended: int = 375
        failed_pairs: int = 0
        transient_failures: int = 0
        still_failed_windows: int = 0
        pairs: dict[tuple[str, str], Any] = field(default_factory=dict)

    @dataclass(frozen=True)
    class _Result:
        report: _Report = field(default_factory=_Report)
        retry: None = None

        @property
        def still_failed_windows(self) -> int:
            return self.report.still_failed_windows

    return _Result()


# ───────────────── anti-fake: ISOLATION PROOF (real spine, real sink) ─────────────────


def _run_cli_pull_over_slow_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    write_sleep: float,
    pipeline_off: bool,
) -> tuple[float, _TimedAdapter, Path]:
    """Run a REAL ``cmd_fetch`` spot pull whose every durable write SLEEPS *write_sleep*.

    Returns the (fetch_span, adapter, lake_root). The fetch_span is
    ``fetch_instants[-1] - fetch_instants[0]`` — the wall time the PRODUCER spent issuing all of
    its fetches. That is the precise "is the fetch lane progressing?" signal: when the durable
    write blocks the fetch worker (the inline / kill-switch path) the producer can only issue the
    NEXT fetch after the PREVIOUS window's write returns, so the span inflates to ~the serial
    write floor; when the write is decoupled onto the pipeline consumer (the fix) the producer
    races ahead at its own pacing and the span stays far below the floor — even though the TOTAL
    pull still waits on the consumer drain at the end (the writes are durable, just off the fetch
    lane). Faked: the creds gate + the live adapter/sink build; the spine + pipeline + sink +
    ledger are the real stack.

    The PRODUCER fan-out is pinned to SERIAL (``run_ingest.PoolRunParallel`` →
    ``SerialRunParallel``) so a single window's durable write must return before the next fetch on
    the INLINE path — a single-pair pull otherwise hands its windows the full multi-worker outer
    pool (``run_ingest.py``'s ``inner_runner = runner`` branch), which would fetch ahead and
    incidentally hide the write regardless of the pipeline.
    """
    from brutex.adapters.groww import run_ingest as _run_ingest_mod
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink
    from brutex.orchestrator.parallel import SerialRunParallel

    lake = tmp_path / "lake"
    # Pin the PRODUCER to a serial window runner so the inline write tail blocks the next fetch.
    monkeypatch.setattr(_run_ingest_mod, "PoolRunParallel", SerialRunParallel)

    class _SlowSink(ParquetBarSink):
        def append(self, **kw: object):  # type: ignore[override,no-untyped-def]
            time.sleep(write_sleep)
            return super().append(**kw)  # type: ignore[arg-type]

    adapter = _TimedAdapter()
    _credless(monkeypatch)
    _one_index(monkeypatch)

    def _build(lake_root: Path, catalog: Any = None) -> tuple[object, object]:
        return adapter, _SlowSink(writer=ParquetWriter(lake_root=lake_root), catalog=catalog)

    monkeypatch.setattr(cli, "_build_live_groww_spot", _build)
    monkeypatch.delenv("BRUTEX_WRITE_BATCH", raising=False)
    if pipeline_off:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
    else:
        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)

    rc = cli.cmd_fetch(
        lake,
        "2021-01-01",
        "2021-09-01",  # ~8 monthly 1-minute tiles → ~8 slow writes
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=1,
    )
    assert rc == cli.EXIT_OK
    instants = adapter.fetch_instants
    fetch_span = (instants[-1] - instants[0]) if len(instants) >= 2 else 0.0
    return fetch_span, adapter, lake


#: Per-write sleep CHOSEN to exceed the CLI live pull's per-window rate-controller pacing
#: (~0.13-0.20 s/window at the 8 r/s ceiling). Making the slow write the DOMINANT per-window
#: cost is what surfaces the isolation: INLINE it serialises onto the fetch worker (wall ≈
#: n × write_sleep); on the pipeline it overlaps the fetch/pace loop on a background consumer
#: (wall ≈ the smaller pacing floor). A write SHORTER than the pace would be hidden behind the
#: pace even inline, erasing the signal.
_SLOW_WRITE_SECONDS = 0.4


class TestCliFetchIsolationProof:
    """The CLI fetch lane keeps progressing through a SLOW durable write (real spine).

    The CLI live pull drives the REAL spine over a REAL ``ParquetBarSink`` whose every
    ``append`` SLEEPS :data:`_SLOW_WRITE_SECONDS` (chosen ABOVE the per-window pacing so the slow
    write is the dominant per-window cost). With the pipeline ON those writes run on a background
    consumer so the fetch lane never waits on them; with the ``BRUTEX_GROWW_FETCH_PIPELINE=off``
    kill-switch they run INLINE on the fetch worker (the pre-DEC-CLI-FETCH-ISOLATION-001
    behaviour), serialising the pull at ≥ the write floor.
    """

    def test_slow_write_does_not_block_the_cli_fetch_lane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ON: the producer issues ALL fetches in WELL under the serial write floor.

        Each window's durable ``sink.append`` SLEEPS :data:`_SLOW_WRITE_SECONDS`. With the CLI's
        async pipeline (this fix) those writes run on the background consumer pool, so the
        producer races ahead at its own pacing and issues every fetch WITHOUT waiting on the tail
        — the FETCH SPAN (first→last fetch) lands far below the ~8 × sleep serial write floor. The
        data still lands durably after the pull's drain (settle-after-durable).
        """
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        fetch_span, adapter, lake = _run_cli_pull_over_slow_sink(
            tmp_path, monkeypatch, write_sleep=_SLOW_WRITE_SECONDS, pipeline_off=False
        )
        n = len(adapter.fetch_instants)
        assert n >= 6, f"expected ~8 monthly tiles, fetched {n}"
        serial_write_floor = n * _SLOW_WRITE_SECONDS
        # The producer issued every fetch in WELL under the serial write floor — the slow write
        # never blocked the fetch lane. The 0.7× margin stays robust on a busy CI box while the
        # inline sibling (a span ≈ the floor) below is unambiguously on the other side.
        assert fetch_span < 0.7 * serial_write_floor, (
            f"fetch span was {fetch_span:.3f}s for {n} windows — NOT isolated from the "
            f"{serial_write_floor:.3f}s of serial writes (the fetch lane was blocked)"
        )
        assert list(lake.rglob("*.parquet")), "no spot Parquet written after the drained pull"

    def test_kill_switch_off_reproduces_the_inline_serial_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION pin: ``BRUTEX_GROWW_FETCH_PIPELINE=off`` runs the old inline-tail path.

        With the kill-switch set the CLI builds NO pipeline (the pre-DEC-CLI-FETCH-ISOLATION-001
        behaviour), so each window's durable ``sink.append`` runs INLINE on the fetch worker — and
        because the producer is serial, the NEXT fetch can only issue AFTER the previous window's
        write returns. The FETCH SPAN therefore inflates to ~the serial write floor (the exact
        slowdown the wiring removes — contrast the isolated sibling above, whose span is < 0.7×
        the floor). This pins that, without the wiring, the write tail blocks the fetch thread.
        """
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        pytest.importorskip("pyarrow")
        fetch_span, adapter, lake = _run_cli_pull_over_slow_sink(
            tmp_path, monkeypatch, write_sleep=_SLOW_WRITE_SECONDS, pipeline_off=True
        )
        n = len(adapter.fetch_instants)
        assert n >= 6, f"expected ~8 monthly tiles, fetched {n}"
        # The inline write is on the fetch critical path: each of the (n-1) gaps between fetches
        # is AT LEAST one write_sleep, so the first→last span is ≥ (n-1) × write_sleep. A 0.9×
        # margin absorbs only timing jitter, never the background-overlap a pipeline would give.
        inline_span_floor = (n - 1) * _SLOW_WRITE_SECONDS
        assert fetch_span >= 0.9 * inline_span_floor, (
            f"kill-switch fetch span was only {fetch_span:.3f}s for {n} windows — expected the "
            f"inline write tail to block the fetch lane (≥ {0.9 * inline_span_floor:.3f}s)"
        )
        assert list(lake.rglob("*.parquet")), "no spot Parquet written on the synchronous path"
