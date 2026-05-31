"""Tests for `brutex.data.spot_features` — prev-close % + 09:15 open-gap %.

100% line+branch: pct_change (normal, zero base, full precision), the
session-open finder (found / absent), and open_gap_pct (with / without a
09:15 bar).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.data.spot_features import open_gap_pct, pct_change, session_open_bar


def _bar(utc_h: int, utc_m: int, *, open_: str = "100", close: str = "100") -> CandleBar:
    return CandleBar(
        timestamp=datetime(2021, 1, 4, utc_h, utc_m, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal("200"),
        low=Decimal("50"),
        close=Decimal(close),
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
