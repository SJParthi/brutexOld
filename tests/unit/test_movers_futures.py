"""Unit tests for the Futures movers core (DEC-MOVERS-FUTURES-001) — pure, no FastAPI.

Covers every pure branch of :mod:`brutex.dashboard.movers_futures`: the contract
symbol parser, the near-month picker, the seven ranking functions, the
:class:`~brutex.dashboard.movers_futures.FuturesMover` ₹ rendering,
:func:`~brutex.dashboard.movers_futures.compute_futures_mover`, the payload
builder (every category key + the honest empty / no-mover notes), and the
``_prev_day_oi`` ``None`` branches (over a real ``tmp_path`` — the lake-reading
helper's filesystem branches, no DuckDB/route needed here).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.movers_futures import (
    FuturesMover,
    _prev_day_oi,
    build_futures_payload,
    compute_futures_mover,
    parse_future_symbol,
    rank_discount,
    rank_oi_gainers,
    rank_oi_losers,
    rank_premium,
    rank_price_gainers,
    rank_price_losers,
    rank_volume,
    select_near_month,
)
from brutex.data.models import CandleBar

# ── parse_future_symbol ───────────────────────────────────────────────────


class TestParseFutureSymbol:
    def test_index_future(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-30Sep25-FUT") == ("NIFTY", date(2025, 9, 30))

    def test_stock_future(self) -> None:
        assert parse_future_symbol("NSE-RELIANCE-25Jun26-FUT") == ("RELIANCE", date(2026, 6, 25))

    def test_option_symbol_is_none(self) -> None:
        # An option ends -CE / -PE (last part is not FUT) → not a future.
        assert parse_future_symbol("NSE-NIFTY-30Sep25-24000-CE") is None

    def test_junk_too_few_parts_is_none(self) -> None:
        assert parse_future_symbol("NIFTY-FUT") is None

    def test_bad_month_is_none(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-30Xxx25-FUT") is None

    def test_bad_expiry_length_is_none(self) -> None:
        # Expiry token is not 7 chars (DDMmmYY).
        assert parse_future_symbol("NSE-NIFTY-3Sep25-FUT") is None

    def test_non_numeric_day_is_none(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-XXSep25-FUT") is None

    def test_non_numeric_year_is_none(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-30SepYY-FUT") is None

    def test_day_out_of_range_is_none(self) -> None:
        # 31 Feb is not a real date → ValueError caught → None.
        assert parse_future_symbol("NSE-NIFTY-31Feb25-FUT") is None

    def test_empty_underlying_is_none(self) -> None:
        # EXCH then an empty underlying part then expiry then FUT.
        assert parse_future_symbol("NSE--30Sep25-FUT") is None


# ── select_near_month ─────────────────────────────────────────────────────


class TestSelectNearMonth:
    def test_front_month_picked(self) -> None:
        items = [("FAR", date(2025, 12, 25)), ("NEAR", date(2025, 9, 30))]
        assert select_near_month(items, on_date=date(2025, 9, 1)) == "NEAR"

    def test_all_expired_is_none(self) -> None:
        items = [("A", date(2025, 1, 30)), ("B", date(2025, 2, 27))]
        assert select_near_month(items, on_date=date(2025, 9, 1)) is None

    def test_exact_on_date_is_live(self) -> None:
        # A contract expiring ON the snapshot date is still live (>=).
        items = [("TODAY", date(2025, 9, 30)), ("LATER", date(2025, 10, 30))]
        assert select_near_month(items, on_date=date(2025, 9, 30)) == "TODAY"

    def test_tie_breaks_on_symbol(self) -> None:
        # Two contracts sharing the earliest live expiry → deterministic symbol pick.
        items = [("ZZZ", date(2025, 9, 30)), ("AAA", date(2025, 9, 30))]
        assert select_near_month(items, on_date=date(2025, 9, 1)) == "AAA"


# ── FuturesMover ₹ rendering + compute_futures_mover ──────────────────────


def _mover(
    *,
    underlying: str = "NIFTY",
    symbol: str = "NSE-NIFTY-30Sep25-FUT",
    ltp: str = "100",
    spot: str | None = "98",
    premium: str | None = "2",
    pct: str | None = "5",
    oi: int | None = 1000,
    oi_chg_pct: str | None = "3",
    volume: int = 50,
) -> FuturesMover:
    """A FuturesMover with sensible defaults for the ranking / rendering tests."""
    return FuturesMover(
        underlying=underlying,
        symbol=symbol,
        exchange="NSE",
        segment="FNO",
        ltp=Decimal(ltp),
        spot=Decimal(spot) if spot is not None else None,
        premium=Decimal(premium) if premium is not None else None,
        pct_change=Decimal(pct) if pct is not None else None,
        oi=oi,
        oi_chg_pct=Decimal(oi_chg_pct) if oi_chg_pct is not None else None,
        volume=volume,
    )


class TestFuturesMoverRendering:
    def test_inr_strings_present(self) -> None:
        m = _mover(ltp="23650", spot="23622.9", premium="27.1")
        assert m.ltp_inr == "₹23,650.00"
        assert m.spot_inr == "₹23,622.90"
        assert m.premium_inr == "₹27.10"

    def test_negative_premium_renders(self) -> None:
        m = _mover(ltp="100", spot="105", premium="-5")
        # format_inr prefixes ₹ then the signed amount (negative premium = discount).
        assert m.premium_inr == "₹-5.00"

    def test_none_spot_and_premium_render_none(self) -> None:
        m = _mover(spot=None, premium=None)
        assert m.spot_inr is None
        assert m.premium_inr is None


class TestComputeFuturesMover:
    def test_full_row(self) -> None:
        m = compute_futures_mover(
            underlying="NIFTY",
            symbol="NSE-NIFTY-30Sep25-FUT",
            exchange="NSE",
            ltp=Decimal("110"),
            volume=70,
            oi=1100,
            prev_close=Decimal("100"),
            spot_ltp=Decimal("108"),
            prev_oi=1000,
        )
        assert m.ltp == Decimal("110.00")
        assert m.spot == Decimal("108.00")
        assert m.premium == Decimal("2.00")
        assert m.pct_change == Decimal("10")
        assert m.oi_chg_pct == Decimal("10")
        assert m.segment == "FNO"

    def test_none_baselines(self) -> None:
        # No prev_close → pct None; no spot → premium None; no prev_oi → oi% None.
        m = compute_futures_mover(
            underlying="X",
            symbol="NSE-X-30Sep25-FUT",
            exchange="NSE",
            ltp=Decimal("100"),
            volume=0,
            oi=500,
            prev_close=None,
            spot_ltp=None,
            prev_oi=None,
        )
        assert m.pct_change is None
        assert m.premium is None
        assert m.spot is None
        assert m.oi_chg_pct is None

    def test_zero_prev_oi_yields_none(self) -> None:
        m = compute_futures_mover(
            underlying="X",
            symbol="NSE-X-30Sep25-FUT",
            exchange="NSE",
            ltp=Decimal("100"),
            volume=0,
            oi=500,
            prev_close=Decimal("90"),
            spot_ltp=Decimal("99"),
            prev_oi=0,
        )
        assert m.oi_chg_pct is None

    def test_none_current_oi_yields_none_oi_pct(self) -> None:
        m = compute_futures_mover(
            underlying="X",
            symbol="NSE-X-30Sep25-FUT",
            exchange="NSE",
            ltp=Decimal("100"),
            volume=0,
            oi=None,
            prev_close=Decimal("90"),
            spot_ltp=Decimal("99"),
            prev_oi=1000,
        )
        assert m.oi_chg_pct is None
        assert m.oi is None


# ── ranking functions ─────────────────────────────────────────────────────


class TestRanking:
    def test_price_gainers_and_losers(self) -> None:
        up = _mover(symbol="UP", pct="5")
        down = _mover(symbol="DOWN", pct="-3")
        flat = _mover(symbol="FLAT", pct="0")
        none = _mover(symbol="NONE", pct=None)
        pool = [up, down, flat, none]
        assert [m.symbol for m in rank_price_gainers(pool, 10)] == ["UP"]
        assert [m.symbol for m in rank_price_losers(pool, 10)] == ["DOWN"]

    def test_premium_and_discount(self) -> None:
        prem = _mover(symbol="PREM", premium="3")
        disc = _mover(symbol="DISC", premium="-2")
        flat = _mover(symbol="FLAT", premium="0")
        none = _mover(symbol="NONE", premium=None)
        pool = [prem, disc, flat, none]
        assert [m.symbol for m in rank_premium(pool, 10)] == ["PREM"]
        assert [m.symbol for m in rank_discount(pool, 10)] == ["DISC"]

    def test_volume(self) -> None:
        a = _mover(symbol="A", volume=100)
        b = _mover(symbol="B", volume=0)
        assert [m.symbol for m in rank_volume([a, b], 10)] == ["A"]

    def test_oi_gainers_and_losers(self) -> None:
        up = _mover(symbol="UP", oi_chg_pct="4")
        down = _mover(symbol="DOWN", oi_chg_pct="-1")
        flat = _mover(symbol="FLAT", oi_chg_pct="0")
        none = _mover(symbol="NONE", oi_chg_pct=None)
        pool = [up, down, flat, none]
        assert [m.symbol for m in rank_oi_gainers(pool, 10)] == ["UP"]
        assert [m.symbol for m in rank_oi_losers(pool, 10)] == ["DOWN"]

    def test_top_n_caps_and_orders_descending(self) -> None:
        big = _mover(symbol="BIG", pct="9")
        mid = _mover(symbol="MID", pct="5")
        small = _mover(symbol="SMALL", pct="1")
        assert [m.symbol for m in rank_price_gainers([small, big, mid], 2)] == ["BIG", "MID"]

    def test_tiebreak_is_deterministic(self) -> None:
        # Equal pct → ordered by symbol (the (symbol, exchange, segment) tiebreak).
        z = _mover(symbol="ZZZ", pct="5")
        a = _mover(symbol="AAA", pct="5")
        assert [m.symbol for m in rank_price_gainers([z, a], 10)] == ["AAA", "ZZZ"]


# ── build_futures_payload ─────────────────────────────────────────────────

_CATEGORY_KEYS = (
    "price_gainers",
    "price_losers",
    "premium",
    "discount",
    "volume",
    "oi_gainers",
    "oi_losers",
)


class TestBuildFuturesPayload:
    def test_all_category_keys_present(self) -> None:
        payload = build_futures_payload(
            [_mover()], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        for key in _CATEGORY_KEYS:
            assert key in payload
        assert payload["interval"] == "1day"
        assert payload["date"] == "2025-09-01"
        assert payload["instrument_count"] == 1
        assert payload["skipped_count"] == 0

    def test_row_fields_complete(self) -> None:
        m = _mover(ltp="100", spot="98", premium="2", pct="5", oi=1000, oi_chg_pct="3", volume=50)
        payload = build_futures_payload(
            [m], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        row = payload["price_gainers"][0]
        assert row["name"] == "NIFTY"
        assert row["ltp"] == "₹100.00"
        assert row["spot"] == "₹98.00"
        assert row["premium"] == "₹2.00"
        assert row["pct_change"] == "5"
        assert row["oi"] == 1000
        assert row["oi_chg_pct"] == "3"
        assert row["volume"] == 50

    def test_null_optional_row_fields(self) -> None:
        m = _mover(spot=None, premium=None, pct=None, oi=None, oi_chg_pct=None)
        payload = build_futures_payload(
            [m], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        row = payload["volume"][0]
        assert row["spot"] is None
        assert row["premium"] is None
        assert row["pct_change"] is None
        assert row["oi"] is None
        assert row["oi_chg_pct"] is None

    def test_empty_lake_note(self) -> None:
        payload = build_futures_payload(
            [], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O futures first" in payload["note"]

    def test_present_but_no_mover_note(self) -> None:
        # A future in the lake but every category empty (skipped-only) → "pick another".
        payload = build_futures_payload(
            [], interval="5minute", date="2025-09-01", time="09:20", top_n=5, skipped=2
        )
        assert payload["instrument_count"] == 2
        assert "none had a bar" in payload["note"]
        assert "09:20" in payload["note"]

    def test_happy_note_mentions_premium_and_previous_close(self) -> None:
        payload = build_futures_payload(
            [_mover()], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        assert "previous close" in payload["note"]
        assert "premium" in payload["note"]


# ── _prev_day_oi (filesystem None branches over tmp_path) ──────────────────


def _day_bar(*, day: date, close: str, oi: int | None) -> CandleBar:
    """A 1-day OHLCV bar labelled at 09:15 IST (03:45 UTC) on *day*, with OI."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=1,
        open_interest=oi,
    )


class TestPrevDayOi:
    def test_no_1day_dir_is_none(self, tmp_path: Path) -> None:
        assert (
            _prev_day_oi(
                tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY-30Sep25-FUT",
                day=date(2025, 9, 1),
            )
            is None
        )

    def test_prior_day_oi_returned(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Sep25-FUT",
            interval="1day",
            bars=[
                _day_bar(day=date(2025, 8, 28), close="100", oi=900),
                _day_bar(day=date(2025, 8, 29), close="101", oi=1234),
            ],
        )
        got = _prev_day_oi(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Sep25-FUT",
            day=date(2025, 9, 1),
        )
        assert got == 1234

    def test_only_same_day_has_no_prior(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Sep25-FUT",
            interval="1day",
            bars=[_day_bar(day=date(2025, 9, 1), close="100", oi=500)],
        )
        assert (
            _prev_day_oi(
                tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY-30Sep25-FUT",
                day=date(2025, 9, 1),
            )
            is None
        )

    def test_prior_day_with_null_oi_is_none(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Sep25-FUT",
            interval="1day",
            bars=[_day_bar(day=date(2025, 8, 29), close="100", oi=None)],
        )
        assert (
            _prev_day_oi(
                tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY-30Sep25-FUT",
                day=date(2025, 9, 1),
            )
            is None
        )
