"""Tests for the /db live-table browse backend (:mod:`brutex.dashboard.live_db_view`)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.dashboard.live_db_view import LIVE_TABLES, query_live_table
from brutex.live.candle_builder import build_minute_candles
from brutex.live.lake import minute_tick_counts, write_live_candles, write_live_ticks
from brutex.live.store import TickRecord
from brutex.live.tick import iter_ticks

_T0 = datetime(2026, 6, 15, 3, 45, 10, tzinfo=UTC)


def _seed(tmp_path) -> None:
    records = [TickRecord(seq=0, recv_ts=_T0, snapshot={"NSE:IDX:NIFTY": 22500.0}, recv_ns=1)]
    write_live_ticks(records, lake_root=tmp_path, capture_run_id="r")
    candles = build_minute_candles(iter_ticks(records))
    write_live_candles(
        candles, lake_root=tmp_path, built_run_id="r", tick_counts=minute_tick_counts(records)
    )


def test_live_tables_allowlist() -> None:
    assert set(LIVE_TABLES) == {"live_ticks", "live_candles_1m"}


def test_query_live_ticks_payload(tmp_path) -> None:
    _seed(tmp_path)
    out = query_live_table(tmp_path, "live_ticks")
    assert out["table"] == "live_ticks" and out["total"] == 1 and len(out["rows"]) == 1
    assert "recv_ns" in out["columns"] and "vendor_ts_ms" in out["columns"]
    ts_idx = out["columns"].index("recv_ts")
    assert isinstance(out["rows"][0][ts_idx], str)  # datetime → ISO string (JSON-safe)


def test_query_live_candles_payload(tmp_path) -> None:
    _seed(tmp_path)
    out = query_live_table(tmp_path, "live_candles_1m", limit=10, offset=0)
    assert out["table"] == "live_candles_1m" and out["total"] == 1
    assert {"open", "high", "low", "close", "tick_count"}.issubset(out["columns"])


def test_query_clamps_limit_and_offset(tmp_path) -> None:
    _seed(tmp_path)
    out = query_live_table(tmp_path, "live_ticks", limit=99999, offset=-5)
    assert out["limit"] == 500 and out["offset"] == 0


def test_query_unknown_table_raises(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown live table"):
        query_live_table(tmp_path, "instruments")


def test_query_empty_lake_has_schema_columns(tmp_path) -> None:
    out = query_live_table(tmp_path, "live_ticks")
    assert out["total"] == 0 and out["rows"] == []
    assert "instrument_key" in out["columns"]  # schema present even when empty
