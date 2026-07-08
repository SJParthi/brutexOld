"""O(1)-per-call gates for the nightly PROBES (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Gated surfaces — each must stay latency-flat as the sweeps tree deepens:

1. :func:`check_disk_floors` — two ``disk_usage`` probes + pure arithmetic
   (the INVENTORY size is an input, not computed here);
2. :func:`probe_sweep_liveness` — at most three stats + one small JSON read;
3. :func:`write_report` — one atomic JSON write whose size depends on the
   REPORT (fixed 6 links), never on the lake.

The ratio ``mean_ns(deep tree) / mean_ns(small tree)`` is gated through
:func:`tests.bench._o1_tiers.ratio_ceiling` (robust shared-CI ceiling; strict
1.1× on the M4-Pro tier).

Honest envelope (Golden Rule #10): BACKUP is O(files + bytes) BY DESIGN —
asserted LINEAR-ish below (a deeper tree must cost MORE), explicitly NOT an
O(1) claim; the DISK link's inventory walk is likewise O(files) and stated so
in the runbook — it is deliberately not gated flat here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from brutex.nightly.backup import create_snapshot
from brutex.nightly.chain import (
    LINK_ORDER,
    LinkOutcome,
    LinkStatus,
    NightlyReport,
)
from brutex.nightly.probes import check_disk_floors, probe_sweep_liveness
from brutex.nightly.report import write_report

from tests.bench._o1_tiers import ratio_ceiling

#: Sweeps-tree file counts for the small vs deep tiers.
N_SMALL = 20
N_DEEP = 2_000

#: Timing repetitions (best-of-N min — the house noise-robust idiom).
_REPS = 30


def _build_tree(root: Path, files: int) -> tuple[Path, Path]:
    lake = root / "lake"
    results = lake / "sweeps"
    (lake / "window_ledger").mkdir(parents=True)
    (lake / "window_ledger" / "ledger.parquet").write_bytes(b"L")
    (results / "NIFTY" / "5minute").mkdir(parents=True)
    for i in range(files):
        (results / "NIFTY" / "5minute" / f"t{i:05d}.parquet").write_bytes(b"x" * 64)
    (results / "progress").mkdir()
    (results / "progress" / "sweep-progress.json").write_text('{"state": "done"}', encoding="utf-8")
    return lake, results


def _best_ns(fn: object, reps: int = _REPS) -> float:
    best = float("inf")
    for _ in range(reps):
        start = time.perf_counter_ns()
        fn()  # type: ignore[operator]
        best = min(best, float(time.perf_counter_ns() - start))
    return max(best, 1.0)


def _report(results_root: Path, nightly_id: str) -> NightlyReport:
    return NightlyReport(
        schema_version=1,
        nightly_id=nightly_id,
        lake_root="/lake",
        results_root=str(results_root),
        backup_root="/backups",
        started_utc=0.0,
        ended_utc=1.0,
        links=tuple(
            LinkOutcome(
                link=link,
                status=LinkStatus.OK,
                started_utc=0.0,
                ended_utc=1.0,
                detail="",
                counts={},
                error_type=None,
                blocked_by=None,
                exit_hint=None,
            )
            for link in LINK_ORDER
        ),
        tripwires=(),
        counts={},
        snapshot_id=None,
        absent_artifacts=(),
        honesty_notes=(),
        dry_run=False,
    )


def test_probes_and_report_write_flat_vs_tree_depth(tmp_path: Path) -> None:
    small_lake, small_results = _build_tree(tmp_path / "small", N_SMALL)
    deep_lake, deep_results = _build_tree(tmp_path / "deep", N_DEEP)
    env = {
        "BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES": "0",
        "BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES": "0",
    }

    def disk(lake: Path) -> object:
        return check_disk_floors(lake_root=lake, backup_root=lake, inventory_bytes=10**9, env=env)

    def live(results: Path) -> object:
        return probe_sweep_liveness(results, fresh_seconds=900.0)

    counter = {"n": 0}

    def report_write(results: Path) -> object:
        counter["n"] += 1
        return write_report(results, _report(results, f"nightly-bench-{counter['n']:05d}"))

    ceiling = ratio_ceiling(5.0)
    for name, fn in (("disk", disk), ("liveness", live), ("report", report_write)):
        small_arg = small_lake if name == "disk" else small_results
        deep_arg = deep_lake if name == "disk" else deep_results
        small_ns = _best_ns(lambda fn=fn, arg=small_arg: fn(arg))
        deep_ns = _best_ns(lambda fn=fn, arg=deep_arg: fn(arg))
        ratio = deep_ns / small_ns
        assert ratio <= ceiling, (
            f"{name} probe latency grew {ratio:.2f}x for a {N_DEEP // N_SMALL}x deeper "
            f"sweeps tree (ceiling {ceiling:.2f}x) — no longer O(1) per call"
        )


def test_backup_is_honestly_linear_not_o1(tmp_path: Path) -> None:
    """Backup cost MUST grow with the tree — the honest NON-O(1) assertion."""
    small_lake, small_results = _build_tree(tmp_path / "s", 10)
    deep_lake, deep_results = _build_tree(tmp_path / "d", 1_000)

    start = time.perf_counter_ns()
    small = create_snapshot(
        lake_root=small_lake,
        results_root=small_results,
        backup_root=tmp_path / "bk-s",
        snapshot_id="s",
        nightly_id=None,
    )
    small_ns = time.perf_counter_ns() - start
    start = time.perf_counter_ns()
    deep = create_snapshot(
        lake_root=deep_lake,
        results_root=deep_results,
        backup_root=tmp_path / "bk-d",
        snapshot_id="d",
        nightly_id=None,
    )
    deep_ns = time.perf_counter_ns() - start
    assert deep.file_count > small.file_count * 50
    # 100x the files must cost MORE wall time — O(files + bytes), never O(1).
    assert deep_ns > small_ns
    # And the manifests carry the real per-file entries (no sampling shortcut).
    manifest = json.loads((deep.snapshot_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["item_count"] == deep.file_count
