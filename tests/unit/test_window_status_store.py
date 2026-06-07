"""Unit tests for :mod:`brutex.data.window_status_store` (DEC-WINDOW-STATUS-001).

100% line+branch: the JSON round-trip, the degrade-to-empty reliability paths
(missing / corrupt / wrong-shape), the crash-safe write, and the
begin/end_pull_status dry-run rule.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_status import SUCCESS, WindowStatusLog
from brutex.data.window_status_store import (
    begin_pull_status,
    end_pull_status,
    load_window_status,
    make_live_status_flush,
    save_window_status,
    status_path,
)

_NOW = datetime(2026, 6, 4, 9, 15, tzinfo=UTC)


def _log_with_one() -> WindowStatusLog:
    log = WindowStatusLog()
    log.record(
        WindowKey(
            market="dhan",
            security_id="NIFTY",
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 4, 1),
        ),
        raw_bars=5,
        error_type=None,
        bars_appended=5,
        now=_NOW,
    )
    return log


def test_status_path(tmp_path: Path) -> None:
    assert status_path(tmp_path) == tmp_path / "window_status.json"


def test_live_flush_writes_first_then_throttles_then_writes(tmp_path: Path) -> None:
    # DEC-WINDOW-STATUS-LIVE-001: first call writes; a second within the throttle
    # window is skipped; a call past the window writes again.
    clock = {"t": 100.0}
    flush = make_live_status_flush(
        _log_with_one(), tmp_path, min_interval_s=2.0, clock=lambda: clock["t"]
    )
    flush()  # first → writes
    path = status_path(tmp_path)
    assert path.is_file()

    path.unlink()  # remove so we can detect whether the next call rewrites
    clock["t"] = 101.0  # within the 2s window → throttled, no write
    flush()
    assert not path.is_file()

    clock["t"] = 103.0  # past the window → writes again
    flush()
    assert path.is_file()


def test_live_flush_none_log_is_noop(tmp_path: Path) -> None:
    # A dry-run (None log) records nothing → the flush must never touch disk.
    make_live_status_flush(None, tmp_path)()
    assert not status_path(tmp_path).is_file()


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    assert load_window_status(tmp_path).count == 0


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    save_window_status(_log_with_one(), tmp_path)
    restored = load_window_status(tmp_path)
    assert restored.count == 1
    assert restored.records()[0].status == SUCCESS


def test_save_creates_lake_root(tmp_path: Path) -> None:
    root = tmp_path / "lake"  # does not exist yet
    save_window_status(_log_with_one(), root)
    assert status_path(root).exists()


def test_load_corrupt_json_degrades_to_empty(tmp_path: Path) -> None:
    status_path(tmp_path).write_text("not json {{{", encoding="utf-8")
    assert load_window_status(tmp_path).count == 0


def test_load_wrong_shape_degrades_to_empty(tmp_path: Path) -> None:
    # A non-iterable / wrong-arity payload raises in the comprehension → empty.
    status_path(tmp_path).write_text('{"not": "a list of rows"}', encoding="utf-8")
    assert load_window_status(tmp_path).count == 0


def test_load_with_null_last_error(tmp_path: Path) -> None:
    # last_error == None must round-trip as None (the SUCCESS/EMPTY case).
    save_window_status(_log_with_one(), tmp_path)
    assert load_window_status(tmp_path).records()[0].last_error is None


class TestBeginEndPullStatus:
    def test_dry_run_returns_none_and_never_saves(self, tmp_path: Path) -> None:
        assert begin_pull_status(tmp_path, dry_run=True) is None
        end_pull_status(None, tmp_path, dry_run=True)  # no-op
        assert not status_path(tmp_path).exists()

    def test_live_loads_then_saves(self, tmp_path: Path) -> None:
        save_window_status(_log_with_one(), tmp_path)
        log = begin_pull_status(tmp_path, dry_run=False)
        assert log is not None
        assert log.count == 1  # loaded the persisted history
        end_pull_status(log, tmp_path, dry_run=False)
        assert status_path(tmp_path).exists()

    def test_end_with_none_log_is_noop(self, tmp_path: Path) -> None:
        end_pull_status(None, tmp_path, dry_run=False)  # no log → no write
        assert not status_path(tmp_path).exists()
