"""Tests for `brutex.adapters.dhan.body` — request-body builders.

100% line+branch: happy paths, date formatting per endpoint, securityId
stringification, every validation branch, and the rolling-option
required_data rules.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.adapters.dhan.body import (
    DhanBodyError,
    build_historical_body,
    build_intraday_body,
    build_rolling_option_body,
)

_FROM = datetime(2021, 8, 1, 9, 15, 0, tzinfo=UTC)
_TO = datetime(2021, 9, 1, 15, 30, 0, tzinfo=UTC)


class TestIntradayBody:
    def test_happy_path_defaults_to_1m(self) -> None:
        body = build_intraday_body(
            security_id=13,
            exchange_segment="NSE_EQ",
            instrument="INDEX",
            from_date=_FROM,
            to_date=_TO,
        )
        assert body == {
            "securityId": "13",
            "exchangeSegment": "NSE_EQ",
            "instrument": "INDEX",
            "interval": "1",
            "oi": True,
            "fromDate": "2021-08-01 09:15:00",
            "toDate": "2021-09-01 15:30:00",
        }

    def test_oi_can_be_disabled(self) -> None:
        body = build_intraday_body(
            security_id=13,
            exchange_segment="NSE_EQ",
            instrument="INDEX",
            from_date=_FROM,
            to_date=_TO,
            oi=False,
        )
        assert body["oi"] is False

    def test_security_id_string_passthrough(self) -> None:
        body = build_intraday_body(
            security_id="25",
            exchange_segment="NSE_EQ",
            instrument="EQUITY",
            from_date=_FROM,
            to_date=_TO,
        )
        assert body["securityId"] == "25"

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"exchange_segment": "MCX_FO"}, "invalid exchangeSegment"),
            ({"instrument": "BADTYPE"}, "invalid instrument"),
            ({"interval": "7"}, "invalid interval"),
        ],
    )
    def test_validation_rejects_bad_enums(self, kwargs: dict[str, str], match: str) -> None:
        base: dict[str, object] = {
            "security_id": 13,
            "exchange_segment": "NSE_EQ",
            "instrument": "INDEX",
            "from_date": _FROM,
            "to_date": _TO,
        }
        base.update(kwargs)
        with pytest.raises(DhanBodyError, match=match):
            build_intraday_body(**base)  # type: ignore[arg-type]


class TestHistoricalBody:
    def test_happy_path_date_only_format(self) -> None:
        body = build_historical_body(
            security_id=51,
            exchange_segment="BSE_EQ",
            instrument="INDEX",
            from_date=_FROM,
            to_date=_TO,
        )
        assert body == {
            "securityId": "51",
            "exchangeSegment": "BSE_EQ",
            "instrument": "INDEX",
            "oi": True,
            "fromDate": "2021-08-01",
            "toDate": "2021-09-01",
        }

    def test_oi_can_be_disabled(self) -> None:
        body = build_historical_body(
            security_id=51,
            exchange_segment="BSE_EQ",
            instrument="INDEX",
            from_date=_FROM,
            to_date=_TO,
            oi=False,
        )
        assert body["oi"] is False

    def test_rejects_bad_segment(self) -> None:
        with pytest.raises(DhanBodyError, match="invalid exchangeSegment"):
            build_historical_body(
                security_id=51,
                exchange_segment="NYSE",
                instrument="INDEX",
                from_date=_FROM,
                to_date=_TO,
            )

    def test_rejects_bad_instrument(self) -> None:
        with pytest.raises(DhanBodyError, match="invalid instrument"):
            build_historical_body(
                security_id=51,
                exchange_segment="NSE_EQ",
                instrument="XYZ",
                from_date=_FROM,
                to_date=_TO,
            )


class TestRollingOptionBody:
    def _call(self, **overrides: object) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "security_id": 13,
            "exchange_segment": "NSE_FNO",
            "instrument": "OPTIDX",
            "expiry_flag": "MONTH",
            "expiry_code": 1,
            "strike": "ATM",
            "drv_option_type": "CALL",
            "required_data": ["open", "high", "low", "close", "volume"],
            "from_date": _FROM,
            "to_date": _TO,
        }
        kwargs.update(overrides)
        return build_rolling_option_body(**kwargs)  # type: ignore[arg-type]

    def test_happy_path(self) -> None:
        body = self._call()
        assert body["securityId"] == "13"
        assert body["interval"] == "1"
        assert body["expiryCode"] == 1
        assert body["strike"] == "ATM"
        assert body["drvOptionType"] == "CALL"
        assert body["requiredData"] == ["open", "high", "low", "close", "volume"]
        assert body["fromDate"] == "2021-08-01"
        assert body["toDate"] == "2021-09-01"

    def test_empty_required_data_rejected(self) -> None:
        with pytest.raises(DhanBodyError, match="non-empty array"):
            self._call(required_data=[])

    def test_invalid_required_data_rejected(self) -> None:
        with pytest.raises(DhanBodyError, match="invalid required_data"):
            self._call(required_data=["open", "delta"])

    def test_invalid_expiry_flag_rejected(self) -> None:
        with pytest.raises(DhanBodyError, match="invalid expiryFlag"):
            self._call(expiry_flag="YEAR")

    def test_invalid_option_type_rejected(self) -> None:
        with pytest.raises(DhanBodyError, match="invalid drvOptionType"):
            self._call(drv_option_type="PUTT")

    def test_invalid_segment_rejected(self) -> None:
        with pytest.raises(DhanBodyError, match="invalid exchangeSegment"):
            self._call(exchange_segment="BAD")

    def test_invalid_instrument_rejected(self) -> None:
        with pytest.raises(DhanBodyError, match="invalid instrument"):
            self._call(instrument="BAD")

    def test_invalid_interval_rejected(self) -> None:
        with pytest.raises(DhanBodyError, match="invalid interval"):
            self._call(interval="3")
