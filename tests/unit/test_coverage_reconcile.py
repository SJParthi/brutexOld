"""Unit tests for :mod:`brutex.dashboard.coverage_reconcile` (DEC-LAKE-RECONCILE-001).

100% line+branch on the pure :func:`reconcile_windows`: a window the lake span
ENCLOSES is marked complete (→ skipped on re-pull, no conflict flood); a window
the lake does NOT cover stays un-marked (→ re-pulled). Spans are injected (the
shape :func:`~brutex.dashboard.db_view.summarize_spot_datasets` returns), so no
DuckDB / lake I/O is touched here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.dhan.ingest import _windows  # the tiler the reconcile must match
from brutex.dashboard.coverage_reconcile import (
    EMPTY_TERMINAL_THRESHOLD,
    reconcile_coverage_with_lake,
    reconcile_empty_terminal,
    reconcile_failed_windows_for_repull,
    reconcile_windows,
)
from brutex.data.fetch_coverage import FetchCoverage, WindowKey
from brutex.data.window_status import WindowStatusLog
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_NIFTY = Instrument(
    exch_id=Exchange.NSE,
    security_id=13,
    symbol_name="NIFTY",
    display_name="NIFTY",
    instrument=InstrumentType.IDX,
    segment=Segment.CASH,
    series=None,
    isin=None,
    underlying_symbol=None,
    underlying_security_id=None,
    lot_size=0,
    tick_size=Decimal("0"),
    sm_freeze_qty=None,
    sm_expiry_date=None,
    strike_price=None,
    asm_gsm_flag=False,
)

_START = date(2021, 1, 1)
_END = date(2021, 8, 1)  # ~212 days → 3 intraday windows


def _span(*, bars: int, first: str | None, last: str | None, interval: str = "1minute") -> dict:
    """A lake-span dict in the shape ``summarize_spot_datasets`` returns."""
    return {
        "exchange": _NIFTY.exch_id.value,
        "segment": _NIFTY.segment.value,
        "symbol": _NIFTY.symbol_name,
        "interval": interval,
        "bars": bars,
        "first_date": first,
        "last_date": last,
    }


def test_full_span_marks_every_window() -> None:
    """A span enclosing the whole range marks all 3 intraday windows complete."""
    cov = FetchCoverage()
    span = _span(bars=100, first="2021-01-01", last="2026-06-03")
    marked = reconcile_windows(
        cov, [span], instruments=[_NIFTY], intervals=["1minute"], start=_START, end=_END
    )
    expected = len(_windows("1minute", _START, _END))
    assert marked == expected == 3
    assert cov.completed_count == 3


def test_marked_keys_match_the_ingest_skip_probe() -> None:
    """The marked WindowKeys use market='dhan' + security_id=symbol_name (ingest parity)."""
    cov = FetchCoverage()
    reconcile_windows(
        cov,
        [_span(bars=100, first="2021-01-01", last="2026-06-03")],
        instruments=[_NIFTY],
        intervals=["1minute"],
        start=_START,
        end=_END,
    )
    for w_start, w_end in _windows("1minute", _START, _END):
        key = WindowKey(
            market="dhan",
            security_id="NIFTY",
            interval="1minute",
            start=w_start,
            end=w_end,
        )
        assert cov.is_complete(key)


def test_partial_span_marks_only_enclosed_windows() -> None:
    """A span that stops mid-range leaves the un-enclosed (recent) windows un-marked."""
    cov = FetchCoverage()
    # last_date only reaches into the 2nd window → the 3rd window is NOT enclosed.
    span = _span(bars=50, first="2021-01-01", last="2021-05-01")
    marked = reconcile_windows(
        cov, [span], instruments=[_NIFTY], intervals=["1minute"], start=_START, end=_END
    )
    assert 0 < marked < 3  # some settled windows enclosed, the tail re-pulls
    assert cov.completed_count == marked


def test_daily_single_window_enclosed() -> None:
    """The 1day single window is enclosed by a current span → marked."""
    cov = FetchCoverage()
    span = _span(bars=1342, first="2021-01-01", last="2026-06-03", interval="1day")
    marked = reconcile_windows(
        cov, [span], instruments=[_NIFTY], intervals=["1day"], start=_START, end=_END
    )
    assert marked == 1
    assert cov.is_complete(
        WindowKey(market="dhan", security_id="NIFTY", interval="1day", start=_START, end=_END)
    )


def test_no_span_for_interval_marks_nothing() -> None:
    """An interval with no matching lake span (a missing 1day dataset) marks nothing → re-pull."""
    cov = FetchCoverage()
    span = _span(bars=100, first="2021-01-01", last="2026-06-03", interval="1minute")
    marked = reconcile_windows(
        cov, [span], instruments=[_NIFTY], intervals=["1day"], start=_START, end=_END
    )
    assert marked == 0
    assert cov.completed_count == 0


def test_zero_bar_span_marks_nothing() -> None:
    """A present-but-empty dataset (bars=0) covers no window."""
    cov = FetchCoverage()
    span = _span(bars=0, first="2021-01-01", last="2026-06-03")
    marked = reconcile_windows(
        cov, [span], instruments=[_NIFTY], intervals=["1minute"], start=_START, end=_END
    )
    assert marked == 0


def test_null_dates_mark_nothing() -> None:
    """A span with no first/last date (unreadable parquet) is skipped, never crashes."""
    cov = FetchCoverage()
    span = _span(bars=100, first=None, last=None)
    marked = reconcile_windows(
        cov, [span], instruments=[_NIFTY], intervals=["1minute"], start=_START, end=_END
    )
    assert marked == 0


def test_span_entirely_after_range_encloses_nothing() -> None:
    """A span whose dates are entirely AFTER the pull range encloses no window → all re-pull."""
    cov = FetchCoverage()
    span = _span(bars=100, first="2027-01-01", last="2027-12-31")
    marked = reconcile_windows(
        cov, [span], instruments=[_NIFTY], intervals=["1minute"], start=_START, end=_END
    )
    assert marked == 0  # nothing enclosed (every window starts before 2027)
    assert cov.completed_count == 0


def test_reconcile_with_lake_delegates_to_loaded_spans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``reconcile_coverage_with_lake`` loads spans then delegates to the pure core."""
    cov = FetchCoverage()
    span = _span(bars=100, first="2021-01-01", last="2026-06-03")
    monkeypatch.setattr("brutex.dashboard.coverage_reconcile.load_lake_spans", lambda _root: [span])
    marked = reconcile_coverage_with_lake(
        cov, tmp_path, instruments=[_NIFTY], intervals=["1minute"], start=_START, end=_END
    )
    assert marked == 3
    assert cov.completed_count == 3


def test_reconcile_over_a_real_parquet_lake_skips_stored_windows() -> None:
    """End-to-end: write real 1-day bars to a tmp lake, reconcile → the window is marked.

    Proves ``load_lake_spans`` reads the real Parquet span and the enclosure rule
    marks the stored window so the ingest would skip it (no re-fetch / flood).
    Skipped if DuckDB / pyarrow are unavailable (matches the data-lake test convention).
    """
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from datetime import UTC, datetime

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    lake_root = Path(__import__("tempfile").mkdtemp())
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    # One 1-day bar per day across the full pull range so the stored span encloses it.
    bars = [
        CandleBar(
            timestamp=datetime(2021, 1, 1, 6, 0, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=1,
            open_interest=None,
        ),
        CandleBar(
            timestamp=datetime(2026, 6, 3, 6, 0, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=1,
            open_interest=None,
        ),
    ]
    sink.append(
        exchange=_NIFTY.exch_id.value,
        segment=_NIFTY.segment.value,
        symbol=_NIFTY.symbol_name,
        interval="1day",
        bars=bars,
    )
    cov = FetchCoverage()
    marked = reconcile_coverage_with_lake(
        cov, lake_root, instruments=[_NIFTY], intervals=["1day"], start=_START, end=_END
    )
    assert marked == 1
    assert cov.is_complete(
        WindowKey(market="dhan", security_id="NIFTY", interval="1day", start=_START, end=_END)
    )


_NOW = datetime(2026, 6, 4, tzinfo=UTC)


def _sensex_key(*, interval: str = "1minute") -> WindowKey:
    """A SENSEX 1-minute window (the genuinely-empty pre-2021-08-04 case)."""
    return WindowKey(
        market="dhan",
        security_id="SENSEX",
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


def _log_recorded(
    key: WindowKey, *, times: int, raw_bars: int, error_type: str | None
) -> WindowStatusLog:
    """A status log with *key* recorded *times* times (attempts accumulate to *times*)."""
    log = WindowStatusLog()
    for _ in range(times):
        log.record(key, raw_bars=raw_bars, error_type=error_type, bars_appended=raw_bars, now=_NOW)
    return log


class TestReconcileEmptyTerminal:
    """DEC-EMPTY-TERMINAL-001: a window seen genuinely EMPTY ≥ threshold times → complete."""

    def test_empty_window_at_threshold_is_marked(self) -> None:
        cov = FetchCoverage()
        key = _sensex_key()
        log = _log_recorded(key, times=EMPTY_TERMINAL_THRESHOLD, raw_bars=0, error_type=None)
        assert reconcile_empty_terminal(cov, log) == 1
        assert cov.is_complete(key)  # genuinely-empty → skipped from now on

    def test_empty_window_below_threshold_is_not_marked(self) -> None:
        cov = FetchCoverage()
        key = _sensex_key()
        log = _log_recorded(key, times=EMPTY_TERMINAL_THRESHOLD - 1, raw_bars=0, error_type=None)
        assert reconcile_empty_terminal(cov, log) == 0
        assert not cov.is_complete(key)  # still gets a few more chances

    def test_failed_window_is_never_terminal(self) -> None:
        # A 429/timeout (FAILED, error_type set) reading 0 bars is NEVER marked,
        # even past the threshold — it keeps retrying (DEC-DHAN-429-RETRY-001).
        cov = FetchCoverage()
        key = _sensex_key()
        log = _log_recorded(
            key, times=EMPTY_TERMINAL_THRESHOLD + 2, raw_bars=0, error_type="DhanRateLimitError"
        )
        assert reconcile_empty_terminal(cov, log) == 0
        assert not cov.is_complete(key)

    def test_success_window_is_not_terminal(self) -> None:
        # A SUCCESS window (bars returned) is not EMPTY → not the terminal path.
        cov = FetchCoverage()
        key = _sensex_key()
        log = _log_recorded(key, times=EMPTY_TERMINAL_THRESHOLD + 1, raw_bars=5, error_type=None)
        assert reconcile_empty_terminal(cov, log) == 0
        assert not cov.is_complete(key)

    def test_custom_threshold(self) -> None:
        cov = FetchCoverage()
        key = _sensex_key()
        log = _log_recorded(key, times=2, raw_bars=0, error_type=None)
        assert reconcile_empty_terminal(cov, log, threshold=2) == 1
        assert cov.is_complete(key)


def _nifty_tile(index: int) -> WindowKey:
    """The *index*-th NIFTY 1-minute fetch tile over the test range (ingest-exact key)."""
    w_start, w_end = list(_windows("1minute", _START, _END))[index]
    return WindowKey(
        market="dhan",
        security_id="NIFTY",
        interval="1minute",
        start=w_start,
        end=w_end,
    )


class TestReconcileFailedWindowsForRepull:
    """DEC-FAILED-WINDOW-FORCE-REPULL-001: a FAILED window is un-marked → re-pulled."""

    def test_failed_window_is_unmarked_for_repull(self) -> None:
        # The internal-gap case: span-enclosure wrongly marked the failed window
        # complete; un-marking it forces the ingest to re-fetch (not skip) it.
        key = _nifty_tile(1)
        cov = FetchCoverage()
        cov.mark_complete(key)  # the wrong span-enclosure mark
        log = WindowStatusLog()
        log.record(key, raw_bars=0, error_type="DhanRateLimitError", bars_appended=0, now=_NOW)
        assert reconcile_failed_windows_for_repull(cov, log) == 1
        assert not cov.is_complete(key)  # re-pulled next pass

    def test_success_window_is_left_complete(self) -> None:
        # A SUCCESS row is genuinely done — never un-marked (no needless re-fetch).
        key = _nifty_tile(1)
        cov = FetchCoverage()
        cov.mark_complete(key)
        log = WindowStatusLog()
        log.record(key, raw_bars=10, error_type=None, bars_appended=10, now=_NOW)  # SUCCESS
        assert reconcile_failed_windows_for_repull(cov, log) == 0
        assert cov.is_complete(key)

    def test_empty_window_is_left_complete(self) -> None:
        # An EMPTY (clean 0-bar) row is not a retryable failure → not un-marked
        # (the EMPTY-terminal reconcile owns it); never re-pulled by this path.
        key = _nifty_tile(1)
        cov = FetchCoverage()
        cov.mark_complete(key)
        log = WindowStatusLog()
        log.record(key, raw_bars=0, error_type=None, bars_appended=0, now=_NOW)  # EMPTY
        assert reconcile_failed_windows_for_repull(cov, log) == 0
        assert cov.is_complete(key)

    def test_failed_window_absent_from_ledger_is_a_noop(self) -> None:
        # Un-marking a window that was never marked complete is a safe no-op.
        key = _nifty_tile(1)
        cov = FetchCoverage()  # ledger empty
        log = WindowStatusLog()
        log.record(key, raw_bars=0, error_type="DhanTimeoutError", bars_appended=0, now=_NOW)
        assert reconcile_failed_windows_for_repull(cov, log) == 1
        assert not cov.is_complete(key)

    def test_no_recorded_window_no_unmark(self) -> None:
        cov = FetchCoverage()
        log = WindowStatusLog()  # nothing recorded
        assert reconcile_failed_windows_for_repull(cov, log) == 0
