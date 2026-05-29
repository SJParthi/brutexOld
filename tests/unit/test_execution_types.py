"""Unit tests for :mod:`brutex.execution.types` dataclasses.

Covers every construction-time validation branch plus the derived
properties (``is_flat``, ``direction``, ``total_fills``) so the
pure-dataclass surface is at 100% coverage in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.execution.types import (
    BacktestResult,
    EquityPoint,
    Fill,
    FillCosts,
    InstrumentMeta,
    Position,
)
from brutex.models import InstrumentType, Segment
from brutex.strategies.types import Side

# ────────────────────────────────────────────────────────────────────────
# InstrumentMeta validation
# ────────────────────────────────────────────────────────────────────────


class TestInstrumentMetaValidation:
    def test_zero_lot_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="lot_size must be positive"):
            InstrumentMeta(
                symbol="X",
                segment=Segment.FNO,
                instrument_type=InstrumentType.FUT,
                lot_size=0,
            )

    def test_negative_lot_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="lot_size must be positive"):
            InstrumentMeta(
                symbol="X",
                segment=Segment.FNO,
                instrument_type=InstrumentType.FUT,
                lot_size=-5,
            )

    def test_zero_freeze_qty_rejected(self) -> None:
        with pytest.raises(ValueError, match="freeze_quantity"):
            InstrumentMeta(
                symbol="X",
                segment=Segment.FNO,
                instrument_type=InstrumentType.FUT,
                lot_size=25,
                freeze_quantity=0,
            )

    def test_none_freeze_qty_allowed(self) -> None:
        meta = InstrumentMeta(
            symbol="X",
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
            lot_size=1,
            freeze_quantity=None,
        )
        assert meta.freeze_quantity is None

    def test_index_zero_lot_size_allowed(self) -> None:
        # DEC-INDEX-PARSE-FIX-001 — indices are reference-only (never sized),
        # so the Groww master's blank lot_size (parsed as 0) is tolerated here.
        meta = InstrumentMeta(
            symbol="NSE-NIFTY",
            segment=Segment.CASH,
            instrument_type=InstrumentType.IDX,
            lot_size=0,
        )
        assert meta.lot_size == 0

    def test_index_negative_lot_size_rejected(self) -> None:
        # Tolerance is zero-or-positive only — a NEGATIVE lot is still invalid.
        with pytest.raises(ValueError, match="lot_size must not be negative"):
            InstrumentMeta(
                symbol="NSE-NIFTY",
                segment=Segment.CASH,
                instrument_type=InstrumentType.IDX,
                lot_size=-1,
            )


# ────────────────────────────────────────────────────────────────────────
# Fill validation + derived properties
# ────────────────────────────────────────────────────────────────────────


class TestFillValidation:
    _TS = datetime(2024, 1, 2, 9, 15, tzinfo=UTC)

    def test_zero_qty_rejected(self) -> None:
        with pytest.raises(ValueError, match="qty must be positive"):
            Fill(
                symbol="X",
                side=Side.BUY,
                qty=0,
                requested_price=Decimal("100"),
                fill_price=Decimal("100"),
                timestamp=self._TS,
                costs=FillCosts(),
            )

    def test_hold_side_rejected(self) -> None:
        with pytest.raises(ValueError, match="side must be BUY or SELL"):
            Fill(
                symbol="X",
                side=Side.HOLD,
                qty=1,
                requested_price=Decimal("100"),
                fill_price=Decimal("100"),
                timestamp=self._TS,
                costs=FillCosts(),
            )

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Fill(
                symbol="X",
                side=Side.BUY,
                qty=1,
                requested_price=Decimal("100"),
                fill_price=Decimal("100"),
                timestamp=datetime(2024, 1, 2, 9, 15),  # naive
                costs=FillCosts(),
            )

    def test_notional_is_qty_times_fill_price(self) -> None:
        fill = Fill(
            symbol="X",
            side=Side.BUY,
            qty=5,
            requested_price=Decimal("100"),
            fill_price=Decimal("102"),
            timestamp=self._TS,
            costs=FillCosts(),
        )
        assert fill.notional == Decimal("510")

    def test_buy_cash_delta_is_negative(self) -> None:
        fill = Fill(
            symbol="X",
            side=Side.BUY,
            qty=1,
            requested_price=Decimal("100"),
            fill_price=Decimal("100"),
            timestamp=self._TS,
            costs=FillCosts(brokerage=Decimal("20")),
        )
        assert fill.cash_delta == Decimal("-120")

    def test_sell_cash_delta_is_positive_minus_costs(self) -> None:
        fill = Fill(
            symbol="X",
            side=Side.SELL,
            qty=1,
            requested_price=Decimal("100"),
            fill_price=Decimal("100"),
            timestamp=self._TS,
            costs=FillCosts(brokerage=Decimal("20")),
        )
        assert fill.cash_delta == Decimal("80")


# ────────────────────────────────────────────────────────────────────────
# Position derived properties
# ────────────────────────────────────────────────────────────────────────


class TestPositionProperties:
    def test_flat_position_is_flat(self) -> None:
        pos = Position(symbol="X", qty=0)
        assert pos.is_flat is True
        assert pos.direction == 0

    def test_long_position_direction_positive(self) -> None:
        pos = Position(symbol="X", qty=5)
        assert pos.is_flat is False
        assert pos.direction == 1

    def test_short_position_direction_negative(self) -> None:
        pos = Position(symbol="X", qty=-5)
        assert pos.is_flat is False
        assert pos.direction == -1


# ────────────────────────────────────────────────────────────────────────
# BacktestResult total_fills derived field
# ────────────────────────────────────────────────────────────────────────


class TestBacktestResultTotalFills:
    def test_empty_result_reports_zero_fills(self) -> None:
        result = BacktestResult(
            fills=(),
            equity_curve=(),
            final_positions={},
            final_cash=Decimal("10000"),
            final_equity=Decimal("10000"),
            total_realized_pnl=Decimal("0"),
            total_costs=Decimal("0"),
        )
        assert result.total_fills == 0

    def test_total_fills_matches_tuple_length(self) -> None:
        fills = tuple(
            Fill(
                symbol="X",
                side=Side.BUY,
                qty=1,
                requested_price=Decimal("100"),
                fill_price=Decimal("100"),
                timestamp=datetime(2024, 1, 2, 9, 15 + i, tzinfo=UTC),
                costs=FillCosts(),
            )
            for i in range(3)
        )
        result = BacktestResult(
            fills=fills,
            equity_curve=(),
            final_positions={},
            final_cash=Decimal("10000"),
            final_equity=Decimal("10000"),
            total_realized_pnl=Decimal("0"),
            total_costs=Decimal("0"),
        )
        assert result.total_fills == 3


# ────────────────────────────────────────────────────────────────────────
# EquityPoint basic construction
# ────────────────────────────────────────────────────────────────────────


class TestEquityPoint:
    def test_construction_preserves_fields(self) -> None:
        point = EquityPoint(
            timestamp=datetime(2024, 1, 2, 9, 15, tzinfo=UTC),
            cash=Decimal("10000"),
            positions_value=Decimal("500"),
            total_equity=Decimal("10500"),
        )
        assert point.cash == Decimal("10000")
        assert point.positions_value == Decimal("500")
        assert point.total_equity == Decimal("10500")
