"""HONEST cost bench for the movers/db generation probe (DEC-O1-BENCH-GAP-CLOSURE-001).

``tests/bench/test_movers_cache_o1.py`` proves the movers/db "cached handoff" is
O(1) — but it stubs ``generation=0``, so it NEVER exercises the per-poll freshness
PROBE (:func:`brutex.dashboard.lake_revision.sweeps_revision` /
:func:`~brutex.dashboard.lake_revision.sweep_results_revision`) that actually decides
hit-vs-miss on a live lake. This bench closes that gap and pins the probe's TRUE
per-poll cost, honestly (Golden Rule #10).

Two facts are pinned:

1. **The probe is NOT O(files) (the meaningful guarantee).** It is a bounded
   *directory*-mtime digest — one ``scandir`` of the sweeps root plus the two-level
   ``<instrument>/<tf>/`` dirs — so its cost is flat vs the number of RESULT FILES
   inside each ``<tf>/`` dir. Measured flat under ``ratio_ceiling(5.0)``
   (``tests/bench/_o1_tiers.py``). A regression that walked the store files per probe
   would scale with the file count and blow past this bound.

2. **The probe IS O(dirs), NOT O(1) — flagged, not hidden.** Its cost grows roughly
   LINEARLY with the number of ``<instrument>`` / ``<tf>`` directories (bounded in
   production by instruments × timeframes — dozens, per the module docstring). This
   bench MEASURES that growth and asserts it is (a) genuinely present — so the
   movers_cache "O(1) handoff" wording is not mistaken for "O(1) per poll" — and
   (b) at most roughly LINEAR (not quadratic). So the honest per-poll complexity of a
   ``/movers`` or ``/results`` freshness check is **O(dirs)**, i.e. O(instruments ×
   timeframes), never O(1) and never O(files).

Best-of-N-MIN timing (DEC-CANARY-MINTIME-001) keeps the shared-runner ratios robust.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from brutex.dashboard.lake_revision import sweeps_revision

from tests.bench._o1_tiers import ratio_ceiling


def _build_sweeps_tree(root: Path, *, n_dirs: int, files_per_dir: int) -> None:
    """Write *n_dirs* ``<instrument>/1minute/`` dirs each holding *files_per_dir* files."""
    for i in range(n_dirs):
        tf_dir = root / "sweeps" / f"INST{i:04d}" / "1minute"
        tf_dir.mkdir(parents=True, exist_ok=True)
        for f in range(files_per_dir):
            (tf_dir / f"result_{f}.parquet").write_bytes(b"x")


def _per_call(root: Path, iterations: int) -> float:
    """MIN over several trial loops of the per-``sweeps_revision`` cost (ns)."""
    best = float("inf")
    for _ in range(5):  # best-of-N-MIN (DEC-CANARY-MINTIME-001)
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            sweeps_revision(root)
        best = min(best, (time.perf_counter_ns() - t0) / iterations)
    return best


class TestGenerationProbeIsNotOFiles:
    """The freshness probe is flat vs the RESULT-FILE count inside each dir."""

    def test_probe_returns_a_token(self, tmp_path: Path) -> None:
        _build_sweeps_tree(tmp_path, n_dirs=8, files_per_dir=3)
        token = sweeps_revision(tmp_path)
        assert token is not None and token != "absent"

    def test_probe_flat_vs_files_per_dir(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        few = tmp_path
        many = tmp_path_factory.mktemp("many_files_per_dir")
        _build_sweeps_tree(few, n_dirs=10, files_per_dir=1)
        _build_sweeps_tree(many, n_dirs=10, files_per_dir=200)  # 200× the files, same dirs
        _per_call(few, 200)  # warm
        t_few = _per_call(few, 1_000)
        t_many = _per_call(many, 1_000)
        ratio = t_many / max(t_few, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"generation-probe cost ratio {ratio:.2f}× (200 vs 1 file/dir, same 10 dirs) "
            f"exceeds the 5× CI ceiling — the probe may have regressed to an O(files) walk. "
            f"few={t_few:.0f}ns many={t_many:.0f}ns."
        )


class TestGenerationProbeIsHonestlyODirs:
    """The probe IS O(dirs), not O(1) — measured, asserted present + at most linear."""

    def test_probe_grows_with_dir_count_but_stays_linear(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        small = tmp_path
        big = tmp_path_factory.mktemp("many_dirs")
        _build_sweeps_tree(small, n_dirs=10, files_per_dir=1)
        _build_sweeps_tree(big, n_dirs=160, files_per_dir=1)  # 16× the dirs
        _per_call(small, 100)  # warm
        t_small = _per_call(small, 500)
        t_big = _per_call(big, 500)
        dir_ratio = t_big / max(t_small, 1.0)
        # (a) HONEST: the probe genuinely GROWS with dir count — it is NOT O(1).
        # 16× the dirs is ~16× the scandir/stat work; even under shared-runner
        # noise this is a large, robust signal, so a > 2× floor never flakes.
        assert dir_ratio > 2.0, (
            f"expected O(dirs) growth for 16× the directories, got only {dir_ratio:.2f}× — "
            f"if this fails the probe became cheaper than O(dirs) (good, but the honest "
            f"complexity note in this bench + lake_revision.py must be revisited). "
            f"small={t_small:.0f}ns big={t_big:.0f}ns."
        )
        # (b) BOUNDED: the growth is at most roughly LINEAR (16× dirs), never
        # quadratic — a generous ×4 tolerance keeps it non-flaky while still
        # catching an O(dirs²) regression (which would be ~256×).
        assert dir_ratio < 16.0 * 4.0, (
            f"generation-probe dir-scaling ratio {dir_ratio:.2f}× (160 vs 10 dirs) exceeds a "
            f"generous linear bound — the probe may have regressed to super-linear-in-dirs. "
            f"small={t_small:.0f}ns big={t_big:.0f}ns."
        )
