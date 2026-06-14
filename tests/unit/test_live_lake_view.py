"""Unit tests for the dashboard live Parquet-lake browse view.

The dashboard reads the lake DIRECTLY (DuckDB) and never imports ``brutex.live``;
the TEST may seed it via ``brutex.live.lake`` (only the dashboard module is
constrained). Covers :mod:`brutex.dashboard.live_lake_view`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.dashboard.live_lake_view import (
    LIVE_TABLES,
    _jsonify,
    clamp_limit,
    clamp_offset,
    live_columns,
    live_table,
    live_tables_summary,
    query_live_table,
)
from brutex.data.models import CandleBar
from brutex.live.lake import write_live_candles, write_live_ticks
from brutex.live.store import TickRecord


def _seed_two(lake_root) -> None:
    recs = [
        TickRecord(
            seq=0,
            recv_ts=datetime(2026, 6, 15, 3, 45, tzinfo=UTC),
            snapshot={"NSE:IDX:NIFTY": 22500.0},
            recv_ns=1,
        ),
        TickRecord(
            seq=1,
            recv_ts=datetime(2026, 6, 15, 3, 46, tzinfo=UTC),
            snapshot={"NSE:IDX:BANKNIFTY": 48000.0},
            recv_ns=2,
        ),
    ]
    write_live_ticks(recs, lake_root=lake_root, capture_run_id="r1", feed_kind="index")


def _seed(lake_root) -> None:
    rec = TickRecord(
        seq=0,
        recv_ts=datetime(2026, 6, 15, 3, 45, tzinfo=UTC),
        snapshot={"NSE:IDX:NIFTY": 22500.0},
        recv_ns=1,
    )
    write_live_ticks([rec], lake_root=lake_root, capture_run_id="r1", feed_kind="index")
    bar = CandleBar(
        timestamp=datetime(2026, 6, 15, 3, 45, tzinfo=UTC),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=0,
        open_interest=None,
    )
    write_live_candles({"NSE:IDX:NIFTY": [bar]}, lake_root=lake_root, built_run_id="r1")


def test_clamps() -> None:
    assert clamp_limit(0) == 1 and clamp_limit(9999) == 500 and clamp_limit(50) == 50
    assert clamp_offset(-3) == 0 and clamp_offset(7) == 7


def test_live_tables_constant() -> None:
    assert LIVE_TABLES == ("live_ticks", "live_candles_1m")


def test_empty_lake_returns_empty_page(tmp_path) -> None:
    page = live_table(tmp_path, "live_ticks")
    assert page == {
        "table": "live_ticks",
        "columns": [],
        "rows": [],
        "total": 0,
        "limit": 100,
        "offset": 0,
    }
    assert live_tables_summary(tmp_path) == [
        {"table": "live_ticks", "row_count": 0},
        {"table": "live_candles_1m", "row_count": 0},
    ]


def test_unknown_table_raises(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown live table"):
        live_table(tmp_path, "bogus")


def test_reads_ticks_and_candles(tmp_path) -> None:
    _seed(tmp_path)
    ticks = live_table(tmp_path, "live_ticks")
    assert ticks["total"] == 1 and "instrument_key" in ticks["columns"]
    gs = ticks["rows"][0][ticks["columns"].index("groww_symbol")]
    assert gs == "NSE-NIFTY"
    candles = live_table(tmp_path, "live_candles_1m", limit=10)
    assert candles["total"] == 1 and "timestamp" in candles["columns"]
    assert {s["table"]: s["row_count"] for s in live_tables_summary(tmp_path)} == {
        "live_ticks": 1,
        "live_candles_1m": 1,
    }


def test_live_columns(tmp_path) -> None:
    assert live_columns(tmp_path, "live_ticks") == []  # no partitions yet
    _seed(tmp_path)
    cols = live_columns(tmp_path, "live_ticks")
    assert "groww_symbol" in cols and "value" in cols
    with pytest.raises(ValueError, match="unknown live table"):
        live_columns(tmp_path, "bogus")


def test_query_live_table_empty(tmp_path) -> None:
    page = query_live_table(tmp_path, "live_ticks")
    assert page["total"] == 0 and page["rows"] == [] and page["limit"] == 100


def test_query_live_table_filter_sort_project(tmp_path) -> None:
    _seed_two(tmp_path)
    # default → all rows, newest-first by the stable key
    allrows = query_live_table(tmp_path, "live_ticks")
    assert allrows["total"] == 2
    # filter → only NIFTY
    only = query_live_table(tmp_path, "live_ticks", filters=(("groww_symbol", "eq", "NSE-NIFTY"),))
    assert only["total"] == 1
    assert only["rows"][0][only["columns"].index("groww_symbol")] == "NSE-NIFTY"
    # projection → just two columns
    proj = query_live_table(tmp_path, "live_ticks", columns=("seq", "groww_symbol"))
    assert proj["columns"] == ["seq", "groww_symbol"]
    # explicit ascending order by seq
    asc = query_live_table(tmp_path, "live_ticks", order_by="seq", descending=False)
    seqs = [r[asc["columns"].index("seq")] for r in asc["rows"]]
    assert seqs == sorted(seqs)


def test_query_live_table_validation(tmp_path) -> None:
    _seed(tmp_path)
    with pytest.raises(ValueError, match="unknown filter column"):
        query_live_table(tmp_path, "live_ticks", filters=(("nope", "eq", "x"),))
    with pytest.raises(ValueError, match="unknown order column"):
        query_live_table(tmp_path, "live_ticks", order_by="nope")
    with pytest.raises(ValueError, match="unknown live table"):
        query_live_table(tmp_path, "bogus")


def test_jsonify_branches() -> None:
    assert _jsonify(None) is None
    assert _jsonify("x") == "x" and _jsonify(5) == 5 and _jsonify(1.5) == 1.5
    assert _jsonify(True) is True
    assert _jsonify(Decimal("2.5")) == 2.5
    assert _jsonify(datetime(2026, 6, 15, tzinfo=UTC)).startswith("2026-06-15")
    assert _jsonify(date(2026, 6, 15)) == "2026-06-15"

    class _Obj:
        def __str__(self) -> str:
            return "obj"

    assert _jsonify(_Obj()) == "obj"
