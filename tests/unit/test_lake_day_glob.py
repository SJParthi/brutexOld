"""Unit tests for the day-scoped lake partition resolver (DEC-MOVERS-O1-LATENCY-001).

Proves :func:`brutex.data.lake_day_glob.day_month_file` /
:func:`~brutex.data.lake_day_glob.read_day_month_frame` /
:func:`~brutex.data.lake_day_glob.read_day_intraday_frame` resolve + read exactly the
one ``<base>/<yyyy>/<mm>.parquet`` monthly file a SESSION-FILTERED IST trading day's
1-minute bars live in — and degrade to ``None`` (never raise) when that month was
never pulled — while a SESSION-EXEMPT symbol (GIFT Nifty) falls back to the WHOLE
partition so its month-boundary bars are never dropped. Every branch is reached
over a real ``tmp_path`` Parquet so the cold-path read bound is exercised
end-to-end (no fakes).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("polars")

import polars as pl
from brutex.data.lake_day_glob import (
    day_month_file,
    read_day_intraday_frame,
    read_day_month_frame,
)


def _seed_month(base: Path, *, year: int, month: int, rows: int = 3) -> Path:
    """Write a tiny ``<base>/<yyyy>/<mm>.parquet`` and return its path.

    Carries the canonical ``timestamp`` column every real month partition has
    (the read gate dedups on it, DEC-LAKE-READ-UNIQUE-001) — one distinct
    minute per row so nothing collapses in these bounded-read tests.
    """
    from datetime import UTC, datetime

    target = base / f"{year:04d}" / f"{month:02d}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "timestamp": [datetime(year, month, 3, 4, i, tzinfo=UTC) for i in range(rows)],
            "close": [float(i) for i in range(rows)],
            "volume": list(range(rows)),
        }
    ).write_parquet(target)
    return target


class TestDayMonthFile:
    """``day_month_file`` returns the month's Parquet when present, else ``None``."""

    def test_returns_path_when_month_file_exists(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        seeded = _seed_month(base, year=2024, month=6)
        assert day_month_file(base, date(2024, 6, 13)) == seeded

    def test_returns_none_when_month_file_absent(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        _seed_month(base, year=2024, month=6)
        # A different month (July) has no file → None (never raises).
        assert day_month_file(base, date(2024, 7, 1)) is None

    def test_returns_none_when_base_dir_missing(self, tmp_path: Path) -> None:
        # Never-pulled symbol — the interval dir itself does not exist.
        assert day_month_file(tmp_path / "1minute", date(2024, 6, 13)) is None

    def test_zero_pads_single_digit_month(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        seeded = _seed_month(base, year=2025, month=1)
        assert day_month_file(base, date(2025, 1, 31)) == seeded
        assert seeded.name == "01.parquet"


class TestReadDayMonthFrame:
    """``read_day_month_frame`` reads the month frame, or ``None`` when absent."""

    def test_reads_month_frame_when_present(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        _seed_month(base, year=2024, month=6, rows=5)
        frame = read_day_month_frame(base, date(2024, 6, 13))
        assert frame is not None
        assert frame.height == 5
        assert set(frame.columns) == {"timestamp", "close", "volume"}

    def test_returns_none_when_month_absent(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        _seed_month(base, year=2024, month=6)
        assert read_day_month_frame(base, date(2024, 8, 4)) is None


class TestReadDayIntradayFrame:
    """Symbol-aware reader: month-bound for session-filtered, whole-partition for exempt."""

    def test_session_filtered_symbol_is_month_bounded(self, tmp_path: Path) -> None:
        # A normal symbol reads ONLY the requested month — a prior-month file is
        # NOT pulled (the byte-equivalence rests on the session never crossing a
        # UTC month boundary, so the prior month holds none of this day's bars).
        base = tmp_path / "1minute"
        _seed_month(base, year=2026, month=6, rows=4)  # prior month
        _seed_month(base, year=2026, month=7, rows=2)  # the requested month
        frame = read_day_intraday_frame(base, date(2026, 7, 1), symbol="NIFTY")
        assert frame is not None
        assert frame.height == 2  # only July, NOT July+June

    def test_session_exempt_symbol_reads_whole_partition(self, tmp_path: Path) -> None:
        # GIFT Nifty (~21h) can have an IST-2026-07-01 bar stored in the PRIOR UTC
        # month's file — the whole-partition fallback keeps BOTH months so those
        # month-boundary bars are never dropped (the regression the fix closes).
        base = tmp_path / "1minute"
        _seed_month(base, year=2026, month=6, rows=4)  # prior-UTC-month early-IST bars
        _seed_month(base, year=2026, month=7, rows=2)
        frame = read_day_intraday_frame(base, date(2026, 7, 1), symbol="GIFTNIFTY")
        assert frame is not None
        assert frame.height == 6  # June (4) + July (2) — nothing dropped

    def test_session_exempt_is_case_insensitive(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        _seed_month(base, year=2026, month=6, rows=4)
        _seed_month(base, year=2026, month=7, rows=2)
        # is_session_exempt upper-cases — a lower-case spelling still takes the
        # whole-partition path.
        frame = read_day_intraday_frame(base, date(2026, 7, 1), symbol="giftnifty")
        assert frame is not None
        assert frame.height == 6

    def test_session_exempt_empty_partition_is_none(self, tmp_path: Path) -> None:
        # Exempt symbol with no parquet anywhere → None (honest degrade, never raises).
        base = tmp_path / "1minute"
        base.mkdir(parents=True)
        assert read_day_intraday_frame(base, date(2026, 7, 1), symbol="GIFTNIFTY") is None

    def test_session_exempt_missing_dir_is_none(self, tmp_path: Path) -> None:
        assert (
            read_day_intraday_frame(tmp_path / "1minute", date(2026, 7, 1), symbol="GIFTNIFTY")
            is None
        )


class TestPoisonedMonthDedup:
    """A physically-duplicated month file collapses keep-FIRST + warns.

    Fails-before: on unfixed main both readers returned the duplicated rows
    verbatim (double-counted movers session volume) with NO warning
    (DEC-LAKE-READ-UNIQUE-001).
    """

    def _seed_poisoned(self, base: Path, *, year: int, month: int) -> None:
        from datetime import UTC, datetime

        target = base / f"{year:04d}" / f"{month:02d}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        t0 = datetime(year, month, 3, 4, 0, tzinfo=UTC)
        pl.DataFrame(
            {
                # The first instant appears TWICE with CONFLICTING closes.
                "timestamp": [t0, t0, datetime(year, month, 3, 4, 1, tzinfo=UTC)],
                "close": [1.0, 99.0, 2.0],
                "volume": [10, 999, 20],
            }
        ).write_parquet(target)

    def test_month_bounded_read_collapses_keep_first(self, tmp_path: Path) -> None:
        import structlog

        base = tmp_path / "1minute"
        self._seed_poisoned(base, year=2026, month=7)
        cap = structlog.testing.LogCapture()
        saved = structlog.get_config()
        try:
            structlog.configure(processors=[cap])
            frame = read_day_month_frame(base, date(2026, 7, 1))
        finally:
            structlog.configure(**saved)
        assert frame is not None
        assert frame.height == 2
        assert frame.get_column("close").to_list() == [1.0, 2.0]  # keep-FIRST won
        events = [e for e in cap.entries if e["event"] == "lake.read.duplicates_dropped"]
        assert len(events) == 1
        assert events[0]["dropped"] == 1
        assert events[0]["conflict_count"] == 1

    def test_exempt_whole_partition_read_collapses_keep_first(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        self._seed_poisoned(base, year=2026, month=7)
        frame = read_day_intraday_frame(base, date(2026, 7, 1), symbol="GIFTNIFTY")
        assert frame is not None
        assert frame.height == 2
        assert frame.get_column("close").to_list() == [1.0, 2.0]


class TestGlobMetacharSymbolDir:
    """DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001 (the #1594 round-15 metachar law
    on the bars tree) — FAILS-BEFORE: a symbol dir named ``NSE-X[12]`` beside
    real ``NSE-X1``/``NSE-X2`` siblings made ``read_day_month_frame``'s
    ``pl.read_parquet(path)`` glob-interpret the concrete month-file path and
    silently serve the SIBLINGS' bars as this symbol's day view."""

    def test_bracket_symbol_dir_reads_literal_file_only(self, tmp_path: Path) -> None:
        bars_root = tmp_path / "bars" / "NSE" / "CASH"
        _seed_month(bars_root / "NSE-X1" / "1minute", year=2024, month=6, rows=2)
        _seed_month(bars_root / "NSE-X2" / "1minute", year=2024, month=6, rows=2)
        own = bars_root / "NSE-X[12]" / "1minute"
        target = own / "2024" / "06.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        from datetime import UTC, datetime

        pl.DataFrame(
            {
                "timestamp": [datetime(2024, 6, 3, 4, 30, tzinfo=UTC)],
                "close": [100.55],
                "volume": [7],
            }
        ).write_parquet(target)
        frame = read_day_month_frame(own, date(2024, 6, 3))
        assert frame is not None
        assert frame.height == 1
        assert frame.get_column("close").to_list() == [100.55]  # the literal file's OWN bars
