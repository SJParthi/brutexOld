"""Unit tests for ``brutex.adapters.dhan.body``.

Pins:

* Field names + types (interval is string for intraday, OMITTED for historical).
* oi=False invariant for Track 1 INDEX instruments.
* 90-day window cap (with off-by-one boundary tests).
* Interval whitelist (only 1/5/15/25/60 minute allowed natively).
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.adapters.dhan.body import (
    INTRADAY_MAX_WINDOW_DAYS,
    SUPPORTED_INTRADAY_INTERVALS,
    HistoricalDailyRequest,
    IntradayChunkRequest,
    build_historical_body,
    build_intraday_body,
)
from brutex.adapters.dhan.constants import BANKNIFTY, INDIAVIX, NIFTY, SENSEX


class TestSupportedIntervals:
    """Native intraday timeframes per Dhan v2."""

    def test_set_matches_dhan_docs(self) -> None:
        assert SUPPORTED_INTRADAY_INTERVALS == ("1", "5", "15", "25", "60")

    def test_intervals_are_strings_not_ints(self) -> None:
        """All entries are str — sending int raises Dhan code 814."""
        for interval in SUPPORTED_INTRADAY_INTERVALS:
            assert isinstance(interval, str)


class TestIntradayChunkRequestValidation:
    """Constructor invariants."""

    def test_valid_request(self) -> None:
        req = IntradayChunkRequest(
            instrument=NIFTY,
            interval="1",
            from_date=date(2024, 3, 1),
            to_date=date(2024, 3, 10),
        )
        assert req.instrument is NIFTY

    def test_invalid_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="interval must be one of"):
            IntradayChunkRequest(
                instrument=NIFTY,
                interval="3",
                from_date=date(2024, 3, 1),
                to_date=date(2024, 3, 10),
            )

    def test_int_interval_raises(self) -> None:
        """An int interval string-typed-as-int must NOT be accepted."""
        with pytest.raises(ValueError, match="interval must be one of"):
            IntradayChunkRequest(
                instrument=NIFTY,
                interval=1,  # type: ignore[arg-type]
                from_date=date(2024, 3, 1),
                to_date=date(2024, 3, 10),
            )

    def test_reversed_window_raises(self) -> None:
        with pytest.raises(ValueError, match="to_date.*must be >= from_date"):
            IntradayChunkRequest(
                instrument=NIFTY,
                interval="1",
                from_date=date(2024, 3, 10),
                to_date=date(2024, 3, 1),
            )

    def test_window_at_boundary_minus_one_allowed(self) -> None:
        """A 89-day window is the largest accepted size."""
        IntradayChunkRequest(
            instrument=NIFTY,
            interval="1",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 1) + (date(2024, 3, 31) - date(2024, 1, 2)),
        )

    def test_window_exceeds_max_raises(self) -> None:
        with pytest.raises(ValueError, match="exceeds Dhan's"):
            IntradayChunkRequest(
                instrument=NIFTY,
                interval="1",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 4, 10),  # 100 days
            )

    def test_zero_day_window_allowed(self) -> None:
        """Same-day request is a valid 1-day chunk."""
        IntradayChunkRequest(
            instrument=NIFTY,
            interval="1",
            from_date=date(2024, 3, 1),
            to_date=date(2024, 3, 1),
        )


class TestBuildIntradayBody:
    """Body dict shape for ``POST /v2/charts/intraday``."""

    @pytest.fixture
    def req(self) -> IntradayChunkRequest:
        return IntradayChunkRequest(
            instrument=NIFTY,
            interval="1",
            from_date=date(2024, 3, 1),
            to_date=date(2024, 3, 10),
        )

    def test_includes_security_id_as_string(self, req: IntradayChunkRequest) -> None:
        body = build_intraday_body(req)
        assert body["securityId"] == "13"
        assert isinstance(body["securityId"], str)

    def test_includes_segment(self, req: IntradayChunkRequest) -> None:
        assert build_intraday_body(req)["exchangeSegment"] == "IDX_I"

    def test_includes_instrument_type(self, req: IntradayChunkRequest) -> None:
        assert build_intraday_body(req)["instrument"] == "INDEX"

    def test_interval_is_string(self, req: IntradayChunkRequest) -> None:
        body = build_intraday_body(req)
        assert body["interval"] == "1"
        assert isinstance(body["interval"], str)

    def test_oi_is_false_for_index(self, req: IntradayChunkRequest) -> None:
        """Track 1 INDEX instruments NEVER request open interest."""
        assert build_intraday_body(req)["oi"] is False

    def test_dates_iso_format(self, req: IntradayChunkRequest) -> None:
        body = build_intraday_body(req)
        assert body["fromDate"] == "2024-03-01"
        assert body["toDate"] == "2024-03-10"

    def test_field_set(self, req: IntradayChunkRequest) -> None:
        """Locked field set — adding/removing a field flips this test."""
        expected = {
            "securityId",
            "exchangeSegment",
            "instrument",
            "interval",
            "oi",
            "fromDate",
            "toDate",
        }
        assert set(build_intraday_body(req).keys()) == expected

    @pytest.mark.parametrize("instrument", [NIFTY, BANKNIFTY, SENSEX, INDIAVIX])
    def test_oi_false_for_all_track1_instruments(self, instrument) -> None:  # type: ignore[no-untyped-def]
        req = IntradayChunkRequest(
            instrument=instrument,
            interval="1",
            from_date=date(2024, 3, 1),
            to_date=date(2024, 3, 5),
        )
        assert build_intraday_body(req)["oi"] is False


class TestBuildHistoricalBody:
    """Body dict shape for ``POST /v2/charts/historical`` (daily)."""

    @pytest.fixture
    def req(self) -> HistoricalDailyRequest:
        return HistoricalDailyRequest(
            instrument=NIFTY,
            from_date=date(2021, 1, 1),
            to_date=date(2026, 5, 18),
        )

    def test_interval_field_is_omitted(self, req: HistoricalDailyRequest) -> None:
        """Historical body has NO interval key — null triggers Dhan 814."""
        assert "interval" not in build_historical_body(req)

    def test_oi_is_false(self, req: HistoricalDailyRequest) -> None:
        assert build_historical_body(req)["oi"] is False

    def test_field_set(self, req: HistoricalDailyRequest) -> None:
        expected = {
            "securityId",
            "exchangeSegment",
            "instrument",
            "oi",
            "fromDate",
            "toDate",
        }
        assert set(build_historical_body(req).keys()) == expected

    def test_no_window_cap(self) -> None:
        """Daily endpoint has no documented 90-day cap (full rolling range OK)."""
        HistoricalDailyRequest(
            instrument=NIFTY,
            from_date=date(2021, 1, 1),
            to_date=date(2026, 5, 18),
        )


class TestMaxWindow:
    """Document INTRADAY_MAX_WINDOW_DAYS is the published Dhan cap."""

    def test_value_is_90(self) -> None:
        assert INTRADAY_MAX_WINDOW_DAYS == 90
