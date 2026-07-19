"""Unit tests — :mod:`brutex.crossverify.owed` (the durable owed-day sentinel).

DEC-CROSSVERIFY-OWED-SENTINEL-001 (umbrella-assurance MAJOR-1): the marker
store the pull launchers write at pull-tail-commit ENTRY and the runner
clears at the day's completed material verdict. Laws pinned here: names-only
scan (a planted FIFO/symlink/corrupt marker still reads OWED and is never
opened by the scan — no hang, no crash), best-effort-loud mark/clear (one
WARN on failure, never a raise), kill-switch-coherent ``mark_owed_for_pull``
(no marker under ``BRUTEX_CROSSVERIFY=off`` / ``BRUTEX_CROSSVERIFY_PUBLISH=off``
/ junk env — the mark/auto-clearer parity law), and the D3 window law (prior
days only; beyond-window/future markers KEPT + surfaced).
"""

from __future__ import annotations

import json
import os
import signal
from contextlib import contextmanager
from datetime import date
from typing import TYPE_CHECKING, Any

import structlog.testing
from brutex.crossverify.config import ENV_KILL_SWITCH, ENV_PUBLISH
from brutex.crossverify.owed import (
    MAX_OWED_MARKER_BYTES,
    OWED_VERSION,
    clear_owed,
    list_owed_days,
    load_owed_marker,
    mark_owed,
    mark_owed_for_pull,
    owed_dir,
    owed_marker_path,
    scan_owed_days,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

_DAY = date(2026, 7, 10)
_PRIOR = date(2026, 7, 9)


@contextmanager
def _hang_watchdog(seconds: int = 10) -> Generator[None]:
    """A blocked-syscall regression HANGS rather than fails — the SIGALRM
    handler turns it into a LOUD failure (the #1614 round-6 watchdog idiom)."""

    def _alarm(_signum: int, _frame: object) -> None:
        raise AssertionError(f"owed-store call blocked > {seconds}s (non-regular-inode hang)")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _events(logs: list[Any], event: str) -> list[Any]:
    return [entry for entry in logs if entry["event"] == event]


class TestPaths:
    def test_owed_dir_and_marker_path(self, tmp_path: Path) -> None:
        assert owed_dir(tmp_path) == tmp_path / ".crossverify" / "owed"
        assert owed_marker_path(tmp_path, _DAY) == (
            tmp_path / ".crossverify" / "owed" / "2026-07-10.json"
        )


class TestMarkOwed:
    def test_mark_writes_forensic_json(self, tmp_path: Path) -> None:
        assert mark_owed(tmp_path, _DAY, source="cli-pull-all-spot") is True
        payload = json.loads(owed_marker_path(tmp_path, _DAY).read_bytes())
        assert payload["version"] == OWED_VERSION
        assert payload["day"] == "2026-07-10"
        assert payload["source"] == "cli-pull-all-spot"
        assert "marked_at_utc" in payload

    def test_remark_is_idempotent_latest_wins(self, tmp_path: Path) -> None:
        assert mark_owed(tmp_path, _DAY, source="a") is True
        assert mark_owed(tmp_path, _DAY, source="b") is True
        payload = json.loads(owed_marker_path(tmp_path, _DAY).read_bytes())
        assert payload["source"] == "b"
        assert list_owed_days(tmp_path) == [_DAY]

    def test_mark_failure_is_loud_never_raises(self, tmp_path: Path) -> None:
        # A FILE planted at the owed DIR path: mkdir(parents=True) raises.
        (tmp_path / ".crossverify").mkdir()
        (tmp_path / ".crossverify" / "owed").write_text("not a dir")
        with structlog.testing.capture_logs() as logs:
            assert mark_owed(tmp_path, _DAY, source="dashboard-pipeline") is False
        (failed,) = _events(logs, "crossverify.owed_mark_failed")
        assert failed["day"] == "2026-07-10"

    def test_mark_failure_directory_at_marker_path(self, tmp_path: Path) -> None:
        # A DIRECTORY planted at the marker path: the atomic replace raises.
        owed_marker_path(tmp_path, _DAY).mkdir(parents=True)
        with structlog.testing.capture_logs() as logs:
            assert mark_owed(tmp_path, _DAY, source="cli-pull-all-spot") is False
        assert len(_events(logs, "crossverify.owed_mark_failed")) == 1


class TestMarkOwedForPull:
    def test_enabled_marks(self, tmp_path: Path) -> None:
        assert mark_owed_for_pull(tmp_path, _DAY, source="s", env={}) is True
        assert list_owed_days(tmp_path) == [_DAY]

    def test_kill_switch_off_marks_nothing(self, tmp_path: Path) -> None:
        with structlog.testing.capture_logs() as logs:
            assert (
                mark_owed_for_pull(tmp_path, _DAY, source="s", env={ENV_KILL_SWITCH: "off"})
                is False
            )
        assert list_owed_days(tmp_path) == []
        (skipped,) = _events(logs, "crossverify.owed_mark_skipped")
        assert "disabled" in str(skipped["reason"])

    def test_publish_off_marks_nothing(self, tmp_path: Path) -> None:
        # Mark/auto-clearer PARITY (round-1 review MINOR-1): the D3 catchup
        # that completes an owed day sits behind run.py's publish gate, so
        # staged-only mode (BRUTEX_CROSSVERIFY_PUBLISH=off) must write NO
        # marker — the mark-side twin of test_owed_scan_gated_off_for_no_publish.
        with structlog.testing.capture_logs() as logs:
            assert mark_owed_for_pull(tmp_path, _DAY, source="s", env={ENV_PUBLISH: "off"}) is False
        assert list_owed_days(tmp_path) == []
        (skipped,) = _events(logs, "crossverify.owed_mark_skipped")
        assert "publish disabled" in str(skipped["reason"])

    def test_junk_env_warns_and_marks_nothing(self, tmp_path: Path) -> None:
        with structlog.testing.capture_logs() as logs:
            assert (
                mark_owed_for_pull(tmp_path, _DAY, source="s", env={ENV_KILL_SWITCH: "banana"})
                is False
            )
        assert list_owed_days(tmp_path) == []
        (skipped,) = _events(logs, "crossverify.owed_mark_skipped")
        assert skipped["reason"] == "config unresolvable"


class TestClearOwed:
    def test_clear_removes_marker(self, tmp_path: Path) -> None:
        mark_owed(tmp_path, _DAY, source="s")
        assert clear_owed(tmp_path, _DAY) is True
        assert list_owed_days(tmp_path) == []

    def test_clear_absent_is_clean_true(self, tmp_path: Path) -> None:
        assert clear_owed(tmp_path, _DAY) is True

    def test_clear_failure_is_loud_never_raises(self, tmp_path: Path) -> None:
        # A NON-EMPTY directory planted at the marker path: unlink raises.
        marker = owed_marker_path(tmp_path, _DAY)
        (marker / "junk").mkdir(parents=True)
        with structlog.testing.capture_logs() as logs:
            assert clear_owed(tmp_path, _DAY) is False
        (failed,) = _events(logs, "crossverify.owed_clear_failed")
        assert failed["day"] == "2026-07-10"
        # The day honestly re-reads OWED (never a wrong clear).
        assert list_owed_days(tmp_path) == [_DAY]


class TestListOwedDays:
    def test_missing_dir_is_silent_empty(self, tmp_path: Path) -> None:
        with structlog.testing.capture_logs() as logs:
            assert list_owed_days(tmp_path) == []
        assert logs == []

    def test_sorted_ascending(self, tmp_path: Path) -> None:
        mark_owed(tmp_path, _DAY, source="s")
        mark_owed(tmp_path, _PRIOR, source="s")
        assert list_owed_days(tmp_path) == [_PRIOR, _DAY]

    def test_junk_names_warned_and_skipped(self, tmp_path: Path) -> None:
        mark_owed(tmp_path, _DAY, source="s")
        (owed_dir(tmp_path) / "notes.txt").write_text("junk")
        (owed_dir(tmp_path) / "not-a-date.json").write_text("junk")
        with structlog.testing.capture_logs() as logs:
            assert list_owed_days(tmp_path) == [_DAY]
        junk = _events(logs, "crossverify.owed_junk_entry")
        assert {entry["name"] for entry in junk} == {repr("notes.txt"), repr("not-a-date.json")}

    def test_basic_iso_name_is_junk_not_owed(self, tmp_path: Path) -> None:
        # Refuter round-3 MINOR-1: since Python 3.11 date.fromisoformat also
        # accepts basic ISO ("20260714") — a superset of the canonical
        # <YYYY-MM-DD> the write/clear path produces. A foreign-planted
        # basic-ISO name must be JUNK (warned, never owed) — pre-fix it read
        # owed FOREVER (clear_owed unlinks only the canonical name).
        owed_dir(tmp_path).mkdir(parents=True)
        (owed_dir(tmp_path) / "20260714.json").write_text("{}")
        with structlog.testing.capture_logs() as logs:
            assert list_owed_days(tmp_path) == []
        (junk,) = _events(logs, "crossverify.owed_junk_entry")
        assert junk["name"] == repr("20260714.json")

    def test_week_date_name_is_junk_not_owed(self, tmp_path: Path) -> None:
        # Refuter round-3 MINOR-1 twin: the ISO WEEK spelling ("2026-W28-1")
        # also parses under fromisoformat — must be junk, never owed.
        owed_dir(tmp_path).mkdir(parents=True)
        (owed_dir(tmp_path) / "2026-W28-1.json").write_text("{}")
        with structlog.testing.capture_logs() as logs:
            assert list_owed_days(tmp_path) == []
        (junk,) = _events(logs, "crossverify.owed_junk_entry")
        assert junk["name"] == repr("2026-W28-1.json")

    def test_canonical_plus_basic_pair_owed_exactly_once(self, tmp_path: Path) -> None:
        # Refuter round-3 MINOR-1: a canonical + basic-ISO pair for the SAME
        # day must list the date EXACTLY ONCE (pre-fix: twice ⇒ D3 ran the
        # day twice per scan + a double panel entry) with the basic spelling
        # junk-warned.
        mark_owed(tmp_path, date(2026, 7, 14), source="s")
        (owed_dir(tmp_path) / "20260714.json").write_text("{}")
        with structlog.testing.capture_logs() as logs:
            assert list_owed_days(tmp_path) == [date(2026, 7, 14)]
        (junk,) = _events(logs, "crossverify.owed_junk_entry")
        assert junk["name"] == repr("20260714.json")

    def test_immortal_clear_repro_is_dead(self, tmp_path: Path) -> None:
        # Refuter round-3 MINOR-1, the demonstrated repro pinned dead: plant
        # the basic-ISO name, complete + clear the day — the day must NOT
        # still read owed (pre-fix: clear_owed unlinked only the canonical
        # name, so the planted spelling kept the day owed FOREVER — a
        # perpetual in-window D3 rerun / permanent beyond-window WARN).
        day = date(2026, 7, 14)
        owed_dir(tmp_path).mkdir(parents=True)
        (owed_dir(tmp_path) / "20260714.json").write_text("{}")
        assert clear_owed(tmp_path, day) is True  # unlinks the canonical name only
        with structlog.testing.capture_logs() as logs:
            assert list_owed_days(tmp_path) == []  # the plant is junk, not owed
        assert len(_events(logs, "crossverify.owed_junk_entry")) == 1

    def test_atomic_write_temp_skipped_silently(self, tmp_path: Path) -> None:
        # Refuter round-3 NIT-1: a scan racing an in-flight mark_owed sees
        # the atomic-write temp (`.brutex-write-*`, atomic_io.py) —
        # production-reachable (panel poll vs a pull's mark). It must be
        # skipped SILENTLY: not owed, and NO spurious junk WARN.
        mark_owed(tmp_path, _DAY, source="s")
        (owed_dir(tmp_path) / ".brutex-write-abc123.parquet.tmp").write_text("{}")
        with structlog.testing.capture_logs() as logs:
            assert list_owed_days(tmp_path) == [_DAY]
        assert _events(logs, "crossverify.owed_junk_entry") == []

    def test_planted_fifo_still_reads_owed_never_opened(self, tmp_path: Path) -> None:
        """The names-only law: a FIFO named as a valid day is OWED and the
        scan never opens it — a plain read would block FOREVER for a writer."""
        owed_dir(tmp_path).mkdir(parents=True)
        os.mkfifo(owed_marker_path(tmp_path, _DAY))
        with _hang_watchdog():
            assert list_owed_days(tmp_path) == [_DAY]

    def test_owed_dir_is_a_file_warns_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".crossverify").mkdir()
        (tmp_path / ".crossverify" / "owed").write_text("not a dir")
        with structlog.testing.capture_logs() as logs:
            assert list_owed_days(tmp_path) == []
        assert len(_events(logs, "crossverify.owed_scan_failed")) == 1


class TestLoadOwedMarker:
    def test_absent_silent_none(self, tmp_path: Path) -> None:
        with structlog.testing.capture_logs() as logs:
            assert load_owed_marker(tmp_path, _DAY) is None
        assert logs == []

    def test_valid_marker_loads(self, tmp_path: Path) -> None:
        mark_owed(tmp_path, _DAY, source="dashboard-pipeline")
        payload = load_owed_marker(tmp_path, _DAY)
        assert payload is not None
        assert payload["source"] == "dashboard-pipeline"

    def test_corrupt_json_warns_none(self, tmp_path: Path) -> None:
        owed_dir(tmp_path).mkdir(parents=True)
        owed_marker_path(tmp_path, _DAY).write_bytes(b"{not json")
        with structlog.testing.capture_logs() as logs:
            assert load_owed_marker(tmp_path, _DAY) is None
        (warned,) = _events(logs, "crossverify.owed_marker_unreadable")
        assert warned["shape"] == "corrupt"

    def test_invalid_utf8_warns_none(self, tmp_path: Path) -> None:
        owed_dir(tmp_path).mkdir(parents=True)
        owed_marker_path(tmp_path, _DAY).write_bytes(b"\xff\xfe{}")
        with structlog.testing.capture_logs() as logs:
            assert load_owed_marker(tmp_path, _DAY) is None
        assert len(_events(logs, "crossverify.owed_marker_unreadable")) == 1

    def test_non_dict_and_wrong_version_warn_none(self, tmp_path: Path) -> None:
        owed_dir(tmp_path).mkdir(parents=True)
        owed_marker_path(tmp_path, _DAY).write_bytes(b"[1, 2]")
        assert load_owed_marker(tmp_path, _DAY) is None
        owed_marker_path(tmp_path, _DAY).write_bytes(b'{"version": 99}')
        assert load_owed_marker(tmp_path, _DAY) is None

    def test_nesting_bomb_warns_none(self, tmp_path: Path) -> None:
        owed_dir(tmp_path).mkdir(parents=True)
        depth = 40_000
        owed_marker_path(tmp_path, _DAY).write_bytes(("[" * depth + "]" * depth).encode("utf-8"))
        with structlog.testing.capture_logs() as logs:
            assert load_owed_marker(tmp_path, _DAY) is None
        assert len(_events(logs, "crossverify.owed_marker_unreadable")) == 1

    def test_oversized_warns_none(self, tmp_path: Path) -> None:
        owed_dir(tmp_path).mkdir(parents=True)
        owed_marker_path(tmp_path, _DAY).write_bytes(b"x" * (MAX_OWED_MARKER_BYTES + 1))
        with structlog.testing.capture_logs() as logs:
            assert load_owed_marker(tmp_path, _DAY) is None
        (warned,) = _events(logs, "crossverify.owed_marker_unreadable")
        assert warned["shape"] == "oversized"

    def test_planted_fifo_warns_none_never_hangs(self, tmp_path: Path) -> None:
        owed_dir(tmp_path).mkdir(parents=True)
        os.mkfifo(owed_marker_path(tmp_path, _DAY))
        with _hang_watchdog(), structlog.testing.capture_logs() as logs:
            assert load_owed_marker(tmp_path, _DAY) is None
        (warned,) = _events(logs, "crossverify.owed_marker_unreadable")
        assert warned["shape"] == "not_regular"


class TestScanOwedDays:
    def test_window_filter_prior_days_only(self, tmp_path: Path) -> None:
        mark_owed(tmp_path, _PRIOR, source="s")
        mark_owed(tmp_path, _DAY, source="s")  # today — the current run's own
        assert scan_owed_days(tmp_path, today=_DAY, lookback_days=5) == [_PRIOR]

    def test_oldest_first_multiple_days(self, tmp_path: Path) -> None:
        older = date(2026, 7, 7)
        mark_owed(tmp_path, _PRIOR, source="s")
        mark_owed(tmp_path, older, source="s")
        assert scan_owed_days(tmp_path, today=_DAY, lookback_days=5) == [older, _PRIOR]

    def test_beyond_window_kept_and_surfaced(self, tmp_path: Path) -> None:
        stale = date(2026, 7, 1)  # 9 days back — outside lookback 5
        mark_owed(tmp_path, stale, source="s")
        with structlog.testing.capture_logs() as logs:
            assert scan_owed_days(tmp_path, today=_DAY, lookback_days=5) == []
        (warned,) = _events(logs, "crossverify.owed_beyond_window")
        assert warned["days"] == ["2026-07-01"]
        assert list_owed_days(tmp_path) == [stale]  # KEPT, never dropped

    def test_future_marker_kept_and_surfaced_never_completed(self, tmp_path: Path) -> None:
        future = date(2026, 7, 20)
        mark_owed(tmp_path, future, source="s")
        with structlog.testing.capture_logs() as logs:
            assert scan_owed_days(tmp_path, today=_DAY, lookback_days=5) == []
        (warned,) = _events(logs, "crossverify.owed_beyond_window")
        assert warned["days"] == ["2026-07-20"]
        assert list_owed_days(tmp_path) == [future]

    def test_window_floor_boundary_exact(self, tmp_path: Path) -> None:
        # Round-1 review NIT (b): the floor day (today − lookback) is IN the
        # scan; the day just past it (today − lookback − 1) is OUT of the
        # scan but KEPT + surfaced via the beyond-window WARN.
        at_floor = date(2026, 7, 5)  # today(10th) − 5 — exactly the floor
        past_floor = date(2026, 7, 4)  # floor − 1 — first excluded day
        mark_owed(tmp_path, at_floor, source="s")
        mark_owed(tmp_path, past_floor, source="s")
        with structlog.testing.capture_logs() as logs:
            assert scan_owed_days(tmp_path, today=_DAY, lookback_days=5) == [at_floor]
        (warned,) = _events(logs, "crossverify.owed_beyond_window")
        assert warned["days"] == ["2026-07-04"]
        assert list_owed_days(tmp_path) == [past_floor, at_floor]  # both KEPT

    def test_corrupt_in_window_marker_stays_owed_with_one_warn(self, tmp_path: Path) -> None:
        owed_dir(tmp_path).mkdir(parents=True)
        owed_marker_path(tmp_path, _PRIOR).write_bytes(b"{corrupt")
        with structlog.testing.capture_logs() as logs:
            assert scan_owed_days(tmp_path, today=_DAY, lookback_days=5) == [_PRIOR]
        assert len(_events(logs, "crossverify.owed_marker_unreadable")) == 1

    def test_planted_fifo_in_window_stays_owed_never_hangs(self, tmp_path: Path) -> None:
        owed_dir(tmp_path).mkdir(parents=True)
        os.mkfifo(owed_marker_path(tmp_path, _PRIOR))
        with _hang_watchdog(), structlog.testing.capture_logs() as logs:
            assert scan_owed_days(tmp_path, today=_DAY, lookback_days=5) == [_PRIOR]
        assert len(_events(logs, "crossverify.owed_marker_unreadable")) == 1

    def test_zero_lookback_scans_nothing(self, tmp_path: Path) -> None:
        # The recursive rerun's structural bound (catchup_days=0).
        mark_owed(tmp_path, _PRIOR, source="s")
        with structlog.testing.capture_logs() as logs:
            assert scan_owed_days(tmp_path, today=_DAY, lookback_days=0) == []
        (warned,) = _events(logs, "crossverify.owed_beyond_window")
        assert warned["days"] == ["2026-07-09"]
