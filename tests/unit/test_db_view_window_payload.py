"""Unit tests for :func:`brutex.dashboard.db_view.build_window_payload` (DEC-WINDOW-LEDGER-001, PR-3).

100% line+branch on the /coverage/windows projection: the unified ledger Parquet
is read when present (full PLANNED/SUCCESS/EMPTY/FAILED/SKIPPED + first_seen_at),
otherwise it falls back to the legacy window_status.json sidecar — never raises.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from brutex.dashboard.db_view import build_window_payload
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import save_window_ledger
from brutex.data.window_status import WindowStatusLog
from brutex.data.window_status_store import save_window_status

_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)


def _key(security_id: str = "NIFTY", interval: str = "1minute") -> WindowKey:
    return WindowKey(
        market="dhan",
        security_id=security_id,
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


def test_reads_the_ledger_when_present(tmp_path: Path) -> None:
    ledger = WindowLedger()
    ledger.plan(_key("SENSEX"), now=_NOW)  # PLANNED
    ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)  # SUCCESS
    save_window_ledger(ledger, tmp_path)

    payload = build_window_payload(tmp_path)
    assert payload["source"] == "ledger"
    assert payload["count"] == 2
    by_symbol = {w["symbol"]: w for w in payload["windows"]}
    assert by_symbol["SENSEX"]["status"] == "PLANNED"
    assert by_symbol["NIFTY"]["status"] == "SUCCESS"
    assert by_symbol["NIFTY"]["first_seen_at"] == _NOW.isoformat()
    # The legacy field names are preserved so the existing UI keeps working.
    assert set(by_symbol["NIFTY"]) == {
        "market",
        "symbol",
        "interval",
        "window_start",
        "window_end",
        "status",
        "bars",
        "attempts",
        "last_error",
        "first_seen_at",
        "last_attempt_at",
    }


def test_falls_back_to_status_sidecar_when_no_ledger(tmp_path: Path) -> None:
    log = WindowStatusLog()
    log.record(
        _key("NIFTY"), raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_NOW
    )
    save_window_status(log, tmp_path)

    payload = build_window_payload(tmp_path)
    assert payload["source"] == "status_log"
    assert payload["count"] == 1
    window = payload["windows"][0]
    assert window["status"] == "FAILED"
    assert window["first_seen_at"] is None  # legacy log has no first_seen_at


def test_empty_when_neither_exists(tmp_path: Path) -> None:
    payload = build_window_payload(tmp_path)
    assert payload == {"source": "status_log", "count": 0, "windows": []}
