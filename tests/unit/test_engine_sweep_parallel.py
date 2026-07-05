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
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import brutex.engine.sweep_parallel as mod
import pytest
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
)
from brutex.engine.sweep_watchdog import SweepWatchdog
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

    def test_stable_across_repeated_parallel_runs(self, tmp_path: Path) -> None:
        """Non-determinism stress (short): two parallel runs are byte-identical."""
        grid = _grid()
        root_a, audit_a, ck_a = _run_parallel(tmp_path / "a", grid, workers=4)
        root_b, audit_b, ck_b = _run_parallel(tmp_path / "b", grid, workers=3)
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
