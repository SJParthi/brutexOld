"""Unit tests for :mod:`brutex.engine.sweep_run_core` (DEC-SWEEP-UNKILLABLE-CORE-001 WP-5).

R7/R16 composition (CM ordering + fail-soft exits) plus the targeted branch
matrix: G1 quarantine composition, G6 single-flight refusal, G9 keep-awake,
G12 checkpoint pre-load, R39 tripwire fan-out, R41 recorder/milestones, R45
diffcheck closure (budget + never-adopt), D-4 stage-3 cell loop, D-12 SUSPECT
latch, and the finalize state machine. 100% line+branch standalone
(``--cov=brutex.engine.sweep_run_core --cov-branch --cov-fail-under=100``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar
from brutex.engine import sweep_run_core as mod
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_diffcheck import DiffCheckMismatch
from brutex.engine.sweep_exit_status import read_exit_status
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    count_sweep_tuples,
    iter_sweep_tuples,
    tuple_store_path,
)
from brutex.engine.sweep_progress import SweepProgressSnapshot
from brutex.engine.sweep_run import SweepRunSummary
from brutex.engine.sweep_run_core import (
    RunCoreHandles,
    Stage3CellStampError,
    per_tuple_diffcheck_budget,
    sweep_run_core,
    tuple_record_stride,
)
from brutex.engine.sweep_singleflight import SweepAlreadyRunningError
from brutex.engine.sweep_supervisor import grid_identity
from brutex.metrics.cascade import FilterStage
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.observability.flight_recorder import iter_run_records
from brutex.observability.run_manifest import load_manifest
from brutex.observability.run_tripwires import TripwireConfig, TripwireEvent
from brutex.observability.stage_stamp import StampVerdict, content_hash_of, write_stage_stamp
from brutex.strategies.types import Side
from structlog.testing import capture_logs

#: Traceability tokens (D-6 convention; folded into WP-6's mechanized matrix).
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_g06_second_core_enter_refused_before_any_writer": ("G6", "R16"),
    "test_g09_keep_awake_entered_once": ("G9",),
    "test_g12_corrupt_checkpoint_quarantined_at_enter": ("G12",),
    "test_g01_store_quarantine_moves_trio_and_records": ("G1",),
    "test_r39_on_trip_records_and_latches_suspect": ("R39",),
    "test_r45_diffcheck_closure_checks_real_masks": ("R45",),
    "test_r41_milestones_fire_once_each": ("R41",),
    "test_r7_clean_run_finalizes_complete": ("R7", "R16"),
}

_IST = timezone(timedelta(hours=5, minutes=30))


def _bars() -> list[CandleBar]:
    """A small deterministic upward-drift session (2 days × 40 minutes)."""
    out: list[CandleBar] = []
    for d in range(2):
        price = 100.0
        for m in range(40):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
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


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": [0, 1, 2],
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


@dataclass
class _SpyAlerter:
    """Duck-typed alerter spy recording exactly the calls the core makes."""

    suspects: list[tuple[str, str, str]]
    milestones: list[tuple[str, int, int, int]]
    halts: list[tuple[str, str]]

    def run_suspect(self, *, run_id: str, tripwire: str, detail: str) -> bool:
        self.suspects.append((run_id, tripwire, detail))
        return True

    def sweep_milestone(self, *, run_id: str, pct: int, tuples_done: int, total: int) -> bool:
        self.milestones.append((run_id, pct, tuples_done, total))
        return True

    def sweep_halted(self, *, run_id: str, reason: str) -> bool:
        self.halts.append((run_id, reason))
        return True


def _spy() -> _SpyAlerter:
    return _SpyAlerter(suspects=[], milestones=[], halts=[])


@contextmanager
def _core(
    root: Path,
    grid: SweepGridConfig | None = None,
    *,
    diffcheck_k: int = 0,
    **kw: Any,
) -> Generator[tuple[RunCoreHandles, _SpyAlerter], None, None]:
    """Enter a run core over *root* with deterministic (env-free) knobs."""
    g = grid if grid is not None else _grid()
    spy = _spy()
    with sweep_run_core(
        results_root=root,
        grid=g,
        alerter=cast("SweepAlerter", spy),
        entry_label="unit test",
        tripwire_config=TripwireConfig(),
        diffcheck_k=diffcheck_k,
        **kw,
    ) as core:
        yield core, spy


def _records(root: Path, run_id: str) -> list[dict[str, object]]:
    return list(iter_run_records(root, run_id))


def _kinds(root: Path, run_id: str) -> list[str]:
    return [str(r["kind"]) for r in _records(root, run_id)]


def _snapshot(*, enumerated: int, evaluated: int, skipped: int = 0) -> SweepProgressSnapshot:
    return SweepProgressSnapshot(
        state="running",
        enumerated=enumerated,
        evaluated=evaluated,
        survivors=0,
        skipped=skipped,
        current="NIFTY/1minute",
        detail="",
        started_at=1.0,
        updated_at=2.0,
    )


def _summary(**overrides: object) -> SweepRunSummary:
    base: dict[str, object] = {
        "enumerated": 3,
        "conflict_pruned": 0,
        "skipped_dedup": 0,
        "evaluated": 3,
        "ranked": 1,
        "persisted": 1,
        "store_content_hash": "ab" * 32,
    }
    base.update(overrides)
    return SweepRunSummary(**base)  # type: ignore[arg-type]


# ────── pure helpers ──────


def test_tuple_record_stride_exact_values() -> None:
    assert tuple_record_stride(0) == 1
    assert tuple_record_stride(1) == 1
    assert tuple_record_stride(10_000) == 1
    assert tuple_record_stride(10_001) == 2
    assert tuple_record_stride(100_000) == 10
    assert tuple_record_stride(-5) == 1


def test_per_tuple_diffcheck_budget_exact_values() -> None:
    assert per_tuple_diffcheck_budget(0, 10) == 0
    assert per_tuple_diffcheck_budget(-1, 10) == 0
    assert per_tuple_diffcheck_budget(200, 4) == 50
    assert per_tuple_diffcheck_budget(3, 10) == 1
    assert per_tuple_diffcheck_budget(8, 0) == 8


def test_conditions_hex() -> None:
    assert mod._conditions_hex([0, 1, 2, 3, 4]) == "1f"
    assert mod._conditions_hex([]) == "0"


def test_selection_fragment_exact_keys_and_extra_merge() -> None:
    grid = _grid()
    frag = mod._selection_fragment(grid, None)
    assert frag["symbols"] == ["NIFTY"]
    assert frag["intervals"] == ["1minute"]
    assert frag["directions"] == ["BUY"]
    assert frag["k"] == [1]
    assert frag["sl"] == [80]
    assert frag["tp"] == [50]
    assert frag["conditions_hex"] == "7"
    assert frag["mode"] == "A"
    merged = mod._selection_fragment(grid, {"exchange": "NSE", "start": "2026-01-01"})
    assert merged["exchange"] == "NSE"
    assert merged["start"] == "2026-01-01"


def test_is_halt_arms() -> None:
    assert mod._is_halt(KeyboardInterrupt()) is True
    assert mod._is_halt(LakeWriteHalt("disk_full")) is True
    assert mod._is_halt(ValueError("x")) is False


# ────── enter / exit lifecycle ──────


def test_r7_clean_run_finalizes_complete(tmp_path: Path) -> None:
    grid = _grid()
    run_id = grid_identity(grid)
    with _core(tmp_path, grid) as (core, _unused_spy):
        assert core.run_id == run_id
        assert core.attempt == 1
        assert core.suspect is False
        assert core.exit_code is None
    status = read_exit_status(tmp_path, run_id)
    assert status is not None
    assert status["state"] == "complete"
    assert status["exit_code"] == 0
    assert status["attempt"] == 1
    assert status["suspect"] is False
    kinds = _kinds(tmp_path, run_id)
    assert kinds[0] == "RUN_START"
    assert kinds[-1] == "RUN_END"
    run_dir = tmp_path / "runs" / run_id
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "manifest.json.stamp.json").exists()
    assert (run_dir / "chain-map.json").exists()
    assert (run_dir / "diagnostic-bundle.zip").exists()
    manifest = load_manifest(tmp_path, run_id)
    assert manifest.exit_code == 0
    assert manifest.suspect is False
    assert manifest.recorder_degraded is False


def test_runcore_writes_no_parquet_under_runs_dir(tmp_path: Path) -> None:
    """Register Row 4 hazard pin (DEC-SWEEP-UNKILLABLE-CORE-001, hostile-review round 1).

    The run-core artifacts live under ``<results_root>/runs/<run_id>/`` which
    sits inside the ``.parquet``-globbed sweeps tree. The Ruling-4 relocation to
    the ``sweep_runs/`` SIBLING root is a documented cross-phase follow-up; until
    it lands the /results-500 hazard is kept structurally DEAD by writing ONLY
    JSON/JSONL/extensionless artifacts here — NEVER a ``.parquet``. This pins
    that invariant so a future run-core write can never silently reopen the
    ``results_report`` / ``sweep_results_view`` / ``sweep_verify`` glob crash.
    """
    grid = _grid()
    run_id = grid_identity(grid)
    with _core(tmp_path, grid):
        pass
    run_dir = tmp_path / "runs" / run_id
    assert run_dir.is_dir()
    # The CLEAN-run finalize wrote the full artifact set — manifest + chain-map +
    # the diagnostic bundle — yet none is a .parquet.
    assert (run_dir / "diagnostic-bundle.zip").exists()
    assert list(run_dir.rglob("*.parquet")) == []


def test_runcore_writes_no_parquet_under_runs_dir_on_failure(tmp_path: Path) -> None:
    """Register Row 4 hazard pin — the FAILURE/diag-bundle finalize path (round 3).

    The round-1 pin only exercised the clean ``with _core: pass`` finalize. The
    hazard the ruling guards against is a run-core write reopening the
    ``.parquet`` glob crash — and the diagnostic bundle + failure artifacts are
    written on the EXCEPTION finalize (exactly when an operator forensically
    reads the run). This drives a raising body so the full failure finalize
    runs (exit-status ``failed`` + the diag bundle) and asserts STILL no
    ``.parquet`` lands under ``runs/<run_id>/``.
    """
    grid = _grid()
    run_id = grid_identity(grid)
    spy = _spy()
    with (
        pytest.raises(ValueError, match="boom"),
        sweep_run_core(
            results_root=tmp_path,
            grid=grid,
            alerter=cast("SweepAlerter", spy),
            entry_label="t",
            tripwire_config=TripwireConfig(),
            diffcheck_k=0,
        ),
    ):
        raise ValueError("boom")
    run_dir = tmp_path / "runs" / run_id
    assert run_dir.is_dir()
    status = read_exit_status(tmp_path, run_id)
    assert status is not None
    assert status["state"] == "failed"
    assert (run_dir / "diagnostic-bundle.zip").exists()  # bundle written on failure
    assert list(run_dir.rglob("*.parquet")) == []


def test_attempt_increments_from_prior_and_junk_resets(tmp_path: Path) -> None:
    grid = _grid()
    run_id = grid_identity(grid)
    with _core(tmp_path, grid):
        pass
    with _core(tmp_path, grid) as (core, _s):
        assert core.attempt == 2
    # A junk prior `attempt` honestly resets to 1 (never fabricated arithmetic).
    path = tmp_path / "runs" / run_id / "exit-status.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["attempt"] = "junk"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with _core(tmp_path, grid) as (core, _s):
        assert core.attempt == 1


def test_resume_after_suspect_never_launders_to_complete(tmp_path: Path) -> None:
    """MAJOR 3 (hostile-review round 1): a prior SUSPECT latch is sticky across resume.

    ``run_id = grid_identity`` is timestamp-free, so a re-run resumes into the
    SAME ``runs/<run_id>/`` and resume-SKIPS the already-complete offending
    tuple — the diffcheck/tripwire that latched SUSPECT never re-fires. The run
    core must carry the prior SUSPECT forward so it can never overwrite
    SUSPECT_COMPLETE with a clean COMPLETE.
    """
    grid = _grid()
    run_id = grid_identity(grid)
    # First attempt latches SUSPECT (a diffcheck-mismatch class defect) and
    # finalizes SUSPECT_COMPLETE.
    with _core(tmp_path, grid) as (core, _s):
        core.mark_suspect("diffcheck_mismatch", "mask abcd: net_pnl_paisa")
    first = read_exit_status(tmp_path, run_id)
    assert first is not None
    assert first["state"] == "suspect_complete"
    assert first["suspect"] is True
    # A resume (fresh handles, NO new tripwire) must stay SUSPECT.
    with _core(tmp_path, grid) as (core, _s):
        assert core.suspect is True  # seeded from the prior attempt's latch
        assert core.attempt == 2
    second = read_exit_status(tmp_path, run_id)
    assert second is not None
    assert second["state"] == "suspect_complete"  # never laundered to "complete"
    assert second["suspect"] is True
    # The manifest + recorder agree the resumed run is still SUSPECT.
    manifest = load_manifest(tmp_path, run_id)
    assert manifest.suspect is True
    resume_records = [r for r in _records(tmp_path, run_id) if r["kind"] == "SUSPECT"]
    assert any(
        cast("dict[str, object]", r["payload"]).get("reason") == "resumed_suspect"
        for r in resume_records
    )


def test_resume_folds_recorder_suspect_when_exit_status_torn(tmp_path: Path) -> None:
    """MAJOR (round 2): a torn exit-status ``suspect`` is caught by the recorder fold.

    ``mark_suspect`` now writes the durable exit-status ``suspect=True`` FIRST,
    but as a defence-in-depth belt the resume ALSO folds the durable
    flight-recorder ``SUSPECT`` record. Simulate a torn/laundered exit-status
    (``suspect`` flipped back to ``false``) and prove the resume STILL reads the
    run as SUSPECT off the recorder — never laundering to a clean COMPLETE.
    """
    grid = _grid()
    run_id = grid_identity(grid)
    with _core(tmp_path, grid) as (core, _s):
        core.mark_suspect("diffcheck_mismatch", "mask abcd: net_pnl_paisa")
    # Corrupt/launder the single fail-soft exit-status artifact: flip suspect off.
    status_path = tmp_path / "runs" / run_id / "exit-status.json"
    doc = json.loads(status_path.read_text(encoding="utf-8"))
    doc["suspect"] = False
    status_path.write_text(json.dumps(doc), encoding="utf-8")
    torn = read_exit_status(tmp_path, run_id)
    assert torn is not None
    assert torn["suspect"] is False  # torn state
    # The durable recorder SUSPECT record is the belt.
    from brutex.observability.flight_recorder import recorder_has_suspect

    assert recorder_has_suspect(tmp_path, run_id) is True
    with _core(tmp_path, grid) as (core, _s):
        assert core.suspect is True  # folded from the recorder despite the torn flag
    second = read_exit_status(tmp_path, run_id)
    assert second is not None
    assert second["state"] == "suspect_complete"  # re-stamped, never laundered
    assert second["suspect"] is True


def test_mark_suspect_writes_exit_status_before_alert(tmp_path: Path) -> None:
    """The durable exit-status ``suspect=True`` lands even if the alerter blocks/raises.

    ``mark_suspect`` fans exit-status FIRST, so a raising (or slow) Telegram
    publish can never leave a resume reading ``suspect=false``.
    """
    grid = _grid()
    run_id = grid_identity(grid)

    class _RaisingAlerter:
        def run_suspect(self, **_kw: Any) -> bool:
            raise RuntimeError("telegram down")

    spy = _spy()
    with sweep_run_core(
        results_root=tmp_path,
        grid=grid,
        alerter=cast("SweepAlerter", spy),
        entry_label="unit test",
        tripwire_config=TripwireConfig(),
        diffcheck_k=0,
    ) as core:
        core._alerter = cast("SweepAlerter", _RaisingAlerter())
        core.mark_suspect("tripwire", "some detail")  # alerter raises, guarded
        status = read_exit_status(tmp_path, run_id)
        assert status is not None
        assert status["suspect"] is True  # written before the raising alert


def test_record_worker_quarantine_records_flight(tmp_path: Path) -> None:
    """G1 (round 2): the parallel-worker quarantine adapter logs a QUARANTINE record."""
    grid = _grid()
    with _core(tmp_path, grid) as (core, _s):
        core.record_worker_quarantine(".quarantine/runtime/abc-1", "ResultsStoreError: bad")
        run_id = core.run_id
    rows = [r for r in _records(tmp_path, run_id) if r["kind"] == "QUARANTINE"]
    assert len(rows) == 1
    payload = cast("dict[str, Any]", rows[0]["payload"])
    assert payload["what"] == "tuple_store"
    assert payload["path_rel"] == ".quarantine/runtime/abc-1"
    assert payload["reason"] == "ResultsStoreError: bad"


def test_forensics_mirrored_to_internal_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 15 (round 2): the R40 forensics are duplicated to the internal-disk mirror."""
    mirror_root = tmp_path / "internal"
    monkeypatch.setenv("BRUTEX_RUN_MIRROR_ROOT", str(mirror_root))
    grid = _grid()
    run_id = grid_identity(grid)
    with _core(tmp_path, grid) as (_core_h, _s):
        pass
    mirror = mirror_root / run_id
    assert (mirror / "manifest.json").is_file()
    assert (mirror / "exit-status.json").is_file()
    assert (mirror / "flight-recorder.jsonl").is_file()
    # The primary copy on the results root is untouched.
    assert (tmp_path / "runs" / run_id / "manifest.json").is_file()


def test_forensics_mirror_disabled_when_root_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty BRUTEX_RUN_MIRROR_ROOT DISABLES the mirror (no internal write)."""
    monkeypatch.setenv("BRUTEX_RUN_MIRROR_ROOT", "")
    assert mod.mirror_run_forensics(tmp_path, "deadbeef") is None


def test_g06_second_core_enter_refused_before_any_writer(tmp_path: Path) -> None:
    grid = _grid()
    with _core(tmp_path, grid), pytest.raises(SweepAlreadyRunningError), _core(tmp_path, grid):
        pytest.fail("the second core must never enter")  # pragma: no cover


def test_g09_keep_awake_entered_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered: list[str] = []

    @contextmanager
    def _fake_keep_awake(reason: str) -> Generator[None, None, None]:
        entered.append(reason)
        yield

    monkeypatch.setattr(mod, "keep_awake", _fake_keep_awake)
    with _core(tmp_path):
        pass
    assert entered == ["unit test"]


def test_exception_finalizes_failed_and_alerts(tmp_path: Path) -> None:
    grid = _grid()
    run_id = grid_identity(grid)
    spy = _spy()
    with (
        pytest.raises(ValueError, match="boom"),
        sweep_run_core(
            results_root=tmp_path,
            grid=grid,
            alerter=cast("SweepAlerter", spy),
            entry_label="t",
            tripwire_config=TripwireConfig(),
            diffcheck_k=0,
        ),
    ):
        raise ValueError("boom")
    status = read_exit_status(tmp_path, run_id)
    assert status is not None
    assert status["state"] == "failed"
    assert status["exit_code"] == 1
    assert status["error_type"] == "ValueError"
    assert spy.halts == [(run_id, "ValueError")]
    assert _kinds(tmp_path, run_id)[-1] == "HALT"


def test_keyboard_interrupt_halts_without_alert(tmp_path: Path) -> None:
    grid = _grid()
    run_id = grid_identity(grid)
    spy = _spy()
    with (
        pytest.raises(KeyboardInterrupt),
        sweep_run_core(
            results_root=tmp_path,
            grid=grid,
            alerter=cast("SweepAlerter", spy),
            entry_label="t",
            tripwire_config=TripwireConfig(),
            diffcheck_k=0,
        ),
    ):
        raise KeyboardInterrupt
    status = read_exit_status(tmp_path, run_id)
    assert status is not None
    assert status["state"] == "halted"
    assert spy.halts == []  # an operator Ctrl-C never alerts


def test_lake_write_halt_maps_to_halted_and_alerts(tmp_path: Path) -> None:
    grid = _grid()
    run_id = grid_identity(grid)
    spy = _spy()
    with (
        pytest.raises(LakeWriteHalt),
        sweep_run_core(
            results_root=tmp_path,
            grid=grid,
            alerter=cast("SweepAlerter", spy),
            entry_label="t",
            tripwire_config=TripwireConfig(),
            diffcheck_k=0,
        ),
    ):
        raise LakeWriteHalt("disk_full")
    status = read_exit_status(tmp_path, run_id)
    assert status is not None
    assert status["state"] == "halted"
    assert spy.halts == [(run_id, "LakeWriteHalt")]


def test_exit_code_honoured_on_clean_and_failed_exit(tmp_path: Path) -> None:
    grid = _grid()
    run_id = grid_identity(grid)
    with _core(tmp_path, grid) as (core, _s):
        core.set_exit_code(3)
        assert core.exit_code == 3
    status = read_exit_status(tmp_path, run_id)
    assert status is not None and status["exit_code"] == 3
    # The failure arm keeps a caller-set code too.
    spy = _spy()
    # PT012 suppressed: the exception MUST propagate through the run-core CM
    # AFTER set_exit_code ran — the two statements ARE the scenario.
    with (  # noqa: PT012
        pytest.raises(RuntimeError),
        sweep_run_core(
            results_root=tmp_path,
            grid=grid,
            alerter=cast("SweepAlerter", spy),
            entry_label="t",
            tripwire_config=TripwireConfig(),
            diffcheck_k=0,
        ) as core,
    ):
        core.set_exit_code(7)
        raise RuntimeError("late")
    status = read_exit_status(tmp_path, run_id)
    assert status is not None and status["exit_code"] == 7


def test_suspect_complete_state_on_clean_suspect_run(tmp_path: Path) -> None:
    grid = _grid()
    run_id = grid_identity(grid)
    with _core(tmp_path, grid) as (core, _s):
        core.mark_suspect("unit_reason", "detail")
        assert core.suspect is True
    status = read_exit_status(tmp_path, run_id)
    assert status is not None
    assert status["state"] == "suspect_complete"
    assert status["suspect"] is True
    manifest = load_manifest(tmp_path, run_id)
    assert manifest.suspect is True
    assert manifest.suspect_reasons == ("unit_reason",)


# ────── SUSPECT fan-out (D-12) ──────


def test_mark_suspect_fans_out_once(tmp_path: Path) -> None:
    with _core(tmp_path) as (core, spy):
        core.mark_suspect("first", "d1")
        core.mark_suspect("second", "d2")
        assert [s[1] for s in spy.suspects] == ["first"]  # ONE fan-out
        run_id = core.run_id
    records = _records(tmp_path, run_id)
    assert sum(1 for r in records if r["kind"] == "SUSPECT") == 1
    manifest = load_manifest(tmp_path, run_id)
    assert manifest.suspect_reasons == ("first", "second")


def test_r39_on_trip_records_and_latches_suspect(tmp_path: Path) -> None:
    event = TripwireEvent(
        ts_utc=123.0,
        name="count_reconciliation",
        severity="critical",
        detail="d",
        values={"x": 1.0},
    )
    with _core(tmp_path) as (core, spy):
        core._on_trip(event)
        assert core.suspect is True
        assert spy.suspects == [(core.run_id, "count_reconciliation", "d")]
        run_id = core.run_id
    trip = [r for r in _records(tmp_path, run_id) if r["kind"] == "TRIPWIRE"]
    assert len(trip) == 1
    payload = cast("dict[str, Any]", trip[0]["payload"])
    assert payload["name"] == "count_reconciliation"
    assert payload["severity"] == "critical"
    assert payload["values"] == {"x": 1.0}
    manifest = load_manifest(tmp_path, run_id)
    assert len(manifest.tripwires) == 1
    assert manifest.tripwires[0]["name"] == "count_reconciliation"


def test_on_mismatch_records_and_marks_suspect(tmp_path: Path) -> None:
    mismatch = DiffCheckMismatch(
        tuple_digest="ab" * 8, mask_hex="0" * 31 + "1", field="pnl_paisa", prod="1", ref="2"
    )
    with _core(tmp_path) as (core, _s):
        core._on_mismatch(mismatch)
        assert core.suspect is True
        run_id = core.run_id
    rows = [r for r in _records(tmp_path, run_id) if r["kind"] == "DIFFCHECK_MISMATCH"]
    assert len(rows) == 1
    assert cast("dict[str, Any]", rows[0]["payload"])["field"] == "pnl_paisa"


# ────── stages + chain map ──────


def test_stage_transitions_time_manifest_and_write_chain_map(tmp_path: Path) -> None:
    with _core(tmp_path) as (core, _s):
        core.stage("sweep")
        assert core._stage == "sweep"
        run_id = core.run_id
    manifest = load_manifest(tmp_path, run_id)
    assert set(manifest.per_stage_timings) == {"preflight", "sweep"}
    chain = json.loads((tmp_path / "runs" / run_id / "chain-map.json").read_text())
    assert chain["schema_version"] == 1
    assert chain["run_id"] == run_id
    kinds = _kinds(tmp_path, run_id)
    assert "STAGE_START" in kinds
    assert "STAGE_END" in kinds


def test_progress_payload_view_none_without_snapshot(tmp_path: Path) -> None:
    with _core(tmp_path) as (core, _s):
        assert core._progress_payload_view() is None
        core._last_snapshot = {"running": True}
        view = core._progress_payload_view()
        assert view == {"running": True, "run_id": core.run_id, "stage": "preflight"}


# ────── grid_done + stage-2 aggregate ──────


def test_grid_done_folds_manifest_doors_and_dedup(tmp_path: Path) -> None:
    result = SimpleNamespace(
        evaluated=5,
        enumerated=9,
        tuples_run=1,
        tuples_failed=0,
        skipped_cache=1,
        skipped_resume=2,
        tuples_skipped_no_bars=1,
    )
    with _core(tmp_path) as (core, _s):
        core.grid_done(result)
        run_id = core.run_id
    manifest = load_manifest(tmp_path, run_id)
    assert manifest.actual["trials"] == 5
    assert manifest.actual["enumerated"] == 9
    assert manifest.dedup_skips == 3
    assert manifest.dedup_skips_detail == {"cache": 1, "resume": 2, "sentinel": 0}
    assert manifest.per_door_kills == {
        "no_bars": 1,
        "cache_skip": 1,
        "resume_skip": 2,
        "min_hits": 0,
    }
    # No sentinels on disk → the aggregate carries zero pairs but IS stamped.
    stage2 = tmp_path / "runs" / run_id / "stage2"
    assert json.loads(stage2.read_text()) == {"pairs": []}
    assert (tmp_path / "runs" / run_id / "stage2.stamp.json").exists()


def test_grid_done_stage2_aggregate_reads_real_sentinel(tmp_path: Path) -> None:
    from brutex.engine.tuple_complete import (
        TupleCompletionSentinel,
        completion_sentinel_path,
        write_completion_sentinel,
    )

    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    sentinel_path = completion_sentinel_path(tuple_store_path(tmp_path, tuple_, grid))
    sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    write_completion_sentinel(
        sentinel_path,
        TupleCompletionSentinel(
            tuple_digest="ab" * 8,
            store_content_hash="cd" * 32,
            sidecar_written=False,
            n_marked=0,
        ),
    )
    result = SimpleNamespace(
        evaluated=1,
        enumerated=1,
        tuples_run=1,
        tuples_failed=0,
        skipped_cache=0,
        skipped_resume=0,
        tuples_skipped_no_bars=0,
    )
    with _core(tmp_path, grid) as (core, _s):
        core.grid_done(result)
        assert core._link_stamps["sweep"] is StampVerdict.OK
        run_id = core.run_id
    stage2 = tmp_path / "runs" / run_id / "stage2"
    assert json.loads(stage2.read_text()) == {"pairs": [["ab" * 8, "cd" * 32]]}


def test_grid_done_stage2_write_failure_still_satisfies_prerequisite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR 4 (hostile-review round 1): a stage-2 aggregate write ENOSPC must NOT flip SUSPECT.

    The forensic aggregate stamp is fail-soft; its failure must never withhold
    the ``stage2`` tripwire prerequisite (a subsequent ``stage("stage3")`` would
    else false-trip STAGE_INVARIANT → SUSPECT). The prerequisite is the grid's
    CORRECTNESS milestone, recorded independently of the guarded write.
    """
    result = SimpleNamespace(
        evaluated=1,
        enumerated=1,
        tuples_run=1,
        tuples_failed=0,
        skipped_cache=0,
        skipped_resume=0,
        tuples_skipped_no_bars=0,
    )

    def _boom(self: object) -> None:
        raise OSError("simulated ENOSPC on the stage-2 aggregate write")

    monkeypatch.setattr(mod.RunCoreHandles, "_write_stage2_aggregate", _boom)
    with _core(tmp_path) as (core, _s):
        core.grid_done(result)
        assert "stage2" in core._stamps_present  # prerequisite met despite write failure
        assert "sweep" not in core._link_stamps  # durable link evidence withheld
        # The next stage entry must NOT trip the stage invariant → no SUSPECT.
        core.stage("stage3")
        assert core.suspect is False


# ────── stage-3 D-4 loop ──────


def _cell_report(cells: list[Path], snapshot: str | None) -> Any:
    return SimpleNamespace(
        cells=tuple(SimpleNamespace(path=p, rows=1, tuple_=None) for p in cells),
        train_snapshot_hash=snapshot,
    )


def test_run_stage3_disabled_pass_through(tmp_path: Path) -> None:
    with _core(tmp_path) as (core, _s):
        report = _cell_report([], None)
        assert core.run_stage3(lambda: report) is report
        assert "stage3" in core._stamps_present  # vacuous prerequisite
        assert "stage3" not in core._link_stamps


def test_run_stage3_valid_cells_stamped(tmp_path: Path) -> None:
    cell = tmp_path / "cell.parquet"
    cell.write_bytes(b"cell-bytes")
    with _core(tmp_path) as (core, _s):
        report = _cell_report([cell], "ab" * 32)
        assert core.run_stage3(lambda: report) is report
        assert core._link_stamps["stage3"] is StampVerdict.OK
    assert (tmp_path / "cell.parquet.stamp.json").exists()


def test_run_stage3_invalid_discards_and_reruns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = tmp_path / "cell.parquet"
    cell.write_bytes(b"cell-bytes")
    verdicts = iter([(cell,), ()])
    monkeypatch.setattr(
        mod, "validate_stage3_cells", lambda cells, *, run_id, snapshot_hash: next(verdicts)
    )
    calls = {"n": 0}

    def _factory() -> Any:
        calls["n"] += 1
        cell.write_bytes(b"cell-bytes")  # the idempotent pass regenerates it
        return _cell_report([cell], "ab" * 32)

    with _core(tmp_path) as (core, _s):
        core.run_stage3(_factory)
        run_id = core.run_id
    assert calls["n"] == 2
    # The discarded forensic copy exists alongside the regenerated cell.
    assert (tmp_path / "cell.parquet.discarded.0").exists()
    assert cell.exists()
    quarantined = [r for r in _records(tmp_path, run_id) if r["kind"] == "QUARANTINE"]
    assert len(quarantined) == 1


def test_run_stage3_still_invalid_raises_hard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = tmp_path / "cell.parquet"
    monkeypatch.setattr(
        mod, "validate_stage3_cells", lambda cells, *, run_id, snapshot_hash: (cell,)
    )

    def _factory() -> Any:
        cell.write_bytes(b"cell-bytes")
        return _cell_report([cell], "ab" * 32)

    with (
        pytest.raises(Stage3CellStampError, match="still invalid"),
        _core(tmp_path) as (
            core,
            _s,
        ),
    ):
        core.run_stage3(_factory)


def test_run_stage3_rerun_without_cells_skips_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = tmp_path / "cell.parquet"
    cell.write_bytes(b"cell-bytes")
    monkeypatch.setattr(
        mod, "validate_stage3_cells", lambda cells, *, run_id, snapshot_hash: (cell,)
    )
    reports = iter([_cell_report([cell], "ab" * 32), _cell_report([], None)])
    with _core(tmp_path) as (core, _s):
        report = core.run_stage3(lambda: next(reports))
        assert report.train_snapshot_hash is None
        assert "stage3" in core._stamps_present


def test_run_stage3_stamp_write_failure_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR 1 (hostile-review round 1): a stamp WRITE OSError must NOT kill the run.

    A transient sidecar ENOSPC/permission failure at ``stamp_stage3_cells`` is
    WARNed and the cells are left UNSTAMPED (the cascade replays them on the
    MISSING_STAMP back-compat path) — the run continues, the ``stage3``
    prerequisite still stands, and the hard ``Stage3CellStampError`` is NEVER
    raised for a write failure (reserved for a genuine post-rerun MISMATCH).
    """
    from brutex.observability.stage_stamp import StageStampError

    cell = tmp_path / "cell.parquet"
    cell.write_bytes(b"cell-bytes")

    def _boom(*_a: object, **_kw: object) -> int:
        raise StageStampError("simulated ENOSPC writing stamp sidecar")

    monkeypatch.setattr(mod, "stamp_stage3_cells", _boom)
    with _core(tmp_path) as (core, _s):
        report = _cell_report([cell], "ab" * 32)
        # No raise — the run survives the observability write failure.
        assert core.run_stage3(lambda: report) is report
        assert "stage3" in core._stamps_present  # correctness prerequisite met
        assert "stage3" not in core._link_stamps  # no durable OK link evidence
        assert core.suspect is False  # a write hiccup NEVER latches SUSPECT


def test_run_stage3_discard_write_failure_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR 1: a forensic DISCARD rename failure is fail-soft — WARN, never raise."""
    from brutex.observability.stage_stamp import StageStampError

    cell = tmp_path / "cell.parquet"
    cell.write_bytes(b"cell-bytes")
    monkeypatch.setattr(
        mod, "validate_stage3_cells", lambda cells, *, run_id, snapshot_hash: (cell,)
    )

    def _boom(*_a: object, **_kw: object) -> None:
        raise StageStampError("simulated rename failure discarding a cell")

    monkeypatch.setattr(mod, "discard_and_flag", _boom)
    with _core(tmp_path) as (core, _s):
        report = _cell_report([cell], "ab" * 32)
        assert core.run_stage3(lambda: report) is report  # no raise
        assert "stage3" in core._stamps_present
        assert "stage3" not in core._link_stamps
        assert core.suspect is False


def test_run_stage3_restamp_write_failure_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR 1: a RE-STAMP write failure on the second (post-discard) pass is fail-soft."""
    from brutex.observability.stage_stamp import StageStampError

    cell = tmp_path / "cell.parquet"
    cell.write_bytes(b"cell-bytes")
    # First validate → invalid (drives the discard + re-run); the re-stamp fails.
    monkeypatch.setattr(
        mod, "validate_stage3_cells", lambda cells, *, run_id, snapshot_hash: (cell,)
    )
    stamp_calls = {"n": 0}

    def _stamp(*_a: object, **_kw: object) -> int:
        stamp_calls["n"] += 1
        if stamp_calls["n"] >= 2:
            raise StageStampError("simulated ENOSPC on the post-discard re-stamp")
        return 1

    monkeypatch.setattr(mod, "stamp_stage3_cells", _stamp)

    def _factory() -> Any:
        cell.write_bytes(b"cell-bytes")
        return _cell_report([cell], "ab" * 32)

    with _core(tmp_path) as (core, _s):
        assert core.run_stage3(_factory) is not None  # no raise
        assert stamp_calls["n"] == 2  # first OK, second failed
        assert "stage3" in core._stamps_present
        assert "stage3" not in core._link_stamps
        assert core.suspect is False


def test_run_stage3_validate_rehash_glitch_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MINOR (round 2): a cell RE-HASH I/O glitch in validate is fail-soft, not FAILED.

    ``validate_stage3_cells`` re-reads each cell + stamp off the results SSD, so
    a transient EIO on the §8 external SSD raises ``StageStampError``. That must
    be treated exactly like a guarded stamp-WRITE failure — WARN + leave the
    cells UNSTAMPED — NOT converted into a whole-sweep ``Stage3CellStampError``
    after the grid + stage-3 compute already succeeded.
    """
    from brutex.observability.stage_stamp import StageStampError

    cell = tmp_path / "cell.parquet"
    cell.write_bytes(b"cell-bytes")

    def _boom(*_a: object, **_kw: object) -> tuple[Path, ...]:
        raise StageStampError("simulated EIO re-hashing a cell")

    monkeypatch.setattr(mod, "validate_stage3_cells", _boom)
    with _core(tmp_path) as (core, _s):
        report = _cell_report([cell], "ab" * 32)
        assert core.run_stage3(lambda: report) is report  # no raise
        assert "stage3" in core._stamps_present
        assert "stage3" not in core._link_stamps  # unstamped → cascade re-derives
        assert core.suspect is False


def test_run_stage3_second_validate_rehash_glitch_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MINOR (round 2): the POST-discard re-validate re-hash glitch is also fail-soft."""
    from brutex.observability.stage_stamp import StageStampError

    cell = tmp_path / "cell.parquet"
    cell.write_bytes(b"cell-bytes")
    validate_calls = {"n": 0}

    def _validate(*_a: object, **_kw: object) -> tuple[Path, ...]:
        validate_calls["n"] += 1
        if validate_calls["n"] == 1:
            return (cell,)  # first pass: invalid → discard + re-run
        raise StageStampError("simulated EIO on the post-discard re-hash")

    monkeypatch.setattr(mod, "validate_stage3_cells", _validate)

    def _factory() -> Any:
        cell.write_bytes(b"cell-bytes")
        return _cell_report([cell], "ab" * 32)

    with _core(tmp_path) as (core, _s):
        assert core.run_stage3(_factory) is not None  # no raise
        assert validate_calls["n"] == 2
        assert "stage3" in core._stamps_present
        assert "stage3" not in core._link_stamps
        assert core.suspect is False


# ────── cascade fold ──────


def _fake_cascade_report(*stages: FilterStage | None) -> Any:
    verdicts = tuple(SimpleNamespace(rejected_at=s) for s in stages)
    group = SimpleNamespace(verdicts=verdicts)
    return SimpleNamespace(groups=(("NIFTY", "1minute", group),))


def test_cascade_done_folds_sourceable_doors_only(tmp_path: Path) -> None:
    from brutex.engine.cascade_wire import manifest_path as cmp
    from brutex.engine.cascade_wire import report_path as crp

    with _core(tmp_path) as (core, _s):
        cmp(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        cmp(tmp_path).write_bytes(b"{}")
        crp(tmp_path).write_bytes(b"{}")
        write_stage_stamp(cmp(tmp_path), stage="cascade_manifest", run_id=core.run_id)
        write_stage_stamp(crp(tmp_path), stage="cascade_report", run_id=core.run_id)
        core.cascade_done(
            _fake_cascade_report(
                None,
                FilterStage.DEFLATED_SHARPE,
                FilterStage.DEFLATED_SHARPE,
                FilterStage.HARD_GATES,  # not sourceable — must stay ABSENT
                FilterStage.CPCV,
            )
        )
        assert core._link_stamps["cascade"] is StampVerdict.OK
        assert {"cascade_manifest", "cascade_report"} <= core._stamps_present
        run_id = core.run_id
    manifest = load_manifest(tmp_path, run_id)
    assert manifest.per_door_kills == {"dsr": 2, "cpcv": 1}


def test_cascade_done_missing_stamps_report_worst_verdict(tmp_path: Path) -> None:
    from brutex.engine.cascade_wire import manifest_path as cmp

    with _core(tmp_path) as (core, _s):
        # Manifest stamped OK, report missing → the worst LINK verdict wins.
        cmp(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        cmp(tmp_path).write_bytes(b"{}")
        write_stage_stamp(cmp(tmp_path), stage="cascade_manifest", run_id=core.run_id)
        core.cascade_done(_fake_cascade_report(None))
        assert core._link_stamps["cascade"] is StampVerdict.MISSING_ARTIFACT
        # Decoupled (DEC-SWEEP-UNKILLABLE-CORE-001, hostile-review round 1): the
        # tripwire prerequisites track the CASCADE CORRECTNESS pass having run,
        # NOT the durable stamp — so BOTH are present even when a forensic stamp
        # is missing; only the chain-map LINK verdict reflects the miss.
        assert "cascade_manifest" in core._stamps_present
        assert "cascade_report" in core._stamps_present
    with _core(tmp_path / "b") as (core, _s):
        # Neither artifact exists → the manifest's own verdict reports first.
        core.cascade_done(_fake_cascade_report(None))
        assert core._link_stamps["cascade"] is StampVerdict.MISSING_ARTIFACT


def test_mark_cascade_absent_vacuously_satisfies_artifacts_prereq(tmp_path: Path) -> None:
    """MAJOR 2 (hostile-review round 1): an empty-cascade CLEAN run never trips SUSPECT.

    A degenerate-but-clean grid (all-no-bars / all below min_hits) produces zero
    cascade specs, so the CLI calls ``mark_cascade_absent`` instead of
    ``cascade_done``. The ``artifacts`` stage invariant must then be vacuously
    satisfied — the clean run stays NOT-suspect.
    """
    with _core(tmp_path) as (core, _s):
        core._stamps_present.add("stage2")  # grid_done satisfied it
        core.stage("stage3")
        core._stamps_present.add("stage3")  # run_stage3 satisfied it
        core.stage("cascade")
        core.mark_cascade_absent()
        assert "cascade_manifest" in core._stamps_present
        assert "cascade_report" in core._stamps_present
        assert "cascade" not in core._link_stamps  # no artifact to evidence
        core.stage("artifacts")
        assert core.suspect is False


# ────── G1 quarantine composition ──────


def test_g01_store_quarantine_moves_trio_and_records(tmp_path: Path) -> None:
    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    store = tuple_store_path(tmp_path, tuple_, grid)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"garbage")
    with _core(tmp_path, grid) as (core, _s):
        core.store_quarantine(tuple_, ValueError("corrupt"))
        run_id = core.run_id
    dest = tmp_path / ".quarantine" / "runtime" / f"{run_id}-1"
    assert (dest / store.name).exists()
    assert not store.exists()
    rows = [r for r in _records(tmp_path, run_id) if r["kind"] == "QUARANTINE"]
    assert len(rows) == 1
    payload = cast("dict[str, Any]", rows[0]["payload"])
    assert payload["what"] == "tuple_store"
    assert payload["reason"] == "ValueError: corrupt"


# ────── G12 checkpoint pre-load ──────


def test_g12_corrupt_checkpoint_quarantined_at_enter(tmp_path: Path) -> None:
    grid = _grid()
    ckpt_path = tmp_path / "checkpoint" / "sweep-all.json"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.write_text("{not json", encoding="utf-8")
    store = SweepCheckpointStore(
        path=ckpt_path, sweep_id=grid_identity(grid), total_combos=count_sweep_tuples(grid)
    )
    with _core(tmp_path, grid, checkpoint_store=store) as (core, _s):
        run_id = core.run_id
    assert not ckpt_path.exists()
    assert (tmp_path / ".quarantine" / "checkpoints" / "1.json").exists()
    assert "RESUME" not in _kinds(tmp_path, run_id)


def test_g12_crash_loop_across_two_enters_fires_alert(tmp_path: Path) -> None:
    """MAJOR (round 2): a persistently-corrupt checkpoint fires the CRITICAL alert.

    The wired path constructs a FRESH tracker per ``sweep_run_core`` enter and
    loads the checkpoint EXACTLY once per enter, so the crash-loop edge is only
    reachable if the consecutive count PERSISTS across enters. Drive the loop
    through TWO SEPARATE enters (a buggy writer re-emitting garbage each relaunch)
    and assert the ``checkpoint_crash_loop`` alert fires on the 2nd enter, never
    the 1st.

    Round-4: a genuine crash-loop is a run that does NOT successfully complete
    between corruptions (the successful-completion reset closes the false-alert
    case — see ``test_g12_two_corruptions_separated_by_success_no_alert``). Each
    enter here RAISES inside the block (the buggy relaunch dies before
    completing), so ``_finalize`` takes the FAILED path and does NOT reset the
    durable counter — the 2nd corruption legitimately reaches the edge.
    """
    grid = _grid()
    ckpt_path = tmp_path / "checkpoint" / "sweep-all.json"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)

    def _store() -> SweepCheckpointStore:
        return SweepCheckpointStore(
            path=ckpt_path,
            sweep_id=grid_identity(grid),
            total_combos=count_sweep_tuples(grid),
        )

    class _Boom(RuntimeError):
        pass

    ckpt_path.write_text("{garbage-1", encoding="utf-8")  # 1st relaunch
    with pytest.raises(_Boom), _core(tmp_path, grid, checkpoint_store=_store()) as (_c1, spy1):
        raise _Boom  # the relaunch dies before completing (no reset)
    assert not any(t == "checkpoint_crash_loop" for _r, t, _d in spy1.suspects)

    ckpt_path.write_text("{garbage-2", encoding="utf-8")  # 2nd relaunch — same garbage class
    with pytest.raises(_Boom), _core(tmp_path, grid, checkpoint_store=_store()) as (_c2, spy2):
        raise _Boom
    assert any(t == "checkpoint_crash_loop" for _r, t, _d in spy2.suspects)


def test_g12_two_corruptions_separated_by_success_no_alert(tmp_path: Path) -> None:
    """Round-4 fix: two corruptions with a SUCCESSFUL run between them are NOT a loop.

    The consecutive-corruption counter is DURABLE (``run_id`` is timestamp-free),
    so a single crash-recovery — this run's OWN load was the corrupt one, it
    quarantined, started fresh, and COMPLETED — otherwise left the count sticky
    at 1 with no reset hook, and a LATER independent corruption would escalate
    ``1 -> 2 == threshold`` and fire a FALSE CRITICAL ``checkpoint_crash_loop``
    page. The successful-completion reset (``reset_checkpoint_corruption`` in
    ``_finalize``) clears the durable counter so the two independent incidents
    never chain. First enter: corrupt → quarantine → COMPLETE (reset). Second
    enter (a much-later independent corruption): corrupt → count 1, NOT the edge.
    """
    grid = _grid()
    ckpt_path = tmp_path / "checkpoint" / "sweep-all.json"
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)

    def _store() -> SweepCheckpointStore:
        return SweepCheckpointStore(
            path=ckpt_path,
            sweep_id=grid_identity(grid),
            total_combos=count_sweep_tuples(grid),
        )

    ckpt_path.write_text("{garbage-1", encoding="utf-8")  # 1st corruption
    with _core(tmp_path, grid, checkpoint_store=_store()) as (_c1, spy1):
        pass  # recovers + COMPLETES → durable counter reset
    assert not any(t == "checkpoint_crash_loop" for _r, t, _d in spy1.suspects)

    ckpt_path.write_text("{garbage-2", encoding="utf-8")  # 2nd, INDEPENDENT corruption
    with _core(tmp_path, grid, checkpoint_store=_store()) as (_c2, spy2):
        pass
    # NOT a consecutive loop — the successful run in between reset the count, so
    # this stands at 1, below the threshold: no false CRITICAL page.
    assert not any(t == "checkpoint_crash_loop" for _r, t, _d in spy2.suspects)


def test_checkpoint_with_progress_records_resume(tmp_path: Path) -> None:
    grid = _grid()
    ckpt_path = tmp_path / "checkpoint" / "sweep-all.json"
    store = SweepCheckpointStore(
        path=ckpt_path, sweep_id=grid_identity(grid), total_combos=count_sweep_tuples(grid)
    )
    store.record(0)
    with _core(tmp_path, grid, checkpoint_store=store) as (core, _s):
        run_id = core.run_id
    resumes = [r for r in _records(tmp_path, run_id) if r["kind"] == "RESUME"]
    assert len(resumes) == 1
    assert cast("dict[str, Any]", resumes[0]["payload"])["resume_from"] == 1


# ────── composed on_progress ──────


def test_on_progress_publishes_ticks_and_beats(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress" / "sweep-progress.json"
    seen: list[int] = []
    with _core(
        tmp_path,
        progress_path=progress_path,
        inner_on_progress=lambda snap: seen.append(snap.evaluated),
    ) as (core, _s):
        # Launch reset (G4): running baseline with the wall/pid evidence.
        launch = json.loads(progress_path.read_text())
        assert launch["running"] is True
        assert launch["run_id"] == core.run_id
        assert launch["pid"] == os.getpid()
        core.on_progress(_snapshot(enumerated=1, evaluated=1))
        published = json.loads(progress_path.read_text())
        assert published["evaluated"] == 1
        assert published["stage"] == "preflight"
        assert published["suspect"] is False
        assert isinstance(published["wall_time_utc"], float)
        run_id = core.run_id
    assert seen == [1]
    terminal = json.loads(progress_path.read_text())
    assert terminal["running"] is False
    assert terminal["run_id"] == run_id


def test_on_progress_without_progress_path_still_ticks(tmp_path: Path) -> None:
    with _core(tmp_path) as (core, _s):
        core.on_progress(_snapshot(enumerated=1, evaluated=1))
        assert core._last_snapshot["evaluated"] == 1


def test_on_progress_failure_warns_once_then_silent(tmp_path: Path) -> None:
    def _boom(snap: SweepProgressSnapshot) -> None:
        raise RuntimeError("inner broke")

    with _core(tmp_path, inner_on_progress=_boom) as (core, _s):
        with capture_logs() as logs:
            core.on_progress(_snapshot(enumerated=1, evaluated=1))
            core.on_progress(_snapshot(enumerated=1, evaluated=1))
        warned = [e for e in logs if e["event"] == "sweep_run_core.on_progress_failed"]
        assert len(warned) == 1  # warn-once latch


def test_r41_milestones_fire_once_each(tmp_path: Path) -> None:
    with _core(tmp_path) as (core, spy):
        core.on_progress(_snapshot(enumerated=100, evaluated=30))
        assert [m[1] for m in spy.milestones] == [25]
        core.on_progress(_snapshot(enumerated=100, evaluated=30))  # no re-fire
        core.on_progress(_snapshot(enumerated=100, evaluated=80))
        assert [m[1] for m in spy.milestones] == [25, 50, 75]
        run_id = core.run_id
    milestone_records = [r for r in _records(tmp_path, run_id) if r["kind"] == "MILESTONE"]
    assert len(milestone_records) == 3


# ────── composed on_tuple ──────


def test_on_tuple_composes_inner_then_records(tmp_path: Path) -> None:
    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    inner: list[str] = []
    with _core(tmp_path, grid, inner_on_tuple=lambda t, s: inner.append(t.instrument)) as (
        core,
        _s,
    ):
        core.on_progress(_snapshot(enumerated=1, evaluated=1))
        core.on_tuple(tuple_, _summary(resumed_complete=True, minhits_dropped=2))
        assert core._sentinel_skips == 1
        assert core._minhits_dropped == 2
        core.on_tuple(tuple_, _summary())
        assert core._sentinel_skips == 1
        run_id = core.run_id
    assert inner == ["NIFTY", "NIFTY"]
    rows = [r for r in _records(tmp_path, run_id) if r["kind"] == "TUPLE_DONE"]
    assert len(rows) == 2
    payload = cast("dict[str, Any]", rows[0]["payload"])
    assert payload["done_cum"] == 1
    assert payload["skipped_cum"] == 0
    assert payload["evaluated"] == 3
    assert payload["kept"] == 1
    assert len(str(payload["digest"])) == 16


def test_on_tuple_inner_raise_propagates_raw(tmp_path: Path) -> None:
    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))

    def _boom(t: SweepTuple, s: SweepRunSummary) -> None:
        raise RuntimeError("inner hook broke")

    with _core(tmp_path, grid, inner_on_tuple=_boom) as (core, _s):
        # The caller's own hook keeps its RAW pre-existing contract (a raising
        # print aborted the sweep before this change too). PT012-shaped: only
        # the hook call sits inside the raises block.
        raises_ctx = pytest.raises(RuntimeError, match="inner hook broke")
        with raises_ctx:
            core.on_tuple(tuple_, _summary())


# ────── diffcheck closure (R45) ──────


def test_diffcheck_disabled_is_none(tmp_path: Path) -> None:
    with _core(tmp_path, diffcheck_k=0) as (core, _s):
        assert core.diffcheck is None


def test_r45_diffcheck_closure_checks_real_masks(tmp_path: Path) -> None:
    from brutex.engine.stage1a import build_bar_bits

    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    bits = build_bar_bits(_BARS, tuple_.proximity, grid.prior_n, tuple_.fib_anchor)
    with _core(tmp_path, grid, diffcheck_k=2) as (core, _s):
        assert core.diffcheck is not None
        core.diffcheck(bars=_BARS, bar_bits=bits, tuple_=tuple_, grid=grid)
        assert core.suspect is False  # production == reference on real data
        # Budget exhausted (2 checked) → the next call is a zero-cost no-op.
        core.diffcheck(bars=_BARS, bar_bits=bits, tuple_=tuple_, grid=grid)
        run_id = core.run_id
    ok_rows = [r for r in _records(tmp_path, run_id) if r["kind"] == "DIFFCHECK_OK"]
    assert len(ok_rows) == 1
    payload = cast("dict[str, Any]", ok_rows[0]["payload"])
    assert payload["checked"] == 2
    assert payload["mismatches"] == 0


def test_r45_diffcheck_production_leg_carries_the_bound_run_stamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Golden-Rule-#3 bind (DEC-VIX-STAMP-WIRE-001): after the CLI
    late-binds the launch stamper via ``bind_reference_stamper``, every
    sampled production re-simulation carries it — and the stamp is
    compare-neutral (the reference compare surface has no VIX field, so a
    stamped production leg never latches SUSPECT)."""
    from brutex.engine import trade_sim as trade_sim_mod
    from brutex.engine.reference_series import TradeReferenceStamper
    from brutex.engine.stage1a import build_bar_bits

    stamper = TradeReferenceStamper.from_vix_bars(
        [
            CandleBar(
                timestamp=bar.timestamp,
                open=Decimal("14.00"),
                high=Decimal("15.00"),
                low=Decimal("13.00"),
                close=Decimal("14.25"),
                volume=0,
                open_interest=None,
            )
            for bar in _BARS
        ]
    )
    captured: list[object] = []
    original = trade_sim_mod.simulate_trades

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("reference_stamper", "OMITTED"))
        return original(*args, **kwargs)

    # Patch BEFORE the core enters: _make_diffcheck from-imports
    # simulate_trades at compose time, so the closure must capture the spy.
    monkeypatch.setattr(trade_sim_mod, "simulate_trades", _spy)
    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    bits = build_bar_bits(_BARS, tuple_.proximity, grid.prior_n, tuple_.fib_anchor)
    with _core(tmp_path, grid, diffcheck_k=2) as (core, _s):
        assert core.diffcheck is not None
        core.bind_reference_stamper(stamper)
        core.diffcheck(bars=_BARS, bar_bits=bits, tuple_=tuple_, grid=grid)
        # Compare-neutral: production (stamped) still matches the reference.
        assert core.suspect is False
    assert captured == [stamper, stamper]  # both sampled masks bound the stamper


def test_diffcheck_zero_checked_records_nothing(tmp_path: Path) -> None:
    # k=3 over the conflicting bits {0,1,2} enumerates ZERO conflict-free
    # masks — the closure spends nothing and records no DIFFCHECK_OK.
    grid = _grid(k_values=[3])
    tuple_ = next(iter_sweep_tuples(grid))
    with _core(tmp_path, grid, diffcheck_k=2) as (core, _s):
        assert core.diffcheck is not None
        core.diffcheck(bars=_BARS, bar_bits=[0] * len(_BARS), tuple_=tuple_, grid=grid)
        run_id = core.run_id
    assert not [r for r in _records(tmp_path, run_id) if r["kind"] == "DIFFCHECK_OK"]


def test_on_tuple_without_inner_still_records(tmp_path: Path) -> None:
    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    with _core(tmp_path, grid) as (core, _s):
        core.on_tuple(tuple_, _summary())
        run_id = core.run_id
    assert len([r for r in _records(tmp_path, run_id) if r["kind"] == "TUPLE_DONE"]) == 1


def test_clean_run_appends_run_manifest_chain_line(tmp_path: Path) -> None:
    from brutex.audit_verify import auto_verify

    grid = _grid()
    chain = tmp_path / "audit" / "sweep-all.jsonl"
    with _core(tmp_path, grid, cli_chain_path=chain):
        pass
    lines = [json.loads(line) for line in chain.read_text().splitlines()]
    assert [entry["action"] for entry in lines] == ["RUN_MANIFEST"]
    payload = lines[0]["payload"]
    manifest_file = tmp_path / "runs" / grid_identity(grid) / "manifest.json"
    assert payload["manifest_content_hash"] == content_hash_of(manifest_file)
    assert payload["manifest"]["run_id"] == grid_identity(grid)
    assert auto_verify(chain).entry_count == 1


# ────── launch/terminal progress edge cases ──────


def test_terminal_patch_tolerates_missing_garbage_and_nondict(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress" / "p.json"
    with _core(tmp_path, progress_path=progress_path) as (_core0, _s):
        progress_path.unlink()  # vanished mid-run → terminal patch no-ops
    assert not progress_path.exists()
    with _core(tmp_path, progress_path=progress_path) as (_core1, _s):
        progress_path.write_text("{torn", encoding="utf-8")
    assert progress_path.read_text() == "{torn"
    with _core(tmp_path, progress_path=progress_path) as (_core2, _s):
        progress_path.write_text("[]", encoding="utf-8")
    assert progress_path.read_text() == "[]"


def test_progress_none_reset_and_terminal_noop(tmp_path: Path) -> None:
    with _core(tmp_path) as (core, _s):
        core._reset_progress_at_launch()
        core._terminal_progress_patch()
    assert not (tmp_path / "progress").exists()


# ────── fail-soft plumbing ──────


def test_guarded_returns_none_and_warns_once_per_call(tmp_path: Path) -> None:
    with _core(tmp_path) as (core, _s):
        with capture_logs() as logs:
            out = core._guarded("unit_step", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert out is None
        warned = [e for e in logs if e["event"] == "sweep_run_core.observability_failed"]
        assert len(warned) == 1
        assert warned[0]["step"] == "unit_step"
        assert core._guarded("ok", lambda: 42) == 42


def test_finalize_manifest_flush_failure_skips_stamp_and_chain(tmp_path: Path) -> None:
    grid = _grid()
    run_id = grid_identity(grid)
    chain = tmp_path / "audit" / "sweep-all.jsonl"
    with _core(tmp_path, grid, cli_chain_path=chain) as (core, _s):
        core.manifest.flush = lambda: None  # type: ignore[method-assign]
    assert not chain.exists()  # no manifest doc → no RUN_MANIFEST line
    assert not (tmp_path / "runs" / run_id / "manifest.json.stamp.json").exists()
    status = read_exit_status(tmp_path, run_id)
    assert status is not None and status["state"] == "complete"


def test_finalize_verdicts_failure_is_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(root: Path, *, run_id: str) -> Path:
        raise OSError("verdicts disk broke")

    monkeypatch.setattr(mod, "build_run_verdicts", _boom)
    with _core(tmp_path) as (core, _s):
        run_id = core.run_id
    status = read_exit_status(tmp_path, run_id)
    assert status is not None and status["state"] == "complete"
    assert not (tmp_path / "runs" / run_id / "verdicts.json").exists()


def test_env_default_knobs_resolved_at_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tripwire_config=None / diffcheck_k=None resolve from a CLEAN env at enter.
    for var in list(os.environ):
        if var.startswith(("BRUTEX_SWEEP_TRIPWIRE_", "BRUTEX_SWEEP_DIFFCHECK")):
            monkeypatch.delenv(var)  # pragma: no cover — CI env is clean
    grid = _grid()
    spy = _spy()
    with sweep_run_core(
        results_root=tmp_path,
        grid=grid,
        alerter=cast("SweepAlerter", spy),
        entry_label="t",
    ) as core:
        assert core.diffcheck is not None  # default-ON tristate → K=200
    status = read_exit_status(tmp_path, grid_identity(grid))
    assert status is not None and status["state"] == "complete"


def test_append_run_manifest_line_direct(tmp_path: Path) -> None:
    from brutex.audit_verify import auto_verify

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"run_id": "r"}), encoding="utf-8")
    chain = tmp_path / "chain.jsonl"
    mod._append_run_manifest_line(manifest_path, chain, run_id="r1")
    mod._append_run_manifest_line(manifest_path, chain, run_id="r1")  # resume append
    lines = [json.loads(line) for line in chain.read_text().splitlines()]
    assert [entry["action"] for entry in lines] == ["RUN_MANIFEST", "RUN_MANIFEST"]
    assert lines[0]["subject"] == "r1"
    assert lines[0]["payload"]["manifest_content_hash"] == content_hash_of(manifest_path)
    assert auto_verify(chain).entry_count == 2  # the chain verifies (D-2 verifier)


def test_total_tuples_default_computed(tmp_path: Path) -> None:
    grid = _grid()
    spy = _spy()
    with sweep_run_core(
        results_root=tmp_path,
        grid=grid,
        alerter=cast("SweepAlerter", spy),
        entry_label="t",
        total_tuples=None,
        tripwire_config=TripwireConfig(),
        diffcheck_k=0,
    ) as core:
        run_id = core.run_id
    start = [r for r in _records(tmp_path, run_id) if r["kind"] == "RUN_START"]
    assert cast("dict[str, Any]", start[0]["payload"])["tuples_total"] == 1


# ══════════ edited-seam coverage (supervisor / orchestrator / parallel / cli) ══════════
#
# The CI engine gate measures brutex.engine over tests/unit/test_engine_*.py
# ALONE, so every branch WP-5 added to the edited engine modules is unit-covered
# HERE (the integration files re-prove the same seams end-to-end).


def test_g11_publish_progress_failure_warns_once_and_latches(tmp_path: Path) -> None:
    from brutex.engine.sweep_supervisor import (
        publish_progress,
        reset_progress_write_warnings,
    )

    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a dir must be", encoding="utf-8")
    dest = blocker / "progress.json"  # mkdir(parents=True) under a FILE raises
    reset_progress_write_warnings()
    with capture_logs() as logs:
        publish_progress(dest, _snapshot(enumerated=1, evaluated=0))
        publish_progress(dest, _snapshot(enumerated=1, evaluated=0))
    warned = [e for e in logs if e["event"] == "sweep_supervisor.progress_write_failed"]
    assert len(warned) == 1  # ONE WARN per path per process (G11)
    reset_progress_write_warnings()
    with capture_logs() as logs:
        publish_progress(dest, _snapshot(enumerated=1, evaluated=0))
    warned = [e for e in logs if e["event"] == "sweep_supervisor.progress_write_failed"]
    assert len(warned) == 1  # the reset seam re-arms the latch


def test_g04_publish_progress_additive_keys(tmp_path: Path) -> None:
    from brutex.engine.sweep_supervisor import publish_progress

    dest = tmp_path / "p.json"
    publish_progress(dest, _snapshot(enumerated=2, evaluated=1))
    bare = json.loads(dest.read_text())
    assert bare["pid"] == os.getpid()
    assert isinstance(bare["wall_time_utc"], float)
    assert "run_id" not in bare
    assert "suspect" not in bare
    assert "stage" not in bare
    publish_progress(
        dest, _snapshot(enumerated=2, evaluated=1), run_id="r1", suspect=True, stage="sweep"
    )
    stamped = json.loads(dest.read_text())
    assert stamped["run_id"] == "r1"
    assert stamped["suspect"] is True
    assert stamped["stage"] == "sweep"
    # Every pre-existing snapshot key is byte-unchanged.
    assert stamped["enumerated"] == 2
    assert stamped["evaluated"] == 1


def test_g05_read_progress_enriches_with_exit_status(tmp_path: Path) -> None:
    from brutex.engine.sweep_exit_status import RunState, write_exit_status
    from brutex.engine.sweep_supervisor import idle_progress, read_progress

    root = tmp_path
    progress_path = root / "progress" / "sweep-progress.json"
    progress_path.parent.mkdir(parents=True)
    write_exit_status(root, run_id="r" * 64, state=RunState.COMPLETE, exit_code=0, attempt=1, pid=1)
    progress_path.write_text(json.dumps({"running": False, "run_id": "r" * 64}))
    out = read_progress(progress_path)
    status = out["exit_status"]
    assert status is not None
    assert status["state"] == "complete"
    assert out["suspect"] is False
    # A payload naming no run enriches honestly (exit_status None).
    progress_path.write_text(json.dumps({"running": False}))
    out = read_progress(progress_path)
    assert out["exit_status"] is None
    # The missing-file idle fallback stays UNenriched (byte-compat contract).
    assert read_progress(root / "absent.json") == idle_progress()


def _run_one(
    root: Path,
    grid: SweepGridConfig,
    tuple_: SweepTuple,
    **kw: Any,
) -> SweepRunSummary | None:
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_kernel import ResidentKernelCache
    from brutex.engine.sweep_orchestrator import make_store_for, run_one_tuple
    from brutex.engine.sweep_run import ComboDedup
    from brutex.orchestrator.audit import AuditWriter

    with AuditWriter.open(root / "audit.jsonl", resume=(root / "audit.jsonl").exists()) as audit:
        return run_one_tuple(
            grid,
            tuple_,
            bars_for=lambda _i, _t: _BARS,
            store_for=make_store_for(root, grid),
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            audit=audit,
            kernel_cache=kw.pop("kernel_cache", ResidentKernelCache(capacity=1)),
            **kw,
        )


def test_g01_run_one_tuple_quarantine_retries_once_and_succeeds(tmp_path: Path) -> None:
    from brutex.engine.sweep_quarantine import quarantine_tuple_trio

    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    store = tuple_store_path(tmp_path, tuple_, grid)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"garbage - not parquet")
    quarantined: list[str] = []

    def _quarantine(t: SweepTuple, exc: BaseException) -> None:
        quarantined.append(type(exc).__name__)
        quarantine_tuple_trio(store, results_root=tmp_path, reason=str(exc))

    summary = _run_one(tmp_path, grid, tuple_, store_quarantine=_quarantine)
    assert summary is not None
    assert summary.persisted >= 0
    assert quarantined == ["ResultsStoreSchemaError"]
    assert store.exists()  # regenerated fresh after the trio moved
    assert (tmp_path / ".quarantine" / "runtime").is_dir()


def test_g01_run_one_tuple_without_quarantine_reraises(tmp_path: Path) -> None:
    from brutex.engine.results_store import ResultsStoreError

    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    store = tuple_store_path(tmp_path, tuple_, grid)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"garbage - not parquet")
    with pytest.raises(ResultsStoreError):
        _run_one(tmp_path, grid, tuple_)


def test_g01_second_failure_demotes_to_combo_error(tmp_path: Path) -> None:
    from brutex.engine.sweep_run import SweepComboError

    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    store = tuple_store_path(tmp_path, tuple_, grid)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"garbage - not parquet")
    # A no-op quarantine leaves the corrupt store in place → the single fresh
    # retry fails again → the tuple-skip contract (grid counts it, continues).
    with pytest.raises(SweepComboError, match="after quarantine"):
        _run_one(tmp_path, grid, tuple_, store_quarantine=lambda t, exc: None)


def test_r45_run_one_tuple_invokes_diffcheck_post_persist(tmp_path: Path) -> None:
    calls: list[int] = []

    def _diffcheck(
        *,
        bars: Sequence[CandleBar],
        bar_bits: Sequence[int],
        tuple_: SweepTuple,
        grid: SweepGridConfig,
        mask_population: Sequence[int] | None = None,
    ) -> None:
        assert len(bar_bits) == len(bars)
        assert mask_population is None  # legacy arm — the kwarg is never passed
        calls.append(len(bar_bits))

    grid = _grid()
    tuple_ = next(iter_sweep_tuples(grid))
    summary = _run_one(tmp_path, grid, tuple_, diffcheck=_diffcheck)
    assert summary is not None
    assert calls == [len(_BARS)]
    # Without a resident kernel the hook is honestly SKIPPED (documented).
    calls.clear()
    summary = _run_one(tmp_path / "b", grid, tuple_, diffcheck=_diffcheck, kernel_cache=None)
    assert summary is not None
    assert calls == []


def test_run_sweep_grid_threads_both_seams(tmp_path: Path) -> None:
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_orchestrator import make_store_for, run_sweep_grid
    from brutex.engine.sweep_run import ComboDedup
    from brutex.orchestrator.audit import AuditWriter

    grid = _grid()
    calls: list[str] = []
    with AuditWriter.open(tmp_path / "audit.jsonl") as audit:
        summary = run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: _BARS,
            store_for=make_store_for(tmp_path, grid),
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            audit=audit,
            diffcheck=lambda **kw: calls.append("diffcheck"),
            store_quarantine=lambda tuple_, exc: calls.append("quarantine"),
        )
    assert summary.tuples_run == 1
    assert calls == ["diffcheck"]  # a clean store never quarantines


def test_run_supervised_grid_threads_both_seams(tmp_path: Path) -> None:
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_audit import SweepAuditWriter
    from brutex.engine.sweep_cache import SweepCache
    from brutex.engine.sweep_orchestrator import make_store_for
    from brutex.engine.sweep_progress import SweepProgress
    from brutex.engine.sweep_run import ComboDedup
    from brutex.engine.sweep_supervisor import SweepGuards, run_supervised_grid
    from brutex.engine.sweep_watchdog import SweepWatchdog
    from brutex.orchestrator.audit import AuditWriter

    grid = _grid()
    calls: list[str] = []
    sup = SweepAuditWriter.open(tmp_path / "sup.jsonl")
    cache = SweepCache.open(tmp_path / "cache", bloom_capacity=10_000)
    try:
        guards = SweepGuards(
            audit=sup,
            progress=SweepProgress(),
            checkpoint=SweepCheckpointStore(
                path=tmp_path / "ckpt.json",
                sweep_id=grid_identity(grid),
                total_combos=count_sweep_tuples(grid),
            ),
            cache=cache,
            watchdog=SweepWatchdog(),
        )
        with AuditWriter.open(tmp_path / "ingest.jsonl") as ingest:
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: _BARS,
                store_for=make_store_for(tmp_path / "out", grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                ingest_audit=ingest,
                guards=guards,
                diffcheck=lambda **kw: calls.append("diffcheck"),
                store_quarantine=lambda tuple_, exc: calls.append("quarantine"),
            )
    finally:
        sup.close()
        cache.close()
    assert summary.tuples_run == 1
    assert calls == ["diffcheck"]


def test_g03_parallel_driver_streams_progress(tmp_path: Path) -> None:
    from brutex.data.option_dedup import OptionDedupIndex  # parity import
    from brutex.engine.sweep_audit import SweepAuditWriter
    from brutex.engine.sweep_cache import SweepCache
    from brutex.engine.sweep_parallel import run_parallel_grid
    from brutex.engine.sweep_progress import SweepProgress
    from brutex.engine.sweep_supervisor import SweepGuards
    from brutex.engine.sweep_watchdog import SweepWatchdog
    from brutex.orchestrator.audit import AuditWriter

    assert OptionDedupIndex is not None  # parity: the dedup module must stay importable
    grid = _grid(directions=[Side.BUY, Side.SELL])
    seen: list[int] = []
    sup = SweepAuditWriter.open(tmp_path / "sup.jsonl")
    cache = SweepCache.open(tmp_path / "cache", bloom_capacity=10_000)
    try:
        guards = SweepGuards(
            audit=sup,
            progress=SweepProgress(),
            checkpoint=SweepCheckpointStore(
                path=tmp_path / "ckpt.json",
                sweep_id=grid_identity(grid),
                total_combos=count_sweep_tuples(grid),
            ),
            cache=cache,
            watchdog=SweepWatchdog(),
        )
        with AuditWriter.open(tmp_path / "ingest.jsonl") as ingest:
            summary = run_parallel_grid(
                grid,
                bars_for=lambda _i, _t: _BARS,
                root=tmp_path / "out",
                ingest_audit=ingest,
                guards=guards,
                workers=2,
                on_progress=lambda snap: seen.append(snap.evaluated),
            )
    finally:
        sup.close()
        cache.close()
    assert summary.tuples_run == 2
    assert seen == [1, 2]  # one tick per applied slot, in enumeration order


def test_g06_cli_main_maps_refusals_to_clean_user_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from brutex import cli

    monkeypatch.setattr(
        cli,
        "_dispatch",
        lambda argv: (_ for _ in ()).throw(
            SweepAlreadyRunningError(Path("/tmp/x.lock"), holder_pid=123)
        ),
    )
    assert cli.main([]) == cli.EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "[brutex]" in err
    assert "123" in err  # the holder pid is named in the refusal


# ══════════ G7 cascade stamp/replay seams (cascade_wire — WP-5 edits) ══════════


def _cascade_setup(tmp_path: Path) -> tuple[Any, Any, Any]:
    """One stamped AUTOMATIC cascade run over the sibling harness's fixture."""
    import brutex.engine.cascade_wire as cw

    from tests.unit.test_engine_cascade_wire import _bars_for, _session_bars, _spec

    bars_for = _bars_for(_session_bars())
    report = cw.run_and_write_from_specs(
        tmp_path,
        sweep_id="a" * 64,
        specs=[_spec()],
        bars_for=bars_for,
        bars_context=cw.BarsContext(
            exchange="NSE", segment="INDEX", start="2021-01-01", end="2021-02-01"
        ),
        cascade_config=cw.cascade_config_for_sweep("a" * 64),
    )
    return cw, bars_for, report


def test_g07_auto_run_stamps_both_artifacts_with_snapshot(tmp_path: Path) -> None:
    from brutex.observability.stage_stamp import read_stage_stamp

    cw, bars_for, _report = _cascade_setup(tmp_path)
    manifest_stamp = read_stage_stamp(cw.manifest_path(tmp_path))
    report_stamp = read_stage_stamp(cw.report_path(tmp_path))
    assert manifest_stamp.stage == "cascade_manifest"
    assert report_stamp.stage == "cascade_report"
    assert manifest_stamp.run_id == "a" * 64
    snapshot = manifest_stamp.extra["data_snapshot_hash"]
    assert len(snapshot) == 64
    assert report_stamp.extra["data_snapshot_hash"] == snapshot
    # The stamped snapshot IS the manifest-pair recomputation (G7 symmetry).
    manifest = cw.load_manifest(tmp_path)
    assert cw.manifest_snapshot_hash(manifest, bars_for=bars_for) == snapshot


def test_g07_replay_validates_and_reproduces_bytes(tmp_path: Path) -> None:
    cw, bars_for, _report = _cascade_setup(tmp_path)
    auto_bytes = cw.report_path(tmp_path).read_bytes()
    cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
    assert cw.report_path(tmp_path).read_bytes() == auto_bytes


def test_g07_mutated_lake_replay_refuses_naming_both_hashes(tmp_path: Path) -> None:
    from tests.unit.test_engine_cascade_wire import _bars_for, _session_bars

    cw, bars_for, _report = _cascade_setup(tmp_path)
    mutated = _session_bars()[:-1]  # one bar fewer → a different snapshot
    with pytest.raises(cw.CascadeWireError, match="data_snapshot_hash") as excinfo:
        cw.run_and_write_from_manifest(tmp_path, bars_for=_bars_for(mutated))
    message = str(excinfo.value)
    manifest = cw.load_manifest(tmp_path)
    assert cw.manifest_snapshot_hash(manifest, bars_for=_bars_for(mutated)) in message
    assert cw.manifest_snapshot_hash(manifest, bars_for=bars_for) in message


def test_g07_torn_stamp_and_torn_manifest_refuse(tmp_path: Path) -> None:
    cw, bars_for, _report = _cascade_setup(tmp_path)
    stamp_file = tmp_path / "cascade" / "manifest.json.stamp.json"
    stamp_file.write_text("{torn", encoding="utf-8")
    with pytest.raises(cw.CascadeWireError, match="malformed"):
        cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
    # A content drift under an intact stamp is a HASH_MISMATCH refusal.
    cw2, bars_for2, _r = _cascade_setup(tmp_path / "b")
    manifest_file = (tmp_path / "b") / "cascade" / "manifest.json"
    manifest_file.write_bytes(manifest_file.read_bytes() + b" ")
    with pytest.raises(cw2.CascadeWireError, match="hash_mismatch"):
        cw2.run_and_write_from_manifest(tmp_path / "b", bars_for=bars_for2)


def test_g07_unstamped_legacy_replay_warns_and_proceeds(tmp_path: Path) -> None:
    cw, bars_for, report = _cascade_setup(tmp_path)
    (tmp_path / "cascade" / "manifest.json.stamp.json").unlink()
    (tmp_path / "cascade" / "cascade-report.json.stamp.json").unlink()
    with capture_logs() as logs:
        replayed = cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
    assert [e for e in logs if e["event"] == "cascade_wire.replay_unstamped"]
    assert cw.serialize_wire_report(replayed) == cw.serialize_wire_report(report)
    # An unstamped replay never re-stamps (the legacy path stays legacy).
    assert not (tmp_path / "cascade" / "manifest.json.stamp.json").exists()


def test_g07_stamp_without_snapshot_extra_is_not_asserted(tmp_path: Path) -> None:
    cw, bars_for, _report = _cascade_setup(tmp_path)
    # Re-stamp WITHOUT the extra: the snapshot compare is honestly skipped.
    write_stage_stamp(cw.manifest_path(tmp_path), stage="cascade_manifest", run_id="a" * 64)
    replayed = cw.run_and_write_from_manifest(tmp_path, bars_for=bars_for)
    assert replayed.n_input >= 0  # replay proceeded


def test_g07_stamp_failure_is_fail_soft_on_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import brutex.observability.stage_stamp as stage_stamp_mod

    def _boom(*args: object, **kwargs: object) -> object:
        raise OSError("stamp disk broke")

    monkeypatch.setattr(stage_stamp_mod, "write_stage_stamp", _boom)
    with capture_logs() as logs:
        _cw, _bars_for, report = _cascade_setup(tmp_path)
    assert report.n_input >= 0  # the AUTOMATIC run completed
    assert [e for e in logs if e["event"] == "cascade_wire.stamp_failed"]
    # The replay side: stamp valid, but the RE-stamp after the rewrite fails.
    monkeypatch.undo()
    cw2, bars_for2, _r = _cascade_setup(tmp_path / "b")
    monkeypatch.setattr(stage_stamp_mod, "write_stage_stamp", _boom)
    with capture_logs() as logs:
        cw2.run_and_write_from_manifest(tmp_path / "b", bars_for=bars_for2)
    assert [e for e in logs if e["event"] == "cascade_wire.stamp_failed"]
