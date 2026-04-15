"""Unit tests for :class:`brutex.execution.costs.IndianCostModel`.

Pins the simplified FR-EXEC-002 charge schedule to concrete
numbers so regulatory changes don't slip in silently. The full
golden test cross-checking against Zerodha / Groww calculators
is planned separately under ``tests/golden/``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.execution.costs import (
    DEFAULT_BROKERAGE,
    EXCH_NSE_EQUITY,
    EXCH_NSE_FUTURES,
    GST_RATE,
    SEBI_TURNOVER_FEE,
    STT_FUTURES,
    STT_INTRADAY_EQUITY,
    STT_OPTIONS,
    IndianCostModel,
)
from brutex.execution.types import FillCosts
from brutex.models import InstrumentType, Segment
from brutex.strategies.types import Side


class TestRateConstants:
    def test_default_brokerage_is_twenty(self) -> None:
        assert Decimal("20") == DEFAULT_BROKERAGE

    def test_gst_is_eighteen_percent(self) -> None:
        assert Decimal("0.18") == GST_RATE

    def test_stt_rates_decrease_intraday_to_fut(self) -> None:
        """Intraday equity STT > futures STT (regulated schedule)."""
        assert STT_INTRADAY_EQUITY > STT_FUTURES

    def test_options_stt_is_highest(self) -> None:
        """Options STT on premium is the highest of the three."""
        assert STT_OPTIONS > STT_INTRADAY_EQUITY
        assert STT_OPTIONS > STT_FUTURES


class TestIndianCostModelEquityIntraday:
    def test_buy_side_has_no_stt(self) -> None:
        """STT is sell-side only for intraday equity \u2014 buy leg is zero."""
        model = IndianCostModel()
        costs = model.compute(
            side=Side.BUY,
            qty=100,
            fill_price=Decimal("250"),
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
        )
        assert costs.stt == Decimal("0")

    def test_sell_side_stt_matches_rate(self) -> None:
        """STT on sell == notional * 0.025% (intraday equity)."""
        model = IndianCostModel()
        notional = Decimal("100") * Decimal("200")  # 20000
        costs = model.compute(
            side=Side.SELL,
            qty=100,
            fill_price=Decimal("200"),
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
        )
        expected = (notional * STT_INTRADAY_EQUITY).quantize(Decimal("0.01"))
        assert costs.stt == expected

    def test_exchange_fee_uses_equity_rate(self) -> None:
        model = IndianCostModel()
        notional = Decimal("100") * Decimal("200")
        costs = model.compute(
            side=Side.BUY,
            qty=100,
            fill_price=Decimal("200"),
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
        )
        expected = (notional * EXCH_NSE_EQUITY).quantize(Decimal("0.01"))
        assert costs.exchange_fee == expected

    def test_sebi_fee_scales_with_notional(self) -> None:
        model = IndianCostModel()
        notional = Decimal("10_000_000")  # \u20b91 crore
        costs = model.compute(
            side=Side.BUY,
            qty=10000,
            fill_price=Decimal("1000"),
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
        )
        # SEBI at \u20b910 per crore \u2192 exactly \u20b910.00 on \u20b91 crore notional.
        expected = (notional * SEBI_TURNOVER_FEE).quantize(Decimal("0.01"))
        assert costs.sebi_fee == expected

    def test_gst_is_eighteen_percent_of_taxable_base(self) -> None:
        """GST base = brokerage + exchange + SEBI (NOT STT)."""
        model = IndianCostModel(brokerage_per_order=Decimal("20"))
        costs = model.compute(
            side=Side.BUY,
            qty=100,
            fill_price=Decimal("200"),
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
        )
        base = costs.brokerage + costs.exchange_fee + costs.sebi_fee
        expected_gst = (base * GST_RATE).quantize(Decimal("0.01"))
        assert costs.gst == expected_gst


class TestIndianCostModelFutures:
    def test_fut_sell_uses_fut_stt_rate(self) -> None:
        model = IndianCostModel()
        notional = Decimal("50") * Decimal("100000")  # 50 lots * 100000
        costs = model.compute(
            side=Side.SELL,
            qty=50,
            fill_price=Decimal("100000"),
            segment=Segment.FNO,
            instrument_type=InstrumentType.FUT,
        )
        expected_stt = (notional * STT_FUTURES).quantize(Decimal("0.01"))
        assert costs.stt == expected_stt

    def test_fut_exchange_fee_uses_fut_rate(self) -> None:
        model = IndianCostModel()
        notional = Decimal("50") * Decimal("100000")
        costs = model.compute(
            side=Side.BUY,
            qty=50,
            fill_price=Decimal("100000"),
            segment=Segment.FNO,
            instrument_type=InstrumentType.FUT,
        )
        expected_exch = (notional * EXCH_NSE_FUTURES).quantize(Decimal("0.01"))
        assert costs.exchange_fee == expected_exch


class TestIndianCostModelOptions:
    def test_ce_sell_uses_options_stt(self) -> None:
        model = IndianCostModel()
        notional = Decimal("50") * Decimal("300")  # 50 lots * premium 300
        costs = model.compute(
            side=Side.SELL,
            qty=50,
            fill_price=Decimal("300"),
            segment=Segment.FNO,
            instrument_type=InstrumentType.CE,
        )
        expected = (notional * STT_OPTIONS).quantize(Decimal("0.01"))
        assert costs.stt == expected

    def test_pe_sell_uses_options_stt(self) -> None:
        model = IndianCostModel()
        notional = Decimal("75") * Decimal("150")
        costs = model.compute(
            side=Side.SELL,
            qty=75,
            fill_price=Decimal("150"),
            segment=Segment.FNO,
            instrument_type=InstrumentType.PE,
        )
        expected = (notional * STT_OPTIONS).quantize(Decimal("0.01"))
        assert costs.stt == expected

    def test_options_exchange_fee_is_highest(self) -> None:
        """NSE charges the most on OPT premium \u2014 more than equity or FUT notional.

        Reality-check of the current schedule (per
        ``docs/REQUIREMENTS.md \u00a712.2``):
        OPT (0.03503%) \u226b EQUITY (0.00297%) > FUT (0.00173%).
        """
        model = IndianCostModel()
        common = {
            "side": Side.BUY,
            "qty": 1000,  # large enough to see the gap in paise
            "fill_price": Decimal("1000"),
        }
        opt_costs = model.compute(
            segment=Segment.FNO,
            instrument_type=InstrumentType.CE,
            **common,
        )
        fut_costs = model.compute(
            segment=Segment.FNO,
            instrument_type=InstrumentType.FUT,
            **common,
        )
        eq_costs = model.compute(
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
            **common,
        )
        assert opt_costs.exchange_fee > eq_costs.exchange_fee
        assert eq_costs.exchange_fee > fut_costs.exchange_fee


class TestIndianCostModelQuantisation:
    def test_every_charge_rounds_to_paise(self) -> None:
        """Every field must be quantised to \u20b90.01 for reproducibility."""
        model = IndianCostModel()
        costs = model.compute(
            side=Side.SELL,
            qty=123,  # deliberately awkward so rates don't land on whole paise
            fill_price=Decimal("237.77"),
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
        )
        for name in (
            "brokerage",
            "stt",
            "exchange_fee",
            "sebi_fee",
            "stamp_duty",
            "gst",
        ):
            value: Decimal = getattr(costs, name)
            # The quantised value has at most 2 decimal places.
            assert -value.as_tuple().exponent <= 2, f"{name} not rounded: {value}"

    def test_total_is_sum_of_fields(self) -> None:
        model = IndianCostModel()
        costs = model.compute(
            side=Side.SELL,
            qty=100,
            fill_price=Decimal("200"),
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
        )
        manual = (
            costs.brokerage
            + costs.stt
            + costs.exchange_fee
            + costs.sebi_fee
            + costs.stamp_duty
            + costs.gst
        )
        assert costs.total == manual


class TestIndianCostModelValidation:
    def test_hold_side_rejected(self) -> None:
        model = IndianCostModel()
        with pytest.raises(ValueError, match="HOLD"):
            model.compute(
                side=Side.HOLD,
                qty=1,
                fill_price=Decimal("100"),
                segment=Segment.CASH,
                instrument_type=InstrumentType.EQ,
            )

    def test_zero_qty_rejected(self) -> None:
        model = IndianCostModel()
        with pytest.raises(ValueError, match="qty must be positive"):
            model.compute(
                side=Side.BUY,
                qty=0,
                fill_price=Decimal("100"),
                segment=Segment.CASH,
                instrument_type=InstrumentType.EQ,
            )

    def test_non_positive_price_rejected(self) -> None:
        model = IndianCostModel()
        with pytest.raises(ValueError, match="fill_price must be positive"):
            model.compute(
                side=Side.BUY,
                qty=100,
                fill_price=Decimal("0"),
                segment=Segment.CASH,
                instrument_type=InstrumentType.EQ,
            )


class TestIndianCostModelCustomBrokerage:
    def test_zero_brokerage(self) -> None:
        """Discount-broker scenario \u2014 brokerage = 0 still produces costs."""
        model = IndianCostModel(brokerage_per_order=Decimal("0"))
        costs = model.compute(
            side=Side.BUY,
            qty=100,
            fill_price=Decimal("200"),
            segment=Segment.CASH,
            instrument_type=InstrumentType.EQ,
        )
        assert costs.brokerage == Decimal("0.00")
        assert costs.exchange_fee > Decimal("0")
        assert costs.sebi_fee > Decimal("0")

    def test_custom_brokerage_reflected_in_gst(self) -> None:
        """Bumping brokerage scales GST proportionally (same base formula)."""
        low = IndianCostModel(brokerage_per_order=Decimal("10"))
        high = IndianCostModel(brokerage_per_order=Decimal("100"))
        args = {
            "side": Side.BUY,
            "qty": 100,
            "fill_price": Decimal("200"),
            "segment": Segment.CASH,
            "instrument_type": InstrumentType.EQ,
        }
        low_costs = low.compute(**args)
        high_costs = high.compute(**args)
        # 90 more brokerage \u2192 GST base up by 90 \u2192 GST up by 90 * 18%.
        expected_gst_delta = Decimal("90") * GST_RATE
        assert (high_costs.gst - low_costs.gst).quantize(Decimal("0.01")) == (
            expected_gst_delta.quantize(Decimal("0.01"))
        )


class TestFillCostsPureBehaviour:
    def test_total_of_empty_costs_is_zero(self) -> None:
        assert FillCosts().total == Decimal("0")

    def test_total_sums_every_field(self) -> None:
        c = FillCosts(
            brokerage=Decimal("20"),
            stt=Decimal("5"),
            exchange_fee=Decimal("1"),
            sebi_fee=Decimal("0.10"),
            stamp_duty=Decimal("0"),
            gst=Decimal("3.80"),
        )
        assert c.total == Decimal("29.90")
