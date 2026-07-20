"""Unit suite for :mod:`brutex.engine.cascade_join` (DEC-RESULTS-CASCADE-VERDICT-JOIN-001).

Covers every arm of the ONE flattener/validator law: structural fail-closed
(root / version pun / future version / sweep_id / groups), the per-item
skip+count matrix (groups + verdicts), nulled-value counting, the
duplicate-candidate exclusion (intra-group AND cross-group — EVERY copy),
the survivor-rank law + counted inconsistencies, unknown-future-stage-token
passthrough, the root-count fallback, the single capped WARN, and the
round-trip equivalence over the REAL wire serializer + the frozen PRE-change
report fixture (the reader None-fill oracle).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from brutex.engine.cascade_join import (
    _WARN_DETAIL_CAP,
    CascadeJoin,
    CascadeJoinError,
    build_cascade_join,
    cascade_report_path,
)
from brutex.engine.cascade_wire import (
    REPORT_VERSION,
    CascadeWireReport,
    report_path,
    serialize_wire_report,
    wire_report_to_dict,
)
from brutex.metrics.cascade import CandidateVerdict, CascadeReport, FilterStage
from structlog.testing import capture_logs

_FIXTURES = Path(__file__).resolve().parents[1] / "golden" / "fixtures"
_PRE_CHANGE_REPORT = _FIXTURES / "cascade_report_pre_cpcv_rate_v1.json"

_CID_A = f"{1:064x}"
_CID_B = f"{2:064x}"
_CID_C = f"{3:064x}"


def _verdict(cid: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "candidate_id": cid,
        "passed": True,
        "rejected_at": None,
        "detail": "survived all Stage-4 filters",
        "composite_score": 1.5,
        "provisional": False,
        "cpcv_pass_rate": 0.75,
    }
    base.update(overrides)
    return base


def _group(
    rows: list[Any],
    *,
    survivor_ids: list[Any] | None = None,
    instrument: str = "NIFTY",
    tf: str = "1minute",
    **report_overrides: Any,
) -> dict[str, Any]:
    passed = [
        v["candidate_id"]
        for v in rows
        if isinstance(v, dict) and v.get("passed") is True and "candidate_id" in v
    ]
    report: dict[str, Any] = {
        "n_input": len(rows),
        "n_survived": len(passed),
        "dsr_n_source": "pool",
        "dsr_n_trials": len(rows),
        "dsr_trial_variance": 0.01,
        "pool_pbo": 0.25,
        "survivor_ids": passed if survivor_ids is None else survivor_ids,
        "stage_survivors": {},
        "verdicts": rows,
    }
    report.update(report_overrides)
    return {"instrument": instrument, "tf": tf, "report": report}


def _payload(groups: list[Any], **overrides: Any) -> dict[str, Any]:
    def _count(key: str) -> int:
        total = 0
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("report"), dict):
                value = g["report"].get(key)
                if type(value) is int:
                    total += value
        return total

    base: dict[str, Any] = {
        "version": REPORT_VERSION,
        "sweep_id": "feedface" * 8,
        "n_input": _count("n_input"),
        "n_survived": _count("n_survived"),
        "groups": groups,
    }
    base.update(overrides)
    return base


class TestCascadeReportPath:
    def test_delegates_to_the_one_wire_path_law(self, tmp_path: Path) -> None:
        assert cascade_report_path(tmp_path) == report_path(tmp_path)
        assert cascade_report_path(tmp_path) == tmp_path / "cascade" / "cascade-report.json"


class TestStructuralFailClosed:
    def test_non_mapping_root(self) -> None:
        for junk in ([], "x", 7, None):
            with pytest.raises(CascadeJoinError, match="not a JSON object"):
                build_cascade_join(junk)

    @pytest.mark.parametrize("version", [True, False, 1.0, "1", None])
    def test_version_pun_rejected(self, version: object) -> None:
        with pytest.raises(CascadeJoinError, match="genuine integer"):
            build_cascade_join(_payload([_group([_verdict(_CID_A)])], version=version))

    def test_future_version_refused(self) -> None:
        with pytest.raises(CascadeJoinError, match="NEWER than the supported"):
            build_cascade_join(_payload([_group([_verdict(_CID_A)])], version=REPORT_VERSION + 1))

    @pytest.mark.parametrize("sweep_id", [None, "", 7])
    def test_bad_sweep_id(self, sweep_id: object) -> None:
        payload = _payload([_group([_verdict(_CID_A)])])
        if sweep_id is None:
            del payload["sweep_id"]
        else:
            payload["sweep_id"] = sweep_id
        with pytest.raises(CascadeJoinError, match="sweep_id"):
            build_cascade_join(payload)

    @pytest.mark.parametrize("junk_groups", [None, {}, "x"])
    def test_groups_not_a_list(self, junk_groups: object) -> None:
        payload = _payload([])
        payload["groups"] = junk_groups
        with pytest.raises(CascadeJoinError, match="groups is not a list"):
            build_cascade_join(payload)


class TestCleanJoin:
    def test_pass_provisional_fail_projected(self) -> None:
        verdicts = [
            _verdict(_CID_A, composite_score=2.0, cpcv_pass_rate=0.9),
            _verdict(_CID_B, provisional=True, composite_score=1.0, cpcv_pass_rate=0.6),
            _verdict(
                _CID_C,
                passed=False,
                rejected_at="cpcv",
                detail="CPCV pass-rate 0.4100 < 0.5",
                composite_score=None,
                cpcv_pass_rate=0.41,
            ),
        ]
        with capture_logs() as logs:
            join = build_cascade_join(_payload([_group(verdicts)]))
        # A clean report fires NO partial WARN.
        assert not [e for e in logs if e["event"] == "cascade_join.partial"]
        assert join.sweep_id == "feedface" * 8
        assert join.version == REPORT_VERSION
        assert join.n_input == 3
        assert join.n_survived == 2
        assert join.skipped_groups == 0
        assert join.skipped_verdicts == 0
        assert join.nulled_values == 0
        assert join.rank_inconsistencies == 0
        assert join.ambiguous_ids == frozenset()
        a = join.by_candidate[_CID_A]
        assert a.passed and not a.provisional
        assert a.cascade_composite == 2.0
        assert a.cpcv_pass_rate == 0.9
        assert a.survivor_rank == 1
        assert a.instrument == "NIFTY" and a.tf == "1minute"
        b = join.by_candidate[_CID_B]
        assert b.passed and b.provisional and b.survivor_rank == 2
        c = join.by_candidate[_CID_C]
        assert not c.passed
        assert c.rejected_at == "cpcv"
        assert c.detail == "CPCV pass-rate 0.4100 < 0.5"
        assert c.cascade_composite is None
        assert c.cpcv_pass_rate == 0.41
        assert c.survivor_rank is None

    def test_group_stats_projected(self) -> None:
        join = build_cascade_join(_payload([_group([_verdict(_CID_A)])]))
        (stats,) = join.groups
        assert stats.instrument == "NIFTY"
        assert stats.tf == "1minute"
        assert stats.n_input == 1
        assert stats.n_survived == 1
        assert stats.pool_pbo == 0.25
        assert stats.dsr_n_source == "pool"
        assert stats.dsr_n_trials == 1
        assert stats.dsr_trial_variance == 0.01

    def test_unknown_future_stage_token_kept_verbatim(self) -> None:
        join = build_cascade_join(
            _payload(
                [
                    _group(
                        [
                            _verdict(
                                _CID_A,
                                passed=False,
                                rejected_at="quantum_bootstrap",
                                composite_score=None,
                            )
                        ]
                    )
                ]
            )
        )
        assert join.by_candidate[_CID_A].rejected_at == "quantum_bootstrap"

    def test_root_counts_fall_back_to_group_sums(self) -> None:
        payload = _payload([_group([_verdict(_CID_A)])])
        payload["n_input"] = "junk"
        del payload["n_survived"]
        join = build_cascade_join(payload)
        assert join.n_input == 1
        assert join.n_survived == 1


class TestGroupTolerance:
    @pytest.mark.parametrize(
        "group",
        [
            "not-a-dict",
            {"instrument": "", "tf": "1minute", "report": {}},
            {"instrument": "NIFTY", "tf": 5, "report": {}},
            {"instrument": "NIFTY", "tf": "1minute", "report": []},
        ],
    )
    def test_malformed_group_skipped_counted(self, group: object) -> None:
        good = _group([_verdict(_CID_A)])
        with capture_logs() as logs:
            join = build_cascade_join(_payload([group, good]))  # type: ignore[list-item]
        assert join.skipped_groups == 1
        assert _CID_A in join.by_candidate
        partial = [e for e in logs if e["event"] == "cascade_join.partial"]
        assert len(partial) == 1

    @pytest.mark.parametrize(
        "overrides",
        [
            {"verdicts": "junk"},
            {"n_input": True},
            {"n_input": -1},
            {"n_survived": 1.0},
        ],
    )
    def test_junk_report_shape_skips_group(self, overrides: dict[str, Any]) -> None:
        bad = _group([_verdict(_CID_A)], **overrides)
        join = build_cascade_join(_payload([bad, _group([_verdict(_CID_B)], tf="5minute")]))
        assert join.skipped_groups == 1
        assert _CID_A not in join.by_candidate
        assert _CID_B in join.by_candidate

    def test_junk_group_numerics_nulled_not_skipped(self) -> None:
        group = _group(
            [_verdict(_CID_A)],
            pool_pbo="junk",
            dsr_n_source=7,
            dsr_n_trials=True,
            dsr_trial_variance=float("nan"),
        )
        join = build_cascade_join(_payload([group]))
        (stats,) = join.groups
        assert stats.pool_pbo is None
        assert stats.dsr_n_source == ""
        assert stats.dsr_n_trials is None
        assert stats.dsr_trial_variance is None


class TestVerdictTolerance:
    def _one(self, verdicts: list[Any], **group_kwargs: Any) -> CascadeJoin:
        return build_cascade_join(_payload([_group(verdicts, **group_kwargs)]))

    @pytest.mark.parametrize(
        "entry",
        [
            "not-a-dict",
            {"passed": True},
            {"candidate_id": "", "passed": True},
            {"candidate_id": 7, "passed": True},
            {"candidate_id": "x" * 64, "passed": "yes"},
            {"candidate_id": "x" * 64, "passed": 1},
        ],
    )
    def test_malformed_verdict_skipped(self, entry: Any) -> None:
        join = self._one([entry, _verdict(_CID_A)], survivor_ids=[_CID_A])
        assert join.skipped_verdicts == 1
        assert _CID_A in join.by_candidate

    def test_passed_with_rejected_at_is_contradictory(self) -> None:
        join = self._one([_verdict(_CID_A, rejected_at="cpcv")], survivor_ids=[])
        assert join.skipped_verdicts == 1
        assert _CID_A not in join.by_candidate

    def test_fail_with_non_string_stage_nulled_and_counted(self) -> None:
        join = self._one(
            [_verdict(_CID_A, passed=False, rejected_at=7, composite_score=None)],
            survivor_ids=[],
        )
        view = join.by_candidate[_CID_A]
        assert view.rejected_at is None
        assert join.nulled_values == 1

    def test_optional_keys_presence_tolerant(self) -> None:
        join = self._one(
            [{"candidate_id": _CID_A, "passed": False}],
            survivor_ids=[],
            n_survived=0,
        )
        view = join.by_candidate[_CID_A]
        assert view.rejected_at is None
        assert view.detail == ""
        assert view.cascade_composite is None
        assert not view.provisional
        assert view.cpcv_pass_rate is None
        assert join.nulled_values == 0

    def test_non_string_detail_reads_empty(self) -> None:
        join = self._one([_verdict(_CID_A, detail=42)], survivor_ids=[_CID_A])
        assert join.by_candidate[_CID_A].detail == ""

    @pytest.mark.parametrize("composite", [float("nan"), float("inf"), "junk", True])
    def test_non_finite_composite_nulled_counted_verdict_kept(self, composite: Any) -> None:
        join = self._one([_verdict(_CID_A, composite_score=composite)], survivor_ids=[_CID_A])
        view = join.by_candidate[_CID_A]
        assert view.cascade_composite is None
        assert join.nulled_values == 1

    @pytest.mark.parametrize("rate", [1.5, -0.1, float("nan"), "junk", False])
    def test_bad_rate_nulled_counted_never_clamped(self, rate: Any) -> None:
        join = self._one([_verdict(_CID_A, cpcv_pass_rate=rate)], survivor_ids=[_CID_A])
        view = join.by_candidate[_CID_A]
        assert view.cpcv_pass_rate is None
        assert join.nulled_values == 1

    def test_boundary_rates_kept_exact(self) -> None:
        join = build_cascade_join(
            _payload(
                [
                    _group([_verdict(_CID_A, cpcv_pass_rate=0.0)]),
                    _group([_verdict(_CID_B, cpcv_pass_rate=1)], tf="5minute"),
                ]
            )
        )
        assert join.by_candidate[_CID_A].cpcv_pass_rate == 0.0
        # An int rate is accepted as its exact float.
        assert join.by_candidate[_CID_B].cpcv_pass_rate == 1.0
        assert join.nulled_values == 0

    def test_provisional_only_on_literal_true(self) -> None:
        join = build_cascade_join(
            _payload(
                [
                    _group([_verdict(_CID_A, provisional="yes")]),
                    _group([_verdict(_CID_B, provisional=True)], tf="5minute"),
                ]
            )
        )
        assert not join.by_candidate[_CID_A].provisional
        assert join.by_candidate[_CID_B].provisional


class TestSurvivorRankLaw:
    def test_survivor_id_without_passed_verdict_counted(self) -> None:
        fail = _verdict(_CID_A, passed=False, rejected_at="pbo", composite_score=None)
        join = build_cascade_join(
            _payload([_group([fail], survivor_ids=[_CID_A, "ghost", 7], n_survived=0)])
        )
        # Three inconsistencies: a FAILED survivor id, an unknown id, a
        # non-string id — each counted, none crashes, no rank assigned.
        assert join.rank_inconsistencies == 3
        assert join.by_candidate[_CID_A].survivor_rank is None

    def test_passed_verdict_absent_from_survivor_ids_counted(self) -> None:
        join = build_cascade_join(_payload([_group([_verdict(_CID_A)], survivor_ids=[])]))
        assert join.rank_inconsistencies == 1
        assert join.by_candidate[_CID_A].survivor_rank is None

    def test_non_list_survivor_ids_tolerated(self) -> None:
        group = _group([_verdict(_CID_A)])
        group["report"]["survivor_ids"] = None
        join = build_cascade_join(_payload([group]))
        # survivor_ids: null → treated empty → the passer counts one
        # inconsistency, rank None — never a crash.
        assert join.rank_inconsistencies == 1
        assert join.by_candidate[_CID_A].survivor_rank is None


class TestDuplicateExclusion:
    def test_cross_group_duplicate_excludes_both(self) -> None:
        g1 = _group([_verdict(_CID_A), _verdict(_CID_B)])
        g2 = _group([_verdict(_CID_A)], tf="5minute")
        join = build_cascade_join(_payload([g1, g2]))
        assert _CID_A not in join.by_candidate
        assert join.ambiguous_ids == frozenset({_CID_A})
        assert _CID_B in join.by_candidate

    def test_intra_group_duplicate_excluded(self) -> None:
        g = _group([_verdict(_CID_A), _verdict(_CID_A)], survivor_ids=[_CID_A])
        join = build_cascade_join(_payload([g]))
        assert _CID_A not in join.by_candidate
        assert join.ambiguous_ids == frozenset({_CID_A})

    def test_later_intra_dup_sweeps_earlier_flat_copy(self) -> None:
        # Group 1 carries X once (lands in the flat index); group 2 carries X
        # TWICE (intra-dup → ambiguous) — the final sweep must remove group
        # 1's copy too: EVERY copy of ambiguous evidence is excluded.
        g1 = _group([_verdict(_CID_A)])
        g2 = _group([_verdict(_CID_A), _verdict(_CID_A)], tf="5minute")
        join = build_cascade_join(_payload([g1, g2]))
        assert _CID_A not in join.by_candidate
        assert join.ambiguous_ids == frozenset({_CID_A})

    def test_third_copy_after_ambiguous_skipped(self) -> None:
        g1 = _group([_verdict(_CID_A)])
        g2 = _group([_verdict(_CID_A)], tf="5minute")
        g3 = _group([_verdict(_CID_A)], tf="15minute")
        join = build_cascade_join(_payload([g1, g2, g3]))
        assert _CID_A not in join.by_candidate
        assert join.ambiguous_ids == frozenset({_CID_A})


class TestWarnCap:
    def test_detail_samples_capped_counts_exact(self) -> None:
        junk = ["junk"] * (_WARN_DETAIL_CAP + 5)
        with capture_logs() as logs:
            join = build_cascade_join(_payload([_group([*junk, _verdict(_CID_A)])]))
        assert join.skipped_verdicts == _WARN_DETAIL_CAP + 5
        (partial,) = [e for e in logs if e["event"] == "cascade_join.partial"]
        assert partial["skipped_verdicts"] == _WARN_DETAIL_CAP + 5
        assert len(partial["details"]) == _WARN_DETAIL_CAP


class TestRoundTrip:
    def _wire_report(self) -> CascadeWireReport:
        report = CascadeReport(
            verdicts=(
                CandidateVerdict(
                    candidate_id=_CID_A,
                    passed=True,
                    rejected_at=None,
                    detail="survived all Stage-4 filters",
                    composite_score=1.25,
                    provisional=True,
                    cpcv_pass_rate=0.875,
                ),
                CandidateVerdict(
                    candidate_id=_CID_B,
                    passed=False,
                    rejected_at=FilterStage.WALK_FORWARD,
                    detail="anchored pass-rate 0.5000 < 0.75",
                    cpcv_pass_rate=0.625,
                ),
            ),
            survivor_ids=(_CID_A,),
            n_input=2,
            n_survived=1,
            stage_survivors={"hard_gates": 2},
            pool_pbo=None,
            dsr_n_source="pool",
            dsr_n_trials=2,
            dsr_trial_variance=0.0,
        )
        return CascadeWireReport(sweep_id="ab" * 32, groups=(("NIFTY", "1minute", report),))

    def test_wire_dict_and_serialized_bytes_flatten_identically(self) -> None:
        wire = self._wire_report()
        from_dict = build_cascade_join(wire_report_to_dict(wire))
        from_bytes = build_cascade_join(json.loads(serialize_wire_report(wire)))
        assert from_dict == from_bytes
        assert from_dict.by_candidate[_CID_A].cpcv_pass_rate == 0.875
        assert from_dict.by_candidate[_CID_A].provisional
        assert from_dict.by_candidate[_CID_A].survivor_rank == 1
        assert from_dict.by_candidate[_CID_B].rejected_at == "walk_forward"
        assert from_dict.by_candidate[_CID_B].cpcv_pass_rate == 0.625

    def test_frozen_pre_change_report_none_fills(self) -> None:
        """The reader None-fill oracle — the PRE-change bytes lack the key."""
        raw = _PRE_CHANGE_REPORT.read_bytes()
        assert b"cpcv_pass_rate" not in raw
        join = build_cascade_join(json.loads(raw))
        assert len(join.by_candidate) == 4
        assert all(v.cpcv_pass_rate is None for v in join.by_candidate.values())
        assert join.skipped_groups == 0
        assert join.skipped_verdicts == 0
        assert join.nulled_values == 0
        assert join.n_input == 4
        assert join.n_survived == 2


class TestNewStatKeysTolerated:
    """DEC-CASCADE-STAT-TIER-WIRE-001: the additive-at-REPORT_VERSION-1 report
    keys (``pool_rc_pvalue`` / ``pool_spa_pvalue`` per group,
    ``candidate_pbo`` per verdict) are UNKNOWN to the joiner — the additive
    convention demands it processes such a report with zero skips/nulls."""

    def test_report_with_the_new_keys_joins_cleanly(self) -> None:
        row = _verdict(_CID_A, candidate_pbo=0.125)
        payload = _payload(
            [
                _group(
                    [row],
                    pool_rc_pvalue=0.01,
                    pool_spa_pvalue=0.02,
                )
            ]
        )
        join = build_cascade_join(payload)
        assert join.skipped_groups == 0
        assert join.skipped_verdicts == 0
        assert join.nulled_values == 0
        assert join.by_candidate[_CID_A].passed is True
        assert join.by_candidate[_CID_A].cpcv_pass_rate == 0.75

    def test_live_writer_report_with_new_keys_round_trips(self) -> None:
        # Drive the joiner over the REAL writer's serialisation of a report
        # carrying measured values in every new field.
        from brutex.metrics.cascade import CandidateVerdict, CascadeReport

        report = CascadeReport(
            verdicts=(
                CandidateVerdict(
                    candidate_id=_CID_A,
                    passed=True,
                    rejected_at=None,
                    detail="survived all Stage-4 filters",
                    composite_score=0.5,
                    cpcv_pass_rate=0.875,
                    candidate_pbo=0.0625,
                ),
            ),
            survivor_ids=(_CID_A,),
            n_input=1,
            n_survived=1,
            stage_survivors={"hard_gates": 1},
            pool_pbo=0.25,
            pool_rc_pvalue=0.005,
            pool_spa_pvalue=0.015,
        )
        wire = CascadeWireReport(sweep_id="ab" * 32, groups=(("NIFTY", "1minute", report),))
        join = build_cascade_join(json.loads(serialize_wire_report(wire)))
        assert join.skipped_groups == 0
        assert join.skipped_verdicts == 0
        assert join.nulled_values == 0
        assert join.by_candidate[_CID_A].survivor_rank == 1
