"""Unit tests for the ``/db`` UX-pack backend (DEC-DB-UX-PACK-001).

Covers the three BACKEND features of the pack against :mod:`brutex.dashboard.db_view`
+ :mod:`brutex.dashboard.fno_cascade`, offline + hermetic (a plain DuckDB connection
+ real Parquet partitions written by the production writers; no FastAPI, no network):

* **Feature 3** — the DERIVED-1day date filter off-by-one: a daily bar stamped
  midnight-IST is stored as the PREVIOUS calendar day 18:30 UTC, so the old UTC-date
  bound returned the neighbour day. ``_date_bounds`` now compares the IST calendar
  date for ``1day`` and keeps the byte-identical UTC date for every intraday interval.
* **Feature 2** — the honest applied-range chip metadata: an empty operator input is
  filled with the true DEFAULT (SPOT ``[2020-01-01, today]``; an F&O contract its FULL
  STORED LIFE), never "today"; every returned row lies inside the selected range.
* **Feature 6** — the per-expiry "listed N with data M" cascade counts.

Each behavioural fix has a fails-before / passes-after assertion and the
spec-mandated permutations (empty lake, single bar, dup-poisoned partition must-not-500,
tz/midnight-IST boundary, page boundaries ascending AND descending).
"""

# Whitebox: the date-bound / span / default-range helpers are private — imported
# directly so each hard-to-reach branch is pinned (precedent: test_dashboard_db_fno_moneyness).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest
from brutex.dashboard import db_view
from brutex.dashboard.db_view import (
    _DAILY_INTERVAL,
    _IST_DATE_EXPR,
    _SPOT_FILTER_FLOOR_ISO,
    _UTC_DATE_EXPR,
    _date_bounds,
    _fno_stored_ist_span_resilient,
    _iso_date_or_none,
    _today_ist_iso,
    candle_count_key,
    candle_span_key,
    list_fno_candle_datasets,
    option_candle_count_key,
    option_candle_span_key,
    query_candles,
    query_option_candles,
    resolve_applied_range,
)
from brutex.dashboard.fno_cascade import build_expiry_counts, build_fno_cascade
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.dashboard.pull_controller import GROWW_HISTORY_FLOOR
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.data.parquet_writer import ParquetWriter

# ────────────────────────────────────────────────────────────────────────
# Harness
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    """A plain DuckDB connection (the reader is session-``TimeZone`` independent)."""
    return duckdb.connect()


def _bar(ts: datetime, close: str = "100.50") -> CandleBar:
    """One OHLCV bar at *ts* (a tz-aware UTC instant)."""
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


#: Fixed +05:30 IST offset (India has no DST — CLAUDE.md §6).
_IST_OFFSET = timedelta(hours=5, minutes=30)


def _midnight_ist_utc(d: date) -> datetime:
    """The UTC instant of midnight-IST on *d* — how the lake stores a derived 1day bar.

    Midnight-IST of ``d`` = ``d 00:00 +05:30`` = the PREVIOUS calendar day ``18:30`` UTC
    (fixed +05:30, no DST). This is exactly what makes the naive UTC-date filter off by
    one (DEC-DB-UX-PACK-001 Feature 3).
    """
    return datetime(d.year, d.month, d.day, 0, 0, tzinfo=UTC) - _IST_OFFSET


def _write_spot_daily(lake: Path, ist_days: list[date], symbol: str = "NSE-NIFTY") -> None:
    """Write derived 1day bars (one per IST day, stamped midnight-IST → prev-day 18:30 UTC)."""
    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval="1day",
        bars=[_bar(_midnight_ist_utc(d)) for d in ist_days],
    )


def _write_spot_1min(lake: Path, instants: list[datetime], symbol: str = "NSE-NIFTY") -> None:
    """Write 1-minute spot bars at the given tz-aware instants."""
    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval="1minute",
        bars=[_bar(t) for t in instants],
    )


def _write_fno_1min(lake: Path, instants: list[datetime], symbol: str) -> None:
    """Write 1-minute F&O candle bars (segment=FNO) at the given instants."""
    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment="FNO",
        symbol=symbol,
        interval="1minute",
        bars=[_bar(t) for t in instants],
    )


_OPT_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"


def _opt_coord() -> OptionSeriesCoordinate:
    return OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment="NSE_FNO",
        security_id=13,
        underlying_symbol="NIFTY",
        expiry_flag="WEEK",
        expiry_code=0,
        strike_offset="ATM",
    )


def _obar(ts: datetime, side: str) -> OptionBar:
    return OptionBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("17500.00"),
        spot=Decimal("17480.00"),
        iv=Decimal("0.1523"),
    )


def _write_options(lake: Path, instants: list[datetime]) -> None:
    """Write a CE+PE option pair at each instant via the real option writer."""
    bars: list[OptionBar] = []
    for t in instants:
        bars.append(_obar(t, "CE"))
        bars.append(_obar(t, "PE"))
    OptionParquetWriter(lake_root=lake).write(
        partition_key="NSE_FNO|13|WEEK|0|ATM|CALL", coordinate=_opt_coord(), bars=bars
    )


_CORRUPT = b"not-a-parquet \r corrupt"


def _drop_corrupt(directory: Path, name: str = "corrupt.parquet") -> None:
    """Poison a partition with a structurally-broken ``*.parquet`` (dup-train blast)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(_CORRUPT)


def _ts_dates(payload: dict) -> list[str]:
    """The rendered ``timestamp`` cell's DATE portion (``YYYY-MM-DD``) for every page row."""
    ts_idx = payload["columns"].index("timestamp")
    return [str(row[ts_idx])[:10] for row in payload["rows"]]


# ════════════════════════════════════════════════════════════════════════
# Feature 3 — the derived-1day date-filter off-by-one
# ════════════════════════════════════════════════════════════════════════


def test_date_bounds_daily_uses_ist_calendar_date() -> None:
    clauses, params = _date_bounds("2026-02-03", "2026-02-03", interval=_DAILY_INTERVAL)
    assert all(_IST_DATE_EXPR in c for c in clauses)
    assert all(_UTC_DATE_EXPR not in c.replace(_IST_DATE_EXPR, "") for c in clauses)
    assert params == ["2026-02-03", "2026-02-03"]


def test_date_bounds_intraday_keeps_utc_calendar_date() -> None:
    # Intraday is byte-identical to the pre-fix behaviour (UTC date, no IST shift).
    clauses, params = _date_bounds("2026-02-03", None, interval="1minute")
    assert clauses == [f"{_UTC_DATE_EXPR} >= ?"]
    assert params == ["2026-02-03"]


def test_date_bounds_none_bounds_contribute_nothing() -> None:
    assert _date_bounds(None, None, interval=_DAILY_INTERVAL) == ([], [])
    # End-only, daily → one IST-anchored upper bound.
    clauses, params = _date_bounds(None, "2026-02-03", interval=_DAILY_INTERVAL)
    assert clauses == [f"{_IST_DATE_EXPR} <= ?"]
    assert params == ["2026-02-03"]


def test_query_candles_daily_filter_returns_the_selected_ist_day_not_the_neighbour(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # THE BUG (fails-before): filtering 2026-02-03 under the old UTC-date bound
    # returned the 02-04 row (stored 02-03 18:30 UTC) and DROPPED the 02-03 row
    # (stored 02-02 18:30 UTC). The IST-anchored bound returns exactly 02-03.
    _write_spot_daily(tmp_path, [date(2026, 2, 3), date(2026, 2, 4)])
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1day",
        start="2026-02-03",
        end="2026-02-03",
        limit=50,
    )
    assert out["total"] == 1
    assert _ts_dates(out) == ["2026-02-03"]


def test_query_candles_daily_filter_neighbour_day_isolated(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    _write_spot_daily(tmp_path, [date(2026, 2, 3), date(2026, 2, 4)])
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1day",
        start="2026-02-04",
        end="2026-02-04",
        limit=50,
    )
    assert out["total"] == 1
    assert _ts_dates(out) == ["2026-02-04"]


@pytest.mark.parametrize("session_tz", ["UTC", "Asia/Kolkata", "America/New_York"])
def test_query_candles_daily_filter_is_session_tz_independent(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path, session_tz: str
) -> None:
    conn.execute(f"SET TimeZone='{session_tz}'")
    _write_spot_daily(tmp_path, [date(2026, 2, 3), date(2026, 2, 4)])
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1day",
        start="2026-02-03",
        end="2026-02-03",
        limit=50,
    )
    assert _ts_dates(out) == ["2026-02-03"]


def test_query_candles_daily_range_only_returns_in_range_rows(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # Enforce-in-range for 1day: a [03, 05] window over six stored IST days returns
    # exactly {03, 04, 05} and NOTHING outside it (the neighbour-leak bug would add 06).
    # IST days 02..07 store as UTC 01..06 (fixed −05:30), all in the Feb partition.
    days = [date(2026, 2, d) for d in (2, 3, 4, 5, 6, 7)]
    _write_spot_daily(tmp_path, days)
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1day",
        start="2026-02-03",
        end="2026-02-05",
        limit=50,
    )
    got = sorted(_ts_dates(out))
    assert got == ["2026-02-03", "2026-02-04", "2026-02-05"]
    assert all("2026-02-03" <= d <= "2026-02-05" for d in got)


def test_query_candles_intraday_filter_unchanged(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # A real trading-hours bar (09:15 IST = 03:45 UTC) sits on the SAME UTC and IST
    # date, so intraday filtering is unchanged and every returned row is in range.
    _write_spot_1min(
        tmp_path,
        [datetime(2026, 2, 3, 3, 45, tzinfo=UTC), datetime(2026, 2, 4, 3, 45, tzinfo=UTC)],
    )
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        start="2026-02-03",
        end="2026-02-03",
        limit=50,
    )
    assert out["total"] == 1
    assert _ts_dates(out) == ["2026-02-03"]


# ════════════════════════════════════════════════════════════════════════
# Feature 2 — resolve_applied_range (pure) + helpers
# ════════════════════════════════════════════════════════════════════════


def test_spot_filter_floor_matches_pull_floor() -> None:
    # The local literal must track GROWW_HISTORY_FLOOR (a drift-guard, §11 COMMON).
    assert _SPOT_FILTER_FLOOR_ISO == GROWW_HISTORY_FLOOR.isoformat() == "2020-01-01"


def test_today_ist_iso_is_todays_ist_date() -> None:
    expected = datetime.now(tz=UTC).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
    assert _today_ist_iso() == expected


def test_iso_date_or_none_variants() -> None:
    assert _iso_date_or_none(None) is None
    assert _iso_date_or_none(date(2026, 2, 3)) == "2026-02-03"
    assert _iso_date_or_none("already-str") == "already-str"


def test_resolve_applied_range_spot_default_is_floor_to_today() -> None:
    r = resolve_applied_range(
        is_fno=False,
        explicit_start=None,
        explicit_end=None,
        stored_start_ist="2021-01-01",
        stored_end_ist="2021-06-01",
        today_ist="2026-07-03",
    )
    assert r == {"start": "2020-01-01", "end": "2026-07-03"}


def test_resolve_applied_range_fno_default_is_full_stored_life() -> None:
    r = resolve_applied_range(
        is_fno=True,
        explicit_start=None,
        explicit_end=None,
        stored_start_ist="2025-01-02",
        stored_end_ist="2025-01-30",
        today_ist="2026-07-03",
    )
    assert r == {"start": "2025-01-02", "end": "2025-01-30"}


def test_resolve_applied_range_fno_empty_partition_is_none() -> None:
    r = resolve_applied_range(
        is_fno=True,
        explicit_start=None,
        explicit_end=None,
        stored_start_ist=None,
        stored_end_ist=None,
        today_ist="2026-07-03",
    )
    assert r == {"start": None, "end": None}


def test_resolve_applied_range_explicit_bounds_win() -> None:
    r = resolve_applied_range(
        is_fno=False,
        explicit_start="2024-03-01",
        explicit_end="2024-03-31",
        stored_start_ist="2020-01-01",
        stored_end_ist="2026-01-01",
        today_ist="2026-07-03",
    )
    assert r == {"start": "2024-03-01", "end": "2024-03-31"}


def test_resolve_applied_range_defaults_each_side_independently() -> None:
    # Explicit "from" + blank "to" keeps the explicit start and the default end.
    spot = resolve_applied_range(
        is_fno=False,
        explicit_start="2024-03-01",
        explicit_end=None,
        stored_start_ist=None,
        stored_end_ist=None,
        today_ist="2026-07-03",
    )
    assert spot == {"start": "2024-03-01", "end": "2026-07-03"}
    fno = resolve_applied_range(
        is_fno=True,
        explicit_start=None,
        explicit_end="2025-01-30",
        stored_start_ist="2025-01-02",
        stored_end_ist="2025-06-01",
        today_ist="2026-07-03",
    )
    assert fno == {"start": "2025-01-02", "end": "2025-01-30"}


# ════════════════════════════════════════════════════════════════════════
# Feature 2 — query_candles / query_option_candles applied_range + enforcement
# ════════════════════════════════════════════════════════════════════════


def test_query_candles_spot_default_applied_range_floor_to_today(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    _write_spot_1min(tmp_path, [datetime(2024, 3, 1, 3, 45, tzinfo=UTC)])
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=50,
    )
    assert out["applied_range"] == {"start": "2020-01-01", "end": _today_ist_iso()}


def test_query_candles_spot_explicit_applied_range_echoes(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    _write_spot_1min(tmp_path, [datetime(2024, 3, 1, 3, 45, tzinfo=UTC)])
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-03-01",
        end="2024-03-01",
        limit=50,
    )
    assert out["applied_range"] == {"start": "2024-03-01", "end": "2024-03-01"}


def test_query_candles_fno_default_applied_range_full_stored_life(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # Two trading days → the F&O default range is the FULL STORED LIFE span (IST).
    _write_fno_1min(
        tmp_path,
        [datetime(2025, 1, 2, 3, 45, tzinfo=UTC), datetime(2025, 1, 3, 3, 45, tzinfo=UTC)],
        symbol="NSE-NIFTY-30Jan25-24000-CE",
    )
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-30Jan25-24000-CE",
        interval="1minute",
        limit=50,
    )
    assert out["applied_range"] == {"start": "2025-01-02", "end": "2025-01-03"}


def test_query_candles_fno_explicit_no_stored_scan(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-30Jan25-24000-CE"
    )
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-30Jan25-24000-CE",
        interval="1minute",
        start="2025-01-02",
        end="2025-01-02",
        limit=50,
    )
    assert out["applied_range"] == {"start": "2025-01-02", "end": "2025-01-02"}


def test_query_candles_single_bar_span_start_equals_end(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # Single-bar permutation: the full-stored-life span collapses to one day.
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-30Jan25-24000-CE"
    )
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-30Jan25-24000-CE",
        interval="1minute",
        limit=50,
    )
    assert out["total"] == 1
    assert out["applied_range"] == {"start": "2025-01-02", "end": "2025-01-02"}


def test_query_candles_empty_spot_partition_applied_range_floor_today(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # Empty-lake permutation (spot): no data, but the honest default range still shows.
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=50,
    )
    assert out["total"] == 0
    assert out["applied_range"] == {"start": "2020-01-01", "end": _today_ist_iso()}


def test_query_candles_empty_fno_partition_applied_range_none(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # Empty-lake permutation (F&O): no stored life → honest (None, None), never "today".
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-30Jan25-24000-CE",
        interval="1minute",
        limit=50,
    )
    assert out["total"] == 0
    assert out["applied_range"] == {"start": None, "end": None}


def test_query_candles_every_returned_row_inside_explicit_range(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    _write_spot_1min(
        tmp_path,
        [
            datetime(2024, 3, 1, 3, 45, tzinfo=UTC),
            datetime(2024, 3, 2, 3, 45, tzinfo=UTC),
            datetime(2024, 3, 3, 3, 45, tzinfo=UTC),
        ],
    )
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-03-02",
        end="2024-03-03",
        limit=50,
    )
    assert all("2024-03-02" <= d <= "2024-03-03" for d in _ts_dates(out))
    assert sorted(_ts_dates(out)) == ["2024-03-02", "2024-03-03"]


def test_query_option_candles_default_applied_range_full_life(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    _write_options(
        tmp_path,
        [datetime(2024, 3, 1, 4, 0, tzinfo=UTC), datetime(2024, 3, 2, 4, 0, tzinfo=UTC)],
    )
    out = query_option_candles(conn, tmp_path, series=_OPT_SERIES, limit=50)
    assert out["applied_range"] == {"start": "2024-03-01", "end": "2024-03-02"}


def test_query_option_candles_explicit_applied_range_echoes(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    _write_options(tmp_path, [datetime(2024, 3, 1, 4, 0, tzinfo=UTC)])
    out = query_option_candles(
        conn, tmp_path, series=_OPT_SERIES, start="2024-03-01", end="2024-03-01", limit=50
    )
    assert out["applied_range"] == {"start": "2024-03-01", "end": "2024-03-01"}


def test_query_option_candles_empty_series_applied_range_none(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    out = query_option_candles(conn, tmp_path, series=_OPT_SERIES, limit=50)
    assert out["total"] == 0
    assert out["applied_range"] == {"start": None, "end": None}


def test_query_option_candles_every_returned_row_inside_range(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    _write_options(
        tmp_path,
        [datetime(2024, 3, 1, 4, 0, tzinfo=UTC), datetime(2024, 3, 2, 4, 0, tzinfo=UTC)],
    )
    out = query_option_candles(
        conn, tmp_path, series=_OPT_SERIES, start="2024-03-02", end="2024-03-02", limit=50
    )
    assert out["total"] == 2  # only the 2nd-day CE+PE
    assert all(d == "2024-03-02" for d in _ts_dates(out))


# ════════════════════════════════════════════════════════════════════════
# Feature 2 — _fno_stored_ist_span_resilient + dup-poisoned must-not-500
# ════════════════════════════════════════════════════════════════════════


def _fno_base(lake: Path, symbol: str, interval: str = "1minute") -> Path:
    return lake / "bars" / "NSE" / "FNO" / symbol / interval


def test_fno_span_good_data(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    _write_fno_1min(
        tmp_path,
        [datetime(2025, 1, 2, 3, 45, tzinfo=UTC), datetime(2025, 1, 6, 3, 45, tzinfo=UTC)],
        symbol="NSE-NIFTY-30Jan25-24000-CE",
    )
    base = _fno_base(tmp_path, "NSE-NIFTY-30Jan25-24000-CE")
    lo, hi = _fno_stored_ist_span_resilient(
        conn, base, source="read_parquet(?, union_by_name=true)", skip_source="t"
    )
    assert (lo, hi) == ("2025-01-02", "2025-01-06")


def test_fno_span_dup_poisoned_only_degrades_to_none(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # A partition whose ONLY file is corrupt → (None, None), never a raise (Golden Rule #1).
    base = _fno_base(tmp_path, "NSE-NIFTY-30Jan25-24000-CE")
    _drop_corrupt(base)
    assert _fno_stored_ist_span_resilient(
        conn, base, source="read_parquet(?, union_by_name=true)", skip_source="t"
    ) == (None, None)


def test_fno_span_mixed_good_and_corrupt_skips_corrupt(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-30Jan25-24000-CE"
    )
    base = _fno_base(tmp_path, "NSE-NIFTY-30Jan25-24000-CE")
    _drop_corrupt(base)
    lo, hi = _fno_stored_ist_span_resilient(
        conn, base, source="read_parquet(?, union_by_name=true)", skip_source="t"
    )
    assert (lo, hi) == ("2025-01-02", "2025-01-02")


def test_query_candles_dup_poisoned_partition_does_not_500(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # Dup-poisoned permutation end-to-end: a good file + a corrupt file in the SAME
    # F&O partition — the page reads the good rows, the span degrades gracefully, and
    # NOTHING 500s (the resilient read guards already exist — DEC-LAKE-WRITE-UNIQUE-001).
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-30Jan25-24000-CE"
    )
    _drop_corrupt(_fno_base(tmp_path, "NSE-NIFTY-30Jan25-24000-CE"))
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-30Jan25-24000-CE",
        interval="1minute",
        limit=50,
    )
    assert out["total"] == 1
    assert out["applied_range"] == {"start": "2025-01-02", "end": "2025-01-02"}


def test_query_candles_dup_poisoned_only_file_does_not_500(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    base = _fno_base(tmp_path, "NSE-NIFTY-30Jan25-24000-CE")
    _drop_corrupt(base)
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-30Jan25-24000-CE",
        interval="1minute",
        limit=50,
    )
    assert out["total"] == 0
    assert out["applied_range"] == {"start": None, "end": None}


def test_query_candles_dup_row_poisoned_partition_renders_not_500(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # A LEGACY dup-poisoned partition — two rows at the SAME timestamp, the
    # pre-DEC-LAKE-WRITE-UNIQUE-001 vendor-duplicate shape (the production writer
    # now dedups keep-first, so the file is written RAW here to mimic the legacy
    # poison). The read-only /db viewer must never 500 — and since main's
    # DEC-LAKE-READ-UNIQUE-001 read-side dedup chokepoint the page collapses the
    # poison keep-FIRST (ONE row; the total counts DISTINCT timestamps), so the
    # viewer serves exactly what the deduped readers serve; the date filter +
    # applied range stay correct.
    import polars as pl

    part_dir = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute" / "2024"
    part_dir.mkdir(parents=True)
    ts = datetime(2024, 3, 1, 3, 45, tzinfo=UTC)
    pl.DataFrame(
        {
            "timestamp": [ts, ts],  # the dup poison
            "open": [100.0, 100.0],
            "high": [101.0, 101.0],
            "low": [99.0, 99.0],
            "close": [100.5, 100.5],
            "volume": [10, 10],
            "open_interest": [None, None],
        },
        schema={
            "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "open_interest": pl.Int64,
        },
    ).write_parquet(part_dir / "03.parquet")
    out = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-03-01",
        end="2024-03-01",
        limit=50,
    )
    assert out["total"] == 1
    assert _ts_dates(out) == ["2024-03-01"]
    assert out["applied_range"] == {"start": "2024-03-01", "end": "2024-03-01"}


# ════════════════════════════════════════════════════════════════════════
# Feature 2 — page boundaries ascending AND descending
# ════════════════════════════════════════════════════════════════════════


def test_query_candles_page_boundaries_ascending_and_descending(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    instants = [datetime(2024, 3, 1, 3, 45 + i, tzinfo=UTC) for i in range(5)]
    _write_spot_1min(tmp_path, instants)
    # Ascending page: first two bars; the applied range is stable across pages.
    asc0 = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=2,
        offset=0,
    )
    asc1 = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=2,
        offset=2,
    )
    assert asc0["total"] == asc1["total"] == 5
    assert asc0["applied_range"] == asc1["applied_range"]
    ts0 = [r[asc0["columns"].index("timestamp")] for r in asc0["rows"]]
    ts1 = [r[asc1["columns"].index("timestamp")] for r in asc1["rows"]]
    assert ts0 == sorted(ts0) and ts1 == sorted(ts1)
    assert max(ts0) <= min(ts1)  # page 0 precedes page 1 ascending
    # Descending page boundary: first page holds the latest bars, sorted DESC.
    desc0 = query_candles(
        conn,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        descending=True,
        limit=2,
        offset=0,
    )
    dts0 = [r[desc0["columns"].index("timestamp")] for r in desc0["rows"]]
    assert dts0 == sorted(dts0, reverse=True)
    assert min(dts0) >= max(ts1)  # newest bars first descending


# ════════════════════════════════════════════════════════════════════════
# Feature 6 — build_expiry_counts (pure)
# ════════════════════════════════════════════════════════════════════════


def _cascade(lake: Path) -> object:
    return build_fno_cascade(list_fno_candle_datasets(lake))


def test_build_expiry_counts_listed_none_when_registry_absent(tmp_path: Path) -> None:
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-02Jan25-24000-CE"
    )
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-02Jan25-24000-PE"
    )
    idx = _cascade(tmp_path)
    rows = build_expiry_counts(idx, "NIFTY", None)  # type: ignore[arg-type]
    assert rows == [{"expiry": "2025-01-02", "listed": None, "with_data": 2}]


def test_build_expiry_counts_young_chain_listed_exceeds_with_data(tmp_path: Path) -> None:
    # Only ONE strike has candle data, but the vendor listed THREE → "listed 3 with data 1".
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-02Jan25-24000-CE"
    )
    idx = _cascade(tmp_path)
    rows = build_expiry_counts(idx, "NIFTY", {"2025-01-02": 3})  # type: ignore[arg-type]
    assert rows == [{"expiry": "2025-01-02", "listed": 3, "with_data": 1}]


def test_build_expiry_counts_listed_equals_with_data(tmp_path: Path) -> None:
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-02Jan25-24000-CE"
    )
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-02Jan25-24000-PE"
    )
    idx = _cascade(tmp_path)
    rows = build_expiry_counts(idx, "NIFTY", {"2025-01-02": 2})  # type: ignore[arg-type]
    assert rows == [{"expiry": "2025-01-02", "listed": 2, "with_data": 2}]


def test_build_expiry_counts_unknown_underlying_is_empty(tmp_path: Path) -> None:
    _write_fno_1min(
        tmp_path, [datetime(2025, 1, 2, 3, 45, tzinfo=UTC)], symbol="NSE-NIFTY-02Jan25-24000-CE"
    )
    idx = _cascade(tmp_path)
    assert build_expiry_counts(idx, "NOPE", {"2025-01-02": 3}) == []  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════════════
# Feature 2 — stored-life SPAN memoised behind the count cache (the
# hostile-review MAJOR: a prev/next paging click must be an O(1) hit,
# never a re-run of the MIN/MAX partition scan)
# ════════════════════════════════════════════════════════════════════════


def test_span_cache_keys_are_collision_free() -> None:
    span = candle_span_key("NSE", "FNO", "NSE-NIFTY-30Jan25-24000-CE", "1minute")
    optspan = option_candle_span_key(_OPT_SERIES)
    count = candle_count_key("NSE", "FNO", "NSE-NIFTY-30Jan25-24000-CE", "1minute", None, None)
    optcount = option_candle_count_key(_OPT_SERIES, None, None)
    assert span == "span|NSE|FNO|NSE-NIFTY-30Jan25-24000-CE|1minute"
    assert optspan == f"optspan|{_OPT_SERIES}"
    # All four key families share ONE injected cache instance — no collisions:
    # a spot/F&O count key opens with a validated exchange token (NSE/BSE),
    # an option count key with "opt|", the span keys with "span|"/"optspan|".
    assert len({span, optspan, count, optcount}) == 4
    assert count.startswith("NSE|")
    assert optcount.startswith("opt|")


def test_query_candles_fno_span_memoised_across_paging(
    monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # THE MAJOR (fails-before): every default-bounded paging click re-ran the
    # MIN/MAX stored-life scan. With the cache injected, the scan runs ONCE per
    # (coords, generation) — offset paging + a sort flip are all O(1) hits.
    _write_fno_1min(
        tmp_path,
        [datetime(2025, 1, 2, 3, 45, tzinfo=UTC), datetime(2025, 1, 6, 3, 45, tzinfo=UTC)],
        symbol="NSE-NIFTY-30Jan25-24000-CE",
    )
    calls = {"n": 0}
    real_span = db_view._fno_stored_ist_span_resilient

    def _counting_span(*args: object, **kwargs: object) -> tuple[str | None, str | None]:
        calls["n"] += 1
        return real_span(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db_view, "_fno_stored_ist_span_resilient", _counting_span)
    cache = MoversSnapshotCache()

    def _page(offset: int, descending: bool = False, generation: int = 7) -> dict:
        return query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jan25-24000-CE",
            interval="1minute",
            limit=1,
            offset=offset,
            descending=descending,
            count_cache=cache,
            generation=generation,
        )

    first = _page(0)
    second = _page(1)
    flipped = _page(0, descending=True)
    assert calls["n"] == 1  # ONE scan across three default-bounded clicks
    expected = {"start": "2025-01-02", "end": "2025-01-06"}
    assert first["applied_range"] == second["applied_range"] == expected
    assert flipped["applied_range"] == expected
    # A pull bumps the generation → the span is re-scanned (never stale).
    bumped = _page(0, generation=8)
    assert calls["n"] == 2
    assert bumped["applied_range"] == expected


class _SpanCountingConn:
    """A delegating DuckDB-connection proxy counting MIN/MAX span scans."""

    def __init__(self, inner: duckdb.DuckDBPyConnection) -> None:
        self._inner = inner
        self.span_scans = 0

    def execute(self, sql: str, params: list[object] | None = None) -> object:
        if "MIN(" in sql:
            self.span_scans += 1
        return self._inner.execute(sql, params)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_query_option_candles_span_memoised_across_paging(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # The expired-options twin of the F&O span memo: paging a default-bounded
    # series runs the MIN/MAX scan once; a generation bump re-scans.
    _write_options(
        tmp_path,
        [datetime(2024, 3, 1, 4, 0, tzinfo=UTC), datetime(2024, 3, 2, 4, 0, tzinfo=UTC)],
    )
    counting = _SpanCountingConn(conn)
    cache = MoversSnapshotCache()

    def _page(offset: int, generation: int = 3) -> dict:
        return query_option_candles(
            counting,
            tmp_path,
            series=_OPT_SERIES,
            limit=1,
            offset=offset,
            count_cache=cache,
            generation=generation,
        )

    first = _page(0)
    second = _page(1)
    assert counting.span_scans == 1  # ONE scan across two default-bounded clicks
    expected = {"start": "2024-03-01", "end": "2024-03-02"}
    assert first["applied_range"] == second["applied_range"] == expected
    bumped = _page(0, generation=4)
    assert counting.span_scans == 2
    assert bumped["applied_range"] == expected


def test_query_option_candles_explicit_bounds_skip_span_scan_with_cache(
    conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    # An explicit range needs NO stored-life scan even with a cache injected —
    # the chip echoes the explicit bounds.
    _write_options(tmp_path, [datetime(2024, 3, 1, 4, 0, tzinfo=UTC)])
    counting = _SpanCountingConn(conn)
    out = query_option_candles(
        counting,
        tmp_path,
        series=_OPT_SERIES,
        start="2024-03-01",
        end="2024-03-01",
        limit=50,
        count_cache=MoversSnapshotCache(),
        generation=1,
    )
    assert counting.span_scans == 0
    assert out["applied_range"] == {"start": "2024-03-01", "end": "2024-03-01"}
