"""Unit tests for brutex.adapters.groww.constants.

The offline tests here pin the expected raw Groww values. A separate
GOLDEN-master test (tests/golden/test_groww_sdk_constants_frozen.py —
added in a later commit) will introspect the live ``growwapi.GrowwAPI``
class and assert that these values still match upstream.
"""

from __future__ import annotations

from brutex.adapters.groww.constants import (
    GROWW_CANDLE_INTERVAL_ATTR_NAMES,
    GROWW_CANDLE_INTERVAL_VALUES,
    GROWW_EXCEPTION_CLASS_NAMES,
    GROWW_EXCHANGE_VALUES,
    GROWW_INSTRUMENT_TYPE_BY_RAW,
    GROWW_INSTRUMENT_TYPE_VALUES,
    GROWW_MAX_DURATION_DAYS,
    GROWW_SEGMENT_VALUES,
)
from brutex.models import Exchange, InstrumentType, Segment


class TestExchangeMapping:
    def test_nse_bse_mapped(self) -> None:
        assert GROWW_EXCHANGE_VALUES[Exchange.NSE] == "NSE"
        assert GROWW_EXCHANGE_VALUES[Exchange.BSE] == "BSE"

    def test_exactly_two_entries(self) -> None:
        assert len(GROWW_EXCHANGE_VALUES) == 2


class TestSegmentMapping:
    def test_cash_fno_mapped(self) -> None:
        assert GROWW_SEGMENT_VALUES[Segment.CASH] == "CASH"
        assert GROWW_SEGMENT_VALUES[Segment.FNO] == "FNO"


class TestCandleIntervalValues:
    def test_all_twelve_intervals_present(self) -> None:
        expected = {
            "MIN_1": "1minute",
            "MIN_2": "2minute",
            "MIN_3": "3minute",
            "MIN_5": "5minute",
            "MIN_10": "10minute",
            "MIN_15": "15minute",
            "MIN_30": "30minute",
            "HOUR_1": "1hour",
            "HOUR_4": "4hour",
            "DAY": "1day",
            "WEEK": "1week",
            "MONTH": "1month",
        }
        assert expected == GROWW_CANDLE_INTERVAL_VALUES

    def test_day_week_month_have_no_suffix(self) -> None:
        """Resolution of OQ-GROWW-CANDLE-NAMES — bare names canonical."""
        assert "DAY" in GROWW_CANDLE_INTERVAL_VALUES
        assert "WEEK" in GROWW_CANDLE_INTERVAL_VALUES
        assert "MONTH" in GROWW_CANDLE_INTERVAL_VALUES
        # The _1 variants should NOT exist
        assert "DAY_1" not in GROWW_CANDLE_INTERVAL_VALUES
        assert "WEEK_1" not in GROWW_CANDLE_INTERVAL_VALUES
        assert "MONTH_1" not in GROWW_CANDLE_INTERVAL_VALUES


class TestCandleIntervalAttrNames:
    def test_all_twelve_attr_names(self) -> None:
        assert len(GROWW_CANDLE_INTERVAL_ATTR_NAMES) == 12

    def test_attr_names_match_prefix_format(self) -> None:
        for name in GROWW_CANDLE_INTERVAL_ATTR_NAMES:
            assert name.startswith("CANDLE_INTERVAL_")

    def test_attr_names_cover_dict_keys(self) -> None:
        short_codes = {n.removeprefix("CANDLE_INTERVAL_") for n in GROWW_CANDLE_INTERVAL_ATTR_NAMES}
        assert short_codes == set(GROWW_CANDLE_INTERVAL_VALUES.keys())


class TestMaxDurationDays:
    def test_min_group_is_30_days(self) -> None:
        for code in ("MIN_1", "MIN_2", "MIN_3", "MIN_5"):
            assert GROWW_MAX_DURATION_DAYS[code] == 30

    def test_sub_hour_group_is_90_days(self) -> None:
        for code in ("MIN_10", "MIN_15", "MIN_30"):
            assert GROWW_MAX_DURATION_DAYS[code] == 90

    def test_hour_plus_group_is_180_days(self) -> None:
        for code in ("HOUR_1", "HOUR_4", "DAY", "WEEK", "MONTH"):
            assert GROWW_MAX_DURATION_DAYS[code] == 180

    def test_covers_every_candle_interval(self) -> None:
        assert set(GROWW_MAX_DURATION_DAYS.keys()) == set(GROWW_CANDLE_INTERVAL_VALUES.keys())


class TestInstrumentTypeMapping:
    def test_all_five_types(self) -> None:
        assert GROWW_INSTRUMENT_TYPE_VALUES[InstrumentType.EQ] == "EQ"
        assert GROWW_INSTRUMENT_TYPE_VALUES[InstrumentType.IDX] == "IDX"
        assert GROWW_INSTRUMENT_TYPE_VALUES[InstrumentType.FUT] == "FUT"
        assert GROWW_INSTRUMENT_TYPE_VALUES[InstrumentType.CE] == "CE"
        assert GROWW_INSTRUMENT_TYPE_VALUES[InstrumentType.PE] == "PE"

    def test_reverse_lookup(self) -> None:
        assert GROWW_INSTRUMENT_TYPE_BY_RAW["IDX"] == InstrumentType.IDX
        assert GROWW_INSTRUMENT_TYPE_BY_RAW["PE"] == InstrumentType.PE

    def test_reverse_lookup_is_total(self) -> None:
        assert set(GROWW_INSTRUMENT_TYPE_BY_RAW.values()) == set(InstrumentType)


class TestExceptionClassNames:
    def test_has_rate_limit_exception(self) -> None:
        assert "GrowwAPIRateLimitException" in GROWW_EXCEPTION_CLASS_NAMES

    def test_british_spelling_authorisation(self) -> None:
        """Groww uses British Authorisation — not Authorization. Locked in."""
        assert "GrowwAPIAuthorisationException" in GROWW_EXCEPTION_CLASS_NAMES
        assert "GrowwAPIAuthorizationException" not in GROWW_EXCEPTION_CLASS_NAMES

    def test_all_eight_classes(self) -> None:
        expected = {
            "GrowwBaseException",
            "GrowwAPIException",
            "GrowwAPIAuthenticationException",
            "GrowwAPIAuthorisationException",
            "GrowwAPIBadRequestException",
            "GrowwAPINotFoundException",
            "GrowwAPIRateLimitException",
            "GrowwAPITimeoutException",
        }
        assert set(GROWW_EXCEPTION_CLASS_NAMES) == expected
