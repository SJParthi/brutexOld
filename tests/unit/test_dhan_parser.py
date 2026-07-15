"""Tests for :mod:`brutex.adapters.dhan.parser` — column arrays, epoch-UTC TZ, drift."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.adapters.dhan.parser import (
    CandleParseError,
    DhanVendorDriftError,
    parse_candle_columns,
    parse_daily_candle_columns,
    parse_epoch_utc,
)

#: 2022-06-01 09:15:00 IST == 2022-06-01 03:45:00 UTC (epoch seconds).
_EPOCH_0915_IST = int(datetime(2022, 6, 1, 3, 45, tzinfo=UTC).timestamp())


def _columns(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.5],
        "volume": [0],
        "timestamp": [_EPOCH_0915_IST],
    }
    base.update(overrides)
    return base


def test_parse_epoch_utc_is_utc_and_correct_wall_clock() -> None:
    ts = parse_epoch_utc(_EPOCH_0915_IST)
    assert ts.tzinfo == UTC
    assert ts == datetime(2022, 6, 1, 3, 45, tzinfo=UTC)
    # Rendered IST (+05:30) is 09:15.
    ist = ts.astimezone(datetime(2022, 6, 1, tzinfo=UTC).tzinfo)  # sanity: tz-aware
    assert ist.utcoffset() == UTC.utcoffset(None)


def test_parse_epoch_utc_is_timezone_invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    # The classic footgun: a naive fromtimestamp reads the box's zone. Prove the
    # parse is INVARIANT under a hostile TZ.
    monkeypatch.setenv("TZ", "America/New_York")
    try:
        import time as _time

        _time.tzset()
    except AttributeError:  # pragma: no cover — tzset absent on some platforms
        pass
    ts = parse_epoch_utc(_EPOCH_0915_IST)
    assert ts == datetime(2022, 6, 1, 3, 45, tzinfo=UTC)


def test_parse_epoch_utc_rejects_bool() -> None:
    with pytest.raises(CandleParseError):
        parse_epoch_utc(True)


def test_parse_epoch_utc_rejects_non_finite() -> None:
    with pytest.raises(CandleParseError):
        parse_epoch_utc(float("inf"))


def test_parse_epoch_utc_rejects_out_of_range() -> None:
    with pytest.raises(CandleParseError):
        parse_epoch_utc(10**20)


def test_parse_epoch_utc_rejects_non_numeric() -> None:
    with pytest.raises(CandleParseError):
        parse_epoch_utc("2022-06-01")


def test_parse_columns_one_bar_exact_decimal_prices() -> None:
    bars = parse_candle_columns(_columns(), drop_volume=True)
    assert len(bars) == 1
    bar = bars[0]
    assert bar.open == Decimal("100.0")
    assert bar.close == Decimal("100.5")
    assert bar.timestamp == datetime(2022, 6, 1, 3, 45, tzinfo=UTC)
    assert bar.volume == 0  # index — dropped


def test_index_volume_passthrough_when_not_dropped() -> None:
    bars = parse_candle_columns(_columns(volume=[7]), drop_volume=False)
    assert bars[0].volume == 7


def test_empty_payload_is_honest_no_data() -> None:
    assert parse_candle_columns({}) == []


def test_all_empty_arrays_is_honest_no_data() -> None:
    empty: dict[str, list[object]] = {
        name: [] for name in ("open", "high", "low", "close", "volume", "timestamp")
    }
    assert parse_candle_columns(empty) == []


def test_no_trade_row_is_skipped() -> None:
    cols = _columns(open=[0], high=[0], low=[0], close=[0])
    assert parse_candle_columns(cols, drop_volume=True) == []


def test_partial_payload_missing_column_is_vendor_drift() -> None:
    cols = _columns()
    del cols["high"]
    with pytest.raises(DhanVendorDriftError) as excinfo:
        parse_candle_columns(cols)
    assert "high" in str(excinfo.value)


def test_unequal_column_lengths_is_vendor_drift() -> None:
    cols = _columns(close=[100.5, 100.6])  # one extra close
    with pytest.raises(DhanVendorDriftError) as excinfo:
        parse_candle_columns(cols)
    assert "unequal" in str(excinfo.value).lower()


def test_non_array_column_is_parse_error() -> None:
    cols = _columns(open=100.0)  # a scalar, not an array
    with pytest.raises(CandleParseError):
        parse_candle_columns(cols)


def test_multiple_bars_preserve_order() -> None:
    second = _EPOCH_0915_IST + 60
    cols = {
        "open": [100.0, 101.0],
        "high": [101.0, 102.0],
        "low": [99.0, 100.0],
        "close": [100.5, 101.5],
        "volume": [0, 0],
        "timestamp": [_EPOCH_0915_IST, second],
    }
    bars = parse_candle_columns(cols, drop_volume=True)
    assert [b.close for b in bars] == [Decimal("100.5"), Decimal("101.5")]


@pytest.fixture(autouse=True)
def _restore_tz() -> object:
    original = os.environ.get("TZ")
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    try:
        import time as _time

        _time.tzset()
    except AttributeError:  # pragma: no cover
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Absent-volume tolerance for index payloads (design §5): under drop_volume=True
# the volume column is OPTIONAL (its values are never read; IDX_I volume content
# is UNVERIFIED — gate G-J); every other missing column stays vendor drift.
# ─────────────────────────────────────────────────────────────────────────────


def test_absent_volume_with_drop_volume_parses() -> None:
    cols = _columns()
    del cols["volume"]  # an IDX_I payload with NO volume array
    bars = parse_candle_columns(cols, drop_volume=True)
    assert len(bars) == 1
    assert bars[0].volume == 0  # dropped → 0 (an inherent index market fact)


def test_absent_volume_without_drop_volume_still_rejects() -> None:
    cols = _columns()
    del cols["volume"]
    with pytest.raises(DhanVendorDriftError):
        parse_candle_columns(cols, drop_volume=False)


def test_absent_close_still_rejects_even_with_drop_volume() -> None:
    cols = _columns()
    del cols["close"]
    with pytest.raises(DhanVendorDriftError):
        parse_candle_columns(cols, drop_volume=True)


def test_present_volume_with_drop_volume_still_length_checked() -> None:
    # When PRESENT the volume array still rides the length check (an unequal
    # volume array is vendor drift even though its values are never read).
    cols = _columns(volume=[0, 0])
    with pytest.raises(DhanVendorDriftError):
        parse_candle_columns(cols, drop_volume=True)


# ─────────────────────────────────────────────────────────────────────────────
# parse_daily_candle_columns — the NATIVE-daily midnight-IST mirror
# (DEC-DHAN-NATIVE-DAILY-001, implementing DEC-SCOPE-MINIMAL-CORE-001)
# ─────────────────────────────────────────────────────────────────────────────


def test_daily_parse_restamps_to_midnight_ist_stored_utc() -> None:
    # The raw epoch (09:15 IST on 2022-06-01) normalizes to the day's IST date
    # and is re-stamped to midnight-IST stored UTC — 2022-05-31 18:30 UTC, the
    # exact spot_daily_resample convention (00:00 IST ⇔ 18:30 UTC prior day).
    bars = parse_daily_candle_columns(_columns())
    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2022, 5, 31, 18, 30, tzinfo=UTC)
    assert bars[0].timestamp.tzinfo == UTC
    # Prices ride parse_candle_columns verbatim.
    assert bars[0].close == Decimal("100.5")
    assert bars[0].volume == 0  # drop_volume=True ALWAYS on the daily mirror


def test_daily_parse_keep_first_on_duplicate_ist_date() -> None:
    # TWO raw bars normalizing to the SAME IST date keep the FIRST occurrence.
    second_epoch = int(datetime(2022, 6, 1, 9, 0, tzinfo=UTC).timestamp())
    cols = _columns(
        open=[100.0, 200.0],
        high=[101.0, 201.0],
        low=[99.0, 199.0],
        close=[100.5, 200.5],
        volume=[0, 0],
        timestamp=[_EPOCH_0915_IST, second_epoch],
    )
    bars = parse_daily_candle_columns(cols)
    assert len(bars) == 1
    assert bars[0].close == Decimal("100.5")  # the FIRST physical copy wins


def test_daily_parse_distinct_dates_preserve_order() -> None:
    day2 = int(datetime(2022, 6, 2, 3, 45, tzinfo=UTC).timestamp())
    cols = _columns(
        open=[100.0, 110.0],
        high=[101.0, 111.0],
        low=[99.0, 109.0],
        close=[100.5, 110.5],
        volume=[0, 0],
        timestamp=[_EPOCH_0915_IST, day2],
    )
    bars = parse_daily_candle_columns(cols)
    assert [b.timestamp for b in bars] == [
        datetime(2022, 5, 31, 18, 30, tzinfo=UTC),
        datetime(2022, 6, 1, 18, 30, tzinfo=UTC),
    ]


def test_daily_parse_empty_payload_is_honest_no_data() -> None:
    assert parse_daily_candle_columns({}) == []


def test_daily_parse_absent_volume_tolerated() -> None:
    # The daily mirror always rides drop_volume=True — an absent volume array
    # (the IDX daily shape) parses.
    cols = _columns()
    del cols["volume"]
    assert len(parse_daily_candle_columns(cols)) == 1


def test_daily_parse_vendor_drift_propagates() -> None:
    cols = _columns()
    del cols["close"]
    with pytest.raises(DhanVendorDriftError):
        parse_daily_candle_columns(cols)
