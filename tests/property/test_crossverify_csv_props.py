"""Hypothesis properties — the cross-verify CSV byte contract (§9.3).

Every property drives the REAL renderer (:mod:`brutex.crossverify.csv_render`)
over generated in-session days: 2dp price-cell law + lossless round-trip,
strictly-increasing in-session timestamps, byte determinism, the futures
canonical-token round trip through the ONE lake grammar, and the
no-quote-ever property.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from brutex.calendars.nse import SessionType, TradingSession
from brutex.crossverify.canonical import canonical_identity
from brutex.crossverify.csv_render import ExportRow, render_day_csv, render_price_cell
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.session import IST
from hypothesis import given
from hypothesis import strategies as st

pytestmark = pytest.mark.property

_DAY = date(2026, 7, 10)
_SESSION = TradingSession(
    date=_DAY,
    session_type=SessionType.REGULAR,
    open_time_ist=time(9, 15),
    close_time_ist=time(15, 30),
)
_PRICE_CELL_RE = re.compile(r"^\d+\.\d{2}$")

#: Stored prices as the lake holds them: positive 2dp-snapped rupees stored
#: as Float64 — generated as paisa ints so every value is a legal post-snap
#: stored float; the renderer must reproduce the exact 2dp text.
_stored_prices = st.integers(min_value=1, max_value=10_000_000_00).map(lambda paisa: paisa / 100.0)

_minute_indexes = st.lists(
    st.integers(min_value=0, max_value=374), min_size=1, max_size=40, unique=True
).map(sorted)


def _identity(symbol: str = "NIFTY") -> object:
    identity = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol=symbol)
    assert identity is not None
    return identity


def _row(minute: int, price: float, volume: int) -> ExportRow:
    value = Decimal(str(price))
    ts = datetime(2026, 7, 10, 3, 45, tzinfo=UTC) + timedelta(minutes=minute)
    return ExportRow(
        ts_utc=ts, open=value, high=value, low=value, close=value, volume=volume, oi=None
    )


class TestPriceCellLaw:
    @given(stored=_stored_prices)
    def test_cell_shape_and_lossless_round_trip(self, stored: float) -> None:
        from brutex.money import round_money

        cell = render_price_cell(stored)
        assert _PRICE_CELL_RE.match(cell)
        assert Decimal(cell) == round_money(Decimal(str(stored)))

    @given(noise=st.floats(min_value=0.01, max_value=1_000_000, allow_nan=False))
    def test_arbitrary_positive_float_renders_canonical_2dp(self, noise: float) -> None:
        from brutex.money import round_money

        cell = render_price_cell(noise)
        assert _PRICE_CELL_RE.match(cell)
        assert Decimal(cell) == round_money(Decimal(str(noise)))


class TestRenderedFileProperties:
    @given(minutes=_minute_indexes, price=_stored_prices, volume=st.integers(0, 10**9))
    def test_timestamps_strictly_increasing_and_in_session(
        self, minutes: list[int], price: float, volume: int
    ) -> None:
        rows = [_row(m, price, volume) for m in minutes]
        body = render_day_csv(identity=_identity(), rows=rows, session=_SESSION)  # type: ignore[arg-type]
        lines = body.decode("utf-8").strip().split("\n")[1:]
        stamps = [line.split(",")[1] for line in lines]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == len(stamps)
        for stamp in stamps:
            parsed = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            assert parsed.date() == _DAY
            assert time(9, 15) <= parsed.time() < time(15, 30)
            assert parsed.second == 0

    @given(minutes=_minute_indexes, price=_stored_prices, volume=st.integers(0, 10**9))
    def test_render_is_deterministic(self, minutes: list[int], price: float, volume: int) -> None:
        rows = [_row(m, price, volume) for m in minutes]
        first = render_day_csv(identity=_identity(), rows=rows, session=_SESSION)  # type: ignore[arg-type]
        second = render_day_csv(identity=_identity(), rows=rows, session=_SESSION)  # type: ignore[arg-type]
        assert first == second

    @given(minutes=_minute_indexes, price=_stored_prices, volume=st.integers(0, 10**9))
    def test_no_quote_ever(self, minutes: list[int], price: float, volume: int) -> None:
        rows = [_row(m, price, volume) for m in minutes]
        body = render_day_csv(identity=_identity(), rows=rows, session=_SESSION)  # type: ignore[arg-type]
        assert b'"' not in body
        assert b"\r" not in body


class TestFuturesTokenRoundTrip:
    @given(
        underlying=st.sampled_from(["NIFTY", "BANKNIFTY"]),
        expiry=st.dates(min_value=date(2026, 1, 1), max_value=date(2030, 12, 31)),
    )
    def test_canonical_token_round_trips_parsed_identity(
        self, underlying: str, expiry: date
    ) -> None:
        # Build the lake dir name the way the vendor does (DDMmmYY), parse it
        # through the ONE grammar, and check the canonical token carries the
        # PARSED (underlying, expiry) — never the token string.
        dir_name = f"NSE-{underlying}-{expiry.strftime('%d%b%y')}-FUT"
        parts = parse_fno_symbol(dir_name)
        assert parts is not None
        identity = canonical_identity(exchange="NSE", segment="FNO", lake_symbol=dir_name)
        assert identity is not None
        assert identity.canonical_symbol == f"{underlying}-{expiry.isoformat()}-FUT"
        assert (parts.underlying, parts.expiry) == (underlying, expiry)
        # No lake-namespace collision: the canonical token itself never parses.
        assert parse_fno_symbol(identity.canonical_symbol) is None
