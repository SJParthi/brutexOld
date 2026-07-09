"""Split-brain reproducer + clean-lake byte-identity for DEC-DASHBOARD-READ-SCREEN-001.

The engine screens stored bars (bad_tick + price_sanity); the dashboard readers
(``/movers`` + ``/db``) used to read the SAME partitions RAW, so an ``×10``
fat-finger spike (or a positivity-impossible bar) was DROPPED by the engine but
SERVED by the dashboard — a fabricated Top-Value / a raw spike row. These tests
drive the REAL readers over a REAL lake and prove: (a) the spike is dropped so
``/movers`` == ``/db`` == the engine's screened result; (b) on a CLEAN lake both
readers are byte-identical to raw (the screen is a no-op); (c) the movers cache
stores the SCREENED figure; (d) a price-insane bar is closed on both surfaces;
(e) the ``BRUTEX_BAD_TICK_SCREEN`` kill switch restores the raw view consistently.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar, OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.data.parquet_writer import ParquetWriter

_DAY = date(2024, 3, 1)
_GOOD_CLOSES = ["1000", "1001", "1002", "1003"]
_SPIKE_CLOSES = ["1000", "1001", "10000", "1002", "1003"]  # engine drops the 10000


def _bar(*, minute: int, close: str, vol: int = 10, oi: int | None = None) -> CandleBar:
    """A valid (positive + ordered) 1-minute bar; the ×10 spike is itself valid."""
    c = Decimal(close)
    # 09:15 IST == 03:45 UTC.
    ts = datetime(2024, 3, 1, 3, 45, tzinfo=UTC) + timedelta(minutes=minute)
    return CandleBar(
        timestamp=ts,
        open=c - 1,
        high=c + 2,
        low=c - 3,
        close=c,
        volume=vol,
        open_interest=oi,
    )


def _write_spot(lake: Path, closes: list[str], *, symbol: str = "NIFTY") -> None:
    bars = [_bar(minute=i, close=c) for i, c in enumerate(closes)]
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment="INDEX", symbol=symbol, interval="1minute", bars=bars
    )


def _write_fno(lake: Path, closes: list[str], *, symbol: str) -> None:
    bars = [_bar(minute=i, close=c, oi=1000 + i) for i, c in enumerate(closes)]
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment="FNO", symbol=symbol, interval="1minute", bars=bars
    )


def _hlc3_paisa(closes: list[str]) -> int:
    """Independent Σ round_money((h+l+c)/3)×vol in paisa (h=c+2, l=c−3, vol=10)."""
    total = Fraction(0)
    for cs in closes:
        c = Fraction(Decimal(cs))
        hlc3 = ((c + 2) + (c - 3) + c) / 3
        scaled = hlc3 * 100
        q, r = divmod(scaled.numerator, scaled.denominator)
        if 2 * r >= scaled.denominator:
            q += 1
        total += Fraction(q, 100) * 10
    return int(total * 100)


# ── /movers spot ──────────────────────────────────────────────────────────


class TestMoversSpotSplitBrain:
    def test_spike_turnover_and_ltp_match_the_screened_engine_result(self, tmp_path: Path) -> None:
        from brutex.dashboard.movers_snapshot import compute_spot_candidates

        spike = tmp_path / "spike"
        clean = tmp_path / "clean"
        _write_spot(spike, _SPIKE_CLOSES)
        _write_spot(clean, _GOOD_CLOSES)

        s = compute_spot_candidates(
            lake_root=spike, interval="1minute", date=_DAY.isoformat(), time="15:30"
        )
        c = compute_spot_candidates(
            lake_root=clean, interval="1minute", date=_DAY.isoformat(), time="15:30"
        )

        assert len(s.snapshots) == 1
        # Screened turnover == the clean 4-bar turnover == the engine's 4-bar HLC3;
        # and NOT the raw 5-bar turnover the split-brain used to serve.
        assert s.snapshots[0].value_paisa == c.snapshots[0].value_paisa == _hlc3_paisa(_GOOD_CLOSES)
        assert s.snapshots[0].value_paisa != _hlc3_paisa(_SPIKE_CLOSES)
        # LTP is the last KEPT bar (1003), not the spike.
        assert str(s.snapshots[0].ltp) == str(c.snapshots[0].ltp)

    def test_cache_stores_the_screened_figure(self, tmp_path: Path) -> None:
        from brutex.dashboard.movers_cache import MoversSnapshotCache
        from brutex.dashboard.movers_snapshot import compute_spot_candidates

        _write_spot(tmp_path, _SPIKE_CLOSES)
        cache: MoversSnapshotCache = MoversSnapshotCache(capacity=8)
        first = cache.get_or_compute(
            "k",
            generation=0,
            compute=lambda: compute_spot_candidates(
                lake_root=tmp_path, interval="1minute", date=_DAY.isoformat(), time="15:30"
            ),
        )
        # A second poll within the same generation must NOT recompute — it serves
        # the cached SCREENED figure (never the fabricated 3.50× / +900%).
        second = cache.get_or_compute(
            "k", generation=0, compute=lambda: pytest.fail("cache recomputed a screened figure")
        )
        assert second is first
        assert first.snapshots[0].value_paisa == _hlc3_paisa(_GOOD_CLOSES)

    def test_kill_switch_off_serves_raw_spike(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard.movers_snapshot import compute_spot_candidates

        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
        _write_spot(tmp_path, _SPIKE_CLOSES)
        got = compute_spot_candidates(
            lake_root=tmp_path, interval="1minute", date=_DAY.isoformat(), time="15:30"
        )
        assert got.snapshots[0].value_paisa == _hlc3_paisa(_SPIKE_CLOSES)  # raw, consistent

    def test_daily_partition_is_screened(self, tmp_path: Path) -> None:
        """The stored 1day read (M3) also routes through the screen (a clean no-op)."""
        from brutex.dashboard.movers_snapshot import compute_spot_candidates

        # One clean daily bar (each daily bar is its own session — insane arm only).
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[_bar(minute=0, close="1000")],
        )
        got = compute_spot_candidates(
            lake_root=tmp_path, interval="1day", date=_DAY.isoformat(), time="15:30"
        )
        assert got.snapshots[0].value_paisa == _hlc3_paisa(["1000"])


# ── /movers futures ─────────────────────────────────────────────────────────


class TestMoversFuturesSplitBrain:
    def test_futures_reader_drops_spike(self, tmp_path: Path) -> None:
        from brutex.dashboard.movers_futures import _read_future_session

        fut = "NSE-NIFTY-28Mar24-FUT"
        _write_fno(tmp_path, _SPIKE_CLOSES, symbol=fut)
        read = _read_future_session(
            tmp_path, exchange="NSE", segment="FNO", symbol=fut, day=_DAY, cutoff_min=None
        )
        assert read is not None
        ltp, _vol, value_paisa, _oi, _iv, _delta = read
        assert value_paisa == _hlc3_paisa(_GOOD_CLOSES)  # spike screened out
        assert str(ltp) == "1003.00"  # last kept bar


# ── /db ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path: Path) -> object:
    return InstrumentStore.open(tmp_path / "instruments.duckdb").connection


def _closes_from_page(out: dict[str, object]) -> list[float]:
    cols = out["columns"]
    assert isinstance(cols, list)
    idx = cols.index("close")
    rows = out["rows"]
    assert isinstance(rows, list)
    return [row[idx] for row in rows]


class TestDbSplitBrain:
    def test_spike_row_omitted(self, conn: object, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import query_candles

        _write_spot(tmp_path, _SPIKE_CLOSES, symbol="NSE-NIFTY")
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            limit=50,
        )
        assert 10000.0 not in _closes_from_page(out)
        assert sorted(_closes_from_page(out)) == [1000.0, 1001.0, 1002.0, 1003.0]

    def test_clean_lake_serves_every_row(self, conn: object, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import query_candles

        _write_spot(tmp_path, _GOOD_CLOSES, symbol="NSE-NIFTY")
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            limit=50,
        )
        # No drops on clean data — the screen is a no-op.
        assert sorted(_closes_from_page(out)) == [1000.0, 1001.0, 1002.0, 1003.0]
        assert out["total"] == 4

    def test_kill_switch_off_serves_raw_spike(
        self, conn: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.dashboard.db_view import query_candles

        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
        _write_spot(tmp_path, _SPIKE_CLOSES, symbol="NSE-NIFTY")
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            limit=50,
        )
        assert 10000.0 in _closes_from_page(out)  # raw view restored, consistent

    def test_empty_page_needs_no_seed(self, conn: object, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import query_candles

        _write_spot(tmp_path, _GOOD_CLOSES, symbol="NSE-NIFTY")
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            start="2099-01-01",  # a range with no bars → empty page → no seed
            end="2099-01-02",
            limit=50,
        )
        assert out["rows"] == []

    def test_seed_finds_same_session_predecessor(self, conn: object, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import query_candles

        # 6 bars in one session; page 2 (offset 3) starts mid-session, so the seed
        # read finds the same-session predecessor (the seed 'found' branch).
        _write_spot(tmp_path, [str(1000 + i) for i in range(6)], symbol="NSE-NIFTY")
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            descending=False,
            limit=3,
            offset=3,
        )
        # Clean data → page 2 served intact (the seed read ran, screen was a no-op).
        assert _closes_from_page(out) == [1003.0, 1004.0, 1005.0]

    def test_seed_window_all_screened_yields_no_seed(self, conn: object, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import query_candles

        # First two same-session predecessor bars are INSANE (high<low); page 2 (the
        # two good bars) then has a seed WINDOW that screens out entirely → no seed
        # (the `if not kept` branch). The page's good bars still serve correctly.
        part = tmp_path / "bars" / "NSE" / "INDEX" / "NSE-NIFTY" / "1minute" / "2024" / "03.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        ts = [datetime(2024, 3, 1, 3, 45, tzinfo=UTC) + timedelta(minutes=i) for i in range(4)]
        pl.DataFrame(
            {
                "timestamp": pl.Series(ts).dt.replace_time_zone("UTC"),
                "open": [100.0, 100.0, 1000.0, 1001.0],
                "high": [1.0, 1.0, 1002.0, 1003.0],  # rows 0,1: high<low insane
                "low": [50.0, 50.0, 999.0, 1000.0],
                "close": [100.5, 100.5, 1000.0, 1001.0],
                "volume": [10, 10, 10, 10],
                "open_interest": [None, None, None, None],
            }
        ).write_parquet(part)
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            descending=False,
            limit=2,
            offset=2,  # page 2 = the two good bars; predecessors (the insane pair) all screen out
        )
        assert _closes_from_page(out) == [1000.0, 1001.0]


# ── price_sanity closure (insane bar) on BOTH surfaces ───────────────────────


def _write_raw_insane(lake: Path, *, symbol: str, segment: str) -> None:
    """Write a raw 1-minute partition with a middle high<low bar (bypasses CandleBar)."""
    part = lake / "bars" / "NSE" / segment / symbol / "1minute" / "2024" / "03.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    ts = [datetime(2024, 3, 1, 3, 45, tzinfo=UTC) + timedelta(minutes=i) for i in range(3)]
    pl.DataFrame(
        {
            "timestamp": pl.Series(ts).dt.replace_time_zone("UTC"),
            "open": [999.0, 100.0, 1001.0],
            "high": [1002.0, 1.0, 1005.0],  # row 1: high(1)<low(50) — insane
            "low": [998.0, 50.0, 999.0],
            "close": [1000.0, 100.5, 1002.0],
            "volume": [10, 10, 10],
            "open_interest": [None, None, None],
        }
    ).write_parquet(part)


class TestPriceSanityClosure:
    def test_db_omits_insane_bar(self, conn: object, tmp_path: Path) -> None:
        from brutex.dashboard.db_view import query_candles

        _write_raw_insane(tmp_path, symbol="NSE-NIFTY", segment="INDEX")
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            limit=50,
        )
        assert sorted(_closes_from_page(out)) == [1000.0, 1002.0]  # insane row dropped

    def test_movers_drops_insane_bar(self, tmp_path: Path) -> None:
        from brutex.dashboard.movers_snapshot import _read_cut_1m_frame

        _write_raw_insane(tmp_path, symbol="NIFTY", segment="INDEX")
        frame = _read_cut_1m_frame(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_DAY,
            cutoff_min=None,
        )
        assert frame is not None
        assert frame.get_column("close").to_list() == [1000.0, 1002.0]


# ── options scope: bad-tick OFF, insane ON (MAJOR-1 fix) ─────────────────────

_OPT_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"
_OPT_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"
# Premium closes with a legit 5× jump (1.00 → 5.00) — a bad tick by the index 4×
# rule, but a legitimate deep-OTM premium move on leverage.
_PREMIUM_CLOSES = ["1.00", "5.00", "1.00"]


def _premium_bar(minute: int, close: str) -> CandleBar:
    """A premium-safe 1m bar (high=c+0.5, low=c-0.5 — positive + ordered for c≥0.5)."""
    c = Decimal(close)
    ts = datetime(2024, 3, 1, 3, 45, tzinfo=UTC) + timedelta(minutes=minute)
    return CandleBar(
        timestamp=ts,
        open=c,
        high=c + Decimal("0.5"),
        low=c - Decimal("0.5"),
        close=c,
        volume=10,
        open_interest=1000 + minute,
    )


def _fno_premium(lake: Path, symbol: str) -> None:
    """Write an FNO 1m contract whose premium closes carry a legit 5× jump."""
    bars = [_premium_bar(i, c) for i, c in enumerate(_PREMIUM_CLOSES)]
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment="FNO", symbol=symbol, interval="1minute", bars=bars
    )


def _opt_coord() -> OptionSeriesCoordinate:
    seg, sec, flag, code, offset, _typ = _OPT_PK.split("|")
    return OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment=seg,
        security_id=int(sec),
        underlying_symbol="NIFTY",
        expiry_flag=flag,
        expiry_code=int(code),
        strike_offset=offset,
    )


def _obar_premium(minute: int, close: str) -> OptionBar:
    c = Decimal(close)
    ts = datetime(2024, 3, 1, 4, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return OptionBar(
        timestamp=ts,
        open=c,
        high=c + Decimal("0.5"),
        low=c - Decimal("0.5"),
        close=c,
        volume=250,
        open_interest=1200,
        side="CE",
        strike=Decimal("17500.00"),
        spot=Decimal("17480.55"),
        iv=Decimal("0.15"),
    )


class TestOptionsScope:
    def test_movers_options_keeps_legit_4x_premium(self, tmp_path: Path) -> None:
        """OPTIONS reader (bad_tick=False) KEEPS a ≥4× premium move; FUTURES drops it."""
        from brutex.dashboard.movers_futures import _read_future_session

        sym = "NSE-NIFTY-28Mar24-17500-CE"
        _fno_premium(tmp_path, sym)
        opt = _read_future_session(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=sym,
            day=_DAY,
            cutoff_min=None,
            bad_tick=False,
        )
        fut = _read_future_session(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=sym,
            day=_DAY,
            cutoff_min=None,
            bad_tick=True,
        )
        assert opt is not None and fut is not None
        # Options keep the 5× bar → strictly larger turnover than the futures view
        # (which drops it as a bad tick). This is the anti-split-brain scope fix.
        assert opt[2] > fut[2]  # value_paisa: options includes the 5.00 bar
        assert str(opt[0]) == "1.00"  # LTP is the last kept premium bar (all 3 kept)

    def test_movers_options_drops_insane_bar(self, tmp_path: Path) -> None:
        """Even with bad_tick=False, a positivity-insane option bar is dropped."""
        from brutex.dashboard.movers_futures import _read_future_session

        sym = "NSE-NIFTY-28Mar24-17500-CE"
        _write_raw_insane(tmp_path, symbol=sym, segment="FNO")
        read = _read_future_session(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol=sym,
            day=_DAY,
            cutoff_min=None,
            bad_tick=False,
        )
        assert read is not None
        # 3 raw bars, middle is high<low insane → dropped → LTP is the last GOOD bar.
        assert str(read[0]) == "1002.00"

    def test_db_options_keeps_legit_4x_premium(self, conn: object, tmp_path: Path) -> None:
        """/db expired-options page keeps a ≥4× premium move (bad_tick OFF)."""
        from brutex.dashboard.db_view import query_option_candles

        OptionParquetWriter(lake_root=tmp_path).write(
            partition_key=_OPT_PK,
            coordinate=_opt_coord(),
            bars=[_obar_premium(i, c) for i, c in enumerate(_PREMIUM_CLOSES)],
        )
        out = query_option_candles(conn, tmp_path, series=_OPT_SERIES, limit=50)
        assert 5.0 in _closes_from_page(out)  # the 5× premium bar is KEPT, not dropped
        assert sorted(_closes_from_page(out)) == [1.0, 1.0, 5.0]
