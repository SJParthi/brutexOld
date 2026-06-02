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
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.dashboard.db_view import (
    ALLOWED_OPERATORS,
    CANDLE_COLUMNS,
    OPTION_CANDLE_COLUMNS,
    _format_candle_timestamp,
    _ist_date,
    _quote_ident,
    list_candle_datasets,
    list_columns,
    list_option_datasets,
    query_candles,
    query_option_candles,
    query_table,
    summarize_spot_datasets,
)
from brutex.data.instrument_store import Instrument, InstrumentStore
from brutex.data.option_parquet_writer import OptionParquetWriter
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, InstrumentType, Segment

from brutex.data.models import CandleBar, OptionBar  # isort: skip  (kept near other data imports)


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


def _write_options(lake_root: Path, partition_key: str = _OPT_PK) -> None:
    """Write a CE+PE option pair across two trading days via the real writer."""
    OptionParquetWriter(lake_root=lake_root).write(
        partition_key=partition_key,
        bars=[
            _obar(datetime(2024, 3, 1, 4, 0, tzinfo=UTC), "CE", "105.004"),
            _obar(datetime(2024, 3, 1, 4, 0, tzinfo=UTC), "PE", "95.5"),
            _obar(datetime(2024, 3, 2, 4, 0, tzinfo=UTC), "CE", "106.2"),
            _obar(datetime(2024, 3, 2, 4, 0, tzinfo=UTC), "PE", "96.1"),
        ],
    )


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
    (bars / "NSE" / "CASH" / "BAR" / "7minute").mkdir(parents=True)
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
    assert out["columns"] == list(CANDLE_COLUMNS)
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
    assert out["columns"] == ["timestamp", "close"]
    assert all(len(r) == 2 for r in out["rows"])


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
        _format_candle_timestamp(datetime(2026, 5, 29, 3, 30, tzinfo=UTC)) == "2026-05-29 09:00:00"
    )


def test_format_candle_timestamp_naive_datetime_kept_as_is() -> None:
    # Defensive branch: a naive datetime (DuckDB returns tz-aware in practice)
    # is formatted as-is, still without an offset suffix.
    assert _format_candle_timestamp(datetime(2024, 3, 1, 14, 45, 0)) == "2024-03-01 14:45:00"


def test_format_candle_timestamp_non_datetime_falls_back() -> None:
    # A non-datetime cell (never the real timestamp column) falls back to the
    # generic JSON coercion path.
    assert _format_candle_timestamp(None) is None
    assert _format_candle_timestamp("already-a-string") == "already-a-string"


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
    assert out["columns"] == ["timestamp", "close"]


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
    assert out["columns"] == list(CANDLE_COLUMNS)


@pytest.mark.parametrize(
    ("exchange", "segment", "symbol", "interval", "match"),
    [
        ("MCX", "CASH", "NSE-NIFTY", "1minute", "unknown exchange"),
        ("NSE", "JUNK", "NSE-NIFTY", "1minute", "unknown segment"),
        ("NSE", "CASH", "NSE-NIFTY", "7minute", "unknown interval"),
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
    assert found == [{"kind": "option", "series": _OPT_SERIES, "interval": "1minute"}]


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
        }
    ]


def test_summarize_spot_datasets_multiple_sets(store: InstrumentStore, tmp_path: Path) -> None:
    _write_candles(tmp_path, symbol="NSE-NIFTY")
    _write_candles(tmp_path, symbol="NSE-BANKNIFTY")
    rows = summarize_spot_datasets(store.connection, tmp_path)
    symbols = [r["symbol"] for r in rows]
    assert symbols == ["NSE-BANKNIFTY", "NSE-NIFTY"]  # list_candle_datasets is sorted
    assert all(r["bars"] == 2 for r in rows)
