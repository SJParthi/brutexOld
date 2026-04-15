"""Unit tests for :mod:`brutex.strategies.types`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.strategies.types import Side, Signal, StrategyState


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
