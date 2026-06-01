"""Tests for `brutex.data.session` — NSE/BSE session filter (09:15–15:30 IST).

100% line+branch: the open-inclusive / close-exclusive boundaries and the
intraday filter (keeps in-session, drops pre-open + post-close).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.data.session import filter_intraday_session, in_session, is_session_exempt


def _bar(hh: int, mm: int) -> CandleBar:
    """A bar whose UTC timestamp equals ``hh:mm`` UTC on 2021-01-04."""
    return CandleBar(
        timestamp=datetime(2021, 1, 4, hh, mm, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=0,
        open_interest=None,
    )


# IST = UTC + 5:30 → 09:15 IST = 03:45 UTC; 15:30 IST = 10:00 UTC.
class TestInSession:
    @pytest.mark.parametrize(
        ("utc_h", "utc_m", "expected"),
        [
            (3, 45, True),  # 09:15 IST — open, inclusive
            (3, 44, False),  # 09:14 IST — pre-open
            (3, 30, False),  # 09:00 IST — pre-open auction
            (6, 0, True),  # 11:30 IST — mid-session
            (9, 59, True),  # 15:29 IST — last intraday minute
            (10, 0, False),  # 15:30 IST — close, exclusive
            (10, 1, False),  # 15:31 IST — post-close
        ],
    )
    def test_boundaries(self, utc_h: int, utc_m: int, expected: bool) -> None:
        assert in_session(_bar(utc_h, utc_m).timestamp) is expected


class TestFilterIntradaySession:
    def test_keeps_in_session_drops_out(self) -> None:
        bars = [_bar(3, 30), _bar(3, 45), _bar(6, 0), _bar(9, 59), _bar(10, 0)]
        kept = filter_intraday_session(bars)
        assert [b.timestamp.hour * 60 + b.timestamp.minute for b in kept] == [
            3 * 60 + 45,
            6 * 60,
            9 * 60 + 59,
        ]

    def test_empty(self) -> None:
        assert filter_intraday_session([]) == []


class TestSessionExempt:
    """GIFT Nifty is exempt from the NSE 09:15–15:30 filter (DEC-DHAN-GIFTNIFTY-SESSION-001)."""

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("GIFTNIFTY", True),
            ("giftnifty", True),  # case-insensitive
            ("  GIFTNIFTY  ", True),  # whitespace-tolerant
            ("NIFTY", False),
            ("BANKNIFTY", False),
            ("SENSEX", False),
            ("", False),
            (None, False),
        ],
    )
    def test_is_session_exempt(self, symbol: str | None, expected: bool) -> None:
        assert is_session_exempt(symbol) is expected
