"""Unit tests for ``brutex.observability.pull_journal`` (DEC-DOCTOR-LIVE-PROOF-KIT-001, G5/G6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from brutex.observability import pull_journal as pj

_T0 = datetime(2026, 7, 1, 22, 14, tzinfo=UTC)


def _write_run(
    lake: Path,
    *,
    pull_id: str = "p1",
    finish: bool = True,
    heartbeat_gap_s: float = 0.0,
) -> None:
    """Seed one run: start, one heartbeat, optional finish (injected clocks)."""
    pj.record_pull_start(lake, pull_id=pull_id, dry_run=False, now_utc=_T0, monotonic_s=100.0)
    pj.record_pull_heartbeat(
        lake,
        pull_id=pull_id,
        now_utc=_T0 + timedelta(seconds=60 + heartbeat_gap_s),
        monotonic_s=160.0,
    )
    if finish:
        pj.record_pull_finish(
            lake,
            pull_id=pull_id,
            verdict="clean",
            now_utc=_T0 + timedelta(seconds=120 + heartbeat_gap_s),
            monotonic_s=220.0,
        )


class TestRoundTrip:
    def test_start_heartbeat_finish_round_trip(self, tmp_path: Path) -> None:
        _write_run(tmp_path)
        events = pj.read_journal(tmp_path)
        assert [event.event for event in events] == ["started", "heartbeat", "finished"]
        assert events[0].dry_run is False
        assert events[0].pull_id == "p1"
        assert events[2].verdict == "clean"
        assert events[1].dry_run is None
        assert events[0].verdict is None

    def test_missing_journal_is_empty(self, tmp_path: Path) -> None:
        assert pj.read_journal(tmp_path) == []

    def test_journal_path_layout(self, tmp_path: Path) -> None:
        assert pj.journal_path(tmp_path) == tmp_path / "journal" / "pull_journal.jsonl"

    def test_default_clocks_used_when_not_injected(self, tmp_path: Path) -> None:
        pj.record_pull_start(tmp_path, pull_id="p", dry_run=True)
        (event,) = pj.read_journal(tmp_path)
        assert event.monotonic_s > 0
        assert event.dry_run is True

    def test_write_failure_never_raises(self, tmp_path: Path) -> None:
        # The journal dir path exists as a FILE → mkdir/open raises OSError,
        # which must be swallowed (an unwritable journal can never break a pull).
        (tmp_path / "journal").write_text("not a directory", encoding="utf-8")
        pj.record_pull_start(tmp_path, pull_id="p", dry_run=False)  # must not raise


class TestCrashTolerance:
    def test_truncated_and_garbled_lines_are_skipped(self, tmp_path: Path) -> None:
        _write_run(tmp_path)
        path = pj.journal_path(tmp_path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"event": "started", "pull_id": "p2", "wall_')  # power-loss truncation
        events = pj.read_journal(tmp_path)
        assert len(events) == 3  # the garbled tail is skipped, not fatal

    @pytest.mark.parametrize(
        "obj",
        [
            "not-a-dict",
            {"event": "unknown", "pull_id": "p", "wall_utc": "w", "monotonic_s": 1.0},
            {"event": "started", "pull_id": 7, "wall_utc": "w", "monotonic_s": 1.0},
            {"event": "started", "pull_id": "p", "wall_utc": 3, "monotonic_s": 1.0},
            {"event": "started", "pull_id": "p", "wall_utc": "w", "monotonic_s": "x"},
            {"event": "started", "pull_id": "p", "wall_utc": "w", "monotonic_s": True},
            {"event": 5, "pull_id": "p", "wall_utc": "w", "monotonic_s": 1.0},
        ],
    )
    def test_malformed_objects_are_skipped(self, tmp_path: Path, obj: object) -> None:
        path = pj.journal_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(obj) + "\n\n", encoding="utf-8")
        assert pj.read_journal(tmp_path) == []

    def test_non_bool_dry_run_and_non_str_verdict_normalise_to_none(self, tmp_path: Path) -> None:
        path = pj.journal_path(tmp_path)
        path.parent.mkdir(parents=True)
        record = {
            "event": "finished",
            "pull_id": "p",
            "wall_utc": _T0.isoformat(),
            "monotonic_s": 1,
            "dry_run": "yes",
            "verdict": 42,
        }
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        (event,) = pj.read_journal(tmp_path)
        assert event.dry_run is None
        assert event.verdict is None
        assert event.monotonic_s == 1.0


class TestLastRun:
    def test_none_when_no_started_record(self, tmp_path: Path) -> None:
        assert pj.last_run([]) is None
        pj.record_pull_heartbeat(tmp_path, pull_id="p", now_utc=_T0, monotonic_s=1.0)
        assert pj.last_run(pj.read_journal(tmp_path)) is None

    def test_groups_last_run_and_detects_interrupted(self, tmp_path: Path) -> None:
        _write_run(tmp_path, pull_id="p1", finish=True)
        _write_run(tmp_path, pull_id="p2", finish=False)
        run = pj.last_run(pj.read_journal(tmp_path))
        assert run is not None
        assert run.started.pull_id == "p2"
        assert run.interrupted is True
        assert len(run.heartbeats) == 1
        assert run.events == (run.started, *run.heartbeats)

    def test_finished_run_not_interrupted(self, tmp_path: Path) -> None:
        _write_run(tmp_path)
        run = pj.last_run(pj.read_journal(tmp_path))
        assert run is not None
        assert run.interrupted is False
        assert run.finished is not None
        assert run.events[-1] is run.finished

    def test_duplicate_started_event_is_neither_heartbeat_nor_finish(self, tmp_path: Path) -> None:
        pj.record_pull_start(tmp_path, pull_id="p", dry_run=False, now_utc=_T0, monotonic_s=1.0)
        pj.record_pull_start(tmp_path, pull_id="p", dry_run=False, now_utc=_T0, monotonic_s=2.0)
        run = pj.last_run(pj.read_journal(tmp_path))
        assert run is not None
        assert run.started.monotonic_s == 2.0  # the LAST start anchors the run
        assert run.heartbeats == ()
        assert run.interrupted is True

    def test_foreign_pull_id_events_after_start_are_ignored(self, tmp_path: Path) -> None:
        pj.record_pull_start(tmp_path, pull_id="mine", dry_run=False, now_utc=_T0, monotonic_s=1.0)
        pj.record_pull_finish(
            tmp_path, pull_id="other", verdict="clean", now_utc=_T0, monotonic_s=2.0
        )
        run = pj.last_run(pj.read_journal(tmp_path))
        assert run is not None
        assert run.interrupted is True  # the OTHER pull's finish does not close mine


class TestSleepGaps:
    def test_no_gap_when_clocks_agree(self, tmp_path: Path) -> None:
        _write_run(tmp_path)  # wall deltas 60s, monotonic deltas 60s
        run = pj.last_run(pj.read_journal(tmp_path))
        assert run is not None
        assert pj.sleep_gaps(run) == []

    def test_detects_suspend_between_heartbeats(self, tmp_path: Path) -> None:
        # Wall advances 6h28m more than monotonic between start and heartbeat.
        _write_run(tmp_path, heartbeat_gap_s=6 * 3600 + 28 * 60, finish=False)
        run = pj.last_run(pj.read_journal(tmp_path))
        assert run is not None
        gaps = pj.sleep_gaps(run)
        assert len(gaps) == 1
        assert gaps[0].gap_seconds == pytest.approx(6 * 3600 + 28 * 60)
        assert gaps[0].after_wall_utc == _T0.isoformat()

    def test_sub_threshold_gap_ignored(self, tmp_path: Path) -> None:
        _write_run(tmp_path, heartbeat_gap_s=30.0, finish=False)
        run = pj.last_run(pj.read_journal(tmp_path))
        assert run is not None
        assert pj.sleep_gaps(run) == []

    def test_unparseable_wall_timestamp_skips_pair(self, tmp_path: Path) -> None:
        started = pj.JournalEvent(
            event="started", pull_id="p", wall_utc="not-a-timestamp", monotonic_s=0.0
        )
        finished = pj.JournalEvent(
            event="finished", pull_id="p", wall_utc=_T0.isoformat(), monotonic_s=1.0
        )
        run = pj.JournalRun(started=started, heartbeats=(), finished=finished)
        assert pj.sleep_gaps(run) == []


class TestPullControllerWiring:
    def test_pull_all_spot_writes_start_and_finish_records(self, tmp_path: Path) -> None:
        """The G5 wiring: a dry-run bulk pull leaves a durable started+finished pair."""
        from brutex.dashboard.pull_controller import pull_all_spot

        pull_all_spot(
            [], lake_root=tmp_path, dry_run=True, now=datetime(2026, 5, 30, 10, 0, tzinfo=UTC)
        )
        events = pj.read_journal(tmp_path)
        assert [event.event for event in events] == ["started", "finished"]
        assert events[0].dry_run is True
        assert events[0].pull_id == events[1].pull_id
        assert events[1].verdict is not None
        run = pj.last_run(events)
        assert run is not None
        assert run.interrupted is False


class TestPipelinePostFinishHeartbeatShape:
    """HONEST-ENVELOPE PIN (DEC-PULL-BEAT-PERRUN-001 fix round): under the one-run-id
    pipeline grouping, ``record_pull_start``/``record_pull_finish`` bracket ONLY the
    SPOT phase while later phases (metadata / F&O / prebuild / greeks) keep
    heartbeating under the SAME run id — so a run's journal can legitimately read
    "finished at T, heartbeats after T". This pins the shape so it is a stated fact,
    not a surprise; relocating start/finish to the run level is a named follow-up."""

    def test_post_finish_heartbeats_group_into_the_finished_run(self, tmp_path: Path) -> None:
        pj.record_pull_start(tmp_path, pull_id="p1", dry_run=False, now_utc=_T0, monotonic_s=100.0)
        pj.record_pull_heartbeat(
            tmp_path, pull_id="p1", now_utc=_T0 + timedelta(seconds=60), monotonic_s=160.0
        )
        # The SPOT phase finishes …
        pj.record_pull_finish(
            tmp_path,
            pull_id="p1",
            verdict="clean",
            now_utc=_T0 + timedelta(seconds=120),
            monotonic_s=220.0,
        )
        # … and a LATER pipeline phase (prebuild/greeks) heartbeats under the SAME id.
        pj.record_pull_heartbeat(
            tmp_path, pull_id="p1", now_utc=_T0 + timedelta(seconds=300), monotonic_s=400.0
        )
        run = pj.last_run(pj.read_journal(tmp_path))
        assert run is not None
        assert run.finished is not None  # the run reads FINISHED …
        assert len(run.heartbeats) == 2  # … yet carries the post-finish heartbeat too
        assert run.interrupted is False  # died_mid_run stays False for post-spot phases
        # events() orders (started, *heartbeats, finished?) — the finished record sorts
        # LAST even though the second heartbeat is wall-LATER: the non-chronological
        # finished-then-heartbeats shape, deliberately tolerated.
        events = run.events
        assert events[-1].event == pj.EVENT_FINISHED
        assert events[-2].wall_utc > events[-1].wall_utc  # the reversed pair, pinned
        # sleep_gaps tolerates the one reversed pair (a negative wall-delta pair can
        # never read as a suspend window).
        assert pj.sleep_gaps(run) == []
