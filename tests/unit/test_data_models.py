"""Unit tests for brutex.data.models — CandleBar + CatalogEntry."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from brutex.data.models import CandleBar, CatalogEntry


# ────────────────────────────────────────────────────────────────────────
# CandleBar
# ────────────────────────────────────────────────────────────────────────


def _ts(minute: int = 15) -> datetime:
    return datetime(2024, 1, 25, 9, minute, 0, tzinfo=UTC)


class TestCandleBarHappyPath:
    def test_minimum_valid_fno_bar(self) -> None:
        bar = CandleBar(
            timestamp=_ts(),
            open=Decimal("22000.00"),
            high=Decimal("22010.00"),
            low=Decimal("21990.00"),
            close=Decimal("22005.00"),
            volume=12500,
            open_interest=500,
        )
        assert bar.open_interest == 500
        assert bar.volume == 12500

    def test_equity_bar_with_none_oi(self) -> None:
        bar = CandleBar(
            timestamp=_ts(),
            open=Decimal("2500.00"),
            high=Decimal("2502.00"),
            low=Decimal("2498.00"),
            close=Decimal("2501.00"),
            volume=100_000,
            open_interest=None,
        )
        assert bar.open_interest is None

    def test_locked_bar_high_equal_low(self) -> None:
        # Limit-locked: all four prices identical.
        price = Decimal("100.0")
        bar = CandleBar(
            timestamp=_ts(),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0,
            open_interest=0,
        )
        assert bar.open == bar.high == bar.low == bar.close


class TestCandleBarValidation:
    def test_rejects_naive_timestamp(self) -> None:
        with pytest.raises(ValueError, match="tz-aware|timezone-aware"):
            CandleBar(
                timestamp=datetime(2024, 1, 25, 9, 15),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=0,
                open_interest=None,
            )

    def test_rejects_negative_volume(self) -> None:
        with pytest.raises(ValueError, match="volume"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=-1,
                open_interest=None,
            )

    def test_rejects_negative_open_interest(self) -> None:
        with pytest.raises(ValueError, match="open_interest"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=0,
                open_interest=-5,
            )

    def test_rejects_high_less_than_low(self) -> None:
        with pytest.raises(ValueError, match="high.*low"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("100"),
                high=Decimal("99"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=0,
                open_interest=None,
            )

    def test_rejects_open_outside_range(self) -> None:
        with pytest.raises(ValueError, match="open"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("150"),  # above high
                high=Decimal("100"),
                low=Decimal("90"),
                close=Decimal("95"),
                volume=0,
                open_interest=None,
            )

    def test_rejects_close_outside_range(self) -> None:
        with pytest.raises(ValueError, match="close"):
            CandleBar(
                timestamp=_ts(),
                open=Decimal("95"),
                high=Decimal("100"),
                low=Decimal("90"),
                close=Decimal("80"),  # below low
                volume=0,
                open_interest=None,
            )

    def test_bar_is_frozen(self) -> None:
        bar = CandleBar(
            timestamp=_ts(),
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=0,
            open_interest=None,
        )
        with pytest.raises((AttributeError, TypeError)):
            bar.volume = 42  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# CatalogEntry
# ────────────────────────────────────────────────────────────────────────


def _valid_entry(**overrides: object) -> CatalogEntry:
    defaults: dict[str, object] = {
        "exchange": "NSE",
        "segment": "FNO",
        "symbol": "NSE-NIFTY",
        "interval": "1minute",
        "year": 2024,
        "month": 1,
        "raw_hash": "0" * 64,
        "row_count": 100,
        "first_ts": _ts(minute=15),
        "last_ts": _ts(minute=55),
        "fetched_at": _ts(minute=59),
    }
    defaults.update(overrides)
    return CatalogEntry(**defaults)  # type: ignore[arg-type]


class TestCatalogEntryValidation:
    def test_happy_path(self) -> None:
        entry = _valid_entry()
        assert entry.year == 2024
        assert len(entry.raw_hash) == 64

    def test_rejects_short_hash(self) -> None:
        with pytest.raises(ValueError, match="raw_hash"):
            _valid_entry(raw_hash="abc123")

    @pytest.mark.parametrize("bad_month", [0, 13, -1, 100])
    def test_rejects_bad_month(self, bad_month: int) -> None:
        with pytest.raises(ValueError, match="month"):
            _valid_entry(month=bad_month)

    def test_rejects_negative_row_count(self) -> None:
        with pytest.raises(ValueError, match="row_count"):
            _valid_entry(row_count=-1)

    def test_rejects_first_after_last(self) -> None:
        with pytest.raises(ValueError, match="first_ts"):
            _valid_entry(
                first_ts=_ts(minute=55),
                last_ts=_ts(minute=15),
            )

    def test_rejects_naive_first_ts(self) -> None:
        with pytest.raises(ValueError, match="first_ts"):
            _valid_entry(first_ts=datetime(2024, 1, 1, 9, 15))

    def test_rejects_naive_last_ts(self) -> None:
        with pytest.raises(ValueError, match="last_ts"):
            _valid_entry(last_ts=datetime(2024, 1, 1, 9, 55))

    def test_rejects_naive_fetched_at(self) -> None:
        with pytest.raises(ValueError, match="fetched_at"):
            _valid_entry(fetched_at=datetime(2024, 1, 1, 9, 59))
