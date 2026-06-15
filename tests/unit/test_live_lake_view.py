"""Unit tests for the dashboard live Parquet-lake browse view.

The dashboard reads the lake DIRECTLY (DuckDB) and never imports ``brutex.live``;
the TEST may seed it via ``brutex.live.lake`` (only the dashboard module is
constrained). Covers :mod:`brutex.dashboard.live_lake_view`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import polars as pl
import pytest
from brutex.dashboard.live_lake_view import (
    _CANONICAL_SCHEMA,
    FRESH_MAX_AGE_S,
    LIVE_TABLES,
    SCHEMA_ONLY_NOTE,
    TICKS_EMPTY_NOTE,
    _age_seconds,
    _jsonify,
    _jsonify_for_column,
    _latest_tick_partition,
    clamp_limit,
    clamp_offset,
    live_columns,
    live_table,
    live_table_schema,
    live_tables_summary,
    query_live_table,
    ticks_flowing,
)
from brutex.data.models import CandleBar
from brutex.live.lake import (
    CANDLE_SCHEMA,
    TICK_SCHEMA,
    write_live_candles,
    write_live_ticks,
)
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


def test_empty_lake_returns_schema_only_page(tmp_path) -> None:
    """No partitions yet → the canonical COLUMN DEFINITIONS + honest note, 0 rows.

    The operator-hit gap (DEC-LIVE-DB-SCHEMA-WHEN-EMPTY-001): an empty live lake must
    still surface the column names + types so the merged columns can be verified —
    never a fabricated row (Golden Rule #1).
    """
    page = live_table(tmp_path, "live_ticks")
    tick_names = [name for name, _ in _CANONICAL_SCHEMA["live_ticks"]]
    assert page == {
        "table": "live_ticks",
        "columns": tick_names,
        "schema": live_table_schema("live_ticks"),
        "rows": [],
        "total": 0,
        "limit": 100,
        "offset": 0,
        "note": SCHEMA_ONLY_NOTE,
    }
    # The merged live-tick columns are visible even with zero captured ticks.
    for merged in ("exchange_ts", "avg_price", "payload_hash"):
        assert merged in page["columns"]
    # The candle table likewise surfaces its derived-% columns when empty.
    candles = live_table(tmp_path, "live_candles_1m")
    assert candles["rows"] == [] and candles["total"] == 0
    assert candles["note"] == SCHEMA_ONLY_NOTE
    for merged in ("pct_change", "pct_from_open"):
        assert merged in candles["columns"]
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
    # No partitions yet → the canonical schema names (schema-when-empty), not [].
    empty = live_columns(tmp_path, "live_ticks")
    assert empty == [name for name, _ in _CANONICAL_SCHEMA["live_ticks"]]
    assert "exchange_ts" in empty  # a merged column the operator must see
    _seed(tmp_path)
    cols = live_columns(tmp_path, "live_ticks")
    assert "groww_symbol" in cols and "value" in cols
    with pytest.raises(ValueError, match="unknown live table"):
        live_columns(tmp_path, "bogus")


def test_query_live_table_empty_returns_schema(tmp_path) -> None:
    page = query_live_table(tmp_path, "live_ticks")
    assert page["total"] == 0 and page["rows"] == [] and page["limit"] == 100
    assert page["columns"] == [name for name, _ in _CANONICAL_SCHEMA["live_ticks"]]
    assert page["note"] == SCHEMA_ONLY_NOTE
    assert page["schema"] == live_table_schema("live_ticks")


def test_query_live_table_empty_honours_projection(tmp_path) -> None:
    """A *columns* projection on the empty path is honoured + validated."""
    proj = query_live_table(tmp_path, "live_ticks", columns=("seq", "groww_symbol"))
    assert proj["columns"] == ["seq", "groww_symbol"]
    assert proj["rows"] == [] and proj["note"] == SCHEMA_ONLY_NOTE
    # An unknown projected column raises on the empty path too (matches populated path).
    with pytest.raises(ValueError, match="unknown column"):
        query_live_table(tmp_path, "live_ticks", columns=("nope",))


def test_live_table_schema_shape_and_unknown(tmp_path) -> None:
    """live_table_schema returns name+type dicts; unknown table raises."""
    schema = live_table_schema("live_candles_1m")
    assert schema == [
        {"name": name, "type": dtype} for name, dtype in _CANONICAL_SCHEMA["live_candles_1m"]
    ]
    assert {"name": "pct_change", "type": "DOUBLE"} in schema
    with pytest.raises(ValueError, match="unknown live table"):
        live_table_schema("bogus")


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


# ───────────── clean-IST live-table timestamps (DEC-LIVE-DASH-PROBE-001) ────────


def _tick_with_exchange_ts(symbol: str, *, ts_in_millis: int | None) -> TickRecord:
    """A TickRecord whose snapshot carries ``tsInMillis`` → a non-null exchange_ts.

    ``None`` ``ts_in_millis`` → the leaf carries no usable vendor time, so exchange_ts
    lands as SQL NULL (the null-exchange_ts edge the formatter must tolerate).
    """
    leaf: dict[str, object] = {"ltp": 22500.0}
    if ts_in_millis is not None:
        leaf["tsInMillis"] = ts_in_millis
    return TickRecord(
        seq=0,
        recv_ts=datetime(2026, 6, 15, 10, 4, 41, 335741, tzinfo=UTC),  # 15:34:41.335 IST
        snapshot={symbol: leaf},
        recv_ns=1,
    )


def test_jsonify_for_column_renders_clean_ist_millis() -> None:
    """The timestamp columns render clean IST (no +05:30, millisecond-trimmed)."""
    value = datetime(2026, 6, 15, 10, 4, 41, 335741, tzinfo=UTC)
    for col in ("recv_ts", "exchange_ts", "timestamp"):
        assert _jsonify_for_column(value, col) == "2026-06-15 15:34:41.335"
        assert "+05:30" not in _jsonify_for_column(value, col)
    # A non-timestamp column with a datetime falls through to the raw isoformat.
    assert _jsonify_for_column(value, "session_date") == value.isoformat()
    # A null cell in a timestamp column stays None (the null-exchange_ts edge).
    assert _jsonify_for_column(None, "exchange_ts") is None
    # A non-datetime in a timestamp column falls through to _jsonify.
    assert _jsonify_for_column(5, "recv_ts") == 5


def test_live_ticks_recv_and_exchange_ts_render_clean_ist(tmp_path) -> None:
    """live_ticks recv_ts + exchange_ts render clean IST without the +05:30 suffix."""
    ms = int(datetime(2026, 6, 15, 10, 4, 41, tzinfo=UTC).timestamp() * 1000)
    write_live_ticks(
        [_tick_with_exchange_ts("NSE:IDX:NIFTY", ts_in_millis=ms)],
        lake_root=tmp_path,
        capture_run_id="r1",
        feed_kind="index",
    )
    page = live_table(tmp_path, "live_ticks")
    recv = page["rows"][0][page["columns"].index("recv_ts")]
    exch = page["rows"][0][page["columns"].index("exchange_ts")]
    assert recv == "2026-06-15 15:34:41.335" and "+05:30" not in recv
    assert exch == "2026-06-15 15:34:41.000" and "+05:30" not in exch


def test_live_ticks_null_exchange_ts_still_renders_recv_ts(tmp_path) -> None:
    """A row whose exchange_ts is NULL still renders its recv_ts cleanly (null edge)."""
    write_live_ticks(
        [_tick_with_exchange_ts("NSE:IDX:NIFTY", ts_in_millis=None)],
        lake_root=tmp_path,
        capture_run_id="r1",
        feed_kind="index",
    )
    page = query_live_table(tmp_path, "live_ticks")  # query path shares _render_rows
    recv = page["rows"][0][page["columns"].index("recv_ts")]
    exch = page["rows"][0][page["columns"].index("exchange_ts")]
    assert recv == "2026-06-15 15:34:41.335"
    assert exch is None  # null vendor time → null cell, not a crash


def test_live_candles_timestamp_renders_clean_ist_minute_boundary(tmp_path) -> None:
    """live_candles_1m.timestamp renders clean IST at a minute boundary (no +05:30)."""
    bar = CandleBar(
        timestamp=datetime(2026, 6, 15, 4, 0, tzinfo=UTC),  # 09:30:00 IST minute start
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=0,
        open_interest=None,
    )
    write_live_candles({"NSE:IDX:NIFTY": [bar]}, lake_root=tmp_path, built_run_id="r1")
    page = live_table(tmp_path, "live_candles_1m", limit=10)
    ts = page["rows"][0][page["columns"].index("timestamp")]
    assert ts == "2026-06-15 09:30:00.000" and "+05:30" not in ts


# ───────────── ticks-flowing freshness indicator (DEC-LIVE-DASH-PROBE-001) ──────


def test_age_seconds_branches() -> None:
    now = datetime(2026, 6, 15, 10, 5, 0, tzinfo=UTC)
    assert _age_seconds(None, now) is None
    assert _age_seconds(datetime(2026, 6, 15, 10, 4, 0, tzinfo=UTC), now) == 60.0
    # A tick stamped slightly ahead of the reader clamps to 0.0 (never negative).
    assert _age_seconds(datetime(2026, 6, 15, 10, 6, 0, tzinfo=UTC), now) == 0.0


def test_latest_tick_partition_none_when_absent(tmp_path) -> None:
    assert _latest_tick_partition(tmp_path) is None


def test_latest_tick_partition_skips_malformed_name(tmp_path) -> None:
    """A non-ISO ``dt=garbage.parquet`` is SKIPPED (never chosen as 'newest').

    Without the date-parse guard a ``dt=zzz.parquet`` would sort lexicographically
    last and be wrongly read; here it must be ignored in favour of the real ISO date.
    """
    ticks = tmp_path / "live" / "ticks"
    ticks.mkdir(parents=True)
    write_live_ticks(
        [_tick_with_exchange_ts("NSE:IDX:NIFTY", ts_in_millis=None)],
        lake_root=tmp_path,
        capture_run_id="r1",
        feed_kind="index",
    )
    # A stray malformed partition that sorts AFTER the real dt=2026-06-15.
    (ticks / "dt=zzz-garbage.parquet").write_bytes(b"")
    latest = _latest_tick_partition(tmp_path)
    assert latest is not None and latest.stem == "dt=2026-06-15"


def test_latest_tick_partition_none_when_all_malformed(tmp_path) -> None:
    """Only malformed partition names present → None (never reads a non-session file)."""
    ticks = tmp_path / "live" / "ticks"
    ticks.mkdir(parents=True)
    (ticks / "dt=garbage.parquet").write_bytes(b"")
    assert _latest_tick_partition(tmp_path) is None


def test_ticks_flowing_empty_lake_is_honest(tmp_path) -> None:
    payload = ticks_flowing(tmp_path)
    assert payload == {
        "flowing": False,
        "total_ticks": 0,
        "fresh_instruments": 0,
        "instruments": [],
        "session_date": None,
        "note": TICKS_EMPTY_NOTE,
    }


def test_ticks_flowing_fresh_within_threshold(tmp_path) -> None:
    """A recent tick (age <= FRESH_MAX_AGE_S) → flowing=True + clean-IST last times."""
    ms = int(datetime(2026, 6, 15, 10, 4, 41, tzinfo=UTC).timestamp() * 1000)
    write_live_ticks(
        [_tick_with_exchange_ts("NSE:IDX:NIFTY", ts_in_millis=ms)],
        lake_root=tmp_path,
        capture_run_id="r1",
        feed_kind="index",
    )
    # 30s after the tick → fresh (< 60s).
    payload = ticks_flowing(tmp_path, now=datetime(2026, 6, 15, 10, 5, 11, 335741, tzinfo=UTC))
    assert payload["flowing"] is True
    assert payload["total_ticks"] == 1 and payload["fresh_instruments"] == 1
    assert payload["session_date"] == "2026-06-15"
    inst = payload["instruments"][0]
    assert inst["groww_symbol"] == "NSE-NIFTY" and inst["ticks"] == 1
    assert inst["last_recv_ts"] == "2026-06-15 15:34:41.335" and inst["fresh"] is True
    assert inst["last_exchange_ts"] == "2026-06-15 15:34:41.000"
    assert inst["last_tick_age_s"] == 30.0


def test_ticks_flowing_stale_when_old(tmp_path) -> None:
    """A tick older than the freshness threshold → flowing=False, fresh=False."""
    write_live_ticks(
        [_tick_with_exchange_ts("NSE:IDX:NIFTY", ts_in_millis=None)],
        lake_root=tmp_path,
        capture_run_id="r1",
        feed_kind="index",
    )
    payload = ticks_flowing(tmp_path, now=datetime(2026, 6, 15, 11, 0, 0, tzinfo=UTC))
    assert payload["flowing"] is False and payload["fresh_instruments"] == 0
    inst = payload["instruments"][0]
    assert inst["fresh"] is False and inst["last_tick_age_s"] > FRESH_MAX_AGE_S
    assert inst["last_exchange_ts"] is None  # null vendor time surfaced honestly


def test_ticks_flowing_present_but_empty_partition_is_honest(tmp_path) -> None:
    """A partition file that exists but holds ZERO rows → honest empty payload.

    Exercises the ``note = ... else TICKS_EMPTY_NOTE`` path with a partition present:
    ``flowing=False`` + an empty instrument list + the note, but the session date is
    still surfaced — never fabricated flow (Golden Rule #1).
    """
    ticks = tmp_path / "live" / "ticks"
    ticks.mkdir(parents=True)
    pl.DataFrame(schema=dict(TICK_SCHEMA)).write_parquet(ticks / "dt=2026-06-15.parquet")
    payload = ticks_flowing(tmp_path, now=datetime(2026, 6, 15, 11, 0, 0, tzinfo=UTC))
    assert payload["flowing"] is False and payload["instruments"] == []
    assert payload["total_ticks"] == 0 and payload["fresh_instruments"] == 0
    assert payload["session_date"] == "2026-06-15" and payload["note"] == TICKS_EMPTY_NOTE


def test_ticks_flowing_reads_only_latest_partition(tmp_path) -> None:
    """Two session-date partitions → the indicator scans ONLY the newest (bounded)."""
    old = TickRecord(
        seq=0,
        recv_ts=datetime(2026, 6, 12, 10, 0, 0, tzinfo=UTC),
        snapshot={"NSE:IDX:NIFTY": {"ltp": 1.0}},
        recv_ns=1,
    )
    new = TickRecord(
        seq=1,
        recv_ts=datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC),
        snapshot={"NSE:IDX:BANKNIFTY": {"ltp": 2.0}},
        recv_ns=2,
    )
    write_live_ticks([old], lake_root=tmp_path, capture_run_id="r1", feed_kind="index")
    write_live_ticks([new], lake_root=tmp_path, capture_run_id="r1", feed_kind="index")
    payload = ticks_flowing(tmp_path, now=datetime(2026, 6, 15, 10, 0, 30, tzinfo=UTC))
    # Only the 2026-06-15 partition is scanned → only BANKNIFTY, never the old NIFTY.
    assert payload["session_date"] == "2026-06-15"
    assert [i["groww_symbol"] for i in payload["instruments"]] == ["NSE-BANKNIFTY"]


# ───── schema-when-empty drift guard (DEC-LIVE-DB-SCHEMA-WHEN-EMPTY-001) ─────


def _duckdb_schema_of(pl_schema: dict[str, pl.DataType], dest: Path) -> tuple[tuple[str, str], ...]:
    """The DuckDB ``read_parquet`` description of an EMPTY Parquet built from a lake schema.

    The dashboard NEVER imports ``brutex.live`` at RUNTIME (DEC-LIVE-DECOUPLE-001), but a
    TEST may — so this builds an empty Parquet from the real ``brutex.live.lake`` Polars
    schema (written to *dest*) and reads back the exact ``(name, duckdb_type)`` pairs the
    POPULATED browse path would derive. This is what the dashboard's decoupled canonical
    constant must equal byte-for-byte.
    """
    pl.DataFrame(schema=pl_schema).write_parquet(dest)
    conn = duckdb.connect(":memory:")
    try:
        cur = conn.execute("SELECT * FROM read_parquet(?) LIMIT 0", [dest.as_posix()])
        description = cur.description
        assert description is not None  # a SELECT always carries a description
        out = tuple((str(col[0]), str(col[1])) for col in description)
    finally:
        conn.close()
    return out


def test_canonical_schema_matches_real_lake_schema(tmp_path: Path) -> None:
    """The dashboard's DECOUPLED canonical schema must not drift from ``brutex.live.lake``.

    Asserts the dashboard's ``_CANONICAL_SCHEMA`` (names + DuckDB types + ORDER) equals
    what an empty Parquet built from the authoritative ``TICK_SCHEMA`` / ``CANDLE_SCHEMA``
    reports through DuckDB. If the live writer adds / renames / retypes a column without
    updating the dashboard mirror, THIS fails — closing the decoupling's drift risk while
    keeping the runtime import boundary intact (DEC-LIVE-DECOUPLE-001 / -SCHEMA-WHEN-EMPTY).
    """
    real_tick = _duckdb_schema_of(dict(TICK_SCHEMA), tmp_path / "tick.parquet")
    real_candle = _duckdb_schema_of(dict(CANDLE_SCHEMA), tmp_path / "candle.parquet")
    assert real_tick == _CANONICAL_SCHEMA["live_ticks"]
    assert real_candle == _CANONICAL_SCHEMA["live_candles_1m"]
