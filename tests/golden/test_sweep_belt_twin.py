"""Twin-run byte/content-identity gate for the sweep spine — arm-parameterized.

Slice-3 scaffolding for the Rust sweep-spine conversion
(DEC-SWEEP-BELT-RUST-001, design-of-record §2.5 gate G2). The harness lives in
:mod:`tests.parity.sweep_twin_harness`; TODAY's shipped arms are IDENTICAL
baselines (OFF vs OFF), which pins run-to-run determinism of the FULL durable
artifact set — store Parquet bytes, trial-stats sidecar, completion sentinel,
audit chain(s) modulo ``ts_utc`` (independently re-verified with the existing
chain verifiers), checkpoint modulo ``updated_at``, the final progress file's
stable view, and quarantine-sidecar absence. The belt PR flips ONE arm to::

    TwinArm(name="belt_on", env={"BRUTEX_SWEEP_RUST_BELT": "1"}, before_run=<latch reset>)

and the same assertions become the belt-ON ≡ belt-OFF parity gate.

Fixture reuse (never re-invented):

* persisted level — the frozen synthetic window + config of
  ``tests/golden/test_engine_sweep_run_golden.py`` (and the twin's store hash
  is cross-checked against that file's pinned ``_GOLDEN_CONTENT_HASH`` — the
  G1 tie-in, with ZERO golden literals edited here);
* grid level — the 4-tuple grid + trending bars of
  ``tests/unit/test_engine_sweep_parallel.py``.

Deliberately NOT duplicated here (design §2.5): the sequential ≡ parallel
byte-identity pin (``TestSequentialEqualsParallelGolden`` in the sweep_parallel
suite — G7 re-runs it with the flag exported into spawn workers) and the
checkpoint/resume + chaos suites (G3 keeps them UNMODIFIED as the regression
assertion — those modules carry zero diff in the belt slice).
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.engine.sweep_orchestrator import count_sweep_tuples
from brutex.engine.trial_stats import TRIAL_STATS_SUFFIX
from brutex.engine.tuple_complete import COMPLETION_SUFFIX

from tests.golden.test_engine_sweep_run_golden import _GOLDEN_CONTENT_HASH, _bars, _config
from tests.parity.sweep_twin_harness import (
    GridArmArtifacts,
    PersistedArmArtifacts,
    grid_twin_divergences,
    identical_baseline_arms,
    persisted_twin_divergences,
    run_grid_arm,
    run_persisted_arm,
)
from tests.parity.trade_divergence import render_divergence_report
from tests.unit.test_engine_sweep_parallel import _bars_for, _grid


class TestPersistedTwinIdenticalArms:
    """The persisted-sweep twin over the frozen sweep-run golden fixture."""

    def _artifacts(self, tmp_path: Path) -> tuple[PersistedArmArtifacts, PersistedArmArtifacts]:
        bars = _bars()
        cfg = _config()
        arm_a, arm_b = identical_baseline_arms()
        return (
            run_persisted_arm(tmp_path / "a", arm_a, bars=bars, cfg=cfg),
            run_persisted_arm(tmp_path / "b", arm_b, bars=bars, cfg=cfg),
        )

    def test_identical_arms_have_zero_divergences(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")  # flush needs the [data] extra
        a, b = self._artifacts(tmp_path)
        divergences = persisted_twin_divergences(a, b)
        assert divergences == [], render_divergence_report(divergences)

    def test_sidecar_and_sentinel_are_genuinely_compared(self, tmp_path: Path) -> None:
        """The digest path writes .trials.json + .complete.json into the tree.

        Guards the harness against silently degenerating to a store-only
        compare (the frozen sweep-run golden never exercises the sidecar or
        the sentinel — this twin does, by construction).
        """
        pytest.importorskip("polars")
        a, _b = self._artifacts(tmp_path)
        suffixes = {Path(rel).name for rel in a.tree}
        assert f"tuple.parquet{TRIAL_STATS_SUFFIX}" in suffixes
        assert f"tuple.parquet{COMPLETION_SUFFIX}" in suffixes
        assert "tuple.parquet" in suffixes

    def test_twin_reproduces_the_frozen_golden_hash_and_chains_verify(self, tmp_path: Path) -> None:
        """G1 tie-in: both arms land on the UNMODIFIED pinned store hash, and
        both audit chains verify (Golden Rule #8 — the P-20 verifier)."""
        pytest.importorskip("polars")
        a, b = self._artifacts(tmp_path)
        assert a.summary["store_content_hash"] == _GOLDEN_CONTENT_HASH
        assert b.summary["store_content_hash"] == _GOLDEN_CONTENT_HASH
        assert a.audit_verified_entries >= 1
        assert b.audit_verified_entries >= 1


class TestGridTwinIdenticalArms:
    """The supervised-grid twin over the sweep_parallel 4-tuple grid fixture."""

    def _artifacts(self, tmp_path: Path) -> tuple[GridArmArtifacts, GridArmArtifacts]:
        grid = _grid()
        arm_a, arm_b = identical_baseline_arms()
        return (
            run_grid_arm(tmp_path / "a", arm_a, grid=grid, bars_for=_bars_for),
            run_grid_arm(tmp_path / "b", arm_b, grid=grid, bars_for=_bars_for),
        )

    def test_identical_arms_have_zero_divergences(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        a, b = self._artifacts(tmp_path)
        divergences = grid_twin_divergences(a, b)
        assert divergences == [], render_divergence_report(divergences)

    def test_artifact_set_is_complete_and_clean(self, tmp_path: Path) -> None:
        """Non-vacuity: files exist, both chains verify, the checkpoint reached
        the grid's end, and NO quarantine sidecar was produced (#1608)."""
        pytest.importorskip("polars")
        a, _b = self._artifacts(tmp_path)
        assert len(a.tree) > 0  # the grid actually produced per-tuple artifacts
        assert a.ingest_verified_entries >= 1
        assert a.supervisor_verified_entries >= 1
        assert a.checkpoint_next_index == count_sweep_tuples(_grid())
        assert a.checkpoint["last_completed_index"] == count_sweep_tuples(_grid()) - 1
        assert a.progress_stable["state"] == "done"
        assert a.quarantine_sidecar_exists is False
