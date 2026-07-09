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

import brutex.engine.cascade_wire as cw
import pytest
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
    n: int, *, passed: bool, rejected_at: FilterStage | None = None, provisional: bool = False
) -> CandidateVerdict:
    return CandidateVerdict(
        candidate_id=_rid(n),
        passed=passed,
        rejected_at=rejected_at,
        detail="synthetic",
        composite_score=0.5 if passed else None,
        provisional=provisional,
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
        assert markers == {
            _rid(1): VerdictMarker(passed=True, rejected_at=None, provisional=True),
            _rid(2): VerdictMarker(
                passed=False, rejected_at=FilterStage.WALK_FORWARD.value, provisional=False
            ),
            _rid(3): VerdictMarker(passed=True, rejected_at=None, provisional=False),
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
        assert markers[_rid(7)] == VerdictMarker(passed=True, rejected_at=None, provisional=False)
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
        assert markers[_rid(4)] == VerdictMarker(passed=True, rejected_at=None, provisional=False)


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
        path = tmp_path / "cascade-report.json"
        path.write_text("{}", encoding="utf-8")

        def _deny(_self: Path) -> bytes:
            raise PermissionError("synthetic read outage")

        monkeypatch.setattr(Path, "read_bytes", _deny)
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
