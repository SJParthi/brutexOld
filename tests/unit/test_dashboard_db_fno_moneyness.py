"""Unit tests for the per-bar F&O moneyness derivation in the ``/db`` candle viewer.

DEC-FNO-MONEYNESS-001 surface C: :func:`brutex.dashboard.db_view.query_candles` appends
the per-bar ``moneyness`` (ITM/ATM/OTM vs the underlying spot at that bar) + signed
``atm_offset`` (ATM±N) columns to every ``segment="FNO"`` page. Moneyness is DERIVED
against the underlying spot at each bar's instant (time-varying — a fixed strike reads
OTM then ITM as spot moves), with a DYNAMIC strike step inferred from the contract's
sibling strike ladder so it works for EVERY F&O underlying (Golden Rule #1).

Covers the spot-by-timestamp reader (INDEX then CASH probe · bounded read · illegal
coordinate · absent spot), the dynamic step deriver (sibling glob · FUT excluded · <2
strikes), the per-bar row builder (option · future · undecodable · no-spot · no-timestamp
projection), the ``_utc_instant`` normaliser, and the ``query_candles`` integration
(populated + empty page · spot page unchanged).
"""

# Whitebox: the per-bar moneyness helpers are private — imported directly so each
# hard-to-reach branch (illegal underlying, naive timestamp, CASH-only spot) is pinned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from brutex.dashboard.db_view import (
    CANDLE_COLUMNS,
    FNO_MONEYNESS_COLUMNS,
    SPOT_DERIVED_COLUMNS,
    _fno_derived_columns,
    _fno_moneyness_rows,
    _fno_strike_step,
    _underlying_spot_close_by_ts,
    _utc_instant,
    query_candles,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

_T1 = datetime(2025, 6, 2, 9, 15, tzinfo=UTC)
_T2 = datetime(2025, 6, 2, 9, 16, tzinfo=UTC)


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    """A throwaway in-memory DuckDB connection (query_candles only needs ``execute``)."""
    return duckdb.connect(":memory:")


def _bar(ts: datetime, close: str, *, oi: int | None = 1000) -> CandleBar:
    c = Decimal(close)
    return CandleBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=10, open_interest=oi)


def _write(
    lake: Path, *, segment: str, symbol: str, bars: list[CandleBar], interval: str = "1minute"
) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment=segment, symbol=symbol, interval=interval, bars=bars
    )


def _seed_option_lake(lake: Path) -> None:
    """A NIFTY 24000 CE + sibling strikes (grid 50) + the NIFTY INDEX spot.

    bar T1 spot 23900 < 24000 → OTM (offset +2); bar T2 spot 24200 > 24000 → ITM
    (offset −4). Sibling strikes 23950/24000/24050 ⇒ derived step 50; a FUT sibling
    is present (must be ignored by the step deriver).
    """
    _write(
        lake,
        segment="FNO",
        symbol="NSE-NIFTY-30Jun25-24000-CE",
        bars=[_bar(_T1, "150"), _bar(_T2, "210")],
    )
    _write(lake, segment="FNO", symbol="NSE-NIFTY-30Jun25-23950-CE", bars=[_bar(_T1, "170")])
    _write(lake, segment="FNO", symbol="NSE-NIFTY-30Jun25-24050-CE", bars=[_bar(_T1, "130")])
    _write(lake, segment="FNO", symbol="NSE-NIFTY-30Jun25-FUT", bars=[_bar(_T1, "24000")])
    _write(
        lake,
        segment="INDEX",
        symbol="NIFTY",
        bars=[_bar(_T1, "23900", oi=None), _bar(_T2, "24200", oi=None)],
    )


# ── _utc_instant ──────────────────────────────────────────────────────────


class TestUtcInstant:
    def test_aware_is_converted_to_utc(self) -> None:
        from zoneinfo import ZoneInfo

        ist = datetime(2025, 6, 2, 14, 45, tzinfo=ZoneInfo("Asia/Kolkata"))
        assert _utc_instant(ist) == datetime(2025, 6, 2, 9, 15, tzinfo=UTC)

    def test_naive_is_stamped_utc(self) -> None:
        naive = datetime(2025, 6, 2, 9, 15)
        assert _utc_instant(naive) == datetime(2025, 6, 2, 9, 15, tzinfo=UTC)


# ── _fno_derived_columns ────────────────────────────────────────────────────


class TestFnoDerivedColumns:
    def test_fno_segment_carries_the_moneyness_pair(self) -> None:
        assert _fno_derived_columns("FNO") == FNO_MONEYNESS_COLUMNS

    @pytest.mark.parametrize("segment", ["CASH", "INDEX"])
    def test_spot_segment_carries_nothing(self, segment: str) -> None:
        assert _fno_derived_columns(segment) == ()


# ── _fno_strike_step (dynamic grid) ─────────────────────────────────────────


class TestFnoStrikeStep:
    def test_derives_min_gap_ignoring_fut(self, tmp_path: Path) -> None:
        _seed_option_lake(tmp_path)
        step = _fno_strike_step(tmp_path, exchange="NSE", symbol="NSE-NIFTY-30Jun25-24000-CE")
        assert step == Decimal("50")

    def test_no_fno_dir_is_none(self, tmp_path: Path) -> None:
        assert (
            _fno_strike_step(tmp_path, exchange="NSE", symbol="NSE-NIFTY-30Jun25-24000-CE") is None
        )

    def test_single_sibling_strike_is_none(self, tmp_path: Path) -> None:
        _write(
            tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24000-CE", bars=[_bar(_T1, "150")]
        )
        assert (
            _fno_strike_step(tmp_path, exchange="NSE", symbol="NSE-NIFTY-30Jun25-24000-CE") is None
        )

    def test_undecodable_sibling_dir_is_skipped(self, tmp_path: Path) -> None:
        # A present-but-undecodable sibling never breaks the step derivation.
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24000-CE", bars=[_bar(_T1, "1")])
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24050-CE", bars=[_bar(_T1, "1")])
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-BADX-CE", bars=[_bar(_T1, "1")])
        assert _fno_strike_step(
            tmp_path, exchange="NSE", symbol="NSE-NIFTY-30Jun25-24000-CE"
        ) == Decimal("50")


# ── _underlying_spot_close_by_ts ────────────────────────────────────────────


class TestUnderlyingSpotByTs:
    def test_empty_ts_values_is_empty(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        assert (
            _underlying_spot_close_by_ts(
                conn, tmp_path, exchange="NSE", underlying="NIFTY", interval="1minute", ts_values=[]
            )
            == {}
        )

    def test_illegal_underlying_coordinate_is_empty(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A derived underlying that is not a legal coordinate degrades to {} (no 500).
        assert (
            _underlying_spot_close_by_ts(
                conn,
                tmp_path,
                exchange="NSE",
                underlying="../evil",
                interval="1minute",
                ts_values=[_T1],
            )
            == {}
        )

    def test_index_hit_returns_close_map(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_option_lake(tmp_path)
        out = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            ts_values=[_T1, _T2],
        )
        assert out[_utc_instant(_T1)] == Decimal("23900.00")
        assert out[_utc_instant(_T2)] == Decimal("24200.00")

    def test_cash_fallback_when_index_absent(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A stock underlying lives under CASH — the INDEX probe misses, CASH hits.
        _write(tmp_path, segment="CASH", symbol="RELIANCE", bars=[_bar(_T1, "1290.00", oi=None)])
        out = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="RELIANCE",
            interval="1minute",
            ts_values=[_T1],
        )
        assert out[_utc_instant(_T1)] == Decimal("1290.00")

    def test_absent_spot_is_empty(self, conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
        assert (
            _underlying_spot_close_by_ts(
                conn,
                tmp_path,
                exchange="NSE",
                underlying="NIFTY",
                interval="1minute",
                ts_values=[_T1],
            )
            == {}
        )

    def test_read_is_bounded_to_the_page_range(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A spot bar OUTSIDE the [min,max] page range is not returned (BETWEEN bound).
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            bars=[
                _bar(_T1, "23900", oi=None),
                _bar(_T2, "24200", oi=None),
                _bar(datetime(2025, 6, 2, 9, 30, tzinfo=UTC), "99999", oi=None),
            ],
        )
        out = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            ts_values=[_T1, _T2],
        )
        assert set(out) == {_utc_instant(_T1), _utc_instant(_T2)}


# ── _fno_moneyness_rows ─────────────────────────────────────────────────────


class TestFnoMoneynessRows:
    def test_option_classifies_per_bar_against_spot(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_option_lake(tmp_path)
        raw = [(_T1, Decimal("150")), (_T2, Decimal("210"))]
        rows = _fno_moneyness_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp", "close"],
        )
        # T1 spot 23900 < 24000 CE → OTM +2; T2 spot 24200 > 24000 → ITM -4 (combined label).
        assert rows == [("OTM +2",), ("ITM -4",)]

    def test_future_is_na_on_every_bar(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        raw = [(_T1,), (_T2,)]
        rows = _fno_moneyness_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-FUT",
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp"],
        )
        assert rows == [("—",), ("—",)]

    def test_undecodable_symbol_is_na(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        raw = [(_T1,)]
        rows = _fno_moneyness_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-BADX-FUT",
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp"],
        )
        assert rows == [("—",)]

    def test_option_without_spot_is_unknown(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # CE present, but no underlying spot in the lake → UNKNOWN, never faked.
        _write(
            tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24000-CE", bars=[_bar(_T1, "150")]
        )
        raw = [(_T1, Decimal("150"))]
        rows = _fno_moneyness_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp", "close"],
        )
        assert rows == [("?",)]

    def test_no_timestamp_projection_is_unknown(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # When the page omits the timestamp column there is no per-bar spot → UNKNOWN.
        _seed_option_lake(tmp_path)
        raw = [(Decimal("150"),), (Decimal("210"),)]
        rows = _fno_moneyness_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=raw,
            out_columns=["close"],
        )
        assert rows == [("?",), ("?",)]


# ── query_candles integration ───────────────────────────────────────────────


class TestQueryCandlesFnoMoneyness:
    def test_fno_page_appends_moneyness_columns_and_values(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_option_lake(tmp_path)
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            limit=50,
        )
        assert out["columns"] == [*CANDLE_COLUMNS, *SPOT_DERIVED_COLUMNS, *FNO_MONEYNESS_COLUMNS]
        # Two derived spot cols then the ONE combined moneyness label per bar vs spot.
        m_idx = out["columns"].index("moneyness")
        assert [r[m_idx] for r in out["rows"]] == ["OTM +2", "ITM -4"]

    def test_fno_empty_page_still_has_moneyness_columns(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A valid F&O coordinate with NO parquet yet → stable column set, zero rows.
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            limit=50,
        )
        assert out["rows"] == []
        assert out["columns"] == [*CANDLE_COLUMNS, *SPOT_DERIVED_COLUMNS, *FNO_MONEYNESS_COLUMNS]

    def test_spot_page_has_no_moneyness_columns(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A CASH (spot) page is unchanged — no F&O moneyness pair.
        _write(tmp_path, segment="CASH", symbol="NSE-RELIANCE", bars=[_bar(_T1, "1290", oi=None)])
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-RELIANCE",
            interval="1minute",
            limit=50,
        )
        assert out["columns"] == [*CANDLE_COLUMNS, *SPOT_DERIVED_COLUMNS]
