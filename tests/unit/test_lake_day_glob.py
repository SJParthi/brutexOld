"""Unit tests for the day-scoped lake partition resolver (DEC-MOVERS-O1-LATENCY-001).

Proves :func:`brutex.data.lake_day_glob.day_month_file` /
:func:`~brutex.data.lake_day_glob.read_day_month_frame` resolve + read exactly the
one ``<base>/<yyyy>/<mm>.parquet`` monthly file an IST trading day's 1-minute bars
live in — and degrade to ``None`` (never raise) when that month was never pulled.
Every branch is reached over a real ``tmp_path`` Parquet so the cold-path read
bound is exercised end-to-end (no fakes).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("polars")

import polars as pl
from brutex.data.lake_day_glob import day_month_file, read_day_month_frame


def _seed_month(base: Path, *, year: int, month: int, rows: int = 3) -> Path:
    """Write a tiny ``<base>/<yyyy>/<mm>.parquet`` and return its path."""
    target = base / f"{year:04d}" / f"{month:02d}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"close": [float(i) for i in range(rows)], "volume": list(range(rows))}).write_parquet(
        target
    )
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
        assert set(frame.columns) == {"close", "volume"}

    def test_returns_none_when_month_absent(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        _seed_month(base, year=2024, month=6)
        assert read_day_month_frame(base, date(2024, 8, 4)) is None
