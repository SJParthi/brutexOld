"""Tests for the prev-day OHLCV reference layer (DEC-LIVE-PREVDAY-OHLCV-001).

Covers:

* previous-completed-trading-day resolution across a weekend and a holiday;
* a present instrument returns its 2dp-snapped OHLCV;
* a missing instrument / missing day is OMITTED (never raised);
* a malformed instrument key is skipped;
* an empty daily lake yields ``{}`` + a schema-correct empty Parquet;
* idempotent rewrite (byte-identical on rerun);
* ``prev_close`` equals the prior session's close;
* volume is NOT rounded (raw integer passthrough);
* ``open_interest`` ``null`` passthrough for non-FNO + integer OI for FNO;
* the ``resolve_previous=False`` direct-date read path.

The historical lake is seeded via the PRODUCTION :class:`ParquetWriter` (the
real write path — 2dp snap + canonical schema), so the read path is exercised
against a faithful lake. ``brutex.live.prevday`` never imports the dashboard
(DEC-LIVE-DECOUPLE-001).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.live.prevday import (
    PREVDAY_SCHEMA,
    PrevDayOHLCV,
    load_prev_day,
    previous_trading_day,
    read_prev_day,
    rows_to_frame,
    write_prev_day,
)

# A NIFTY index instrument keyed as the lake coordinate triple.
_NIFTY = "NSE:INDEX:NSE-NIFTY"
_COORDS = {"exchange": "NSE", "segment": "INDEX", "symbol": "NSE-NIFTY"}


def _seed_daily_bar(
    lake: Path,
    *,
    coords: dict[str, str],
    session_date: date,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: int,
    open_interest: int | None,
) -> None:
    """Write a single ``1day`` bar for *session_date* to the historical lake.

    The 1-day bar is stamped at 09:15 IST (03:45 UTC) of the session, exactly
    like a real spot daily candle, so the reader's IST-date resolution is tested.
    """
    timestamp = datetime(session_date.year, session_date.month, session_date.day, 3, 45, tzinfo=UTC)
    bar = CandleBar(
        timestamp=timestamp,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=open_interest,
    )
    ParquetWriter(lake_root=lake).write(
        exchange=coords["exchange"],
        segment=coords["segment"],
        symbol=coords["symbol"],
        interval="1day",
        bars=[bar],
    )


# ────────────────────────────────────────────────────────────────────────
# previous_trading_day resolution
# ────────────────────────────────────────────────────────────────────────


def test_previous_trading_day_back_across_weekend() -> None:
    """A Monday resolves back to the prior Friday (skips Sat + Sun)."""
    # 2026-06-15 is a Monday; the prior trading day is Friday 2026-06-12.
    assert previous_trading_day(date(2026, 6, 15)) == date(2026, 6, 12)


def test_previous_trading_day_simple_weekday() -> None:
    """A mid-week day resolves to the immediately preceding weekday."""
    # 2026-06-12 (Fri) → 2026-06-11 (Thu).
    assert previous_trading_day(date(2026, 6, 12)) == date(2026, 6, 11)


def test_previous_trading_day_back_across_holiday() -> None:
    """Resolution skips an NSE holiday (and the weekend behind it).

    2026-01-26 is Republic Day (Monday holiday in the calendar). The day after,
    2026-01-27 (Tuesday), must resolve PAST the holiday AND the prior weekend to
    Friday 2026-01-23.
    """
    assert previous_trading_day(date(2026, 1, 27)) == date(2026, 1, 23)


# ────────────────────────────────────────────────────────────────────────
# load_prev_day — present / missing / malformed
# ────────────────────────────────────────────────────────────────────────


def test_load_prev_day_present_instrument_returns_2dp_ohlcv(tmp_path: Path) -> None:
    """A present instrument returns its prior-session OHLCV snapped to 2dp."""
    lake = tmp_path / "lake"
    # Friday 2026-06-12 is the prior trading day for Monday 2026-06-15.
    # Vendor float-noise prices snap to 2dp at the write boundary.
    _seed_daily_bar(
        lake,
        coords=_COORDS,
        session_date=date(2026, 6, 12),
        open_="22500.005",  # → 22500.01 (ROUND_HALF_UP)
        high="22600.004",  # → 22600.00
        low="22400.10",
        close="22550.255",  # → 22550.26
        volume=123456,
        open_interest=None,
    )
    result = load_prev_day(lake, [_NIFTY], date(2026, 6, 15))
    assert set(result) == {_NIFTY}
    row = result[_NIFTY]
    assert row.session_date == date(2026, 6, 12)
    assert row.open == Decimal("22500.01")
    assert row.high == Decimal("22600.00")
    assert row.low == Decimal("22400.10")
    assert row.close == Decimal("22550.26")
    assert row.volume == 123456
    assert row.open_interest is None


def test_load_prev_day_missing_instrument_is_omitted(tmp_path: Path) -> None:
    """An instrument with no partition at all is omitted, never raised."""
    lake = tmp_path / "lake"
    _seed_daily_bar(
        lake,
        coords=_COORDS,
        session_date=date(2026, 6, 12),
        open_="100",
        high="100",
        low="100",
        close="100",
        volume=1,
        open_interest=None,
    )
    absent = "NSE:INDEX:NSE-BANKNIFTY"
    result = load_prev_day(lake, [_NIFTY, absent], date(2026, 6, 15))
    assert set(result) == {_NIFTY}  # the absent instrument is simply omitted


def test_load_prev_day_missing_day_is_omitted(tmp_path: Path) -> None:
    """A partition that exists but lacks the prior day's bar is omitted."""
    lake = tmp_path / "lake"
    # Seed ONLY 2026-06-10 (Wed); the prior trading day for Mon 06-15 is Fri
    # 06-12, which has no bar in this same-month partition.
    _seed_daily_bar(
        lake,
        coords=_COORDS,
        session_date=date(2026, 6, 10),
        open_="100",
        high="100",
        low="100",
        close="100",
        volume=1,
        open_interest=None,
    )
    result = load_prev_day(lake, [_NIFTY], date(2026, 6, 15))
    assert result == {}


def test_load_prev_day_malformed_key_is_skipped(tmp_path: Path) -> None:
    """An instrument key without the 3-part coordinate triple is skipped."""
    lake = tmp_path / "lake"
    _seed_daily_bar(
        lake,
        coords=_COORDS,
        session_date=date(2026, 6, 12),
        open_="100",
        high="100",
        low="100",
        close="100",
        volume=1,
        open_interest=None,
    )
    # "JUSTNIFTY" has no ":" → split_instrument_key returns ("", "", key).
    result = load_prev_day(lake, ["JUSTNIFTY", _NIFTY], date(2026, 6, 15))
    assert set(result) == {_NIFTY}


def test_load_prev_day_unknown_coordinate_is_skipped(tmp_path: Path) -> None:
    """A key with an unknown exchange/segment (partition rejects) is skipped."""
    lake = tmp_path / "lake"
    _seed_daily_bar(
        lake,
        coords=_COORDS,
        session_date=date(2026, 6, 12),
        open_="100",
        high="100",
        low="100",
        close="100",
        volume=1,
        open_interest=None,
    )
    # "MCX" is not a VALID_EXCHANGE → build_partition_path raises → omitted.
    bad = "MCX:CASH:GOLD"
    result = load_prev_day(lake, [bad, _NIFTY], date(2026, 6, 15))
    assert set(result) == {_NIFTY}


def test_load_prev_day_empty_lake_returns_empty_dict(tmp_path: Path) -> None:
    """A lake with no daily bars at all yields ``{}`` (honest degrade)."""
    lake = tmp_path / "lake"
    assert load_prev_day(lake, [_NIFTY], date(2026, 6, 15)) == {}


def test_load_prev_day_resolve_previous_false_reads_as_of_date(tmp_path: Path) -> None:
    """``resolve_previous=False`` reads *as_of_date* itself (no back-walk)."""
    lake = tmp_path / "lake"
    _seed_daily_bar(
        lake,
        coords=_COORDS,
        session_date=date(2026, 6, 12),
        open_="100",
        high="110",
        low="90",
        close="105",
        volume=7,
        open_interest=None,
    )
    # Read 2026-06-12 directly (not its previous trading day).
    result = load_prev_day(lake, [_NIFTY], date(2026, 6, 12), resolve_previous=False)
    assert set(result) == {_NIFTY}
    assert result[_NIFTY].session_date == date(2026, 6, 12)
    assert result[_NIFTY].close == Decimal("105.00")


def test_load_prev_day_fno_open_interest_passthrough(tmp_path: Path) -> None:
    """An FNO instrument's integer open_interest passes through unrounded."""
    lake = tmp_path / "lake"
    coords = {"exchange": "NSE", "segment": "FNO", "symbol": "NSE-RELIANCE-FUT"}
    _seed_daily_bar(
        lake,
        coords=coords,
        session_date=date(2026, 6, 12),
        open_="2500.00",
        high="2550.00",
        low="2490.00",
        close="2530.00",
        volume=9876,
        open_interest=543210,
    )
    key = "NSE:FNO:NSE-RELIANCE-FUT"
    result = load_prev_day(lake, [key], date(2026, 6, 15))
    assert result[key].open_interest == 543210
    assert result[key].volume == 9876


# ────────────────────────────────────────────────────────────────────────
# write_prev_day — schema / idempotence / prev_close / empty
# ────────────────────────────────────────────────────────────────────────


def test_write_prev_day_schema_and_prev_close(tmp_path: Path) -> None:
    """The written Parquet matches the schema; prev_close == close."""
    lake = tmp_path / "lake"
    row = PrevDayOHLCV(
        instrument_key=_NIFTY,
        session_date=date(2026, 6, 12),
        open=Decimal("22500.01"),
        high=Decimal("22600.00"),
        low=Decimal("22400.10"),
        close=Decimal("22550.26"),
        volume=123456,
        open_interest=None,
    )
    dest = write_prev_day(lake, [row], date(2026, 6, 15))
    assert dest == lake / "live" / "prevday" / "dt=2026-06-15.parquet"
    frame = pl.read_parquet(dest)
    assert frame.schema == pl.Schema(PREVDAY_SCHEMA)
    record = frame.row(0, named=True)
    assert record["instrument_key"] == _NIFTY
    assert record["session_date"] == date(2026, 6, 12)
    assert record["close"] == 22550.26
    assert record["prev_close"] == 22550.26  # prev_close mirrors the prior close
    assert record["volume"] == 123456


def test_write_prev_day_volume_not_rounded(tmp_path: Path) -> None:
    """Volume is stored as the raw integer (never money-rounded)."""
    lake = tmp_path / "lake"
    row = PrevDayOHLCV(
        instrument_key=_NIFTY,
        session_date=date(2026, 6, 12),
        open=Decimal("1.00"),
        high=Decimal("1.00"),
        low=Decimal("1.00"),
        close=Decimal("1.00"),
        volume=1_234_567,
        open_interest=None,
    )
    frame = read_prev_day_after_write(lake, row)
    assert frame.row(0, named=True)["volume"] == 1_234_567


def read_prev_day_after_write(lake: Path, row: PrevDayOHLCV) -> pl.DataFrame:
    """Helper: write a single row then read its partition back."""
    write_prev_day(lake, [row], date(2026, 6, 15))
    return read_prev_day(lake, date(2026, 6, 15))


def test_write_prev_day_is_idempotent_byte_identical(tmp_path: Path) -> None:
    """Re-writing the same inputs produces a byte-identical Parquet file."""
    lake = tmp_path / "lake"
    rows = [
        PrevDayOHLCV(
            instrument_key="NSE:INDEX:NSE-BANKNIFTY",
            session_date=date(2026, 6, 12),
            open=Decimal("48000.00"),
            high=Decimal("48500.00"),
            low=Decimal("47900.00"),
            close=Decimal("48200.00"),
            volume=10,
            open_interest=None,
        ),
        PrevDayOHLCV(
            instrument_key=_NIFTY,
            session_date=date(2026, 6, 12),
            open=Decimal("22500.01"),
            high=Decimal("22600.00"),
            low=Decimal("22400.10"),
            close=Decimal("22550.26"),
            volume=123456,
            open_interest=None,
        ),
    ]
    dest1 = write_prev_day(lake, rows, date(2026, 6, 15))
    first = dest1.read_bytes()
    # Rerun with the SAME rows in a DIFFERENT input order — sort-by-PK makes the
    # output order stable, so the bytes must match.
    dest2 = write_prev_day(lake, list(reversed(rows)), date(2026, 6, 15))
    assert dest2.read_bytes() == first


def test_write_prev_day_dedups_on_instrument_key(tmp_path: Path) -> None:
    """Duplicate keys collapse to the last occurrence (overwrite-by-PK)."""
    lake = tmp_path / "lake"
    base = PrevDayOHLCV(
        instrument_key=_NIFTY,
        session_date=date(2026, 6, 12),
        open=Decimal("1.00"),
        high=Decimal("2.00"),
        low=Decimal("1.00"),
        close=Decimal("1.50"),
        volume=1,
        open_interest=None,
    )
    newer = PrevDayOHLCV(
        instrument_key=_NIFTY,
        session_date=date(2026, 6, 12),
        open=Decimal("1.00"),
        high=Decimal("2.00"),
        low=Decimal("1.00"),
        close=Decimal("1.99"),
        volume=99,
        open_interest=None,
    )
    write_prev_day(lake, [base, newer], date(2026, 6, 15))
    frame = read_prev_day(lake, date(2026, 6, 15))
    assert frame.height == 1
    assert frame.row(0, named=True)["close"] == 1.99  # keep="last"


def test_write_prev_day_empty_rows_writes_schema_correct_empty(tmp_path: Path) -> None:
    """No rows → a schema-correct empty Parquet (never invents data)."""
    lake = tmp_path / "lake"
    dest = write_prev_day(lake, [], date(2026, 6, 15))
    frame = pl.read_parquet(dest)
    assert frame.height == 0
    assert frame.schema == pl.Schema(PREVDAY_SCHEMA)


# ────────────────────────────────────────────────────────────────────────
# read_prev_day / rows_to_frame
# ────────────────────────────────────────────────────────────────────────


def test_read_prev_day_absent_partition_returns_empty_schema_frame(tmp_path: Path) -> None:
    """Reading a never-written partition yields an empty schema frame."""
    lake = tmp_path / "lake"
    frame = read_prev_day(lake, date(2026, 6, 15))
    assert frame.height == 0
    assert frame.schema == pl.Schema(PREVDAY_SCHEMA)


def test_rows_to_frame_empty_is_schema_correct() -> None:
    """``rows_to_frame([])`` returns the empty canonical schema frame."""
    frame = rows_to_frame([])
    assert frame.height == 0
    assert frame.schema == pl.Schema(PREVDAY_SCHEMA)


def _seed_daily_bar_at(lake: Path, *, coords: dict[str, str], timestamp: datetime) -> None:
    """Write a single ``1day`` bar stamped at an EXPLICIT UTC *timestamp*.

    Used to exercise the IST-date resolution where the bar's UTC date differs
    from its IST trading date (a bar stamped at IST midnight = 18:30 UTC the
    prior calendar day).
    """
    bar = CandleBar(
        timestamp=timestamp,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=42,
        open_interest=None,
    )
    ParquetWriter(lake_root=lake).write(
        exchange=coords["exchange"],
        segment=coords["segment"],
        symbol=coords["symbol"],
        interval="1day",
        bars=[bar],
    )


def test_load_prev_day_cross_year_boundary(tmp_path: Path) -> None:
    """A prior trading day in the PRIOR month AND year reads the right partition.

    2026-01-01 (Thu) resolves to 2025-12-31 (Wed) — the partition is built from
    the RESOLVED session date (prior year/month), not ``as_of_date``.
    """
    lake = tmp_path / "lake"
    _seed_daily_bar(
        lake,
        coords=_COORDS,
        session_date=date(2025, 12, 31),
        open_="22000.00",
        high="22100.00",
        low="21900.00",
        close="22050.00",
        volume=555,
        open_interest=None,
    )
    result = load_prev_day(lake, [_NIFTY], date(2026, 1, 1))
    assert set(result) == {_NIFTY}
    assert result[_NIFTY].session_date == date(2025, 12, 31)
    assert result[_NIFTY].close == Decimal("22050.00")


def test_load_prev_day_ist_date_differs_from_utc_date(tmp_path: Path) -> None:
    """A bar stamped at IST midnight (18:30 UTC prior day) resolves by IST date.

    The bar for IST trading day 2026-06-12 is stamped 2026-06-11T18:30Z (00:00
    IST 2026-06-12). ``_bar_ist_date`` must pick the IST date 2026-06-12, NOT the
    UTC date 2026-06-11 — and the partition (built from session_date 2026-06-12)
    is the June partition the writer also chose (writer partitions by UTC, but
    18:30 UTC on 06-11 lands in June too, so both agree here).
    """
    lake = tmp_path / "lake"
    # 18:30 UTC on 2026-06-11 == 00:00 IST on 2026-06-12.
    _seed_daily_bar_at(
        lake,
        coords=_COORDS,
        timestamp=datetime(2026, 6, 11, 18, 30, tzinfo=UTC),
    )
    result = load_prev_day(lake, [_NIFTY], date(2026, 6, 15))
    assert set(result) == {_NIFTY}
    assert result[_NIFTY].session_date == date(2026, 6, 12)
    assert result[_NIFTY].close == Decimal("105.00")


def test_load_then_write_then_read_roundtrip(tmp_path: Path) -> None:
    """End-to-end: seed lake → load → write → read surfaces the same OHLCV."""
    lake = tmp_path / "lake"
    _seed_daily_bar(
        lake,
        coords=_COORDS,
        session_date=date(2026, 6, 12),
        open_="22500.01",
        high="22600.00",
        low="22400.10",
        close="22550.26",
        volume=123456,
        open_interest=None,
    )
    loaded = load_prev_day(lake, [_NIFTY], date(2026, 6, 15))
    write_prev_day(lake, loaded.values(), date(2026, 6, 15))
    frame = read_prev_day(lake, date(2026, 6, 15))
    record = frame.row(0, named=True)
    assert record["instrument_key"] == _NIFTY
    assert record["close"] == 22550.26
    assert record["prev_close"] == 22550.26
    assert record["session_date"] == date(2026, 6, 12)
