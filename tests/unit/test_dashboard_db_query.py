"""Unit tests for the structured-query + candle-viewer surface of
:mod:`brutex.dashboard.db_view` (DEC-DB-QUERY-001).

Offline + hermetic: ``query_table`` runs against a real
:class:`~brutex.data.instrument_store.InstrumentStore` (DuckDB, schema
auto-created); ``query_candles`` / ``list_candle_datasets`` run against
real Parquet partitions written by the production
:class:`~brutex.data.parquet_writer.ParquetWriter`. No FastAPI, no network.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from brutex.dashboard.db_view import (
    _IST_DATE_EXPR,
    _IST_TS_EXPR,
    _SESSION_OPEN_MINUTES,
    ALLOWED_OPERATORS,
    CANDLE_COLUMNS,
    FNO_GREEKS_COLUMN_TYPES,
    FNO_GREEKS_COLUMNS,
    OPTION_CANDLE_COLUMNS,
    SPOT_DERIVED_COLUMNS,
    _aggregate_parquet_resilient,
    _bar_ist_date,
    _bar_ist_time,
    _daily_close_by_date,
    _daily_open_by_date,
    _execute_candle_page_resilient,
    _execute_parquet_resilient,
    _format_candle_timestamp,
    _intraday_last_close_by_date,
    _intraday_open_by_date,
    _intraday_prev_close_by_page_date,
    _iso_date,
    _ist_date,
    _page_ist_dates,
    _prev_trading_close,
    _quote_ident,
    _readable_parquet_subset,
    _spot_derived_rows,
    _to_decimal,
    _validate_candle_coords,
    list_candle_datasets,
    list_columns,
    list_option_datasets,
    list_option_series,
    query_candles,
    query_option_candles,
    query_table,
    summarize_option_datasets,
    summarize_spot_datasets,
)
from brutex.data.instrument_store import Instrument, InstrumentStore
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path
from brutex.models import Exchange, InstrumentType, Segment
from structlog.testing import capture_logs

from brutex.data.models import (  # isort: skip  (kept near other data imports)
    GREEKS_OVERLAY_FIELDS,
    CandleBar,
    FnoGreeksBar,
    OptionBar,
)


# ────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────


def _idx(symbol: str, name: str) -> Instrument:
    """Minimal NSE index instrument-master row (mirrors the db_view tests)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(symbol) % 100_000,
        symbol_name=symbol,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> InstrumentStore:
    """A fresh store seeded with three index rows."""
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY", "NIFTY 50"), _idx("BANKNIFTY", "NIFTY BANK"), _idx("SENSEX", "BSE SENSEX")],
        snapshot_date=date.today(),
        run_id="test-seed",
        csv_source_sha256="0" * 64,
    )
    return s


def _bar(ts: datetime, close: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


def _write_candles(lake_root: Path, symbol: str = "NSE-NIFTY", interval: str = "1minute") -> None:
    """Write two single-day candle partitions (2024-03-01, 2024-03-02)."""
    writer = ParquetWriter(lake_root=lake_root)
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval=interval,
        bars=[
            _bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4"),
            _bar(datetime(2024, 3, 2, 9, 15, tzinfo=UTC), "100.8"),
        ],
    )


#: The filesystem-safe series segment the writer derives from this PK
#: (``|`` → ``__``). Used by the option-dataset + option-query tests.
_OPT_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"
_OPT_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"


def _obar(ts: datetime, side: str, close: str, *, iv: str = "0.1523") -> OptionBar:
    """One expired-option bar (full-precision iv, 2dp money like the writer)."""
    return OptionBar(
        timestamp=ts,
        open=Decimal("100.005"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal(close),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("17500.005"),
        spot=Decimal("17480.567"),
        iv=Decimal(iv),
    )


def _coord_for(partition_key: str, *, underlying_symbol: str = "NIFTY") -> OptionSeriesCoordinate:
    """Build a series coordinate from a ``|``-joined partition key (test helper)."""
    seg, sec, flag, code, offset, _typ = partition_key.split("|")
    return OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment=seg,
        security_id=int(sec),
        underlying_symbol=underlying_symbol,
        expiry_flag=flag,
        expiry_code=int(code),
        strike_offset=offset,
    )


_OPT_COORD = _coord_for(_OPT_PK)


def _write_options(
    lake_root: Path,
    partition_key: str = _OPT_PK,
    *,
    underlying_symbol: str = "NIFTY",
) -> None:
    """Write a CE+PE option pair across two trading days via the real writer."""
    OptionParquetWriter(lake_root=lake_root).write(
        partition_key=partition_key,
        coordinate=_coord_for(partition_key, underlying_symbol=underlying_symbol),
        bars=[
            _obar(datetime(2024, 3, 1, 4, 0, tzinfo=UTC), "CE", "105.004"),
            _obar(datetime(2024, 3, 1, 4, 0, tzinfo=UTC), "PE", "95.5"),
            _obar(datetime(2024, 3, 2, 4, 0, tzinfo=UTC), "CE", "106.2"),
            _obar(datetime(2024, 3, 2, 4, 0, tzinfo=UTC), "PE", "96.1"),
        ],
    )


#: A structurally-broken Parquet blob (no trailing ``PAR1`` magic → DuckDB
#: raises ``duckdb.Error`` on read). The embedded ``\r`` mirrors the operator's
#: real symptom (``don't know what type: \r``); the missing magic is what
#: deterministically triggers the resilient-read degradation across DuckDB
#: versions (DEC-COVERAGE-PARQUET-RESILIENT-001).
_CORRUPT_PARQUET = b"not-a-parquet \r corrupt"


def _drop_corrupt_parquet(directory: Path, name: str = "corrupt.parquet") -> Path:
    """Drop an unreadable ``*.parquet`` into *directory* (matches the coverage glob)."""
    directory.mkdir(parents=True, exist_ok=True)
    bad = directory / name
    bad.write_bytes(_CORRUPT_PARQUET)
    return bad


# ────────────────────────────────────────────────────────────────────────
# _quote_ident
# ────────────────────────────────────────────────────────────────────────


def test_quote_ident_wraps_plain_name() -> None:
    assert _quote_ident("trading_symbol") == '"trading_symbol"'


def test_quote_ident_rejects_embedded_doublequote() -> None:
    with pytest.raises(ValueError, match="illegal identifier"):
        _quote_ident('foo"; DROP TABLE x; --')


# ────────────────────────────────────────────────────────────────────────
# list_columns
# ────────────────────────────────────────────────────────────────────────


def test_list_columns_returns_real_columns(store: InstrumentStore) -> None:
    cols = list_columns(store.connection, "instruments")
    assert "symbol_name" in cols
    assert "exch_id" in cols


def test_list_columns_unknown_table_raises(store: InstrumentStore) -> None:
    with pytest.raises(ValueError, match="unknown table"):
        list_columns(store.connection, "definitely_not_a_table")


# ────────────────────────────────────────────────────────────────────────
# query_table — projection
# ────────────────────────────────────────────────────────────────────────


def test_query_table_all_columns_default(store: InstrumentStore) -> None:
    out = query_table(store.connection, "instruments", limit=50)
    assert out["total"] == 3
    assert "symbol_name" in out["columns"]
    assert len(out["rows"]) == 3


def test_query_table_specific_columns_in_order(store: InstrumentStore) -> None:
    out = query_table(
        store.connection,
        "instruments",
        columns=("symbol_name", "display_name"),
        limit=50,
    )
    assert out["columns"] == ["symbol_name", "display_name"]
    assert all(len(r) == 2 for r in out["rows"])


def test_query_table_empty_columns_tuple_raises(store: InstrumentStore) -> None:
    with pytest.raises(ValueError, match="at least one column"):
        query_table(store.connection, "instruments", columns=(), limit=50)


def test_query_table_unknown_column_raises(store: InstrumentStore) -> None:
    with pytest.raises(ValueError, match="unknown column"):
        query_table(store.connection, "instruments", columns=("nope",), limit=50)


def test_query_table_unknown_table_raises(store: InstrumentStore) -> None:
    with pytest.raises(ValueError, match="unknown table"):
        query_table(store.connection, "evil; DROP TABLE contracts", limit=50)


# ────────────────────────────────────────────────────────────────────────
# query_table — filters
# ────────────────────────────────────────────────────────────────────────


def test_query_table_eq_filter_narrows_and_total_reflects(store: InstrumentStore) -> None:
    out = query_table(
        store.connection,
        "instruments",
        filters=(("symbol_name", "eq", "NIFTY"),),
        limit=50,
    )
    assert out["total"] == 1
    assert len(out["rows"]) == 1


def test_query_table_contains_filter(store: InstrumentStore) -> None:
    # "contains" wraps the bound value in %…% — both NIFTY + BANKNIFTY match.
    out = query_table(
        store.connection,
        "instruments",
        filters=(("symbol_name", "contains", "NIFTY"),),
        limit=50,
    )
    assert out["total"] == 2


def test_query_table_like_filter_pattern(store: InstrumentStore) -> None:
    out = query_table(
        store.connection,
        "instruments",
        filters=(("symbol_name", "like", "BANK%"),),
        limit=50,
    )
    assert out["total"] == 1


@pytest.mark.parametrize("op", sorted(ALLOWED_OPERATORS))
def test_query_table_every_operator_executes(store: InstrumentStore, op: str) -> None:
    # Each allowed operator must build + run without error against a real
    # column. Value chosen so the comparison is valid for every operator.
    value = "NIFTY%" if op in {"like"} else "NIFTY"
    out = query_table(
        store.connection,
        "instruments",
        filters=(("symbol_name", op, value),),
        limit=50,
    )
    assert "total" in out


def test_query_table_unknown_filter_column_raises(store: InstrumentStore) -> None:
    with pytest.raises(ValueError, match="unknown filter column"):
        query_table(
            store.connection,
            "instruments",
            filters=(("nope", "eq", "x"),),
            limit=50,
        )


def test_query_table_unknown_operator_raises(store: InstrumentStore) -> None:
    with pytest.raises(ValueError, match="unknown operator"):
        query_table(
            store.connection,
            "instruments",
            filters=(("symbol_name", "regexp", "x"),),
            limit=50,
        )


# ────────────────────────────────────────────────────────────────────────
# query_table — order + paging
# ────────────────────────────────────────────────────────────────────────


def test_query_table_order_by_ascending(store: InstrumentStore) -> None:
    out = query_table(
        store.connection,
        "instruments",
        columns=("symbol_name",),
        order_by="symbol_name",
        limit=50,
    )
    symbols = [r[0] for r in out["rows"]]
    assert symbols == sorted(symbols)


def test_query_table_order_by_descending(store: InstrumentStore) -> None:
    out = query_table(
        store.connection,
        "instruments",
        columns=("symbol_name",),
        order_by="symbol_name",
        descending=True,
        limit=50,
    )
    symbols = [r[0] for r in out["rows"]]
    assert symbols == sorted(symbols, reverse=True)


def test_query_table_unknown_order_column_raises(store: InstrumentStore) -> None:
    with pytest.raises(ValueError, match="unknown order column"):
        query_table(store.connection, "instruments", order_by="nope", limit=50)


def test_query_table_paging_clamps_and_offsets(store: InstrumentStore) -> None:
    page = query_table(
        store.connection,
        "instruments",
        columns=("symbol_name",),
        order_by="symbol_name",
        limit=999_999,  # clamped to 500
        offset=-5,  # clamped to 0
    )
    assert page["limit"] == 500
    assert page["offset"] == 0
    assert page["total"] == 3
    # A 1-row window at offset 1 returns exactly one (the 2nd) row.
    window = query_table(
        store.connection,
        "instruments",
        columns=("symbol_name",),
        order_by="symbol_name",
        limit=1,
        offset=1,
    )
    assert window["total"] == 3
    assert len(window["rows"]) == 1


# ────────────────────────────────────────────────────────────────────────
# list_candle_datasets
# ────────────────────────────────────────────────────────────────────────


def test_list_candle_datasets_missing_bars_dir(tmp_path: Path) -> None:
    assert list_candle_datasets(tmp_path) == []


def test_list_candle_datasets_finds_written_partitions(tmp_path: Path) -> None:
    _write_candles(tmp_path, symbol="NSE-NIFTY", interval="1minute")
    _write_candles(tmp_path, symbol="NSE-BANKNIFTY", interval="5minute")
    found = list_candle_datasets(tmp_path)
    assert {(d["symbol"], d["interval"]) for d in found} == {
        ("NSE-NIFTY", "1minute"),
        ("NSE-BANKNIFTY", "5minute"),
    }


def test_list_candle_datasets_skips_foreign_and_empty_dirs(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    # Valid dataset.
    _write_candles(tmp_path, symbol="NSE-NIFTY", interval="1minute")
    # Foreign exchange dir (skipped).
    (bars / "MCX" / "CASH" / "GOLD" / "1minute").mkdir(parents=True)
    # Foreign segment dir (skipped).
    (bars / "NSE" / "JUNK" / "FOO" / "1minute").mkdir(parents=True)
    # Invalid interval dir (skipped).
    (bars / "NSE" / "CASH" / "BAR" / "45minute").mkdir(parents=True)
    # Valid interval dir but NO parquet inside (skipped — empty).
    (bars / "NSE" / "CASH" / "EMPTY" / "1minute").mkdir(parents=True)
    found = list_candle_datasets(tmp_path)
    assert found == [
        {
            "kind": "spot",
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
        }
    ]


# ────────────────────────────────────────────────────────────────────────
# query_candles
# ────────────────────────────────────────────────────────────────────────


def test_query_candles_returns_all_rows(store: InstrumentStore, tmp_path: Path) -> None:
    _write_candles(tmp_path)
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=50,
    )
    assert out["total"] == 2
    # The two server-computed derived %-columns are appended (DEC-DB-CANDLE-DERIVED-001).
    assert out["columns"] == [*CANDLE_COLUMNS, "change_pct", "open_gap_pct"]
    assert out["symbol"] == "NSE-NIFTY"


def test_query_candles_column_subset(store: InstrumentStore, tmp_path: Path) -> None:
    _write_candles(tmp_path)
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        columns=("timestamp", "close"),
        limit=50,
    )
    # Projection + the two appended derived %-columns (DEC-DB-CANDLE-DERIVED-001).
    assert out["columns"] == ["timestamp", "close", "change_pct", "open_gap_pct"]
    assert all(len(r) == 4 for r in out["rows"])


def test_query_candles_empty_columns_tuple_raises(store: InstrumentStore, tmp_path: Path) -> None:
    _write_candles(tmp_path)
    with pytest.raises(ValueError, match="at least one column"):
        query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            columns=(),
            limit=50,
        )


def test_query_candles_date_range_end_inclusive(store: InstrumentStore, tmp_path: Path) -> None:
    _write_candles(tmp_path)
    # start+end both on 2024-03-02 → only that day's bar (end day inclusive).
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-03-02",
        end="2024-03-02",
        limit=50,
    )
    assert out["total"] == 1


def test_query_candles_start_only(store: InstrumentStore, tmp_path: Path) -> None:
    _write_candles(tmp_path)
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-03-02",
        limit=50,
    )
    assert out["total"] == 1


def test_format_candle_timestamp_tz_aware_converts_to_ist() -> None:
    # A UTC TIMESTAMPTZ cell → IST wall-clock, no offset (03:30Z → 09:00 IST).
    assert (
        _format_candle_timestamp(datetime(2026, 5, 29, 3, 30, tzinfo=UTC), "1minute")
        == "2026-05-29 09:00:00"
    )


def test_format_candle_timestamp_daily_is_date_only() -> None:
    # For the 1day interval the bar renders DATE-ONLY — the 00:00:00 is noise
    # (DEC-DB-CANDLE-DERIVED-001). A daily bar at 18:30Z is 00:00 IST next day.
    assert (
        _format_candle_timestamp(datetime(2026, 5, 28, 18, 30, tzinfo=UTC), "1day") == "2026-05-29"
    )
    # A tz-aware mid-session daily stamp still collapses to its IST date.
    assert (
        _format_candle_timestamp(datetime(2026, 5, 29, 3, 30, tzinfo=UTC), "1day") == "2026-05-29"
    )


def test_format_candle_timestamp_naive_daily_is_date_only() -> None:
    # A naive datetime (defensive) under the 1day interval is also date-only.
    assert _format_candle_timestamp(datetime(2024, 3, 1, 0, 0, 0), "1day") == "2024-03-01"


def test_format_candle_timestamp_naive_datetime_kept_as_is() -> None:
    # Defensive branch: a naive datetime (DuckDB returns tz-aware in practice)
    # is formatted as-is, still without an offset suffix.
    assert (
        _format_candle_timestamp(datetime(2024, 3, 1, 14, 45, 0), "1minute")
        == "2024-03-01 14:45:00"
    )


def test_format_candle_timestamp_non_datetime_falls_back() -> None:
    # A non-datetime cell (never the real timestamp column) falls back to the
    # generic JSON coercion path, regardless of interval.
    assert _format_candle_timestamp(None, "1minute") is None
    assert _format_candle_timestamp("already-a-string", "1day") == "already-a-string"


def test_query_candles_timestamp_is_clean_ist_no_offset(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # The bar timestamp must render as a clean IST wall-clock string with NO
    # +05:30 offset suffix (operator: "it should be clean"). The fixture bars
    # are 09:15 UTC → 14:45 IST. Deterministic regardless of session TimeZone.
    _write_candles(tmp_path)
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        columns=("timestamp",),
        limit=50,
    )
    stamps = sorted(str(r[0]) for r in out["rows"])
    assert stamps == ["2024-03-01 14:45:00", "2024-03-02 14:45:00"]
    assert all("+" not in s and "T" not in s for s in stamps)


def test_query_candles_descending(store: InstrumentStore, tmp_path: Path) -> None:
    _write_candles(tmp_path)
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        columns=("timestamp",),
        descending=True,
        limit=50,
    )
    stamps = [r[0] for r in out["rows"]]
    assert stamps == sorted(stamps, reverse=True)


def test_query_candles_empty_partition_set_returns_empty_page(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # No parquet written → empty page, never raises.
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NOTPULLED",
        interval="1minute",
        columns=("timestamp", "close"),
        limit=50,
    )
    assert out["total"] == 0
    assert out["rows"] == []
    # Even an empty partition exposes the stable derived-column set.
    assert out["columns"] == ["timestamp", "close", "change_pct", "open_gap_pct"]


def test_query_candles_empty_partition_default_columns(
    store: InstrumentStore, tmp_path: Path
) -> None:
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NOTPULLED",
        interval="1minute",
        limit=50,
    )
    assert out["columns"] == [*CANDLE_COLUMNS, "change_pct", "open_gap_pct"]


@pytest.mark.parametrize(
    ("exchange", "segment", "symbol", "interval", "match"),
    [
        ("MCX", "CASH", "NSE-NIFTY", "1minute", "unknown exchange"),
        ("NSE", "JUNK", "NSE-NIFTY", "1minute", "unknown segment"),
        ("NSE", "CASH", "NSE-NIFTY", "45minute", "unknown interval"),
        ("NSE", "CASH", "../etc/passwd", "1minute", "illegal candle symbol"),
        ("NSE", "CASH", "bad symbol!", "1minute", "illegal candle symbol"),
    ],
)
def test_query_candles_invalid_coords_raise(
    store: InstrumentStore,
    tmp_path: Path,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        query_candles(
            store.connection,
            tmp_path,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            limit=50,
        )


# ── DEC-PREOPEN-AMPERSAND-001 — ``&`` symbols (M&M / M&MFIN / GVT&D) ──────────

#: Real NSE/BSE F&O-eligible underlyings whose ticker carries ``&``. The ingest
#: writes them verbatim (the partition denylist permits ``&``); before the fix
#: the read path 500'd on them (``illegal candle symbol 'M&M'``).
_AMPERSAND_SYMBOLS = ["M&M", "M&MFIN", "GVT&D"]


def test_query_candles_accepts_ampersand_symbol(store: InstrumentStore, tmp_path: Path) -> None:
    # M&M (Mahindra & Mahindra) is a real F&O underlying — the read path must
    # SERVE its candles, not raise ``illegal candle symbol`` (the 500 bug).
    _write_candles(tmp_path, symbol="M&M", interval="1minute")
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="M&M",
        interval="1minute",
        limit=50,
    )
    assert len(out["rows"]) == 2  # both written bars round-trip through the ``&`` path


@pytest.mark.parametrize("symbol", _AMPERSAND_SYMBOLS)
def test_ampersand_symbol_write_read_validators_agree(symbol: str) -> None:
    # CLAUDE.md §11 COMMON: the partition WRITE validator and the dashboard READ
    # validator must accept exactly the same real symbols — neither may drift
    # and reject what the other stored (the root cause of the M&M 500).
    build_partition_path(  # write side: must not raise
        lake_root=Path("/lake"),
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval="1day",
        year=2026,
        month=1,
    )
    _validate_candle_coords("NSE", "CASH", symbol, "1day")  # read side: must not raise


@pytest.mark.parametrize("bad", ["FOO[BAR]", "FOO{1}", "x*", "y?z"])
def test_validate_candle_coords_still_rejects_glob_metachars(bad: str) -> None:
    # Admitting ``&`` (DEC-PREOPEN-AMPERSAND-001) and a literal space
    # (DEC-PREOPEN-SPACE-SYMBOL-001) must NOT loosen the glob-injection guard:
    # the DuckDB-glob metacharacters ``* ? [ ] { }`` stay illegal so a symbol can
    # never widen the bound ``read_parquet`` glob (``..`` traversal is rejected
    # separately — see test_query_candles_invalid_coords_raise).
    with pytest.raises(ValueError, match="illegal candle symbol"):
        _validate_candle_coords("NSE", "CASH", bad, "1day")


@pytest.mark.parametrize("ok", ["INDIA VIX", "NIFTY 50", "NIFTY TOTAL MKT"])
def test_validate_candle_coords_accepts_space_symbol(ok: str) -> None:
    # DEC-PREOPEN-SPACE-SYMBOL-001: real stored INDEX underlyings carry a space.
    # A space is a bound-parameter literal path component (not a glob metachar,
    # not a separator), so the read allowlist admits it — the pre-open report no
    # longer 500s on 'INDIA VIX' / 'NIFTY 50' / 'NIFTY TOTAL MKT'.
    _validate_candle_coords("NSE", "CASH", ok, "1day")  # must NOT raise


@pytest.mark.parametrize("field", ["start", "end"])
def test_query_candles_malformed_date_raises(
    store: InstrumentStore, tmp_path: Path, field: str
) -> None:
    _write_candles(tmp_path)
    kwargs = {"start": None, "end": None} | {field: "2024/03/01"}
    with pytest.raises(ValueError, match=f"{field} must be ISO"):
        query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            limit=50,
            **kwargs,  # type: ignore[arg-type]
        )


def test_query_candles_paging_clamps(store: InstrumentStore, tmp_path: Path) -> None:
    _write_candles(tmp_path)
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=10**9,
        offset=-3,
    )
    assert out["limit"] == 500
    assert out["offset"] == 0


def test_query_candles_date_bound_is_utc_not_session_tz(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # A bar at 23:30 UTC on 2024-03-02 is genuinely on the 2nd in UTC but the
    # 3rd in IST. With a naive session-tz cast it would be DROPPED from
    # end="2024-03-02"; the UTC-anchored bound must INCLUDE it regardless of
    # the ambient DuckDB session TimeZone (the operator runs Asia/Kolkata).
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=[_bar(datetime(2024, 3, 2, 23, 30, tzinfo=UTC), "100.5")],
    )
    store.connection.execute("SET TimeZone='Asia/Kolkata'")
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-03-02",
        end="2024-03-02",
        limit=50,
    )
    assert out["total"] == 1


@pytest.mark.parametrize("bad", ["20240302", "2024-W09-6", "2024/03/01"])
def test_query_candles_rejects_nonstrict_date(
    store: InstrumentStore, tmp_path: Path, bad: str
) -> None:
    _write_candles(tmp_path)
    with pytest.raises(ValueError, match="start must be ISO"):
        query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            start=bad,
            limit=50,
        )


def test_query_candles_rejects_impossible_but_well_shaped_date(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Matches YYYY-MM-DD but is not a real calendar date → fromisoformat raises
    # (covers the second guard inside _parse_iso_date).
    _write_candles(tmp_path)
    with pytest.raises(ValueError, match="end must be ISO"):
        query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            end="2024-13-45",
            limit=50,
        )


# ────────────────────────────────────────────────────────────────────────
# query_candles — corrupt-Parquet resilience (DEC-DB-CORRUPT-SKIP-001)
# ────────────────────────────────────────────────────────────────────────


def _write_two_month_candles(
    lake_root: Path, symbol: str = "NSE-NIFTY", interval: str = "1minute"
) -> tuple[Path, Path]:
    """Write a March + an April single-day partition → two separate month Parquet files.

    Returns ``(march_file, april_file)`` so a test can corrupt ONE and keep the other
    readable (a single month would collapse into one file, leaving nothing to skip).
    """
    writer = ParquetWriter(lake_root=lake_root)
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval=interval,
        bars=[_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4")],
    )
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval=interval,
        bars=[_bar(datetime(2024, 4, 1, 9, 15, tzinfo=UTC), "100.8")],
    )
    base = lake_root / "bars" / "NSE" / "CASH" / symbol / interval
    files = sorted(base.rglob("*.parquet"))
    assert len(files) == 2, "expected one Parquet per month"
    return files[0], files[1]


def _write_three_adjacency_months(
    lake_root: Path, *, interval: str = "1minute"
) -> tuple[Path, Path, Path]:
    """Write Feb / March / April single-day partitions for the adjacency-baseline tests.

    Closes are chosen so the WRONG-baseline branch yields a DIFFERENT number from the
    true one: Feb=99.5 (non-adjacent predecessor) · March=100.0 (the TRUE predecessor of
    April) · April=101.0 (the viewed page). True April change = (101−100)/100 = +1.0%;
    bridging to Feb (after a corrupt March) = (101−99.5)/99.5 ≈ +1.51% — a confidently
    WRONG number. The honest answer when March is corrupt is BLANK (``None``). Closes
    stay inside the fixture ``_bar`` ``[low=99, high=101]`` band. Returns the three month
    Parquet paths ``(feb, march, april)`` in calendar order.
    """
    writer = ParquetWriter(lake_root=lake_root)
    for day, close in (
        (datetime(2024, 2, 1, 9, 15, tzinfo=UTC), "99.5"),
        (datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.0"),
        (datetime(2024, 4, 1, 9, 15, tzinfo=UTC), "101.0"),
    ):
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval=interval,
            bars=[_bar(day, close)],
        )
    base = lake_root / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / interval
    files = sorted(base.rglob("*.parquet"))
    assert len(files) == 3, "expected one Parquet per month (Feb, March, April)"
    return files[0], files[1], files[2]


def test_query_candles_skips_one_corrupt_file_count_and_page(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # One readable month + ONE corrupt *.parquet in the SAME partition. The
    # whole-glob COUNT + page reads raise duckdb.Error; the resilient pager skips
    # the bad file (logged + counted out), still returns the readable month's bar,
    # and does NOT 500 (DEC-DB-CORRUPT-SKIP-001). Covers the COUNT path + the page path.
    _march, april = _write_two_month_candles(tmp_path)
    april.write_bytes(_CORRUPT_PARQUET)
    with capture_logs() as logs:
        out = query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            limit=50,
        )
    # Only the readable March bar survives — the corrupt April month is omitted,
    # never a fabricated bar.
    assert out["total"] == 1
    assert len(out["rows"]) == 1
    skips = [e for e in logs if e["event"] == "db.parquet_skip"]
    # Every skip names the one corrupt file, never the readable March file.
    assert skips, "expected db.parquet_skip for the corrupt April file"
    assert all(s["path"].endswith(april.name) for s in skips)
    # The COUNT + page reads both probe under the candle: source.
    page_skips = [s for s in skips if s["source"] == "candle:NSE/CASH/NSE-NIFTY/1minute"]
    assert page_skips, "expected the COUNT/page reads to skip the corrupt file"
    assert not [e for e in logs if e["event"] == "db.parquet_unreadable"]


def test_query_candles_corrupt_predecessor_yields_blank_not_wrong_pct(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # BLOCKER regression (DEC-DB-CORRUPT-SKIP-001): when the TRUE predecessor month is
    # corrupt, the intraday prev-close baseline must NOT bridge to the non-adjacent
    # earlier month and fabricate a confidently-WRONG %-change — it must emit an honest
    # BLANK. WITHOUT the adjacency guard this asserts a number (≈ +1.51% vs Feb=99.5);
    # WITH the guard it is None. The page (April) itself still renders + does not 500.
    feb, march, april = _write_three_adjacency_months(tmp_path)
    march.write_bytes(_CORRUPT_PARQUET)  # corrupt the TRUE predecessor of the April page
    with capture_logs() as logs:
        out = query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            start="2024-04-01",
            end="2024-04-01",
            limit=50,
        )
    assert len(out["rows"]) == 1  # the April page renders
    change_idx = out["columns"].index("change_pct")
    # The load-bearing assertion the prior len-only test MASKED: an HONEST blank, never
    # the wrong number computed against the non-adjacent February close.
    assert out["rows"][0][change_idx] is None
    baseline_skips = [
        e
        for e in logs
        if e["event"] == "db.parquet_skip" and e["source"].startswith("candle-prevclose:")
    ]
    assert baseline_skips, "expected the prior-close baseline read to skip the corrupt predecessor"
    assert all(s["path"].endswith(march.name) for s in baseline_skips)
    assert feb.exists() and april.exists()  # readable files untouched — operator quarantines


def test_query_candles_clean_baseline_keeps_true_pct(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Control for the BLOCKER test: with NO corruption the same April page reports the
    # TRUE +1.0% (vs the adjacent March=100.0), proving the adjacency guard does NOT
    # blank a clean baseline — only a skip blanks it.
    _write_three_adjacency_months(tmp_path)
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-04-01",
        end="2024-04-01",
        limit=50,
    )
    change_idx = out["columns"].index("change_pct")
    # The cell is JSON-rendered (2-dp display string); the TRUE +1.0% (vs March=100.0),
    # never None and never the wrong Feb-baselined number.
    assert out["rows"][0][change_idx] == "1.00"  # (101 − 100) / 100 × 100


def test_query_candles_daily_corrupt_predecessor_yields_blank_not_wrong_pct(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # The 1-DAY-view twin of the BLOCKER regression: the 1-day prev-close baseline
    # (_daily_close_by_date → _prev_trading_close) must also blank rather than bridge
    # across a corrupt 1-day predecessor month.
    _feb, march, _april = _write_three_adjacency_months(tmp_path, interval="1day")
    march.write_bytes(_CORRUPT_PARQUET)
    with capture_logs() as logs:
        out = query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1day",
            start="2024-04-01",
            end="2024-04-01",
            limit=50,
        )
    assert len(out["rows"]) == 1
    change_idx = out["columns"].index("change_pct")
    assert out["rows"][0][change_idx] is None  # honest blank, never the wrong Feb baseline
    assert [
        e
        for e in logs
        if e["event"] == "db.parquet_skip" and e["source"].startswith("candle-dayclose:")
    ], "expected the 1-day prev-close baseline read to skip the corrupt predecessor"


def test_query_candles_all_corrupt_returns_empty_page_not_500(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # EVERY Parquet in the partition is corrupt. The rglob guard passes (files exist),
    # the whole-glob scan fails, NO file is readable → a degraded-but-correct EMPTY page
    # (total 0, no rows, never a fabricated bar) plus a db.parquet_unreadable error log
    # so the operator can quarantine the lake — never a 500.
    march, april = _write_two_month_candles(tmp_path)
    march.write_bytes(_CORRUPT_PARQUET)
    april.write_bytes(_CORRUPT_PARQUET)
    with capture_logs() as logs:
        out = query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            limit=50,
        )
    assert out["total"] == 0
    assert out["rows"] == []
    unreadable = [e for e in logs if e["event"] == "db.parquet_unreadable"]
    assert unreadable, "expected a db.parquet_unreadable error when nothing is readable"
    assert all(u["source"] == "candle:NSE/CASH/NSE-NIFTY/1minute" for u in unreadable)


def test_execute_parquet_resilient_fast_path_no_probe(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # All files readable → the fast path executes the bound glob once and never
    # probes per-file (no skip logs). Byte-identical to a direct query.
    _write_two_month_candles(tmp_path)
    base = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
    with capture_logs() as logs:
        rows = _execute_parquet_resilient(
            store.connection,
            base,
            "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true)",
            [],
            source="unit",
        )
    assert rows == [(2,)]
    assert not [e for e in logs if e["event"].startswith("db.parquet")]


def test_execute_candle_page_resilient_all_corrupt_returns_empty_columns(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Direct cover of the page helper's no-readable-file branch: every file corrupt
    # → ([], []) (empty columns + rows) so the caller renders a degraded empty page.
    march, april = _write_two_month_candles(tmp_path)
    march.write_bytes(_CORRUPT_PARQUET)
    april.write_bytes(_CORRUPT_PARQUET)
    base = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
    out_columns, raw_rows = _execute_candle_page_resilient(
        store.connection,
        base,
        'SELECT "timestamp", close FROM read_parquet(?, union_by_name=true)',
        [],
        source="unit",
    )
    assert out_columns == []
    assert raw_rows == []


def test_readable_parquet_subset_logs_skip_and_keeps_good(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Direct cover of the shared degraded-path resolver: a mixed dir returns only the
    # readable file + ``omitted=True`` (a file was dropped), logs one skip for the bad
    # one, no unreadable error.
    march, april = _write_two_month_candles(tmp_path)
    april.write_bytes(_CORRUPT_PARQUET)
    base = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
    with capture_logs() as logs:
        readable, omitted = _readable_parquet_subset(
            store.connection,
            base,
            RuntimeError("whole-glob failed"),
            source="unit",
            skip_event="db.parquet_skip",
            unreadable_event="db.parquet_unreadable",
        )
    assert [Path(p).name for p in readable] == [march.name]
    assert omitted is True  # a corrupt file WAS dropped → adjacency no longer guaranteed
    skips = [e for e in logs if e["event"] == "db.parquet_skip"]
    assert len(skips) == 1
    assert skips[0]["path"].endswith(april.name)
    assert not [e for e in logs if e["event"] == "db.parquet_unreadable"]


def test_readable_parquet_subset_all_readable_reports_not_omitted(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # The transient-error branch: the whole-glob read raised but EVERY file probes
    # readable → ``omitted=False`` (nothing dropped), so an adjacency-sensitive caller
    # keeps its baseline. (A genuine SQL bug rides this branch and is re-raised by the
    # caller's re-run — see test_execute_parquet_resilient_genuine_sql_error_propagates.)
    march, april = _write_two_month_candles(tmp_path)
    base = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
    with capture_logs() as logs:
        readable, omitted = _readable_parquet_subset(
            store.connection,
            base,
            RuntimeError("transient"),
            source="unit",
            skip_event="db.parquet_skip",
            unreadable_event="db.parquet_unreadable",
        )
    assert {Path(p).name for p in readable} == {march.name, april.name}
    assert omitted is False
    assert not [e for e in logs if e["event"].startswith("db.parquet")]


def test_execute_parquet_resilient_genuine_sql_error_propagates(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # A genuine SQL bug (a non-existent column) is NEVER swallowed by the resilient
    # helper: the whole-glob read raises duckdb.Error, every file probes readable
    # (omitted=False), and the degraded re-run over the SAME files RE-RAISES — so a real
    # bug surfaces as a 500, not a silent empty result (the over-broad-catch MINOR).
    _write_two_month_candles(tmp_path)
    base = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
    with pytest.raises(duckdb.Error):
        _execute_parquet_resilient(
            store.connection,
            base,
            "SELECT definitely_not_a_column FROM read_parquet(?, union_by_name=true)",
            [],
            source="unit",
        )


def test_aggregate_parquet_resilient_uses_coverage_namespace(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # The coverage wrapper keeps its own ``coverage.*`` log namespace over the SHARED
    # probe-and-skip logic (a corrupt file → coverage.parquet_skip, not db.*).
    _march, april = _write_two_month_candles(tmp_path)
    april.write_bytes(_CORRUPT_PARQUET)
    base = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
    with capture_logs() as logs:
        rows = _aggregate_parquet_resilient(
            store.connection,
            base,
            "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true)",
            source="spot:unit",
        )
    assert rows == [(1,)]  # only the readable March bar
    assert [e["event"] for e in logs if e["event"].startswith("coverage.parquet")] == [
        "coverage.parquet_skip"
    ]
    assert not [e for e in logs if e["event"].startswith("db.parquet")]


def test_query_table_contains_escapes_underscore_wildcard(store: InstrumentStore) -> None:
    # "NI_TY" must match literally — the LIKE `_` wildcard would otherwise let
    # it match "NIFTY". Escaped → zero matches.
    out = query_table(
        store.connection,
        "instruments",
        filters=(("symbol_name", "contains", "NI_TY"),),
        limit=50,
    )
    assert out["total"] == 0


def test_query_table_contains_escapes_percent_wildcard(store: InstrumentStore) -> None:
    # A literal "%" matches none of the seeded symbols (would match ALL if the
    # wildcard leaked through).
    out = query_table(
        store.connection,
        "instruments",
        filters=(("symbol_name", "contains", "%"),),
        limit=50,
    )
    assert out["total"] == 0


# ────────────────────────────────────────────────────────────────────────
# list_option_datasets — the expired-options lake (options-in-db-viewer)
# ────────────────────────────────────────────────────────────────────────


def test_list_option_datasets_missing_options_dir(tmp_path: Path) -> None:
    assert list_option_datasets(tmp_path) == []


def test_list_option_datasets_finds_written_series(tmp_path: Path) -> None:
    _write_options(tmp_path)
    _write_options(tmp_path, partition_key="NSE_FNO|99|MONTH|1|ATM_P1|PUT")
    found = list_option_datasets(tmp_path)
    assert {d["series"] for d in found} == {
        _OPT_SERIES,
        "NSE_FNO__99__MONTH__1__ATM_P1__PUT",
    }
    # every entry is tagged kind=option + the rolled 1-minute interval.
    assert all(d["kind"] == "option" and d["interval"] == "1minute" for d in found)


def test_list_option_datasets_skips_foreign_and_empty_dirs(tmp_path: Path) -> None:
    options = tmp_path / "options"
    # Valid series (written via the real writer).
    _write_options(tmp_path)
    # A series dir with NO parquet inside (skipped — empty).
    (options / "NSE_FNO__1__WEEK__0__ATM__PUT").mkdir(parents=True)
    # A traversal-shaped dir name (skipped — fails the safe-segment grammar).
    (options / "..evil").mkdir(parents=True)
    # A separator-shaped dir name (skipped).
    (options / "bad series!").mkdir(parents=True)
    found = list_option_datasets(tmp_path)
    assert found == [
        {
            "kind": "option",
            "series": _OPT_SERIES,
            "interval": "1minute",
            # No id→symbol map here → the label decodes with the numeric id
            # (DEC-DB-OPTION-DATASET-DECODE-001).
            "label": "13 · WEEK#0 ATM CALL · 1m",
        }
    ]


# ── option dataset labels (DEC-DB-OPTION-DATASET-DECODE-001) ───────────


def test_list_option_datasets_decodes_label_with_symbol_map(tmp_path: Path) -> None:
    # The #629 tracked follow-up: the /db picker showed the raw vendor segment
    # (NSE_FNO__25__MONTH__1__ATM-10__CALL); the id→symbol map decodes it to
    # the operator's coordinates. The series KEY stays byte-unchanged so
    # queries keep addressing the on-disk segment.
    _write_options(tmp_path, partition_key="NSE_FNO|25|MONTH|1|ATM-10|CALL")
    (found,) = list_option_datasets(tmp_path, symbol_for_id={"25": "BANKNIFTY"})
    assert found == {
        "kind": "option",
        "series": "NSE_FNO__25__MONTH__1__ATM-10__CALL",
        "interval": "1minute",
        "label": "BANKNIFTY · MONTH#1 ATM-10 CALL · 1m",
    }


def test_list_option_datasets_unknown_id_keeps_numeric_id(tmp_path: Path) -> None:
    _write_options(tmp_path)  # security_id 13 — absent from the map
    (found,) = list_option_datasets(tmp_path, symbol_for_id={"25": "BANKNIFTY"})
    # Still decoded, still distinguishable — never hidden, never a crash.
    assert found["label"] == "13 · WEEK#0 ATM CALL · 1m"


def test_list_option_datasets_label_falls_back_raw_on_non_parsing_shape(tmp_path: Path) -> None:
    # A segment-grammar-valid directory whose name is NOT a 6-part series key
    # (wrong arity / an empty part) keeps its RAW name as the label — fallback,
    # never an exception, never a hidden dataset. list_option_datasets only
    # globs for *.parquet PRESENCE (content is never read here), so an empty
    # file is enough to make the directory a dataset.
    options = tmp_path / "options"
    for weird in ("WEIRD__SHAPE", "NSE_FNO____MONTH__1__ATM__CALL"):
        (options / weird).mkdir(parents=True)
        (options / weird / "2024-01.parquet").write_bytes(b"")
    found = list_option_datasets(tmp_path, symbol_for_id={"25": "BANKNIFTY"})
    # Sorted by series; every label is the raw segment (wrong arity for
    # WEIRD__SHAPE, an empty part for the quadruple-underscore name).
    assert [d["series"] for d in found] == [
        "NSE_FNO____MONTH__1__ATM__CALL",
        "WEIRD__SHAPE",
    ]
    assert [d["label"] for d in found] == [d["series"] for d in found]


def test_list_option_datasets_finds_plus_strike_series_and_queries(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Regression (DEC-DB-OPTION-DATASET-DECODE-001 bundled allowlist fix —
    # FAILED before it): the writer stores the rolling-option ATM+N selector
    # VERBATIM (ATM+3 → an …__ATM+3__CALL directory), but the old segment
    # grammar had no `+` — the upper half of every strike chain was silently
    # MISSING from the /db picker AND refused by query_option_candles (the
    # read allowlist dropped what the write path stores, the
    # DEC-PREOPEN-AMPERSAND-001 class). Now: discovered, decoded, queryable.
    _write_options(
        tmp_path, partition_key="NSE_FNO|25|WEEK|1|ATM+3|CALL", underlying_symbol="BANKNIFTY"
    )
    (found,) = list_option_datasets(tmp_path, symbol_for_id={"25": "BANKNIFTY"})
    assert found["series"] == "NSE_FNO__25__WEEK__1__ATM+3__CALL"
    assert found["label"] == "BANKNIFTY · WEEK#1 ATM+3 CALL · 1m"
    # The discovered KEY round-trips through the reader (picker → query loop).
    page = query_option_candles(store.connection, tmp_path, series=found["series"], limit=50)
    assert page["total"] == 4  # 2 sides × 2 trading days


# ────────────────────────────────────────────────────────────────────────
# list_option_series — distinct addressing tuples (DEC-EXPIRED-OPTIONS-TABLE-001)
# ────────────────────────────────────────────────────────────────────────


def test_list_option_series_empty_lake_returns_empty(
    store: InstrumentStore, tmp_path: Path
) -> None:
    assert list_option_series(store.connection, tmp_path) == []


def test_list_option_series_distinct_addressing(store: InstrumentStore, tmp_path: Path) -> None:
    _write_options(tmp_path)  # NIFTY ATM CALL series, CE+PE bars
    rows = list_option_series(store.connection, tmp_path)
    # One distinct (strike, side) tuple per side, both with the same addressing.
    assert {r["side"] for r in rows} == {"CE", "PE"}
    for r in rows:
        assert r["exchange"] == "NSE"
        assert r["underlying_symbol"] == "NIFTY"
        assert r["expiry_flag"] == "WEEK"
        assert r["expiry_code"] == 0
        assert r["strike_offset"] == "ATM"  # the REQUESTED selector
        assert r["strike"] == pytest.approx(17500.01)  # the RESOLVED strike (2dp)


def test_list_option_series_filters_by_underlying(store: InstrumentStore, tmp_path: Path) -> None:
    _write_options(tmp_path, underlying_symbol="NIFTY")
    _write_options(
        tmp_path,
        partition_key="NSE_FNO|99|MONTH|1|ATM+1|PUT",
        underlying_symbol="BANKNIFTY",
    )
    nifty = list_option_series(store.connection, tmp_path, underlying_symbol="NIFTY")
    assert {r["underlying_symbol"] for r in nifty} == {"NIFTY"}
    banknifty = list_option_series(store.connection, tmp_path, underlying_symbol="BANKNIFTY")
    assert {r["underlying_symbol"] for r in banknifty} == {"BANKNIFTY"}
    assert {r["strike_offset"] for r in banknifty} == {"ATM+1"}


# ────────────────────────────────────────────────────────────────────────
# summarize_option_datasets — per-underlying expired-options lake coverage
# ────────────────────────────────────────────────────────────────────────


def test_summarize_option_datasets_no_options_dir_returns_empty(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # No <lake>/options directory at all → [] (the is_dir() guard).
    assert summarize_option_datasets(store.connection, tmp_path) == []


def test_summarize_option_datasets_empty_options_dir_returns_empty(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # The options dir EXISTS but holds no .parquet → [] (the rglob guard's
    # second operand — covers the branch the no-dir case can't reach).
    (tmp_path / "options").mkdir()
    assert summarize_option_datasets(store.connection, tmp_path) == []


def test_summarize_option_datasets_aggregates_per_underlying(
    store: InstrumentStore, tmp_path: Path
) -> None:
    _write_options(tmp_path, underlying_symbol="NIFTY")  # CE+PE across 2 days
    _write_options(
        tmp_path,
        partition_key="NSE_FNO|99|MONTH|1|ATM+1|PUT",
        underlying_symbol="BANKNIFTY",
    )
    rows = summarize_option_datasets(store.connection, tmp_path)
    # Sorted by (exchange, underlying): BANKNIFTY before NIFTY (both NSE).
    assert [r["underlying"] for r in rows] == ["BANKNIFTY", "NIFTY"]
    for r in rows:
        assert r["exchange"] == "NSE"
        assert r["series"] == 2  # one CE + one PE series
        assert r["bars"] == 4  # 2 sides × 2 trading days
        assert r["first_date"] == "2024-03-01"
        assert r["last_date"] == "2024-03-02"


def test_summarize_option_datasets_skips_one_corrupt_file(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Two readable underlyings + ONE unreadable *.parquet in the same options
    # tree. The whole-glob scan raises duckdb.Error; the resilient read skips the
    # bad file (logged) and still reports BOTH underlyings — never a 500.
    _write_options(tmp_path, underlying_symbol="NIFTY")
    _write_options(
        tmp_path, partition_key="NSE_FNO|99|MONTH|1|ATM+1|PUT", underlying_symbol="BANKNIFTY"
    )
    bad = _drop_corrupt_parquet(tmp_path / "options")
    with capture_logs() as logs:
        rows = summarize_option_datasets(store.connection, tmp_path)
    assert [r["underlying"] for r in rows] == ["BANKNIFTY", "NIFTY"]
    assert all(r["series"] == 2 and r["bars"] == 4 for r in rows)
    skips = [e for e in logs if e["event"] == "coverage.parquet_skip"]
    assert len(skips) == 1
    assert skips[0]["source"] == "options"
    assert skips[0]["path"].endswith(bad.name)


def test_summarize_option_datasets_all_unreadable_returns_empty(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # The options dir holds ONLY an unreadable *.parquet → the rglob guard passes,
    # the whole-glob scan fails, and NO file is readable → [] (never a 500) plus
    # an error log naming the source so the operator can quarantine the lake.
    _drop_corrupt_parquet(tmp_path / "options")
    with capture_logs() as logs:
        rows = summarize_option_datasets(store.connection, tmp_path)
    assert rows == []
    unreadable = [e for e in logs if e["event"] == "coverage.parquet_unreadable"]
    assert len(unreadable) == 1
    assert unreadable[0]["source"] == "options"


# ────────────────────────────────────────────────────────────────────────
# query_option_candles — read the expired-options lake
# ────────────────────────────────────────────────────────────────────────


def test_query_option_candles_returns_all_rows(store: InstrumentStore, tmp_path: Path) -> None:
    _write_options(tmp_path)
    out = query_option_candles(store.connection, tmp_path, series=_OPT_SERIES, limit=50)
    assert out["total"] == 4  # CE+PE across two days
    assert out["columns"] == list(OPTION_CANDLE_COLUMNS)
    assert out["series"] == _OPT_SERIES
    assert out["interval"] == "1minute"


def test_query_option_candles_surfaces_addressing_columns(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # DEC-EXPIRED-OPTIONS-TABLE-001: the 7 promoted addressing columns are now
    # part of the projected option-candle column set.
    _write_options(tmp_path)
    out = query_option_candles(store.connection, tmp_path, series=_OPT_SERIES, limit=50)
    for addr in (
        "exch_id",
        "exchange_segment",
        "security_id",
        "underlying_symbol",
        "expiry_flag",
        "expiry_code",
        "strike_offset",
    ):
        assert addr in out["columns"]
    ul_idx = out["columns"].index("underlying_symbol")
    assert {str(r[ul_idx]) for r in out["rows"]} == {"NIFTY"}


def test_query_option_candles_surfaces_option_columns(
    store: InstrumentStore, tmp_path: Path
) -> None:
    _write_options(tmp_path)
    out = query_option_candles(store.connection, tmp_path, series=_OPT_SERIES, limit=50)
    # the option-specific stamps are present in the projected columns.
    for extra in ("side", "strike", "spot", "iv"):
        assert extra in out["columns"]
    side_idx = out["columns"].index("side")
    assert {str(r[side_idx]) for r in out["rows"]} == {"CE", "PE"}


def test_query_option_candles_iv_full_precision(store: InstrumentStore, tmp_path: Path) -> None:
    # iv is NOT money (mirrors option_parquet_writer): it must keep full
    # precision, NOT be 2dp-rounded like the price columns.
    OptionParquetWriter(lake_root=tmp_path).write(
        partition_key=_OPT_PK,
        coordinate=_OPT_COORD,
        bars=[_obar(datetime(2024, 3, 1, 4, 0, tzinfo=UTC), "CE", "105.004", iv="0.123456")],
    )
    out = query_option_candles(
        store.connection,
        tmp_path,
        series=_OPT_SERIES,
        columns=("iv",),
        limit=50,
    )
    assert out["columns"] == ["iv"]
    # float round-trip of 0.123456 — well beyond 2dp, so rounding would corrupt.
    assert out["rows"] == [[pytest.approx(0.123456)]]


def test_query_option_candles_column_subset(store: InstrumentStore, tmp_path: Path) -> None:
    _write_options(tmp_path)
    out = query_option_candles(
        store.connection,
        tmp_path,
        series=_OPT_SERIES,
        columns=("timestamp", "side", "strike"),
        limit=50,
    )
    assert out["columns"] == ["timestamp", "side", "strike"]
    assert all(len(r) == 3 for r in out["rows"])


def test_query_option_candles_empty_columns_tuple_raises(
    store: InstrumentStore, tmp_path: Path
) -> None:
    _write_options(tmp_path)
    with pytest.raises(ValueError, match="at least one column"):
        query_option_candles(store.connection, tmp_path, series=_OPT_SERIES, columns=(), limit=50)


def test_query_option_candles_unknown_column_raises(store: InstrumentStore, tmp_path: Path) -> None:
    _write_options(tmp_path)
    with pytest.raises(ValueError, match="unknown column"):
        query_option_candles(
            store.connection, tmp_path, series=_OPT_SERIES, columns=("delta",), limit=50
        )


def test_query_option_candles_date_range_end_inclusive(
    store: InstrumentStore, tmp_path: Path
) -> None:
    _write_options(tmp_path)
    out = query_option_candles(
        store.connection,
        tmp_path,
        series=_OPT_SERIES,
        start="2024-03-02",
        end="2024-03-02",
        limit=50,
    )
    assert out["total"] == 2  # only the 2nd-day CE+PE


def test_query_option_candles_start_only(store: InstrumentStore, tmp_path: Path) -> None:
    _write_options(tmp_path)
    out = query_option_candles(
        store.connection, tmp_path, series=_OPT_SERIES, start="2024-03-02", limit=50
    )
    assert out["total"] == 2


def test_query_option_candles_timestamp_clean_ist_no_offset(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # 04:00 UTC → 09:30 IST; clean wall-clock, no +05:30 / T separator.
    _write_options(tmp_path)
    out = query_option_candles(
        store.connection, tmp_path, series=_OPT_SERIES, columns=("timestamp",), limit=50
    )
    stamps = sorted(str(r[0]) for r in out["rows"])
    assert stamps == [
        "2024-03-01 09:30:00",
        "2024-03-01 09:30:00",
        "2024-03-02 09:30:00",
        "2024-03-02 09:30:00",
    ]
    assert all("+" not in s and "T" not in s for s in stamps)


def test_query_option_candles_descending(store: InstrumentStore, tmp_path: Path) -> None:
    _write_options(tmp_path)
    out = query_option_candles(
        store.connection,
        tmp_path,
        series=_OPT_SERIES,
        columns=("timestamp",),
        descending=True,
        limit=50,
    )
    stamps = [r[0] for r in out["rows"]]
    assert stamps == sorted(stamps, reverse=True)


def test_query_option_candles_empty_series_returns_empty_page(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # No parquet written for this series → empty page, never raises.
    out = query_option_candles(
        store.connection,
        tmp_path,
        series="NSE_FNO__1__WEEK__0__ATM__CALL",
        columns=("timestamp", "iv"),
        limit=50,
    )
    assert out["total"] == 0
    assert out["rows"] == []
    assert out["columns"] == ["timestamp", "iv"]


def test_query_option_candles_empty_series_default_columns(
    store: InstrumentStore, tmp_path: Path
) -> None:
    out = query_option_candles(
        store.connection, tmp_path, series="NSE_FNO__1__WEEK__0__ATM__CALL", limit=50
    )
    assert out["columns"] == list(OPTION_CANDLE_COLUMNS)


@pytest.mark.parametrize("bad", ["../etc/passwd", "bad series!", "has/slash", "..", "NSE_FNO|raw"])
def test_query_option_candles_illegal_series_raises(
    store: InstrumentStore, tmp_path: Path, bad: str
) -> None:
    with pytest.raises(ValueError, match="illegal option series"):
        query_option_candles(store.connection, tmp_path, series=bad, limit=50)


@pytest.mark.parametrize("field", ["start", "end"])
def test_query_option_candles_malformed_date_raises(
    store: InstrumentStore, tmp_path: Path, field: str
) -> None:
    _write_options(tmp_path)
    kwargs = {"start": None, "end": None} | {field: "2024/03/01"}
    with pytest.raises(ValueError, match=f"{field} must be ISO"):
        query_option_candles(
            store.connection,
            tmp_path,
            series=_OPT_SERIES,
            limit=50,
            **kwargs,  # type: ignore[arg-type]
        )


def test_query_option_candles_paging_clamps(store: InstrumentStore, tmp_path: Path) -> None:
    _write_options(tmp_path)
    out = query_option_candles(
        store.connection,
        tmp_path,
        series=_OPT_SERIES,
        limit=10**9,
        offset=-3,
    )
    assert out["limit"] == 500
    assert out["offset"] == 0


def test_query_option_candles_date_bound_is_utc_not_session_tz(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # A bar at 23:30 UTC on 2024-03-02 is on the 2nd in UTC but the 3rd in IST.
    # The UTC-anchored bound must INCLUDE it regardless of session TimeZone.
    OptionParquetWriter(lake_root=tmp_path).write(
        partition_key=_OPT_PK,
        coordinate=_OPT_COORD,
        bars=[_obar(datetime(2024, 3, 2, 23, 30, tzinfo=UTC), "CE", "105.0")],
    )
    store.connection.execute("SET TimeZone='Asia/Kolkata'")
    out = query_option_candles(
        store.connection,
        tmp_path,
        series=_OPT_SERIES,
        start="2024-03-02",
        end="2024-03-02",
        limit=50,
    )
    assert out["total"] == 1


# ────────────────────────────────────────────────────────────────────────
# Store-lock leak regression (DEC-STORE-LOCK-LEAK-FIX-001)
#
# The production read conn (_LockedReadConn) holds the store RLock from
# execute() until a fetch. A description-only reader (list_columns) used to
# leak it → the background universe-pull's writes deadlocked (the live
# "Fetch all expiries" 26-minute hang). These are CONCURRENCY tests: they
# prove a DIFFERENT thread can take the lock after each read pattern.
# ────────────────────────────────────────────────────────────────────────


def _lock_free(store: InstrumentStore, timeout: float = 2.0) -> bool:
    """True iff a SEPARATE thread can acquire the store lock (it isn't leaked)."""
    got: list[bool] = []

    def _try() -> None:
        acquired = store._lock.acquire(timeout=timeout)
        got.append(acquired)
        if acquired:
            store._lock.release()

    t = threading.Thread(target=_try)
    t.start()
    t.join()
    return got == [True]


def test_list_columns_releases_store_lock(store: InstrumentStore) -> None:
    # The actual production deadlock: list_columns reads .description; before
    # the fix it never fetched → leaked the store lock forever.
    list_columns(store.connection, "instruments")
    assert _lock_free(store)


def test_locked_conn_context_manager_releases_without_fetch(store: InstrumentStore) -> None:
    # A `with` block releases the lock even if the caller only reads description.
    with store.connection.execute("SELECT * FROM instruments LIMIT 0") as cur:
        assert cur.description is not None
    assert _lock_free(store)


def test_locked_conn_close_is_idempotent(store: InstrumentStore) -> None:
    cur = store.connection.execute("SELECT * FROM instruments LIMIT 0")
    cur.close()
    cur.close()  # idempotent — second close is a no-op, not a double-release
    assert _lock_free(store)


def test_locked_conn_fetchall_releases(store: InstrumentStore) -> None:
    store.connection.execute("SELECT * FROM instruments LIMIT 0").fetchall()
    assert _lock_free(store)


def test_locked_conn_execute_error_releases_lock(store: InstrumentStore) -> None:
    # A failed execute must NOT leak the lock (the except → _release path).
    with pytest.raises(Exception, match="(?i)nonexistent|catalog|table"):
        store.connection.execute("SELECT * FROM nonexistent_table_xyz")
    assert _lock_free(store)


def test_concurrent_read_then_write_does_not_deadlock(store: InstrumentStore) -> None:
    # End-to-end shape of the live bug: a /db read (list_columns) followed by a
    # store WRITE from another thread (mirrors the background expiries pull).
    list_columns(store.connection, "instruments")
    done: list[bool] = []

    def _writer() -> None:
        store.upsert_many(
            [_idx("SBIN", "State Bank")],
            snapshot_date=date.today(),
            run_id="lock-regression",
            csv_source_sha256="1" * 64,
        )
        done.append(True)

    t = threading.Thread(target=_writer)
    t.start()
    t.join(timeout=5.0)
    assert done == [True]  # the write completed — no deadlock


def test_locked_conn_reexecute_without_fetch_does_not_leak(store: InstrumentStore) -> None:
    # Worst case (hostile-review LOW): re-execute on the SAME view without an
    # intervening fetch must not leak an extra acquisition — execute() drops the
    # prior hold first, so the final fetch frees the lock completely.
    cur = store.connection
    cur.execute("SELECT * FROM instruments LIMIT 0")
    cur.execute("SELECT * FROM instruments LIMIT 0")  # re-execute, no fetch between
    cur.fetchall()
    assert _lock_free(store)


# ────────────────────────────────────────────────────────────────────────
# _ist_date + summarize_spot_datasets (DEC-COVERAGE-SPOT-001)
# ────────────────────────────────────────────────────────────────────────


def test_ist_date_tz_aware_converts_to_ist_date() -> None:
    # 2024-03-01 20:30Z → 2024-03-02 02:00 IST → the IST calendar date rolls.
    assert _ist_date(datetime(2024, 3, 1, 20, 30, tzinfo=UTC)) == "2024-03-02"
    # 2024-03-01 09:15Z → 2024-03-01 14:45 IST → same date.
    assert _ist_date(datetime(2024, 3, 1, 9, 15, tzinfo=UTC)) == "2024-03-01"


def test_ist_date_naive_datetime_kept_as_is() -> None:
    # Defensive branch: a naive datetime is used as-is (DuckDB returns tz-aware).
    assert _ist_date(datetime(2024, 3, 1, 14, 45, 0)) == "2024-03-01"


def test_ist_date_non_datetime_is_none() -> None:
    # A SQL NULL MIN/MAX over an empty partition surfaces as None → None.
    assert _ist_date(None) is None
    assert _ist_date("not-a-datetime") is None


def test_summarize_spot_datasets_empty_lake(store: InstrumentStore, tmp_path: Path) -> None:
    assert summarize_spot_datasets(store.connection, tmp_path) == []


def test_summarize_spot_datasets_reports_bars_and_date_span(
    store: InstrumentStore, tmp_path: Path
) -> None:
    _write_candles(tmp_path)
    rows = summarize_spot_datasets(store.connection, tmp_path)
    assert rows == [
        {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
            "bars": 2,
            "first_date": "2024-03-01",
            "last_date": "2024-03-02",
            # Re-baselined golden (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001): the fixture's
            # bars sit at 09:15 UTC = 14:45 IST, i.e. AFTER the 09:15 IST canonical open,
            # so BOTH stored sessions carry a substituted derived daily open.
            "open_substituted_days": 2,
            "session_days": 2,
        }
    ]


def test_summarize_spot_datasets_session_days_counts_distinct_ist_dates(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # DEC-SPOT-PARTIAL-RANGE-CHIP-001: ``session_days`` counts DISTINCT IST calendar
    # dates (not bars, not UTC dates). Three bars: two on the SAME IST date (one of
    # them crossing the UTC boundary — 2024-03-01 20:30 UTC is 2024-03-02 02:00 IST)
    # plus one on a second IST date → bars=3 but session_days=2.
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=[
            _bar(datetime(2024, 3, 1, 20, 30, tzinfo=UTC), "100.4"),  # IST 2024-03-02
            _bar(datetime(2024, 3, 2, 9, 15, tzinfo=UTC), "100.8"),  # IST 2024-03-02
            _bar(datetime(2024, 3, 4, 9, 15, tzinfo=UTC), "101.0"),  # IST 2024-03-04
        ],
    )
    [row] = summarize_spot_datasets(store.connection, tmp_path)
    assert row["bars"] == 3
    assert row["session_days"] == 2
    assert row["first_date"] == "2024-03-02" and row["last_date"] == "2024-03-04"


def test_summarize_spot_datasets_session_days_ambient_timezone_robust(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Regression (DEC-SPOT-SESSION-DAYS-IST-TZ-001): ``session_days`` must count
    # IST dates via the COMMON _IST_DATE_EXPR (AT TIME ZONE 'UTC' BEFORE the
    # +05:30 shift), NOT a bare CAST-to-DATE on the TIMESTAMPTZ column, which
    # resolves in the ambient DuckDB session TimeZone. Under a negative-offset
    # zone (America/Adak, UTC-10 pre-DST) the buggy expression dated the NSE
    # session-open bar 2024-03-01 03:45 UTC to 2024-02-29 while the session-close
    # bar 2024-03-01 09:59 UTC dated to 2024-03-01 — ONE session counted as TWO
    # session_days, inflating density and masking a genuinely sparse symbol from
    # the amber settled_partial_range verdict.
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=[
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), "100.4"),  # 09:15 IST open
            _bar(datetime(2024, 3, 1, 9, 59, tzinfo=UTC), "100.8"),  # 15:29 IST close
        ],
    )
    conn = store.connection
    (original_tz,) = conn.execute("SELECT current_setting('TimeZone')").fetchall()[0]
    conn.execute("SET TimeZone = 'America/Adak'")
    try:
        [row] = summarize_spot_datasets(conn, tmp_path)
    finally:
        conn.execute(f"SET TimeZone = '{original_tz}'")
    assert row["bars"] == 2
    assert row["session_days"] == 1  # ONE NSE session, in EVERY ambient zone
    assert row["first_date"] == "2024-03-01" and row["last_date"] == "2024-03-01"


def test_summarize_spot_datasets_multiple_sets(store: InstrumentStore, tmp_path: Path) -> None:
    _write_candles(tmp_path, symbol="NSE-NIFTY")
    _write_candles(tmp_path, symbol="NSE-BANKNIFTY")
    rows = summarize_spot_datasets(store.connection, tmp_path)
    symbols = [r["symbol"] for r in rows]
    assert symbols == ["NSE-BANKNIFTY", "NSE-NIFTY"]  # list_candle_datasets is sorted
    assert all(r["bars"] == 2 for r in rows)


def test_summarize_spot_datasets_skips_one_corrupt_file(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # An unreadable *.parquet sibling inside the dataset dir makes the whole-glob
    # scan fail; the resilient read skips it and still reports the real 2 bars.
    _write_candles(tmp_path)
    dataset_dir = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
    _drop_corrupt_parquet(dataset_dir)
    with capture_logs() as logs:
        rows = summarize_spot_datasets(store.connection, tmp_path)
    assert rows == [
        {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
            "bars": 2,
            "first_date": "2024-03-01",
            "last_date": "2024-03-02",
            # Both fixture sessions open at 14:45 IST (> 09:15) — substituted; the
            # resilient readable-subset re-run computes the SAME per-session CTE
            # (re-baselined, DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001).
            "open_substituted_days": 2,
            "session_days": 2,
        }
    ]
    skips = [e for e in logs if e["event"] == "coverage.parquet_skip"]
    assert len(skips) == 1
    assert skips[0]["source"] == "spot:NSE/CASH/NSE-NIFTY/1minute"


def test_summarize_spot_datasets_all_unreadable_reports_zero(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Every parquet in the dataset is unreadable → the dataset is still discovered
    # (path-based) but aggregates to 0 bars / no dates via the empty-subset branch,
    # never a 500.
    _write_candles(tmp_path)
    dataset_dir = tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute"
    for parquet in dataset_dir.rglob("*.parquet"):
        parquet.write_bytes(_CORRUPT_PARQUET)
    with capture_logs() as logs:
        rows = summarize_spot_datasets(store.connection, tmp_path)
    assert rows == [
        {
            "exchange": "NSE",
            "segment": "CASH",
            "symbol": "NSE-NIFTY",
            "interval": "1minute",
            "bars": 0,
            "first_date": None,
            "last_date": None,
            # An all-unreadable partition honestly degrades the substitution counts to
            # (0, 0) — nothing observed (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001).
            "open_substituted_days": 0,
            "session_days": 0,
        }
    ]
    unreadable = [e for e in logs if e["event"] == "coverage.parquet_unreadable"]
    assert len(unreadable) == 1
    assert unreadable[0]["source"] == "spot:NSE/CASH/NSE-NIFTY/1minute"


# ────────────────────────────────────────────────────────────────────────
# Opt-in session-date enrichment (DEC-COLDSTART-SEED-INTERIOR-GAP-001)
# ────────────────────────────────────────────────────────────────────────


#: The three INDEX `1minute` bar instants of the enrichment fixture — the last one
#: crosses IST midnight (20:30 UTC → 02:00 IST NEXT day), pinning the SQL IST cast
#: against the Python `_ist_date` twin.
_SESSION_FIXTURE_TS = (
    datetime(2024, 3, 1, 9, 15, tzinfo=UTC),
    datetime(2024, 3, 22, 9, 15, tzinfo=UTC),
    datetime(2024, 3, 1, 20, 30, tzinfo=UTC),  # IST calendar date 2024-03-02
)


def _write_session_fixture(lake_root: Path) -> None:
    """An INDEX `1minute` + INDEX `1day` + CASH `1minute` lake for the enrichment tests."""
    writer = ParquetWriter(lake_root=lake_root)
    writer.write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_bar(ts, "100.4") for ts in _SESSION_FIXTURE_TS],
    )
    writer.write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4")],
    )
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="RELIANCE",
        interval="1minute",
        bars=[_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4")],
    )


def test_summarize_spot_datasets_default_carries_no_session_dates(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # FAILS-BEFORE trivially (the key never existed) + pins the hot-path contract
    # FOREVER: the default (poller) read is BYTE-IDENTICAL — no `session_dates`
    # key on ANY row, INDEX included (the enrichment is strictly opt-in).
    _write_session_fixture(tmp_path)
    rows = summarize_spot_datasets(store.connection, tmp_path)
    assert len(rows) == 3
    assert all("session_dates" not in row for row in rows)


def test_summarize_spot_datasets_session_dates_on_index_1minute_only(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # The opted-in read attaches `session_dates` to the INDEX `1minute` row ONLY:
    # the derived INDEX `1day` row must NEVER carry it (REVISION 1 — a derived
    # window must never be density-gated) and CASH stays span-only.
    _write_session_fixture(tmp_path)
    rows = summarize_spot_datasets(
        store.connection, tmp_path, session_date_segments=frozenset({"INDEX"})
    )
    by_key = {(r["segment"], r["interval"]): r for r in rows}
    index_minute = by_key[("INDEX", "1minute")]
    # GOLDEN IST parity: the SQL cast agrees with the Python `_ist_date` twin for
    # every bar — including the 20:30 UTC bar whose IST calendar date rolls over.
    expected = {_ist_date(ts) for ts in _SESSION_FIXTURE_TS}
    assert {d.isoformat() for d in index_minute["session_dates"]} == expected
    assert date(2024, 3, 2) in index_minute["session_dates"]  # the rolled-over IST date
    assert isinstance(index_minute["session_dates"], frozenset)
    assert "session_dates" not in by_key[("INDEX", "1day")]
    assert "session_dates" not in by_key[("CASH", "1minute")]
    # The enriched aggregate leaves the historical 3 scalars byte-identical.
    plain = {
        (r["segment"], r["interval"]): r
        for r in summarize_spot_datasets(store.connection, tmp_path)
    }
    for key, row in by_key.items():
        assert {k: v for k, v in row.items() if k != "session_dates"} == plain[key]


def test_summarize_spot_datasets_session_dates_all_unreadable_is_empty_set(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # An all-unreadable INDEX `1minute` partition degrades to an EMPTY session set
    # (over which the seed's density check honestly abstains), never a crash.
    _write_session_fixture(tmp_path)
    dataset_dir = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
    for parquet in dataset_dir.rglob("*.parquet"):
        parquet.write_bytes(_CORRUPT_PARQUET)
    with capture_logs():
        rows = summarize_spot_datasets(
            store.connection, tmp_path, session_date_segments=frozenset({"INDEX"})
        )
    by_key = {(r["segment"], r["interval"]): r for r in rows}
    index_minute = by_key[("INDEX", "1minute")]
    assert index_minute["bars"] == 0
    assert index_minute["session_dates"] == frozenset()


# ────────────────────────────────────────────────────────────────────────
# Daily-open substitution honesty (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001)
# ────────────────────────────────────────────────────────────────────────
# 09:15 IST (the canonical NSE/BSE session open) == 03:45 UTC on the SAME date.


def _write_index_minutes(lake_root: Path, bars: list[CandleBar], *, symbol: str = "NIFTY") -> None:
    """Write one INDEX ``1minute`` dataset from explicit bars (the substitution fixtures)."""
    ParquetWriter(lake_root=lake_root).write(
        exchange="NSE", segment="INDEX", symbol=symbol, interval="1minute", bars=bars
    )


def _index_minute_row(store: InstrumentStore, lake_root: Path) -> dict[str, Any]:
    """The NIFTY INDEX ``1minute`` summary row (the substitution tests' subject)."""
    rows = summarize_spot_datasets(store.connection, lake_root)
    return next(r for r in rows if r["segment"] == "INDEX" and r["interval"] == "1minute")


def test_open_substitution_zero_when_first_minute_exactly_0915_boundary(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # BOUNDARY pin (strict `>`): a session whose FIRST present minute is EXACTLY the
    # canonical 09:15:00 IST open is NOT counted — its derived 1-day open IS the
    # canonical open price, no substitution happened.
    _write_index_minutes(
        tmp_path,
        [
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), "100.4"),  # 09:15:00 IST exactly
            _bar(datetime(2024, 3, 1, 3, 46, tzinfo=UTC), "100.5"),
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), "100.6"),  # 09:15:00 IST exactly
        ],
    )
    row = _index_minute_row(store, tmp_path)
    assert row["open_substituted_days"] == 0
    assert row["session_days"] == 2


def test_missing_0915_session_counts_open_substituted_regression(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # FAILS-BEFORE / PASSES-AFTER (the fix (c) bug): a session whose real 09:15 IST
    # minute is ABSENT (vendor gap) gets a derived 1-day open silently substituted
    # from its earliest present minute (tf_resampler `.first()`) with NO signal —
    # pre-fix the summary row carried NO substitution keys at all (KeyError here),
    # so the completeness surface could not expose it.
    _write_index_minutes(
        tmp_path,
        [
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), "100.4"),  # canonical 09:15 open
            _bar(datetime(2024, 3, 4, 3, 46, tzinfo=UTC), "100.5"),  # first minute 09:16 IST
        ],
    )
    row = _index_minute_row(store, tmp_path)
    assert row["open_substituted_days"] == 1
    assert row["session_days"] == 2


def test_late_special_open_session_counts_as_substituted(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # A genuine late/special open (e.g. a 10:00 IST special session) ALSO counts —
    # honestly: its derived daily open is still not the canonical 09:15 price. The
    # count is a SUBSTITUTION signal, not exclusively "vendor lost data". Mixed
    # sessions: exact-open (not counted) + 09:16 (counted) + 10:00 (counted) → 2/3.
    _write_index_minutes(
        tmp_path,
        [
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), "100.4"),  # 09:15 IST — canonical
            _bar(datetime(2024, 3, 4, 3, 46, tzinfo=UTC), "100.5"),  # 09:16 IST — substituted
            _bar(datetime(2024, 3, 5, 4, 30, tzinfo=UTC), "100.6"),  # 10:00 IST — substituted
        ],
    )
    row = _index_minute_row(store, tmp_path)
    assert row["open_substituted_days"] == 2
    assert row["session_days"] == 3


def test_open_substitution_empty_partition_reports_zero_zero(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # A discovered-but-ZERO-ROW 1minute partition aggregates the per-session CTE to
    # SQL NULLs → the honest (bars=0, no dates, 0/0 counts), never a crash.
    target = tmp_path / "bars" / "NSE" / "INDEX" / "EMPTYIDX" / "1minute" / "2024"
    target.mkdir(parents=True)
    store.connection.execute(
        'COPY (SELECT CAST(NULL AS TIMESTAMPTZ) AS "timestamp" WHERE 1=0) '
        f"TO '{(target / '03.parquet').as_posix()}'"
    )
    rows = summarize_spot_datasets(store.connection, tmp_path)
    [row] = rows
    assert row["symbol"] == "EMPTYIDX"
    assert row["bars"] == 0
    assert row["first_date"] is None and row["last_date"] is None
    assert row["open_substituted_days"] == 0
    assert row["session_days"] == 0


def test_open_substitution_none_for_derived_1day_but_counted_for_1minute(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # FAILS-BEFORE (keys absent): the substitution columns exist on EVERY summary row,
    # but they are COMPUTED only for the ONE fetched granularity (TRACK1_SPOT_INTERVAL,
    # 1minute) — a derived `1day` (or intraday-TF) row carries an honest None, never a
    # fabricated 0 ("checked, found none"). The CASH 1minute row is counted too (CASH
    # is a SCORED spot segment — SPOT_PROBE_SEGMENTS; the hostile-review perf fold
    # gates the CTE on the scored segments, not on the interval alone).
    _write_session_fixture(tmp_path)  # INDEX 1minute + INDEX 1day + CASH 1minute
    rows = summarize_spot_datasets(store.connection, tmp_path)
    by_key = {(r["segment"], r["interval"]): r for r in rows}
    one_day = by_key[("INDEX", "1day")]
    assert one_day["open_substituted_days"] is None
    assert one_day["session_days"] is None
    # CASH 1minute (RELIANCE, one 14:45-IST-first session) is counted: 1/1.
    cash_minute = by_key[("CASH", "1minute")]
    assert cash_minute["open_substituted_days"] == 1
    assert cash_minute["session_days"] == 1
    # INDEX 1minute: 14:45-first ×2 counted; the 02:00-IST-first rolled-over session
    # opens BEFORE 09:15 → NOT counted (the under-boundary side of the strict `>`).
    index_minute = by_key[("INDEX", "1minute")]
    assert index_minute["open_substituted_days"] == 2
    assert index_minute["session_days"] == 3


def test_open_substitution_none_for_fno_1minute_reference_partition(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Hostile-review perf fold: a REFERENCE_ONLY F&O `1minute` contract partition
    # keeps the historical flat 3-scalar aggregate on the default (hot-path) read —
    # its per-session substitution counts were computed then DISCARDED on the
    # #920-sensitive /completeness + /coverage pollers (the scored projections join
    # only INDEX/CASH). The row carries honest None counts, never a fabricated 0,
    # and its bars/first/last stay byte-identical to any 1minute read.
    _write_fno(
        tmp_path,
        [
            _bar(datetime(2024, 3, 1, 3, 46, tzinfo=UTC), "100.4"),  # 09:16 IST first
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), "100.6"),
        ],
        symbol="NIFTY24MAYFUT",
    )
    rows = summarize_spot_datasets(store.connection, tmp_path)
    [row] = rows
    assert row["segment"] == "FNO"
    assert row["interval"] == "1minute"
    assert row["bars"] == 2
    assert row["first_date"] == "2024-03-01"
    assert row["last_date"] == "2024-03-04"
    assert row["open_substituted_days"] is None
    assert row["session_days"] is None
    assert "session_dates" not in row


def test_fno_segment_opted_in_session_dates_takes_cte_and_counts(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # The DEC-COLDSTART-SEED-INTERIOR-GAP-001 extensibility path: a segment
    # EXPLICITLY opted in via `session_date_segments` takes the per-session CTE
    # even outside the scored spot segments — the row carries real substitution
    # counts AND its `session_dates` (`with_session_dates` ⇒ `use_session_cte`,
    # so an opted-in row can never miss its array_agg — the IndexError hazard the
    # gate refactor had to preserve against).
    _write_fno(
        tmp_path,
        [
            _bar(datetime(2024, 3, 1, 3, 46, tzinfo=UTC), "100.4"),  # 09:16 IST first
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), "100.6"),  # 09:15 IST exactly
        ],
        symbol="NIFTY24MAYFUT",
    )
    rows = summarize_spot_datasets(
        store.connection, tmp_path, session_date_segments=frozenset({"FNO"})
    )
    [row] = rows
    assert row["open_substituted_days"] == 1
    assert row["session_days"] == 2
    assert row["session_dates"] == frozenset({date(2024, 3, 1), date(2024, 3, 4)})


def test_folded_cte_matches_prechange_flat_aggregate_golden(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # GOLDEN (the fix (c) single-scan fold): the per-session CTE's dataset-level
    # bars / first_date / last_date are BYTE-EQUAL to the pre-change flat 3-scalar
    # aggregate — including across an IST midnight rollover (the 20:30 UTC bar) —
    # so folding the substitution count into ONE scan changed no historical value.
    _write_session_fixture(tmp_path)
    row = _index_minute_row(store, tmp_path)
    glob = (tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute").as_posix() + "/**/*.parquet"
    flat = store.connection.execute(
        'SELECT COUNT(DISTINCT "timestamp"), MIN("timestamp"), MAX("timestamp") '
        "FROM read_parquet(?, union_by_name=true)",
        [glob],
    ).fetchone()
    assert flat is not None
    assert row["bars"] == int(flat[0])
    assert row["first_date"] == _ist_date(flat[1])
    assert row["last_date"] == _ist_date(flat[2])


def test_ist_ts_expr_is_the_one_offset_source() -> None:
    # COMMON pin: `_IST_DATE_EXPR` derives from `_IST_TS_EXPR` (one 330-minute literal —
    # no second offset can drift), and the session-open minutes derive from the ONE
    # canonical `brutex.data.session.SESSION_OPEN` (09:15 → 555), never a 4th literal.
    from brutex.data.session import SESSION_OPEN

    assert f"{_IST_TS_EXPR}::DATE" == _IST_DATE_EXPR
    assert _SESSION_OPEN_MINUTES == SESSION_OPEN.hour * 60 + SESSION_OPEN.minute == 555


def test_iso_date_renders_date_and_none() -> None:
    # The CTE returns already-IST DATE cells: a date renders ISO; SQL NULL → None;
    # a non-date cell (defensive) → None.
    assert _iso_date(date(2024, 3, 1)) == "2024-03-01"
    assert _iso_date(None) is None
    assert _iso_date("2024-03-01") is None


def test_resample_math_unchanged_daily_open_substitution_oracle(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # BUG-REGRESSION ORACLE (fix (c) is read-side ONLY): for a session missing its
    # 09:15 IST minute, the 1m→daily resampler STILL substitutes the earliest present
    # minute's open (tf_resampler.py `.first()` — deliberately UNCHANGED by this fix),
    # while the completeness surface now REPORTS the substitution. Detection with NO
    # resample-math change.
    pl = pytest.importorskip("polars")
    from brutex.data.tf_resampler import resample_1m_to_tf

    # Session 2024-03-04 IST: first present minute 09:16 IST (03:46 UTC) — 09:15 absent.
    df_1m = pl.DataFrame(
        {
            "ts_utc": [datetime(2024, 3, 4, 3, 46), datetime(2024, 3, 4, 3, 47)],
            "open": [200.5, 201.0],
            "high": [201.5, 202.0],
            "low": [200.0, 200.5],
            "close": [201.0, 201.5],
            "volume": [10, 10],
        },
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )
    daily = resample_1m_to_tf(df_1m, 375)  # the daily bucket
    assert daily.height == 1
    # The derived daily open IS the substituted 09:16 open — byte-identical to the
    # pre-change resampler (this fix never touches the math).
    assert daily.get_column("open")[0] == 200.5
    # …and the SAME session, written to a real lake, is now REPORTED substituted.
    _write_index_minutes(
        tmp_path,
        [
            _ocbar(
                datetime(2024, 3, 4, 3, 46, tzinfo=UTC),
                open_="200.5",
                high="201.5",
                low="200.0",
                close="201.0",
            ),
            _ocbar(
                datetime(2024, 3, 4, 3, 47, tzinfo=UTC),
                open_="201.0",
                high="202.0",
                low="200.5",
                close="201.5",
            ),
        ],
    )
    row = _index_minute_row(store, tmp_path)
    assert row["open_substituted_days"] == 1
    assert row["session_days"] == 1


# ────────────────────────────────────────────────────────────────────────
# Derived %-change columns (DEC-DB-CANDLE-DERIVED-001)
# ────────────────────────────────────────────────────────────────────────


def _ocbar(ts: datetime, *, open_: str, high: str, low: str, close: str) -> CandleBar:
    """A spot bar with explicit OHLC (the gap test needs a known open)."""
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


# ── _to_decimal ──────────────────────────────────────────────────────


def test_to_decimal_passthrough_and_coerce() -> None:
    d = Decimal("100.5")
    assert _to_decimal(d) is d  # a Decimal passes through untouched
    # a DuckDB float is coerced exactly via its str() repr
    assert _to_decimal(100.5) == Decimal("100.5")
    assert _to_decimal(0.0) == Decimal("0.0")


# ── _bar_ist_date / _bar_ist_time ────────────────────────────────────


def test_bar_ist_date_tz_aware_and_naive() -> None:
    # 18:30Z is 00:00 IST the NEXT calendar day → the IST trading date rolls.
    assert _bar_ist_date(datetime(2024, 2, 29, 18, 30, tzinfo=UTC)) == date(2024, 3, 1)
    # 03:45Z is 09:15 IST same day.
    assert _bar_ist_date(datetime(2024, 3, 4, 3, 45, tzinfo=UTC)) == date(2024, 3, 4)
    # naive (defensive) is read as-is.
    assert _bar_ist_date(datetime(2024, 3, 1, 0, 0, 0)) == date(2024, 3, 1)


def test_bar_ist_time_tz_aware_and_naive() -> None:
    assert _bar_ist_time(datetime(2024, 3, 4, 3, 45, tzinfo=UTC)) == time(9, 15)
    assert _bar_ist_time(datetime(2024, 3, 4, 9, 15, 0)) == time(9, 15)  # naive as-is


# ── _intraday_last_close_by_date ─────────────────────────────────────


def test_intraday_last_close_missing_partition_is_empty(
    store: InstrumentStore, tmp_path: Path
) -> None:
    out = _intraday_last_close_by_date(
        store.connection, tmp_path, exchange="NSE", segment="CASH", symbol="NSE-NIFTY"
    )
    assert out == {}


def test_intraday_last_close_picks_each_days_final_1min_bar(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Two minute bars per day → arg_max(close, timestamp) must pick the LATER
    # bar's close (the 15:29 IST "previous close" the engine baselines on).
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=[
            # 2024-03-01: 09:15 close 99, 15:29 close 100 → last = 100.
            _ocbar(
                datetime(2024, 3, 1, 3, 45, tzinfo=UTC),
                open_="98",
                high="101",
                low="97",
                close="99",
            ),
            _ocbar(
                datetime(2024, 3, 1, 9, 59, tzinfo=UTC),
                open_="99",
                high="101",
                low="98",
                close="100",
            ),
            # 2024-03-04: 09:15 close 111, 15:29 close 110 → last = 110 (drifts DOWN
            # into the close, exactly the COALINDIA case: last 1-min ≠ daily VWAP).
            _ocbar(
                datetime(2024, 3, 4, 3, 45, tzinfo=UTC),
                open_="112",
                high="113",
                low="109",
                close="111",
            ),
            _ocbar(
                datetime(2024, 3, 4, 9, 59, tzinfo=UTC),
                open_="111",
                high="112",
                low="109",
                close="110",
            ),
        ],
    )
    out = _intraday_last_close_by_date(
        store.connection, tmp_path, exchange="NSE", segment="CASH", symbol="NSE-NIFTY"
    )
    assert set(out) == {date(2024, 3, 1), date(2024, 3, 4)}
    assert out[date(2024, 3, 1)] == Decimal("100.0")  # the 15:29 close, not the 09:15 99
    assert out[date(2024, 3, 4)] == Decimal("110.0")


# ── _intraday_open_by_date (DEC-PREOPEN-MINUTE-BASIS-001) ────────────


def test_intraday_open_missing_partition_is_empty(store: InstrumentStore, tmp_path: Path) -> None:
    out = _intraday_open_by_date(
        store.connection, tmp_path, exchange="NSE", segment="CASH", symbol="NSE-NIFTY"
    )
    assert out == {}


def test_intraday_open_picks_each_days_first_1min_bar(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # Two minute bars per day → arg_min(open, timestamp) must pick the EARLIER
    # bar's open (the 09:15 IST session-open OPEN = the minute-basis IEP).
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=[
            # 2024-03-01: 09:15 open 98 (the IEP), 15:29 open 104.
            _ocbar(
                datetime(2024, 3, 1, 3, 45, tzinfo=UTC),
                open_="98",
                high="101",
                low="98",
                close="100",
            ),
            _ocbar(
                datetime(2024, 3, 1, 9, 59, tzinfo=UTC),
                open_="104",
                high="106",
                low="103",
                close="105",
            ),
            # 2024-03-04: 09:15 open 112 (the IEP), 15:29 open 111.
            _ocbar(
                datetime(2024, 3, 4, 3, 45, tzinfo=UTC),
                open_="112",
                high="115",
                low="110",
                close="114",
            ),
            _ocbar(
                datetime(2024, 3, 4, 9, 59, tzinfo=UTC),
                open_="111",
                high="113",
                low="109",
                close="110",
            ),
        ],
    )
    out = _intraday_open_by_date(
        store.connection, tmp_path, exchange="NSE", segment="CASH", symbol="NSE-NIFTY"
    )
    assert set(out) == {date(2024, 3, 1), date(2024, 3, 4)}
    assert out[date(2024, 3, 1)] == Decimal("98.0")  # the 09:15 open, not the 15:29 104
    assert out[date(2024, 3, 4)] == Decimal("112.0")


# ── _prev_trading_close ──────────────────────────────────────────────


def test_prev_trading_close_empty() -> None:
    assert _prev_trading_close({}) == {}


def test_prev_trading_close_maps_each_day_to_prior_close() -> None:
    daily = {
        date(2024, 3, 1): Decimal("100"),
        date(2024, 3, 4): Decimal("110"),
        date(2024, 3, 5): Decimal("120"),
    }
    # first day absent (no predecessor); each later day → the prior day's close.
    assert _prev_trading_close(daily) == {
        date(2024, 3, 4): Decimal("100"),
        date(2024, 3, 5): Decimal("110"),
    }


# ── _daily_close_by_date / _daily_open_by_date (the 1-day readers) ────


def test_daily_readers_empty_partition(store: InstrumentStore, tmp_path: Path) -> None:
    # No 1day partition → both readers return an empty map (the blank-degrade path).
    assert (
        _daily_close_by_date(store.connection, tmp_path, exchange="NSE", segment="CASH", symbol="X")
        == {}
    )
    assert (
        _daily_open_by_date(store.connection, tmp_path, exchange="NSE", segment="CASH", symbol="X")
        == {}
    )


def test_daily_readers_keyed_by_ist_date(store: InstrumentStore, tmp_path: Path) -> None:
    # The 1day close + open per IST trading date (DEC-DB-CANDLE-PERINTERVAL-001).
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="SBILIFE",
        interval="1day",
        bars=[
            _ocbar(
                datetime(2021, 1, 1, 3, 45, tzinfo=UTC),
                open_="903",
                high="907",
                low="894",
                close="895.4",
            ),
            _ocbar(
                datetime(2021, 1, 4, 3, 45, tzinfo=UTC),
                open_="900",
                high="917",
                low="894.5",
                close="911.4",
            ),
        ],
    )
    closes = _daily_close_by_date(
        store.connection, tmp_path, exchange="NSE", segment="CASH", symbol="SBILIFE"
    )
    opens = _daily_open_by_date(
        store.connection, tmp_path, exchange="NSE", segment="CASH", symbol="SBILIFE"
    )
    assert closes == {date(2021, 1, 1): Decimal("895.4"), date(2021, 1, 4): Decimal("911.4")}
    assert opens == {date(2021, 1, 1): Decimal("903"), date(2021, 1, 4): Decimal("900")}


# ── _spot_derived_rows (every branch) ────────────────────────────────

_ALL: list[str] = list(CANDLE_COLUMNS)  # timestamp, open, high, low, close, volume, open_interest
_PREV = {date(2024, 3, 4): Decimal("100")}


def _row(ts: object, *, open_: object = "105", close: object = "110") -> tuple[object, ...]:
    """A raw candle row aligned to _ALL (timestamp, open, high, low, close, vol, oi)."""
    return (ts, open_, "112", "104", close, 10, None)


def test_spot_derived_915_bar_has_change_and_gap() -> None:
    ts = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)  # 09:15 IST on a day in _PREV
    [(change, gap)] = _spot_derived_rows([_row(ts)], _ALL, _PREV)
    assert float(change) == pytest.approx(10.0)  # (110-100)/100*100
    assert float(gap) == pytest.approx(5.0)  # (105-100)/100*100


def test_spot_derived_non_open_bar_has_change_only() -> None:
    ts = datetime(2024, 3, 4, 3, 46, tzinfo=UTC)  # 09:16 IST → not the gap anchor
    [(change, gap)] = _spot_derived_rows([_row(ts)], _ALL, _PREV)
    assert float(change) == pytest.approx(10.0)
    assert gap is None


def test_spot_derived_date_absent_from_prev_map_is_blank() -> None:
    ts = datetime(2024, 3, 1, 3, 45, tzinfo=UTC)  # date not in _PREV (first day)
    assert _spot_derived_rows([_row(ts)], _ALL, _PREV) == [(None, None)]


def test_spot_derived_non_datetime_timestamp_is_blank() -> None:
    # A non-datetime ts cell (never the real column) → both blank.
    assert _spot_derived_rows([_row("not-a-dt")], _ALL, _PREV) == [(None, None)]


def test_spot_derived_no_timestamp_column_is_blank() -> None:
    # Projection without "timestamp" → no date → both blank.
    cols = ["open", "close"]
    assert _spot_derived_rows([("105", "110")], cols, _PREV) == [(None, None)]


def test_spot_derived_no_close_column_keeps_gap_only() -> None:
    # timestamp+open but NO close → change blank, gap still computed on the 09:15 bar.
    cols = ["timestamp", "open"]
    ts = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)
    [(change, gap)] = _spot_derived_rows([(ts, "105")], cols, _PREV)
    assert change is None
    assert float(gap) == pytest.approx(5.0)


def test_spot_derived_no_open_column_keeps_change_only() -> None:
    # timestamp+close but NO open → gap blank even on the 09:15 bar; change set.
    cols = ["timestamp", "close"]
    ts = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)
    [(change, gap)] = _spot_derived_rows([(ts, "110")], cols, _PREV)
    assert float(change) == pytest.approx(10.0)
    assert gap is None


def test_spot_derived_zero_prev_close_is_blank() -> None:
    # A zero previous close must not divide-by-zero (pct_change returns None).
    prev = {date(2024, 3, 4): Decimal("0")}
    ts = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)
    assert _spot_derived_rows([_row(ts)], _ALL, prev) == [(None, None)]


def test_spot_derived_1day_gap_on_every_bar() -> None:
    # On the 1-day basis (DEC-DB-CANDLE-PERINTERVAL-001) the open-vs-prior-close
    # gap is computed on EVERY bar — not gated to the 09:15 IST instant (a 1-day
    # bar is stamped at midnight/other times, which is why it used to read null).
    ts = datetime(2024, 3, 4, 0, 0, tzinfo=UTC)  # IST 05:30 — NOT the 09:15 anchor
    [(change, gap)] = _spot_derived_rows([_row(ts)], _ALL, _PREV, interval="1day")
    assert float(change) == pytest.approx(10.0)  # (110-100)/100*100
    assert float(gap) == pytest.approx(5.0)  # (105-100)/100*100 — gap on a non-09:15 bar


# ── query_candles end-to-end with the derived columns ────────────────


def test_query_candles_1day_uses_1day_baseline_and_fills_gap(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # SBILIFE 2021-01-04 (Dhan/NSE: +1.79%): the 1-day view must baseline on the
    # prior 1-DAY close (895.4), NOT the prior 1-minute close — and fill the
    # open_gap_pct that used to read null (DEC-DB-CANDLE-PERINTERVAL-001).
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="SBILIFE",
        interval="1day",
        bars=[
            _ocbar(
                datetime(2021, 1, 1, 3, 45, tzinfo=UTC),
                open_="903",
                high="907",
                low="894",
                close="895.4",
            ),
            _ocbar(
                datetime(2021, 1, 4, 3, 45, tzinfo=UTC),
                open_="900",
                high="917",
                low="894.5",
                close="911.4",
            ),
        ],
    )
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="SBILIFE",
        interval="1day",
        limit=50,
    )
    cols = out["columns"]
    ci = cols.index("change_pct")
    gi = cols.index("open_gap_pct")
    row = next(r for r in out["rows"] if str(r[0]) == "2021-01-04")
    assert round(float(row[ci]), 2) == 1.79  # (911.4-895.4)/895.4*100 — matches NSE
    assert round(float(row[gi]), 2) == 0.51  # (900-895.4)/895.4*100 — the 1day gap


def test_query_candles_derived_columns_end_to_end(store: InstrumentStore, tmp_path: Path) -> None:
    # Baseline = prior day's LAST 1-minute close (DEC-SPOT-PREVCLOSE-1MIN-001):
    # 2024-03-01's final 1-min close is 100, so it anchors 2024-03-04's bars.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=[
            # 2024-03-01: last 1-min close = 100 (the 09:59Z = 15:29 IST bar).
            _ocbar(
                datetime(2024, 3, 1, 3, 45, tzinfo=UTC),
                open_="95",
                high="101",
                low="94",
                close="98",
            ),
            _ocbar(
                datetime(2024, 3, 1, 9, 59, tzinfo=UTC),
                open_="98",
                high="101",
                low="97",
                close="100",
            ),
            # 2024-03-04: the 09:15 IST open bar + a later bar (anchored on 100).
            _ocbar(
                datetime(2024, 3, 4, 3, 45, tzinfo=UTC),
                open_="105",
                high="112",
                low="104",
                close="110",
            ),
            _ocbar(
                datetime(2024, 3, 4, 3, 46, tzinfo=UTC),
                open_="110",
                high="122",
                low="109",
                close="121",
            ),
        ],
    )
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=50,
    )
    assert out["columns"][-2:] == list(SPOT_DERIVED_COLUMNS)
    ts_i = out["columns"].index("timestamp")
    chg_i = out["columns"].index("change_pct")
    gap_i = out["columns"].index("open_gap_pct")
    by_ts = {r[ts_i]: r for r in out["rows"]}
    open_row = by_ts["2024-03-04 09:15:00"]
    later_row = by_ts["2024-03-04 09:16:00"]
    assert float(open_row[chg_i]) == pytest.approx(10.0)  # (110-100)/100*100
    assert float(open_row[gap_i]) == pytest.approx(5.0)  # (105-100)/100*100
    assert float(later_row[chg_i]) == pytest.approx(21.0)  # (121-100)/100*100
    assert later_row[gap_i] is None  # gap only on the 09:15 bar
    # 2024-03-01 is the FIRST day in the 1-min lake → no prior close → blank.
    assert by_ts["2024-03-01 09:15:00"][chg_i] is None
    assert by_ts["2024-03-01 09:15:00"][gap_i] is None


def test_query_candles_daily_interval_is_date_only_with_change(
    store: InstrumentStore, tmp_path: Path
) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1day",
        bars=[
            _ocbar(
                datetime(2024, 3, 1, 3, 45, tzinfo=UTC),
                open_="100",
                high="101",
                low="99",
                close="100",
            ),
            _ocbar(
                datetime(2024, 3, 4, 3, 45, tzinfo=UTC),
                open_="110",
                high="111",
                low="109",
                close="110",
            ),
        ],
    )
    # The 1day VIEW baselines on the prior 1-DAY close, NOT the 1-minute lake
    # (DEC-DB-CANDLE-PERINTERVAL-001): 2024-03-01's 1-day close is 100 → anchors
    # the 2024-03-04 daily bar. No 1minute partition is needed.
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1day",
        limit=50,
    )
    ts_i = out["columns"].index("timestamp")
    chg_i = out["columns"].index("change_pct")
    rows = {r[ts_i]: r for r in out["rows"]}
    # DATE-ONLY render for the 1day interval (no 00:00:00 / HH:MM:SS noise).
    assert set(rows) == {"2024-03-01", "2024-03-04"}
    assert rows["2024-03-01"][chg_i] is None  # first day → no previous close
    assert float(rows["2024-03-04"][chg_i]) == pytest.approx(10.0)  # (110-100)/100*100


def test_query_candles_1day_self_baselines_without_intraday(
    store: InstrumentStore, tmp_path: Path
) -> None:
    # A 1day view derives from the 1-DAY close ALONE (DEC-DB-CANDLE-PERINTERVAL-001)
    # — it no longer needs a 1minute partition. _write_candles writes 1day bars
    # 2024-03-01 (close 100.4) + 2024-03-02 (open 100, close 100.8).
    _write_candles(tmp_path, symbol="NSE-NIFTY", interval="1day")
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1day",
        limit=50,
    )
    ts_i = out["columns"].index("timestamp")
    chg_i = out["columns"].index("change_pct")
    gap_i = out["columns"].index("open_gap_pct")
    rows = {r[ts_i]: r for r in out["rows"]}
    # 2024-03-01 is the first day → no prior 1-day close → blank.
    assert rows["2024-03-01"][chg_i] is None
    assert rows["2024-03-01"][gap_i] is None
    # 2024-03-02 baselines on 03-01's 1-day close (100.4), same basis:
    assert round(float(rows["2024-03-02"][chg_i]), 2) == 0.40  # (100.8-100.4)/100.4*100
    assert round(float(rows["2024-03-02"][gap_i]), 2) == -0.40  # (100-100.4)/100.4*100


# ── Page-bounded intraday %-change baseline (DEC-DB-UNIVERSE-O1-LATENCY-001) ──
#
# The /db candle pager appends derived %-change / open-gap columns to every page.
# Computing them needs the prior trading day's last 1-minute close. The OLD code
# aggregated the WHOLE multi-year 1-minute partition on EVERY paging click; the
# new path reads only the page's own date span (+ the single predecessor bar). The
# tests below pin BOTH the branch behaviour AND byte-for-byte equality with the
# full-partition scan it replaces (zero correctness change — Golden Rule #7).


def _next_weekday(d: date) -> date:
    """First weekday on/after *d* (Sat/Sun skipped) — a synthetic trading day."""
    while d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        d += timedelta(days=1)
    return d


def _write_session_minutes(
    lake_root: Path,
    *,
    days: int,
    bars_per_day: int = 3,
    symbol: str = "NSE-NIFTY",
) -> list[date]:
    """Write *days* session-1-minute trading days (weekends skipped) for *symbol*.

    Each trading day gets *bars_per_day* bars from 09:15 IST (03:45 UTC), one per
    minute, with a UNIQUE close per (day, bar) so a byte-for-byte comparison of the
    derived baseline is meaningful. The weekend skips create real gaps between
    consecutive trading dates — exercising the "previous trading day across a
    non-trading gap" path. Returns the ordered list of trading dates written.
    """
    writer = ParquetWriter(lake_root=lake_root)
    bars: list[CandleBar] = []
    written: list[date] = []
    cursor = _next_weekday(date(2024, 3, 1))
    for di in range(days):
        cursor = _next_weekday(cursor)
        written.append(cursor)
        base = datetime(cursor.year, cursor.month, cursor.day, 3, 45, tzinfo=UTC)
        for bi in range(bars_per_day):
            bars.append(
                _ocbar(
                    base + timedelta(minutes=bi),
                    open_=str(99 + di),
                    high=str(110 + di),
                    low=str(90 + di),
                    # Unique, ascending within the day → last bar's close is distinct.
                    close=f"{100 + di}.{bi}",
                )
            )
        cursor += timedelta(days=1)
    writer.write(exchange="NSE", segment="CASH", symbol=symbol, interval="1minute", bars=bars)
    return written


def test_page_ist_dates_no_timestamp_column_is_empty() -> None:
    # No "timestamp" in the projection → the derived columns are blank anyway, so
    # there are no dates to baseline (ts_idx < 0 branch).
    assert _page_ist_dates([(100.0, 101.0)], ["open", "close"]) == set()


def test_page_ist_dates_extracts_distinct_ist_dates_skipping_non_datetime() -> None:
    rows = [
        (datetime(2024, 3, 1, 3, 45, tzinfo=UTC), Decimal("100")),  # 09:15 IST → 03-01
        (datetime(2024, 3, 1, 9, 59, tzinfo=UTC), Decimal("101")),  # 15:29 IST → 03-01
        (datetime(2024, 3, 4, 3, 45, tzinfo=UTC), Decimal("102")),  # 03-04
        (None, Decimal("103")),  # non-datetime cell is skipped (isinstance False arm)
    ]
    assert _page_ist_dates(rows, ["timestamp", "close"]) == {date(2024, 3, 1), date(2024, 3, 4)}


def test_intraday_prev_close_empty_page_dates_is_empty(
    store: InstrumentStore, tmp_path: Path
) -> None:
    _write_session_minutes(tmp_path, days=3)
    out = _intraday_prev_close_by_page_date(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        page_dates=set(),
    )
    assert out == {}


def test_intraday_prev_close_missing_partition_is_empty(
    store: InstrumentStore, tmp_path: Path
) -> None:
    out = _intraday_prev_close_by_page_date(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        page_dates={date(2024, 3, 1)},
    )
    assert out == {}


def test_intraday_prev_close_first_day_has_no_predecessor(
    store: InstrumentStore, tmp_path: Path
) -> None:
    dates = _write_session_minutes(tmp_path, days=3, bars_per_day=3)
    out = _intraday_prev_close_by_page_date(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        page_dates={dates[0]},
    )
    # The earliest stored day has no predecessor bar → no prev-close entry → the
    # derived columns degrade to blank (pred is None branch).
    assert out == {}


def test_intraday_prev_close_interior_day_uses_prior_days_last_close(
    store: InstrumentStore, tmp_path: Path
) -> None:
    dates = _write_session_minutes(tmp_path, days=3, bars_per_day=3)
    out = _intraday_prev_close_by_page_date(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        page_dates={dates[1]},
    )
    # Day 0's bars close 100.0 / 100.1 / 100.2 → the 15:29 (last) close is 100.2,
    # read by the single predecessor query — NOT the whole partition.
    assert out == {dates[1]: Decimal("100.2")}


@pytest.mark.parametrize("days,bars_per_day", [(2, 1), (5, 3), (8, 2), (11, 1)])
def test_intraday_prev_close_page_bounded_matches_full_scan(
    store: InstrumentStore, tmp_path: Path, days: int, bars_per_day: int
) -> None:
    """The page-bounded baseline equals the full-partition scan for EVERY window.

    In production a page's distinct dates form a CONTIGUOUS run of trading days (a
    page is a LIMIT/OFFSET slice of timestamp-sorted bars), so this exhausts every
    contiguous window. The helper is in fact robust to NON-contiguous page_dates too
    — the span query fills the intervening days, so the predecessor chain is complete
    regardless — which the endpoints-only ``{lo, hi}`` check below pins. Either way
    the bounded helper returns the SAME prev-close for each page date as the
    full-partition reference: exhaustive byte-for-byte proof.
    """
    _write_session_minutes(tmp_path, days=days, bars_per_day=bars_per_day)
    coords = {"exchange": "NSE", "segment": "CASH", "symbol": "NSE-NIFTY"}
    reference = _prev_trading_close(
        _intraday_last_close_by_date(store.connection, tmp_path, **coords)
    )
    all_dates = sorted(_intraday_last_close_by_date(store.connection, tmp_path, **coords))
    for i in range(len(all_dates)):
        for j in range(i, len(all_dates)):
            # The contiguous window a real page produces …
            window = set(all_dates[i : j + 1])
            # … AND the NON-contiguous endpoints-only subset (proves the span query
            # fills the gap — the helper does not rely on the page being dense).
            sparse = {all_dates[i], all_dates[j]}
            for page in (window, sparse):
                bounded = _intraday_prev_close_by_page_date(
                    store.connection, tmp_path, **coords, page_dates=page
                )
                for d in page:
                    assert bounded.get(d) == reference.get(d), (d, sorted(page))


@pytest.mark.parametrize("descending", [False, True])
def test_query_candles_chunked_paging_is_byte_identical_to_one_page(
    store: InstrumentStore, tmp_path: Path, descending: bool
) -> None:
    """Paging in tiny chunks yields rows IDENTICAL to one big page — derived cols too.

    Each 5-row page recomputes its OWN page-bounded baseline; concatenating them
    must reproduce the single-query result bar-for-bar, including the derived
    change_pct / open_gap_pct columns (Golden Rule #7 idempotence).
    """
    _write_session_minutes(tmp_path, days=6, bars_per_day=4)

    def _page(limit: int, offset: int) -> dict[str, Any]:
        return query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            descending=descending,
            limit=limit,
            offset=offset,
        )

    full = _page(500, 0)
    paged: list[list[object]] = []
    offset = 0
    while True:
        page = _page(5, offset)
        paged.extend(page["rows"])
        if offset + len(page["rows"]) >= page["total"] or not page["rows"]:
            break
        offset += 5
    assert paged == full["rows"]


def test_query_candles_coarse_interval_baselines_on_1minute_page_span(
    store: InstrumentStore, tmp_path: Path
) -> None:
    """A coarse intraday timeframe still baselines on the 1-minute feed underneath.

    DEC-SPOT-PREVCLOSE-1MIN-001: viewing a 15-minute partition derives %-change
    from the prior day's last 1-MINUTE close. The page-bounded reader must read the
    1minute partition for the VIEWED page's dates (not the 15minute partition).
    """
    _write_session_minutes(tmp_path, days=3, bars_per_day=3, symbol="NSE-NIFTY")
    # A single 15-minute bar on day 1 (the viewed timeframe); its IST date drives
    # the 1-minute baseline lookup.
    dates = (
        _next_weekday(date(2024, 3, 1)),
        _next_weekday(_next_weekday(date(2024, 3, 1)) + timedelta(days=1)),
    )
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="15minute",
        bars=[
            _ocbar(
                datetime(dates[1].year, dates[1].month, dates[1].day, 3, 45, tzinfo=UTC),
                open_="200",
                high="210",
                low="190",
                close="205",
            )
        ],
    )
    out = query_candles(
        store.connection,
        tmp_path,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="15minute",
        limit=50,
    )
    chg_i = out["columns"].index("change_pct")
    # Day 0's last 1-minute close is 100.2 → change = (205 - 100.2) / 100.2 * 100.
    assert out["rows"][0][chg_i] is not None
    assert round(float(out["rows"][0][chg_i]), 4) == round((205 - 100.2) / 100.2 * 100, 4)


def test_clamp_offset_caps_above_int64_and_floors_negative() -> None:
    """clamp_offset bounds the OFFSET into DuckDB's bindable int64 range so a
    hostile huge / negative offset never throws at the bind (DEC-DB-VIEWER-PAGINATE-ALL-001
    hostile-review MINOR-1)."""
    from brutex.dashboard.db_view import _OFFSET_MAX, clamp_offset

    assert clamp_offset(-5) == 0
    assert clamp_offset(0) == 0
    assert clamp_offset(42) == 42
    assert clamp_offset(_OFFSET_MAX) == _OFFSET_MAX
    assert clamp_offset(10**19) == _OFFSET_MAX  # > 2**63 - 1 → capped, not overflowed


# ────────────────────────────────────────────────────────────────────────
# F&O stored Greeks overlay projection (DEC-GREEKS-ENGINE-PR3-001)
# ────────────────────────────────────────────────────────────────────────

#: A decodable F&O option contract symbol (underlying spot absent on purpose —
#: the derived moneyness cells honestly read "?" without breaking the page).
_GREEKS_SYMBOL = "NSE-NIFTY-30Jun25-24000-CE"


def _greeks_bar(ts: datetime, close: str) -> FnoGreeksBar:
    """One ENRICHED option bar carrying the full stored Greeks overlay."""
    c = Decimal(close)
    return FnoGreeksBar(
        timestamp=ts,
        open=c,
        high=c,
        low=c,
        close=c,
        volume=25,
        open_interest=1000,
        iv=0.1812,
        delta=0.62,
        gamma=0.0004,
        theta=-3.5,
        vega=11.2,
        rho=4.1,
        spot_at_bar=Decimal("24000.00"),
        t_years_used=0.076,
        rate_used=0.0655,
        greeks_provenance_id=1234,
    )


def _write_fno(lake_root: Path, bars: list[CandleBar], symbol: str = _GREEKS_SYMBOL) -> None:
    """Write one F&O 1-minute partition batch via the production writer."""
    ParquetWriter(lake_root=lake_root).write(
        exchange="NSE", segment="FNO", symbol=symbol, interval="1minute", bars=bars
    )


class TestFnoGreeksProjection:
    """The 10 stored per-bar Greeks overlay columns on an F&O candle page."""

    def test_column_map_mirrors_writer_schema(self) -> None:
        """The viewer's selectable overlay set can never drift from the writer's
        (models.GREEKS_OVERLAY_FIELDS is the ONE source of truth — §11 COMMON)."""
        assert FNO_GREEKS_COLUMNS == GREEKS_OVERLAY_FIELDS
        assert tuple(FNO_GREEKS_COLUMN_TYPES) == GREEKS_OVERLAY_FIELDS
        # Every overlay column has a concrete SQL type for the typed-NULL cast.
        assert set(FNO_GREEKS_COLUMN_TYPES.values()) == {"DOUBLE", "INTEGER"}
        assert FNO_GREEKS_COLUMN_TYPES["greeks_provenance_id"] == "INTEGER"

    def test_enriched_partition_returns_stored_values(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _write_fno(tmp_path, [_greeks_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "150.00")])
        out = query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=_GREEKS_SYMBOL,
            interval="1minute",
            columns=("timestamp", "close", "iv", "delta", "greeks_provenance_id"),
            limit=10,
        )
        assert out["columns"][:5] == ["timestamp", "close", "iv", "delta", "greeks_provenance_id"]
        row = out["rows"][0]
        assert float(row[2]) == pytest.approx(0.1812)
        assert float(row[3]) == pytest.approx(0.62)
        assert int(row[4]) == 1234

    def test_legacy_partition_renders_typed_nulls(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """A pre-Greeks 7-column partition tree serves honest NULL cells for every
        requested overlay column instead of a binder-error 500 (Golden Rule #1)."""
        _write_fno(tmp_path, [_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.40")])
        out = query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=_GREEKS_SYMBOL,
            interval="1minute",
            columns=("timestamp", *FNO_GREEKS_COLUMNS),
            limit=10,
        )
        assert out["columns"][: 1 + len(FNO_GREEKS_COLUMNS)] == ["timestamp", *FNO_GREEKS_COLUMNS]
        assert out["total"] == 1
        overlay_cells = out["rows"][0][1 : 1 + len(FNO_GREEKS_COLUMNS)]
        assert all(cell is None for cell in overlay_cells)

    def test_mixed_tree_unions_legacy_and_enriched(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """A mixed legacy/enriched tree pages both file generations in one query:
        the legacy month's overlay cells are NULL, the enriched month's are real."""
        _write_fno(tmp_path, [_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.20")])
        _write_fno(tmp_path, [_greeks_bar(datetime(2024, 4, 1, 9, 15, tzinfo=UTC), "150.00")])
        out = query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=_GREEKS_SYMBOL,
            interval="1minute",
            columns=("timestamp", "iv"),
            limit=10,
        )
        assert out["total"] == 2
        ivs = [row[1] for row in out["rows"]]
        assert ivs[0] is None
        assert float(ivs[1]) == pytest.approx(0.1812)

    def test_spot_page_rejects_overlay_column(self, store: InstrumentStore, tmp_path: Path) -> None:
        """The overlay exists only under segment="FNO" — a spot page still rejects
        a Greeks column as unknown (never a silently-NULL spot Greek)."""
        _write_candles(tmp_path)
        with pytest.raises(ValueError, match="unknown column"):
            query_candles(
                store.connection,
                tmp_path,
                exchange="NSE",
                segment="CASH",
                symbol="NSE-NIFTY",
                interval="1minute",
                columns=("timestamp", "iv"),
                limit=10,
            )

    def test_select_star_carries_stored_overlay(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _write_fno(tmp_path, [_greeks_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "150.00")])
        out = query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=_GREEKS_SYMBOL,
            interval="1minute",
            limit=10,
        )
        for column in FNO_GREEKS_COLUMNS:
            assert column in out["columns"]

    def test_empty_partition_keeps_requested_overlay_columns(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """The empty-page early return echoes the requested overlay columns so the
        viewer's column set is stable across an empty vs populated partition."""
        out = query_candles(
            store.connection,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=_GREEKS_SYMBOL,
            interval="1minute",
            columns=("timestamp", "iv"),
            limit=10,
        )
        assert out["rows"] == []
        assert out["columns"][:2] == ["timestamp", "iv"]
