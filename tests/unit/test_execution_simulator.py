"""Unit tests for :class:`brutex.execution.simulator.ExecutionSimulator`.

Pure dataclass + state-machine tests \u2014 no feed, no catalog, no
``polars`` / ``duckdb`` / ``lmdb``. Covers lot-size + freeze
validation, slippage direction, position open/extend/close/flip
transitions, and equity snapshotting.

Per DEC-P0-017 (PR #39): this is a WHITEBOX test that probes the
simulator's private cost-model knobs (``_slippage_bps``,
``_default_qty``) to verify cost-direction sign + lot-size rounding
independent of the public construction surface. The file-level
``reportPrivateUsage=none`` directive below is intentional.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.execution.errors import (
    FreezeQuantityExceededError,
    InsufficientFundsError,
    InvalidLotSizeError,
    InvalidPriceError,
)
from brutex.execution.simulator import ExecutionSimulator
from brutex.execution.types import InstrumentMeta
from brutex.models import InstrumentType, Segment
from brutex.strategies.types import Side, Signal

# The dormant IndianCostModel is deprecated (R2-OC-8): constructing it (directly
# or via ExecutionSimulator's default cost model) emits a DeprecationWarning.
# This legacy suite exercises the FROZEN dormant stack, so the warning is
# expected here — ignore it (message-scoped) instead of failing under the
# repo-wide ``filterwarnings = error`` gate.
pytestmark = pytest.mark.filterwarnings("ignore:IndianCostModel:DeprecationWarning")

# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


def _bar(close: float, *, minute: int = 15) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, 2, 9, minute, 0, tzinfo=UTC),
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=100,
        open_interest=0,
    )


def _signal(
    side: Side,
    *,
    symbol: str = "NSE-NIFTY",
    minute: int = 15,
) -> Signal:
    return Signal(
        symbol=symbol,
        side=side,
        timestamp=datetime(2024, 1, 2, 9, minute, 0, tzinfo=UTC),
    )


@pytest.fixture
def idx_meta() -> InstrumentMeta:
    return InstrumentMeta(
        symbol="NSE-NIFTY",
        segment=Segment.CASH,
        instrument_type=InstrumentType.IDX,
        lot_size=1,
        freeze_quantity=10_000,
    )


@pytest.fixture
def fno_meta() -> InstrumentMeta:
    return InstrumentMeta(
        symbol="NSE-NIFTY-FUT",
        segment=Segment.FNO,
        instrument_type=InstrumentType.FUT,
        lot_size=25,
        freeze_quantity=1_800,
    )


def _make_sim(
    *instruments: InstrumentMeta,
    cash: Decimal = Decimal("1_000_000"),
    default_qty: int = 1,
    slippage_bps: Decimal = Decimal("5"),
) -> ExecutionSimulator:
    return ExecutionSimulator(
        initial_cash=cash,
        instruments={m.symbol: m for m in instruments},
        slippage_bps=slippage_bps,
        default_qty=default_qty,
    )


# ────────────────────────────────────────────────────────────────────────
# Construction
# ────────────────────────────────────────────────────────────────────────


class TestExecutionSimulatorConstruction:
    def test_zero_cash_rejected(self, idx_meta: InstrumentMeta) -> None:
        with pytest.raises(ValueError, match="initial_cash"):
            ExecutionSimulator(
                initial_cash=Decimal("0"),
                instruments={idx_meta.symbol: idx_meta},
            )

    def test_negative_slippage_rejected(self, idx_meta: InstrumentMeta) -> None:
        with pytest.raises(ValueError, match="slippage_bps"):
            ExecutionSimulator(
                initial_cash=Decimal("1000"),
                instruments={idx_meta.symbol: idx_meta},
                slippage_bps=Decimal("-1"),
            )

    def test_non_positive_default_qty_rejected(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        with pytest.raises(ValueError, match="default_qty"):
            ExecutionSimulator(
                initial_cash=Decimal("1000"),
                instruments={idx_meta.symbol: idx_meta},
                default_qty=0,
            )

    def test_starts_with_empty_book(self, idx_meta: InstrumentMeta) -> None:
        sim = _make_sim(idx_meta)
        assert sim.cash == Decimal("1_000_000")
        assert sim.fills == ()
        assert sim.equity_curve == ()
        assert sim.positions == {}


# ────────────────────────────────────────────────────────────────────────
# Slippage
# ────────────────────────────────────────────────────────────────────────


class TestSlippage:
    def test_buy_pays_up(self, idx_meta: InstrumentMeta) -> None:
        sim = _make_sim(idx_meta, slippage_bps=Decimal("10"))
        fill = sim.execute(_signal(Side.BUY), _bar(100))
        assert fill is not None
        # 10 bps = 0.1%, BUY fills 0.1% above mid → 100.10
        assert fill.fill_price == Decimal("100.1000")

    def test_sell_takes_a_cut(self, idx_meta: InstrumentMeta) -> None:
        sim = _make_sim(idx_meta, slippage_bps=Decimal("10"))
        # Need an open long to sell against; first buy at zero-slippage env
        sim._slippage_bps = Decimal("0")  # type: ignore[attr-defined]
        sim.execute(_signal(Side.BUY), _bar(100))
        sim._slippage_bps = Decimal("10")  # type: ignore[attr-defined]
        fill = sim.execute(_signal(Side.SELL, minute=16), _bar(100, minute=16))
        assert fill is not None
        # SELL 10 bps below → 99.9
        assert fill.fill_price == Decimal("99.9000")

    def test_hold_returns_none(self, idx_meta: InstrumentMeta) -> None:
        sim = _make_sim(idx_meta)
        assert sim.execute(_signal(Side.HOLD), _bar(100)) is None
        assert sim.fills == ()


# ────────────────────────────────────────────────────────────────────────
# Lot size / freeze validation
# ────────────────────────────────────────────────────────────────────────


class TestLotSizeValidation:
    def test_qty_not_multiple_of_lot_rejected(
        self,
        fno_meta: InstrumentMeta,
    ) -> None:
        # 25-lot instrument, order qty 10 \u2192 InvalidLotSizeError.
        sim = _make_sim(fno_meta, default_qty=10)
        with pytest.raises(InvalidLotSizeError, match="multiple"):
            sim.execute(_signal(Side.BUY, symbol="NSE-NIFTY-FUT"), _bar(100))

    def test_qty_multiple_of_lot_accepted(self, fno_meta: InstrumentMeta) -> None:
        sim = _make_sim(
            fno_meta,
            default_qty=50,  # 2 lots
            cash=Decimal("1_000_000_000"),
        )
        fill = sim.execute(
            _signal(Side.BUY, symbol="NSE-NIFTY-FUT"),
            _bar(100),
        )
        assert fill is not None
        assert fill.qty == 50

    def test_freeze_qty_exceeded_rejected(self, fno_meta: InstrumentMeta) -> None:
        # freeze_qty = 1800, lot_size = 25 \u2192 2000 clears lot_size but
        # exceeds freeze.
        sim = _make_sim(
            fno_meta,
            default_qty=2000,
            cash=Decimal("1_000_000_000"),
        )
        with pytest.raises(FreezeQuantityExceededError, match="freeze"):
            sim.execute(_signal(Side.BUY, symbol="NSE-NIFTY-FUT"), _bar(100))

    def test_unknown_symbol_rejected(self, idx_meta: InstrumentMeta) -> None:
        sim = _make_sim(idx_meta)
        with pytest.raises(InvalidLotSizeError, match="unknown symbol"):
            sim.execute(_signal(Side.BUY, symbol="OTHER"), _bar(100))


# ────────────────────────────────────────────────────────────────────────
# Cash / insufficient funds
# ────────────────────────────────────────────────────────────────────────


class TestFundsCheck:
    def test_underfunded_buy_rejected(self, idx_meta: InstrumentMeta) -> None:
        sim = _make_sim(
            idx_meta,
            cash=Decimal("50"),  # too low for even one share @ 100
            default_qty=1,
        )
        with pytest.raises(InsufficientFundsError, match="required"):
            sim.execute(_signal(Side.BUY), _bar(100))

    def test_funded_buy_deducts_notional_plus_costs(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        sim = _make_sim(
            idx_meta,
            cash=Decimal("1000"),
            default_qty=1,
            slippage_bps=Decimal("0"),
        )
        fill = sim.execute(_signal(Side.BUY), _bar(100))
        assert fill is not None
        # Cash starts at 1000, notional 100, costs > 0 → cash < 900.
        assert sim.cash == Decimal("1000") + fill.cash_delta
        assert sim.cash < Decimal("900")


# ────────────────────────────────────────────────────────────────────────
# Position state transitions
# ────────────────────────────────────────────────────────────────────────


class TestPositionTransitions:
    def test_fresh_buy_opens_long(self, idx_meta: InstrumentMeta) -> None:
        sim = _make_sim(idx_meta, slippage_bps=Decimal("0"))
        sim.execute(_signal(Side.BUY), _bar(100))
        pos = sim.positions["NSE-NIFTY"]
        assert pos.qty == 1
        assert pos.avg_price == Decimal("100")
        assert pos.realized_pnl == Decimal("0")

    def test_second_buy_weighted_averages_avg_price(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        sim = _make_sim(idx_meta, slippage_bps=Decimal("0"), default_qty=1)
        sim.execute(_signal(Side.BUY, minute=15), _bar(100, minute=15))
        sim.execute(_signal(Side.BUY, minute=16), _bar(120, minute=16))
        pos = sim.positions["NSE-NIFTY"]
        assert pos.qty == 2
        # Average of 100 and 120 = 110.
        assert pos.avg_price == Decimal("110")

    def test_sell_closes_long_and_realizes_pnl(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        sim = _make_sim(
            idx_meta,
            slippage_bps=Decimal("0"),
            cash=Decimal("10000"),
        )
        sim.execute(_signal(Side.BUY, minute=15), _bar(100, minute=15))
        sim.execute(_signal(Side.SELL, minute=16), _bar(110, minute=16))
        pos = sim.positions["NSE-NIFTY"]
        assert pos.qty == 0
        assert pos.avg_price == Decimal("0")
        assert pos.realized_pnl == Decimal("10")  # (110 - 100) * 1

    def test_sell_beyond_flat_opens_short(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        """SELL into a flat position is a naked short \u2014 qty goes negative."""
        sim = _make_sim(idx_meta, slippage_bps=Decimal("0"))
        sim.execute(_signal(Side.SELL), _bar(100))
        pos = sim.positions["NSE-NIFTY"]
        assert pos.qty == -1
        assert pos.avg_price == Decimal("100")

    def test_buy_flips_short_to_long(self, idx_meta: InstrumentMeta) -> None:
        """BUY qty > short size closes the short and opens a long with the remainder."""
        sim = _make_sim(idx_meta, slippage_bps=Decimal("0"), default_qty=1)
        # Open short of 1 @ 100
        sim.execute(_signal(Side.SELL, minute=15), _bar(100, minute=15))
        assert sim.positions["NSE-NIFTY"].qty == -1
        # Bump default_qty to 3 to get a 3-unit BUY that closes + opens long.
        sim._default_qty = 3  # type: ignore[attr-defined]
        sim.execute(_signal(Side.BUY, minute=16), _bar(95, minute=16))
        pos = sim.positions["NSE-NIFTY"]
        # Close short of 1 @ 100 against fill 95 \u2192 realized +5 (short profits on drop).
        # Remainder = 2 opens fresh long @ 95.
        assert pos.qty == 2
        assert pos.avg_price == Decimal("95")
        assert pos.realized_pnl == Decimal("5")

    def test_partial_close_leaves_avg_price_unchanged(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        sim = _make_sim(idx_meta, slippage_bps=Decimal("0"), default_qty=3)
        sim.execute(_signal(Side.BUY, minute=15), _bar(100, minute=15))
        sim._default_qty = 1  # type: ignore[attr-defined]
        sim.execute(_signal(Side.SELL, minute=16), _bar(110, minute=16))
        pos = sim.positions["NSE-NIFTY"]
        assert pos.qty == 2
        assert pos.avg_price == Decimal("100")  # unchanged
        assert pos.realized_pnl == Decimal("10")  # (110-100) * 1


# ────────────────────────────────────────────────────────────────────────
# Mark-to-market + equity curve
# ────────────────────────────────────────────────────────────────────────


class TestMarkToMarket:
    def test_flat_position_equity_equals_cash(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        sim = _make_sim(idx_meta, cash=Decimal("1_000_000"))
        point = sim.mark_to_market(
            symbol="NSE-NIFTY",
            price=Decimal("100"),
            timestamp=datetime(2024, 1, 2, 9, 15, tzinfo=UTC),
        )
        assert point.cash == Decimal("1_000_000")
        assert point.positions_value == Decimal("0")
        assert point.total_equity == Decimal("1_000_000")

    def test_long_position_unrealized_updates(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        sim = _make_sim(
            idx_meta,
            slippage_bps=Decimal("0"),
            cash=Decimal("10000"),
        )
        sim.execute(_signal(Side.BUY), _bar(100))
        sim.mark_to_market(
            symbol="NSE-NIFTY",
            price=Decimal("110"),
            timestamp=datetime(2024, 1, 2, 9, 16, tzinfo=UTC),
        )
        pos = sim.positions["NSE-NIFTY"]
        assert pos.unrealized_pnl == Decimal("10")
        assert pos.last_mark_price == Decimal("110")

    def test_equity_curve_has_one_point_per_mark(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        sim = _make_sim(idx_meta, cash=Decimal("10000"))
        for i in range(5):
            sim.mark_to_market(
                symbol="NSE-NIFTY",
                price=Decimal("100"),
                timestamp=datetime(2024, 1, 2, 9, 15 + i, tzinfo=UTC),
            )
        assert len(sim.equity_curve) == 5

    def test_equity_curve_reflects_position_value(
        self,
        idx_meta: InstrumentMeta,
    ) -> None:
        sim = _make_sim(
            idx_meta,
            slippage_bps=Decimal("0"),
            cash=Decimal("10000"),
        )
        sim.execute(_signal(Side.BUY), _bar(100))
        # After fill, cash dropped by ~100 + costs. Mark at 110 \u2014
        # equity = cash + 1 share * 110.
        sim.mark_to_market(
            symbol="NSE-NIFTY",
            price=Decimal("110"),
            timestamp=datetime(2024, 1, 2, 9, 16, tzinfo=UTC),
        )
        point = sim.equity_curve[-1]
        assert point.positions_value == Decimal("110")
        assert point.total_equity == point.cash + point.positions_value


# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# Coverage backfill \u2014 lines 229-230, 240, 247, 312, 342
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


class TestCoverageBackfill:
    """Cover the remaining branches that close 93.67\u2192100% per
    DEC-COV-BACKFILL-004 (MIGRATION entry #260)."""

    # NOTE: Line 247 (`raise InvalidLotSizeError(f"order qty must be
    # positive...")`) is structurally unreachable from public API:
    # ExecutionSimulator.__init__ rejects `default_qty <= 0` at line
    # 120-121 (ValueError), and the only caller `execute()` uses
    # `self._default_qty` (already validated > 0). Source-level
    # `# pragma: no cover` documents this; defensive code kept in
    # place for future-proofing.

    def test_mark_zero_qty_position_zeros_unrealized(self, idx_meta: InstrumentMeta) -> None:
        """Cover lines 229-230 + 342: mark_to_market on a position that
        was opened then fully closed (qty == 0) sets last_mark_price
        and zeros unrealized_pnl. The _compute_positions_value loop
        also continues past the zero-qty entry (line 342)."""
        sim = _make_sim(idx_meta, cash=Decimal("10000"), slippage_bps=Decimal("0"))
        # Open
        sim.execute(_signal(Side.BUY), _bar(100))
        # Fully close \u2014 partial close where signed_delta exactly cancels
        sim.execute(_signal(Side.SELL), _bar(110))
        pos = sim.positions["NSE-NIFTY"]
        assert pos.qty == 0
        # Now mark on the zero-qty position (covers 229-230)
        sim.mark_to_market(
            symbol="NSE-NIFTY",
            price=Decimal("120"),
            timestamp=datetime(2024, 1, 2, 9, 17, tzinfo=UTC),
        )
        assert pos.last_mark_price == Decimal("120")
        assert pos.unrealized_pnl == Decimal("0")
        # And the positions_value summing loop skipped this zero-qty
        # entry (covers line 342). Last equity point's positions_value
        # should be zero.
        assert sim.equity_curve[-1].positions_value == Decimal("0")

    def test_snapshot_returns_equity_point(self, idx_meta: InstrumentMeta) -> None:
        """Cover line 240: ``snapshot()`` delegates to ``_snapshot_equity``
        and returns the appended EquityPoint."""
        sim = _make_sim(idx_meta, cash=Decimal("10000"))
        ts = datetime(2024, 1, 2, 9, 15, tzinfo=UTC)
        point = sim.snapshot(ts)
        assert point.timestamp == ts
        assert point.cash == Decimal("10000")
        assert point.positions_value == Decimal("0")
        assert sim.equity_curve[-1] is point

    # NOTE: Line 312 (`if pos.qty == 0: pos.avg_price = Decimal("0")`
    # inside the partial-close branch) is structurally unreachable:
    # the partial-close branch requires `|signed_delta| < |pos.qty|`
    # (strict inequality), so after `pos.qty += signed_delta` the new
    # qty cannot be zero. Source-level `# pragma: no branch` documents
    # this; defensive code kept in place for future-proofing.


# ────────────────────────────────────────────────────────────────────────
# reset() — reproducible re-run (FIX 1, DEC-EXECUTION-REPRO-RESILIENCE-001)
# ────────────────────────────────────────────────────────────────────────


class TestReset:
    def test_reset_restores_initial_state(self, idx_meta: InstrumentMeta) -> None:
        """After fills + marks, reset() restores cash and clears the book."""
        sim = _make_sim(idx_meta, cash=Decimal("10000"), slippage_bps=Decimal("0"))
        sim.execute(_signal(Side.BUY), _bar(100))
        sim.mark_to_market(
            symbol="NSE-NIFTY",
            price=Decimal("110"),
            timestamp=datetime(2024, 1, 2, 9, 16, tzinfo=UTC),
        )
        # Sanity — state is now non-trivial.
        assert sim.cash != Decimal("10000")
        assert sim.fills != ()
        assert sim.equity_curve != ()
        assert sim.positions != {}

        sim.reset()

        assert sim.cash == Decimal("10000")
        assert sim.fills == ()
        assert sim.equity_curve == ()
        assert sim.positions == {}

    def test_reset_on_fresh_sim_is_noop_equivalent(self, idx_meta: InstrumentMeta) -> None:
        """reset() on a never-used simulator leaves it at construction state."""
        sim = _make_sim(idx_meta, cash=Decimal("777"))
        sim.reset()
        assert sim.cash == Decimal("777")
        assert sim.fills == ()
        assert sim.equity_curve == ()
        assert sim.positions == {}


# ────────────────────────────────────────────────────────────────────────
# _require_tradeable_price — non-tradeable price guard (FIX 3)
# ────────────────────────────────────────────────────────────────────────


class TestRequireTradeablePrice:
    _TS = datetime(2024, 1, 2, 9, 15, tzinfo=UTC)

    @pytest.mark.parametrize(
        "price",
        [
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
            Decimal("0"),
            Decimal("-5"),
        ],
    )
    def test_non_tradeable_price_rejected(self, price: Decimal) -> None:
        """NaN / inf / zero / negative all raise InvalidPriceError."""
        with pytest.raises(InvalidPriceError, match="non-tradeable price"):
            ExecutionSimulator._require_tradeable_price(
                price, symbol="NSE-NIFTY", timestamp=self._TS
            )

    def test_positive_finite_price_passes(self) -> None:
        """A normal positive price does not raise (no return value)."""
        assert (
            ExecutionSimulator._require_tradeable_price(
                Decimal("123.45"), symbol="NSE-NIFTY", timestamp=self._TS
            )
            is None
        )

    def test_execute_rejects_zero_close(self, idx_meta: InstrumentMeta) -> None:
        """A zero bar.close trips the guard inside execute() (typed, not ValueError)."""
        sim = _make_sim(idx_meta)
        with pytest.raises(InvalidPriceError, match="non-tradeable price"):
            sim.execute(_signal(Side.BUY), _bar(0))

    def test_mark_to_market_rejects_negative_price(self, idx_meta: InstrumentMeta) -> None:
        """A negative mark price trips the guard inside mark_to_market()."""
        sim = _make_sim(idx_meta)
        with pytest.raises(InvalidPriceError, match="non-tradeable price"):
            sim.mark_to_market(
                symbol="NSE-NIFTY",
                price=Decimal("-1"),
                timestamp=self._TS,
            )
