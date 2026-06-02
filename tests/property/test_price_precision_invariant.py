"""Property invariant: lake prices are exact Decimal rupees, never int64 paisa.

Pins DEC-PAISA-DOC-RECONCILE-001. ``candle_fields.to_price`` parses an OHLC /
spot / option price to an exact :class:`~decimal.Decimal` rupee value (via
``Decimal(str(raw))`` — no float drift) and does **not** scale to int64 paisa.
int64 paisa is the unit of the costs calculator + money display only
(``brutex.costs`` / ``brutex.money``), never of price storage. This test fails
if a future change re-introduces a ``x 100`` paisa scaling into the price path.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.data.candle_fields import CandleParseError, to_price
from hypothesis import given, settings
from hypothesis import strategies as st

#: Two-decimal rupee prices (paisa-precision) — the real OHLC value domain.
_RUPEE_PRICES = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("10000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


@given(price=_RUPEE_PRICES)
@settings(max_examples=300, deadline=None)
def test_to_price_returns_exact_decimal_rupees(price: Decimal) -> None:
    """``to_price`` returns the exact rupee value as a ``Decimal`` (no drift)."""
    result = to_price(str(price), "close")
    assert isinstance(result, Decimal)
    assert result == price
    assert result == Decimal(str(price))


@given(price=_RUPEE_PRICES)
@settings(max_examples=300, deadline=None)
def test_to_price_does_not_scale_to_paisa(price: Decimal) -> None:
    """The result is rupees, never the ``x 100`` paisa integer (the doc-myth)."""
    result = to_price(str(price), "open")
    assert result == price
    assert result != price * 100  # a paisa-scaled value would be 100x larger
    assert result < price * 100


def test_to_price_concrete_rupees_not_paisa() -> None:
    """Worked example: 100.00 rupees stays 100.00, never 10000 paisa."""
    assert to_price("100.00", "high") == Decimal("100.00")
    assert to_price("100.00", "high") != Decimal("10000")


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "Infinity", "NaN"])
def test_to_price_rejects_non_finite(bad: str) -> None:
    """Non-finite 'prices' are rejected — never silently stored."""
    with pytest.raises(CandleParseError):
        to_price(bad, "low")
