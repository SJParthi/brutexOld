"""Stress test for the gainers/losers screener (DEC-MOVERS-GAINERS-001).

Screens a LARGE lake (tens of thousands of instruments) to prove the screener
stays correct + deterministic at scale: no crash on a wide varied universe, the
sign-split holds, the lists are correctly capped + ordered, every instrument is
accounted for, and the whole screen is byte-stable across repeated runs (Golden
Rule #4 / #7).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.data.models import CandleBar
from brutex.screeners.gainers_losers import InstrumentRef, screen_gainers_losers

from tests.fakes.screener_lake import FakeLakeReader, make_bar

pytestmark = pytest.mark.stress

_UNIVERSE = 30_000
_TOP_N = 50
_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2024, 12, 31, tzinfo=UTC)


def _large_lake() -> FakeLakeReader:
    """A deterministic ``_UNIVERSE``-instrument lake spanning gain/lose/flat/skip."""
    data: dict[InstrumentRef, list[CandleBar]] = {}
    for i in range(_UNIVERSE):
        ref = InstrumentRef(symbol=f"SYM{i:05d}", exchange="NSE", segment="CASH")
        bucket = i % 4
        if bucket == 0:  # gainer — close above open
            data[ref] = [make_bar(minute=0, open_="100.00", close=f"{100 + (i % 50) + 1}.00")]
        elif bucket == 1:  # loser — close below open
            data[ref] = [make_bar(minute=0, open_="100.00", close=f"{99 - (i % 50)}.00")]
        elif bucket == 2:  # flat — ranked out
            data[ref] = [make_bar(minute=0, open_="100.00", close="100.00")]
        else:  # no window data — skipped
            data[ref] = []
    return FakeLakeReader(data=data)


def test_large_screen_is_correct_and_deterministic() -> None:
    reader = _large_lake()
    result = screen_gainers_losers(reader, interval="1day", start=_START, end=_END, top_n=_TOP_N)

    # Sign-split holds at scale.
    assert all(m.pct_change > 0 for m in result.gainers)
    assert all(m.pct_change < 0 for m in result.losers)

    # Both lists are capped at top_n (the universe is large enough to fill both).
    assert len(result.gainers) == _TOP_N
    assert len(result.losers) == _TOP_N

    # Ordered: gainers descending, losers ascending (most negative first).
    g_pct = [m.pct_change for m in result.gainers]
    l_pct = [m.pct_change for m in result.losers]
    assert g_pct == sorted(g_pct, reverse=True)
    assert l_pct == sorted(l_pct)

    # Every "no data" instrument (bucket 3) is skipped — exactly a quarter.
    assert len(result.skipped) == _UNIVERSE // 4
    assert [r.symbol for r in result.skipped] == sorted(r.symbol for r in result.skipped)

    # Byte-stable across a re-run (Golden Rule #4 / #7).
    rerun = screen_gainers_losers(reader, interval="1day", start=_START, end=_END, top_n=_TOP_N)
    assert rerun == result
