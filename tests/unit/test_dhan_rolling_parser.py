"""Tests for the Dhan rolling-ATM option parser + the RollingOptionBar model.

DEC-DHAN-ROLLING-OPTIONS-001.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.adapters.dhan.errors import DhanVendorDriftError
from brutex.adapters.dhan.rolling_option_parser import (
    RESOLVED_EXPIRY_SOURCE_DERIVED,
    RESOLVED_EXPIRY_SOURCE_UNRESOLVED,
    RESOLVED_EXPIRY_SOURCE_VENDOR,
    RollingOptionBar,
    _optional_iv,
    _optional_price,
    _parse_vendor_expiry,
    _resolve_expiry,
    parse_rolling_option_columns,
)
from brutex.data.candle_fields import CandleParseError

_IST = timezone(timedelta(hours=5, minutes=30))


def _epoch(y: int, mo: int, d: int, h: int, mi: int) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


def _valid_bar(**overrides: object) -> RollingOptionBar:
    kwargs: dict[str, object] = {
        "timestamp": datetime(2024, 6, 3, 3, 45, tzinfo=UTC),
        "open": Decimal("101"),
        "high": Decimal("102"),
        "low": Decimal("99"),
        "close": Decimal("100"),
        "volume": 10,
        "open_interest": 5,
        "iv": Decimal("0.15"),
        "spot": Decimal("24010"),
        "resolved_strike": Decimal("24000"),
        "resolved_expiry_date": date(2024, 6, 27),
        "resolved_expiry_source": RESOLVED_EXPIRY_SOURCE_DERIVED,
    }
    kwargs.update(overrides)
    return RollingOptionBar(**kwargs)  # type: ignore[arg-type]


# ── RollingOptionBar model validation ────────────────────────────────────────


def test_valid_bar_constructs() -> None:
    bar = _valid_bar()
    assert bar.resolved_strike == Decimal("24000")
    assert bar.resolved_expiry_date == date(2024, 6, 27)


def test_bar_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _valid_bar(timestamp=datetime(2024, 6, 3, 3, 45))


def test_bar_rejects_inconsistent_ohlc() -> None:
    with pytest.raises(ValueError, match="high"):
        _valid_bar(high=Decimal("50"), low=Decimal("99"))


def test_bar_rejects_negative_iv() -> None:
    with pytest.raises(ValueError, match="iv must be non-negative"):
        _valid_bar(iv=Decimal("-1"))


def test_bar_allows_none_iv_spot_strike() -> None:
    bar = _valid_bar(iv=None, spot=None, resolved_strike=None, resolved_expiry_date=None)
    assert bar.iv is None and bar.spot is None and bar.resolved_strike is None


def test_bar_rejects_nonpositive_spot() -> None:
    with pytest.raises(ValueError, match="spot must be positive"):
        _valid_bar(spot=Decimal("0"))


def test_bar_rejects_nonpositive_strike() -> None:
    with pytest.raises(ValueError, match="resolved_strike must be positive"):
        _valid_bar(resolved_strike=Decimal("-100"))


def test_bar_rejects_unknown_expiry_source() -> None:
    with pytest.raises(ValueError, match="resolved_expiry_source"):
        _valid_bar(resolved_expiry_source="fabricated")


# ── _optional_price / _optional_iv / _to_price ───────────────────────────────


@pytest.mark.parametrize("raw", [None, "", 0, -5])
def test_optional_price_absent_zero_negative_is_none(raw: object) -> None:
    assert _optional_price(raw, "strike") is None


def test_optional_price_valid() -> None:
    assert _optional_price("24000.5", "strike") == Decimal("24000.5")


def test_optional_price_nonfinite_raises() -> None:
    with pytest.raises(CandleParseError, match="non-finite"):
        _optional_price(float("inf"), "strike")


def test_optional_price_unparseable_raises() -> None:
    with pytest.raises(CandleParseError, match="unparseable"):
        _optional_price("abc", "strike")


@pytest.mark.parametrize("raw", [None, ""])
def test_optional_iv_absent_is_none(raw: object) -> None:
    assert _optional_iv(raw) is None


def test_optional_iv_valid_and_zero() -> None:
    assert _optional_iv("0.15") == Decimal("0.15")
    assert _optional_iv("0") == Decimal("0")


@pytest.mark.parametrize("raw", [-0.5, float("nan"), float("inf")])
def test_optional_iv_negative_or_nonfinite_is_none(raw: object) -> None:
    assert _optional_iv(raw) is None


def test_optional_iv_unparseable_raises() -> None:
    with pytest.raises(CandleParseError, match="unparseable iv"):
        _optional_iv("abc")


# ── _parse_vendor_expiry ──────────────────────────────────────────────────────


def test_parse_vendor_expiry_iso_string() -> None:
    assert _parse_vendor_expiry("2024-06-27") == date(2024, 6, 27)


def test_parse_vendor_expiry_iso_datetime_string() -> None:
    assert _parse_vendor_expiry("2024-06-27T00:00:00") == date(2024, 6, 27)


def test_parse_vendor_expiry_empty_string_raises() -> None:
    with pytest.raises(CandleParseError, match="empty vendor expiry"):
        _parse_vendor_expiry("  ")


def test_parse_vendor_expiry_bad_string_raises() -> None:
    with pytest.raises(CandleParseError, match="unparseable vendor expiry"):
        _parse_vendor_expiry("not-a-date")


def test_parse_vendor_expiry_bool_raises() -> None:
    with pytest.raises(CandleParseError, match="must not be bool"):
        _parse_vendor_expiry(True)


def test_parse_vendor_expiry_epoch_seconds() -> None:
    epoch = datetime(2024, 6, 27, 10, 0, tzinfo=UTC).timestamp()
    assert (
        _parse_vendor_expiry(epoch)
        == datetime(2024, 6, 27, 10, 0, tzinfo=UTC).astimezone(_IST).date()
    )


def test_parse_vendor_expiry_out_of_range_epoch_raises() -> None:
    with pytest.raises(CandleParseError, match="out-of-range vendor expiry"):
        _parse_vendor_expiry(1e30)


def test_parse_vendor_expiry_other_type_raises() -> None:
    with pytest.raises(CandleParseError, match="unparseable vendor expiry"):
        _parse_vendor_expiry([2024, 6, 27])


# ── _resolve_expiry ───────────────────────────────────────────────────────────


def test_resolve_expiry_vendor_wins() -> None:
    d, src = _resolve_expiry(
        timestamp=datetime(2024, 6, 3, 3, 45, tzinfo=UTC),
        vendor_expiry_raw="2024-06-27",
        has_vendor_column=True,
        expiry_flag="WEEK",
        expiry_code=0,
        derive_expiry=None,
    )
    assert (d, src) == (date(2024, 6, 27), RESOLVED_EXPIRY_SOURCE_VENDOR)


def test_resolve_expiry_derived() -> None:
    calls: list[tuple[date, str, int]] = []

    def deriver(bar_date: date, flag: str, code: int) -> date:
        calls.append((bar_date, flag, code))
        return date(2024, 7, 25)

    d, src = _resolve_expiry(
        timestamp=datetime(2024, 6, 3, 3, 45, tzinfo=UTC),
        vendor_expiry_raw=None,
        has_vendor_column=False,
        expiry_flag="MONTH",
        expiry_code=1,
        derive_expiry=deriver,
    )
    assert (d, src) == (date(2024, 7, 25), RESOLVED_EXPIRY_SOURCE_DERIVED)
    # The deriver received the bar's IST date + the fixed selectors.
    assert calls == [(date(2024, 6, 3), "MONTH", 1)]


def test_resolve_expiry_unresolved_when_no_echo_no_deriver() -> None:
    d, src = _resolve_expiry(
        timestamp=datetime(2024, 6, 3, 3, 45, tzinfo=UTC),
        vendor_expiry_raw=None,
        has_vendor_column=False,
        expiry_flag="WEEK",
        expiry_code=0,
        derive_expiry=None,
    )
    assert (d, src) == (None, RESOLVED_EXPIRY_SOURCE_UNRESOLVED)


# ── parse_rolling_option_columns ─────────────────────────────────────────────


def _full_payload() -> dict[str, list[object]]:
    return {
        "timestamp": [_epoch(2024, 6, 3, 3, 45), _epoch(2024, 6, 3, 3, 46)],
        "open": [100, 101],
        "high": [102, 103],
        "low": [99, 100],
        "close": [101, 102],
        "volume": [10, 20],
        "oi": [5, 6],
        "iv": [0.15, 0.16],
        "strike": [24000, 24000],
        "spot": [24010, 24020],
    }


def test_parse_full_payload_epoch_utc_and_fields() -> None:
    bars = parse_rolling_option_columns(_full_payload(), expiry_flag="WEEK", expiry_code=0)
    assert len(bars) == 2
    first = bars[0]
    # Epoch → UTC (09:15 IST ⇔ 03:45 UTC).
    assert first.timestamp == datetime(2024, 6, 3, 3, 45, tzinfo=UTC)
    assert first.timestamp.astimezone(_IST).strftime("%H:%M") == "09:15"
    assert first.volume == 10
    assert first.open_interest == 5
    assert first.iv == Decimal("0.15")
    assert first.resolved_strike == Decimal("24000")
    assert first.spot == Decimal("24010")
    # No vendor echo, no deriver → unresolved (honest, never fabricated).
    assert first.resolved_expiry_date is None
    assert first.resolved_expiry_source == RESOLVED_EXPIRY_SOURCE_UNRESOLVED


def test_parse_empty_payload_is_no_data() -> None:
    assert parse_rolling_option_columns({}, expiry_flag="WEEK", expiry_code=0) == []


def test_parse_all_empty_arrays_is_no_data() -> None:
    payload: dict[str, list[object]] = {
        name: [] for name in ("timestamp", "open", "high", "low", "close")
    }
    assert parse_rolling_option_columns(payload, expiry_flag="WEEK", expiry_code=0) == []


def test_parse_missing_required_column_is_drift() -> None:
    payload = _full_payload()
    del payload["close"]
    with pytest.raises(DhanVendorDriftError, match="missing required column"):
        parse_rolling_option_columns(payload, expiry_flag="WEEK", expiry_code=0)


def test_parse_non_array_column_raises() -> None:
    payload = _full_payload()
    payload["open"] = 5  # type: ignore[assignment]
    with pytest.raises(CandleParseError, match="must be an array"):
        parse_rolling_option_columns(payload, expiry_flag="WEEK", expiry_code=0)


def test_parse_unequal_lengths_is_drift() -> None:
    payload = _full_payload()
    payload["open"] = [100]  # shorter than timestamp
    with pytest.raises(DhanVendorDriftError, match="unequal lengths"):
        parse_rolling_option_columns(payload, expiry_flag="WEEK", expiry_code=0)


def test_parse_optional_columns_absent_yield_null_fields() -> None:
    # Only the required columns present → volume 0, oi/iv/spot/strike None.
    payload = {
        "timestamp": [_epoch(2024, 6, 3, 3, 45)],
        "open": [100],
        "high": [102],
        "low": [99],
        "close": [101],
    }
    bars = parse_rolling_option_columns(payload, expiry_flag="WEEK", expiry_code=0)
    assert len(bars) == 1
    bar = bars[0]
    assert bar.volume == 0
    assert bar.open_interest is None
    assert bar.iv is None and bar.spot is None and bar.resolved_strike is None


def test_parse_skips_no_trade_row() -> None:
    payload = {
        "timestamp": [_epoch(2024, 6, 3, 3, 45), _epoch(2024, 6, 3, 3, 46)],
        "open": [None, 100],
        "high": [None, 102],
        "low": [None, 99],
        "close": [None, 101],
    }
    bars = parse_rolling_option_columns(payload, expiry_flag="WEEK", expiry_code=0)
    # The all-empty row is skipped; the real row survives.
    assert len(bars) == 1
    assert bars[0].close == Decimal("101")


def test_parse_vendor_expiry_column_sets_vendor_source() -> None:
    payload = _full_payload()
    payload["expiry"] = ["2024-06-06", "2024-06-06"]
    bars = parse_rolling_option_columns(payload, expiry_flag="WEEK", expiry_code=0)
    assert all(b.resolved_expiry_source == RESOLVED_EXPIRY_SOURCE_VENDOR for b in bars)
    assert all(b.resolved_expiry_date == date(2024, 6, 6) for b in bars)


def test_parse_with_deriver_sets_derived_source() -> None:
    payload = _full_payload()

    def deriver(bar_date: date, flag: str, code: int) -> date:
        return date(2024, 6, 6)

    bars = parse_rolling_option_columns(
        payload, expiry_flag="WEEK", expiry_code=0, derive_expiry=deriver
    )
    assert all(b.resolved_expiry_source == RESOLVED_EXPIRY_SOURCE_DERIVED for b in bars)
    assert all(b.resolved_expiry_date == date(2024, 6, 6) for b in bars)
