# pyright: reportPrivateUsage=none
"""Hypothesis property tests for the NSE/BSE 1-day cross-check (DEC-NSE-1DAY-VALIDATION-001).

Invariants proven over random inputs:

1. **Reflexive** — ``compare_quotes(q, q)`` is always MATCHED (every field Δ=0).
2. **Symmetric** — the per-field magnitudes and the verdict status of
   ``compare(a, b)`` and ``compare(b, a)`` agree (only the delta sign flips).
3. **Tolerance monotonicity** — a pair MATCHED at tolerance ``t`` stays MATCHED
   at any ``t' >= t``.
4. **2-dp snap** — :func:`quote_from_groww_candles` never emits a price off the
   2-decimal grid, for any valid candle.
5. **Total override parse** — :func:`_override_sources` never raises for ANY env
   text (fuzz).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest import mock

import pytest
from brutex.calendars import IST
from brutex.data.models import CandleBar
from brutex.data.validation import nse_oneday as m
from brutex.data.validation.nse_oneday import (
    OhlcQuote,
    VerdictStatus,
    compare_quotes,
    quote_from_groww_candles,
)
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.property

_DATE = date(2025, 9, 24)

# 2-dp Decimal prices (both quote builders snap to this grid, so generating on it
# is the realistic post-snap domain).
_prices = st.decimals(
    min_value=Decimal("0.00"),
    max_value=Decimal("100000.00"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
_tolerances = st.decimals(
    min_value=Decimal("0.00"),
    max_value=Decimal("10.00"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _quotes(draw: st.DrawFn, *, source: str = "groww") -> OhlcQuote:
    return OhlcQuote(
        symbol="NIFTY",
        trade_date=_DATE,
        open=draw(_prices),
        high=draw(_prices),
        low=draw(_prices),
        close=draw(_prices),
        source=source,
    )


@st.composite
def _valid_bars(draw: st.DrawFn) -> CandleBar:
    """A valid index CandleBar (low <= open, close <= high; high >= low)."""
    low = draw(_prices)
    span = draw(
        st.decimals(
            min_value=Decimal("0.00"),
            max_value=Decimal("5000.00"),
            places=2,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    high = low + span
    inside = st.decimals(
        min_value=low, max_value=high, places=2, allow_nan=False, allow_infinity=False
    )
    ts = datetime(2025, 9, 24, 9, 15, tzinfo=IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=draw(inside),
        high=high,
        low=low,
        close=draw(inside),
        volume=0,
        open_interest=None,
    )


@settings(max_examples=200)
@given(_quotes())
def test_reflexive_match(quote: OhlcQuote) -> None:
    verdict = compare_quotes(quote, quote)
    assert verdict.status is VerdictStatus.MATCHED
    assert all(field.delta_paisa == 0 for field in verdict.fields)


@settings(max_examples=200)
@given(_quotes(), _quotes(source="nse"), _tolerances)
def test_symmetric_magnitude(a: OhlcQuote, b: OhlcQuote, tol: Decimal) -> None:
    ab = compare_quotes(a, b, tolerance=tol)
    ba = compare_quotes(b, a, tolerance=tol)
    assert ab.status is ba.status
    for fa, fb in zip(ab.fields, ba.fields, strict=True):
        assert fa.delta == -fb.delta
        assert fa.matched is fb.matched


@settings(max_examples=200)
@given(_quotes(), _quotes(source="nse"), _tolerances, _tolerances)
def test_tolerance_monotonic(a: OhlcQuote, b: OhlcQuote, t1: Decimal, t2: Decimal) -> None:
    lo, hi = sorted((t1, t2))
    if compare_quotes(a, b, tolerance=lo).status is VerdictStatus.MATCHED:
        assert compare_quotes(a, b, tolerance=hi).status is VerdictStatus.MATCHED


@settings(max_examples=200)
@given(_valid_bars())
def test_quote_prices_on_2dp_grid(bar: CandleBar) -> None:
    quote = quote_from_groww_candles([bar], symbol="NIFTY", trade_date=_DATE)
    assert quote is not None
    for field_name in ("open", "high", "low", "close"):
        value: Decimal = getattr(quote, field_name)
        assert value.as_tuple().exponent == -2


# Fuzz the PARSER over truly arbitrary env content. We patch the module's view
# of ``os.environ`` with a plain dict rather than the real one, because the real
# ``os.environ`` rejects NUL / un-encodable surrogate strings at assignment (an
# OS limit, not a parser concern) — which would test the OS, not our code.
@settings(max_examples=300)
@given(st.text())
def test_override_parse_never_raises(raw: str) -> None:
    fake_env = {m.INDEX_SOURCE_OVERRIDE_ENV: raw}
    with mock.patch.object(m.os, "environ", fake_env):
        result = m._override_sources()
    assert isinstance(result, dict)
