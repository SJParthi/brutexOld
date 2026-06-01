"""Property tests for `brutex.data.spot_features.open_pct_series`
(DEC-DHAN-OPEN-PCT-COLUMN-001).

Invariants (hold for any single-day bar series):

* **Alignment** — the result is 1:1 with the input bars.
* **Twin of pct_change** — when the day's 09:15 open is present and non-zero,
  every entry equals ``pct_change(bar.close, 09:15_open)`` (full precision).
* **No anchor → all None** — without a 09:15 bar there is no base.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.data.spot_features import open_pct_series, pct_change
from hypothesis import given
from hypothesis import strategies as st

# 09:15:00 IST == 03:45 UTC (the session-open anchor).
_OPEN_H, _OPEN_M = 3, 45

#: Positive integer "paisa" prices → exact Decimals, well within OHLC bounds.
_price = st.integers(min_value=1, max_value=10_000_000)
_closes = st.lists(_price, min_size=1, max_size=30)


def _bar(utc_h: int, utc_m: int, open_: int, close: int) -> CandleBar:
    """A valid CandleBar whose high/low span its open+close (so OHLC is always valid)."""
    return CandleBar(
        timestamp=datetime(2021, 1, 4, utc_h, utc_m, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(max(open_, close)),
        low=Decimal(min(open_, close)),
        close=Decimal(close),
        volume=0,
        open_interest=None,
    )


@given(anchor_open=_price, closes=_closes)
def test_open_pct_is_pct_change_of_close_vs_0915_open(anchor_open: int, closes: list[int]) -> None:
    # First bar IS the 09:15 anchor; the rest are later in-session minutes.
    bars = [_bar(_OPEN_H, _OPEN_M, anchor_open, closes[0])]
    bars += [_bar(4, i % 60, c, c) for i, c in enumerate(closes[1:], start=1)]

    result = open_pct_series(bars)

    assert len(result) == len(bars)  # alignment
    base = Decimal(anchor_open)
    for bar, value in zip(bars, result, strict=True):
        assert value == pct_change(bar.close, base)  # exact twin


@given(closes=_closes)
def test_no_0915_bar_yields_all_none(closes: list[int]) -> None:
    # Every bar at 06:xx UTC (11:xx IST) — never the 03:45 UTC anchor.
    bars = [_bar(6, i % 60, c, c) for i, c in enumerate(closes)]
    assert open_pct_series(bars) == [None] * len(bars)
