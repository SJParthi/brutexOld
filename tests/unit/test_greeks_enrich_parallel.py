"""Unit tests for the parallel Greeks-enrichment driver (DEC-GREEKS-PARALLEL-001).

Coverage strategy (deliberate): child-process code is NOT traced by the
coverage gates (pytest-cov 7 has no subprocess embed machinery in this repo),
so EVERY worker-side function — ``_greeks_worker_init`` / ``_greeks_worker`` /
``_run_unit`` / the parent fold / the halt + BrokenProcessPool handlers — is
exercised here by DIRECT IN-PROCESS calls (the ``test_engine_sweep_parallel``
precedent) plus an inline synchronous executor stand-in for the pool driver.
The REAL spawn pool is behaviourally proven by the dual-path byte-identity
golden (``tests/golden/test_greeks_enrich_parallel_golden.py``) — that test
contributes parent-side proof only, never the line coverage.
"""

from __future__ import annotations

from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")
pytest.importorskip("polars", reason="the default lake seams need polars")

import brutex.data.greeks_enrich as ge
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.greeks_enrich import (
    GREEKS_WORKERS_ENV,
    EnrichStats,
    GreeksEnrichTarget,
    _build_units,
    _greeks_worker,
    _greeks_worker_init,
    _GreeksUnit,
    _GreeksUnitResult,
    _GreeksWorkerContext,
    _run_parallel,
    _run_target,
    _run_unit,
    _SpotJoinMemo,
    _TargetOutcome,
    enrich_greeks_for_targets,
    greeks_pool_workers,
    resolve_greeks_workers,
)
from brutex.data.models import CandleBar
from brutex.data.spot_join import SpotJoin
from brutex.observability.progress_beat import (
    register_progress_beat,
    unregister_progress_beat,
)
from brutex.options.risk_free import CsvRateSource, FixedRateSource

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from brutex.data.fno_symbol import FnoSymbolParts
    from brutex.data.models import FnoGreeksBar

_RATE = FixedRateSource()
_WINDOW = (date(2024, 1, 1), date(2024, 1, 26))
_WINDOW_B = (date(2024, 2, 1), date(2024, 2, 26))
_BASE_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)  # 09:15 IST
_NIFTY_CE = "NSE-NIFTY-25Jan24-21500-CE"
_NIFTY_PE = "NSE-NIFTY-25Jan24-21000-PE"
_BANK_CE = "NSE-BANKNIFTY-25Jan24-45500-CE"
_FUT = "NSE-NIFTY-25Jan24-FUT"


def _target(symbol: str, window: tuple[date, date] = _WINDOW) -> GreeksEnrichTarget:
    return GreeksEnrichTarget(exchange="NSE", symbol=symbol, start=window[0], end=window[1])


def _bar(ts: datetime, close: str, *, oi: int | None) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(close),
        high=Decimal(close) + 2,
        low=Decimal(close) - 2,
        close=Decimal(close),
        volume=7,
        open_interest=oi,
    )


def _build_two_underlying_lake(lake: Path) -> list[GreeksEnrichTarget]:
    """A REAL two-chain lake: NIFTY (CE+PE) + BANKNIFTY (CE), one spot each."""
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    spot = [_bar(_BASE_TS + timedelta(minutes=i), "21500", oi=None) for i in range(2)]
    sink.append(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=spot)
    bank = [_bar(_BASE_TS + timedelta(minutes=i), "45500", oi=None) for i in range(2)]
    sink.append(exchange="NSE", segment="INDEX", symbol="BANKNIFTY", interval="1minute", bars=bank)
    ce = [_bar(_BASE_TS + timedelta(minutes=i), c, oi=50) for i, c in enumerate(("200", "205.5"))]
    sink.append(exchange="NSE", segment="FNO", symbol=_NIFTY_CE, interval="1minute", bars=ce)
    pe = [_bar(_BASE_TS + timedelta(minutes=i), c, oi=25) for i, c in enumerate(("35.6", "36.1"))]
    sink.append(exchange="NSE", segment="FNO", symbol=_NIFTY_PE, interval="1minute", bars=pe)
    bce = [_bar(_BASE_TS + timedelta(minutes=i), c, oi=10) for i, c in enumerate(("410", "415"))]
    sink.append(exchange="NSE", segment="FNO", symbol=_BANK_CE, interval="1minute", bars=bce)
    return [_target(_NIFTY_CE), _target(_NIFTY_PE), _target(_BANK_CE)]


def _lake_hashes(lake: Path) -> dict[str, str]:
    import hashlib

    return {
        str(p.relative_to(lake)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((lake / "bars").rglob("*.parquet"))
    }


class _InlinePool:
    """A synchronous in-process ``ProcessPoolExecutor`` stand-in.

    Runs the initializer + every submitted task in the TEST process so every
    worker-side line is coverage-traced; results/exceptions ride real
    ``Future`` objects so ``as_completed`` behaves identically.
    """

    last: _InlinePool | None = None

    def __init__(
        self,
        max_workers: int | None = None,
        mp_context: object = None,
        initializer: Callable[..., None] | None = None,
        initargs: tuple[object, ...] = (),
    ) -> None:
        type(self).last = self
        self.max_workers = max_workers
        self.mp_context = mp_context
        self.shutdown_calls: list[tuple[bool, bool]] = []
        if initializer is not None:
            initializer(*initargs)

    def __enter__(self) -> _InlinePool:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown(wait=True)

    def submit(self, fn: Callable[..., Any], /, *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Snapshot the env keys + module global the in-process initializer mutates."""
    for key in ("OMP_NUM_THREADS", "RAYON_NUM_THREADS", "POLARS_MAX_THREADS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ge, "_GREEKS_WORKER_CTX", None)
    return monkeypatch


class TestWorkersResolver:
    def test_pool_workers_none_cpu_is_sequential(self) -> None:
        assert greeks_pool_workers(None) == 1

    @pytest.mark.parametrize(
        ("cpu", "expected"), [(1, 1), (2, 1), (3, 1), (4, 2), (10, 8), (14, 8), (64, 8)]
    )
    def test_pool_workers_formula(self, cpu: int, expected: int) -> None:
        assert greeks_pool_workers(cpu) == expected

    def test_unset_env_resolves_auto(self) -> None:
        assert resolve_greeks_workers({}, cpu_count_fn=lambda: 6) == 4

    def test_blank_env_resolves_auto(self) -> None:
        assert resolve_greeks_workers({GREEKS_WORKERS_ENV: "  "}, cpu_count_fn=lambda: 6) == 4

    def test_explicit_env_wins(self) -> None:
        assert resolve_greeks_workers({GREEKS_WORKERS_ENV: "6"}, cpu_count_fn=lambda: 4) == 6

    def test_env_one_is_the_kill_switch(self) -> None:
        assert resolve_greeks_workers({GREEKS_WORKERS_ENV: "1"}, cpu_count_fn=lambda: 64) == 1

    @pytest.mark.parametrize("raw", ["abc", "0", "-3", "2.5"])
    def test_invalid_env_warns_and_falls_back_to_auto(self, raw: str) -> None:
        with capture_logs() as logs:
            resolved = resolve_greeks_workers({GREEKS_WORKERS_ENV: raw}, cpu_count_fn=lambda: 6)
        assert resolved == 4
        events = [e for e in logs if e["event"] == "greeks_enrich.workers_invalid"]
        assert len(events) == 1
        assert events[0]["requested_env"] == repr(raw)

    def test_defaults_read_process_env_and_cpu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREEKS_WORKERS_ENV, "3")
        assert resolve_greeks_workers() == 3


class TestBuildUnits:
    def test_one_unit_per_chain_no_collision(self) -> None:
        targets = [_target(_NIFTY_CE), _target(_NIFTY_PE), _target(_BANK_CE)]
        ordered = sorted(targets, key=ge._target_sort_key)
        units = _build_units(ordered)
        # BANKNIFTY sorts before NIFTY; the CE+PE share the NIFTY chain.
        assert [u.targets for u in units] == [
            (_target(_BANK_CE),),
            (_target(_NIFTY_PE), _target(_NIFTY_CE)),
        ]
        assert [u.index for u in units] == [0, 1]

    def test_two_windows_without_collision_stay_separate_units(self) -> None:
        targets = [_target(_NIFTY_CE, _WINDOW), _target(_NIFTY_PE, _WINDOW_B)]
        units = _build_units(sorted(targets, key=ge._target_sort_key))
        assert len(units) == 2

    def test_same_symbol_two_windows_merges_the_underlying_group(self) -> None:
        """Symbol affinity (public-API shape): every occurrence of one symbol
        must land on ONE worker so its writes stay time-ordered."""
        targets = [
            _target(_NIFTY_CE, _WINDOW),
            _target(_NIFTY_CE, _WINDOW_B),
            _target(_NIFTY_PE, _WINDOW),
            _target(_BANK_CE),
        ]
        units = _build_units(sorted(targets, key=ge._target_sort_key))
        assert len(units) == 2  # BANKNIFTY chain + ONE merged NIFTY unit
        merged = units[1]
        assert [t.symbol for t in merged.targets] == [_NIFTY_PE, _NIFTY_CE, _NIFTY_CE]

    def test_duplicate_identical_targets_share_one_chain(self) -> None:
        units = _build_units(sorted([_target(_NIFTY_CE)] * 2, key=ge._target_sort_key))
        assert len(units) == 1
        assert len(units[0].targets) == 2

    def test_undecodable_and_fut_key_on_raw_symbol_fallback(self) -> None:
        targets = [_target("not-a-contract"), _target(_FUT)]
        units = _build_units(sorted(targets, key=ge._target_sort_key))
        # "not-a-contract" keys on the raw symbol; the FUT decodes to NIFTY.
        assert len(units) == 2


class TestRunTargetPolicy:
    """The shared per-target body's caller-owned exception policy."""

    def _memo(self) -> _SpotJoinMemo:
        return _SpotJoinMemo(lambda _t, _p: SpotJoin({}, None))

    def test_memory_error_quarantined_under_sequential_default(self) -> None:
        """Pinned pre-refactor behaviour: the sequential ``except Exception``
        quarantine catches ``MemoryError`` too (documented, unchanged)."""

        def _read(_t: GreeksEnrichTarget) -> list[dict[str, object]]:
            raise MemoryError("boom")

        outcome = _run_target(
            _target(_NIFTY_CE),
            reader=_read,
            writer=lambda _t, _b: 0,
            memo=self._memo(),
            rate_source=_RATE,
        )
        assert outcome.kind == "errored"
        assert outcome.error == "boom"

    def test_memory_error_reraises_under_the_worker_policy(self) -> None:
        def _read(_t: GreeksEnrichTarget) -> list[dict[str, object]]:
            raise MemoryError("worker OOM")

        with pytest.raises(MemoryError):
            _run_target(
                _target(_NIFTY_CE),
                reader=_read,
                writer=lambda _t, _b: 0,
                memo=self._memo(),
                rate_source=_RATE,
                reraise=(LakeWriteHalt, MemoryError),
            )

    def test_errored_after_solve_keeps_the_skipped_tally(self) -> None:
        """``bars_skipped`` counted BEFORE the write (pre-refactor semantics):
        a writer failure must not lose the non-finite-row skip tally."""
        rows: list[dict[str, object]] = [
            {
                "timestamp": _BASE_TS,
                "open": 200.0,
                "high": 205.0,
                "low": 195.0,
                "close": 200.0,
                "volume": 10,
                "open_interest": 100,
            },
            {
                "timestamp": _BASE_TS + timedelta(minutes=1),
                "open": float("nan"),
                "high": 205.0,
                "low": 195.0,
                "close": 200.0,
                "volume": 10,
                "open_interest": 100,
            },
        ]
        from brutex.data.models import GREEKS_OVERLAY_FIELDS

        for row in rows:
            for name in GREEKS_OVERLAY_FIELDS:
                row.setdefault(name, None)

        def _write(_t: GreeksEnrichTarget, _b: Sequence[FnoGreeksBar]) -> int:
            raise OSError("disk hiccup")

        outcome = _run_target(
            _target(_NIFTY_CE),
            reader=lambda _t: rows,
            writer=_write,
            memo=_SpotJoinMemo(lambda _t, _p: SpotJoin({_BASE_TS: Decimal("21500")}, "INDEX")),
            rate_source=_RATE,
        )
        assert outcome.kind == "errored"
        assert outcome.skipped == 1


class TestWorkerInit:
    def test_pins_single_threaded_numerics_and_stashes_ctx(
        self, worker_env: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import os

        ctx = _GreeksWorkerContext(lake_root=tmp_path, rate_source=_RATE)
        _greeks_worker_init(ctx)
        assert os.environ["OMP_NUM_THREADS"] == "1"
        assert os.environ["RAYON_NUM_THREADS"] == "1"
        assert os.environ["POLARS_MAX_THREADS"] == "1"
        assert ge._GREEKS_WORKER_CTX is ctx


class TestRunUnit:
    def test_enriches_a_real_chain_over_default_seams(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)
        ordered = sorted(targets, key=ge._target_sort_key)
        units = _build_units(ordered)
        result = _run_unit(units[1], lake_root=lake, rate_source=_RATE)
        assert result.halt_reason is None
        assert [o.kind for o in result.outcomes] == ["enriched", "enriched"]
        assert result.spot_reads == 1  # CE + PE share ONE chain read
        assert result.spot_hits == 1

    def test_lake_write_halt_stops_the_unit_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)
        reads: list[str] = []
        real_read = ge.read_option_rows

        def _spy_read(root: Path, target: GreeksEnrichTarget) -> list[dict[str, object]]:
            reads.append(target.symbol)
            return real_read(root, target)

        def _halt(_root: Path, _t: GreeksEnrichTarget, _b: Sequence[FnoGreeksBar]) -> int:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr(ge, "read_option_rows", _spy_read)
        monkeypatch.setattr(ge, "write_option_greeks", _halt)
        units = _build_units(sorted(targets, key=ge._target_sort_key))
        result = _run_unit(units[1], lake_root=lake, rate_source=_RATE)
        assert result.halt_reason == "disk_full"
        assert result.outcomes == ()  # the first target halted — nothing folded
        assert reads == [_NIFTY_PE]  # the second target was never even read

    def test_memory_error_propagates_out_of_the_unit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)

        def _oom(_root: Path, _t: GreeksEnrichTarget) -> list[dict[str, object]]:
            raise MemoryError("worker OOM")

        monkeypatch.setattr(ge, "read_option_rows", _oom)
        units = _build_units(sorted(targets, key=ge._target_sort_key))
        with pytest.raises(MemoryError):
            _run_unit(units[0], lake_root=lake, rate_source=_RATE)

    def test_ordinary_error_quarantines_in_unit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)

        def _boom(_root: Path, _t: GreeksEnrichTarget) -> list[dict[str, object]]:
            raise OSError("corrupt partition")

        monkeypatch.setattr(ge, "read_option_rows", _boom)
        units = _build_units(sorted(targets, key=ge._target_sort_key))
        result = _run_unit(units[0], lake_root=lake, rate_source=_RATE)
        assert result.halt_reason is None
        assert [o.kind for o in result.outcomes] == ["errored"]

    def test_greeks_worker_binds_the_initializer_context(
        self, worker_env: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)
        units = _build_units(sorted(targets, key=ge._target_sort_key))
        _greeks_worker_init(_GreeksWorkerContext(lake_root=lake, rate_source=_RATE))
        result = _greeks_worker(units[0])
        assert result.index == 0
        assert [o.kind for o in result.outcomes] == ["enriched"]


class TestParallelDriver:
    """`_run_parallel` + the dispatch — inline in-process executor."""

    @pytest.fixture
    def inline_pool(self, worker_env: pytest.MonkeyPatch) -> type[_InlinePool]:
        worker_env.setattr(ge, "ProcessPoolExecutor", _InlinePool)
        _InlinePool.last = None
        return _InlinePool

    def test_parallel_equals_sequential_bytes_counters_and_events(
        self, tmp_path: Path, inline_pool: type[_InlinePool]
    ) -> None:
        lake_seq = tmp_path / "seq"
        lake_par = tmp_path / "par"
        targets_seq = _build_two_underlying_lake(lake_seq)
        targets_par = _build_two_underlying_lake(lake_par)
        with capture_logs() as logs_seq:
            report_seq = enrich_greeks_for_targets(
                targets=targets_seq, lake_root=lake_seq, rate_source=_RATE, workers=1
            )
        with capture_logs() as logs_par:
            report_par = enrich_greeks_for_targets(
                targets=targets_par, lake_root=lake_par, rate_source=_RATE, workers=2
            )
        assert report_par == report_seq
        assert report_par.enriched == 3
        assert report_par.spot_reads == 2 and report_par.spot_hits == 1
        assert _lake_hashes(lake_par) == _lake_hashes(lake_seq)
        driver_events = {"greeks_enrich.target_enriched", "greeks_enrich.target_errored"}
        assert [e for e in logs_par if e["event"] in driver_events] == [
            e for e in logs_seq if e["event"] in driver_events
        ]
        assert [e["event"] for e in logs_par if e["event"] == "greeks_enrich.parallel_start"] == [
            "greeks_enrich.parallel_start"
        ]

    def test_raising_spot_read_keeps_counter_equality(
        self, tmp_path: Path, inline_pool: type[_InlinePool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A RAISING spot read increments ``reads`` and caches nothing (the
        memo contract) — the per-unit order is identical to sequential, so the
        counters still match parallel-vs-sequential."""
        real_spot = ge.read_spot_join_with_recovery

        def _flaky_spot(root: Path, target: GreeksEnrichTarget, parts: FnoSymbolParts) -> SpotJoin:
            if parts.underlying == "BANKNIFTY":
                raise OSError("spot partition unreadable")
            return real_spot(root, target, parts)

        monkeypatch.setattr(ge, "read_spot_join_with_recovery", _flaky_spot)
        lake_seq = tmp_path / "seq"
        lake_par = tmp_path / "par"
        targets_seq = _build_two_underlying_lake(lake_seq)
        targets_par = _build_two_underlying_lake(lake_par)
        report_seq = enrich_greeks_for_targets(
            targets=targets_seq, lake_root=lake_seq, rate_source=_RATE, workers=1
        )
        report_par = enrich_greeks_for_targets(
            targets=targets_par, lake_root=lake_par, rate_source=_RATE, workers=2
        )
        assert report_par == report_seq
        assert report_seq.errored == 1  # the BANKNIFTY target quarantined
        assert report_par.spot_reads == report_seq.spot_reads
        assert report_par.spot_hits == report_seq.spot_hits

    def test_parent_beats_before_dispatch_and_per_completed_unit(
        self, tmp_path: Path, inline_pool: type[_InlinePool]
    ) -> None:
        """DEC-PIPELINE-STALL-HEARTBEAT-001 anti-regression: real observed
        progress only — one beat pre-dispatch + one per completed unit."""
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)
        beats: list[int] = []
        register_progress_beat(lambda: beats.append(1))
        try:
            enrich_greeks_for_targets(targets=targets, lake_root=lake, rate_source=_RATE, workers=2)
        finally:
            unregister_progress_beat()
        assert len(beats) == 1 + 2  # pre-dispatch + 2 completed units

    def test_worker_halt_cancels_pending_and_reraises_lake_write_halt(
        self, tmp_path: Path, inline_pool: type[_InlinePool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)

        def _halt(_root: Path, _t: GreeksEnrichTarget, _b: Sequence[FnoGreeksBar]) -> int:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr(ge, "write_option_greeks", _halt)
        with pytest.raises(LakeWriteHalt) as excinfo:
            enrich_greeks_for_targets(targets=targets, lake_root=lake, rate_source=_RATE, workers=2)
        assert excinfo.value.reason == "disk_full"
        pool = inline_pool.last
        assert pool is not None
        assert (True, True) in pool.shutdown_calls  # cancel_futures=True fired

    def test_broken_pool_from_submit_raises_honest_pool_broken(
        self, tmp_path: Path, worker_env: pytest.MonkeyPatch
    ) -> None:
        class _BrokenOnSubmit(_InlinePool):
            def submit(self, fn: Callable[..., Any], /, *args: Any) -> Future[Any]:
                raise BrokenProcessPool("A process in the process pool was terminated abruptly")

        worker_env.setattr(ge, "ProcessPoolExecutor", _BrokenOnSubmit)
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)
        with capture_logs() as logs, pytest.raises(GreeksEnrichPoolBroken) as excinfo:
            enrich_greeks_for_targets(targets=targets, lake_root=lake, rate_source=_RATE, workers=2)
        # HONEST: OOM SIGKILL vs external kill vs interpreter crash is NOT
        # distinguishable through the public executor API — the message says so.
        assert "NOT distinguishable" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, BrokenProcessPool)
        broken = [e for e in logs if e["event"] == "greeks_enrich.pool_broken"]
        assert len(broken) == 1
        assert broken[0]["units_done"] == 0

    def test_broken_pool_from_result_raises_pool_broken(
        self, tmp_path: Path, worker_env: pytest.MonkeyPatch
    ) -> None:
        class _BrokenOnResult(_InlinePool):
            def submit(self, fn: Callable[..., Any], /, *args: Any) -> Future[Any]:
                future: Future[Any] = Future()
                future.set_exception(BrokenProcessPool("terminated abruptly"))
                return future

        worker_env.setattr(ge, "ProcessPoolExecutor", _BrokenOnResult)
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)
        with pytest.raises(GreeksEnrichPoolBroken):
            enrich_greeks_for_targets(targets=targets, lake_root=lake, rate_source=_RATE, workers=2)

    def test_run_parallel_caps_pool_size_at_unit_count(
        self, tmp_path: Path, inline_pool: type[_InlinePool]
    ) -> None:
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)
        units = _build_units(sorted(targets, key=ge._target_sort_key))
        _run_parallel(units, lake_root=lake, rate_source=_RATE, max_workers=8)
        pool = inline_pool.last
        assert pool is not None
        assert pool.max_workers == 2  # min(8, 2 units)


class TestDispatchGuards:
    """Every forced-sequential rail (requirement g) — the pool is NEVER built."""

    @pytest.fixture
    def no_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _forbidden(*_a: object, **_k: object) -> object:
            raise AssertionError("ProcessPoolExecutor must not be constructed")

        monkeypatch.setattr(ge, "ProcessPoolExecutor", _forbidden)

    def test_workers_param_one_forces_sequential(self, tmp_path: Path, no_pool: None) -> None:
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)
        report = enrich_greeks_for_targets(
            targets=targets, lake_root=lake, rate_source=_RATE, workers=1
        )
        assert report.enriched == 3

    def test_workers_param_zero_clamps_to_sequential(self, tmp_path: Path, no_pool: None) -> None:
        report = enrich_greeks_for_targets(
            targets=[_target(_NIFTY_CE), _target(_BANK_CE)],
            lake_root=tmp_path,
            rate_source=_RATE,
            workers=0,
        )
        assert report.no_bars == 2

    def test_env_kill_switch_forces_sequential(
        self, tmp_path: Path, no_pool: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GREEKS_WORKERS_ENV, "1")
        report = enrich_greeks_for_targets(
            targets=[_target(_NIFTY_CE), _target(_BANK_CE)],
            lake_root=tmp_path,
            rate_source=_RATE,
        )
        assert report.no_bars == 2

    def test_env_auto_dispatches_parallel(
        self, tmp_path: Path, worker_env: pytest.MonkeyPatch
    ) -> None:
        worker_env.setattr(ge, "ProcessPoolExecutor", _InlinePool)
        worker_env.setenv(GREEKS_WORKERS_ENV, "2")
        lake = tmp_path / "lake"
        targets = _build_two_underlying_lake(lake)
        report = enrich_greeks_for_targets(targets=targets, lake_root=lake, rate_source=_RATE)
        assert report.enriched == 3

    def test_any_custom_seam_forces_sequential(self, tmp_path: Path, no_pool: None) -> None:
        targets = [_target(_NIFTY_CE), _target(_BANK_CE)]
        for seams in (
            {"read_bars": lambda _t: []},
            {"read_spot": lambda _t, _p: SpotJoin({}, None)},
            {"write_bars": lambda _t, _b: 0},
        ):
            report = enrich_greeks_for_targets(
                targets=targets,
                lake_root=tmp_path,
                rate_source=_RATE,
                workers=2,
                **seams,  # type: ignore[arg-type]
            )
            assert report.no_bars + report.errored + report.already_current >= 1

    def test_single_unit_forces_sequential(self, tmp_path: Path, no_pool: None) -> None:
        lake = tmp_path / "lake"
        _build_two_underlying_lake(lake)
        report = enrich_greeks_for_targets(
            targets=[_target(_NIFTY_CE), _target(_NIFTY_PE)],  # ONE chain
            lake_root=lake,
            rate_source=_RATE,
            workers=2,
        )
        assert report.enriched == 2

    def test_unpicklable_rate_source_warns_and_runs_sequential(
        self, tmp_path: Path, no_pool: None
    ) -> None:
        class _LocalRate:
            """Local class — unpicklable across a spawn boundary."""

            def rate_for(self, _on: date) -> object:
                raise AssertionError("never called on a no-bars pass")

        with capture_logs() as logs:
            report = enrich_greeks_for_targets(
                targets=[_target(_NIFTY_CE), _target(_BANK_CE)],
                lake_root=tmp_path,
                rate_source=_LocalRate(),  # type: ignore[arg-type]
                workers=2,
            )
        assert report.no_bars == 2
        disabled = [e for e in logs if e["event"] == "greeks_enrich.parallel_disabled"]
        assert len(disabled) == 1
        assert disabled[0]["reason"] == "context_unpicklable"


class TestPicklableCrossings:
    """Everything that crosses the spawn boundary round-trips pickle."""

    @pytest.mark.parametrize(
        "value",
        [
            GreeksEnrichTarget(exchange="NSE", symbol=_NIFTY_CE, start=_WINDOW[0], end=_WINDOW[1]),
            EnrichStats(solved=1, nulls=2, reused=3, changed=4, skipped=5),
            _TargetOutcome(kind="enriched", symbol=_NIFTY_CE, start="2024-01-01", end="2024-01-26"),
            _GreeksUnit(index=0, targets=(_target(_NIFTY_CE),)),
            _GreeksUnitResult(index=0, outcomes=(), halt_reason=None, spot_reads=1, spot_hits=2),
            FixedRateSource(),
            CsvRateSource(dates=(date(2024, 1, 1),), rates=(0.065,), source="t-bill.csv"),
        ],
    )
    def test_roundtrip(self, value: object) -> None:
        import pickle

        copy = pickle.loads(pickle.dumps(value))  # noqa: S301 — self-produced bytes
        assert copy == value

    def test_worker_context_roundtrips(self, tmp_path: Path) -> None:
        import pickle

        ctx = _GreeksWorkerContext(lake_root=tmp_path, rate_source=_RATE)
        copy = pickle.loads(pickle.dumps(ctx))  # noqa: S301 — self-produced bytes
        assert copy.lake_root == tmp_path
        assert copy.rate_source == _RATE
