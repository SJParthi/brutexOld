"""Unit tests for :mod:`brutex.data.window_status` (DEC-WINDOW-STATUS-001).

100% line+branch on the pure status model: classification (SUCCESS/EMPTY/FAILED),
the in-memory log's record/attempt-accumulation/snapshot, and the
to_records/from_records round-trip.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_status import (
    EMPTY,
    FAILED,
    SUCCESS,
    WindowStatusLog,
    classify_window_status,
)

_NOW = datetime(2026, 6, 4, 9, 15, tzinfo=UTC)


def _key(*, symbol: str = "NIFTY", interval: str = "1minute") -> WindowKey:
    return WindowKey(
        market="dhan",
        security_id=symbol,
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


class TestClassify:
    def test_error_is_failed(self) -> None:
        assert classify_window_status(0, "DhanTimeoutError") == FAILED
        # error wins even if bars were somehow present
        assert classify_window_status(5, "DhanRateLimitError") == FAILED

    def test_bars_is_success(self) -> None:
        assert classify_window_status(5, None) == SUCCESS

    def test_zero_bars_no_error_is_empty(self) -> None:
        assert classify_window_status(0, None) == EMPTY

    def test_degraded_demotes_a_clean_success_to_failed(self) -> None:
        # DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001: a bars-returning
        # SUCCESS whose stored bars are incomplete (interior gap / truncation) is demoted to
        # a re-pullable FAILED so the skip rule revisits it.
        assert classify_window_status(5, None, degraded=True) == FAILED
        # Default (degraded=False) is byte-identical to the prior SUCCESS classification.
        assert classify_window_status(5, None, degraded=False) == SUCCESS

    def test_degraded_is_ignored_for_empty_and_error(self) -> None:
        # No bars → still EMPTY (nothing to be incomplete); an error → still FAILED.
        assert classify_window_status(0, None, degraded=True) == EMPTY
        assert classify_window_status(0, "GrowwTimeoutError", degraded=True) == FAILED


class TestWindowStatusLog:
    def test_record_success_stores_fields(self) -> None:
        log = WindowStatusLog()
        status = log.record(_key(), raw_bars=10, error_type=None, bars_appended=10, now=_NOW)
        assert status == SUCCESS
        assert log.count == 1
        (rec,) = log.records()
        assert rec.status == SUCCESS
        assert rec.bars == 10
        assert rec.attempts == 1
        assert rec.last_error is None
        assert rec.security_id == "NIFTY"
        assert rec.window_start == "2021-01-01"
        assert rec.last_attempt_at == _NOW.isoformat()

    def test_record_empty(self) -> None:
        log = WindowStatusLog()
        assert log.record(_key(), raw_bars=0, error_type=None, bars_appended=0, now=_NOW) == EMPTY
        assert log.records()[0].status == EMPTY

    def test_record_failed_then_success_accumulates_attempts(self) -> None:
        log = WindowStatusLog()
        key = _key()
        log.record(key, raw_bars=0, error_type="DhanTimeoutError", bars_appended=0, now=_NOW)
        # same window re-pulled, now succeeds → attempts == 2, status flips to SUCCESS
        final = log.record(key, raw_bars=7, error_type=None, bars_appended=7, now=_NOW)
        assert final == SUCCESS
        assert log.count == 1  # same window
        (rec,) = log.records()
        assert rec.status == SUCCESS
        assert rec.attempts == 2
        assert rec.bars == 7
        assert rec.last_error is None

    def test_records_sorted_deterministic(self) -> None:
        log = WindowStatusLog()
        log.record(_key(symbol="SENSEX"), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        log.record(_key(symbol="BANKNIFTY"), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        log.record(_key(symbol="NIFTY"), raw_bars=1, error_type=None, bars_appended=1, now=_NOW)
        symbols = [r.security_id for r in log.records()]
        assert symbols == ["BANKNIFTY", "NIFTY", "SENSEX"]  # sorted

    def test_to_from_records_round_trips(self) -> None:
        log = WindowStatusLog()
        log.record(_key(), raw_bars=3, error_type=None, bars_appended=3, now=_NOW)
        log.record(
            _key(interval="1day"),
            raw_bars=0,
            error_type="DhanServerError",
            bars_appended=0,
            now=_NOW,
        )
        rows = log.to_records()
        restored = WindowStatusLog.from_records(rows)
        assert restored.to_records() == rows
        assert restored.count == 2


class TestG8RoutingPins:
    """DEC-LAKE-RUST-WINDOW-LEDGER-001 — the routed public law ≡ its oracle."""

    def test_classify_equals_python_oracle(self) -> None:
        from brutex.data.window_status import (
            _classify_window_status_python,
            classify_window_status,
        )

        for raw_bars in (0, 1, 375):
            for error_type in (None, "GrowwTimeoutError"):
                for degraded in (False, True):
                    assert classify_window_status(
                        raw_bars, error_type, degraded=degraded
                    ) == _classify_window_status_python(raw_bars, error_type, degraded=degraded)

    def test_window_key_python_law(self) -> None:
        from brutex.data.window_status import WindowStatusLog, window_key_python

        key = WindowKey(
            market="groww",
            security_id="NSE-NIFTY",
            interval="1minute",
            start=date(2024, 1, 2),
            end=date(2024, 1, 3),
        )
        expected = ("groww", "NSE-NIFTY", "1minute", "2024-01-02", "2024-01-03")
        assert window_key_python(key) == expected
        assert WindowStatusLog._key(key) == expected
