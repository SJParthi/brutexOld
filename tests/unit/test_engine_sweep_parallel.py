"""Unit + integration + golden tests for the multi-core sweep driver.

`brutex.engine.sweep_parallel` (DEC-MULTICORE-SWEEP-PHASE1-001) — the FIRST safe,
tested slice of multi-core grid-sweep parallelism, behind a DEFAULT-OFF flag.

The 7-test-types discipline (CLAUDE.md §12 / Golden Rule #12), held to the strict
100% line + 100% branch gate:

* **unit** — :func:`mod.resolve_sweep_workers` (every branch),
  :func:`mod.refuse_parallel_on_legacy_path`, the ``workers < 2`` guard, the
  ``BufferedAuditCollector`` seam, ``mod._run_tuple_in_worker`` (success / no-bars /
  ``SweepComboError`` → failed marker), ``mod._result_to_outcome`` (all three kinds),
  ``mod._Outcome`` heap ordering, ``mod._NoDedup``'s raising probes, and ``mod._worker_init``'s
  determinism pins.
* **integration** — a real end-to-end ``ProcessPoolExecutor`` sweep over real bars
  / real ``AuditWriter`` / ``SweepAuditWriter`` / ``ResultsStore`` / ``SweepCache``
  / ``SweepCheckpointStore``, incl. resume + cache + no-bars skips.
* **golden (THE HEADLINE)** — a fixed grid run BOTH sequential (``workers=1`` via
  ``run_supervised_grid``) AND parallel (``workers=4`` via ``mod.run_parallel_grid``);
  every per-tuple Parquet + sidecar + sentinel is BYTE-IDENTICAL, the ingest audit
  chain is identical modulo timestamps AND verifies, and the checkpoint state is
  identical — regardless of worker interleaving.
* **failure** — crash-mid-parallel resume (redo-not-skip), failed-tuple
  contiguous-prefix checkpoint freeze, worker-crash → global stop, and the stall
  watchdog raising rather than hanging.

Bars are small, deterministic, tz-aware synthetic sessions with ``Decimal`` OHLC
(Golden Rule #1). Real wheel-backed objects throughout (no monkeypatched runner),
mirroring ``tests/unit/test_engine_sweep_supervisor.py``.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
from collections.abc import Callable, Generator, Iterable, Sequence
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import brutex.engine.sweep_parallel as mod
import pytest
import structlog
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.engine.sweep_audit import read_entries as read_sup_entries
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    count_sweep_tuples,
    iter_sweep_tuples,
    make_store_for,
    tuple_store_path,
)
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_run import ComboDedup
from brutex.engine.sweep_supervisor import (
    SweepGuards,
    grid_identity,
    run_supervised_grid,
    tuple_label,
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.observability.logging import LOG_LEVEL_ENV, reset_configuration
from brutex.orchestrator.audit import (
    AuditWriter,
    BufferedAuditCollector,
    BufferedAuditRecord,
)
from brutex.orchestrator.audit import read_entries as read_ingest_entries
from brutex.orchestrator.audit import verify_chain as verify_ingest_chain
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Generator[None, None, None]:
    """``_worker_init`` now binds process logging (DEC-SWEEP-LOG-VOLUME-GUARD-001
    R4 fold) — in-process ``_worker_init`` calls configure GLOBAL structlog
    state, so reset after each test to keep the suite leak-free."""
    yield
    reset_configuration()


# ────────────────────────── shared fixtures ──────────────────────────


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """Build one IST-session minute bar (stored UTC) with Decimal OHLC."""
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 4, per_day: int = 60) -> list[CandleBar]:
    """A multi-day upward-drifting session — BUY combos win via the TP leg."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


_BARS = _trending_bars()


def _bars_for(_instrument: str, _tf: str) -> list[CandleBar]:
    return _BARS


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY, Side.SELL],
        "k_values": [1, 2],
        "condition_bits": [0, 1, 2, 3, 4],
        "stop_losses": [80],
        "take_profits": [50],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


@contextmanager
def _guards(tmp_path: Path, grid: SweepGridConfig) -> Generator[SweepGuards, None, None]:
    """Open the five real G&A objects for one run (fresh per call)."""
    audit = SweepAuditWriter.open(tmp_path / "sup.jsonl")
    cache = SweepCache.open(tmp_path / "cache", bloom_capacity=10_000)
    checkpoint = SweepCheckpointStore(
        path=tmp_path / "ckpt.json",
        sweep_id=grid_identity(grid),
        total_combos=count_sweep_tuples(grid),
    )
    guards = SweepGuards(
        audit=audit,
        progress=SweepProgress(),
        checkpoint=checkpoint,
        cache=cache,
        watchdog=SweepWatchdog(),
    )
    try:
        yield guards
    finally:
        audit.close()
        cache.close()


def _tree(root: Path) -> dict[str, str]:
    """SHA-256 of every file under *root*, keyed by its relative path."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _ingest_lines(path: Path) -> list[tuple[int, str, str, dict[str, Any]]]:
    """The ingest audit chain reduced to (seq, action, subject, payload) — ts-masked."""
    return [(e.seq, e.action, e.subject, e.payload) for e in read_ingest_entries(path)]


def _run_sequential(tmp: Path, grid: SweepGridConfig) -> tuple[Path, Path, int]:
    """Run the sequential supervised grid; return (results_root, ingest_audit, ckpt)."""
    out = tmp / "out"
    with (
        AuditWriter.open(tmp / "ingest.jsonl") as ingest,
        _guards(tmp, grid) as guards,
    ):
        run_supervised_grid(
            grid,
            bars_for=_bars_for,
            store_for=make_store_for(out, grid),
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            ingest_audit=ingest,
            guards=guards,
        )
        ckpt = guards.checkpoint.next_index
    return out, tmp / "ingest.jsonl", ckpt


def _run_parallel(tmp: Path, grid: SweepGridConfig, workers: int = 4) -> tuple[Path, Path, int]:
    """Run the parallel grid; return (results_root, ingest_audit, ckpt)."""
    out = tmp / "out"
    with (
        AuditWriter.open(tmp / "ingest.jsonl") as ingest,
        _guards(tmp, grid) as guards,
    ):
        mod.run_parallel_grid(
            grid,
            bars_for=_bars_for,
            root=out,
            ingest_audit=ingest,
            guards=guards,
            workers=workers,
        )
        ckpt = guards.checkpoint.next_index
    return out, tmp / "ingest.jsonl", ckpt


# ────────────────────────── flag resolution ──────────────────────────
# ADR-0016 (DEC-MULTICORE-SWEEP-AUTODEFAULT-001): unset env → AUTO =
# max(1, min(cpu_count - 2, 8)); explicit env beats auto (=1 stays the
# sequential opt-out); INVALID env → auto (the conscious ADR-owned reversal,
# logged LOUDLY); a small grid (tuple_count < 2 × auto) stays sequential.


class TestAutoSweepWorkers:
    @pytest.mark.parametrize(
        ("cpu", "expected"),
        [(1, 1), (2, 1), (4, 2), (10, 8), (14, 8), (None, 1)],
    )
    def test_auto_formula(self, cpu: int | None, expected: int) -> None:
        # max(1, min(cpu - 2, 8)); None (os.cpu_count may return None) → 1.
        assert mod.auto_sweep_workers(cpu) == expected


class TestSmallGridFactor:
    """The env-tunable small-grid demotion factor (ADR-0016 §5, review MINOR-1)."""

    def test_unset_is_the_default(self) -> None:
        assert mod.small_grid_factor({}) == mod.SMALL_GRID_FACTOR == 2

    def test_blank_is_the_default(self) -> None:
        assert mod.small_grid_factor({mod.SMALL_GRID_FACTOR_ENV: "   "}) == 2

    def test_valid_override_is_verbatim(self) -> None:
        assert mod.small_grid_factor({mod.SMALL_GRID_FACTOR_ENV: "5"}) == 5

    def test_zero_is_valid_and_disables(self) -> None:
        assert mod.small_grid_factor({mod.SMALL_GRID_FACTOR_ENV: "0"}) == 0

    @pytest.mark.parametrize("raw", ["two", "-1", "2.5"])
    def test_invalid_falls_back_to_default_and_warns(self, raw: str) -> None:
        with capture_logs() as logs:
            factor = mod.small_grid_factor({mod.SMALL_GRID_FACTOR_ENV: raw})
        assert factor == mod.SMALL_GRID_FACTOR
        (event,) = logs
        assert event["event"] == "sweep.small_grid_factor.invalid"
        assert event["log_level"] == "warning"
        assert event["requested_env"] == repr(raw)
        assert event["fallback"] == mod.SMALL_GRID_FACTOR

    def test_reads_from_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(mod.SMALL_GRID_FACTOR_ENV, "3")
        assert mod.small_grid_factor() == 3


class TestResolveSweepWorkers:
    @pytest.mark.parametrize(
        ("cpu", "expected"),
        [(1, 1), (2, 1), (4, 2), (10, 8), (14, 8), (None, 1)],
    )
    def test_unset_env_resolves_to_auto(self, cpu: int | None, expected: int) -> None:
        assert mod.resolve_sweep_workers({}, cpu_count_fn=lambda: cpu) == expected

    def test_unset_env_reason_is_auto(self) -> None:
        with capture_logs() as logs:
            mod.resolve_sweep_workers({}, cpu_count_fn=lambda: 14)
        (event,) = logs
        assert event["event"] == "sweep.workers.resolved"
        assert event["log_level"] == "info"
        assert event["reason"] == "auto"
        assert event["requested_env"] == repr(None)
        assert event["env_invalid"] is False
        assert event["cpu_count"] == 14
        assert event["auto"] == 8
        assert event["tuple_count"] is None
        assert event["resolved"] == 8

    def test_blank_env_is_unset_not_invalid(self) -> None:
        # A blank value is treated as UNSET (auto, INFO) — not an invalid value.
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers(
                {mod.SWEEP_WORKERS_ENV: "   "}, cpu_count_fn=lambda: 14
            )
        assert resolved == 8
        (event,) = logs
        assert event["log_level"] == "info"
        assert event["reason"] == "auto"

    def test_cpu_count_none_resolves_sequential_with_reason(self) -> None:
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers({}, cpu_count_fn=lambda: None)
        assert resolved == 1
        (event,) = logs
        assert event["reason"] == "cpu_count_none"
        assert event["cpu_count"] is None
        assert event["auto"] == 1

    def test_explicit_env_beats_auto(self) -> None:
        # env=6 on a 14-core box (auto=8) → 6, verbatim.
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers(
                {mod.SWEEP_WORKERS_ENV: "6"}, cpu_count_fn=lambda: 14
            )
        assert resolved == 6
        (event,) = logs
        assert event["reason"] == "env_explicit"
        assert event["requested_env"] == repr("6")

    def test_explicit_env_above_auto_is_honored(self) -> None:
        # The override is verbatim even ABOVE the auto cap (operator's call).
        assert (
            mod.resolve_sweep_workers({mod.SWEEP_WORKERS_ENV: "12"}, cpu_count_fn=lambda: 4) == 12
        )

    def test_explicit_one_is_the_sequential_opt_out(self) -> None:
        # =1 stays 1 regardless of cores or grid size — the opt-out survives.
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers(
                {mod.SWEEP_WORKERS_ENV: "1"}, tuple_count=1_000_000, cpu_count_fn=lambda: 14
            )
        assert resolved == 1
        (event,) = logs
        assert event["reason"] == "env_explicit"

    @pytest.mark.parametrize("raw", ["four", "0", "-3", "2.5"])
    def test_invalid_env_resolves_to_auto_and_logs_loudly(self, raw: str) -> None:
        # The ADR-0016 §4 CONSCIOUS REVERSAL of "a bad value can never silently
        # enable parallelism": invalid → AUTO, pinned LOUD (WARNING, value named).
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers(
                {mod.SWEEP_WORKERS_ENV: raw}, cpu_count_fn=lambda: 14
            )
        assert resolved == 8
        (event,) = logs
        assert event["event"] == "sweep.workers.resolved"
        assert event["log_level"] == "warning"
        assert event["reason"] == "env_invalid_auto"
        assert event["requested_env"] == repr(raw)
        assert event["env_invalid"] is True
        assert event["resolved"] == 8

    def test_small_grid_boundary_below_resolves_sequential(self) -> None:
        # cpu=14 → auto=8; tuple_count == 2*auto - 1 == 15 → 1 (sequential).
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers({}, tuple_count=15, cpu_count_fn=lambda: 14)
        assert resolved == 1
        (event,) = logs
        assert event["reason"] == "small_grid_sequential"
        assert event["tuple_count"] == 15

    def test_small_grid_boundary_at_resolves_auto(self) -> None:
        # tuple_count == 2*auto == 16 → auto (the boundary is strict `<`).
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers({}, tuple_count=16, cpu_count_fn=lambda: 14)
        assert resolved == 8
        (event,) = logs
        assert event["reason"] == "auto"

    def test_factor_env_moves_the_small_grid_boundary(self) -> None:
        # BRUTEX_SWEEP_SMALL_GRID_FACTOR=1 on cpu=14 (auto=8): the strict
        # boundary moves to `< 1*auto` — 7 → sequential, 8 → auto.
        env = {mod.SMALL_GRID_FACTOR_ENV: "1"}
        assert mod.resolve_sweep_workers(env, tuple_count=7, cpu_count_fn=lambda: 14) == 1
        assert mod.resolve_sweep_workers(env, tuple_count=8, cpu_count_fn=lambda: 14) == 8

    def test_factor_env_zero_disables_the_demotion(self) -> None:
        # factor=0 → `tuple_count < 0` never true — even a 1-tuple grid goes auto
        # (the operator lever for a few-but-huge-tuple grid, ADR-0016 §5).
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers(
                {mod.SMALL_GRID_FACTOR_ENV: "0"}, tuple_count=1, cpu_count_fn=lambda: 14
            )
        assert resolved == 8
        (event,) = logs
        assert event["reason"] == "auto"

    def test_invalid_factor_env_keeps_default_and_warns(self) -> None:
        # An invalid factor value falls back to the default 2× rule (tiny grid
        # still demoted) and emits its OWN warning alongside the resolved event.
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers(
                {mod.SMALL_GRID_FACTOR_ENV: "huge"}, tuple_count=3, cpu_count_fn=lambda: 14
            )
        assert resolved == 1
        factor_event, resolved_event = logs
        assert factor_event["event"] == "sweep.small_grid_factor.invalid"
        assert factor_event["log_level"] == "warning"
        assert resolved_event["event"] == "sweep.workers.resolved"
        assert resolved_event["reason"] == "small_grid_sequential"

    def test_small_grid_never_tagged_when_auto_is_one(self) -> None:
        # auto==1 already IS sequential — the small-grid demotion is a no-op and
        # must not masquerade as the reason (cpu=2 → auto=1; tuple_count=1 < 2).
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers({}, tuple_count=1, cpu_count_fn=lambda: 2)
        assert resolved == 1
        (event,) = logs
        assert event["reason"] == "auto"

    def test_small_grid_never_demotes_explicit_env(self) -> None:
        # An explicit override beats the small-grid fallback (operator's call).
        assert (
            mod.resolve_sweep_workers(
                {mod.SWEEP_WORKERS_ENV: "4"}, tuple_count=1, cpu_count_fn=lambda: 14
            )
            == 4
        )

    def test_invalid_env_with_small_grid_stays_sequential_and_loud(self) -> None:
        # Invalid env lands on the AUTO path → the small-grid fallback still
        # applies; the invalid value stays LOUD (WARNING) even so.
        with capture_logs() as logs:
            resolved = mod.resolve_sweep_workers(
                {mod.SWEEP_WORKERS_ENV: "banana"}, tuple_count=3, cpu_count_fn=lambda: 14
            )
        assert resolved == 1
        (event,) = logs
        assert event["log_level"] == "warning"
        assert event["reason"] == "small_grid_sequential"
        assert event["requested_env"] == repr("banana")
        # NIT-1: the invalid-env cause stays FILTERABLE even when the
        # small-grid fallback takes the `reason` slot.
        assert event["env_invalid"] is True

    def test_reads_from_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(mod.SWEEP_WORKERS_ENV, "5")
        assert mod.resolve_sweep_workers(cpu_count_fn=lambda: 14) == 5

    def test_default_probe_is_os_cpu_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # cpu_count_fn=None → os.cpu_count, resolved at CALL time (a monkeypatch
        # of os.cpu_count is honored — the injectable seam's default contract).
        monkeypatch.delenv(mod.SWEEP_WORKERS_ENV, raising=False)
        monkeypatch.setattr("os.cpu_count", lambda: 10)
        assert mod.resolve_sweep_workers() == mod.auto_sweep_workers(10) == 8


class TestAutoDefaultProof:
    """The ADR-0016 auto-default + the preserved sequential reference.

    :class:`TestSequentialEqualsParallelGolden` (below, UNMODIFIED by the
    auto-default flip) proves workers=1 sequential == workers=4 parallel
    byte-for-byte — the correctness net the auto-default rests on: whichever
    count the resolver picks, the result tree is the same bytes.
    """

    def test_unset_env_is_multicore_on_a_big_box(self) -> None:
        # The #1080 deferred default-raise, now live: unset env on a 14-core
        # box with a big grid → 8 workers (was 1 before ADR-0016).
        assert mod.resolve_sweep_workers({}, tuple_count=100, cpu_count_fn=lambda: 14) == 8

    def test_sequential_opt_out_constant(self) -> None:
        assert mod.SEQUENTIAL_SWEEP_WORKERS == 1

    def test_sequential_result_is_the_reference(self, tmp_path: Path) -> None:
        # A workers=1 run IS the sequential path; the golden test proves the
        # parallel path reproduces it exactly. Here we pin that the sequential
        # reference actually produces output (a non-empty result-set to compare).
        grid = _grid()
        seq_root, seq_audit, seq_ckpt = _run_sequential(tmp_path, grid)
        assert len(_tree(seq_root)) > 0
        assert verify_ingest_chain(seq_audit) >= 1
        assert seq_ckpt == count_sweep_tuples(grid)


# ─────────────────────── legacy-path hard-refuse ──────────────────────


class TestRefuseParallelOnLegacyPath:
    def test_workers_one_never_refused(self) -> None:
        # workers==1 is always fine on either path (sequential).
        mod.refuse_parallel_on_legacy_path(1, digest_path=False)
        mod.refuse_parallel_on_legacy_path(1, digest_path=True)

    def test_workers_gt1_on_digest_path_allowed(self) -> None:
        mod.refuse_parallel_on_legacy_path(4, digest_path=True)

    def test_workers_gt1_on_legacy_path_refused(self) -> None:
        with pytest.raises(mod.SweepParallelUsageError, match="LEGACY no-digest"):
            mod.refuse_parallel_on_legacy_path(4, digest_path=False)

    def test_driver_refuses_workers_below_two(self, tmp_path: Path) -> None:
        grid = _grid()
        with (
            _guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "i.jsonl") as ing,
            pytest.raises(mod.SweepParallelUsageError, match="workers >= 2"),
        ):
            mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ing,
                guards=guards,
                workers=1,
            )


# ───────────────────── BufferedAuditCollector seam ────────────────────


class TestBufferedAuditCollector:
    def test_collects_records_in_order(self) -> None:
        c = BufferedAuditCollector()
        c.append(action="A", subject="s1", payload={"x": 1})
        c.append(action="B", subject="s2")
        recs = c.records
        assert [r.action for r in recs] == ["A", "B"]
        assert recs[0].payload == {"x": 1}
        # A None payload becomes an empty dict (never None on the record).
        assert recs[1].payload == {}

    def test_append_returns_the_record(self) -> None:
        c = BufferedAuditCollector()
        rec = c.append(action="A", subject="s", payload={"k": "v"}, ts_utc=123.0)
        assert isinstance(rec, BufferedAuditRecord)
        assert rec.action == "A"
        # ts_utc is accepted for parity but never stored on the record.
        assert not hasattr(rec, "ts_utc")

    def test_records_is_an_immutable_snapshot(self) -> None:
        c = BufferedAuditCollector()
        c.append(action="A", subject="s")
        snap = c.records
        c.append(action="B", subject="s")
        assert len(snap) == 1  # the earlier snapshot did not grow
        assert len(c.records) == 2


# ─────────────────────────── worker helpers ───────────────────────────


class TestNoDedup:
    def test_seen_raises(self) -> None:
        with pytest.raises(mod.SweepParallelUsageError, match="dedup.seen"):
            mod._NoDedup().seen("deadbeef")

    def test_mark_many_raises(self) -> None:
        with pytest.raises(mod.SweepParallelUsageError, match="dedup.mark_many"):
            mod._NoDedup().mark_many(["deadbeef"])


class TestWorkerInit:
    def test_pins_single_threaded_numerics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("OMP_NUM_THREADS", "RAYON_NUM_THREADS", "POLARS_MAX_THREADS"):
            monkeypatch.delenv(var, raising=False)
        ctx = mod._WorkerContext(grid=_grid(), root=Path("/tmp/x"))
        mod._worker_init(ctx)
        import os as _os

        assert _os.environ["OMP_NUM_THREADS"] == "1"
        assert _os.environ["RAYON_NUM_THREADS"] == "1"
        assert _os.environ["POLARS_MAX_THREADS"] == "1"

    def test_stashes_context(self) -> None:
        ctx = mod._WorkerContext(grid=_grid(), root=Path("/tmp/y"))
        mod._worker_init(ctx)
        assert mod._WORKER_CTX is ctx

    def test_binds_worker_logging(self, capsys: pytest.CaptureFixture[str]) -> None:
        """R4 fold: the initializer bounds the worker's OWN structlog sink.

        A spawn worker starts unconfigured (the parent's config is not
        inherited) — after ``_worker_init`` the process is configured and a
        per-trade-shaped DEBUG event is suppressed at the INFO default.
        """
        reset_configuration()
        import os as _os

        _os.environ.pop(LOG_LEVEL_ENV, None)
        assert not structlog.is_configured()
        mod._worker_init(mod._WorkerContext(grid=_grid(), root=Path("/tmp/z")))
        assert structlog.is_configured()
        structlog.get_logger("brutex.engine.trade_sim").debug("trade_sim.entry", idx=1)
        assert "trade_sim.entry" not in capsys.readouterr().err

    def test_logging_failure_never_breaks_the_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Best-effort contract: a raising logging config cannot kill the worker."""
        reset_configuration()

        def _boom(**_kw: object) -> None:
            raise RuntimeError("logging config blew up")

        monkeypatch.setattr("brutex.observability.logging.configure_logging", _boom)
        ctx = mod._WorkerContext(grid=_grid(), root=Path("/tmp/boom"))
        mod._worker_init(ctx)  # must NOT raise
        assert mod._WORKER_CTX is ctx


class TestWorkerLogVolumeGuardSpawn:
    """R4 regression net: REAL spawn workers through ``run_parallel_grid``.

    The parent guard alone cannot cover the parallel path — spawn workers
    start from a clean interpreter and the per-trade ``trade_sim.entry`` /
    ``trade_sim.exit`` DEBUG events fire INSIDE the workers onto the
    inherited stderr fd. ``capfd`` captures at the fd level, so the child
    processes' actual output bytes are asserted — never a mock. The DEBUG-env
    twin proves the assertion is non-vacuous (the same grid DOES emit
    per-trade events when the operator opts in).
    """

    def test_spawn_worker_per_trade_debug_suppressed_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
        _run_parallel(tmp_path, _grid(), workers=2)
        captured = capfd.readouterr()
        assert "trade_sim." not in captured.err
        assert "trade_sim." not in captured.out

    def test_spawn_worker_per_trade_debug_restored_with_debug_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")
        _run_parallel(tmp_path, _grid(), workers=2)
        captured = capfd.readouterr()
        combined = captured.err + captured.out
        assert "trade_sim.entry" in combined
        assert "trade_sim.exit" in combined


class TestRunTupleInWorker:
    def test_success_returns_summary(self, tmp_path: Path) -> None:
        grid = _grid()
        mod._worker_init(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
        tuple_ = next(iter_sweep_tuples(grid))
        result = mod._run_tuple_in_worker(0, tuple_, _BARS)
        assert result.index == 0
        assert result.failed_error is None
        assert result.summary is not None
        # The audit collector captured the SWEEP_START / SWEEP_FLUSH / SWEEP_COMPLETE lines.
        assert len(result.audit_records) == 3
        assert result.audit_records[0].action == "SWEEP_START"

    def test_no_bars_returns_none_summary(self, tmp_path: Path) -> None:
        grid = _grid()
        mod._worker_init(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
        tuple_ = next(iter_sweep_tuples(grid))
        result = mod._run_tuple_in_worker(2, tuple_, [])
        assert result.index == 2
        assert result.summary is None
        assert result.failed_error is None

    def test_combo_error_returns_failed_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine import sweep_run as sr

        def _boom(*_a: object, **_k: object) -> None:
            raise ValueError("combo blew up")

        # The wired per-mask METRICS TAIL (DEC-SWEEP-RUST-TRADESIM-WIRE-001) —
        # raising here produces the real fingerprinted SweepComboError exactly
        # as the pre-wiring per-mask metrics_for_mask seam did.
        monkeypatch.setattr(sr, "metrics_from_trades", _boom)
        grid = _grid()
        mod._worker_init(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
        tuple_ = next(iter_sweep_tuples(grid))
        result = mod._run_tuple_in_worker(1, tuple_, _BARS)
        assert result.summary is None
        assert result.failed_error is not None
        assert "combo blew up" in result.failed_error
        # Even on failure the collected pre-failure audit lines are returned.
        assert result.audit_records[0].action == "SWEEP_START"


class TestOutcomeAndResultMapping:
    def test_outcome_heap_orders_by_index(self) -> None:
        import heapq

        grid = _grid()
        t = next(iter_sweep_tuples(grid))
        heap: list[mod._Outcome] = []
        for i in (3, 1, 2, 0):
            heapq.heappush(heap, mod._skip_outcome(i, t, "resume"))
        assert [heapq.heappop(heap).index for _ in range(4)] == [0, 1, 2, 3]

    def test_result_to_outcome_done(self) -> None:
        grid = _grid()
        t = next(iter_sweep_tuples(grid))
        mod._worker_init(mod._WorkerContext(grid=grid, root=Path("/tmp/z")))
        # A real summary via the worker.
        res = mod._run_tuple_in_worker(0, t, _BARS)
        outcome = mod._result_to_outcome(res, t)
        assert outcome.kind == "done"
        assert outcome.summary is not None

    def test_result_to_outcome_no_bars(self) -> None:
        grid = _grid()
        t = next(iter_sweep_tuples(grid))
        res = mod.TupleWorkResult(index=5, summary=None, failed_error=None, audit_records=())
        assert mod._result_to_outcome(res, t).kind == "no_bars"

    def test_result_to_outcome_failed(self) -> None:
        grid = _grid()
        t = next(iter_sweep_tuples(grid))
        res = mod.TupleWorkResult(index=5, summary=None, failed_error="boom", audit_records=())
        assert mod._result_to_outcome(res, t).kind == "failed"


class TestAccumulator:
    def test_defaults(self) -> None:
        acc = mod._Accumulator()
        assert acc.tuples_run == 0
        assert acc.had_failure is False


# ════════════════════ THE HEADLINE GOLDEN TEST ════════════════════════
# Sequential (workers=1) and parallel (workers=4) produce a BIT-FOR-BIT
# identical result set + ingest audit chain + checkpoint — regardless of
# worker interleaving. This is the whole point of the PR.
# Extended (PR-4): the cross-worker-count pin below additionally chains
# workers ∈ {3, 10, 12} to the workers=4 reference, so byte-identity holds
# at pool sizes configured ABOVE the AUTO cap too. HONEST scope: with the
# 4-tuple _grid() the lazy ProcessPoolExecutor materialises at most ~4 EVAL
# processes, so these rows pin invariance to the configured pool-size
# PARAMETER — genuine 11-process contention is exercised by the 24-tuple
# bench driver evidence (DEC-SWEEP-CAP-BENCH-001), not by this in-CI golden.


class TestSequentialEqualsParallelGolden:
    def test_parquet_trees_are_byte_identical(self, tmp_path: Path) -> None:
        grid = _grid()
        seq_root, _seq_audit, seq_ckpt = _run_sequential(tmp_path / "seq", grid)
        par_root, _par_audit, par_ckpt = _run_parallel(tmp_path / "par", grid, workers=4)

        seq_tree = _tree(seq_root)
        par_tree = _tree(par_root)
        # Every per-tuple Parquet + trial-stats sidecar + completion sentinel is
        # byte-for-byte identical (same content_hash → same bytes).
        assert seq_tree == par_tree
        assert len(seq_tree) > 0  # the grid actually produced files
        # The checkpoint advanced to the same high-water mark.
        assert seq_ckpt == par_ckpt

    def test_ingest_audit_chain_is_identical_modulo_ts_and_verifies(self, tmp_path: Path) -> None:
        grid = _grid()
        _seq_root, seq_audit, _ = _run_sequential(tmp_path / "seq", grid)
        _par_root, par_audit, _ = _run_parallel(tmp_path / "par", grid, workers=4)

        # Both hash chains verify (Golden Rule #8).
        assert verify_ingest_chain(seq_audit) >= 1
        assert verify_ingest_chain(par_audit) >= 1
        # And the (seq, action, subject, payload) sequence is byte-identical —
        # the parallel writer-actor replayed the workers' buffered records in
        # canonical enumeration order.
        assert _ingest_lines(seq_audit) == _ingest_lines(par_audit)

    def test_supervisor_audit_tuple_lines_match_modulo_ts(self, tmp_path: Path) -> None:
        grid = _grid()
        _run_sequential(tmp_path / "seq", grid)
        _run_parallel(tmp_path / "par", grid, workers=4)
        # Both supervisor chains verify; the TUPLE_DONE lines carry the same
        # per-tuple store_content_hash payloads in the same enumeration order.
        seq_sup = [
            (e.action, e.payload.get("tuple"), e.payload.get("store_content_hash"))
            for e in read_sup_entries(tmp_path / "seq" / "sup.jsonl")
            if e.action == "TUPLE_DONE"
        ]
        par_sup = [
            (e.action, e.payload.get("tuple"), e.payload.get("store_content_hash"))
            for e in read_sup_entries(tmp_path / "par" / "sup.jsonl")
            if e.action == "TUPLE_DONE"
        ]
        assert seq_sup == par_sup
        assert len(seq_sup) > 0

    @pytest.mark.parametrize(("workers_a", "workers_b"), [(4, 3), (10, 4), (12, 10)])
    def test_stable_across_repeated_parallel_runs(
        self, tmp_path: Path, workers_a: int, workers_b: int
    ) -> None:
        """Non-determinism stress (short): two parallel runs are byte-identical.

        The pair rows chain every pinned worker count to the ``workers=4``
        reference (itself pinned bit-identical to sequential by
        ``test_parquet_trees_are_byte_identical``), so sequential == 3 == 4 ==
        10 == 12 holds transitively.  The 10/12 rows (PR-4, ADR-0016 §5
        pre-cap-raise pin) configure pool sizes that EXCEED the usable cores
        on shared CI runners; the ``_worker_init`` OMP/RAYON/POLARS=1 pins +
        the writer-actor's index-ordered reorder heap make the result
        independent of scheduler interleaving, which is exactly the property
        any future ``AUTO_SWEEP_WORKERS_CAP`` raise depends on.  HONEST
        scope: the 4-tuple ``_grid()`` means the lazy pool materialises at
        most ~4 EVAL processes, so this pins invariance to the configured
        pool-size PARAMETER — the genuine 11-process-contention digest
        evidence lives in the bench measurement (DEC-SWEEP-CAP-BENCH-001).
        """
        grid = _grid()
        root_a, audit_a, ck_a = _run_parallel(tmp_path / "a", grid, workers=workers_a)
        root_b, audit_b, ck_b = _run_parallel(tmp_path / "b", grid, workers=workers_b)
        assert _tree(root_a) == _tree(root_b)
        assert _ingest_lines(audit_a) == _ingest_lines(audit_b)
        assert ck_a == ck_b


# ─────────────────── integration: skip paths ──────────────────────────


class TestParallelSkipPaths:
    def test_no_bars_tuples_are_skipped(self, tmp_path: Path) -> None:
        grid = _grid()

        def _no_bars(_i: str, _tf: str) -> list[CandleBar]:
            return []

        out = tmp_path / "out"
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            summary = mod.run_parallel_grid(
                grid,
                bars_for=_no_bars,
                root=out,
                ingest_audit=ing,
                guards=guards,
                workers=3,
            )
        assert summary.tuples_run == 0
        assert summary.tuples_skipped_no_bars == count_sweep_tuples(grid)
        # No-bars tuples DO advance the checkpoint (they are clean) — every index
        # 0..total-1 is recorded, so next_index == total.
        assert guards.checkpoint.next_index == count_sweep_tuples(grid)

    def test_cache_skip_short_circuits_a_completed_tuple(self, tmp_path: Path) -> None:
        grid = _grid()
        # First run populates the cache + results.
        _run_parallel(tmp_path / "first", grid, workers=3)
        # A second run against a SHARED cache dir short-circuits every tuple.
        out2 = tmp_path / "second-out"
        cache_dir = tmp_path / "first" / "cache"
        with (
            AuditWriter.open(tmp_path / "i2.jsonl") as ing,
            SweepAuditWriter.open(tmp_path / "s2.jsonl") as sup,
            SweepCache.open(cache_dir, bloom_capacity=10_000) as cache,
        ):
            ckpt = SweepCheckpointStore(
                path=tmp_path / "ck2.json",
                sweep_id=grid_identity(grid),
                total_combos=count_sweep_tuples(grid),
            )
            guards = SweepGuards(
                audit=sup,
                progress=SweepProgress(),
                checkpoint=ckpt,
                cache=cache,
                watchdog=SweepWatchdog(),
            )
            summary = mod.run_parallel_grid(
                grid, bars_for=_bars_for, root=out2, ingest_audit=ing, guards=guards, workers=3
            )
        assert summary.skipped_cache == count_sweep_tuples(grid)
        assert summary.tuples_run == 0

    def test_resume_skips_already_checkpointed_tuples(self, tmp_path: Path) -> None:
        grid = _grid()
        total = count_sweep_tuples(grid)
        out = tmp_path / "out"
        # Pre-seed a checkpoint at index 1 (so 0 and 1 are "resumed").
        pre = SweepCheckpointStore(
            path=tmp_path / "ckpt.json",
            sweep_id=grid_identity(grid),
            total_combos=total,
        )
        pre.record(0)
        pre.record(1)
        with (
            AuditWriter.open(tmp_path / "i.jsonl") as ing,
            SweepAuditWriter.open(tmp_path / "s.jsonl") as sup,
            SweepCache.open(tmp_path / "cache", bloom_capacity=10_000) as cache,
        ):
            ckpt = SweepCheckpointStore(
                path=tmp_path / "ckpt.json", sweep_id=grid_identity(grid), total_combos=total
            )
            guards = SweepGuards(
                audit=sup,
                progress=SweepProgress(),
                checkpoint=ckpt,
                cache=cache,
                watchdog=SweepWatchdog(),
            )
            summary = mod.run_parallel_grid(
                grid, bars_for=_bars_for, root=out, ingest_audit=ing, guards=guards, workers=3
            )
        assert summary.skipped_resume == 2
        assert summary.tuples_run == total - 2


# ─────────────── failure paths via a deterministic FAKE pool ───────────
# The reorder / checkpoint-freeze / stall logic is exercised deterministically
# by driving the driver's internals with pre-resolved `Future`s (no real
# multiprocessing → no flakiness), plus a real-pool crash test below.


class _FakePool:
    """A ``pool.submit``-compatible fake that runs the worker body IN-PROCESS.

    Each submitted tuple's :class:`mod.TupleWorkResult` is either produced by the
    real in-process worker body OR overridden by an injected ``result_for`` /
    ``never_resolves`` map keyed on enumeration index — so a test can make one
    index fail, or wedge forever (for the stall watchdog).
    """

    def __init__(
        self,
        ctx: mod._WorkerContext,
        *,
        result_for: dict[int, mod.TupleWorkResult] | None = None,
        never_resolve: set[int] | None = None,
    ) -> None:
        mod._worker_init(ctx)
        self._result_for = result_for or {}
        self._never_resolve = never_resolve or set()

    def submit(
        self, fn: object, index: int, tuple_: SweepTuple, bars: Sequence[CandleBar]
    ) -> Future[mod.TupleWorkResult]:
        fut: Future[mod.TupleWorkResult] = Future()
        if index in self._never_resolve:
            return fut  # deliberately never resolved → drives the stall watchdog
        if index in self._result_for:
            fut.set_result(self._result_for[index])
        else:
            fut.set_result(mod._run_tuple_in_worker(index, tuple_, bars))
        return fut


def _driver(
    tmp_path: Path,
    grid: SweepGridConfig,
    guards: SweepGuards,
    ingest: AuditWriter,
    *,
    workers: int = 4,
    stall_seconds: float = 300.0,
    monotonic: Any = None,
) -> mod.ParallelGridDriver:
    kwargs: dict[str, Any] = {
        "bars_for": _bars_for,
        "root": tmp_path / "out",
        "ingest_audit": ingest,
        "guards": guards,
        "workers": workers,
        "stall_seconds": stall_seconds,
    }
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return mod.ParallelGridDriver(grid, **kwargs)


def _drive_with_fake(
    tmp_path: Path,
    grid: SweepGridConfig,
    *,
    result_for: dict[int, mod.TupleWorkResult] | None = None,
    later_seen_index: int | None = None,
) -> tuple[mod._Accumulator, int, bool]:
    """Run the driver's core loop against a deterministic in-process fake pool.

    Returns ``(accumulator, checkpoint.next_index, later_tuple_cache_seen)``. The
    cache probe MUST be taken INSIDE the ``with`` block — the ``_guards`` context
    manager CLOSES the ``SweepCache`` on exit (a probe afterwards raises
    ``SweepCacheError``), so the probe result is captured here and returned.
    """
    from brutex.engine.sweep_supervisor import tuple_cache_params

    later_seen = False
    with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
        driver = _driver(tmp_path, grid, guards, ing)
        guards.checkpoint.load()
        resume_from = guards.checkpoint.next_index
        acc = mod._Accumulator()
        fake = _FakePool(
            mod._WorkerContext(grid=grid, root=tmp_path / "out"), result_for=result_for
        )
        driver._drive(fake, resume_from, 2, 4, acc)  # type: ignore[arg-type]
        if later_seen_index is not None:
            later = list(iter_sweep_tuples(grid))[later_seen_index]
            later_seen = guards.cache.seen(
                later.k,
                later.instrument,
                later.tf.label,
                "bull" if later.direction is Side.BUY else "bear",
                tuple_cache_params(later, grid),
                data_snapshot_hash=grid.data_snapshot_hash,
                indicator_version=grid.indicator_version,
                code_commit_sha=grid.code_commit_sha,
            )
        next_index = guards.checkpoint.next_index
    return acc, next_index, later_seen


class TestFailedTuplePrefixFreeze:
    def test_failure_at_j_freezes_checkpoint_at_j_minus_1(self, tmp_path: Path) -> None:
        """A SweepComboError at index j freezes the checkpoint at j-1 (C5 lesson)."""
        grid = _grid()
        total = count_sweep_tuples(grid)
        assert total >= 3  # the grid must span the failure + a later success
        # Inject a failure at enumeration index 1; every OTHER index runs normally.
        failed = mod.TupleWorkResult(index=1, summary=None, failed_error="boom", audit_records=())
        acc, next_index, _ = _drive_with_fake(tmp_path, grid, result_for={1: failed})
        assert acc.tuples_failed == 1
        assert acc.had_failure is True
        # The checkpoint advanced over 0 but FROZE before the failed index 1 —
        # so a resume re-runs index 1 (never permanently skipped).
        assert next_index == 1  # next_index = last_completed(0) + 1

    def test_later_success_after_failure_is_cache_recorded_not_checkpointed(
        self, tmp_path: Path
    ) -> None:
        grid = _grid()
        total = count_sweep_tuples(grid)
        failed = mod.TupleWorkResult(index=0, summary=None, failed_error="boom", audit_records=())
        # Probe the LAST tuple's cache-recording INSIDE the run (before cache close).
        acc, next_index, later_seen = _drive_with_fake(
            tmp_path, grid, result_for={0: failed}, later_seen_index=total - 1
        )
        # Index 0 failed → NOTHING checkpointed (frozen at the start).
        assert next_index == 0
        # But a later successful tuple IS cache-recorded (so resume skips it, riding
        # the cache tier not the frozen checkpoint — the C5 lesson).
        assert later_seen is True
        assert acc.tuples_run == total - 1

    def test_cache_skip_after_failure_does_not_advance_checkpoint(self, tmp_path: Path) -> None:
        """A cache-hit tuple AFTER a failed one is skipped but NOT checkpointed."""
        from brutex.engine.sweep_supervisor import tuple_cache_params

        grid = _grid()
        tuples = list(iter_sweep_tuples(grid))
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            # Pre-seed the cache so a LATER tuple (index 2) is a cache-hit.
            hit = tuples[2]
            guards.cache.record(
                hit.k,
                hit.instrument,
                hit.tf.label,
                "bull" if hit.direction is Side.BUY else "bear",
                tuple_cache_params(hit, grid),
                data_snapshot_hash=grid.data_snapshot_hash,
                indicator_version=grid.indicator_version,
                code_commit_sha=grid.code_commit_sha,
            )
            acc = mod._Accumulator()
            # Fail index 0; index 2 is then a cache-skip that occurs AFTER the failure.
            failed = mod.TupleWorkResult(
                index=0, summary=None, failed_error="boom", audit_records=()
            )
            fake = _FakePool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), result_for={0: failed}
            )
            driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]
            next_index = guards.checkpoint.next_index
        assert acc.skipped_cache == 1
        assert acc.had_failure is True
        # Frozen at the failed index 0 — the post-failure cache skip did NOT advance.
        assert next_index == 0

    def test_no_bars_after_failure_does_not_advance_checkpoint(self, tmp_path: Path) -> None:
        """A no-bars tuple AFTER a failed one is skipped but NOT checkpointed."""
        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            # Fail index 0; make every OTHER tuple return no bars (a no-bars skip
            # that occurs AFTER the failure).
            failed = mod.TupleWorkResult(
                index=0, summary=None, failed_error="boom", audit_records=()
            )
            no_bars = {
                i: mod.TupleWorkResult(index=i, summary=None, failed_error=None, audit_records=())
                for i in range(1, count_sweep_tuples(grid))
            }
            fake = _FakePool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"),
                result_for={0: failed, **no_bars},
            )
            driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]
            next_index = guards.checkpoint.next_index
        assert acc.tuples_failed == 1
        assert acc.tuples_skipped_no_bars == count_sweep_tuples(grid) - 1
        assert acc.had_failure is True
        # Frozen at the failed index 0 — the post-failure no-bars skips did NOT advance.
        assert next_index == 0


class TestStallWatchdog:
    def test_wedged_worker_raises_sweep_stall_error(self, tmp_path: Path) -> None:
        """A worker whose future never resolves raises mod.SweepStallError, never hangs."""
        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(
                tmp_path, grid, guards, ing, stall_seconds=10.0, monotonic=_jumping_clock()
            )
            guards.checkpoint.load()
            acc = mod._Accumulator()
            # Index 0 never resolves → the drain waits, the clock jumps past the
            # stall window, and mod.SweepStallError is raised (bounded, not a hang).
            fake = _FakePool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), never_resolve={0}
            )
            with pytest.raises(mod.SweepStallError, match="no progress"):
                driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]

    def test_run_wraps_a_drive_failure_in_an_abort_line(self, tmp_path: Path) -> None:
        """``run()`` records a SUPERVISOR_ABORT line then re-raises when ``_drive`` fails.

        Uses a subclass overriding ``_drive`` (the driver is ``__slots__``-based, so
        an instance attribute cannot be patched) to raise BEFORE any tuple is
        dispatched — so the pool spawns + tears down cleanly and the abort contract
        is asserted without a heavy real sweep.
        """

        class _AbortingDriver(mod.ParallelGridDriver):
            def _drive(
                self,
                pool: Any,
                resume_from: int,
                max_inflight: int,
                heap_cap: int,
                acc: mod._Accumulator,
            ) -> None:
                raise mod.SweepStallError("wedged")

        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _AbortingDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ing,
                guards=guards,
                workers=2,
            )
            with pytest.raises(mod.SweepStallError, match="wedged"):
                driver.run()
        # The supervisor chain carries the SUPERVISOR_ABORT line + the progress fails.
        actions = [e.action for e in read_sup_entries(tmp_path / "sup.jsonl")]
        assert "SUPERVISOR_ABORT" in actions


def _jumping_clock() -> Callable[[], float]:
    """A monotonic clock that jumps 1000s per call — trips the stall deadline fast.

    A plain ``def`` closure over a mutable holder (NOT a callable CLASS) so CodeQL's
    points-to analysis can see the production ``self._monotonic()`` call site is
    callable (a callable-class fixture triggers a ``py/call-to-non-callable`` false
    positive on the UNCHANGED production line).
    """
    holder = {"t": 0.0}

    def clock() -> float:
        holder["t"] += 1000.0
        return holder["t"]

    return clock


# ─────────────── crash-mid-parallel resume (redo-not-skip) ─────────────


class TestCrashMidParallelResume:
    def test_missing_sentinel_tuple_is_redone_and_result_is_identical(self, tmp_path: Path) -> None:
        """Simulate a crash: delete a tuple's outputs; a resume RE-runs it, and the
        final result-set equals a clean single run (redo-not-skip, Golden Rule #7)."""
        grid = _grid()
        # Clean reference run.
        ref_root, _ref_audit, _ = _run_parallel(tmp_path / "ref", grid, workers=3)
        ref_tree = _tree(ref_root)

        # A "crashed" run: run it, then DELETE one tuple's whole Parquet+sidecar+
        # sentinel triple (simulating a half-written / never-finalised tuple) and
        # rewind the checkpoint so that tuple is re-processed.
        crash_root, _crash_audit, _ = _run_parallel(tmp_path / "crash", grid, workers=3)
        victim = next(iter(iter_sweep_tuples(grid)))
        victim_store = tuple_store_path(crash_root, victim, grid)
        # Remove the Parquet + its sidecar + sentinel siblings.
        for sib in victim_store.parent.glob(victim_store.stem + "*"):
            sib.unlink()

        # Resume against a FRESH cache (a new process starts with no dedup/cache
        # marks — the sentinel is the sole redo authority) with a rewound checkpoint.
        with (
            AuditWriter.open(tmp_path / "crash" / "resume-i.jsonl") as ing,
            SweepAuditWriter.open(tmp_path / "crash" / "resume-s.jsonl") as sup,
            SweepCache.open(tmp_path / "crash" / "resume-cache", bloom_capacity=10_000) as cache,
        ):
            ckpt = SweepCheckpointStore(
                path=tmp_path / "crash" / "resume-ck.json",
                sweep_id=grid_identity(grid),
                total_combos=count_sweep_tuples(grid),
            )
            guards = SweepGuards(
                audit=sup,
                progress=SweepProgress(),
                checkpoint=ckpt,
                cache=cache,
                watchdog=SweepWatchdog(),
            )
            mod.run_parallel_grid(
                grid,
                bars_for=_bars_for,
                root=crash_root,
                ingest_audit=ing,
                guards=guards,
                workers=3,
            )
        # The victim tuple's outputs were regenerated byte-identically, and the
        # whole tree once again matches the clean reference run.
        assert _tree(crash_root) == ref_tree


class TestWorkerCrashGlobalStop:
    def test_non_combo_exception_in_worker_tears_down_and_reraises(self, tmp_path: Path) -> None:
        """A worker future that raises a NON-SweepComboError (e.g. MemoryError /
        a genuine bug) propagates → the pool is torn down + a SUPERVISOR_ABORT
        line is written + the error re-raises (global stop, not a per-tuple skip)."""
        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _CrashingPool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), crash_index=0
            )
            with pytest.raises(RuntimeError, match="worker exploded"):
                driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]


class _CrashingPool:
    """A fake pool whose ``crash_index`` future raises a non-combo exception."""

    def __init__(self, ctx: mod._WorkerContext, *, crash_index: int) -> None:
        mod._worker_init(ctx)
        self._crash_index = crash_index

    def submit(
        self, fn: object, index: int, tuple_: SweepTuple, bars: Sequence[CandleBar]
    ) -> Future[mod.TupleWorkResult]:
        fut: Future[mod.TupleWorkResult] = Future()
        if index == self._crash_index:
            fut.set_exception(RuntimeError("worker exploded"))
        else:
            fut.set_result(mod._run_tuple_in_worker(index, tuple_, bars))
        return fut


# ─────────────── back-pressure: bounded in-flight window ───────────────


class _DeferredPool:
    """A fake pool whose futures resolve ONLY when the driver waits on them.

    ``submit`` returns an UNRESOLVED future (so the driver's ``pending`` window
    fills to the bound). A patched module-level ``_futures_wait`` (installed by
    the test) resolves the lowest-index pending future on demand — modelling a
    real pool that completes one tuple at a time — and records the peak
    simultaneously-outstanding futures so the O(pool_size) bound can be asserted.
    """

    def __init__(self, ctx: mod._WorkerContext) -> None:
        mod._worker_init(ctx)
        self.outstanding: list[
            tuple[int, SweepTuple, Sequence[CandleBar], Future[mod.TupleWorkResult]]
        ] = []
        self.peak = 0

    def submit(
        self, fn: object, index: int, tuple_: SweepTuple, bars: Sequence[CandleBar]
    ) -> Future[mod.TupleWorkResult]:
        fut: Future[mod.TupleWorkResult] = Future()
        self.outstanding.append((index, tuple_, bars, fut))
        self.peak = max(self.peak, len(self.outstanding))
        return fut

    def resolve_one(self) -> None:
        """Resolve the LOWEST-index outstanding future (one completion)."""
        self.outstanding.sort(key=lambda e: e[0])
        index, tuple_, bars, fut = self.outstanding.pop(0)
        fut.set_result(mod._run_tuple_in_worker(index, tuple_, bars))


class TestBackPressure:
    def test_inflight_window_is_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outstanding-futures window never exceeds ~2×pool_size (O(pool_size))."""
        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing, workers=3)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _DeferredPool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))

            def _fake_wait(
                pending: Iterable[Future[mod.TupleWorkResult]],
                *,
                timeout: float,
                return_when: object,
            ) -> tuple[set[Future[mod.TupleWorkResult]], set[Future[mod.TupleWorkResult]]]:
                # The driver blocks here when the window is full → resolve exactly
                # one future, then return the now-completed set (FIRST_COMPLETED).
                fake.resolve_one()
                done = {f for f in pending if f.done()}
                return {next(iter(done))}, set()

            monkeypatch.setattr(mod, "_futures_wait", _fake_wait)
            # max_inflight=4, heap_cap=4 (pool_size 2 × the multipliers); in-order
            # resolution keeps the heap near-empty so neither bound binds here.
            driver._drive(fake, 0, (3 - 1) * 2, (3 - 1) * 2, acc)  # type: ignore[arg-type]
        # The peak simultaneously-outstanding futures never exceeded the window.
        assert fake.peak <= 4
        assert acc.tuples_run == count_sweep_tuples(grid)


# ─────────── head-of-line blocking: heap bound (MAJOR-1) + stall (MAJOR-2) ──────────


class _HeadBlockedPool:
    """A fake pool that keeps ONE designated ``blocked_index`` future UNRESOLVED.

    Every OTHER submitted tuple's future is created UNRESOLVED too and parked in
    ``deferred`` (lowest-index-first); the test's patched ``_futures_wait``
    resolves them one at a time via :meth:`resolve_one_sibling`, modelling a real
    pool where the HEAD tuple is legitimately slow while its siblings complete.
    The blocked head is resolved on demand via :meth:`resolve_head`.
    """

    def __init__(self, ctx: mod._WorkerContext, *, blocked_index: int) -> None:
        mod._worker_init(ctx)
        self._blocked_index = blocked_index
        self._head: Future[mod.TupleWorkResult] | None = None
        self._head_payload: tuple[int, SweepTuple, Sequence[CandleBar]] | None = None
        self.deferred: list[
            tuple[int, SweepTuple, Sequence[CandleBar], Future[mod.TupleWorkResult]]
        ] = []

    def submit(
        self, fn: object, index: int, tuple_: SweepTuple, bars: Sequence[CandleBar]
    ) -> Future[mod.TupleWorkResult]:
        """Return an UNRESOLVED future; park it as the head or a resolvable sibling."""
        fut: Future[mod.TupleWorkResult] = Future()
        if index == self._blocked_index:
            self._head = fut
            self._head_payload = (index, tuple_, bars)
        else:
            self.deferred.append((index, tuple_, bars, fut))
        return fut

    def resolve_one_sibling(self) -> bool:
        """Resolve the lowest-index parked SIBLING future; ``False`` if none remain."""
        if not self.deferred:
            return False
        self.deferred.sort(key=lambda e: e[0])
        index, tuple_, bars, fut = self.deferred.pop(0)
        fut.set_result(mod._run_tuple_in_worker(index, tuple_, bars))
        return True

    def resolve_head(self) -> None:
        """Resolve the blocked head future (the slow tuple finally finishes)."""
        assert self._head is not None and self._head_payload is not None
        index, tuple_, bars = self._head_payload
        self._head.set_result(mod._run_tuple_in_worker(index, tuple_, bars))


class TestHeadOfLineHeapBound:
    """MAJOR-1: dispatch is coupled to apply-progress so the reorder heap is bounded."""

    def test_heap_stays_bounded_while_head_is_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With index 0 blocked and MANY siblings completing, the heap never grows
        to O(T): dispatch pauses at the heap cap, so the peak heap is a small
        constant multiple of pool_size — NOT the whole remaining grid."""
        # A grid MUCH larger than the O(pool_size) bound, so O(T) heap growth would
        # be unmistakable: 2 dir × 5 k × 2 sl × 2 tp = 40 tuples.
        grid = _grid(k_values=[1, 2, 3, 4, 5], stop_losses=[80, 90], take_profits=[50, 60])
        total = count_sweep_tuples(grid)
        assert total == 40

        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing, workers=3)  # pool_size = 2
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _HeadBlockedPool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), blocked_index=0
            )
            pool_size = 2
            max_inflight = pool_size * 2  # 4
            heap_cap = pool_size * 2  # 4
            peak_heap = 0
            head_released = False

            def _fake_wait(
                pending: Iterable[Future[mod.TupleWorkResult]],
                *,
                timeout: float,
                return_when: object,
            ) -> tuple[set[Future[mod.TupleWorkResult]], set[Future[mod.TupleWorkResult]]]:
                nonlocal peak_heap, head_released
                # Record the reorder-heap depth every time the driver blocks (this is
                # exactly the moment the head-of-line siblings have piled up).
                peak_heap = max(peak_heap, len(driver._heap))
                already_done = {f for f in pending if f.done()}
                if already_done:
                    return {next(iter(already_done))}, set()
                # Resolve one more sibling; when the siblings are exhausted the ONLY
                # thing left is the blocked head → release it so the drive completes.
                if not fake.resolve_one_sibling() and not head_released:
                    fake.resolve_head()
                    head_released = True
                done = {f for f in pending if f.done()}
                return {next(iter(done))}, set()

            monkeypatch.setattr(mod, "_futures_wait", _fake_wait)
            driver._drive(fake, 0, max_inflight, heap_cap, acc)  # type: ignore[arg-type]

        # THE BOUND: the heap never approached O(T). It can overshoot the cap by the
        # futures already in flight when dispatch pauses, so the honest ceiling is
        # heap_cap + max_inflight — a small constant multiple of pool_size, FAR below
        # the 40-tuple grid (which is what an unbounded O(T) heap would reach).
        assert peak_heap <= heap_cap + max_inflight
        assert peak_heap < total  # genuinely bounded, not O(T)
        # Correctness is untouched: every tuple still ran + applied in order.
        assert acc.tuples_run == total
        assert guards.checkpoint.next_index == total


class TestHeadOfLineStallClock:
    """MAJOR-2: the stall clock measures time-since-COMPLETION, not time-since-APPLY."""

    def test_slow_head_with_completing_siblings_does_not_abort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow-but-progressing head (index 0) while siblings 1,2,… complete must
        NOT raise mod.SweepStallError — each sibling completion resets the stall clock,
        even though the head keeps the apply prefix blocked for a long time."""
        grid = _grid(k_values=[1, 2, 3, 4, 5])  # 2 dir × 5 k = 10 tuples
        total = count_sweep_tuples(grid)
        assert total == 10

        # A clock that advances 100s per _futures_wait call. With stall_seconds=250
        # the CUMULATIVE time across the many sibling completions FAR exceeds the
        # stall window — so the OLD (reset-only-on-apply) watchdog would spuriously
        # abort. The FIX (reset on completion) refreshes the deadline each sibling.
        clock = _stepping_clock(step=100.0)

        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(
                tmp_path, grid, guards, ing, workers=3, stall_seconds=250.0, monotonic=clock
            )
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _HeadBlockedPool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), blocked_index=0
            )
            head_released = False

            def _fake_wait(
                pending: Iterable[Future[mod.TupleWorkResult]],
                *,
                timeout: float,
                return_when: object,
            ) -> tuple[set[Future[mod.TupleWorkResult]], set[Future[mod.TupleWorkResult]]]:
                nonlocal head_released
                # A completion arrives ONLY if the driver still had time budget when
                # it entered the wait — model that: a non-positive timeout means the
                # deadline expired with NOTHING done (the genuine-stall signal).
                if timeout <= 0.0:
                    return set(), set(pending)
                already_done = {f for f in pending if f.done()}
                if already_done:
                    return {next(iter(already_done))}, set()
                if not fake.resolve_one_sibling() and not head_released:
                    fake.resolve_head()
                    head_released = True
                done = {f for f in pending if f.done()}
                return {next(iter(done))}, set()

            monkeypatch.setattr(mod, "_futures_wait", _fake_wait)
            # Must NOT raise: the head is slow but siblings keep completing.
            driver._drive(fake, 0, 4, 4, acc)  # type: ignore[arg-type]

        assert acc.tuples_run == total
        assert guards.checkpoint.next_index == total

    def test_genuine_full_hang_still_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine hang — NOTHING ever completes — STILL raises mod.SweepStallError
        within the deadline (the liveness guarantee is preserved, not weakened)."""
        grid = _grid(k_values=[1, 2, 3, 4, 5])
        clock = _stepping_clock(step=100.0)

        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(
                tmp_path, grid, guards, ing, workers=3, stall_seconds=250.0, monotonic=clock
            )
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _HeadBlockedPool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), blocked_index=0
            )

            def _fake_wait(
                pending: Iterable[Future[mod.TupleWorkResult]],
                *,
                timeout: float,
                return_when: object,
            ) -> tuple[set[Future[mod.TupleWorkResult]], set[Future[mod.TupleWorkResult]]]:
                # A total hang: no future EVER resolves (the head is blocked and no
                # sibling is ever released), so every wait returns nothing done.
                return set(), set(pending)

            monkeypatch.setattr(mod, "_futures_wait", _fake_wait)
            with pytest.raises(mod.SweepStallError, match="no progress"):
                driver._drive(fake, 0, 4, 4, acc)  # type: ignore[arg-type]


def _stepping_clock(*, step: float) -> Callable[[], float]:
    """A monotonic clock advancing by ``step`` seconds per call (a plain-def closure).

    A plain ``def`` closure over a mutable holder (NOT a callable CLASS) so CodeQL's
    points-to analysis keeps seeing the production ``self._monotonic()`` call site as
    callable (a callable-class fixture triggers a ``py/call-to-non-callable`` false
    positive on the UNCHANGED production line — same guard as :func:`_jumping_clock`).
    """
    holder = {"t": 0.0}

    def clock() -> float:
        holder["t"] += step
        return holder["t"]

    return clock


# ───────────────────── heavy-grid-aware stall + OOM quarantine ─────────────────────
# DEC-SWEEP-STALL-OOM-GUARDS-001: (1) the stall bound scales with the in-flight
# mask·bar work so a legitimately-slow k=5 tuple is not spuriously aborted near grid
# completion, while a genuine hang still aborts (finite bound); (2) a worker OOM →
# BrokenProcessPool is caught DISTINCTLY, the in-flight tuples are durably quarantined
# (cache-record + checkpoint advance) so a resume never re-dispatches the same OOMing
# tuple, then the run fails LOUDLY naming the offending tuple(s) + remediation.


def _cache_seen(guards: SweepGuards, grid: SweepGridConfig, tuple_: SweepTuple) -> bool:
    """Probe the SweepCache for *tuple_* (mirrors the driver's cache key)."""
    from brutex.engine.sweep_supervisor import tuple_cache_params

    return guards.cache.seen(
        tuple_.k,
        tuple_.instrument,
        tuple_.tf.label,
        "bull" if tuple_.direction is Side.BUY else "bear",
        tuple_cache_params(tuple_, grid),
        data_snapshot_hash=grid.data_snapshot_hash,
        indicator_version=grid.indicator_version,
        code_commit_sha=grid.code_commit_sha,
    )


class TestStallEnvResolution:
    """The two positive-float env knobs — unset/valid/invalid, LOUD on bad values."""

    def test_stall_seconds_unset_is_default(self) -> None:
        assert mod.resolve_stall_seconds(env={}) == mod.DEFAULT_STALL_SECONDS

    def test_stall_seconds_blank_is_default(self) -> None:
        assert mod.resolve_stall_seconds(env={mod.STALL_SECONDS_ENV: "  "}) == (
            mod.DEFAULT_STALL_SECONDS
        )

    def test_stall_seconds_valid_is_verbatim(self) -> None:
        assert mod.resolve_stall_seconds(env={mod.STALL_SECONDS_ENV: "600"}) == 600.0

    @pytest.mark.parametrize("raw", ["oops", "0", "-5", "inf", "nan"])
    def test_stall_seconds_invalid_falls_back_and_warns(self, raw: str) -> None:
        with capture_logs() as logs:
            got = mod.resolve_stall_seconds(env={mod.STALL_SECONDS_ENV: raw})
        assert got == mod.DEFAULT_STALL_SECONDS
        events = [e for e in logs if e["event"] == "sweep.stall_seconds.invalid"]
        assert len(events) == 1
        assert events[0]["log_level"] == "warning"
        assert events[0]["requested_env"] == repr(raw)

    def test_stall_seconds_reads_os_environ_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(mod.STALL_SECONDS_ENV, raising=False)
        assert mod.resolve_stall_seconds() == mod.DEFAULT_STALL_SECONDS

    def test_mask_bars_unset_is_default(self) -> None:
        assert mod.resolve_stall_min_mask_bars_per_sec(env={}) == (mod.STALL_MIN_MASK_BARS_PER_SEC)

    def test_mask_bars_valid_is_verbatim(self) -> None:
        assert (
            mod.resolve_stall_min_mask_bars_per_sec(
                env={mod.STALL_MASK_BARS_PER_SEC_ENV: "1000000"}
            )
            == 1_000_000.0
        )

    @pytest.mark.parametrize("raw", ["nope", "-1", "0"])
    def test_mask_bars_invalid_falls_back_and_warns(self, raw: str) -> None:
        with capture_logs() as logs:
            got = mod.resolve_stall_min_mask_bars_per_sec(
                env={mod.STALL_MASK_BARS_PER_SEC_ENV: raw}
            )
        assert got == mod.STALL_MIN_MASK_BARS_PER_SEC
        assert any(e["event"] == "sweep.stall_mask_bars_per_sec.invalid" for e in logs)


class TestTupleWorkAndEffectiveStall:
    """The pure work estimate + the heavy-grid-aware bound (both arms)."""

    def test_tuple_work_is_masks_times_bars(self, tmp_path: Path) -> None:
        from brutex.engine.combo_mask import count_combos

        grid = _grid()  # condition_bits [0..4] → n_unique = 5
        tuples = list(iter_sweep_tuples(grid))
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            for t in tuples:
                assert driver._tuple_work(t, 240) == float(count_combos(t.k, 5)) * 240.0

    def test_effective_stall_light_grid_keeps_base_floor(self, tmp_path: Path) -> None:
        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing, stall_seconds=300.0)
            # A tiny work value / the huge default throughput floor → base wins.
            assert driver._effective_stall_seconds(2400.0) == 300.0

    def test_effective_stall_heavy_grid_scales_above_base(self, tmp_path: Path) -> None:
        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ing,
                guards=guards,
                workers=4,
                stall_seconds=300.0,
                stall_min_mask_bars_per_sec=1.0,  # 1 mask·bar/s → work == seconds
            )
            # work / 1.0 = 5_000_000 s ≫ the 300 s base → the work-scaled bound wins.
            assert driver._effective_stall_seconds(5_000_000.0) == 5_000_000.0


class TestHeavyGridStallBound:
    """A legitimately-slow heavy tuple is NOT aborted; a real hang STILL aborts."""

    def test_heavy_inflight_tuple_scales_the_wait_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stall wait budget handed to ``_futures_wait`` SCALES with the in-flight
        work — for a heavy grid it is FAR above the flat 300 s (the pre-fix bound that
        would abort a legit slow tuple), and the run completes without aborting."""
        grid = _grid()
        total = count_sweep_tuples(grid)
        orig_wait = mod._futures_wait
        seen_timeouts: list[float] = []

        def _spy(
            pending: Iterable[Future[mod.TupleWorkResult]],
            *,
            timeout: float,
            return_when: str,
        ) -> tuple[set[Future[mod.TupleWorkResult]], set[Future[mod.TupleWorkResult]]]:
            seen_timeouts.append(timeout)
            return orig_wait(pending, timeout=timeout, return_when=return_when)

        monkeypatch.setattr(mod, "_futures_wait", _spy)
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ing,
                guards=guards,
                workers=4,
                stall_seconds=300.0,
                stall_min_mask_bars_per_sec=1.0,  # heavy: work == seconds
            )
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _FakePool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]
        # The pre-fix flat bound would have been ~300; the heavy bound is ≫ that.
        assert seen_timeouts, "the drive must have awaited at least one result"
        assert max(seen_timeouts) > 1000.0
        assert acc.tuples_run == total  # never aborted

    def test_genuine_hang_on_a_heavy_grid_still_aborts(self, tmp_path: Path) -> None:
        """Even with a large work-scaled bound, a fully-wedged pool STILL aborts (the
        bound is FINITE) — proven by a clock that jumps past the scaled deadline."""
        grid = _grid()
        clock = _stepping_clock(step=3000.0)  # jumps past any small work-scaled bound
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ing,
                guards=guards,
                workers=4,
                stall_seconds=300.0,
                stall_min_mask_bars_per_sec=1.0,
                monotonic=clock,
            )
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _FakePool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), never_resolve={0, 1}
            )
            with pytest.raises(mod.SweepStallError, match="mask"):
                driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]


class _BrokenPool:
    """A fake pool modelling a worker OOM breaking the process pool.

    ``break_on`` selects which enumeration indices' FUTURES carry
    :class:`BrokenProcessPool` (``None`` = all — a real OOM breaks EVERY in-flight
    future); ``raise_on_submit`` makes ``submit`` ITSELF raise (the pool broke before
    any task was accepted). A non-breaking index runs the real in-process worker body.
    """

    def __init__(
        self,
        ctx: mod._WorkerContext,
        *,
        break_on: set[int] | None = None,
        raise_on_submit: bool = False,
    ) -> None:
        mod._worker_init(ctx)
        self._break_on = break_on
        self._raise_on_submit = raise_on_submit

    def submit(
        self, fn: object, index: int, tuple_: SweepTuple, bars: Sequence[CandleBar]
    ) -> Future[mod.TupleWorkResult]:
        if self._raise_on_submit:
            raise BrokenProcessPool("A child process terminated abruptly (OOM)")
        fut: Future[mod.TupleWorkResult] = Future()
        if self._break_on is None or index in self._break_on:
            fut.set_exception(BrokenProcessPool("A child process terminated abruptly (OOM)"))
        else:
            fut.set_result(mod._run_tuple_in_worker(index, tuple_, bars))
        return fut


class _RecordingFakePool(_FakePool):
    """A normal in-process fake pool that RECORDS every dispatched enumeration index."""

    def __init__(self, ctx: mod._WorkerContext) -> None:
        super().__init__(ctx)
        self.submitted: list[int] = []

    def submit(
        self, fn: object, index: int, tuple_: SweepTuple, bars: Sequence[CandleBar]
    ) -> Future[mod.TupleWorkResult]:
        self.submitted.append(index)
        return super().submit(fn, index, tuple_, bars)


class TestQuarantineSidecar:
    """The durable, reversible quarantine sidecar helpers (pure I/O)."""

    def test_missing_sidecar_loads_empty(self, tmp_path: Path) -> None:
        assert mod.load_quarantined_tuple_labels(tmp_path, "sid") == set()

    def test_append_then_load_round_trips(self, tmp_path: Path) -> None:
        path = mod.append_quarantine_entries(tmp_path, "sid", [(0, "t0"), (3, "t3")], "OOM")
        assert path == mod.quarantine_sidecar_path(tmp_path)
        assert mod.load_quarantined_tuple_labels(tmp_path, "sid") == {"t0", "t3"}

    def test_append_merges_with_existing_entries(self, tmp_path: Path) -> None:
        mod.append_quarantine_entries(tmp_path, "sid", [(0, "t0")], "OOM")
        mod.append_quarantine_entries(tmp_path, "sid", [(1, "t1")], "OOM")
        assert mod.load_quarantined_tuple_labels(tmp_path, "sid") == {"t0", "t1"}

    def test_other_sweep_ids_are_filtered(self, tmp_path: Path) -> None:
        mod.append_quarantine_entries(tmp_path, "other", [(0, "t0")], "OOM")
        assert mod.load_quarantined_tuple_labels(tmp_path, "sid") == set()

    def test_corrupt_sidecar_loads_empty_and_warns(self, tmp_path: Path) -> None:
        mod.quarantine_sidecar_path(tmp_path).write_text("{not json", encoding="utf-8")
        with capture_logs() as logs:
            got = mod.load_quarantined_tuple_labels(tmp_path, "sid")
        assert got == set()
        assert any(e["event"] == "sweep.quarantine.sidecar_corrupt" for e in logs)

    def test_wrong_shape_sidecar_loads_empty_and_warns(self, tmp_path: Path) -> None:
        mod.quarantine_sidecar_path(tmp_path).write_text('{"nope": 1}', encoding="utf-8")
        with capture_logs() as logs:
            got = mod.load_quarantined_tuple_labels(tmp_path, "sid")
        assert got == set()
        assert any(e["event"] == "sweep.quarantine.sidecar_corrupt" for e in logs)

    def test_append_replaces_a_corrupt_sidecar(self, tmp_path: Path) -> None:
        mod.quarantine_sidecar_path(tmp_path).write_text("{not json", encoding="utf-8")
        mod.append_quarantine_entries(tmp_path, "sid", [(0, "t0")], "OOM")
        assert mod.load_quarantined_tuple_labels(tmp_path, "sid") == {"t0"}

    def test_append_replaces_a_non_list_entries_sidecar(self, tmp_path: Path) -> None:
        mod.quarantine_sidecar_path(tmp_path).write_text('{"entries": 7}', encoding="utf-8")
        mod.append_quarantine_entries(tmp_path, "sid", [(0, "t0")], "OOM")
        assert mod.load_quarantined_tuple_labels(tmp_path, "sid") == {"t0"}

    def test_directory_at_sidecar_path_loads_empty_and_warns(self, tmp_path: Path) -> None:
        """R4 FINDING 1: a DIRECTORY squatting at the sidecar path (IsADirectoryError —
        an OSError shape) must honour the advisory contract: LOUD + empty, never a
        crash at run()'s resume load."""
        mod.quarantine_sidecar_path(tmp_path).mkdir()
        with capture_logs() as logs:
            got = mod.load_quarantined_tuple_labels(tmp_path, "sid")
        assert got == set()
        assert any(e["event"] == "sweep.quarantine.sidecar_corrupt" for e in logs)

    def test_symlink_self_loop_loads_empty_and_warns(self, tmp_path: Path) -> None:
        """R4 FINDING 1: a symlink self-loop (ELOOP OSError) → LOUD + empty."""
        path = mod.quarantine_sidecar_path(tmp_path)
        path.symlink_to(path)
        with capture_logs() as logs:
            got = mod.load_quarantined_tuple_labels(tmp_path, "sid")
        assert got == set()
        assert any(e["event"] == "sweep.quarantine.sidecar_corrupt" for e in logs)

    def test_directory_at_sidecar_path_append_skips_and_warns(self, tmp_path: Path) -> None:
        """R4 FINDING 1: a DIRECTORY at the sidecar path must not let the append's
        IsADirectoryError MASK the caller's SweepWorkerPoolBrokenError — the append
        is skipped with ONE loud warning."""
        mod.quarantine_sidecar_path(tmp_path).mkdir()
        with capture_logs() as logs:
            path = mod.append_quarantine_entries(tmp_path, "sid", [(0, "t0")], "OOM")
        assert path == mod.quarantine_sidecar_path(tmp_path)
        assert any(e["event"] == "sweep.quarantine.sidecar_corrupt" for e in logs)

    def test_symlink_self_loop_append_self_heals(self, tmp_path: Path) -> None:
        """R4 FINDING 1: a symlink self-loop at the sidecar path never raises an
        ELOOP masking the real error — the read leg treats it as empty (OSError
        caught) and ``os.replace`` REPLACES the symlink itself (rename does not
        follow links), so the append SELF-HEALS the loop into a real sidecar."""
        path = mod.quarantine_sidecar_path(tmp_path)
        path.symlink_to(path)
        got = mod.append_quarantine_entries(tmp_path, "sid", [(0, "t0")], "OOM")
        assert got == path
        assert not path.is_symlink()  # the loop was healed into a regular file
        assert mod.load_quarantined_tuple_labels(tmp_path, "sid") == {"t0"}

    def test_non_dict_entries_are_skipped_on_load(self, tmp_path: Path) -> None:
        mod.quarantine_sidecar_path(tmp_path).write_text(
            '{"entries": [7, {"sweep_id": "sid", "tuple": "t0"}]}', encoding="utf-8"
        )
        assert mod.load_quarantined_tuple_labels(tmp_path, "sid") == {"t0"}


class TestDriverStallValidation:
    """NIT: direct-constructor 0 / non-finite stall knobs are refused, never a ZeroDivision."""

    @pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
    def test_zero_or_nonfinite_mask_bars_refused(self, tmp_path: Path, value: float) -> None:
        grid = _grid()
        with (
            AuditWriter.open(tmp_path / "i.jsonl") as ing,
            _guards(tmp_path, grid) as guards,
            pytest.raises(mod.SweepParallelUsageError, match="positive finite"),
        ):
            mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ing,
                guards=guards,
                workers=4,
                stall_min_mask_bars_per_sec=value,
            )

    def test_zero_stall_seconds_refused(self, tmp_path: Path) -> None:
        grid = _grid()
        with (
            AuditWriter.open(tmp_path / "i.jsonl") as ing,
            _guards(tmp_path, grid) as guards,
            pytest.raises(mod.SweepParallelUsageError, match="positive finite"),
        ):
            mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ing,
                guards=guards,
                workers=4,
                stall_seconds=0.0,
            )


class _MemoryErrorPool(_FakePool):
    """A fake pool whose designated index's future carries a worker MemoryError.

    Models the cgroup/rlimit OOM shape: the worker SURVIVES (the pool does NOT
    break), the tuple's future resolves with :exc:`MemoryError`.
    """

    def __init__(self, ctx: mod._WorkerContext, *, memory_error_index: int) -> None:
        super().__init__(ctx)
        self._memory_error_index = memory_error_index

    def submit(
        self, fn: object, index: int, tuple_: SweepTuple, bars: Sequence[CandleBar]
    ) -> Future[mod.TupleWorkResult]:
        if index == self._memory_error_index:
            fut: Future[mod.TupleWorkResult] = Future()
            fut.set_exception(MemoryError("worker allocation failed"))
            return fut
        return super().submit(fn, index, tuple_, bars)


class TestBrokenProcessPoolQuarantine:
    def test_broken_pool_quarantines_inflight_and_fails_loud(self, tmp_path: Path) -> None:
        """A broken pool → SweepWorkerPoolBrokenError naming tuple(s) + sidecar +
        remediation; the in-flight tuples land in the DURABLE sidecar — deliberately
        NOT cache-recorded, NOT checkpoint-advanced (the reversible-recovery law)."""
        grid = _grid()
        sweep_id = grid_identity(grid)
        tuples = list(iter_sweep_tuples(grid))
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _BrokenPool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            with pytest.raises(mod.SweepWorkerPoolBrokenError) as exc_info:
                driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]
            ckpt = guards.checkpoint.next_index
            seen0 = _cache_seen(guards, grid, tuples[0])
            seen1 = _cache_seen(guards, grid, tuples[1])
        msg = str(exc_info.value)
        assert "OOM" in msg
        assert mod.SWEEP_WORKERS_ENV in msg  # remediation names the workers knob
        assert mod.MEM_BUDGET_ENV in msg  # …and the memory-budget knob
        assert "NIFTY" in msg  # names the offending tuple(s)
        # HONESTY: the message states the resume-skip + names the reversible lever.
        assert "tuples_quarantined_prior" in msg
        assert mod.QUARANTINE_SIDECAR_NAME in msg
        assert "DELETE" in msg
        # max_inflight=2 → the two in-flight tuples were quarantined DURABLY.
        assert acc.tuples_pool_broken == 2
        labels = mod.load_quarantined_tuple_labels(tmp_path / "out", sweep_id)
        assert labels == {tuple_label(tuples[0]), tuple_label(tuples[1])}
        # The reversible-recovery law: NO cache record, NO checkpoint advance —
        # the sidecar is the ONE thing that makes a resume skip them.
        assert ckpt == 0
        assert seen0 is False and seen1 is False

    def test_resume_skips_quarantined_visibly_and_makes_progress(self, tmp_path: Path) -> None:
        """After a broken-pool quarantine, a RESUME never re-dispatches the poison
        tuples — they are counted as the DISTINCT tuples_quarantined_prior (never
        folded into skipped_cache) and the rest of the grid makes progress."""
        grid = _grid()
        total = count_sweep_tuples(grid)
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver1 = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc1 = mod._Accumulator()
            broken = _BrokenPool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            with pytest.raises(mod.SweepWorkerPoolBrokenError):
                driver1._drive(broken, 0, 2, 4, acc1)  # type: ignore[arg-type]

            # RESUME: a fresh driver over the SAME durable guards + sidecar. The
            # driver loads the sidecar in run(); at the _drive level the test seeds
            # it exactly as run() does (the loader is run()-covered below).
            resume_from = guards.checkpoint.next_index
            assert resume_from == 0  # checkpoint FROZEN (reversible-recovery law)
            driver2 = _driver(tmp_path, grid, guards, ing)
            driver2._quarantined = frozenset(
                mod.load_quarantined_tuple_labels(tmp_path / "out", grid_identity(grid))
            )
            acc2 = mod._Accumulator()
            recording = _RecordingFakePool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            driver2._drive(recording, resume_from, 2, 4, acc2)  # type: ignore[arg-type]

        # The quarantined indices 0 & 1 were NEVER re-submitted to a worker…
        assert 0 not in recording.submitted and 1 not in recording.submitted
        # …counted DISTINCTLY (the completeness hole is visible, never a cache blend)…
        assert acc2.tuples_quarantined_prior == 2
        assert acc2.skipped_cache == 0
        # …and the rest of the grid made progress.
        assert acc2.tuples_run == total - 2

    def test_deleting_the_sidecar_recovers_the_quarantined_tuples(self, tmp_path: Path) -> None:
        """THE RECOVERY LEVER: delete the sidecar (after relieving memory pressure)
        → the next resume genuinely RE-DISPATCHES + recomputes the tuples."""
        grid = _grid()
        total = count_sweep_tuples(grid)
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver1 = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc1 = mod._Accumulator()
            broken = _BrokenPool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            with pytest.raises(mod.SweepWorkerPoolBrokenError):
                driver1._drive(broken, 0, 2, 4, acc1)  # type: ignore[arg-type]

            # The operator relieves memory pressure + deletes the sidecar.
            mod.quarantine_sidecar_path(tmp_path / "out").unlink()

            driver2 = _driver(tmp_path, grid, guards, ing)
            driver2._quarantined = frozenset(
                mod.load_quarantined_tuple_labels(tmp_path / "out", grid_identity(grid))
            )
            acc2 = mod._Accumulator()
            recording = _RecordingFakePool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            driver2._drive(recording, guards.checkpoint.next_index, 2, 4, acc2)  # type: ignore[arg-type]

        # The previously-quarantined tuples were genuinely RE-DISPATCHED…
        assert 0 in recording.submitted and 1 in recording.submitted
        # …and the WHOLE grid completed (no permanent loss — Golden Rule #1).
        assert acc2.tuples_run == total
        assert acc2.tuples_quarantined_prior == 0

        # R4 FINDING 2 — "genuinely recomputes" pinned by BYTES, not counts: the
        # recovered result tree is byte-identical to a CLEAN never-quarantined run
        # (delete-to-recompute determinism, Golden Rule #4/#7).
        clean_dir = tmp_path / "clean"
        clean_dir.mkdir()
        with (
            AuditWriter.open(clean_dir / "i.jsonl") as clean_ing,
            _guards(clean_dir, grid) as clean_guards,
        ):
            clean_driver = _driver(clean_dir, grid, clean_guards, clean_ing)
            clean_guards.checkpoint.load()
            clean_acc = mod._Accumulator()
            clean_pool = _FakePool(mod._WorkerContext(grid=grid, root=clean_dir / "out"))
            clean_driver._drive(clean_pool, 0, 2, 4, clean_acc)  # type: ignore[arg-type]
        assert clean_acc.tuples_run == total
        assert _tree(tmp_path / "out") == _tree(clean_dir / "out")

    def test_full_run_resume_reports_quarantined_prior_in_summary(self, tmp_path: Path) -> None:
        """run() loads the sidecar + the returned SupervisedGridSummary carries the
        DISTINCT tuples_quarantined_prior counter (a REAL pool resume run)."""
        grid = _grid()
        total = count_sweep_tuples(grid)
        # Quarantine indices 0+1 via a fake broken pool against the SAME root.
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver1 = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            broken = _BrokenPool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            with pytest.raises(mod.SweepWorkerPoolBrokenError):
                driver1._drive(broken, 0, 2, 4, mod._Accumulator())  # type: ignore[arg-type]

        # A REAL parallel resume over the same root: the summary surfaces the hole.
        # (Fresh guards in a sibling dir — the sup.jsonl chain does not resume; the
        # quarantine gate is sidecar-based, so the SAME root is what matters.)
        resume_dir = tmp_path / "resume"
        resume_dir.mkdir()
        with (
            AuditWriter.open(resume_dir / "ingest2.jsonl") as ingest,
            _guards(resume_dir, grid) as guards,
        ):
            summary = mod.run_parallel_grid(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ingest,
                guards=guards,
                workers=3,
            )
        assert summary.tuples_quarantined_prior == 2
        assert summary.tuples_run == total - 2
        assert summary.skipped_cache == 0  # NOT folded into cache skips

    def test_memory_error_quarantines_only_that_tuple(self, tmp_path: Path) -> None:
        """A worker's Python-level MemoryError (pool NOT broken) quarantines ONLY the
        offending tuple — innocent siblings are NOT sidecar'd — then fails loud."""
        grid = _grid()
        sweep_id = grid_identity(grid)
        tuples = list(iter_sweep_tuples(grid))
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _MemoryErrorPool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), memory_error_index=0
            )
            with pytest.raises(mod.SweepWorkerPoolBrokenError) as exc_info:
                driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]
        msg = str(exc_info.value)
        assert "MemoryError" in msg
        assert "tuples_quarantined_prior" in msg
        assert acc.tuples_pool_broken == 1
        labels = mod.load_quarantined_tuple_labels(tmp_path / "out", sweep_id)
        assert labels == {tuple_label(tuples[0])}  # ONLY the poison tuple

    def test_memory_error_resume_skips_only_the_poison_tuple(self, tmp_path: Path) -> None:
        grid = _grid()
        total = count_sweep_tuples(grid)
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver1 = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            fake = _MemoryErrorPool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), memory_error_index=0
            )
            with pytest.raises(mod.SweepWorkerPoolBrokenError):
                driver1._drive(fake, 0, 2, 4, mod._Accumulator())  # type: ignore[arg-type]

            driver2 = _driver(tmp_path, grid, guards, ing)
            driver2._quarantined = frozenset(
                mod.load_quarantined_tuple_labels(tmp_path / "out", grid_identity(grid))
            )
            acc2 = mod._Accumulator()
            recording = _RecordingFakePool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            driver2._drive(recording, guards.checkpoint.next_index, 2, 4, acc2)  # type: ignore[arg-type]
        assert 0 not in recording.submitted  # the poison tuple stays out
        assert acc2.tuples_quarantined_prior == 1
        assert acc2.tuples_run == total - 1  # every innocent tuple recomputed

    def test_quarantine_drains_completed_futures_before_quarantining(self, tmp_path: Path) -> None:
        """A pool break with an already-FINISHED sibling future: the finished
        result is DRAINED + applied as a real outcome (compute never discarded);
        only the genuinely-broken future's tuple is quarantined."""
        grid = _grid()
        sweep_id = grid_identity(grid)
        tuples = list(iter_sweep_tuples(grid))
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            mod._worker_init(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            done_fut: Future[mod.TupleWorkResult] = Future()
            done_fut.set_result(mod._run_tuple_in_worker(0, tuples[0], _BARS))
            broken_fut: Future[mod.TupleWorkResult] = Future()
            broken_fut.set_exception(BrokenProcessPool("terminated abruptly"))
            # A genuinely IN-FLIGHT (never-resolved) future — the pool broke under it.
            inflight_fut: Future[mod.TupleWorkResult] = Future()
            pending = {
                done_fut: mod._Dispatched(index=0, tuple_=tuples[0], work=1.0),
                broken_fut: mod._Dispatched(index=1, tuple_=tuples[1], work=1.0),
                inflight_fut: mod._Dispatched(index=2, tuple_=tuples[2], work=1.0),
            }
            with pytest.raises(mod.SweepWorkerPoolBrokenError):
                driver._quarantine_broken_pool(
                    pending, acc, BrokenProcessPool("terminated abruptly")
                )
            seen0 = _cache_seen(guards, grid, tuples[0])
        # The finished sibling's REAL result was applied (run + cache-recorded)…
        assert acc.tuples_run == 1
        assert seen0 is True
        # …and ONLY the broken + still-in-flight futures' tuples were quarantined.
        assert acc.tuples_pool_broken == 2
        labels = mod.load_quarantined_tuple_labels(tmp_path / "out", sweep_id)
        assert labels == {tuple_label(tuples[1]), tuple_label(tuples[2])}
        assert pending == {}

    def test_submit_broken_before_any_task_fails_loud_with_zero_quarantined(
        self, tmp_path: Path
    ) -> None:
        """A pool that breaks at the FIRST ``submit`` (before any tuple is in flight)
        still fails LOUD (nothing to quarantine — the empty-pending path)."""
        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _BrokenPool(
                mod._WorkerContext(grid=grid, root=tmp_path / "out"), raise_on_submit=True
            )
            with pytest.raises(mod.SweepWorkerPoolBrokenError, match="OOM"):
                driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]
            ckpt = guards.checkpoint.next_index
        assert acc.tuples_pool_broken == 0
        assert ckpt == 0

    def test_apply_pool_broken_freezes_checkpoint_and_skips_cache(self, tmp_path: Path) -> None:
        """The pool_broken apply branch: counter + had_failure latch; deliberately NO
        checkpoint advance and NO cache record (the sidecar is the only skip lever)."""
        grid = _grid()
        tuples = list(iter_sweep_tuples(grid))
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            outcome = mod._Outcome(
                index=0,
                tuple_=tuples[0],
                kind="pool_broken",
                summary=None,
                failed_error="BrokenProcessPool: OOM",
                audit_records=(),
            )
            driver._apply(outcome, acc)
            ckpt = guards.checkpoint.next_index
            seen0 = _cache_seen(guards, grid, tuples[0])
        assert acc.tuples_pool_broken == 1
        assert acc.had_failure is True
        assert ckpt == 0  # FROZEN — recovery stays possible after a sidecar delete
        assert seen0 is False  # NEVER cache-recorded (would be irreversible)

    def test_apply_quarantined_prior_counts_distinctly_and_freezes(self, tmp_path: Path) -> None:
        """The quarantined_prior apply branch: DISTINCT counter + had_failure latch +
        the distinct audit action naming the sidecar."""
        grid = _grid()
        tuples = list(iter_sweep_tuples(grid))
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            outcome = mod._Outcome(
                index=0,
                tuple_=tuples[0],
                kind="quarantined_prior",
                summary=None,
                failed_error=None,
                audit_records=(),
            )
            with capture_logs() as logs:
                driver._apply(outcome, acc)
            ckpt = guards.checkpoint.next_index
        assert acc.tuples_quarantined_prior == 1
        assert acc.skipped_cache == 0
        assert acc.had_failure is True
        assert ckpt == 0
        assert any(e["event"] == "sweep_parallel.tuple_quarantined_prior" for e in logs)
        actions = [e.action for e in read_sup_entries(tmp_path / "sup.jsonl")]
        assert mod.SUPERVISOR_ACTION_TUPLE_QUARANTINED_PRIOR in actions

    def test_pool_broken_emits_a_distinct_audit_action(self, tmp_path: Path) -> None:
        """The quarantine records the distinct SUPERVISOR_TUPLE_POOL_BROKEN audit line."""
        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _driver(tmp_path, grid, guards, ing)
            guards.checkpoint.load()
            acc = mod._Accumulator()
            fake = _BrokenPool(mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            with pytest.raises(mod.SweepWorkerPoolBrokenError):
                driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]
        actions = [e.action for e in read_sup_entries(tmp_path / "sup.jsonl")]
        assert mod.SUPERVISOR_ACTION_TUPLE_POOL_BROKEN in actions

    def test_run_wraps_a_broken_pool_error_in_an_abort_line(self, tmp_path: Path) -> None:
        """``run()`` records SUPERVISOR_ABORT then re-raises the DISTINCT broken-pool
        error (a subclass overriding ``_drive`` — the driver is ``__slots__``-based)."""

        class _BreakingDriver(mod.ParallelGridDriver):
            def _drive(
                self,
                pool: Any,
                resume_from: int,
                max_inflight: int,
                heap_cap: int,
                acc: mod._Accumulator,
            ) -> None:
                raise mod.SweepWorkerPoolBrokenError("worker OOM")

        grid = _grid()
        with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
            driver = _BreakingDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ing,
                guards=guards,
                workers=2,
            )
            with pytest.raises(mod.SweepWorkerPoolBrokenError, match="worker OOM"):
                driver.run()
        actions = [e.action for e in read_sup_entries(tmp_path / "sup.jsonl")]
        assert "SUPERVISOR_ABORT" in actions


class TestWorkerCorruptStoreContained:
    """DB-02 parallel twin (DEC-SWEEP-STORE-CORRUPT-CONTAIN-001, MIGRATION.md #1633).

    A CORRUPT / old-schema finalist store raised at its LOAD boundary inside a
    worker is CONTAINED as a failed marker (surfaced through the supervisor's
    ``TUPLE_FAILED`` lane, whose error string names the store path) instead of
    crashing the worker → the whole grid. Only the G14 ``ResultsStoreSchemaError``
    is caught, NOT its parent ``ResultsStoreError``.
    """

    def test_corrupt_store_returns_failed_marker(self, tmp_path: Path) -> None:
        grid = _grid()
        out = tmp_path / "out"
        mod._worker_init(mod._WorkerContext(grid=grid, root=out))
        tuple_ = next(iter_sweep_tuples(grid))
        # Corrupt the tuple's finalist store so its LOAD raises ResultsStoreSchemaError.
        store_path = tuple_store_path(out, tuple_, grid)
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_bytes(b"CORRUPT\x00\x01" * 40)
        result = mod._run_tuple_in_worker(0, tuple_, _BARS)
        assert result.summary is None
        assert result.failed_error is not None
        assert "ResultsStoreSchemaError" in result.failed_error

    def test_corrupt_store_contained_in_real_spawn_pool(self, tmp_path: Path) -> None:
        grid = _grid()  # 4 tuples via BUY/SELL × k={1,2}
        out = tmp_path / "out"
        victim = next(iter_sweep_tuples(grid))
        store_path = tuple_store_path(out, victim, grid)
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_bytes(b"CORRUPT\x00\x01" * 40)
        with capture_logs() as logs:
            _run_parallel(tmp_path, grid, workers=2)
        entries = read_sup_entries(tmp_path / "sup.jsonl")
        actions = [e.action for e in entries]
        # The grid FINISHED (not a whole-grid abort) with the corrupt tuple contained.
        assert "SUPERVISOR_COMPLETE" in actions
        failed = [e for e in entries if e.action == "TUPLE_FAILED"]
        assert len(failed) == 1
        assert "ResultsStoreSchemaError" in failed[0].payload["error"]
        # Remediation PARITY (review round 1): the PARENT fires the SAME
        # remediation-carrying WARN the sequential/supervisor sites emit — the
        # production multi-day path must not lose the heal instruction.
        from brutex.engine.sweep_orchestrator import TUPLE_STORE_CORRUPT_REMEDIATION

        corrupt = [e for e in logs if e.get("event") == "sweep_grid.tuple_store_corrupt"]
        assert len(corrupt) == 1
        assert "ResultsStoreSchemaError" in corrupt[0]["error"]
        assert corrupt[0]["remediation"] == TUPLE_STORE_CORRUPT_REMEDIATION

    def test_non_schema_failed_marker_fires_no_corrupt_warn(self, tmp_path: Path) -> None:
        """The parity arm's FALSE branch: a plain failed marker stays generic.

        Drives ``_apply_outcome``'s failed lane directly with a
        SweepComboError-shaped marker (the worker's other failed class) and
        asserts the remediation WARN does NOT fire — only the pre-existing
        ``sweep_parallel.tuple_failed`` — so the discriminator can never
        over-trigger on an ordinary combo failure.
        """
        grid = _grid()
        out = tmp_path / "out"
        tuple_ = next(iter_sweep_tuples(grid))
        outcome = mod._Outcome(
            index=0,
            tuple_=tuple_,
            kind="failed",
            summary=None,
            failed_error="SweepComboError: boom",
            audit_records=(),
        )
        with (
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
            _guards(tmp_path, grid) as guards,
        ):
            driver = mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=out,
                ingest_audit=ingest,
                guards=guards,
                workers=2,
            )
            acc = mod._Accumulator()
            with capture_logs() as logs:
                driver._apply(outcome, acc)
        assert acc.tuples_failed == 1
        assert any(e.get("event") == "sweep_parallel.tuple_failed" for e in logs)
        assert not any(e.get("event") == "sweep_grid.tuple_store_corrupt" for e in logs)


class TestStoreCorruptPrefixIsComplete:
    """R2-review-round-2 SHOULD-FIX 2: the cross-process corrupt-store prefix is COMPLETE.

    ``_apply_outcome``'s remediation WARN keys on
    ``error_text.startswith(_STORE_CORRUPT_ERROR_PREFIX)`` — a STRING coupling
    across the spawn boundary, since a worker serialises its caught exception as
    ``f"{type(exc).__name__}: {exc}"`` and the exception OBJECT never crosses the
    pickle boundary. The exact ``"ResultsStoreSchemaError: "`` prefix is a COMPLETE
    discriminator ONLY while ``ResultsStoreSchemaError`` has no subclasses: a
    subclass would serialise under its OWN ``__name__`` and SILENTLY drop the
    heal-instruction WARN. This pin fails loudly the moment such a subclass is
    introduced (forcing either a widened discriminator or a conscious decision).
    """

    def test_results_store_schema_error_has_no_subclasses(self) -> None:
        from brutex.engine.results_store import ResultsStoreSchemaError

        assert ResultsStoreSchemaError.__subclasses__() == [], (
            "ResultsStoreSchemaError gained a subclass — the parallel-lane "
            "startswith(_STORE_CORRUPT_ERROR_PREFIX) discriminator "
            "(sweep_parallel._apply_outcome) would silently drop the DB-02 "
            "remediation WARN for it; widen the prefix match or split the class."
        )

    def test_prefix_matches_the_worker_serialisation_shape(self) -> None:
        """The prefix EXACTLY matches ``f"{type(exc).__name__}: "`` for the class."""
        from brutex.engine.results_store import ResultsStoreSchemaError

        exc = ResultsStoreSchemaError("unreadable results store at /x: boom")
        serialised = f"{type(exc).__name__}: {exc}"
        assert serialised.startswith(mod._STORE_CORRUPT_ERROR_PREFIX)
        assert mod._STORE_CORRUPT_ERROR_PREFIX == "ResultsStoreSchemaError: "
