"""Golden byte-equality fixture for the gainers/losers screener (DEC-MOVERS-GAINERS-001).

Freezes the EXACT screen a fixed lake produces — the ranked gainers, losers,
and skipped instruments, with the **full-precision** % change rendered as its
exact :class:`~decimal.Decimal` string — so any drift in the sign-split, the
ordering / tiebreak, the 2-decimal price snap, or the (deliberately
un-rounded) % change maths is caught bit-for-bit (Golden Rule #4).

The repeating ``-66.66666666666666666666666667`` for DDD is the determinism
oracle: it pins that the % change keeps full Decimal precision and is NEVER
money-rounded (CLAUDE.md §4 / ``DEC-MONEY-2DP-001`` is display-money-only).

Regenerate the constant only on a deliberate behaviour change and record it in
``docs/MIGRATION.md`` (entry #700).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from brutex.screeners.gainers_losers import (
    InstrumentRef,
    Mover,
    ScreenResult,
    screen_gainers_losers,
)

from tests.fakes.screener_lake import FakeLakeReader, make_bar

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2024, 12, 31, tzinfo=UTC)


def _ref(symbol: str) -> InstrumentRef:
    return InstrumentRef(symbol=symbol, exchange="NSE", segment="INDEX")


def _mover_row(m: Mover) -> dict[str, object]:
    return {
        "symbol": m.ref.symbol,
        "exchange": m.ref.exchange,
        "segment": m.ref.segment,
        "base_price": str(m.base_price),
        "last_price": str(m.last_price),
        "pct_change": str(m.pct_change),  # exact Decimal string — never rounded
        "bar_count": m.bar_count,
    }


def _ref_row(ref: InstrumentRef) -> dict[str, str]:
    return {"symbol": ref.symbol, "exchange": ref.exchange, "segment": ref.segment}


def _render(result: ScreenResult) -> str:
    return json.dumps(
        {
            "gainers": [_mover_row(m) for m in result.gainers],
            "losers": [_mover_row(m) for m in result.losers],
            "skipped": [_ref_row(r) for r in result.skipped],
        },
        indent=2,
    )


def _fixture_reader() -> FakeLakeReader:
    return FakeLakeReader(
        data={
            _ref("AAA"): [make_bar(minute=0, open_="100.00", close="110.00")],
            _ref("BBB"): [make_bar(minute=0, open_="200.00", close="150.00")],
            _ref("CCC"): [make_bar(minute=0, open_="100.00", close="133.33")],
            _ref("DDD"): [make_bar(minute=0, open_="300.00", close="100.00")],
            _ref("EEE"): [make_bar(minute=0, open_="100.00", close="100.00")],
            _ref("FFF"): [],
        }
    )


#: The frozen screen. Decimals are stored as exact strings so the comparison is
#: bit-for-bit (no tolerance) — the determinism oracle.
_GOLDEN: str = json.dumps(
    {
        "gainers": [
            {
                "symbol": "CCC",
                "exchange": "NSE",
                "segment": "INDEX",
                "base_price": "100.00",
                "last_price": "133.33",
                "pct_change": "33.3300",
                "bar_count": 1,
            },
            {
                "symbol": "AAA",
                "exchange": "NSE",
                "segment": "INDEX",
                "base_price": "100.00",
                "last_price": "110.00",
                "pct_change": "10.0",
                "bar_count": 1,
            },
        ],
        "losers": [
            {
                "symbol": "DDD",
                "exchange": "NSE",
                "segment": "INDEX",
                "base_price": "300.00",
                "last_price": "100.00",
                "pct_change": "-66.66666666666666666666666667",
                "bar_count": 1,
            },
            {
                "symbol": "BBB",
                "exchange": "NSE",
                "segment": "INDEX",
                "base_price": "200.00",
                "last_price": "150.00",
                "pct_change": "-25.00",
                "bar_count": 1,
            },
        ],
        "skipped": [{"symbol": "FFF", "exchange": "NSE", "segment": "INDEX"}],
    },
    indent=2,
)


def test_screen_is_byte_frozen() -> None:
    """A fixed lake produces the exact frozen screen (Golden Rule #4)."""
    result = screen_gainers_losers(
        _fixture_reader(), interval="1day", start=_START, end=_END, top_n=3
    )
    assert _render(result) == _GOLDEN


def test_screen_is_reproducible() -> None:
    """Same inputs → byte-identical render across repeated calls."""
    first = screen_gainers_losers(
        _fixture_reader(), interval="1day", start=_START, end=_END, top_n=3
    )
    second = screen_gainers_losers(
        _fixture_reader(), interval="1day", start=_START, end=_END, top_n=3
    )
    assert _render(first) == _render(second) == _GOLDEN
