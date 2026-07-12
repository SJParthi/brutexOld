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
    FNO_DERIVED_COLUMNS,
    SPOT_DERIVED_COLUMNS,
    _fno_derived_columns,
    _fno_derived_rows,
    _fno_strike_step,
    _underlying_spot_close_by_ts,
    _utc_instant,
    query_candles,
)
from brutex.data.models import CandleBar
from brutex.data.moneyness import StepConfidence
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
    def test_fno_segment_carries_the_derived_columns(self) -> None:
        assert _fno_derived_columns("FNO") == FNO_DERIVED_COLUMNS
        # moneyness first, then the four value columns (DEC-DB-FNO-COLUMNS-001), then
        # the decoded Greeks status (DEC-DB-FNO-DISPLAY-TRUTH-001 item 3).
        assert FNO_DERIVED_COLUMNS == (
            "moneyness",
            "spot",
            "intrinsic",
            "time_value",
            "days_to_expiry",
            "greeks_status",
        )

    @pytest.mark.parametrize("segment", ["CASH", "INDEX"])
    def test_spot_segment_carries_nothing(self, segment: str) -> None:
        assert _fno_derived_columns(segment) == ()


# ── _fno_strike_step (dynamic grid) ─────────────────────────────────────────


class TestFnoStrikeStep:
    def test_derives_min_gap_ignoring_fut(self, tmp_path: Path) -> None:
        _seed_option_lake(tmp_path)
        resolved = _fno_strike_step(tmp_path, exchange="NSE", symbol="NSE-NIFTY-30Jun25-24000-CE")
        assert resolved.step == Decimal("50")
        # No registry chain supplied → the lake-observed ladder, honestly labelled.
        assert resolved.confidence is StepConfidence.OBSERVED

    def test_no_fno_dir_is_none(self, tmp_path: Path) -> None:
        resolved = _fno_strike_step(tmp_path, exchange="NSE", symbol="NSE-NIFTY-30Jun25-24000-CE")
        assert resolved.step is None
        assert resolved.confidence is None

    def test_single_sibling_strike_is_none(self, tmp_path: Path) -> None:
        _write(
            tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24000-CE", bars=[_bar(_T1, "150")]
        )
        resolved = _fno_strike_step(tmp_path, exchange="NSE", symbol="NSE-NIFTY-30Jun25-24000-CE")
        assert resolved.step is None
        assert resolved.confidence is None

    def test_undecodable_sibling_dir_is_skipped(self, tmp_path: Path) -> None:
        # A present-but-undecodable sibling never breaks the step derivation.
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24000-CE", bars=[_bar(_T1, "1")])
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24050-CE", bars=[_bar(_T1, "1")])
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-BADX-CE", bars=[_bar(_T1, "1")])
        assert _fno_strike_step(
            tmp_path, exchange="NSE", symbol="NSE-NIFTY-30Jun25-24000-CE"
        ).step == Decimal("50")

    def test_registry_chain_corrects_a_sparse_lake_overestimate(self, tmp_path: Path) -> None:
        # DEC-MONEYNESS-REGISTRY-STEP-001 (fails before the fix — the observed-only
        # derivation read 100): only alternate strikes were pulled (24000/24100 —
        # observed min gap 100), but the AUTHORITATIVE vendor chain carries the full
        # 50-grid → the registry step wins with high confidence.
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24000-CE", bars=[_bar(_T1, "1")])
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24100-CE", bars=[_bar(_T1, "1")])
        chain = [Decimal("24000"), Decimal("24050"), Decimal("24100")]
        resolved = _fno_strike_step(
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            registry_strikes=chain,
        )
        assert resolved.step == Decimal("50")
        assert resolved.confidence is StepConfidence.REGISTRY

    def test_unusable_registry_chain_falls_back_to_observed(self, tmp_path: Path) -> None:
        # A degenerate (single-strike) registry chain cannot size the grid → the
        # observed ladder resolves it, still labelled low-confidence.
        _seed_option_lake(tmp_path)
        resolved = _fno_strike_step(
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            registry_strikes=[Decimal("24000")],
        )
        assert resolved.step == Decimal("50")
        assert resolved.confidence is StepConfidence.OBSERVED


# ── _underlying_spot_close_by_ts ────────────────────────────────────────────


class TestUnderlyingSpotByTs:
    def test_empty_ts_values_is_empty(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        assert _underlying_spot_close_by_ts(
            conn, tmp_path, exchange="NSE", underlying="NIFTY", interval="1minute", ts_values=[]
        ) == ({}, None)

    def test_illegal_underlying_coordinate_is_empty(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A derived underlying that is not a legal coordinate degrades to {} (no 500).
        assert _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="../evil",
            interval="1minute",
            ts_values=[_T1],
        ) == ({}, None)

    def test_index_hit_returns_close_map(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_option_lake(tmp_path)
        out, joined = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            ts_values=[_T1, _T2],
        )
        assert out[_utc_instant(_T1)] == Decimal("23900.00")
        assert out[_utc_instant(_T2)] == Decimal("24200.00")
        assert joined == "1minute"  # served at the page interval, no fallback

    def test_cash_fallback_when_index_absent(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A stock underlying lives under CASH — the INDEX probe misses, CASH hits.
        _write(tmp_path, segment="CASH", symbol="RELIANCE", bars=[_bar(_T1, "1290.00", oi=None)])
        out, joined = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="RELIANCE",
            interval="1minute",
            ts_values=[_T1],
        )
        assert out[_utc_instant(_T1)] == Decimal("1290.00")
        assert joined == "1minute"

    def test_absent_spot_is_empty(self, conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
        assert _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            ts_values=[_T1],
        ) == ({}, None)

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
        out, _joined = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            ts_values=[_T1, _T2],
        )
        assert set(out) == {_utc_instant(_T1), _utc_instant(_T2)}


# ── _fno_derived_rows ───────────────────────────────────────────────────────


class TestFnoDerivedRows:
    def test_option_classifies_per_bar_against_spot(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_option_lake(tmp_path)
        raw = [(_T1, Decimal("150")), (_T2, Decimal("210"))]
        rows, step_conf, spot_join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp", "close"],
        )
        # (moneyness, spot, intrinsic, time_value, days_to_expiry). T1 spot 23900 < 24000 CE
        # → OTM +2, intrinsic max(0, 23900−24000)=0, tv 150−0=150; T2 spot 24200 → ITM -4,
        # intrinsic 200, tv 210−200=10. Bar IST date 2025-06-02 → expiry 2025-06-30 = 28 days.
        assert rows == [
            ("OTM +2", "23900.00", "0.00", "150.00", 28, "not enriched"),
            ("ITM -4", "24200.00", "200.00", "10.00", 28, "not enriched"),
        ]
        # No registry chain wired → the observed lake ladder, honestly labelled.
        assert step_conf is StepConfidence.OBSERVED
        # Served at the page's own interval — no 1minute fallback fired (item 9).
        assert spot_join == "1minute"

    def test_future_is_na_with_days_to_expiry(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        raw = [(_T1,), (_T2,)]
        rows, step_conf, spot_join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-FUT",
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp"],
        )
        # A future: "—" + None value columns (no spot read), but days_to_expiry resolves;
        # greeks_status "—" too — a future is never enriched by design (item 3).
        assert rows == [("—", None, None, None, 28, "—"), ("—", None, None, None, 28, "—")]
        assert step_conf is None  # a future presents no strike grid — nothing to qualify
        assert spot_join is None  # no spot join attempted for a future

    def test_undecodable_symbol_is_na_and_dte_none(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        raw = [(_T1,)]
        rows, step_conf, spot_join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-BADX-FUT",  # bad expiry token → no expiry → dte None
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp"],
        )
        assert rows == [("—", None, None, None, None, "—")]
        assert step_conf is None
        assert spot_join is None

    def test_option_without_spot_is_unknown(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # CE present, but no underlying spot in the lake → "?" + None values, never faked;
        # days_to_expiry still resolves (it is pure, no spot needed).
        _write(
            tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24000-CE", bars=[_bar(_T1, "150")]
        )
        raw = [(_T1, Decimal("150"))]
        rows, step_conf, spot_join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp", "close"],
        )
        assert rows == [("?", None, None, None, 28, "not enriched")]
        assert step_conf is None  # a single-strike ladder has no derivable grid
        assert spot_join is None  # nothing joined anywhere — honest None (item 9)

    def test_no_timestamp_projection_is_unknown(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # When the page omits the timestamp column there is no per-bar spot → "?", and no
        # join key for days_to_expiry either → None.
        _seed_option_lake(tmp_path)
        raw = [(Decimal("150"),), (Decimal("210"),)]
        rows, step_conf, spot_join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=raw,
            out_columns=["close"],
        )
        assert rows == [
            ("?", None, None, None, None, "not enriched"),
            ("?", None, None, None, None, "not enriched"),
        ]
        assert step_conf is StepConfidence.OBSERVED
        assert spot_join is None  # no join key projected → nothing joined

    def test_spot_present_but_close_not_projected_time_value_none(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # Spot resolves (timestamp projected + spot in lake) but ``close`` is not in the
        # projection → spot/intrinsic still compute, time_value is None (no close to net).
        _seed_option_lake(tmp_path)
        raw = [(_T1,)]
        rows, step_conf, spot_join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp"],  # no "close"
        )
        assert rows == [("OTM +2", "23900.00", "0.00", None, 28, "not enriched")]
        assert step_conf is StepConfidence.OBSERVED
        assert spot_join == "1minute"

    def test_put_intrinsic_is_strike_minus_spot(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A PUT mirrors the CE: intrinsic = max(0, strike − spot). The seeded lake gives the
        # NIFTY spot (T1 23900, T2 24200) + the sibling-strike grid (step 50); add a 24000 PE.
        _seed_option_lake(tmp_path)
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-PE",
            bars=[_bar(_T1, "150"), _bar(_T2, "90")],
        )
        raw = [(_T1, Decimal("150")), (_T2, Decimal("90"))]
        rows, step_conf, spot_join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-PE",
            interval="1minute",
            raw_rows=raw,
            out_columns=["timestamp", "close"],
        )
        # T1 spot 23900 < 24000 PE → ITM, intrinsic 24000−23900=100, tv 150−100=50;
        # T2 spot 24200 > 24000 → OTM, intrinsic max(0, 24000−24200)=0, tv 90−0=90.
        assert [(r[0].split()[0], r[2], r[3]) for r in rows] == [
            ("ITM", "100.00", "50.00"),
            ("OTM", "0.00", "90.00"),
        ]
        assert step_conf is StepConfidence.OBSERVED
        assert spot_join == "1minute"


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
        assert out["columns"] == [*CANDLE_COLUMNS, *SPOT_DERIVED_COLUMNS, *FNO_DERIVED_COLUMNS]
        # The combined moneyness label + the value columns per bar vs spot.
        m_idx = out["columns"].index("moneyness")
        assert [r[m_idx] for r in out["rows"]] == ["OTM +2", "ITM -4"]
        cols = out["columns"]
        spot_idx, intr_idx, tv_idx, dte_idx = (
            cols.index("spot"),
            cols.index("intrinsic"),
            cols.index("time_value"),
            cols.index("days_to_expiry"),
        )
        # T1 spot 23900 (CE 24000 → intrinsic 0, tv close-0); T2 spot 24200 (intrinsic 200).
        assert [r[spot_idx] for r in out["rows"]] == ["23900.00", "24200.00"]
        assert [r[intr_idx] for r in out["rows"]] == ["0.00", "200.00"]
        assert [r[tv_idx] for r in out["rows"]] == ["150.00", "10.00"]
        assert [r[dte_idx] for r in out["rows"]] == [28, 28]
        # Provenance: the join was served at the page's own interval (item 9).
        assert out["spot_join_interval"] == "1minute"

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
        assert out["columns"] == [*CANDLE_COLUMNS, *SPOT_DERIVED_COLUMNS, *FNO_DERIVED_COLUMNS]
        # The strike-step honesty field is present-but-None on an empty F&O page
        # (stable key set; no step presented — DEC-MONEYNESS-REGISTRY-STEP-001), and so
        # is the spot-join provenance (nothing joined — DEC-DB-FNO-DISPLAY-TRUTH-001).
        assert out["strike_step_confidence"] is None
        assert out["spot_join_interval"] is None

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
        # No strike grid on a spot page → no honesty field at all (byte-stable payload),
        # and no F&O spot-join provenance either.
        assert "strike_step_confidence" not in out
        assert "spot_join_interval" not in out

    def test_spot_empty_page_has_no_strike_step_field(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-RELIANCE",
            interval="1minute",
            limit=50,
        )
        assert out["rows"] == []
        assert "strike_step_confidence" not in out
        assert "spot_join_interval" not in out


# ── the AUTHORITATIVE registry step in query_candles (DEC-MONEYNESS-REGISTRY-STEP-001) ──


def _seed_sparse_ce_lake(lake: Path) -> None:
    """ONLY alternate strikes pulled (24000 / 24100 — observed gap 100) + the spot.

    The true vendor grid is 50 (24000/24050/24100); the 24050 candles were never
    pulled, so the lake-observed derivation overestimates the step 2×.
    """
    _write(lake, segment="FNO", symbol="NSE-NIFTY-30Jun25-24000-CE", bars=[_bar(_T1, "150")])
    _write(lake, segment="FNO", symbol="NSE-NIFTY-30Jun25-24100-CE", bars=[_bar(_T1, "110")])
    _write(lake, segment="INDEX", symbol="NIFTY", bars=[_bar(_T1, "23900", oi=None)])


def _seed_registry_chain(registry_path: Path) -> None:
    """The FULL vendor chain for NIFTY 30-Jun-25: strikes 24000/24050/24100 (grid 50)."""
    from datetime import date

    from brutex.data.registry_store import RegistryStore

    now = datetime(2025, 6, 1, tzinfo=UTC)
    with RegistryStore.open(registry_path) as reg:
        for strike in (24000.0, 24050.0, 24100.0):
            reg.upsert_contract(
                "NSE",
                "NIFTY",
                date(2025, 6, 30),
                f"NSE-NIFTY-30Jun25-{int(strike)}-CE",
                side="CE",
                strike=strike,
                run_id="r1",
                fetched_at=now,
            )


class TestQueryCandlesRegistryStep:
    """query_candles + the registry chain: the corrected step + the honesty label."""

    def test_registry_chain_corrects_the_sparse_step(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # REGRESSION (fails before the fix): spot 23900, strike 24000. The observed
        # ladder (24000/24100) reads step 100 → "OTM +1"; the AUTHORITATIVE chain
        # restores the 50 grid → "OTM +2" + the high-confidence label.
        _seed_sparse_ce_lake(tmp_path)
        registry_path = tmp_path / "registry.duckdb"
        _seed_registry_chain(registry_path)
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            limit=50,
            registry_path=registry_path,
        )
        m_idx = out["columns"].index("moneyness")
        assert [r[m_idx] for r in out["rows"]] == ["OTM +2"]
        assert out["strike_step_confidence"] == "registry"

    def test_without_registry_the_sparse_step_overestimates_with_honest_label(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # The pre-fix behaviour is PRESERVED (graceful fallback) but no longer
        # silent: the payload carries the "observed" low-confidence label.
        _seed_sparse_ce_lake(tmp_path)
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            limit=50,
            registry_path=tmp_path / "registry.duckdb",  # absent → graceful fallback
        )
        m_idx = out["columns"].index("moneyness")
        assert [r[m_idx] for r in out["rows"]] == ["OTM +1"]  # the overestimated depth
        assert out["strike_step_confidence"] == "observed"

    def test_no_registry_path_wired_keeps_the_observed_label(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_sparse_ce_lake(tmp_path)
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            limit=50,
        )
        assert out["strike_step_confidence"] == "observed"

    def test_future_page_confidence_is_none(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-FUT", bars=[_bar(_T1, "24000")])
        out = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-FUT",
            interval="1minute",
            limit=50,
        )
        assert out["strike_step_confidence"] is None

    def test_chain_is_memoised_per_generation(
        self,
        conn: duckdb.DuckDBPyConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two pages at ONE generation share a single registry read (the injected
        # generation-stamped cache); a generation bump re-reads (never stale).
        from brutex.dashboard import db_view as db_view_module
        from brutex.dashboard.movers_cache import MoversSnapshotCache

        _seed_sparse_ce_lake(tmp_path)
        registry_path = tmp_path / "registry.duckdb"
        _seed_registry_chain(registry_path)
        calls: list[str] = []
        real_loader = db_view_module.load_registry_strike_chain

        def _counting_loader(*args: object, **kwargs: object) -> object:
            calls.append("load")
            return real_loader(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(db_view_module, "load_registry_strike_chain", _counting_loader)
        cache = MoversSnapshotCache()

        def _page(generation: int) -> dict[str, object]:
            return query_candles(
                conn,
                tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY-30Jun25-24000-CE",
                interval="1minute",
                limit=50,
                count_cache=cache,
                generation=generation,
                registry_path=registry_path,
            )

        first = _page(1)
        second = _page(1)  # a paging click within one generation → O(1) chain hit
        assert len(calls) == 1
        assert first["strike_step_confidence"] == second["strike_step_confidence"] == "registry"
        _page(2)  # a pull bumped the generation → the chain is re-read, never stale
        assert len(calls) == 2

    def test_registry_chain_key_shape(self) -> None:
        from datetime import date

        from brutex.dashboard.db_view import fno_strike_chain_key

        key = fno_strike_chain_key("NSE", "NIFTY", date(2025, 6, 30))
        assert key == "strikechain|NSE|NIFTY|2025-06-30"

    def test_registry_chain_helper_none_paths(self, tmp_path: Path) -> None:
        # The direct None guards: no registry wired / an undecodable expiry.
        from datetime import date

        from brutex.dashboard.db_view import _registry_strike_chain

        assert (
            _registry_strike_chain(
                None,
                exchange="NSE",
                underlying="NIFTY",
                expiry=date(2025, 6, 30),
                count_cache=None,
                generation=0,
            )
            is None
        )
        assert (
            _registry_strike_chain(
                tmp_path / "registry.duckdb",
                exchange="NSE",
                underlying="NIFTY",
                expiry=None,
                count_cache=None,
                generation=0,
            )
            is None
        )

    def test_registry_chain_helper_absent_registry_inline(self, tmp_path: Path) -> None:
        # No injected cache (the pure default) + an absent registry → inline None.
        from datetime import date

        from brutex.dashboard.db_view import _registry_strike_chain

        assert (
            _registry_strike_chain(
                tmp_path / "registry.duckdb",
                exchange="NSE",
                underlying="NIFTY",
                expiry=date(2025, 6, 30),
                count_cache=None,
                generation=0,
            )
            is None
        )

    def test_registry_chain_helper_reads_one_chain_inline(self, tmp_path: Path) -> None:
        from datetime import date

        from brutex.dashboard.db_view import _registry_strike_chain

        registry_path = tmp_path / "registry.duckdb"
        _seed_registry_chain(registry_path)
        chain = _registry_strike_chain(
            registry_path,
            exchange="NSE",
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            count_cache=None,
            generation=0,
        )
        assert chain is not None
        assert sorted(chain) == [Decimal("24000"), Decimal("24050"), Decimal("24100")]


# ── spot-key drift recovery (DEC-DB-MONEYNESS-KEY-RECOVER-001) ──────────────


class TestSpotKeyRecovery:
    """The /db moneyness spot-join walks the COMMON drift ladder when the exact
    key probe is empty — mirroring the Greeks enrichment's SPOTJOIN-2 recovery
    (DEC-GREEKS-CORRECTNESS-AUDIT-001), never fabricating a spot."""

    def test_drifted_prefix_key_recovers_spot(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # FAILS BEFORE THE FIX: the spot dir carries the #911 "<EXCH>-<underlying>"
        # drifted form; the raw "NIFTY" probe is empty, the ladder recovers it.
        _write(tmp_path, segment="INDEX", symbol="NSE-NIFTY", bars=[_bar(_T1, "23900", oi=None)])
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            out, joined = _underlying_spot_close_by_ts(
                conn,
                tmp_path,
                exchange="NSE",
                underlying="NIFTY",
                interval="1minute",
                ts_values=[_T1],
            )
        assert out[_utc_instant(_T1)] == Decimal("23900.00")
        assert joined == "1minute"
        recovered = [e for e in logs if e["event"] == "db_view.spot_key_recovered"]
        assert len(recovered) == 1  # exactly ONE recovery log, naming both keys.
        assert recovered[0]["underlying"] == "NIFTY"
        assert recovered[0]["recovered_key"] == "NSE-NIFTY"

    def test_verbose_index_alias_recovers_spot(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # The MIDCPNIFTY chain over a NIFTYMIDSELECT-keyed spot dir (the sourced
        # reverse alias rung) — pre-fix this read moneyness UNKNOWN on every bar.
        _write(
            tmp_path, segment="INDEX", symbol="NIFTYMIDSELECT", bars=[_bar(_T1, "12500", oi=None)]
        )
        out, _joined = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="MIDCPNIFTY",
            interval="1minute",
            ts_values=[_T1],
        )
        assert out[_utc_instant(_T1)] == Decimal("12500.00")

    def test_unrecoverable_key_stays_empty(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # Spot stored under a key NO ladder rung can reach → the honest empty map
        # (every bar classifies UNKNOWN), never a fabricated spot.
        _write(tmp_path, segment="INDEX", symbol="SOMETHINGELSE", bars=[_bar(_T1, "1", oi=None)])
        assert _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            ts_values=[_T1],
        ) == ({}, None)

    def test_canonical_key_never_walks_the_ladder(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Byte-identity on the already-canonical hot path: the exact-key hit
        # returns the same map as before the fix, with ZERO ladder probes and
        # no recovery log.
        from brutex.dashboard import db_view as db_view_mod

        _seed_option_lake(tmp_path)

        def _boom(underlying: str, exchange: str) -> tuple[str, ...]:
            raise AssertionError("ladder must not be walked on an exact-key hit")

        monkeypatch.setattr(db_view_mod, "spot_recovery_candidates", _boom)
        out, joined = _underlying_spot_close_by_ts(
            conn,
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            ts_values=[_T1, _T2],
        )
        assert out[_utc_instant(_T1)] == Decimal("23900.00")
        assert out[_utc_instant(_T2)] == Decimal("24200.00")
        assert joined == "1minute"

    def test_recovered_spot_renders_real_moneyness_end_to_end(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # FAILS BEFORE THE FIX: the full per-bar derivation reads real moneyness
        # (not "?") when the spot dir key drifted to the #911 form.
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            bars=[_bar(_T1, "150"), _bar(_T2, "210")],
        )
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-23950-CE", bars=[_bar(_T1, "1")])
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NSE-NIFTY",
            bars=[_bar(_T1, "23900", oi=None), _bar(_T2, "24200", oi=None)],
        )
        cells, _confidence, _spot_join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=[(_T1, Decimal("150")), (_T2, Decimal("210"))],
            out_columns=["timestamp", "close"],
        )
        # T1 spot 23900 < 24000 CE → OTM; T2 spot 24200 > 24000 → ITM — real
        # labels, not the pre-fix "?" UNKNOWN.
        assert cells[0][0].startswith("OTM")
        assert cells[1][0].startswith("ITM")
        assert cells[0][1] == "23900.00"  # the recovered spot value itself.


# ── the derived greeks_status column (DEC-DB-FNO-DISPLAY-TRUTH-001 item 3) ──


def _enriched_bar(
    ts: datetime,
    close: str,
    *,
    status: int,
    iv: float | None = None,
    delta: float | None = None,
) -> CandleBar:
    """One enriched option bar carrying a REAL packed provenance stamp."""
    from brutex.data.greeks_provenance import (
        RATE_SOURCE_ID_FIXED,
        STYLE_EUROPEAN_EXACT,
        pack_greeks_provenance,
    )
    from brutex.data.models import FnoGreeksBar

    c = Decimal(close)
    return FnoGreeksBar(
        timestamp=ts,
        open=c,
        high=c,
        low=c,
        close=c,
        volume=10,
        open_interest=1000,
        iv=iv,
        delta=delta,
        greeks_provenance_id=pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            rate_version=1,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=status,
        ),
    )


class TestGreeksStatusColumn:
    """The stored provenance stamp's status is DECODED into words on every F&O page
    (item 3) — a NULL iv/delta is never explained only by a bare packed int."""

    def _page(self, conn: duckdb.DuckDBPyConnection, lake: Path, symbol: str) -> dict[str, object]:
        return query_candles(
            conn,
            lake,
            exchange="NSE",
            segment="FNO",
            symbol=symbol,
            interval="1minute",
            limit=50,
        )

    @staticmethod
    def _status_cells(out: dict[str, object]) -> list[object]:
        cols = out["columns"]
        rows = out["rows"]
        assert isinstance(cols, list) and isinstance(rows, list)
        idx = cols.index("greeks_status")
        return [r[idx] for r in rows]

    def test_enriched_ok_bar_reads_ok_with_populated_greeks(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # FAILS BEFORE THE FIX: the "greeks_status" column did not exist at all.
        from brutex.data.greeks_provenance import STATUS_OK

        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            bars=[_enriched_bar(_T1, "150", status=STATUS_OK, iv=0.15, delta=0.5)],
        )
        out = self._page(conn, tmp_path, "NSE-NIFTY-30Jun25-24000-CE")
        cols = out["columns"]
        assert isinstance(cols, list)
        assert "greeks_status" in cols
        assert self._status_cells(out) == ["ok"]
        rows = out["rows"]
        assert isinstance(rows, list)
        assert rows[0][cols.index("iv")] == pytest.approx(0.15)

    def test_below_intrinsic_bar_reads_the_words(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # The audit's headliner: a NULL-iv bar's stored reason renders as WORDS,
        # never a bare "null" beside an opaque packed int.
        from brutex.data.greeks_provenance import STATUS_BELOW_INTRINSIC

        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            bars=[_enriched_bar(_T1, "150", status=STATUS_BELOW_INTRINSIC)],
        )
        out = self._page(conn, tmp_path, "NSE-NIFTY-30Jun25-24000-CE")
        assert self._status_cells(out) == ["below intrinsic — no unique IV"]

    def test_legacy_partition_reads_not_enriched(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A plain 7-column partition (no stamp anywhere) → the honest "not enriched",
        # never a fabricated status.
        _write(
            tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-24000-CE", bars=[_bar(_T1, "150")]
        )
        out = self._page(conn, tmp_path, "NSE-NIFTY-30Jun25-24000-CE")
        assert self._status_cells(out) == ["not enriched"]

    def test_future_reads_na(self, conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
        # A future is never enriched BY DESIGN → "—", mirroring the moneyness "—".
        _write(tmp_path, segment="FNO", symbol="NSE-NIFTY-30Jun25-FUT", bars=[_bar(_T1, "24000")])
        out = self._page(conn, tmp_path, "NSE-NIFTY-30Jun25-FUT")
        assert self._status_cells(out) == ["—"]

    def test_empty_fno_page_carries_the_column_name(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        out = self._page(conn, tmp_path, "NSE-NIFTY-30Jun25-24000-CE")
        cols = out["columns"]
        assert isinstance(cols, list)
        assert out["rows"] == []
        assert "greeks_status" in cols

    def test_non_int_pid_cell_degrades_to_not_enriched(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # Defensive: a non-int greeks_provenance_id cell (a foreign-typed projection)
        # degrades to the honest None-stamp label, never a raise (whitebox — the
        # isinstance guard branch).
        _seed_option_lake(tmp_path)
        rows, _conf, _join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=[(_T1, Decimal("150"), "junk")],
            out_columns=["timestamp", "close", "greeks_provenance_id"],
        )
        assert rows[0][5] == "not enriched"

    def test_bool_pid_cell_degrades_to_not_enriched(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # Finder fold (Golden Rule #1): bool subclasses int, so a stray ``True``
        # cell passed a bare ``isinstance(pid, int)`` narrowing and decoded as
        # packed id 1 → a fabricated "ok". Production-unreachable (DuckDB never
        # yields bool for an INTEGER column) but the narrowing must not rely on
        # that — a bool stamp reads the honest "not enriched", never "ok".
        # FAILS BEFORE THE FIX: rows[0][5] read "ok".
        _seed_option_lake(tmp_path)
        rows, _conf, _join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=[(_T1, Decimal("150"), True)],
            out_columns=["timestamp", "close", "greeks_provenance_id"],
        )
        assert rows[0][5] == "not enriched"
        assert rows[0][5] != "ok"

    def test_int_pid_cell_decodes_through_the_derived_rows(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # Whitebox twin: an int pid in the projection decodes to its words.
        from brutex.data.greeks_provenance import (
            RATE_SOURCE_ID_FIXED,
            STATUS_NO_SPOT,
            STYLE_EUROPEAN_EXACT,
            pack_greeks_provenance,
        )

        _seed_option_lake(tmp_path)
        pid = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            rate_version=1,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=STATUS_NO_SPOT,
        )
        rows, _conf, _join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval="1minute",
            raw_rows=[(_T1, Decimal("150"), pid)],
            out_columns=["timestamp", "close", "greeks_provenance_id"],
        )
        assert rows[0][5] == "no spot — underlying bar missing"


# ── the 1minute spot-join fallback (DEC-DB-FNO-DISPLAY-TRUTH-001 item 9) ──


_T5M2 = datetime(2025, 6, 2, 9, 20, tzinfo=UTC)  # the next 5-minute bucket open


class TestSpotJoin1mFallback:
    """A partially-prebuilt lake (option 5minute present, spot 5minute never
    prebuilt) joins the underlying spot from the always-present 1minute base feed
    — the %CHNG 1minute convention (DEC-SPOT-PREVCLOSE-1MIN-001) mirrored — with
    honest ``spot_join_interval`` provenance; never a fabricated value."""

    def _page(
        self, conn: duckdb.DuckDBPyConnection, lake: Path, *, interval: str
    ) -> dict[str, object]:
        return query_candles(
            conn,
            lake,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            interval=interval,
            limit=50,
        )

    def _seed_5m_option(self, lake: Path) -> None:
        _write(
            lake,
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            bars=[_bar(_T1, "150"), _bar(_T5M2, "210")],
            interval="5minute",
        )
        _write(
            lake,
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-23950-CE",
            bars=[_bar(_T1, "170")],
            interval="5minute",
        )

    def test_5minute_page_falls_back_to_1minute_spot(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # FAILS BEFORE THE FIX (the audit's repro shape): the SAME contract+instant
        # read real ITM/OTM at 1minute but "?" at 5minute on a lake whose spot
        # 5minute partition was never prebuilt.
        self._seed_5m_option(tmp_path)
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            bars=[_bar(_T1, "23900", oi=None), _bar(_T5M2, "24200", oi=None)],
            interval="1minute",
        )
        out = self._page(conn, tmp_path, interval="5minute")
        cols = out["columns"]
        rows = out["rows"]
        assert isinstance(cols, list) and isinstance(rows, list)
        m_idx, s_idx = cols.index("moneyness"), cols.index("spot")
        assert [r[m_idx] for r in rows] == ["OTM +2", "ITM -4"]
        assert [r[s_idx] for r in rows] == ["23900.00", "24200.00"]
        assert out["spot_join_interval"] == "1minute"  # the honest provenance stamp

    def test_display_interval_partition_wins_no_fallback(
        self,
        conn: duckdb.DuckDBPyConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # REGRESSION: with the spot 5minute partition PRESENT the fallback never
        # fires — the 5minute (resampled-close) values serve, provenance reads the
        # page interval, and ONLY the display interval is ever probed (spy).
        self._seed_5m_option(tmp_path)
        # DISTINCT closes per interval prove which partition served.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            bars=[_bar(_T1, "23905", oi=None), _bar(_T5M2, "24205", oi=None)],
            interval="5minute",
        )
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            bars=[_bar(_T1, "23900", oi=None), _bar(_T5M2, "24200", oi=None)],
            interval="1minute",
        )
        from brutex.dashboard import db_view as db_view_mod

        probed: list[str] = []
        real = db_view_mod._execute_parquet_resilient

        def _spy(*args: object, **kwargs: object) -> object:
            source = kwargs.get("source")
            if isinstance(source, str) and source.startswith("candle-spotjoin:"):
                probed.append(source)
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(db_view_mod, "_execute_parquet_resilient", _spy)
        out = self._page(conn, tmp_path, interval="5minute")
        cols = out["columns"]
        rows = out["rows"]
        assert isinstance(cols, list) and isinstance(rows, list)
        s_idx = cols.index("spot")
        assert [r[s_idx] for r in rows] == ["23905.00", "24205.00"]  # the 5minute values
        assert out["spot_join_interval"] == "5minute"
        assert probed and all(src.endswith("/5minute") for src in probed)

    def test_1day_page_cannot_fall_back(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # HONEST LIMIT (documented): a 1day bar's instant is midnight-IST — no
        # 1-minute bar exists at that instant, so the fallback finds nothing and
        # the page stays "?" (never a fabricated spot).
        midnight_ist = datetime(2025, 6, 1, 18, 30, tzinfo=UTC)  # 2025-06-02 00:00 IST
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            bars=[_bar(midnight_ist, "150")],
            interval="1day",
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol="NSE-NIFTY-30Jun25-23950-CE",
            bars=[_bar(midnight_ist, "170")],
            interval="1day",
        )
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            bars=[_bar(_T1, "23900", oi=None)],
            interval="1minute",
        )
        out = self._page(conn, tmp_path, interval="1day")
        cols = out["columns"]
        rows = out["rows"]
        assert isinstance(cols, list) and isinstance(rows, list)
        m_idx = cols.index("moneyness")
        assert [r[m_idx] for r in rows] == ["?"]
        assert out["spot_join_interval"] is None

    def test_missing_minute_stays_unknown_per_bar(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # A single missing 1-minute spot bar degrades ONLY that bar to "?" — the
        # joined bar still serves (per-instant honesty, never nearest-fallback).
        self._seed_5m_option(tmp_path)
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            bars=[_bar(_T1, "23900", oi=None)],  # _T5M2 minute missing
            interval="1minute",
        )
        out = self._page(conn, tmp_path, interval="5minute")
        cols = out["columns"]
        rows = out["rows"]
        assert isinstance(cols, list) and isinstance(rows, list)
        m_idx = cols.index("moneyness")
        assert [r[m_idx] for r in rows] == ["OTM +2", "?"]
        assert out["spot_join_interval"] == "1minute"

    def test_fallback_emits_one_provenance_log(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        from structlog.testing import capture_logs

        self._seed_5m_option(tmp_path)
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            bars=[_bar(_T1, "23900", oi=None)],
            interval="1minute",
        )
        with capture_logs() as logs:
            out = self._page(conn, tmp_path, interval="5minute")
        assert out["spot_join_interval"] == "1minute"
        fallbacks = [e for e in logs if e["event"] == "db_view.spot_tf_fallback"]
        assert len(fallbacks) == 1
        assert fallbacks[0]["interval"] == "5minute"
        assert fallbacks[0]["fallback"] == "1minute"


class TestPostExpiryDte:
    def test_post_expiry_bar_yields_a_negative_days_to_expiry(
        self, conn: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # Documents the data edge the frontend "(expired)" marker exists for
        # (DEC-DB-FNO-DISPLAY-TRUTH-001 item 10): a stored bar dated AFTER the
        # decoded expiry (an explicitly-dated pull past expiry) serves a negative
        # int — the VALUE is untouched, the marking is display-only.
        after_expiry = datetime(2025, 7, 3, 9, 15, tzinfo=UTC)  # expiry 30Jun25
        rows, _conf, _join = _fno_derived_rows(
            conn,
            tmp_path,
            exchange="NSE",
            symbol="NSE-NIFTY-30Jun25-FUT",
            interval="1minute",
            raw_rows=[(after_expiry,)],
            out_columns=["timestamp"],
        )
        assert rows == [("—", None, None, None, -3, "—")]
