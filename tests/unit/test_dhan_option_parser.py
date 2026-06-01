"""Tests for `brutex.adapters.dhan.option_parser` — rollingoption → OptionBar.

100% line+branch: ce/pe side dispatch (both, null side, absent side,
non-mapping side), required/optional column handling, length-mismatch guard,
empty-bar skip, oi→open_interest mapping, and per-bar strike/spot/iv
(present / absent-cell / absent-column).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.adapters.dhan.option_parser import parse_rolling_option_sides
from brutex.data.candle_fields import CandleParseError
from brutex.data.models import OptionBar

# 1609729200 = 2021-01-04 03:00:00 UTC = 08:30 IST (epoch seconds).
_TS_A = 1609729200
_TS_B = 1609729260  # +60s


def _ce_side() -> dict[str, object]:
    """A CE side with one valid bar + one no-trade (all-zero) bar to skip."""
    return {
        "timestamp": [_TS_A, _TS_B],
        "open": [100, 0],
        "high": [110, 0],
        "low": [90, 0],
        "close": [105, 0],
        "volume": [250, 0],
        "oi": [1200, 0],
        "strike": [17500, 17500],
        "spot": [17480.5, 17480.5],
        "iv": [0.1523, 0.1523],
    }


class TestParseRollingOption:
    def test_single_side_full_fields(self) -> None:
        bars = parse_rolling_option_sides({"ce": _ce_side(), "pe": None})
        # Second bar is a no-trade (all-zero OHLC) row → skipped.
        assert len(bars) == 1
        bar = bars[0]
        assert isinstance(bar, OptionBar)
        assert bar.side == "CE"
        assert bar.timestamp == datetime(2021, 1, 4, 3, 0, tzinfo=UTC)
        assert bar.open == Decimal("100")
        assert bar.close == Decimal("105")
        assert bar.volume == 250
        assert bar.open_interest == 1200
        assert bar.strike == Decimal("17500")
        assert bar.spot == Decimal("17480.5")
        assert bar.iv == Decimal("0.1523")

    def test_both_sides_ce_then_pe(self) -> None:
        pe = _ce_side()
        bars = parse_rolling_option_sides({"ce": _ce_side(), "pe": pe})
        assert [b.side for b in bars] == ["CE", "PE"]

    def test_absent_pe_key_is_skipped(self) -> None:
        bars = parse_rolling_option_sides({"ce": _ce_side()})
        assert [b.side for b in bars] == ["CE"]

    def test_null_side_yields_no_bars(self) -> None:
        assert parse_rolling_option_sides({"ce": None, "pe": None}) == []

    def test_optional_columns_absent_become_none_or_zero(self) -> None:
        side = {
            "timestamp": [_TS_A],
            "open": [100],
            "high": [110],
            "low": [90],
            "close": [105],
            # no volume / oi / strike / spot / iv columns at all
        }
        (bar,) = parse_rolling_option_sides({"ce": side})
        assert bar.volume == 0  # absent volume → 0 (never quarantined)
        assert bar.open_interest is None
        assert bar.strike is None
        assert bar.spot is None
        assert bar.iv is None

    def test_per_bar_absent_extra_cells_become_none(self) -> None:
        side = {
            "timestamp": [_TS_A],
            "open": [100],
            "high": [110],
            "low": [90],
            "close": [105],
            "strike": [""],  # empty cell → None
            "spot": [None],  # null cell → None
            "iv": ["  "],  # whitespace cell → None
        }
        (bar,) = parse_rolling_option_sides({"ce": side})
        assert bar.strike is None
        assert bar.spot is None
        assert bar.iv is None


class TestParseRollingOptionErrors:
    def test_non_mapping_side_raises(self) -> None:
        with pytest.raises(CandleParseError, match="side 'ce' must be an object"):
            parse_rolling_option_sides({"ce": [1, 2, 3]})

    def test_missing_required_column_raises(self) -> None:
        side = {"open": [1], "high": [1], "low": [1], "close": [1]}  # no timestamp
        with pytest.raises(
            CandleParseError, match="missing required rollingoption column 'timestamp'"
        ):
            parse_rolling_option_sides({"ce": side})

    def test_non_array_column_raises(self) -> None:
        side = {
            "timestamp": _TS_A,  # scalar, not an array
            "open": [1],
            "high": [1],
            "low": [1],
            "close": [1],
        }
        with pytest.raises(CandleParseError, match="column 'timestamp' must be an array"):
            parse_rolling_option_sides({"ce": side})

    def test_length_mismatch_raises(self) -> None:
        side = {
            "timestamp": [_TS_A, _TS_B],
            "open": [100],  # length 1 != 2
            "high": [110, 110],
            "low": [90, 90],
            "close": [105, 105],
        }
        with pytest.raises(CandleParseError, match="column 'open' length 1 != timestamp length 2"):
            parse_rolling_option_sides({"ce": side})

    def test_non_array_optional_column_raises(self) -> None:
        side = {
            "timestamp": [_TS_A],
            "open": [100],
            "high": [110],
            "low": [90],
            "close": [105],
            "strike": 17500,  # scalar optional column → still must be an array
        }
        with pytest.raises(CandleParseError, match="column 'strike' must be an array"):
            parse_rolling_option_sides({"ce": side})
