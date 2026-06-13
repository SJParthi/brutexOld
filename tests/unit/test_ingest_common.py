"""Tests for `brutex.adapters.ingest_common` — the vendor-neutral ingest helpers.

100% line+branch on the moved-out shared utilities (PR-J1):

* :func:`~brutex.adapters.ingest_common.plan_windows` + :class:`DateWindow` — the
  generic ``[start, end)`` tiler (single window, exact multiples, clipped tail,
  empty range, the positive-window guard),
* :func:`~brutex.adapters.ingest_common.tile_windows` — the per-interval builder
  over an injected width resolver (a finite width tiles; ``None`` emits one
  un-tiled window),
* :func:`~brutex.adapters.ingest_common.clamp_window_end` +
  :func:`~brutex.adapters.ingest_common.now_ist` — the current-day settled-data
  clamp,
* :func:`~brutex.adapters.ingest_common.summarize_conflicts` — the keep-first
  conflict aggregation.

The auto-retry round loop has its own file (``test_ingest_common_auto_retry.py``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise

import pytest
from brutex.adapters.ingest_common import (
    DateWindow,
    clamp_window_end,
    now_ist,
    plan_windows,
    summarize_conflicts,
    tile_windows,
    window_days_for_interval,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import BarConflict


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 9, 15, tzinfo=UTC)


class TestPlanWindows:
    def test_single_window_when_range_under_limit(self) -> None:
        windows = plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 10), window_days=90)
        assert windows == [
            DateWindow(start=_dt(2021, 1, 1).isoformat(), end=_dt(2021, 1, 10).isoformat())
        ]

    def test_exact_multiple_tiles_without_overlap(self) -> None:
        windows = plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 31), window_days=10)
        assert len(windows) == 3
        # Contiguous + non-overlapping: each window's end == next window's start.
        for a, b in pairwise(windows):
            assert a.end == b.start
        assert windows[0].start == _dt(2021, 1, 1).isoformat()
        assert windows[-1].end == _dt(2021, 1, 31).isoformat()

    def test_final_window_clipped_to_end(self) -> None:
        windows = plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 25), window_days=10)
        assert windows[-1].end == _dt(2021, 1, 25).isoformat()
        # Last span is shorter than the limit (clipped).
        assert windows[-1].start == _dt(2021, 1, 21).isoformat()

    def test_empty_when_start_not_before_end(self) -> None:
        assert plan_windows(_dt(2021, 1, 10), _dt(2021, 1, 10), window_days=90) == []
        assert plan_windows(_dt(2021, 1, 20), _dt(2021, 1, 10), window_days=90) == []

    @pytest.mark.parametrize("bad", [0, -1, -90])
    def test_non_positive_window_raises(self, bad: int) -> None:
        with pytest.raises(ValueError, match="window_days must be positive"):
            plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 10), window_days=bad)


class TestTileWindows:
    def test_finite_width_tiles(self) -> None:
        """A positive width resolver tiles via plan_windows, decoded back to date."""
        windows = tile_windows(
            "1minute", date(2021, 1, 1), date(2021, 4, 1), window_days_for=lambda _i: 30
        )
        assert windows[0][0] == date(2021, 1, 1)
        assert windows[-1][1] == date(2021, 4, 1)
        # Contiguous: each window's end == next window's start.
        for a, b in pairwise(windows):
            assert a[1] == b[0]

    def test_none_width_emits_single_untiled_window(self) -> None:
        """A ``None`` width (the Dhan daily case) emits ONE window over the whole range."""
        windows = tile_windows(
            "1day", date(2021, 1, 1), date(2025, 1, 1), window_days_for=lambda _i: None
        )
        assert windows == [(date(2021, 1, 1), date(2025, 1, 1))]


class TestClampWindowEnd:
    def test_daily_clamped_to_today(self) -> None:
        """The current-day 1-day candle is unsettled → daily bound clamps to today."""
        ist = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
        assert clamp_window_end("1day", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 12)

    def test_daily_not_clamped_when_already_past(self) -> None:
        """A daily bound already at/before today is left untouched (min keeps it)."""
        ist = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
        assert clamp_window_end("1day", date(2026, 6, 1), now_ist=ist) == date(2026, 6, 1)

    def test_intraday_before_close_clamps_to_today(self) -> None:
        """Before 15:30 IST the current day is partial → intraday bound clamps to today."""
        ist = datetime(2026, 6, 12, 9, 0, tzinfo=UTC)  # before 15:30
        assert clamp_window_end("1minute", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 12)

    def test_intraday_after_close_allows_today_plus_one(self) -> None:
        """From 15:30 IST the full session exists → intraday bound allows today + 1."""
        ist = datetime(2026, 6, 12, 16, 0, tzinfo=UTC)  # after 15:30
        assert clamp_window_end("1minute", date(2026, 6, 30), now_ist=ist) == date(2026, 6, 13)


class TestNowIst:
    def test_now_ist_is_kolkata_tz(self) -> None:
        """The production clock default carries the Asia/Kolkata tz."""
        assert now_ist().tzinfo is not None
        assert "Kolkata" in str(now_ist().tzinfo)


def _make_conflict(
    *,
    ts_epoch: int = 1609733100,
    stored_close: str = "105.00",
    vendor_close: str = "104.50",
) -> BarConflict:
    """Build a minimal :class:`BarConflict` for direct summarize_conflicts cover."""
    ts = datetime.fromtimestamp(ts_epoch, tz=UTC)
    stored = CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal(stored_close),
        volume=1,
        open_interest=None,
    )
    vendor = CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal(vendor_close),
        volume=1,
        open_interest=None,
    )
    return BarConflict(timestamp=ts, stored=stored, vendor=vendor)


class TestSummarizeConflicts:
    """Direct unit cover for the pure ``summarize_conflicts`` helper
    (DEC-LAKE-CONFLICT-LOG-AGG-001)."""

    def test_empty_is_zero_and_none(self) -> None:
        """No conflicts → zero count, empty fields, zero delta, null timestamps."""
        assert summarize_conflicts([]) == {
            "conflicts": 0,
            "differing_fields": "",
            "max_close_delta": "0",
            "first_ts": None,
            "last_ts": None,
        }

    def test_first_and_last_ts_are_order_independent(self) -> None:
        """``first_ts`` / ``last_ts`` are the min / max regardless of input order."""
        early = datetime.fromtimestamp(1609733100, tz=UTC)
        late = datetime.fromtimestamp(1609740000, tz=UTC)
        # Feed LATE first, EARLY second — min/max must still bracket correctly.
        c_late = _make_conflict(ts_epoch=int(late.timestamp()))
        c_early = _make_conflict(ts_epoch=int(early.timestamp()))
        summary = summarize_conflicts([c_late, c_early])
        assert summary["conflicts"] == 2
        assert summary["first_ts"] == early.isoformat()
        assert summary["last_ts"] == late.isoformat()

    def test_differing_fields_and_max_close_delta(self) -> None:
        """The differing OHLCV+OI field set + max abs close delta are reported."""
        summary = summarize_conflicts(
            [
                _make_conflict(stored_close="105.00", vendor_close="104.50"),
                _make_conflict(ts_epoch=1609740000, stored_close="100.00", vendor_close="102.00"),
            ]
        )
        assert summary["differing_fields"] == "close"
        assert summary["max_close_delta"] == "2.00"


class TestWindowDaysForInterval:
    """The vendor-neutral interval→tiling-width policy (the back-compat default)."""

    def test_daily_is_a_single_untiled_window(self) -> None:
        """A ``1day`` pull takes the whole range in ONE call → ``None`` width."""
        assert window_days_for_interval("1day") is None

    def test_intraday_uses_the_flat_90_day_cap(self) -> None:
        """Every intraday interval tiles at the flat 90-day per-call cap."""
        assert window_days_for_interval("1minute") == 90
        assert window_days_for_interval("5minute") == 90
