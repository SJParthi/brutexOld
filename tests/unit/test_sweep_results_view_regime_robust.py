"""The ``regime_robust`` /results preset (`DEC-REGIME-REJECT-TIER-001` PROPOSAL).

READ-TIME rejection tier, SEMANTIC OPTION A: the preset ranks EXACTLY like the
DEFAULT preset (the min_dd_first law, delegated) but EXCLUDES rows whose joined
cascade verdict is a FAIL at a regime-evidence stage
(``extreme_vol_stratum`` / ``regime_fragility``); honest-abstain statuses are
INCLUDED but counted. The default (no-preset) payload stays byte-identical
(negative-pinned — the #1631 precedent), and a lake with NO cascade report
serves the preset honestly (nothing excluded, counts 0).

The exclusion fixtures drive the REAL chain: ``write_report`` (the production
cascade wire writer) → ``load_cascade_join_file`` → ``build_sweep_leaderboard``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the reader loads per-tuple Parquet stores via ResultsStore")

import brutex.dashboard.sweep_results_view as srv
from brutex.dashboard.sweep_results_view import (
    REGIME_REJECT_STAGES,
    REGIME_ROBUST_PRESET,
    REGIME_UNPROVEN_STATUSES,
    build_sweep_leaderboard,
    load_cascade_join_file,
    resolve_preset_param,
)
from brutex.engine.cascade_wire import CascadeWireReport, write_report
from brutex.metrics.cascade import CandidateVerdict, CascadeReport, FilterStage
from brutex.ranking.presets import DEFAULT_PRESET

from tests.unit.test_sweep_results_view import _row, _run_id, _write_store


@pytest.fixture(autouse=True)
def _reset_join_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-global single-entry cascade-join memo per test."""
    monkeypatch.setattr(srv, "_JOIN_MEMO", None)


def _seed_rows(lake_root: Path, seeds: list[int]) -> None:
    _write_store(
        lake_root,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=s, sharpe=10.0 - s) for s in seeds],
    )


def _regime_report(*, sweep_id: str = "s" * 64) -> CascadeWireReport:
    """Seven verdicts spanning every regime_robust membership class."""
    inner = CascadeReport(
        verdicts=(
            # Kept: a clean regime-proven pass.
            CandidateVerdict(
                candidate_id=_run_id(1),
                passed=True,
                rejected_at=None,
                detail="survived all Stage-4 filters",
                composite_score=2.0,
                extreme_vol_status="ok",
                regime_fragility_status="ok",
            ),
            # EXCLUDED: regime-stage fails (option A's rejection set).
            CandidateVerdict(
                candidate_id=_run_id(2),
                passed=False,
                rejected_at=FilterStage.EXTREME_VOL_STRATUM,
                detail="extreme-vol stratum expectancy < 0",
                extreme_vol_status="fail",
            ),
            CandidateVerdict(
                candidate_id=_run_id(3),
                passed=False,
                rejected_at=FilterStage.REGIME_FRAGILITY,
                detail="regime concentration over threshold",
                regime_fragility_status="fail",
            ),
            # Kept: a fail at a NON-regime stage stays ranked (red-flagged).
            CandidateVerdict(
                candidate_id=_run_id(4),
                passed=False,
                rejected_at=FilterStage.WALK_FORWARD,
                detail="anchored pass-rate 0.5000 < 0.75",
            ),
            # Kept + counted: the three honest-abstain statuses.
            CandidateVerdict(
                candidate_id=_run_id(5),
                passed=True,
                rejected_at=None,
                detail="survived all Stage-4 filters",
                extreme_vol_status="unproven_current_regime",
            ),
            CandidateVerdict(
                candidate_id=_run_id(6),
                passed=True,
                rejected_at=None,
                detail="survived all Stage-4 filters",
                regime_fragility_status="insufficient_strata",
            ),
            CandidateVerdict(
                candidate_id=_run_id(7),
                passed=True,
                rejected_at=None,
                detail="survived all Stage-4 filters",
                extreme_vol_status="insufficient_stratum_trades",
            ),
        ),
        survivor_ids=(_run_id(1), _run_id(5), _run_id(6), _run_id(7)),
        n_input=7,
        n_survived=4,
        stage_survivors={},
    )
    return CascadeWireReport(sweep_id=sweep_id, groups=(("NIFTY", "5m", inner),))


def _seeded_load(lake_root: Path) -> srv.CascadeJoinLoad:
    write_report(lake_root / "sweeps", _regime_report())
    load = load_cascade_join_file(lake_root / "sweeps", token="t1")
    assert load.state == "joined"
    return load


class TestVocabulary:
    def test_regime_robust_token_accepted(self) -> None:
        assert resolve_preset_param("regime_robust") == REGIME_ROBUST_PRESET
        assert resolve_preset_param("  regime_robust  ") == REGIME_ROBUST_PRESET

    def test_unknown_token_refusal_names_regime_robust(self) -> None:
        with pytest.raises(ValueError, match="regime_robust"):
            resolve_preset_param("regime_bogus")

    def test_token_sets_are_the_ruled_vocabulary(self) -> None:
        # The option-A membership law's exact token sets (drift pin).
        assert {"extreme_vol_stratum", "regime_fragility"} == REGIME_REJECT_STAGES
        assert {
            "unproven_current_regime",
            "insufficient_stratum_trades",
            "insufficient_strata",
        } == REGIME_UNPROVEN_STATUSES


class TestDefaultByteIdentity:
    def test_no_preset_payload_gains_no_regime_keys(self, tmp_path: Path) -> None:
        """The #1631-precedent negative pin: default view byte-identical."""
        _seed_rows(tmp_path, seeds=[1, 2, 3, 4, 5, 6, 7])
        cascade = _seeded_load(tmp_path)
        board = build_sweep_leaderboard(tmp_path, cascade=cascade)
        payload = board.to_dict()
        assert "regime_rejected_count" not in payload
        assert "regime_unproven_count" not in payload
        assert "preset" not in payload
        # Every row still present — annotation only, never exclusion.
        assert payload["total_rows"] == 7
        assert len(payload["rows"]) == 7

    def test_other_preset_payload_gains_no_regime_keys(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, seeds=[1, 2, 3])
        cascade = _seeded_load(tmp_path)
        payload = build_sweep_leaderboard(
            tmp_path, preset=DEFAULT_PRESET, cascade=cascade
        ).to_dict()
        assert payload["preset"] == DEFAULT_PRESET
        assert "regime_rejected_count" not in payload
        assert "regime_unproven_count" not in payload


class TestExclusion:
    def test_regime_stage_fails_excluded_other_fail_kept(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, seeds=[1, 2, 3, 4, 5, 6, 7])
        cascade = _seeded_load(tmp_path)
        board = build_sweep_leaderboard(tmp_path, preset=REGIME_ROBUST_PRESET, cascade=cascade)
        served = [r["run_id"] for r in board.rows]
        assert _run_id(2) not in served  # fail @ extreme_vol_stratum
        assert _run_id(3) not in served  # fail @ regime_fragility
        assert _run_id(4) in served  # fail @ walk_forward — kept, red-flagged
        assert board.total_rows == 5
        assert board.regime_rejected_count == 2
        assert board.regime_unproven_count == 3  # runs 5, 6, 7

    def test_counts_emitted_on_payload(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, seeds=[1, 2, 3, 4, 5, 6, 7])
        cascade = _seeded_load(tmp_path)
        payload = build_sweep_leaderboard(
            tmp_path, preset=REGIME_ROBUST_PRESET, cascade=cascade
        ).to_dict()
        assert payload["preset"] == REGIME_ROBUST_PRESET
        assert payload["regime_rejected_count"] == 2
        assert payload["regime_unproven_count"] == 3

    def test_ordering_equals_default_preset_over_kept_rows(self, tmp_path: Path) -> None:
        """The delegation law: membership changes, the ranking rule never does."""
        _seed_rows(tmp_path, seeds=[1, 2, 3, 4, 5, 6, 7])
        cascade = _seeded_load(tmp_path)
        robust = build_sweep_leaderboard(tmp_path, preset=REGIME_ROBUST_PRESET, cascade=cascade)
        default = build_sweep_leaderboard(tmp_path, preset=DEFAULT_PRESET, cascade=cascade)
        excluded = {_run_id(2), _run_id(3)}
        expected = [r["run_id"] for r in default.rows if r["run_id"] not in excluded]
        assert [r["run_id"] for r in robust.rows] == expected


class TestAbsentReportHonesty:
    def test_no_cascade_report_excludes_nothing(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, seeds=[1, 2, 3])
        load = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        assert load.state == "absent"
        board = build_sweep_leaderboard(tmp_path, preset=REGIME_ROBUST_PRESET, cascade=load)
        assert board.total_rows == 3
        assert board.regime_rejected_count == 0
        assert board.regime_unproven_count == 0
        payload = board.to_dict()
        assert payload["regime_rejected_count"] == 0
        assert payload["regime_unproven_count"] == 0

    def test_cascade_none_orders_like_default_preset(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, seeds=[3, 1, 2])
        robust = build_sweep_leaderboard(tmp_path, preset=REGIME_ROBUST_PRESET)
        default = build_sweep_leaderboard(tmp_path, preset=DEFAULT_PRESET)
        assert json.dumps([r["run_id"] for r in robust.rows]) == json.dumps(
            [r["run_id"] for r in default.rows]
        )
        assert robust.regime_rejected_count == 0
        assert robust.regime_unproven_count == 0
