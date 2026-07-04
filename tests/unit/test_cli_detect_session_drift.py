"""Tests for ``brutex detect-session-drift`` — the reconciler's invocation surface.

DEC-CALENDAR-OBSERVED-SESSIONS-001 (refuter finding R1): the drift reconciler is
deliberately MANUALLY-invoked-only, and THIS command is the named production
surface — without it the detector would ship as dead code. Covers the parser,
the main() dispatch, every exit path of ``cmd_detect_session_drift``, and one
REAL end-to-end run over a synthetic lake (fictional drift day 2026-05-12 —
never a real NSE claim, Golden Rule #1).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from unittest import mock

import polars as pl
import pytest
from brutex import cli
from brutex.calendars import observed_sessions as obs
from brutex.calendars.nse import trading_days_between
from brutex.calendars.observed_sessions import load_observed_holidays
from brutex.data.fetch_coverage import WindowKey
from brutex.data.lake_invariants import ENGINE_INDEX_COORDS
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import save_window_ledger

_MONTH = date(2026, 5, 1)
_DRIFT_DAY = date(2026, 5, 12)
_NOW = datetime(2026, 7, 4, 10, 0, tzinfo=UTC)


def _build_lake(lake_root: Path) -> None:
    days = trading_days_between(_MONTH, date(2026, 5, 31))
    for exchange, segment, symbol in ENGINE_INDEX_COORDS:
        base = lake_root / "bars" / exchange / segment / symbol / "1minute"
        path = base / "2026" / "05.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamps = [
            datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC) for d in days if d != _DRIFT_DAY
        ]
        pl.DataFrame({"timestamp": stamps, "close": [100.0] * len(stamps)}).write_parquet(path)
    ledger = WindowLedger()
    for symbol in ("NIFTY", "BANKNIFTY", "SENSEX"):
        key = WindowKey(
            market="groww",
            security_id=symbol,
            interval="1minute",
            start=_MONTH,
            end=date(2026, 6, 1),
        )
        ledger.record(key, raw_bars=100, error_type=None, bars_appended=100, now=_NOW)
    save_window_ledger(ledger, lake_root)


def test_parser_accepts_month_and_lake_root(tmp_path: Path) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["detect-session-drift", "--month", "2026-05", "--lake-root", str(tmp_path)]
    )
    assert args.command == "detect-session-drift"
    assert args.month == "2026-05"
    assert args.lake_root == tmp_path


def test_main_dispatch_threads_args() -> None:
    with mock.patch.object(cli, "cmd_detect_session_drift", return_value=0) as cmd:
        rc = cli.main(["detect-session-drift", "--month", "2026-05"])
    assert rc == 0
    _, kwargs = cmd.call_args
    assert kwargs["month"] == "2026-05"
    assert kwargs["lake_root"] is None


def test_invalid_month_is_user_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.cmd_detect_session_drift(lake_root=tmp_path, month="May-2026")
    assert rc == cli.EXIT_USER_ERROR
    assert "expected YYYY-MM" in capsys.readouterr().err


def test_not_fully_past_month_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The current IST month is never scanned — the driver's ValueError maps to a
    # loud REFUSED + EXIT_USER_ERROR (uses the real clock; month 9999 is always
    # in-the-future -> the fully-past guard fires before coverage is consulted).
    rc = cli.cmd_detect_session_drift(lake_root=tmp_path, month="9999-01")
    assert rc == cli.EXIT_USER_ERROR
    assert "REFUSED" in capsys.readouterr().err


def test_reconciler_failure_is_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> obs.DriftReport:
        raise obs.ObservedSessionsError("sidecar write failed")

    monkeypatch.setattr(obs, "detect_session_drift", _boom)
    rc = cli.cmd_detect_session_drift(lake_root=tmp_path, month="2026-05")
    assert rc == cli.EXIT_INTERNAL_ERROR
    assert "FAILED" in capsys.readouterr().err


def test_cap_exceeded_is_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    verdict = obs.DriftVerdict(
        month=_MONTH,
        candidates=(date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 7)),
        flagged=(),
        vetoed_failed=(),
        no_evidence=(),
        over_month_cap=True,
        over_year_cap=False,
    )
    report = obs.DriftReport(verdict=verdict, overlay_path="x", wrote_overlay=False)
    monkeypatch.setattr(obs, "detect_session_drift", lambda *_a, **_k: report)
    rc = cli.cmd_detect_session_drift(lake_root=tmp_path, month="2026-05")
    assert rc == cli.EXIT_USER_ERROR
    out = capsys.readouterr().out
    assert "month-cap exceeded: True" in out
    assert "overlay unchanged" in out


def test_end_to_end_flags_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _build_lake(tmp_path)
    rc = cli.cmd_detect_session_drift(lake_root=tmp_path, month="2026-05")
    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "flagged: 2026-05-12" in out
    assert "overlay REWRITTEN" in out
    assert "observed-not-official" in out
    assert load_observed_holidays(tmp_path) == frozenset({_DRIFT_DAY})


def test_end_to_end_clean_month_reports_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _build_lake(tmp_path)
    # Fill the drift day with bars too -> a clean month, nothing flagged.
    days = trading_days_between(_MONTH, date(2026, 5, 31))
    for exchange, segment, symbol in ENGINE_INDEX_COORDS:
        base = tmp_path / "bars" / exchange / segment / symbol / "1minute"
        stamps = [datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC) for d in days]
        pl.DataFrame({"timestamp": stamps, "close": [100.0] * len(stamps)}).write_parquet(
            base / "2026" / "05.parquet"
        )
    rc = cli.cmd_detect_session_drift(lake_root=tmp_path, month="2026-05")
    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "flagged: none" in out
    assert "overlay unchanged" in out
