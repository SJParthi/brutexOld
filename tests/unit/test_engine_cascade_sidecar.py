"""Unit tests for :mod:`brutex.engine.cascade_sidecar` — the cascade-verdict sidecar.

Targets the strict 100% line + branch gate (CLAUDE.md §4 / Golden Rule #12):

* :func:`write_cascade_sidecar` — happy path + the byte-identical re-write
  (Golden Rule #4 / #7) + the content-hash stability.
* :func:`cascade_verdict_table` — survivor + reject rendering, the ``top`` cap,
  and the ``None`` composite-score branch.
* The mixed survivor/reject report round-trips to the pinned Parquet schema.

These import the cascade engine to build a real :class:`CascadeReport` (no
fabricated verdicts — Golden Rule #1). Engine imports pull the ``fno_math`` /
``trading_core`` wheels, so this is a CI-3.12 test (the sandbox cannot import the
chain); CI is the oracle.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import numpy.typing as npt
from brutex.engine.cascade_sidecar import (
    CASCADE_SIDECAR_NAME,
    CascadeSidecarFlush,
    cascade_verdict_table,
    write_cascade_sidecar,
)
from brutex.metrics.cascade import (
    CandidateInput,
    CascadeConfig,
    CascadeReport,
    run_stage4_cascade,
)


def _null_exit(positions: npt.NDArray[np.intp]) -> float:
    """A null exit-logic: random entries never beat a positive observed Sharpe."""
    return 0.0


def _strong_returns(seed: int, n: int = 32, edge: float = 1.0) -> tuple[float, ...]:
    return tuple((np.random.default_rng(seed).standard_normal(n) + edge).tolist())


def _candidate(cid: str, *, seed: int = 0, **overrides: object) -> CandidateInput:
    rng = np.random.default_rng(seed + 100)
    base: dict[str, object] = {
        "candidate_id": cid,
        "sharpe_ratio": 2.0,
        "sortino_ratio": 2.4,
        "calmar_ratio": 1.6,
        "profit_factor": 1.8,
        "win_rate": 0.55,
        "win_loss_ratio": 2.5,
        "recovery_factor": 2.1,
        "max_drawdown_pct": 12.0,
        "max_consecutive_losses": 4,
        "total_trades": 600,
        "training_sharpe": 0.1,
        "training_max_drawdown": 5.0,
        "skew": 0.0,
        "kurtosis": 3.0,
        "returns": _strong_returns(seed),
        "lockbox_returns": tuple((rng.standard_normal(40) * 0.05 + 0.2).tolist()),
        "lockbox_days": tuple(range(40)),
        "mc_n_bars": 400,
        "mc_n_entries": 20,
        "mc_evaluate": _null_exit,
    }
    base.update(overrides)
    return CandidateInput(**base)  # type: ignore[arg-type]


def _pass_config(**overrides: object) -> CascadeConfig:
    base: dict[str, object] = {
        "seed": 7,
        "min_total_trades": 30,
        "dsr_min": 0.0,
        "cpcv_min_pass_rate": 0.0,
        "spa_alpha": 1.0,
        "rw_alpha": 1.0,
        "pbo_max": 1.0,
        "wfo_min_pass_rate": 0.0,
        "wfo_train_size": 8,
        "wfo_test_size": 4,
        "wfo_step": 4,
        "lockbox_min_trades": 5,
        "lockbox_default_window_days": 60,
        "mc_alpha": 1.0,
        "mc_n_permutations": 4,
        "n_resamples": 16,
        "pbo_n_splits": 4,
    }
    base.update(overrides)
    return CascadeConfig(**base)  # type: ignore[arg-type]


def _mixed_report() -> CascadeReport:
    """A report with both a survivor and a hard-gate reject (both branches)."""
    survivor = _candidate("a" * 64, seed=1)
    rejected = _candidate("b" * 64, seed=2, total_trades=5)  # fails min_total_trades
    return run_stage4_cascade([survivor, rejected], _pass_config())


class TestWriteCascadeSidecar:
    def test_writes_one_row_per_verdict(self, tmp_path: Path) -> None:
        report = _mixed_report()
        flush = write_cascade_sidecar(report, tmp_path / CASCADE_SIDECAR_NAME)
        assert isinstance(flush, CascadeSidecarFlush)
        assert flush.row_count == report.n_input == 2
        assert flush.path.exists()
        assert len(flush.content_hash) == 64
        assert flush.bytes_written > 0

    def test_rewrite_is_byte_identical(self, tmp_path: Path) -> None:
        report = _mixed_report()
        first = write_cascade_sidecar(report, tmp_path / "v.parquet")
        second = write_cascade_sidecar(report, tmp_path / "v.parquet")
        assert first.content_hash == second.content_hash
        assert first.bytes_written == second.bytes_written

    def test_roundtrip_columns(self, tmp_path: Path) -> None:
        import polars as pl

        report = _mixed_report()
        path = tmp_path / "v.parquet"
        write_cascade_sidecar(report, path)
        df = pl.read_parquet(path)
        assert set(df.columns) == {
            "candidate_id",
            "passed",
            "rejected_at",
            "detail",
            "composite_score",
        }
        # The rejected row carries a non-empty stage; the survivor carries "".
        stages = set(df["rejected_at"].to_list())
        assert "" in stages  # the survivor
        assert "hard_gates" in stages  # the reject


class TestCascadeVerdictTable:
    def test_renders_survivor_and_reject(self) -> None:
        table = cascade_verdict_table(_mixed_report())
        assert "Stage-4 cascade:" in table
        assert "PASS" in table
        assert "REJECT[hard_gates]" in table

    def test_top_cap_limits_rows(self) -> None:
        report = _mixed_report()
        full = cascade_verdict_table(report)
        capped = cascade_verdict_table(report, top=1)
        # The header is 2 lines; full has both verdicts, capped has one.
        assert len(full.splitlines()) == 4
        assert len(capped.splitlines()) == 3

    def test_survivor_with_none_score_renders_dash(self) -> None:
        # An all-reject report has no survivors; force the None-score PASS branch
        # by hand-building a report whose survivor has composite_score=None.
        from brutex.metrics.cascade import CandidateVerdict, FilterStage

        passed = CandidateVerdict(
            candidate_id="c" * 64,
            passed=True,
            rejected_at=None,
            detail="survived",
            composite_score=None,
        )
        rejected = CandidateVerdict(
            candidate_id="d" * 64,
            passed=False,
            rejected_at=FilterStage.CPCV,
            detail="cpcv low",
        )
        # A defensive reject with no stage exercises the "-" fallback branch.
        rejected_no_stage = CandidateVerdict(
            candidate_id="e" * 64,
            passed=False,
            rejected_at=None,
            detail="no stage",
        )
        report = CascadeReport(
            verdicts=(passed, rejected, rejected_no_stage),
            survivor_ids=("c" * 64,),
            n_input=3,
            n_survived=1,
            stage_survivors={},
        )
        table = cascade_verdict_table(report)
        assert "score=-" in table
        assert "REJECT[cpcv]" in table
        assert "REJECT[-]" in table


def test_module_all_is_complete() -> None:
    import brutex.engine.cascade_sidecar as mod

    for name in mod.__all__:
        assert hasattr(mod, name)
