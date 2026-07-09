"""End-to-end integration for the resilience sidecar via the REAL results-depth pass.

Drives a real ``run_persisted_sweep`` per tuple then
``run_results_depth_pass`` on the fixed 12-day fixture (the
``test_engine_results_depth_pass`` chain, no own-module mocks): the sidecar +
stamp are written, the top100 entries carry the ``resilience`` block, resume
skips both, the adoption backfill rebuilds byte-identically WITHOUT re-sim, and
the per-tuple fence swallows a fold failure while a global stop re-raises.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine import results_depth_pass
from brutex.engine.resilience_streaks import load_resilience_stamp, read_resilience_records
from brutex.engine.results_depth_pass import run_results_depth_pass
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    iter_sweep_tuples,
    load_or_create_store,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup, run_persisted_sweep
from brutex.engine.sweep_supervisor import grid_identity
from brutex.engine.top100_artifact import TOP100_FILENAME, load_top100
from brutex.engine.trade_store import sidecar_paths_for, sweep_run_dir
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("polars")

_IST = timezone(timedelta(hours=5, minutes=30))


def _bars() -> list[CandleBar]:
    out: list[CandleBar] = []
    for d in range(12):
        price = 100.0
        for m in range(120):
            close = price + 0.6 if m % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            hi = max(price, close) + 0.5
            lo = min(price, close) - 0.5
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(hi, 2))),
                    low=Decimal(str(round(lo, 2))),
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
        timeframes=[Timeframe(label="1minute", minutes=1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _run_sweep(out_root: Path) -> None:
    audit_path = out_root / "audit" / "sweep-all.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with AuditWriter(path=audit_path) as audit:
        for tuple_ in iter_sweep_tuples(_grid()):
            store = load_or_create_store(tuple_store_path(out_root, tuple_, _grid()))
            dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
            run_persisted_sweep(
                _BARS, tuple_.to_run_config(_grid()), store=store, dedup=dedup, audit=audit
            )


@pytest.fixture
def swept(tmp_path: Path) -> Path:
    root = tmp_path / "sweeps"
    root.mkdir()
    _run_sweep(root)
    return root


def _run_pass(out_root: Path, **kwargs: object) -> results_depth_pass.ResultsDepthReport:
    return run_results_depth_pass(
        out_root=out_root,
        grid=_grid(),
        bars_for=lambda _i, _tf: _BARS,
        minute_bars_for=lambda _i: _BARS,
        **kwargs,  # type: ignore[arg-type]
    )


def _paths(out_root: Path) -> object:
    tuple_ = next(iter_sweep_tuples(_grid()))
    return sidecar_paths_for(
        tuple_store_path(out_root, tuple_, _grid()), out_root, run_id=grid_identity(_grid())
    )


def test_fresh_pass_writes_resilience_and_top100_carries_it(swept: Path) -> None:
    report = _run_pass(swept)
    assert report.tuples_processed == 1
    paths = _paths(swept)
    assert load_resilience_stamp(paths) is not None  # type: ignore[arg-type]
    records = read_resilience_records(paths)  # type: ignore[arg-type]
    assert records

    run_id = grid_identity(_grid())
    runs_dir = sweep_run_dir(swept, run_id).parent
    artifact = load_top100(runs_dir, run_id)
    assert artifact is not None
    assert artifact.entries
    for entry in artifact.entries:
        block = entry["resilience"]
        assert isinstance(block, dict)
        assert "stats_live" in block
        # The cross-check against the REAL aggregate_combo output (§b tension 3):
        # the persisted P95 is never above the agg's max_consecutive_losses.
        rec = records[entry["run_id"]]  # type: ignore[index]
        metrics = entry["metrics"]
        assert isinstance(metrics, dict)
        if rec.losing_streak_p95 is not None:
            assert rec.losing_streak_p95 <= metrics["max_consecutive_losses"]


def test_resume_skips_both_when_current(swept: Path) -> None:
    _run_pass(swept)
    with capture_logs() as logs:
        report = _run_pass(swept)
    assert report.tuples_skipped_current == 1
    assert report.tuples_processed == 0
    assert any(e["event"] == "results_depth.tuple_skipped_current" for e in logs)


def test_adoption_backfill_rebuilds_without_resim(
    swept: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_pass(swept)
    paths = _paths(swept)
    pristine = paths.resilience.read_bytes()  # type: ignore[attr-defined]

    # Delete ONLY the resilience sidecar + stamp — the trio stays current.
    paths.resilience.unlink()  # type: ignore[attr-defined]
    paths.resilience_stamp.unlink()  # type: ignore[attr-defined]

    # Spy: capture_combo (the re-sim) must NOT run on the backfill path.
    import brutex.engine.results_depth_pass as mod

    calls = {"n": 0}
    real = mod.capture_combo

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "capture_combo", _spy)

    with capture_logs() as logs:
        report = _run_pass(swept)

    assert calls["n"] == 0  # NO re-simulation
    assert report.resilience_backfilled == 1
    assert report.tuples_processed == 0
    assert any(e["event"] == "results_depth.resilience_backfilled" for e in logs)
    # Byte-identical rebuild (the backfill reads the trio, folds, converges).
    assert paths.resilience.read_bytes() == pristine  # type: ignore[attr-defined]


def test_fence_swallows_a_fold_failure(swept: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import brutex.engine.results_depth_pass as mod

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("fold exploded")

    monkeypatch.setattr(mod, "fold_resilience", _boom)
    with capture_logs() as logs:
        report = _run_pass(swept)
    assert report.tuples_errored == 1  # counted, never fails the sweep
    assert report.tuples_processed == 0
    assert any(e["event"] == "results_depth.tuple_error" for e in logs)


def test_memory_error_re_raises(swept: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import brutex.engine.results_depth_pass as mod

    def _oom(*args: object, **kwargs: object) -> object:
        raise MemoryError

    monkeypatch.setattr(mod, "fold_resilience", _oom)
    with pytest.raises(MemoryError):
        _run_pass(swept)


def test_top100_json_on_disk_carries_resilience_key(swept: Path) -> None:
    _run_pass(swept)
    run_id = grid_identity(_grid())
    runs_dir = sweep_run_dir(swept, run_id).parent
    payload = json.loads((runs_dir / run_id / TOP100_FILENAME).read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    for entry in payload["entries"]:
        assert "resilience" in entry
        assert "stats_live" in entry["resilience"]
