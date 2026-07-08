"""G3/G4/G5 progress seams end-to-end (DEC-SWEEP-UNKILLABLE-CORE-001 WP-5).

* **G3** — the parallel driver streams live progress through the writer-actor
  ``on_progress`` tick and the run-core publish lands the file the dashboard
  bridge reads (monotone, terminal-honest).
* **G5** — ``read_progress`` enrichment serves the run's ``exit_status``
  through the EXISTING ``/api/v1/sweep-progress`` dashboard route with ZERO
  route edits (the route file is untouched by this change — proven by driving
  the real app).
* **G4 heal-region regression** — REPRODUCES the monotonic-vs-wall
  misjudgment (a LIVE run's progress read as "not running" by the OLD
  expression) then proves the fixed ``cmd_sweep_heal`` REFUSES to heal beside
  it, refuses fail-closed on a legacy payload, and proceeds for a dead run.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_exit_status import RunState, write_exit_status
from brutex.engine.sweep_heal import HealPlan, RecordedContext, write_heal_plan
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    count_sweep_tuples,
)
from brutex.engine.sweep_parallel import run_parallel_grid
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_supervisor import (
    SweepGuards,
    grid_identity,
    publish_progress,
    read_progress,
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _bars() -> list[CandleBar]:
    out: list[CandleBar] = []
    price = 100.0
    for m in range(60):
        close = price + 0.6 if m % 3 else price - 0.3
        ts = (datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(minutes=m)).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(str(round(price, 2))),
                high=Decimal(str(round(max(price, close) + 0.5, 2))),
                low=Decimal(str(round(min(price, close) - 0.5, 2))),
                close=Decimal(str(round(close, 2))),
                volume=100,
                open_interest=None,
            )
        )
        price = close
    return out


_BARS = _bars()


def _grid() -> SweepGridConfig:
    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY, Side.SELL],
        k_values=[1],
        condition_bits=[0, 1, 2],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


@contextmanager
def _guards(tmp: Path, grid: SweepGridConfig) -> Generator[SweepGuards, None, None]:
    audit = SweepAuditWriter.open(tmp / "sup.jsonl")
    cache = SweepCache.open(tmp / "cache", bloom_capacity=10_000)
    guards = SweepGuards(
        audit=audit,
        progress=SweepProgress(),
        checkpoint=SweepCheckpointStore(
            path=tmp / "ckpt.json",
            sweep_id=grid_identity(grid),
            total_combos=count_sweep_tuples(grid),
        ),
        cache=cache,
        watchdog=SweepWatchdog(),
    )
    try:
        yield guards
    finally:
        audit.close()
        cache.close()


# ────── G3: parallel live progress ──────


def test_g03_parallel_grid_publishes_monotone_progress(tmp_path: Path) -> None:
    grid = _grid()
    progress_path = tmp_path / "out" / "progress" / "sweep-progress.json"
    published: list[int] = []

    def _on_progress(snap: object) -> None:
        publish_progress(progress_path, snap, run_id=grid_identity(grid))  # type: ignore[arg-type]
        published.append(json.loads(progress_path.read_text())["evaluated"])

    with (
        AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        _guards(tmp_path, grid) as guards,
    ):
        summary = run_parallel_grid(
            grid,
            bars_for=lambda _i, _t: _BARS,
            root=tmp_path / "out",
            ingest_audit=ingest,
            guards=guards,
            workers=2,
            on_progress=_on_progress,
        )
    assert summary.tuples_run == 2
    assert published == sorted(published)  # monotone advance
    assert published[-1] == count_sweep_tuples(grid)
    final = json.loads(progress_path.read_text())
    assert final["run_id"] == grid_identity(grid)
    assert final["pid"] == os.getpid()
    assert isinstance(final["wall_time_utc"], float)


# ────── G5: exit-status through read_progress + the UNTOUCHED route ──────


def _write_run(tmp_path: Path) -> tuple[Path, str]:
    """A finished run's progress file + exit-status artifact (the run-core layout)."""
    root = tmp_path / "sweeps"
    run_id = "e" * 64
    progress_path = root / "progress" / "sweep-progress.json"
    progress_path.parent.mkdir(parents=True)
    write_exit_status(root, run_id=run_id, state=RunState.RUNNING, attempt=1, pid=os.getpid())
    write_exit_status(
        root, run_id=run_id, state=RunState.COMPLETE, exit_code=0, attempt=1, pid=os.getpid()
    )
    progress = SweepProgress()
    progress.start(enumerated=2)
    progress.advance(current="NIFTY/1minute")
    publish_progress(progress_path, progress.snapshot(), run_id=run_id, suspect=False)
    return progress_path, run_id


def test_g05_read_progress_serves_exit_status(tmp_path: Path) -> None:
    progress_path, run_id = _write_run(tmp_path)
    payload = read_progress(progress_path)
    assert payload["run_id"] == run_id
    status = payload["exit_status"]
    assert status is not None
    assert status["state"] == "complete"
    assert status["exit_code"] == 0
    assert status["wall_ended_utc"] is not None
    assert payload["suspect"] is False
    # Every pre-existing snapshot key is byte-unchanged.
    assert payload["enumerated"] == 2
    assert payload["evaluated"] == 1


def test_g05_route_serves_exit_status_with_zero_route_edits(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from brutex.dashboard.app import DashboardState, build_app
    from fastapi.testclient import TestClient

    progress_path, run_id = _write_run(tmp_path)
    state = DashboardState()
    state.sweep_progress_path = progress_path
    with TestClient(build_app(state)) as client:
        resp = client.get("/api/v1/sweep-progress")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["exit_status"]["state"] == "complete"
    assert body["exit_status"]["exit_code"] == 0
    assert body["suspect"] is False


# ────── G4: the heal-region monotonic-vs-wall regression ──────


def _empty_plan(root: Path) -> Path:
    return write_heal_plan(
        root,
        HealPlan(
            created_utc="20260707T000000Z",
            grid_identity="a" * 64,
            context=RecordedContext(
                data_snapshot_hash="snap0001",
                indicator_version="ind-v1",
                code_commit_sha="deadbeef",
            ),
            n_expected_stores=1,
            entries=(),
        ),
    )


def _live_progress_payload() -> dict[str, object]:
    """EXACTLY what a live run-core sweep publishes: monotonic ``updated_at``
    (the pre-fix trap) + the NEW wall/pid honesty keys."""
    return {
        "state": "running",
        "running": True,
        "enumerated": 10,
        "evaluated": 3,
        "updated_at": time.monotonic(),  # MONOTONIC — small vs epoch seconds
        "wall_time_utc": time.time(),
        "pid": os.getpid(),
        "run_id": "a" * 64,
    }


def _cmd_heal(lake: Path, plan: Path) -> int:
    from brutex.cli import cmd_sweep_heal

    return cmd_sweep_heal(
        plan=plan,
        lake_root=lake,
        results_root=None,
        execute=False,
        allow_mass_quarantine=False,
    )


def test_g04_heal_refuses_live_run_the_old_expression_misjudged(tmp_path: Path) -> None:
    from brutex.cli import EXIT_HEAL_REFUSED

    lake = tmp_path / "lake"
    out = lake / "sweeps"
    plan = _empty_plan(out)
    progress_path = out / "progress" / "sweep-progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _live_progress_payload()
    progress_path.write_text(json.dumps(payload), encoding="utf-8")

    # REPRODUCE the pre-fix defect: `updated_at` is time.monotonic()-based
    # (sweep_progress.py), so the OLD `time.time() - updated_at < 600` freshness
    # read this LIVE run as stale ⇒ sweep_running=False ⇒ quarantinable.
    old_fresh = (time.time() - float(payload["updated_at"] or 0.0)) < 600.0  # type: ignore[arg-type]
    assert old_fresh is False  # the misjudgment, reproduced verbatim

    # The FIX: the real cmd_sweep_heal now judges wall_time_utc + pid and
    # REFUSES to heal beside the live run (C6, exit EXIT_HEAL_REFUSED).
    assert _cmd_heal(lake, plan) == EXIT_HEAL_REFUSED


def test_g04_heal_refuses_legacy_payload_fail_closed(tmp_path: Path) -> None:
    from brutex.cli import EXIT_HEAL_REFUSED

    lake = tmp_path / "lake"
    out = lake / "sweeps"
    plan = _empty_plan(out)
    progress_path = out / "progress" / "sweep-progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    # A LEGACY payload (no wall/pid evidence) → UNKNOWN → refuse on doubt.
    progress_path.write_text(
        json.dumps({"running": True, "updated_at": time.monotonic()}), encoding="utf-8"
    )
    assert _cmd_heal(lake, plan) == EXIT_HEAL_REFUSED


def test_g04_heal_proceeds_for_dead_run_and_missing_file(tmp_path: Path) -> None:
    from brutex.cli import EXIT_OK

    lake = tmp_path / "lake"
    out = lake / "sweeps"
    plan = _empty_plan(out)
    # No progress file at all — nothing to protect, the dry-run proceeds.
    assert _cmd_heal(lake, plan) == EXIT_OK
    # A cleanly-finished run (running=False + wall/pid) also proceeds.
    progress_path = out / "progress" / "sweep-progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _live_progress_payload()
    payload["running"] = False
    progress_path.write_text(json.dumps(payload), encoding="utf-8")
    assert _cmd_heal(lake, plan) == EXIT_OK
    # A dead-pid leftover (machine-death stale file) proceeds too.
    payload["running"] = True
    payload["pid"] = 2**22 + 1  # beyond the default pid_max — never alive
    progress_path.write_text(json.dumps(payload), encoding="utf-8")
    assert _cmd_heal(lake, plan) == EXIT_OK
