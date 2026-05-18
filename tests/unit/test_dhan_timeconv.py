"""Unit tests for ``brutex.adapters.dhan.timeconv``.

Pins the IST↔UTC conversion at known wall-clock moments. The 5h30m IST
offset is fixed (India does not observe DST), so the conversion is a
single-formula invariant the golden tests can freeze.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from brutex.adapters.dhan.timeconv import IST, UTC, dhan_epoch_to_utc, utc_to_dhan_epoch


class TestZoneInfoSingletons:
    """IST + UTC ZoneInfo objects are well-formed."""

    def test_ist_is_asia_kolkata(self) -> None:
        assert str(IST) == "Asia/Kolkata"

    def test_utc_is_utc(self) -> None:
        assert str(UTC) == "UTC"


class TestDhanEpochToUtc:
    """Pin known IST wall-clock moments → UTC datetime."""

    def test_market_open_2024_03_01(self) -> None:
        """2024-03-01 09:15 IST → 2024-03-01 03:45 UTC."""
        utc_dt = dhan_epoch_to_utc(1709284500)
        assert utc_dt == datetime(2024, 3, 1, 3, 45, 0, tzinfo=UTC)

    def test_market_close_2024_03_01(self) -> None:
        """2024-03-01 15:30 IST → 2024-03-01 10:00 UTC."""
        utc_dt = dhan_epoch_to_utc(1709307000)
        assert utc_dt == datetime(2024, 3, 1, 10, 0, 0, tzinfo=UTC)

    def test_track1_forced_exit_2024_03_01(self) -> None:
        """Track 1 forced exit 15:20 IST → 09:50 UTC."""
        utc_dt = dhan_epoch_to_utc(1709306400)
        assert utc_dt == datetime(2024, 3, 1, 9, 50, 0, tzinfo=UTC)

    def test_returns_tz_aware_utc(self) -> None:
        """Every result has tzinfo = UTC, never naïve, never IST."""
        result = dhan_epoch_to_utc(1709284500)
        assert result.tzinfo is UTC

    def test_does_not_handle_dst(self) -> None:
        """India does NOT observe DST. Summer + winter give same 5h30m offset."""
        # Same wall-clock 09:15 IST on different dates → both 03:45 UTC
        summer = dhan_epoch_to_utc(1717233300)  # 2024-06-01 09:15 IST
        winter = dhan_epoch_to_utc(1701422100)  # 2023-12-01 09:15 IST
        assert summer.hour == 3
        assert summer.minute == 45
        assert winter.hour == 3
        assert winter.minute == 45


class TestUtcToDhanEpoch:
    """Inverse direction: UTC datetime → Dhan epoch integer."""

    def test_round_trip_market_open(self) -> None:
        """2024-03-01 03:45 UTC → 1709284500."""
        utc_dt = datetime(2024, 3, 1, 3, 45, 0, tzinfo=UTC)
        assert utc_to_dhan_epoch(utc_dt) == 1709284500

    def test_round_trip_with_ist_aware_input(self) -> None:
        """IST-aware input also converts correctly via astimezone."""
        ist_dt = datetime(2024, 3, 1, 9, 15, 0, tzinfo=IST)
        assert utc_to_dhan_epoch(ist_dt) == 1709284500

    def test_naive_datetime_raises(self) -> None:
        """Refuses naïve datetimes — Golden Rule #1 no-hallucination."""
        with pytest.raises(ValueError, match="naïve"):
            utc_to_dhan_epoch(datetime(2024, 3, 1, 3, 45, 0))


class TestBidirectionalIdentity:
    """For every Dhan epoch, the round-trip is the identity."""

    @pytest.mark.parametrize(
        "epoch",
        [
            1709284500,  # 2024-03-01 09:15 IST
            1709307000,  # 2024-03-01 15:30 IST
            1717233300,  # 2024-06-01 09:15 IST (summer)
            1701422100,  # 2023-12-01 09:15 IST (winter)
            1620897300,  # 2021-05-13 09:15 IST (rolling-range start)
        ],
    )
    def test_round_trip_preserves_epoch(self, epoch: int) -> None:
        utc_dt = dhan_epoch_to_utc(epoch)
        assert utc_to_dhan_epoch(utc_dt) == epoch
