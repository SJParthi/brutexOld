"""The I-1 proof — run-core ON vs OFF byte identity (DEC-SWEEP-UNKILLABLE-CORE-001 WP-5).

Runs the SAME grid twice through the REAL sequential supervisor: once plain
(run-core OFF — the pre-Phase-2 wiring) and once fully wrapped in
:func:`brutex.engine.sweep_run_core.sweep_run_core` with EVERY Phase-2 default
ON (recorder + tripwires + manifest + composed hooks + diffcheck + G1
quarantine seam + the D-3 ``RUN_MANIFEST`` chain append), then asserts:

* the per-tuple result trees (store Parquet + trial-stats sidecar + completion
  sentinel) are SHA-256-identical file for file;
* the CLI-level SHA-256 ingest chains are content-equal modulo timestamps,
  with EXACTLY ONE additive ``RUN_MANIFEST`` action appended on the ON side
  (D-3) — and BOTH chains independently verify;
* the supervisor BLAKE3 chains carry identical TUPLE_DONE content;
* the checkpoints advanced to the same high-water mark.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

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
from brutex.engine.sweep_supervisor import (
    SweepGuards,
    grid_identity,
    run_supervised_grid,
)
from brutex.engine.sweep_watchdog import SweepWatchdog
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.observability.run_tripwires import TripwireConfig
from brutex.orchestrator.audit import iter_entries
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))

#: Phase-2 state dirs the ON run adds under the results root — everything
#: OUTSIDE these must be byte-identical to the OFF run.
_PHASE2_PREFIXES = ("runs/", ".locks/", ".quarantine/", "progress/", "audit/")


def _trending_bars(n_days: int = 3, per_day: int = 50) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            bars.append(
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
    return bars


_BARS = _trending_bars()


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
def _guards(tmp: Path, grid: SweepGridConfig) -> Generator[SweepGuards, None, None]:
    audit = SweepAuditWriter.open(tmp / "sup.jsonl")
    cache = SweepCache.open(tmp / "cache", bloom_capacity=10_000)
    checkpoint = SweepCheckpointStore(
        path=tmp / "ckpt.json",
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


def _result_tree(root: Path) -> dict[str, str]:
    """SHA-256 of every RESULT file under *root* (Phase-2 state excluded)."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if any(rel.startswith(prefix) for prefix in _PHASE2_PREFIXES):
            continue
        if rel.endswith(".stamp.json") or rel == "stage2":
            continue  # Phase-2 sidecars beside the results (additive by design)
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _chain_lines(path: Path) -> list[tuple[int, str, str, dict[str, Any]]]:
    """The ingest chain reduced to (seq, action, subject, payload) — ts-masked."""
    return [(e.seq, e.action, e.subject, e.payload) for e in iter_entries(path)]


def _sup_tuple_lines(path: Path) -> list[tuple[str, object, object]]:
    from brutex.engine.sweep_audit import read_entries

    return [
        (e.action, e.payload.get("tuple"), e.payload.get("store_content_hash"))
        for e in read_entries(path)
        if e.action == "TUPLE_DONE"
    ]


def _run_off(tmp: Path, grid: SweepGridConfig) -> tuple[Path, Path, int]:
    """The run-core-OFF reference (the exact pre-Phase-2 wiring)."""
    from brutex.orchestrator.audit import AuditWriter

    out = tmp / "out"
    ingest_path = out / "audit" / "sweep-all.jsonl"
    ingest_path.parent.mkdir(parents=True, exist_ok=True)
    with AuditWriter.open(ingest_path) as ingest, _guards(tmp, grid) as guards:
        run_supervised_grid(
            grid,
            bars_for=_bars_for,
            store_for=make_store_for(out, grid),
            dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
            ingest_audit=ingest,
            guards=guards,
        )
        ckpt = guards.checkpoint.next_index
    return out, ingest_path, ckpt


def _run_on(tmp: Path, grid: SweepGridConfig) -> tuple[Path, Path, int, RunCoreHandles]:
    """The run-core-ON arm: EVERY Phase-2 default engaged, incl. diffcheck."""
    from brutex.orchestrator.audit import AuditWriter

    out = tmp / "out"
    ingest_path = out / "audit" / "sweep-all.jsonl"
    ingest_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path = out / "progress" / "sweep-progress.json"
    handles: RunCoreHandles | None = None
    with sweep_run_core(
        results_root=out,
        grid=grid,
        alerter=SweepAlerter(),
        entry_label="byte-identity test",
        progress_path=progress_path,
        cli_chain_path=ingest_path,
        tripwire_config=TripwireConfig(),
        diffcheck_k=8,  # the CI_K rehearsal budget — diffcheck genuinely ON
        selection_extra={"exchange": "NSE", "segment": "INDEX"},
    ) as core:
        handles = core
        core.stage("sweep")
        with AuditWriter.open(ingest_path, resume=ingest_path.exists()) as ingest:
            with _guards(tmp, grid) as guards:
                result = run_supervised_grid(
                    grid,
                    bars_for=_bars_for,
                    store_for=make_store_for(out, grid),
                    dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                    ingest_audit=ingest,
                    guards=guards,
                    on_tuple=core.on_tuple,
                    on_progress=core.on_progress,
                    diffcheck=core.diffcheck,
                    store_quarantine=core.store_quarantine,
                )
                ckpt = guards.checkpoint.next_index
        core.grid_done(result)
        core.set_exit_code(0)
    return out, ingest_path, ckpt, cast("RunCoreHandles", handles)


def test_runcore_on_vs_off_result_trees_are_byte_identical(tmp_path: Path) -> None:
    grid = _grid()
    off_root, off_chain, off_ckpt = _run_off(tmp_path / "off", grid)
    on_root, on_chain, on_ckpt, core = _run_on(tmp_path / "on", grid)

    off_tree = _result_tree(off_root)
    on_tree = _result_tree(on_root)
    assert len(off_tree) > 0
    assert off_tree == on_tree  # per-tuple SHA-256 maps identical (I-1)
    assert off_ckpt == on_ckpt
    assert core.suspect is False  # a clean run never trips diffcheck/tripwires

    # Chains: content-equal modulo timestamps + EXACTLY ONE additive
    # RUN_MANIFEST tail line on the ON side (D-3); both verify independently.
    off_lines = _chain_lines(off_chain)
    on_lines = _chain_lines(on_chain)
    assert len(on_lines) == len(off_lines) + 1
    assert [line[1:] for line in on_lines[:-1]] == [line[1:] for line in off_lines]
    tail = on_lines[-1]
    assert tail[1] == "RUN_MANIFEST"
    assert tail[2] == grid_identity(grid)
    assert set(tail[3]) == {"manifest", "manifest_content_hash"}
    assert auto_verify(off_chain).entry_count == len(off_lines)
    assert auto_verify(on_chain).entry_count == len(on_lines)

    # Supervisor BLAKE3 chains: identical TUPLE_DONE content (never touched).
    off_sup = _sup_tuple_lines(tmp_path / "off" / "sup.jsonl")
    on_sup = _sup_tuple_lines(tmp_path / "on" / "sup.jsonl")
    assert len(off_sup) > 0
    assert off_sup == on_sup


def test_runcore_on_run_dir_carries_the_phase2_artifacts(tmp_path: Path) -> None:
    grid = _grid()
    _on_root, _chain, _ckpt, core = _run_on(tmp_path, grid)
    run_dir = tmp_path / "out" / "runs" / core.run_id
    for name in (
        "flight-recorder.jsonl",
        "manifest.json",
        "exit-status.json",
        "chain-map.json",
        "diagnostic-bundle.zip",
        "stage2",
    ):
        assert (run_dir / name).exists(), name
    status = json.loads((run_dir / "exit-status.json").read_text())
    assert status["state"] == "complete"
    assert status["exit_code"] == 0
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["run_id"] == core.run_id
    assert manifest["suspect"] is False
    assert manifest["actual"]["tuples_done"] == 4
    # The flight recorder chain verifies with the SAME auto verifier (D-2).
    assert auto_verify(run_dir / "flight-recorder.jsonl").entry_count > 0
