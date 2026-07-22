"""Fault-injection matrix for the unkillable run core (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6).

The design §0/§7 fault matrix made executable: every row injects a REAL fault
(simulated kill boundary, ENOSPC at a specific write site, corrupt bytes, torn
tail, lock contention, read-only dir, injected-clock stall) into the REAL
wheel-backed engine wrapped by :func:`brutex.engine.sweep_run_core.sweep_run_core`
and asserts the row's DEFINED answer plus the R42 one-line law:

    a break at link N never corrupts links 1..N-1 — earlier artifacts'
    before/after hashes are EQUAL and recovery starts at N.

Kill-timing sub-matrix (3 boundaries): before-flush (loader crash) ·
after-flush-pre-sentinel · post-sentinel-pre-checkpoint — each resumes
bit-identical to an uninterrupted run with a ``RESUME`` flight record.

ENOSPC sub-matrix: EACH new Phase-2 write site — flight append · manifest ·
exit-status · stamp · progress · chain-map · bundle — is failed and the run
COMPLETES with identical result bytes (fail-soft, I-3); the STORE-flush ENOSPC
row pins the loud-abort answer (exit-status ``failed`` + earlier tuple intact).

The torn-CASCADE-artifact replay row lives in
``tests/integration/test_sweep_full_chain_rehearsal.py`` (the one place a REAL
cascade manifest exists); this file owns every other row.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.audit_verify import auto_verify
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.sweep_audit import SweepAuditWriter
from brutex.engine.sweep_cache import SweepCache
from brutex.engine.sweep_checkpoint import SweepCheckpointStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    count_sweep_tuples,
    make_store_for,
)
from brutex.engine.sweep_progress import SweepProgress
from brutex.engine.sweep_run import ComboDedup
from brutex.engine.sweep_run_core import RunCoreHandles, sweep_run_core
from brutex.engine.sweep_singleflight import (
    LakeBusyError,
    SweepAlreadyRunningError,
    lake_read_guard,
    sweep_run_lock,
)
from brutex.engine.sweep_stall_policy import resolve_stall_policy
from brutex.engine.sweep_supervisor import (
    SweepGuards,
    grid_identity,
    run_supervised_grid,
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.observability.flight_recorder import iter_run_records
from brutex.observability.run_tripwires import TripwireConfig
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("lmdb")

pytestmark = pytest.mark.chaos

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r42_kill_before_flush_resume_bit_identical": ("R42", "R7"),
    "test_r42_kill_after_flush_before_sentinel_resume_bit_identical": ("R42", "R7"),
    "test_r42_kill_after_sentinel_before_checkpoint_resume_bit_identical": ("R42", "R7"),
    "test_r7_enospc_at_each_observability_write_site_run_completes": ("R7", "R42"),
    "test_r41_enospc_at_flight_append_degrades_and_completes": ("R41", "R7"),
    "test_r42_enospc_during_store_flush_aborts_loudly_earlier_tuple_intact": ("R42",),
    "test_g12_corrupt_checkpoint_quarantined_fresh_start_no_crash_loop": ("G12", "R42"),
    "test_g1_corrupt_tuple_store_mid_grid_quarantined_grid_completes": ("G1", "R42"),
    "test_r42_stage3_cell_torn_between_stamp_and_consume_discarded_and_rerun": ("R42",),
    "test_r41_torn_flight_recorder_tail_resumes_and_verifies": ("R41",),
    "test_g2_stall_watchdog_fires_once_under_injected_clock": ("G2",),
    "test_g2_scaled_policy_prevents_false_abort_of_slow_but_alive_tuple": ("G2",),
    "test_g6_concurrent_second_sweep_refused_and_winner_chain_verifies": ("G6",),
    "test_g10_preload_refused_while_pull_holds_the_lake_lock": ("G10",),
    "test_g10_pull_refused_while_preload_holds_the_shared_lock": ("G10",),
    "test_g11_readonly_progress_dir_run_completes": ("G11", "R7"),
    "test_r7_detached_spawn_is_a_new_session_leader_and_completes": ("R7",),
}

_IST = timezone(timedelta(hours=5, minutes=30))


class _SimulatedCrash(RuntimeError):
    """A deliberate mid-sweep failure standing in for a SIGKILL / power loss."""


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


def _trending_bars(n_days: int = 3, per_day: int = 40) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


_BARS = _trending_bars()


def _grid() -> SweepGridConfig:
    """A 4-tuple grid (2 sl × 2 tp) — wide enough for mid-grid boundaries."""
    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80, 120],
        take_profits=[50, 90],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap-unkillable",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _dedup() -> ComboDedup:
    return ComboDedup(OptionDedupIndex(capacity=100_000, fpr_target=1e-6))


@contextmanager
def _guards(
    state_dir: Path, grid: SweepGridConfig, *, resume: bool
) -> Generator[SweepGuards, None, None]:
    """One process 'life' — reloads only the on-disk checkpoint/cache/chains."""
    audit = SweepAuditWriter.open(state_dir / "sup.jsonl", resume=resume)
    cache = SweepCache.open(state_dir / "cache", bloom_capacity=100_000)
    checkpoint = SweepCheckpointStore(
        path=state_dir / "ckpt.json",
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


def _one_life(
    root: Path,
    grid: SweepGridConfig,
    *,
    life_no: int,
    bars_for: Callable[[str, str], Sequence[CandleBar]] | None = None,
    checkpoint_wired: bool = True,
) -> tuple[Any, RunCoreHandles]:
    """Run ONE run-core-wrapped life over the grid (the WP-5 CLI wiring shape)."""
    out = root / "out"
    ingest_path = out / "audit" / "sweep-all.jsonl"
    ingest_path.parent.mkdir(parents=True, exist_ok=True)
    resume = life_no > 0
    checkpoint_store = (
        SweepCheckpointStore(
            path=root / "state" / "ckpt.json",
            sweep_id=grid_identity(grid),
            total_combos=count_sweep_tuples(grid),
        )
        if checkpoint_wired
        else None
    )
    with sweep_run_core(
        results_root=out,
        grid=grid,
        alerter=SweepAlerter(),
        entry_label="chaos life",
        progress_path=out / "progress" / "sweep-progress.json",
        cli_chain_path=ingest_path,
        checkpoint_store=checkpoint_store,
        tripwire_config=TripwireConfig(),
        diffcheck_k=0,  # the kill rows target persistence, not R45
    ) as core:
        core.stage("sweep")
        with (
            AuditWriter.open(ingest_path, resume=resume) as ingest,
            _guards(root / "state", grid, resume=resume) as guards,
        ):
            result = run_supervised_grid(
                grid,
                bars_for=bars_for or (lambda _i, _t: _BARS),
                store_for=make_store_for(out, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_tuple=core.on_tuple,
                on_progress=core.on_progress,
                diffcheck=core.diffcheck,
                store_quarantine=core.store_quarantine,
            )
        core.grid_done(result)
        core.set_exit_code(0)
    return result, core


#: Result-artifact prefixes EXCLUDED from byte-identity maps (per-run state).
_PHASE2_PREFIXES = ("runs/", ".locks/", ".quarantine/", "progress/", "audit/")


def _result_hashes(out: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for p in sorted(out.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(out))
        if any(rel.startswith(prefix) for prefix in _PHASE2_PREFIXES):
            continue
        if rel.endswith(".stamp.json") or rel == "stage2":
            continue
        hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def _golden(tmp_path: Path) -> dict[str, str]:
    """One uninterrupted run's result fingerprint (the bit-identity oracle)."""
    root = tmp_path / "golden"
    result, _core = _one_life(root, _grid(), life_no=0)
    assert result.tuples_failed == 0
    return _result_hashes(root / "out")


def _records(root: Path, run_id: str) -> list[dict[str, object]]:
    return list(iter_run_records(root / "out", run_id))


def _relaunch_until_complete(
    root: Path, grid: SweepGridConfig, first_bars: Callable[[str, str], Sequence[CandleBar]]
) -> RunCoreHandles:
    """Life 0 with the crashing/injected seam, then ONE clean relaunch life."""
    with contextlib.suppress(_SimulatedCrash):
        _one_life(root, grid, life_no=0, bars_for=first_bars)
    _result, core = _one_life(root, grid, life_no=1)
    return core


# ────── kill-timing sub-matrix (3 boundaries) ──────


def test_r42_kill_before_flush_resume_bit_identical(tmp_path: Path) -> None:
    """Boundary 1: the loader dies BEFORE tuple 2's flush → redo on resume."""
    grid = _grid()
    golden = _golden(tmp_path)
    root = tmp_path / "chaos"
    calls = {"n": 0}

    def _crashing_bars(_i: str, _t: str) -> Sequence[CandleBar]:
        # DEC-FILL-EXEC-GRID-1M-001: the dual-feed preload loads the decision
        # TF AND the 1-minute execution stream through this seam (2 calls per
        # tuple — this raw fake is not memoised), so tuple 1 consumes calls
        # 1-2 and the crash lands at call 3: the start of tuple 2's load,
        # the SAME logical boundary as the pre-grid single-feed `> 1`.
        calls["n"] += 1
        if calls["n"] > 2:
            raise _SimulatedCrash("loader crash before tuple 2's flush")
        return _BARS

    core = _relaunch_until_complete(root, grid, _crashing_bars)
    assert _result_hashes(root / "out") == golden
    kinds = [r["kind"] for r in _records(root, core.run_id)]
    assert "RESUME" in kinds  # the relaunch life announced its resume point
    manifest = json.loads((root / "out" / "runs" / core.run_id / "manifest.json").read_text())
    # Life 2's actuals: 1 tuple survived life 1 → 3 done + 1 resume-skip.
    assert manifest["actual"]["tuples_done"] == 3
    assert manifest["actual"]["tuples_skipped_resume"] == 1


def test_r42_kill_after_flush_before_sentinel_resume_bit_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary 2: store flushed, process dies BEFORE the sentinel lands →
    the un-sentineled tuple REDOES on resume (sentinel-last ordering law)."""
    from brutex.engine import sweep_run as sweep_run_mod
    from brutex.engine.tuple_complete import write_completion_sentinel as real

    grid = _grid()
    golden = _golden(tmp_path)
    root = tmp_path / "chaos"
    calls = {"n": 0}

    def _crash_on_second(path: Path, sentinel: Any) -> Path:
        calls["n"] += 1
        if calls["n"] == 2:
            raise _SimulatedCrash("killed after flush, before sentinel")
        return real(path, sentinel)

    monkeypatch.setattr(sweep_run_mod, "write_completion_sentinel", _crash_on_second)
    with contextlib.suppress(_SimulatedCrash):
        _one_life(root, grid, life_no=0)
    monkeypatch.setattr(sweep_run_mod, "write_completion_sentinel", real)
    _result, core = _one_life(root, grid, life_no=1)
    assert _result_hashes(root / "out") == golden
    assert "RESUME" in [r["kind"] for r in _records(root, core.run_id)]


def test_r42_kill_after_sentinel_before_checkpoint_resume_bit_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary 3: sentinel landed, process dies BEFORE the checkpoint records
    it → the sentinel short-circuits the redo (cheap resume, same bytes)."""
    from brutex.engine.sweep_checkpoint import SweepCheckpointStore as Store

    grid = _grid()
    golden = _golden(tmp_path)
    root = tmp_path / "chaos"
    real_record = Store.record
    calls = {"n": 0}

    def _crash_on_second(self: Store, index: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise _SimulatedCrash("killed after sentinel, before checkpoint")
        real_record(self, index)

    monkeypatch.setattr(Store, "record", _crash_on_second)
    with contextlib.suppress(_SimulatedCrash):
        _one_life(root, grid, life_no=0)
    monkeypatch.setattr(Store, "record", real_record)
    _result, core = _one_life(root, grid, life_no=1)
    assert _result_hashes(root / "out") == golden
    assert "RESUME" in [r["kind"] for r in _records(root, core.run_id)]


# ────── ENOSPC at EACH new write site (F9 fail-soft column) ──────


def _enospc(*_args: object, **_kwargs: object) -> None:
    raise OSError(errno.ENOSPC, "No space left on device (injected)")


@pytest.mark.parametrize(
    ("target", "expected_warn"),
    [
        ("manifest.json", "run_manifest.write_failed"),
        ("exit-status.json", "sweep_exit_status.write_failed"),
        ("stage2.stamp.json", "sweep_run_core.observability_failed"),
        ("sweep-progress.json", "sweep_supervisor.progress_write_failed"),
        ("chain-map.json", "chain_map.write_failed"),
        ("diagnostic-bundle.zip", "diag_bundle.build_failed"),
    ],
)
def test_r7_enospc_at_each_observability_write_site_run_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, expected_warn: str
) -> None:
    """F9/I-3: a dead disk at ANY observability write site never kills compute
    — the run completes and the RESULT bytes equal an uninjected run's."""
    grid = _grid()
    golden = _golden(tmp_path)
    root = tmp_path / "chaos"
    real_replace = os.replace

    def _selective_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        if Path(os.fspath(dst)).name == target:
            _enospc()
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", _selective_replace)
    with capture_logs() as logs:
        result, _core = _one_life(root, grid, life_no=0)
    monkeypatch.setattr(os, "replace", real_replace)
    assert result.tuples_failed == 0
    assert _result_hashes(root / "out") == golden
    # The failure was LOUD — the site's OWN fail-soft WARN fired, never silent.
    warn_events = {e["event"] for e in logs if e["log_level"] == "warning"}
    assert expected_warn in warn_events, f"no {expected_warn} for {target}: {warn_events}"


def test_r41_enospc_at_flight_append_degrades_and_completes(tmp_path: Path) -> None:
    """D-20: a dead disk at the flight-append site latches degraded (ONE WARN,
    drops counted, no retry spam) and the run + manifest still complete."""
    grid = _grid()
    golden = _golden(tmp_path)
    root = tmp_path / "chaos"
    out = root / "out"
    ingest_path = out / "audit" / "sweep-all.jsonl"
    ingest_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        capture_logs() as logs,
        sweep_run_core(
            results_root=out,
            grid=grid,
            alerter=SweepAlerter(),
            entry_label="chaos flight enospc",
            cli_chain_path=None,
            tripwire_config=TripwireConfig(),
            diffcheck_k=0,
        ) as core,
    ):
        writer = core.recorder._writer  # pyright: ignore[reportPrivateUsage]
        assert writer is not None
        writer.append = _enospc  # type: ignore[assignment,method-assign]
        core.stage("sweep")
        with (
            AuditWriter.open(ingest_path) as ingest,
            _guards(root / "state", grid, resume=False) as guards,
        ):
            result = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: _BARS,
                store_for=make_store_for(out, grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                on_tuple=core.on_tuple,
                on_progress=core.on_progress,
            )
        core.grid_done(result)
        core.set_exit_code(0)
    assert result.tuples_failed == 0
    assert _result_hashes(out) == golden
    assert core.recorder.degraded is True
    assert core.recorder.dropped > 0
    warns = [e for e in logs if e["event"] == "flight_recorder.append_failed"]
    assert len(warns) == 1  # ONE WARN then silent — no retry spam (D-20)
    manifest = json.loads((out / "runs" / core.run_id / "manifest.json").read_text())
    assert manifest["recorder_degraded"] is True


def test_r42_enospc_during_store_flush_aborts_loudly_earlier_tuple_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead disk at the RESULT store flush is a MATERIAL failure: the run
    aborts loudly (exit-status ``failed`` + HALT record) and tuple 1's
    already-persisted artifacts are byte-untouched (the one-line law)."""
    import brutex.engine.results_store as results_store_mod

    grid = _grid()
    root = tmp_path / "chaos"
    real_write = results_store_mod._atomic_write  # pyright: ignore[reportPrivateUsage]
    seen: list[Path] = []

    def _fail_second_store(path: Path, content: bytes) -> None:
        if path.suffix == ".parquet" and path not in seen:
            seen.append(path)
        if path.suffix == ".parquet" and len(seen) >= 2:
            _enospc()
        real_write(path, content)

    monkeypatch.setattr(results_store_mod, "_atomic_write", _fail_second_store)
    with pytest.raises(OSError, match="No space left"):
        _one_life(root, grid, life_no=0)
    monkeypatch.setattr(results_store_mod, "_atomic_write", real_write)

    out = root / "out"
    first_tuple_files = {
        p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.rglob("*.parquet"))
    }
    assert len(first_tuple_files) == 1  # tuple 1 persisted; tuple 2 never landed
    run_dirs = sorted((out / "runs").iterdir())
    assert len(run_dirs) == 1
    status = json.loads((run_dirs[0] / "exit-status.json").read_text())
    assert status["state"] == "failed"
    assert status["error_type"] == "OSError"
    records = list(iter_run_records(out, run_dirs[0].name))
    halts = [r for r in records if r["kind"] == "HALT"]
    assert len(halts) == 1
    # Recovery starts at N: the relaunch completes and tuple 1's bytes never moved.
    _one_life(root, grid, life_no=1)
    for path, digest in first_tuple_files.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


# ────── corrupt checkpoint / corrupt store ──────


@pytest.mark.parametrize("garbage", [b"\x00garbage{{{", b'{"sweep_id": "truncat'])
def test_g12_corrupt_checkpoint_quarantined_fresh_start_no_crash_loop(
    tmp_path: Path, garbage: bytes
) -> None:
    grid = _grid()
    golden = _golden(tmp_path)
    root = tmp_path / "chaos"
    _one_life(root, grid, life_no=0)
    ckpt = root / "state" / "ckpt.json"
    ckpt.write_bytes(garbage)
    _result2, core2 = _one_life(root, grid, life_no=1)
    assert _result_hashes(root / "out") == golden
    forensics = sorted((root / "out" / ".quarantine" / "checkpoints").glob("*"))
    assert len(forensics) == 1
    assert forensics[0].read_bytes() == garbage  # kept for forensics, never deleted
    kinds = [r["kind"] for r in _records(root, core2.run_id)]
    assert "QUARANTINE" in kinds


def test_g1_corrupt_tuple_store_mid_grid_quarantined_grid_completes(tmp_path: Path) -> None:
    """A pre-existing garbage store at a MID-GRID tuple's path historically
    aborted the whole grid; the G1 seam quarantines the trio + redoes it."""
    from brutex.engine.sweep_orchestrator import iter_sweep_tuples, tuple_store_path

    grid = _grid()
    golden = _golden(tmp_path)
    root = tmp_path / "chaos"
    out = root / "out"
    # Poison the SECOND tuple's store BEFORE the grid ever runs — the seam
    # engages when run_one_tuple opens the existing-but-unreadable Parquet.
    tuples = list(iter_sweep_tuples(grid))
    poisoned = tuple_store_path(out, tuples[1], grid)
    poisoned.parent.mkdir(parents=True, exist_ok=True)
    poisoned.write_bytes(b"\x00not-a-parquet")
    result, core = _one_life(root, grid, life_no=0)
    assert result.tuples_failed == 0  # the grid COMPLETED — no abort
    assert _result_hashes(out) == golden  # the redo regenerated the bytes
    runtime = sorted((out / ".quarantine" / "runtime").iterdir())
    assert len(runtime) == 1
    quarantined = sorted(runtime[0].iterdir())
    assert [p.name for p in quarantined] == [poisoned.name]
    assert quarantined[0].read_bytes() == b"\x00not-a-parquet"  # forensics kept
    assert "QUARANTINE" in [r["kind"] for r in _records(root, core.run_id)]


# ────── stage-3 cell torn between stamp and consume (D-4) ──────


def test_r42_stage3_cell_torn_between_stamp_and_consume_discarded_and_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cell torn AFTER stamping fails validation → discard + ONE idempotent
    re-run; the discarded artifact is KEPT and unrelated files never move."""
    from brutex.engine import sweep_run_core as core_mod

    grid = _grid()
    out = tmp_path / "out"
    cell = out / "cell.parquet"
    unrelated = out / "unrelated.bin"
    out.mkdir(parents=True)
    unrelated.write_bytes(b"earlier-artifact")
    before = hashlib.sha256(unrelated.read_bytes()).hexdigest()

    from brutex.observability.stage_stamp import stamp_stage3_cells as real_stamp

    tears = {"n": 0}

    def _stamp_then_tear(
        cells: Sequence[Path], *, run_id: str, snapshot_hash: str | None = None
    ) -> int:
        n = real_stamp(cells, run_id=run_id, snapshot_hash=snapshot_hash)
        if tears["n"] == 0:
            tears["n"] += 1
            cell.write_bytes(b"torn")  # the artifact tears AFTER its stamp landed
        return n

    monkeypatch.setattr(core_mod, "stamp_stage3_cells", _stamp_then_tear)

    from types import SimpleNamespace

    factory_calls = {"n": 0}

    def _factory() -> Any:
        factory_calls["n"] += 1
        cell.write_bytes(b"good-cell-bytes")
        return SimpleNamespace(
            cells=(SimpleNamespace(path=cell, rows=1, tuple_=None),),
            train_snapshot_hash="ab" * 32,
        )

    with sweep_run_core(
        results_root=out,
        grid=grid,
        alerter=SweepAlerter(),
        entry_label="chaos stage3 tear",
        tripwire_config=TripwireConfig(),
        diffcheck_k=0,
    ) as core:
        core.stage("stage3")
        core.run_stage3(_factory)
        run_id = core.run_id
        core.set_exit_code(0)
    assert factory_calls["n"] == 2  # exactly ONE re-run of the idempotent pass
    assert cell.read_bytes() == b"good-cell-bytes"
    discarded = sorted(out.glob("cell.parquet.discarded.*"))
    assert len(discarded) == 1
    assert discarded[0].read_bytes() == b"torn"  # forensics kept
    assert "QUARANTINE" in [r["kind"] for r in list(iter_run_records(out, run_id))]
    assert hashlib.sha256(unrelated.read_bytes()).hexdigest() == before


# ────── torn flight-recorder tail ──────


def test_r41_torn_flight_recorder_tail_resumes_and_verifies(tmp_path: Path) -> None:
    """A half-written last JSONL line (power loss mid-append) is truncated on
    resume; the chain keeps appending and verifies end-to-end."""
    from brutex.observability.flight_recorder import FlightRecorder, recorder_path

    rec = FlightRecorder.open(tmp_path, run_id="ab" * 32, attempt=1)
    assert rec.record("RUN_START", {}) is True
    assert rec.record("CHECKPOINT", {"index": 1}) is True
    rec.close()
    path = recorder_path(tmp_path, "ab" * 32)
    whole = path.read_bytes()
    path.write_bytes(whole[:-25])  # tear the tail mid-record
    rec2 = FlightRecorder.open(tmp_path, run_id="ab" * 32, attempt=2)
    assert rec2.record("RESUME", {"resume_from": 1}) is True
    rec2.close()
    records = list(iter_run_records(tmp_path, "ab" * 32))
    assert [r["kind"] for r in records] == ["RUN_START", "RESUME"]
    assert auto_verify(path).entry_count == 2


# ────── stall (injected clock) ──────


def test_g2_stall_watchdog_fires_once_under_injected_clock(tmp_path: Path) -> None:
    policy = resolve_stall_policy(per_tuple_estimate_s=None, workers=1, env={})
    assert policy.timeout_seconds == 300.0  # the behavior-identical default
    now = {"t": 1000.0}
    hung: list[str] = []
    wd = SweepWatchdog(
        timeout_seconds=policy.timeout_seconds,
        on_hung=lambda event: hung.append(event.worker_name),
        clock=lambda: now["t"],
    )
    wd.beat("tuple-worker")
    now["t"] += policy.timeout_seconds + 1.0
    assert len(wd.check_once()) == 1
    assert wd.check_once() == ()  # hysteresis: a stuck worker fires ONCE
    assert hung == ["tuple-worker"]


def test_g2_scaled_policy_prevents_false_abort_of_slow_but_alive_tuple(
    tmp_path: Path,
) -> None:
    """A big-grid tuple legitimately slower than 300 s no longer false-aborts:
    the scaled policy RAISES the bound (raises-only law) above the estimate."""
    policy = resolve_stall_policy(per_tuple_estimate_s=120.0, workers=1, env={})
    assert policy.timeout_seconds == 1200.0  # 10x the estimate — scaled, source
    assert policy.source == "scaled"
    now = {"t": 0.0}
    hung: list[str] = []
    wd = SweepWatchdog(
        timeout_seconds=policy.timeout_seconds,
        on_hung=lambda event: hung.append(event.worker_name),
        clock=lambda: now["t"],
    )
    wd.beat("slow-tuple")
    now["t"] += 400.0  # would have false-fired under the fixed 300 s constant
    assert wd.check_once() == ()
    assert hung == []


# ────── concurrency: G6 single-flight + G10 lake locks ──────


def test_g6_concurrent_second_sweep_refused_and_winner_chain_verifies(
    tmp_path: Path,
) -> None:
    grid = _grid()
    root = tmp_path / "chaos"
    out = root / "out"
    out.mkdir(parents=True)
    with sweep_run_core(
        results_root=out,
        grid=grid,
        alerter=SweepAlerter(),
        entry_label="winner",
        tripwire_config=TripwireConfig(),
        diffcheck_k=0,
    ) as core:
        # A second sweep-lock acquisition while `core` holds it MUST be refused
        # at __enter__ (SweepAlreadyRunningError). pytest.raises is the tooth —
        # a wrongly-granted lock enters the body, appends the marker, and the
        # assert below fails loudly. A marker LIST (not a bare bool) keeps the
        # post-`with` assertions visibly reachable to static analysis — a
        # `flag = True` body constant-folds into an always-failing assert on
        # the normal-exit CFG path, so CodeQL marked everything after it
        # unreachable; list contents are not constant-propagated.
        second_sweep_entries: list[str] = []
        with pytest.raises(SweepAlreadyRunningError) as excinfo, sweep_run_lock(out):
            second_sweep_entries.append("second sweep body ran")
        assert not second_sweep_entries, "the second sweep must never proceed"
        assert excinfo.value.holder_pid == os.getpid()
        run_id = core.run_id
        core.set_exit_code(0)
    # The loser's refusal forked nothing: the winner's chain verifies clean.
    from brutex.observability.flight_recorder import recorder_path

    assert auto_verify(recorder_path(out, run_id)).entry_count > 0


def test_g10_preload_refused_while_pull_holds_the_lake_lock(tmp_path: Path) -> None:
    import fcntl

    from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock_path

    lake = tmp_path / "lake"
    lock_path = pull_singleflight_lock_path(lake)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as fh:  # the pull's EXCLUSIVE hold
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LakeBusyError, match="pull/heal is running"), lake_read_guard(lake):
            raise AssertionError("preload must refuse while a pull holds EX")


def test_g10_pull_refused_while_preload_holds_the_shared_lock(tmp_path: Path) -> None:
    import fcntl

    from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock_path

    lake = tmp_path / "lake"
    lock_path = pull_singleflight_lock_path(lake)
    with (
        lake_read_guard(lake),  # the sweep preload's SHARED hold
        lock_path.open("w") as fh,
        pytest.raises(BlockingIOError),
    ):
        # A starting pull's EX|NB acquisition fails while SH is held —
        # its own loud refusal path (wording debt pinned, D-13).
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Guard released → the pull acquires cleanly.
    with lock_path.open("w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ────── G11: read-only progress dir ──────


def test_g11_readonly_progress_dir_run_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable progress file never kills compute — WARNed + continued.

    Injection is an EACCES at the progress file's atomic-replace site (a chmod
    0o555 dir is a no-op under the CI container's root user, so the permission
    fault is injected at the syscall the chmod would have failed)."""
    grid = _grid()
    golden = _golden(tmp_path)
    root = tmp_path / "chaos"
    out = root / "out"
    real_replace = os.replace

    def _eacces_on_progress(src: Any, dst: Any, **kwargs: Any) -> None:
        if Path(os.fspath(dst)).name == "sweep-progress.json":
            raise OSError(errno.EACCES, "Permission denied (injected read-only dir)")
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", _eacces_on_progress)
    with capture_logs() as logs:
        result, _core = _one_life(root, grid, life_no=0)
    monkeypatch.setattr(os, "replace", real_replace)
    assert result.tuples_failed == 0
    assert _result_hashes(out) == golden
    warns = {e["event"] for e in logs if e["log_level"] == "warning"}
    assert "sweep_supervisor.progress_write_failed" in warns


# ────── R7: detached child is a NEW session leader ──────


def test_r7_detached_spawn_is_a_new_session_leader_and_completes(tmp_path: Path) -> None:
    """The detach mechanism: the child runs in its OWN session (the dashboard's
    death cannot signal it) and its work lands after the spawn returns."""
    import sys
    import time as time_mod

    from brutex.engine.sweep_detach import spawn_detached_sweep

    marker = tmp_path / "child-done.txt"
    script = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('done')"
    handle = spawn_detached_sweep(
        [],
        results_root=tmp_path,
        command=[sys.executable, "-c", script],
    )
    assert handle.pid != os.getpid()
    assert os.getsid(handle.pid) != os.getsid(0)  # its OWN session — detached
    deadline = time_mod.monotonic() + 30.0
    while not marker.exists() and time_mod.monotonic() < deadline:
        time_mod.sleep(0.05)
    assert marker.read_text() == "done"
