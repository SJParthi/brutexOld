"""Property + fuzz tests for `brutex.adapters.groww.parser`.

The candle parser is the data-mismatch boundary — so it must (1) parse every
*well-formed* row correctly (exact Decimal prices, IST→UTC timestamps) and
(2) NEVER blow up on a *malformed* row with an unexpected exception. Property
(2) is the fuzz invariant: feed it arbitrary garbage, it only ever returns a
``CandleBar`` or raises ``CandleParseError`` / ``ValueError`` — nothing else.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.adapters.groww.parser import CandleParseError, parse_candle_row
from hypothesis import given
from hypothesis import strategies as st

_IST = ZoneInfo("Asia/Kolkata")


@st.composite
def _well_formed_rows(draw: st.DrawFn) -> list[object]:
    """Generate a row with sane OHLC (low ≤ open/close ≤ high, vol ≥ 0)."""
    low = draw(st.decimals(min_value=Decimal("0.05"), max_value=Decimal("100000"), places=2))
    high = draw(st.decimals(min_value=low, max_value=low + Decimal("5000"), places=2))
    open_ = draw(st.decimals(min_value=low, max_value=high, places=2))
    close = draw(st.decimals(min_value=low, max_value=high, places=2))
    volume = draw(st.integers(min_value=0, max_value=10**12))
    oi = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=10**12)))
    dt = draw(
        st.datetimes(min_value=datetime(2020, 1, 1, 0, 0), max_value=datetime(2030, 1, 1, 0, 0))
    )
    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    return [ts, str(open_), str(high), str(low), str(close), volume, oi]


class TestWellFormed:
    @given(_well_formed_rows())
    def test_prices_exact_and_timestamp_utc(self, row: list[object]) -> None:
        bar = parse_candle_row(row)
        # Exact Decimal prices — no binary-float drift (NFR-DET-001).
        assert bar.open == Decimal(str(row[1]))
        assert bar.high == Decimal(str(row[2]))
        assert bar.low == Decimal(str(row[3]))
        assert bar.close == Decimal(str(row[4]))
        # Timestamps are always UTC-aware.
        assert bar.timestamp.tzinfo is not None
        assert bar.timestamp.utcoffset() == UTC.utcoffset(None)

    @given(_well_formed_rows())
    def test_ist_to_utc_conversion(self, row: list[object]) -> None:
        bar = parse_candle_row(row)
        naive_ist = datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_IST)
        assert bar.timestamp == naive_ist.astimezone(UTC)

    @given(_well_formed_rows())
    def test_prices_are_finite(self, row: list[object]) -> None:
        bar = parse_candle_row(row)
        assert bar.open.is_finite() and bar.high.is_finite()
        assert bar.low.is_finite() and bar.close.is_finite()


#: Arbitrary nasty scalars — text, ints, floats (incl. nan/inf via defaults),
#: bools, None, bytes, nested lists. The raw material for the fuzz invariant.
_nasty = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),  # allow_nan / allow_infinity default True — the nasty cases
    st.text(),
    st.binary(),
    st.lists(st.integers(), max_size=3),
)


class TestFuzzRobustness:
    @given(st.lists(_nasty, min_size=0, max_size=10))
    def test_never_raises_unexpected(self, row: list[object]) -> None:
        """For ANY input, parse_candle_row returns a CandleBar or raises only
        CandleParseError / ValueError — any other exception type propagates and
        fails the test (that's the fuzz invariant)."""
        with contextlib.suppress(CandleParseError, ValueError):
            parse_candle_row(row)

    @given(_well_formed_rows(), st.floats())
    def test_nonfinite_price_is_rejected(self, row: list[object], bad: float) -> None:
        """A non-finite price (nan / inf) must be rejected, never stored."""
        if bad == bad and bad not in (float("inf"), float("-inf")):  # noqa: PLR0124
            return  # only exercise the nan/inf cases
        row[1] = bad
        try:
            bar = parse_candle_row(row)
        except (CandleParseError, ValueError):
            return
        assert bar.open.is_finite()  # if it parsed, it must be finite
