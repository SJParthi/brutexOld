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
    FRESH_MAX_AGE_S,
    LIVE_TABLES,
    MOVERS_EMPTY_NOTE,
    TICKS_EMPTY_NOTE,
    _age_seconds,
    _jsonify,
    _jsonify_for_column,
    _latest_candle_partition,
    _latest_prevday_partition,
    _latest_tick_partition,
    clamp_limit,
    clamp_offset,
    clamp_top_n,
    live_columns,
    live_movers,
    live_table,
    live_tables_summary,
    query_live_table,
    ticks_flowing,
)
from brutex.data.models import CandleBar
from brutex.live.lake import write_live_candles, write_live_ticks
from brutex.live.prevday import PrevDayOHLCV, write_prev_day
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
    import polars as pl
    from brutex.live.lake import TICK_SCHEMA

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


# ── live_movers (DEC-LIVE-DASH-MOVERS-PARITY-001) ──


def _candle(ts: datetime, *, o: str, h: str, low: str, c: str, volume: int = 100) -> CandleBar:
    """Build one minute :class:`CandleBar` (close validated within [low, high])."""
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=volume,
        open_interest=None,
    )


def _seed_movers(lake_root, *, prev_closes, with_prevday: bool = True) -> None:
    """Seed a one-session live candle lake (+ optional prev-day reference partition).

    Two index instruments at 2026-06-15 09:45 UTC: NIFTY up (close 110 vs open 100),
    BANKNIFTY down (close 190 vs open 200). *prev_closes* feeds the merged candle
    ``pct_change``; *with_prevday* additionally writes the ``live/prevday`` reference
    row for NIFTY only (so the join-absent path is also exercised for BANKNIFTY).
    """
    ts = datetime(2026, 6, 15, 9, 45, tzinfo=UTC)
    write_live_candles(
        {
            "NSE:IDX:NIFTY": [_candle(ts, o="100", h="112", low="99", c="110")],
            "NSE:IDX:BANKNIFTY": [_candle(ts, o="200", h="205", low="189", c="190")],
        },
        lake_root=lake_root,
        built_run_id="r1",
        prev_closes=prev_closes,
    )
    if with_prevday:
        write_prev_day(
            lake_root,
            [
                PrevDayOHLCV(
                    instrument_key="NSE:IDX:NIFTY",
                    session_date=date(2026, 6, 14),
                    open=Decimal("99"),
                    high=Decimal("101"),
                    low=Decimal("98"),
                    close=Decimal("100"),
                    volume=1,
                    open_interest=None,
                )
            ],
            as_of_date=date(2026, 6, 15),
        )


def test_clamp_top_n() -> None:
    assert clamp_top_n(0) == 1 and clamp_top_n(9999) == 100 and clamp_top_n(25) == 25


def test_live_movers_empty_lake_is_honest(tmp_path) -> None:
    """Absent candle lake → empty gainers/losers + the honest note, never fabricated."""
    payload = live_movers(tmp_path)
    assert payload == {
        "gainers": [],
        "losers": [],
        "session_date": None,
        "total_instruments": 0,
        "ranked_instruments": 0,
        "top_n": 25,
        "note": MOVERS_EMPTY_NOTE,
    }


def test_live_movers_ranks_by_pct_change_and_joins_prevday(tmp_path) -> None:
    """Gainers desc / losers asc by pct_change; prev_close joined from live/prevday."""
    _seed_movers(
        tmp_path, prev_closes={"NSE:IDX:NIFTY": Decimal("100"), "NSE:IDX:BANKNIFTY": Decimal("200")}
    )
    payload = live_movers(tmp_path, top_n=5)
    assert payload["session_date"] == "2026-06-15"
    assert payload["total_instruments"] == 2 and payload["ranked_instruments"] == 2
    assert payload["note"] is None
    gainers, losers = payload["gainers"], payload["losers"]
    # NIFTY +10% leads gainers; BANKNIFTY -5% leads losers.
    assert [g["groww_symbol"] for g in gainers] == ["NSE-NIFTY", "NSE-BANKNIFTY"]
    assert [low["groww_symbol"] for low in losers] == ["NSE-BANKNIFTY", "NSE-NIFTY"]
    top = gainers[0]
    assert top["pct_change"] == 10.0 and top["pct_from_open"] == 10.0
    # NIFTY's prev_close is the AUTHORITATIVE value from the live/prevday join.
    assert top["close"] == 110.0 and top["prev_close"] == 100.0 and top["volume"] == 100
    # The candle timestamp renders clean IST (no +05:30 offset).
    assert "+05:30" not in top["timestamp"] and top["timestamp"].startswith("2026-06-15 ")
    # BANKNIFTY had NO prevday reference row → prev_close falls back to the
    # reconstruction from its candle's own pct_change (190 / (1 + -5/100) == 200.0,
    # exact to float precision), never fabricated and never gratuitously null (the
    # join-miss resilience path).
    bank = next(g for g in gainers if g["groww_symbol"] == "NSE-BANKNIFTY")
    assert bank["prev_close"] == pytest.approx(200.0) and bank["pct_change"] == -5.0


def test_live_movers_without_prevday_partition_reconstructs_prev_close(tmp_path) -> None:
    """No live/prevday partition → prev_close is reconstructed from close + pct_change.

    The exact reconstruction (``close / (1 + pct_change/100)``) is the prior close the
    candle's own pct_change was computed against — so the reference column is populated
    (not gratuitously null) even with no prev-day lake, and ranking still works.
    """
    _seed_movers(
        tmp_path,
        prev_closes={"NSE:IDX:NIFTY": Decimal("100"), "NSE:IDX:BANKNIFTY": Decimal("200")},
        with_prevday=False,
    )
    assert _latest_prevday_partition(tmp_path) is None
    payload = live_movers(tmp_path, top_n=5)
    assert payload["ranked_instruments"] == 2
    by_symbol = {g["groww_symbol"]: g for g in payload["gainers"]}
    # 110 / (1 + 10/100) ≈ 100.0 ; 190 / (1 + -5/100) ≈ 200.0 — reconstructed to float
    # precision (the reconstruction is approximate, NOT bit-exact like the stored join).
    assert by_symbol["NSE-NIFTY"]["prev_close"] == pytest.approx(100.0)
    assert by_symbol["NSE-BANKNIFTY"]["prev_close"] == pytest.approx(200.0)
    assert payload["gainers"][0]["groww_symbol"] == "NSE-NIFTY"


def test_live_movers_prev_close_null_when_total_wipeout(tmp_path) -> None:
    """A ``pct_change == -100`` (close → 0) → prev_close honestly NULL (NULLIF guard).

    The reconstruction ``close / (1 + pct_change/100)`` would divide by zero (an
    infinite prior close); ``NULLIF`` degrades it to NULL rather than raising — the one
    ranked row whose prev_close cannot be derived (covers the ``_jsonify(None)`` path).
    """
    ts = datetime(2026, 6, 15, 9, 45, tzinfo=UTC)
    write_live_candles(
        {"NSE:IDX:WIPEOUT": [_candle(ts, o="1", h="1", low="0", c="0", volume=5)]},
        lake_root=tmp_path,
        built_run_id="r1",
        prev_closes={"NSE:IDX:WIPEOUT": Decimal("10")},  # 0 vs 10 → pct_change = -100
    )
    payload = live_movers(tmp_path, top_n=5)
    assert payload["ranked_instruments"] == 1
    row = payload["losers"][0]
    assert row["pct_change"] == -100.0 and row["close"] == 0.0 and row["prev_close"] is None


def test_live_movers_excludes_null_pct_change(tmp_path) -> None:
    """An instrument with no prior close (null pct_change) is counted but NOT ranked."""
    # Only NIFTY gets a prev_close → BANKNIFTY's pct_change is null and is unrankable.
    _seed_movers(tmp_path, prev_closes={"NSE:IDX:NIFTY": Decimal("100")}, with_prevday=False)
    payload = live_movers(tmp_path, top_n=5)
    assert payload["total_instruments"] == 2 and payload["ranked_instruments"] == 1
    assert [g["groww_symbol"] for g in payload["gainers"]] == ["NSE-NIFTY"]
    assert [low["groww_symbol"] for low in payload["losers"]] == ["NSE-NIFTY"]


def test_live_movers_top_n_is_clamped(tmp_path) -> None:
    """A requested top_n above the cap is clamped to 100 (bounded payload)."""
    _seed_movers(tmp_path, prev_closes={"NSE:IDX:NIFTY": Decimal("100")}, with_prevday=False)
    payload = live_movers(tmp_path, top_n=9999)
    assert payload["top_n"] == 100


def test_live_movers_reads_only_latest_partition(tmp_path) -> None:
    """Two candle session partitions → only the newest is ranked (bounded read)."""
    old_ts = datetime(2026, 6, 12, 9, 45, tzinfo=UTC)
    write_live_candles(
        {"NSE:IDX:NIFTY": [_candle(old_ts, o="50", h="60", low="49", c="60")]},
        lake_root=tmp_path,
        built_run_id="r0",
        prev_closes={"NSE:IDX:NIFTY": Decimal("50")},
    )
    _seed_movers(tmp_path, prev_closes={"NSE:IDX:BANKNIFTY": Decimal("200")}, with_prevday=False)
    latest = _latest_candle_partition(tmp_path)  # narrow the Optional before .stem access
    assert latest is not None and latest.stem == "dt=2026-06-15"
    payload = live_movers(tmp_path, top_n=5)
    # Only the 2026-06-15 partition is scanned → no NIFTY-on-2026-06-12 row.
    symbols = {g["groww_symbol"] for g in payload["gainers"]}
    assert symbols == {"NSE-BANKNIFTY"} and payload["session_date"] == "2026-06-15"


def test_live_movers_latest_minute_per_instrument(tmp_path) -> None:
    """When an instrument has multiple minutes, the NEWEST candle's pct_change ranks."""
    early = datetime(2026, 6, 15, 9, 45, tzinfo=UTC)
    late = datetime(2026, 6, 15, 9, 46, tzinfo=UTC)
    write_live_candles(
        {
            "NSE:IDX:NIFTY": [
                _candle(early, o="100", h="105", low="99", c="105"),
                _candle(late, o="105", h="120", low="104", c="120"),
            ]
        },
        lake_root=tmp_path,
        built_run_id="r1",
        prev_closes={"NSE:IDX:NIFTY": Decimal("100")},
    )
    payload = live_movers(tmp_path, top_n=5)
    # The 09:46 candle (close 120 vs prev 100 → +20%) ranks, not the 09:45 (+5%).
    assert payload["gainers"][0]["pct_change"] == 20.0
    assert payload["gainers"][0]["timestamp"].startswith("2026-06-15 ")
