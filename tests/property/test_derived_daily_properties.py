"""Property invariant: the DERIVED 1-day bar is an exact OHLCV aggregation of its 1-minute.

Locks the operator's precision guarantee for DEC-1MINUTE-ONLY-DERIVE-1DAY-001: 1-minute
is the ONLY fetched granularity and the ``1day`` bar (the PDH/PDL anchor the engine reads)
is DERIVED from it. For ANY random valid 1-minute OHLCV series over a SINGLE IST trading
day, the single derived daily bar produced by
:func:`brutex.data.spot_daily_resample.resample_1m_to_daily` must satisfy:

* ``open``   == the FIRST minute's open,
* ``high``   == max over ALL minute highs,
* ``low``    == min over ALL minute lows,
* ``close``  == the LAST minute's close,
* ``volume`` == sum over ALL minute volumes, and
* ``low <= open <= high`` and ``low <= close <= high`` (the bar is internally consistent).

This proves the aggregation across infinite permutations, not just the fixtures.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.spot_daily_resample import resample_1m_to_daily
from hypothesis import given, settings
from hypothesis import strategies as st

pl = pytest.importorskip("polars")  # the frame construction needs the [data] extra

# A fixed IST trading day whose 09:15 IST open is 03:45 UTC (same calendar day in UTC, so
# every in-session minute shares the one IST date → exactly one derived daily bar).
_DAY = date(2024, 1, 15)
_SESSION_OPEN_UTC = datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC)
# ``before`` strictly AFTER the session date so the (complete) bar is never dropped as
# still-forming (the look-ahead guard only drops sessions whose IST date >= before).
_BEFORE = date(2024, 1, 16)


@st.composite
def _minute_bars(draw: st.DrawFn) -> list[dict[str, object]]:
    """Draw 1..120 consecutive in-session 1-minute bars with valid intra-bar OHLC.

    Each bar's ``low <= open, close <= high`` is enforced at construction so the strategy
    only emits VALID 1-minute candles (a malformed input is out of scope for this
    aggregation invariant). Prices are 2-dp Decimals; volumes are non-negative ints.
    """
    n = draw(st.integers(min_value=1, max_value=120))
    price = st.decimals(
        min_value=Decimal("1.00"),
        max_value=Decimal("100000.00"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    )
    bars: list[dict[str, object]] = []
    for i in range(n):
        a = draw(price)
        b = draw(price)
        c = draw(price)
        d = draw(price)
        low = min(a, b, c, d)
        high = max(a, b, c, d)
        # open / close are the two middle values — guaranteed within [low, high].
        mids = sorted((a, b, c, d))
        open_ = mids[1]
        close = mids[2]
        bars.append(
            {
                "ts_utc": _SESSION_OPEN_UTC + timedelta(minutes=i),
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": draw(st.integers(min_value=0, max_value=10_000_000)),
            }
        )
    return bars


@given(rows=_minute_bars())
@settings(max_examples=200, deadline=None)
def test_derived_daily_is_exact_ohlcv_aggregation(rows: list[dict[str, object]]) -> None:
    """The derived daily bar exactly aggregates its 1-minute series (open/high/low/close/vol)."""
    df = pl.DataFrame(
        rows,
        schema={
            "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )
    daily = resample_1m_to_daily(df, before=_BEFORE)
    # Exactly one COMPLETE IST trading day is present → exactly one derived daily bar.
    assert len(daily) == 1
    bar = daily[0]

    first = rows[0]
    last = rows[-1]
    assert bar.open == Decimal(str(first["open"]))  # open = first minute's open
    assert bar.close == Decimal(str(last["close"]))  # close = last minute's close
    assert bar.high == Decimal(str(max(float(r["high"]) for r in rows)))  # high = max
    assert bar.low == Decimal(str(min(float(r["low"]) for r in rows)))  # low = min
    assert bar.volume == sum(int(r["volume"]) for r in rows)  # volume = sum
    # The derived bar is internally consistent.
    assert bar.low <= bar.open <= bar.high
    assert bar.low <= bar.close <= bar.high
    # Spot/index daily bars carry no OI (the derive sets open_interest=None).
    assert bar.open_interest is None
