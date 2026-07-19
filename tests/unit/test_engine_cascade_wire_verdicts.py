"""Unit tests — the /results cascade-verdict join loader (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

``brutex.engine.cascade_wire.load_verdict_markers`` + ``report_group_count``:
the happy path is driven through the REAL writer (``write_report`` →
``serialize_wire_report`` bytes — never a hand-faked report shape), the
degrades through absent / unreadable / corrupt / hostile-shape files. The
degrade law under test: absent ⇒ empty map silently-at-DEBUG; corrupt ⇒ empty
map + ONE ``cascade_wire.verdict_markers_unreadable`` WARN; a conflicting
duplicate run_id ⇒ the loud :data:`AMBIGUOUS_MARKER`, never a coin-flip chip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from brutex.engine import cascade_wire as cw
from brutex.engine.cascade_wire import (
    AMBIGUOUS_MARKER,
    VerdictMarker,
    load_verdict_markers,
    report_group_count,
)
from brutex.metrics.cascade import CandidateVerdict, CascadeReport, FilterStage
from structlog.testing import capture_logs


def _rid(n: int) -> str:
    """A deterministic 64-hex run identity."""
    return f"{n:064x}"


def _verdict(
    n: int,
    *,
    passed: bool,
    rejected_at: FilterStage | None = None,
    provisional: bool = False,
    dsr_corrected: float | None = None,
) -> CandidateVerdict:
    return CandidateVerdict(
        candidate_id=_rid(n),
        passed=passed,
        rejected_at=rejected_at,
        detail="synthetic",
        composite_score=0.5 if passed else None,
        provisional=provisional,
        dsr_corrected=dsr_corrected,
    )


def _report(groups: tuple[tuple[str, str, CascadeReport], ...]) -> cw.CascadeWireReport:
    return cw.CascadeWireReport(sweep_id="a" * 64, groups=groups)


def _group(*verdicts: CandidateVerdict) -> CascadeReport:
    survivors = tuple(v.candidate_id for v in verdicts if v.passed)
    return CascadeReport(
        verdicts=tuple(verdicts),
        survivor_ids=survivors,
        n_input=len(verdicts),
        n_survived=len(survivors),
        stage_survivors={},
        pool_pbo=None,
    )


class TestLoadVerdictMarkersRealWriter:
    """The happy path — REAL report bytes from the production writer."""

    def test_pass_reject_provisional_markers(self, tmp_path: Path) -> None:
        report = _report(
            (
                (
                    "NIFTY",
                    "1minute",
                    _group(
                        _verdict(1, passed=True, provisional=True),
                        _verdict(2, passed=False, rejected_at=FilterStage.WALK_FORWARD),
                    ),
                ),
                ("SENSEX", "5minute", _group(_verdict(3, passed=True))),
            )
        )
        path = cw.write_report(tmp_path, report)
        markers = load_verdict_markers(path)
        # dsr_n_source="pool" is the REAL writer's run-level default
        # (CascadeReport.dsr_n_source) threaded onto every marker of its group
        # (DEC-RESULTS-TRUST-HONESTY-001) — consciously updated from the
        # pre-change no-stamp expectation.
        assert markers == {
            _rid(1): VerdictMarker(
                passed=True, rejected_at=None, provisional=True, dsr_n_source="pool"
            ),
            _rid(2): VerdictMarker(
                passed=False,
                rejected_at=FilterStage.WALK_FORWARD.value,
                provisional=False,
                dsr_n_source="pool",
            ),
            _rid(3): VerdictMarker(
                passed=True, rejected_at=None, provisional=False, dsr_n_source="pool"
            ),
        }
        assert report_group_count(path) == 2

    def test_identical_duplicate_across_groups_keeps_marker(self, tmp_path: Path) -> None:
        """Content-addressed identity: the same combo judged identically in two
        pools keeps ONE non-ambiguous marker (no false ambiguity).
        """
        report = _report(
            (
                ("NIFTY", "1minute", _group(_verdict(7, passed=True))),
                ("NIFTY", "5minute", _group(_verdict(7, passed=True))),
            )
        )
        path = cw.write_report(tmp_path, report)
        with capture_logs() as logs:
            markers = load_verdict_markers(path)
        assert markers[_rid(7)] == VerdictMarker(
            passed=True, rejected_at=None, provisional=False, dsr_n_source="pool"
        )
        assert not markers[_rid(7)].ambiguous
        assert not [e for e in logs if e["event"] == "cascade_wire.verdict_markers_ambiguous"]

    def test_conflicting_duplicate_collapses_to_ambiguous_with_one_warn(
        self, tmp_path: Path
    ) -> None:
        report = _report(
            (
                ("NIFTY", "1minute", _group(_verdict(9, passed=True))),
                (
                    "NIFTY",
                    "5minute",
                    _group(_verdict(9, passed=False, rejected_at=FilterStage.LOCKBOX)),
                ),
            )
        )
        path = cw.write_report(tmp_path, report)
        with capture_logs() as logs:
            markers = load_verdict_markers(path)
        assert markers[_rid(9)] is AMBIGUOUS_MARKER
        assert markers[_rid(9)].ambiguous
        warns = [e for e in logs if e["event"] == "cascade_wire.verdict_markers_ambiguous"]
        assert len(warns) == 1
        assert warns[0]["conflicts"] == 1

    def test_provisional_key_absent_defaults_false(self, tmp_path: Path) -> None:
        """A pre-R2-SR-F4 report (no ``provisional`` key) loads ``False`` — the
        additive-at-version-1 law, never a parse failure.
        """
        payload: dict[str, Any] = json.loads(
            cw.serialize_wire_report(
                _report((("NIFTY", "1minute", _group(_verdict(4, passed=True))),))
            )
        )
        del payload["groups"][0]["report"]["verdicts"][0]["provisional"]
        path = tmp_path / "cascade-report.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        markers = load_verdict_markers(path)
        assert markers[_rid(4)] == VerdictMarker(
            passed=True, rejected_at=None, provisional=False, dsr_n_source="pool"
        )

    def test_dsr_corrected_round_trips_on_pass_and_reject(self, tmp_path: Path) -> None:
        """DEC-RESULTS-TRUST-HONESTY-001 (audit BLOCKER 5): the verdict's own
        filter-#2 DSR + the group's run-level N/source stamps ride the marker
        on BOTH the pass and the reject arm — full precision, never rounded.
        """
        group = CascadeReport(
            verdicts=(
                _verdict(11, passed=True, dsr_corrected=0.9731218374618234),
                _verdict(
                    12,
                    passed=False,
                    rejected_at=FilterStage.DEFLATED_SHARPE,
                    dsr_corrected=0.0031218374618234,
                ),
            ),
            survivor_ids=(_rid(11),),
            n_input=2,
            n_survived=1,
            stage_survivors={},
            pool_pbo=None,
            dsr_n_source="leaderboard",
            dsr_n_trials=8_400_000,
        )
        path = cw.write_report(tmp_path, _report((("NIFTY", "1minute", group),)))
        markers = load_verdict_markers(path)
        assert markers[_rid(11)] == VerdictMarker(
            passed=True,
            rejected_at=None,
            provisional=False,
            dsr_corrected=0.9731218374618234,
            dsr_n_trials=8_400_000,
            dsr_n_source="leaderboard",
        )
        assert markers[_rid(12)].dsr_corrected == 0.0031218374618234
        assert markers[_rid(12)].dsr_n_trials == 8_400_000
        assert markers[_rid(12)].dsr_n_source == "leaderboard"

    def test_pre_change_report_loads_none_dsr_never_zero(self, tmp_path: Path) -> None:
        """An OLD report (no ``dsr_corrected`` verdict key, no run-level stamp
        keys) loads honest ``None`` in all three dsr fields — never a
        fabricated 0 (the additive-at-version-1 law).
        """
        payload: dict[str, Any] = json.loads(
            cw.serialize_wire_report(
                _report((("NIFTY", "1minute", _group(_verdict(5, passed=True))),))
            )
        )
        report_obj = payload["groups"][0]["report"]
        del report_obj["verdicts"][0]["dsr_corrected"]
        del report_obj["dsr_n_trials"]
        del report_obj["dsr_n_source"]
        path = tmp_path / "cascade-report.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        marker = load_verdict_markers(path)[_rid(5)]
        assert marker.dsr_corrected is None
        assert marker.dsr_n_trials is None
        assert marker.dsr_n_source is None
        assert marker.dsr_corrected != 0

    def test_same_verdict_under_conflicting_dsr_populations_is_ambiguous(
        self, tmp_path: Path
    ) -> None:
        """Two groups judging one run_id under DIFFERENT deflation populations
        (different run-level N) collapse to AMBIGUOUS — a conflicting figure
        is never coin-flip-served (the marker-equality law).
        """

        def _stamped(n_trials: int) -> CascadeReport:
            return CascadeReport(
                verdicts=(_verdict(13, passed=True, dsr_corrected=0.5),),
                survivor_ids=(_rid(13),),
                n_input=1,
                n_survived=1,
                stage_survivors={},
                pool_pbo=None,
                dsr_n_source="leaderboard",
                dsr_n_trials=n_trials,
            )

        report = _report(
            (
                ("NIFTY", "1minute", _stamped(1_000)),
                ("NIFTY", "5minute", _stamped(2_000)),
            )
        )
        path = cw.write_report(tmp_path, report)
        with capture_logs() as logs:
            markers = load_verdict_markers(path)
        assert markers[_rid(13)] is AMBIGUOUS_MARKER
        assert markers[_rid(13)].dsr_corrected is None
        warns = [e for e in logs if e["event"] == "cascade_wire.verdict_markers_ambiguous"]
        assert len(warns) == 1


class TestLoadVerdictMarkersDegrades:
    """Absent / unreadable / corrupt / hostile-shape — empty map, never a raise."""

    def test_absent_report_is_silent_empty(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert load_verdict_markers(tmp_path / "cascade-report.json") == {}
        events = {e["event"] for e in logs}
        assert "cascade_wire.verdict_markers_absent" in events
        assert "cascade_wire.verdict_markers_unreadable" not in events

    def test_enotdir_reads_absent(self, tmp_path: Path) -> None:
        """A stray FILE squatting where the cascade dir belongs → absent, not a raise."""
        (tmp_path / "cascade").write_text("not a directory", encoding="utf-8")
        assert load_verdict_markers(tmp_path / "cascade" / "cascade-report.json") == {}

    def test_io_fault_warns_and_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The injection point is the #1793 loader's own ``os.open`` (the
        hardened read no longer touches ``Path.read_bytes`` — #1851 R3)."""
        from brutex.data import sidecar_read

        path = tmp_path / "cascade-report.json"
        path.write_text("{}", encoding="utf-8")

        def _deny(*_args: object, **_kwargs: object) -> int:
            raise PermissionError("synthetic read outage")

        monkeypatch.setattr(sidecar_read.os, "open", _deny)
        with capture_logs() as logs:
            assert load_verdict_markers(path) == {}
        warns = [e for e in logs if e["event"] == "cascade_wire.verdict_markers_unreadable"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "PermissionError"

    @pytest.mark.parametrize(
        ("content", "label"),
        [
            (b"{not json", "bad-json"),
            (b"\xff\xfe garbage", "bad-encoding"),
            (b"[]", "non-object-root"),
            (b'{"sweep_id": "x"}', "no-groups"),
            (b'{"groups": {}}', "groups-not-list"),
            (b'{"groups": ["x"]}', "group-not-object"),
            (b'{"groups": [{"report": 5}]}', "report-not-object"),
            (b'{"groups": [{"report": {"verdicts": 5}}]}', "verdicts-not-list"),
            (b'{"groups": [{"report": {"verdicts": ["x"]}}]}', "verdict-not-object"),
            (
                b'{"groups": [{"report": {"verdicts": [{"candidate_id": "", "passed": true}]}}]}',
                "empty-candidate",
            ),
            (
                b'{"groups": [{"report": {"verdicts": [{"candidate_id": "a", "passed": "yes"}]}}]}',
                "non-bool-passed",
            ),
            (
                b'{"groups": [{"report": {"verdicts": '
                b'[{"candidate_id": "a", "passed": true, "rejected_at": 3}]}}]}',
                "non-str-rejected-at",
            ),
            (
                b'{"groups": [{"report": {"verdicts": '
                b'[{"candidate_id": "a", "passed": true, "provisional": "no"}]}}]}',
                "non-bool-provisional",
            ),
            (
                b'{"groups": [{"report": {"verdicts": '
                b'[{"candidate_id": "a", "passed": true, "dsr_corrected": "0.5"}]}}]}',
                "non-number-dsr-corrected",
            ),
            (
                b'{"groups": [{"report": {"verdicts": '
                b'[{"candidate_id": "a", "passed": true, "dsr_corrected": true}]}}]}',
                "bool-dsr-corrected",
            ),
            (
                b'{"groups": [{"report": {"dsr_n_trials": "many", "verdicts": '
                b'[{"candidate_id": "a", "passed": true}]}}]}',
                "non-int-dsr-n-trials",
            ),
            (
                b'{"groups": [{"report": {"dsr_n_trials": true, "verdicts": '
                b'[{"candidate_id": "a", "passed": true}]}}]}',
                "bool-dsr-n-trials",
            ),
            (
                b'{"groups": [{"report": {"dsr_n_source": 7, "verdicts": '
                b'[{"candidate_id": "a", "passed": true}]}}]}',
                "non-str-dsr-n-source",
            ),
        ],
    )
    def test_corrupt_shapes_warn_once_and_return_empty(
        self, tmp_path: Path, content: bytes, label: str
    ) -> None:
        path = tmp_path / f"{label}.json"
        path.write_bytes(content)
        with capture_logs() as logs:
            assert load_verdict_markers(path) == {}
        warns = [e for e in logs if e["event"] == "cascade_wire.verdict_markers_unreadable"]
        assert len(warns) == 1


class TestHardenedReportRead:
    """The #1851 R3 hardened-read pins — the #1793 bounded regular-file-only
    loader + the widened parse tuple on BOTH report readers: a nesting bomb /
    non-regular inode / oversized out-of-band file reads the SAME wire
    behavior as the corrupt shapes always did (markers ⇒ empty map + ONE
    unreadable WARN; count ⇒ silent ``None``) — never a raw ``RecursionError``
    (the executed fails-before: pre-change ``'['*60_000`` ESCAPED both), never
    a FIFO hang, never a multi-GB whole-read."""

    def test_nesting_bomb_markers_warn_once_and_return_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "bomb.json"
        path.write_bytes(b"[" * 60_000)
        with capture_logs() as logs:
            assert load_verdict_markers(path) == {}
        warns = [e for e in logs if e["event"] == "cascade_wire.verdict_markers_unreadable"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "RecursionError"

    def test_nesting_bomb_count_reads_none(self, tmp_path: Path) -> None:
        path = tmp_path / "bomb.json"
        path.write_bytes(b"[" * 60_000)
        assert report_group_count(path) is None

    def test_fifo_reads_unreadable_never_hangs(self, tmp_path: Path) -> None:
        """A FIFO planted at the report path: a bare ``read_bytes`` blocks for
        a writer FOREVER — the bounded loader folds it into ``not_regular``
        and both readers return their degrade immediately."""
        import os

        path = tmp_path / "fifo.json"
        os.mkfifo(path)
        with capture_logs() as logs:
            assert load_verdict_markers(path) == {}
        warns = [e for e in logs if e["event"] == "cascade_wire.verdict_markers_unreadable"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "not_regular"
        assert report_group_count(path) is None

    def test_oversized_report_reads_unreadable(self, tmp_path: Path) -> None:
        path = tmp_path / "huge.json"
        with path.open("wb") as fh:
            fh.truncate(cw.VERDICT_REPORT_MAX_BYTES + 1)  # sparse — no real 16 MiB write
        with capture_logs() as logs:
            assert load_verdict_markers(path) == {}
        warns = [e for e in logs if e["event"] == "cascade_wire.verdict_markers_unreadable"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "oversized"
        assert report_group_count(path) is None

    def test_at_cap_report_still_loads(self, tmp_path: Path) -> None:
        """Exactly-at-cap stays readable — the cap refuses only strictly-over."""
        path = cw.write_report(tmp_path, _report(()))
        assert path.stat().st_size <= cw.VERDICT_REPORT_MAX_BYTES
        assert report_group_count(path) == 0


class TestReportGroupCount:
    """``report_group_count`` — silent by design (the loader owns the logging)."""

    def test_absent_and_unreadable_read_none(self, tmp_path: Path) -> None:
        assert report_group_count(tmp_path / "missing.json") is None
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_bytes(b"{not json")
        assert report_group_count(corrupt) is None

    def test_non_report_shapes_read_none(self, tmp_path: Path) -> None:
        for content in (b"[]", b'{"groups": 5}'):
            path = tmp_path / "shape.json"
            path.write_bytes(content)
            assert report_group_count(path) is None

    def test_zero_groups_reads_zero_not_none(self, tmp_path: Path) -> None:
        """An EMPTY-groups report is a real report — 0, never conflated with absent."""
        path = cw.write_report(tmp_path, _report(()))
        assert report_group_count(path) == 0
