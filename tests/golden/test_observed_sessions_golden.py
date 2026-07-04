"""Golden byte-equality pin for the observed-sessions overlay sidecar (format v1).

DEC-CALENDAR-OBSERVED-SESSIONS-001. A FIXED synthetic lake (fictional drift day
2026-05-12 — never a real NSE claim, Golden Rule #1) + a FROZEN clock must
serialize to EXACTLY the checked-in fixture bytes: deterministic entry order,
sorted keys, stable note text. Any change to the sidecar format v1 fails here
and must bump ``OBSERVED_OVERLAY_VERSION`` + regenerate the fixture consciously
(Golden Rule #7 — same inputs, bit-for-bit same output).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
from brutex.calendars.nse import trading_days_between
from brutex.calendars.observed_sessions import detect_session_drift, overlay_path
from brutex.data.fetch_coverage import WindowKey
from brutex.data.lake_invariants import ENGINE_INDEX_COORDS
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import save_window_ledger

_FIXTURE = Path(__file__).parent / "fixtures" / "observed_holidays_sidecar_v1.json"
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


def test_overlay_sidecar_bytes_golden(tmp_path: Path) -> None:
    _build_lake(tmp_path)
    report = detect_session_drift(tmp_path, _MONTH, now=_NOW)
    assert report.wrote_overlay is True
    produced = overlay_path(tmp_path).read_bytes()
    expected = _FIXTURE.read_bytes()
    assert produced == expected
