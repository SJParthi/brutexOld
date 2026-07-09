"""Parallel-driver twins of the resume store guard suite (DEC-SWEEP-RESUME-STORE-GUARD-001, T15).

The production CLI DEFAULTS to the parallel driver at ``workers > 1``
(``cmd_sweep_all``), and its ``_gate_or_dispatch`` resume/cache tiers were
proven EQUALLY store-blind — so the guard is a REQUIRED scope extension here,
not an optional twin. These tests drive the REAL
:func:`brutex.engine.sweep_parallel.run_parallel_grid` (spawn pool, workers=2)
over real durable state:

* deleted store Parquets are regenerated IN-WORKER byte-identically
  (``run_persisted_sweep``'s ``_sentinel_resume`` self-heal);
* an intact resume skips with ZERO dispatches (per-tuple outcomes
  byte-identical — no ``TUPLE_DONE``, stores untouched);
* the ``quarantined_prior`` tier stays FIRST and UNGATED;
* the kill-switch arms mirror the sequential driver's (falsy → the honest
  pre-fix loss mode; junk → fail-loud pre-work).
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import brutex.engine.sweep_parallel as mod
import pytest
from brutex.data.models import CandleBar
from brutex.engine.sweep_audit import SweepAuditWriter, read_entries
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    count_sweep_tuples,
    iter_sweep_tuples,
    tuple_store_path,
)
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_supervisor import SweepGuards, grid_identity, tuple_label
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.engine.trial_stats import trial_stats_sidecar_path
from brutex.engine.tuple_complete import completion_sentinel_path
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Generator

    from brutex.engine.sweep_supervisor import SupervisedGridSummary

pytest.importorskip("polars")

_IST = timezone(timedelta(hours=5, minutes=30))
GUARD_ENV = "BRUTEX_SWEEP_RESUME_STORE_GUARD"


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(max(o, c) + 0.5, 2))),
        low=Decimal(str(round(min(o, c) - 0.5, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _make_bars() -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(4):
        price = 100.0
        for m in range(60):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


_BARS = _make_bars()


def _bars_for(_instrument: str, _tf: str) -> list[CandleBar]:
    return _BARS


def _grid() -> SweepGridConfig:
    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY, Side.SELL],
        k_values=[1, 2],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


@contextmanager
def _guards(base: Path, grid: SweepGridConfig, run_tag: str) -> Generator[SweepGuards, None, None]:
    """Fresh audit per run; DURABLE cache dir + checkpoint path across runs."""
    audit = SweepAuditWriter.open(base / f"sup-{run_tag}.jsonl")
    cache = SweepCache.open(base / "cache", bloom_capacity=10_000)
    checkpoint = SweepCheckpointStore(
        path=base / "ckpt.json",
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


def _run(base: Path, grid: SweepGridConfig, run_tag: str) -> SupervisedGridSummary:
    with (
        AuditWriter.open(base / f"ingest-{run_tag}.jsonl") as ingest,
        _guards(base, grid, run_tag) as guards,
    ):
        return mod.run_parallel_grid(
            grid,
            bars_for=_bars_for,
            root=base / "out",
            ingest_audit=ingest,
            guards=guards,
            workers=2,
        )


def _store_paths(base: Path, grid: SweepGridConfig) -> list[Path]:
    return [tuple_store_path(base / "out", t, grid) for t in iter_sweep_tuples(grid)]


def _trio(store: Path) -> tuple[Path, Path, Path]:
    return store, completion_sentinel_path(store), trial_stats_sidecar_path(store)


def _snapshot(paths: list[Path]) -> dict[Path, bytes]:
    return {p: p.read_bytes() for store in paths for p in _trio(store)}


class TestParallelGuardRecovery:
    def test_checkpoint_tier_deleted_stores_regenerate_in_worker(self, tmp_path: Path) -> None:
        """T15/T1 twin — deleted parquets regenerate byte-identically in-worker;
        the WARN carries the checkpoint tier label."""
        grid = _grid()
        s1 = _run(tmp_path, grid, "r1")
        assert s1.tuples_run == 4
        paths = _store_paths(tmp_path, grid)
        snap = _snapshot(paths)
        for i in (0, 2):
            paths[i].unlink()

        with capture_logs() as logs:
            s2 = _run(tmp_path, grid, "r2")
        assert s2.tuples_run == 2
        assert s2.skipped_resume == 2
        assert s2.store_guard_forced == 2
        assert s2.tuples_failed == 0
        for store in paths:
            for p in _trio(store):
                assert p.read_bytes() == snap[p]
        warns = [x for x in logs if x["event"] == "sweep_parallel.resume_store_guard_forced"]
        assert len(warns) == 2
        assert {w["tier"] for w in warns} == {"checkpoint"}
        assert all(w["missing"] == ["store"] for w in warns)
        # Emit-on-nonzero on the COMPLETE payload; and the heal CONVERGES.
        complete = [
            e for e in read_entries(tmp_path / "sup-r2.jsonl") if e.action == "SUPERVISOR_COMPLETE"
        ][-1]
        assert complete.payload["store_guard_forced"] == 2
        s3 = _run(tmp_path, grid, "r3")
        assert s3.tuples_run == 0
        assert s3.store_guard_forced == 0

    def test_cache_tier_deleted_store_regenerates(self, tmp_path: Path) -> None:
        """T15/T2 twin — checkpoint deleted (resume_from == 0): the cache tier
        alone is gated; the intact tuples still cache-skip."""
        grid = _grid()
        _run(tmp_path, grid, "r1")
        paths = _store_paths(tmp_path, grid)
        snap = _snapshot(paths)
        paths[1].unlink()
        (tmp_path / "ckpt.json").unlink()

        with capture_logs() as logs:
            s2 = _run(tmp_path, grid, "r2")
        assert s2.tuples_run == 1
        assert s2.skipped_cache == 3
        assert s2.skipped_resume == 0
        assert s2.store_guard_forced == 1
        for store in paths:
            for p in _trio(store):
                assert p.read_bytes() == snap[p]
        warns = [x for x in logs if x["event"] == "sweep_parallel.resume_store_guard_forced"]
        assert len(warns) == 1
        assert warns[0]["tier"] == "cache"

    def test_negative_control_intact_resume_zero_dispatches(self, tmp_path: Path) -> None:
        """T15/T3 twin — an intact tree resume-skips every tuple: no worker
        outcome (zero TUPLE_DONE lines), stores byte-untouched, no guard key
        on the COMPLETE payload, per-tuple RESUME lines byte-identical."""
        grid = _grid()
        _run(tmp_path, grid, "r1")
        paths = _store_paths(tmp_path, grid)
        snap = _snapshot(paths)

        s2 = _run(tmp_path, grid, "r2")
        assert s2.tuples_run == 0
        assert s2.skipped_resume == 4
        assert s2.store_guard_forced == 0
        for store in paths:
            for p in _trio(store):
                assert p.read_bytes() == snap[p]
        entries = read_entries(tmp_path / "sup-r2.jsonl")
        actions = [e.action for e in entries]
        assert actions.count("TUPLE_DONE") == 0
        assert actions.count("TUPLE_SKIPPED_RESUME") == 4
        resume_payloads = [e.payload for e in entries if e.action == "TUPLE_SKIPPED_RESUME"]
        for payload in resume_payloads:  # byte-identical pre-guard shape
            assert set(payload) == {"index", "tuple"}
        complete = [e for e in entries if e.action == "SUPERVISOR_COMPLETE"][-1]
        assert "store_guard_forced" not in complete.payload

    def test_quarantined_prior_tier_stays_first_and_ungated(self, tmp_path: Path) -> None:
        """The quarantine sidecar wins over the guard: a quarantined tuple with
        MISSING artifacts still classifies quarantined_prior — never forced."""
        grid = _grid()
        _run(tmp_path, grid, "r1")
        paths = _store_paths(tmp_path, grid)
        tuples = list(iter_sweep_tuples(grid))
        sweep_id = grid_identity(grid)
        mod.append_quarantine_entries(
            tmp_path / "out", sweep_id, [(0, tuple_label(tuples[0]))], reason="test-oom"
        )
        paths[0].unlink()  # its artifacts are ALSO missing — quarantine must win

        s2 = _run(tmp_path, grid, "r2")
        assert s2.tuples_quarantined_prior == 1
        assert s2.store_guard_forced == 0
        assert not paths[0].exists()  # the sidecar is the ONE skip lever


class TestParallelWarnDiscipline:
    def test_warn_cap_and_suppressed_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T14 twin — the capped WARN discipline: past the cap the per-tuple
        WARN is suppressed (the counter stays exact) and ONE summarising
        overflow line fires at run end. The cap is monkeypatched to 1 so the
        overflow arm is exercised with a cheap 2-deletion fixture (the
        sequential suite pins the real 20-cap arithmetic over 25 tuples)."""
        grid = _grid()
        _run(tmp_path, grid, "r1")
        paths = _store_paths(tmp_path, grid)
        for i in (0, 2):
            paths[i].unlink()
        monkeypatch.setattr(mod, "RESUME_STORE_GUARD_WARN_CAP", 1)
        with capture_logs() as logs:
            s2 = _run(tmp_path, grid, "r2")
        assert s2.store_guard_forced == 2  # counted, never per-row
        warns = [x for x in logs if x["event"] == "sweep_parallel.resume_store_guard_forced"]
        assert len(warns) == 1  # capped
        summaries = [
            x
            for x in logs
            if x["event"] == "sweep_parallel.resume_store_guard_forced_suppressed"
        ]
        assert len(summaries) == 1
        assert summaries[0]["suppressed"] == 1
        assert summaries[0]["total_forced"] == 2


class TestParallelKillSwitch:
    def test_falsy_restores_prefix_loss_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grid = _grid()
        _run(tmp_path, grid, "r1")
        paths = _store_paths(tmp_path, grid)
        paths[0].unlink()
        monkeypatch.setenv(GUARD_ENV, "0")
        s2 = _run(tmp_path, grid, "r2")
        assert s2.tuples_run == 0
        assert s2.skipped_resume == 4
        assert s2.store_guard_forced == 0
        assert not paths[0].exists()  # the honest OFF: the S5b loss mode returns

    def test_junk_fails_loud_before_any_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grid = _grid()
        monkeypatch.setenv(GUARD_ENV, "maybe")
        with pytest.raises(ValueError, match=GUARD_ENV):
            _run(tmp_path, grid, "r1")
        # Pre-work: no START line was ever appended.
        assert read_entries(tmp_path / "sup-r1.jsonl") == []
