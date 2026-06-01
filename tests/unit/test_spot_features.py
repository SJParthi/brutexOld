"""Tests for `brutex.data.spot_features` — prev-close % + 09:15 open-gap %.

100% line+branch: pct_change (normal, zero base, full precision), the
session-open finder (found / absent), and open_gap_pct (with / without a
09:15 bar).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.data.spot_features import (
    open_gap_pct,
    open_pct_series,
    pct_change,
    session_open_bar,
)


def _bar(utc_h: int, utc_m: int, *, open_: str = "100", close: str = "100") -> CandleBar:
    # high/low span open+close so any (open, close) pair is a valid OHLC bar.
    o, c = Decimal(open_), Decimal(close)
    return CandleBar(
        timestamp=datetime(2021, 1, 4, utc_h, utc_m, tzinfo=UTC),
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=0,
        open_interest=None,
    )


class TestPctChange:
    def test_positive(self) -> None:
        assert pct_change(Decimal("110"), Decimal("100")) == Decimal("10")

    def test_negative(self) -> None:
        assert pct_change(Decimal("90"), Decimal("100")) == Decimal("-10")

    def test_zero_base_is_none(self) -> None:
        assert pct_change(Decimal("5"), Decimal("0")) is None

    def test_full_precision_not_rounded(self) -> None:
        # 1/3 → full Decimal precision, not 2dp.
        result = pct_change(Decimal("101"), Decimal("3"))
        assert result is not None
        assert len(str(result).split(".")[-1]) > 2


class TestSessionOpenBar:
    def test_found(self) -> None:
        # 03:45 UTC = 09:15 IST (session open).
        bars = [_bar(3, 30), _bar(3, 45, open_="105"), _bar(6, 0)]
        found = session_open_bar(bars)
        assert found is not None
        assert found.open == Decimal("105")

    def test_absent(self) -> None:
        assert session_open_bar([_bar(3, 30), _bar(6, 0)]) is None


class TestOpenGapPct:
    def test_gap_from_session_open(self) -> None:
        # 09:15 open = 110 vs prev close 100 → +10% gap.
        bars = [_bar(3, 30, open_="150"), _bar(3, 45, open_="110")]
        assert open_gap_pct(bars, Decimal("100")) == Decimal("10")

    def test_no_session_open_bar_is_none(self) -> None:
        assert open_gap_pct([_bar(6, 0)], Decimal("100")) is None


class TestOpenPctSeries:
    """open_pct = (bar.close − today's 09:15 open)/09:15 open × 100, per candle
    (DEC-DHAN-OPEN-PCT-COLUMN-001)."""

    def test_each_bar_anchored_to_0915_open(self) -> None:
        # 09:15 open = 100; closes 90/100/120 → -10%/0%/+20%.
        bars = [
            _bar(3, 45, open_="100", close="90"),  # the 09:15 bar itself
            _bar(6, 0, close="100"),
            _bar(9, 59, close="120"),
        ]
        assert open_pct_series(bars) == [Decimal("-10"), Decimal("0"), Decimal("20")]

    def test_0915_bar_uses_its_own_open_close(self) -> None:
        # The 09:15 bar's own open_pct = (close − open)/open ×100.
        bars = [_bar(3, 45, open_="200", close="210")]
        assert open_pct_series(bars) == [Decimal("5")]

    def test_no_session_open_bar_all_none(self) -> None:
        bars = [_bar(3, 30, close="90"), _bar(6, 0, close="110")]
        assert open_pct_series(bars) == [None, None]

    def test_zero_0915_open_is_none(self) -> None:
        # 09:15 open = 0 → division base 0 → None per bar.
        bars = [_bar(3, 45, open_="0", close="50"), _bar(6, 0, close="60")]
        assert open_pct_series(bars) == [None, None]

    def test_empty(self) -> None:
        assert open_pct_series([]) == []

    def test_full_precision_not_rounded(self) -> None:
        bars = [_bar(3, 45, open_="3", close="101")]
        result = open_pct_series(bars)
        assert result[0] is not None
        assert len(str(result[0]).split(".")[-1]) > 2  # not money-rounded to 2dp
