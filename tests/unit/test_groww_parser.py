"""Unit tests for ``brutex.adapters.groww.parser`` (DEC-GROWW-DEPS-REINSTATE-001)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.adapters.groww import parser
from brutex.data.candle_fields import CandleParseError


# ── parse_timestamp_ist ─────────────────────────────────────────────────────
def test_iso_t_separator_localizes_ist_to_utc() -> None:
    # 10:30 IST == 05:00 UTC (fixed +05:30).
    assert parser.parse_timestamp_ist("2025-09-24T10:30:00") == datetime(
        2025, 9, 24, 5, 0, tzinfo=UTC
    )


def test_iso_space_separator() -> None:
    assert parser.parse_timestamp_ist("2025-09-24 10:30:00") == datetime(
        2025, 9, 24, 5, 0, tzinfo=UTC
    )


def test_epoch_int_is_utc() -> None:
    assert parser.parse_timestamp_ist(0) == datetime(1970, 1, 1, 0, 0, tzinfo=UTC)


def test_epoch_float_is_utc() -> None:
    ts = parser.parse_timestamp_ist(1_700_000_000.0)
    assert ts.tzinfo == UTC


def test_bool_timestamp_rejected() -> None:
    with pytest.raises(CandleParseError, match="must not be bool"):
        parser.parse_timestamp_ist(True)


def test_unparseable_string_rejected() -> None:
    with pytest.raises(CandleParseError, match="unparseable Groww timestamp"):
        parser.parse_timestamp_ist("not-a-date")


def test_non_finite_epoch_rejected() -> None:
    with pytest.raises(CandleParseError, match="non-finite"):
        parser.parse_timestamp_ist(float("inf"))


def test_out_of_range_epoch_rejected() -> None:
    with pytest.raises(CandleParseError, match="out-of-range"):
        parser.parse_timestamp_ist(1e30)


def test_wrong_type_timestamp_rejected() -> None:
    with pytest.raises(CandleParseError, match="must be a string or epoch"):
        parser.parse_timestamp_ist(["2025-09-24"])


# ── parse_candle_rows ───────────────────────────────────────────────────────
def _row(ts: str = "2025-09-24T10:30:00") -> list[object]:
    return [ts, 100.0, 110.0, 90.0, 105.0, 1000, None]


def test_parses_normal_rows() -> None:
    bars = parser.parse_candle_rows({"candles": [_row(), _row("2025-09-24T10:31:00")]})
    assert len(bars) == 2
    assert bars[0].open == Decimal("100.0")
    assert bars[0].high == Decimal("110.0")
    assert bars[0].volume == 1000


def test_no_trade_row_is_skipped() -> None:
    bars = parser.parse_candle_rows(
        {"candles": [_row(), ["2025-09-24T10:31:00", 0, 0, 0, 0, 0, None]]}
    )
    assert len(bars) == 1  # the empty bar is gracefully dropped


def test_drop_volume_zeros_volume() -> None:
    bars = parser.parse_candle_rows({"candles": [_row()]}, drop_volume=True)
    assert bars[0].volume == 0


def test_fno_open_interest_preserved() -> None:
    bars = parser.parse_candle_rows({"candles": [["2025-09-24T10:30:00", 1, 2, 1, 2, 5, 42]]})
    assert bars[0].open_interest == 42


def test_missing_candles_key_raises() -> None:
    with pytest.raises(CandleParseError, match="missing required 'candles'"):
        parser.parse_candle_rows({})


def test_non_array_candles_raises() -> None:
    with pytest.raises(CandleParseError, match="must be an array"):
        parser.parse_candle_rows({"candles": "nope"})


def test_wrong_arity_row_raises() -> None:
    with pytest.raises(CandleParseError, match="7-element array"):
        parser.parse_candle_rows({"candles": [[1, 2, 3]]})


def test_non_sequence_row_raises() -> None:
    with pytest.raises(CandleParseError, match="7-element array"):
        parser.parse_candle_rows({"candles": [42]})


def test_malformed_nonempty_row_raises() -> None:
    # Non-empty (has prices) but an unparseable price → CandleParseError (not skipped).
    with pytest.raises(CandleParseError):
        parser.parse_candle_rows({"candles": [["2025-09-24T10:30:00", "x", 2, 1, 2, 5, None]]})
