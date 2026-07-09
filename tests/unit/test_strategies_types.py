"""Unit tests for :mod:`brutex.strategies.types`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.strategies.types import (
    DEFAULT_PRICE_SOURCE_CELL,
    PriceSource,
    PriceSourceCell,
    Side,
    Signal,
    StrategyState,
    parse_price_source_cells,
)


class TestSide:
    def test_values(self) -> None:
        assert Side.BUY == "BUY"
        assert Side.SELL == "SELL"
        assert Side.HOLD == "HOLD"

    def test_membership(self) -> None:
        assert Side("BUY") is Side.BUY


class TestSignal:
    def test_happy_path(self) -> None:
        sig = Signal(
            symbol="NSE-A",
            side=Side.BUY,
            timestamp=datetime(2024, 1, 1, 9, 15, tzinfo=UTC),
            strength=0.5,
            reason="test",
        )
        assert sig.symbol == "NSE-A"
        assert sig.side == Side.BUY
        assert sig.strength == 0.5

    def test_defaults(self) -> None:
        sig = Signal(
            symbol="NSE-A",
            side=Side.SELL,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )
        assert sig.strength == 1.0
        assert sig.reason == ""

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Signal(
                symbol="NSE-A",
                side=Side.BUY,
                timestamp=datetime(2024, 1, 1, 9, 15),
            )

    def test_strength_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
            Signal(
                symbol="NSE-A",
                side=Side.BUY,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                strength=1.5,
            )
        with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
            Signal(
                symbol="NSE-A",
                side=Side.BUY,
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                strength=-0.1,
            )

    def test_frozen(self) -> None:
        sig = Signal(
            symbol="NSE-A",
            side=Side.BUY,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(AttributeError):
            sig.side = Side.SELL  # type: ignore[misc]


class TestStrategyState:
    def test_defaults_are_empty(self) -> None:
        state = StrategyState()
        assert state.last_prices == {}
        assert state.bar_count == 0
        assert state.scratch == {}

    def test_reset_clears_everything(self) -> None:
        state = StrategyState()
        state.last_prices["NSE-A"] = Decimal("100")
        state.bar_count = 42
        state.scratch["foo"] = 1
        state.reset()
        assert state.last_prices == {}
        assert state.bar_count == 0
        assert state.scratch == {}

    def test_fresh_instances_are_independent(self) -> None:
        """Default-factory fields must not share state between instances."""
        s1 = StrategyState()
        s2 = StrategyState()
        s1.last_prices["X"] = Decimal("1")
        s1.scratch["y"] = 1
        assert s2.last_prices == {}
        assert s2.scratch == {}


class TestPriceSource:
    """DEC-NIFTY-PRICE-SOURCE-AXIS-001 — the VWAP/EMA price-source enum + cell."""

    def test_exactly_five_members(self) -> None:
        assert {m.value for m in PriceSource} == {"open", "high", "low", "close", "hlc3"}

    def test_hl2_and_ohlc4_deliberately_absent(self) -> None:
        # Override 4 — the rounding-tie clones are NOT in the enum.
        values = {m.value for m in PriceSource}
        assert "hl2" not in values
        assert "ohlc4" not in values

    def test_default_cell_is_hlc3_close(self) -> None:
        assert PriceSourceCell() == DEFAULT_PRICE_SOURCE_CELL
        assert PriceSourceCell().vwap_source is PriceSource.HLC3
        assert PriceSourceCell().ema_source is PriceSource.CLOSE

    def test_cell_is_hashable(self) -> None:
        # Grid-axis product + dict.fromkeys de-dup require hashability.
        assert len({PriceSourceCell(), PriceSourceCell()}) == 1

    def test_cell_is_frozen(self) -> None:
        cell = PriceSourceCell()
        with pytest.raises((AttributeError, TypeError)):
            cell.vwap_source = PriceSource.OPEN  # type: ignore[misc]


class TestParsePriceSourceCells:
    def test_empty_yields_baseline(self) -> None:
        assert parse_price_source_cells("") == [DEFAULT_PRICE_SOURCE_CELL]

    def test_all_blank_yields_baseline(self) -> None:
        assert parse_price_source_cells("  ,  ") == [DEFAULT_PRICE_SOURCE_CELL]

    def test_full_ofat_lattice(self) -> None:
        cells = parse_price_source_cells("hlc3:close,open:close,close:close,hlc3:open,hlc3:hlc3")
        assert cells == [
            PriceSourceCell(PriceSource.HLC3, PriceSource.CLOSE),
            PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE),
            PriceSourceCell(PriceSource.CLOSE, PriceSource.CLOSE),
            PriceSourceCell(PriceSource.HLC3, PriceSource.OPEN),
            PriceSourceCell(PriceSource.HLC3, PriceSource.HLC3),
        ]

    def test_whitespace_and_dedup(self) -> None:
        cells = parse_price_source_cells(" open:close , open:close ,hlc3:hlc3")
        assert cells == [
            PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE),
            PriceSourceCell(PriceSource.HLC3, PriceSource.HLC3),
        ]

    @pytest.mark.parametrize("bad", ["hlc3", "hlc3:close:open", "hlc3close"])
    def test_malformed_separator_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="exactly one ':'"):
            parse_price_source_cells(bad)

    @pytest.mark.parametrize("bad", ["hl2:close", "open:ohlc4", "junk:close"])
    def test_unknown_token_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_price_source_cells(bad)
