"""The /results cascade-verdict + composite join (DEC-RESULTS-CASCADE-VERDICT-JOIN-001).

Covers :func:`load_cascade_join_file`'s full degrade matrix (absent /
unreadable / malformed / oversized / joined) + the single-entry memo law
(ONE parse + ONE WARN per stat token), the EMIT-ON-PRESENT row/meta
projection in :func:`build_sweep_leaderboard` (byte-identity on the
no-report lake pinned via ``json.dumps`` equality), the duplicate/unmatched
row laws, ``cascade_flagged``, and the composite-index preset activation
(the honest dynamic inert-axes disclosure).
"""

# The memo-reset fixture + drift pins read module privates deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars", reason="the reader loads per-tuple Parquet stores via ResultsStore")

from brutex.dashboard import sweep_results_view as srv
from brutex.dashboard.composite_join import CompositeIndex
from brutex.dashboard.sweep_results_view import (
    CASCADE_REPORT_MAX_BYTES,
    CascadeJoinLoad,
    build_sweep_leaderboard,
    load_cascade_join_file,
)
from brutex.engine.cascade_wire import CascadeWireReport, write_report
from brutex.metrics.cascade import CandidateVerdict, CascadeReport, FilterStage
from structlog.testing import capture_logs

from tests.unit.test_sweep_results_view import _row, _run_id, _write_store


@pytest.fixture(autouse=True)
def _reset_join_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-global single-entry memo per test."""
    monkeypatch.setattr(srv, "_JOIN_MEMO", None)


def _wire_report(*, sweep_id: str = "s" * 64) -> CascadeWireReport:
    inner = CascadeReport(
        verdicts=(
            CandidateVerdict(
                candidate_id=_run_id(1),
                passed=True,
                rejected_at=None,
                detail="survived all Stage-4 filters",
                composite_score=2.0,
                provisional=False,
                cpcv_pass_rate=0.9,
            ),
            CandidateVerdict(
                candidate_id=_run_id(2),
                passed=True,
                rejected_at=None,
                detail="survived all Stage-4 filters",
                composite_score=1.0,
                provisional=True,
                cpcv_pass_rate=0.6,
            ),
            CandidateVerdict(
                candidate_id=_run_id(3),
                passed=False,
                rejected_at=FilterStage.WALK_FORWARD,
                detail="anchored pass-rate 0.5000 < 0.75",
                cpcv_pass_rate=0.55,
            ),
        ),
        survivor_ids=(_run_id(1), _run_id(2)),
        n_input=3,
        n_survived=2,
        stage_survivors={},
    )
    return CascadeWireReport(sweep_id=sweep_id, groups=(("NIFTY", "5m", inner),))


def _seed_report(lake_root: Path, report: CascadeWireReport | None = None) -> Path:
    return write_report(lake_root / "sweeps", report if report is not None else _wire_report())


def _seed_rows(lake_root: Path, seeds: list[int]) -> None:
    _write_store(
        lake_root,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=s, sharpe=10.0 - s) for s in seeds],
    )


class TestLoadCascadeJoinFile:
    def test_absent_no_log(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            load = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        assert load.state == "absent"
        assert load.join is None
        assert load.token == "t1"
        assert not [e for e in logs if e["event"] == "sweep_results.cascade_report_load_failed"]

    def test_joined_over_the_real_writer(self, tmp_path: Path) -> None:
        _seed_report(tmp_path)
        load = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        assert load.state == "joined"
        assert load.join is not None
        assert load.join.sweep_id == "s" * 64
        assert load.join.by_candidate[_run_id(1)].cpcv_pass_rate == 0.9

    def test_unreadable(self, tmp_path: Path) -> None:
        # A DIRECTORY at the report path stats fine but cannot be read.
        (tmp_path / "sweeps" / "cascade" / "cascade-report.json").mkdir(parents=True)
        with capture_logs() as logs:
            load = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        assert load.state == "unreadable"
        assert load.join is None
        (warn,) = [e for e in logs if e["event"] == "sweep_results.cascade_report_load_failed"]
        assert warn["state"] == "unreadable"

    def test_stat_oserror_is_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _seed_report(tmp_path)

        real_stat = Path.stat

        def _boom(self: Path, **kwargs: Any) -> Any:
            if self == path:
                raise PermissionError("wedged")
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom)
        assert load_cascade_join_file(tmp_path / "sweeps", token=None).state == "unreadable"

    def test_read_oserror_is_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _seed_report(tmp_path)
        # DEC-LAKE-READ-INODE-GUARD-001: the report read rides the guarded
        # deadline reader now — fault THAT seam (Path.read_bytes is unused).
        from brutex.dashboard import sweep_results_view as srv_mod

        real_read = srv_mod.read_regular_bytes_deadline

        def _boom(target: Path, **kwargs: object) -> bytes:
            if target == path:
                raise PermissionError("wedged mid-read")
            return real_read(target, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(srv_mod, "read_regular_bytes_deadline", _boom)
        assert load_cascade_join_file(tmp_path / "sweeps", token=None).state == "unreadable"

    def test_oversized_never_read_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _seed_report(tmp_path)
        monkeypatch.setattr(srv, "CASCADE_REPORT_MAX_BYTES", path.stat().st_size - 1)

        def _never(self: Path) -> bytes:
            raise AssertionError("an oversized report must never be read whole")

        monkeypatch.setattr(Path, "read_bytes", _never)
        load = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        assert load.state == "oversized"
        assert CASCADE_REPORT_MAX_BYTES == 16 * 1024 * 1024

    @pytest.mark.parametrize(
        "raw",
        [
            b"{not json",
            b'{"version": true, "sweep_id": "x", "groups": []}',  # CascadeJoinError
            b'\xff\xfe{"version":1}',  # UnicodeDecodeError
            b"[" * 200_000 + b"]" * 200_000,  # RecursionError nesting bomb
        ],
    )
    def test_malformed_family(self, tmp_path: Path, raw: bytes) -> None:
        report = tmp_path / "sweeps" / "cascade" / "cascade-report.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(raw)
        with capture_logs() as logs:
            load = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        assert load.state == "malformed"
        assert load.join is None
        assert [e for e in logs if e["event"] == "sweep_results.cascade_report_load_failed"]

    def test_memo_one_parse_and_one_warn_per_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_report(tmp_path)
        parses: list[int] = []
        real_loads = json.loads

        def _spy(*args: Any, **kwargs: Any) -> Any:
            parses.append(1)
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(srv.json, "loads", _spy)
        first = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        second = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        assert len(parses) == 1  # the repeat poll pays ZERO parses
        assert second is first
        # A NEW token re-parses (the report moved).
        load_cascade_join_file(tmp_path / "sweeps", token="t2")
        assert len(parses) == 2

    def test_failed_load_warns_once_per_token(self, tmp_path: Path) -> None:
        report = tmp_path / "sweeps" / "cascade" / "cascade-report.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"{junk")
        with capture_logs() as logs:
            load_cascade_join_file(tmp_path / "sweeps", token="t1")
            load_cascade_join_file(tmp_path / "sweeps", token="t1")
        warns = [e for e in logs if e["event"] == "sweep_results.cascade_report_load_failed"]
        assert len(warns) == 1

    def test_none_token_never_memoises(self, tmp_path: Path) -> None:
        _seed_report(tmp_path)
        first = load_cascade_join_file(tmp_path / "sweeps", token=None)
        second = load_cascade_join_file(tmp_path / "sweeps", token=None)
        assert first is not second
        assert srv._JOIN_MEMO is None

    def test_absent_memoised_under_token(self, tmp_path: Path) -> None:
        first = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        second = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        assert second is first


class TestRowProjection:
    def _board(
        self,
        lake_root: Path,
        *,
        cascade: CascadeJoinLoad | None = None,
        composite: CompositeIndex | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return build_sweep_leaderboard(
            lake_root, cascade=cascade, composite=composite, **kwargs
        ).to_dict()

    def test_no_join_payload_byte_identical(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [1, 2, 3])
        legacy = self._board(tmp_path)
        absent_cascade = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        assert absent_cascade.state == "absent"
        absent_composite = CompositeIndex(
            by_run_id={},
            run_id=None,
            state="absent",
            scored_rows=0,
            skipped_files=0,
            dropped_values=0,
        )
        decorated = self._board(tmp_path, cascade=absent_cascade, composite=absent_composite)
        # The BYTE-IDENTITY law: a no-report lake's payload is the pre-change
        # payload exactly — zero new keys, zero value drift.
        assert json.dumps(decorated, sort_keys=True) == json.dumps(legacy, sort_keys=True)
        assert "cascade_join" not in decorated
        assert "composite_join" not in decorated
        assert "cascade_flagged" not in decorated

    def test_joined_rows_and_meta(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [1, 2, 3, 4])  # 4 has no verdict
        _seed_report(tmp_path)
        cascade = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        board = self._board(tmp_path, cascade=cascade)
        rows = {r["run_id"]: r for r in board["rows"]}
        one = rows[_run_id(1)]
        assert one["cascade_status"] == "pass"
        assert one["cascade_status_text"] == "PASS"
        assert one["cascade_provisional"] is False
        assert one["cascade_composite"] == 2.0
        assert one["cpcv_pass_pct"] == 0.9 * 100.0
        assert "cascade_rejected_at" not in one
        two = rows[_run_id(2)]
        assert two["cascade_status"] == "provisional_pass"
        assert two["cascade_status_text"] == "PASS (provisional)"
        assert two["cascade_provisional"] is True
        three = rows[_run_id(3)]
        assert three["cascade_status"] == "fail"
        assert three["cascade_status_text"] == "FAIL @ walk_forward"
        assert three["cascade_rejected_at"] == "walk_forward"
        assert three["cascade_detail"] == "anchored pass-rate 0.5000 < 0.75"
        assert "cascade_composite" not in three
        assert three["cpcv_pass_pct"] == 0.55 * 100.0
        # Unmatched row: NO cascade keys (the client's meta-joined law).
        four = rows[_run_id(4)]
        assert not any(k.startswith("cascade") for k in four)
        assert "cpcv_pass_pct" not in four
        meta = board["cascade_join"]
        assert meta == {
            "state": "joined",
            "sweep_id": "s" * 64,
            "n_input": 3,
            "n_survived": 2,
            "matched_rows": 3,
            "skipped_groups": 0,
            "skipped_verdicts": 0,
            "duplicate_ids": 0,
        }
        assert board["cascade_flagged"] == 1

    def test_fail_rows_stay_ranked_never_excluded(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [1, 3])
        _seed_report(tmp_path)
        cascade = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        board = self._board(tmp_path, cascade=cascade)
        # The FAILED row is present + ranked by the untouched rank law.
        assert [r["run_id"] for r in board["rows"]] == [_run_id(1), _run_id(3)]

    def test_duplicate_row_reads_only_the_status_text(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [1])
        dup = CascadeReport(
            verdicts=(
                CandidateVerdict(
                    candidate_id=_run_id(1),
                    passed=True,
                    rejected_at=None,
                    detail="d",
                    composite_score=1.0,
                ),
            ),
            survivor_ids=(_run_id(1),),
            n_input=1,
            n_survived=1,
            stage_survivors={},
        )
        report = CascadeWireReport(
            sweep_id="s" * 64,
            groups=(("NIFTY", "5m", dup), ("NIFTY", "15m", dup)),
        )
        _seed_report(tmp_path, report)
        cascade = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        board = self._board(tmp_path, cascade=cascade)
        (row,) = board["rows"]
        assert row["cascade_status_text"] == "not validated · duplicate verdict"
        assert "cascade_status" not in row
        assert board["cascade_join"]["duplicate_ids"] == 1
        assert board["cascade_join"]["matched_rows"] == 0
        assert board["cascade_flagged"] == 0

    def test_failed_load_names_the_state_rows_bare(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [1])
        report = tmp_path / "sweeps" / "cascade" / "cascade-report.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"{junk")
        cascade = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        board = self._board(tmp_path, cascade=cascade)
        assert board["cascade_join"]["state"] == "malformed"
        assert board["cascade_join"]["sweep_id"] is None
        assert board["cascade_join"]["matched_rows"] == 0
        assert board["cascade_flagged"] == 0
        (row,) = board["rows"]
        assert not any(k.startswith("cascade") for k in row)


class TestCompositeActivation:
    def _index(self, by_run_id: dict[str, float], state: str = "joined") -> CompositeIndex:
        return CompositeIndex(
            by_run_id=by_run_id,
            run_id="grid" + "0" * 60,
            state=state,
            scored_rows=len(by_run_id),
            skipped_files=0,
            dropped_values=0,
        )

    def test_rows_gain_composite_and_meta(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [1, 2])
        index = self._index({_run_id(1): 1.25})
        board = build_sweep_leaderboard(tmp_path, composite=index).to_dict()
        rows = {r["run_id"]: r for r in board["rows"]}
        assert rows[_run_id(1)]["composite_score"] == 1.25
        assert "composite_score" not in rows[_run_id(2)]  # unscored = honest absence
        assert board["composite_join"] == {
            "state": "joined",
            "run_id": "grid" + "0" * 60,
            "scored_rows": 1,
        }

    def test_error_state_meta_named_rows_bare(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [1])
        index = CompositeIndex(
            by_run_id={},
            run_id="grid" + "0" * 60,
            state="error",
            scored_rows=0,
            skipped_files=0,
            dropped_values=0,
        )
        board = build_sweep_leaderboard(tmp_path, composite=index).to_dict()
        assert board["composite_join"]["state"] == "error"
        (row,) = board["rows"]
        assert "composite_score" not in row

    def test_preset_composite_activates_with_the_index(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [1, 2, 3])
        index = self._index({_run_id(1): 0.5, _run_id(2): 9.0, _run_id(3): 1.0})
        board = build_sweep_leaderboard(tmp_path, preset="composite", composite=index).to_dict()
        # The joined index ORDERS the composite preset (was run_id-only inert).
        assert [r["run_id"] for r in board["rows"]] == [_run_id(2), _run_id(3), _run_id(1)]
        # The honest dynamic disclosure: composite_score LEFT the inert set.
        assert board["preset_inert_axes"] == ["max_consecutive_losses"]

    def test_preset_without_index_keeps_full_inert_disclosure(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [2, 1, 3])
        board = build_sweep_leaderboard(tmp_path, preset="composite").to_dict()
        # No index → the pre-change inert ordering (run_id ASC) + full set.
        assert [r["run_id"] for r in board["rows"]] == [_run_id(1), _run_id(2), _run_id(3)]
        assert board["preset_inert_axes"] == ["composite_score", "max_consecutive_losses"]

    def test_empty_joined_index_stays_inert(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [2, 1])
        board = build_sweep_leaderboard(
            tmp_path, preset="composite", composite=self._index({})
        ).to_dict()
        # A joined-but-EMPTY index cannot activate the axis (nothing scored).
        assert board["preset_inert_axes"] == ["composite_score", "max_consecutive_losses"]
        assert [r["run_id"] for r in board["rows"]] == [_run_id(1), _run_id(2)]
        # …but the meta still discloses the join ran.
        assert board["composite_join"]["scored_rows"] == 0

    def test_unscored_rows_order_after_scored(self, tmp_path: Path) -> None:
        _seed_rows(tmp_path, [1, 2, 3])
        index = self._index({_run_id(3): 0.25})
        board = build_sweep_leaderboard(tmp_path, preset="composite", composite=index).to_dict()
        # None-after-scored (the presets law): the ONE scored row leads.
        assert board["rows"][0]["run_id"] == _run_id(3)


class TestRowKeyEmitOnPresentEdges:
    def test_null_optional_fields_emit_no_keys(self, tmp_path: Path) -> None:
        # A pass with NO composite + NO rate, and a fail with NO stage/detail
        # (loader-tolerated shapes) — each optional key stays absent (never a
        # fabricated 0 / empty string on the wire).
        _seed_rows(tmp_path, [1, 2])
        raw = {
            "version": 1,
            "sweep_id": "s" * 64,
            "n_input": 2,
            "n_survived": 1,
            "groups": [
                {
                    "instrument": "NIFTY",
                    "tf": "5m",
                    "report": {
                        "n_input": 2,
                        "n_survived": 1,
                        "survivor_ids": [_run_id(1)],
                        "verdicts": [
                            {"candidate_id": _run_id(1), "passed": True},
                            {"candidate_id": _run_id(2), "passed": False},
                        ],
                    },
                }
            ],
        }
        report = tmp_path / "sweeps" / "cascade" / "cascade-report.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(json.dumps(raw).encode())
        cascade = load_cascade_join_file(tmp_path / "sweeps", token="t1")
        board = build_sweep_leaderboard(tmp_path, cascade=cascade).to_dict()
        rows = {r["run_id"]: r for r in board["rows"]}
        passer = rows[_run_id(1)]
        assert passer["cascade_status"] == "pass"
        assert "cascade_composite" not in passer
        assert "cpcv_pass_pct" not in passer
        failer = rows[_run_id(2)]
        assert failer["cascade_status"] == "fail"
        assert failer["cascade_status_text"] == "FAIL @ ?"
        assert "cascade_rejected_at" not in failer
        assert "cascade_detail" not in failer
        assert "cpcv_pass_pct" not in failer

    def test_absent_without_token_not_memoised(self, tmp_path: Path) -> None:
        load = load_cascade_join_file(tmp_path / "sweeps", token=None)
        assert load.state == "absent"
        assert srv._JOIN_MEMO is None
