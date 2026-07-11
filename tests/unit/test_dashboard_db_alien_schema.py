"""Alien-schema Parquet resilience on the ``/db`` + coverage read paths.

DEC-ALIEN-SCHEMA-READ-HEAL-001 (the db_view half; the scanner/healer half is
``test_lake_invariants_schema_alien.py`` / ``test_lake_heal_schema_alien.py``).

FAILS-BEFORE (reproduced against the pre-fix helper): an alien-schema Parquet
that is the ONLY file in its glob passed the readability-only ``SELECT 1``
probe, so ``_execute_parquet_resilient``'s degraded re-run RE-RAISED the
binder error — ``summarize_spot_datasets`` (→ ``/coverage/spot`` +
``/universe/completeness`` via the shared SPOT cache entry) and the ``/db``
candle pager 500'd on EVERY poll until manual cleanup. Now the stage-2
real-sql schema probe folds the alien file into the skip set: skipped +
counted + logged, never a 500.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("fastapi")

import polars as pl
from brutex.dashboard.db_view import (
    _execute_candle_page_resilient,
    _execute_parquet_resilient,
    query_candles,
    summarize_option_datasets,
    summarize_spot_datasets,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs


@pytest.fixture
def store(tmp_path: Path) -> InstrumentStore:
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


def _bar(ts: datetime, close: str) -> CandleBar:
    c = Decimal(close)
    return CandleBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=10, open_interest=None)


def _partition(lake: Path, *, symbol: str = "NIFTY", year: int = 2024, month: int = 6) -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval="1minute",
        year=year,
        month=month,
    )


def _plant_alien(dest: Path) -> None:
    """A readable Parquet with NO canonical column — the alien-schema shape."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"ts": [1, 2], "px": [1.0, 2.0]}).write_parquet(dest)


class TestAlienSchemaSkip:
    def test_spot_summary_single_alien_file_degrades_not_500(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The headline fails-before: pre-fix this raised duckdb.BinderException
        # out of the aggregate (→ a sticky 500 on /coverage/spot +
        # /universe/completeness per poll). Now: one degraded row, skip logged.
        _plant_alien(_partition(tmp_path))
        with capture_logs() as logs:
            rows = summarize_spot_datasets(store.connection, tmp_path)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "NIFTY"
        assert rows[0]["bars"] == 0
        assert rows[0]["first_date"] is None
        skips = [e for e in logs if e["event"] == "coverage.parquet_skip"]
        assert len(skips) == 1
        assert skips[0]["reason"] == "alien_schema"
        assert [e["event"] for e in logs if e["event"] == "coverage.parquet_unreadable"]

    def test_spot_summary_mixed_missing_column_alien_is_null_filled(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # A MISSING-column alien beside a good file never detonates at all:
        # ``union_by_name`` NULL-fills the absent columns, so the whole-glob
        # aggregate succeeds and the good bar is served (the sticky-500 class
        # is specifically the alien-is-the-ONLY-file shape covered above).
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar(datetime(2024, 6, 3, 4, 1, tzinfo=UTC), "100.40")],
        )
        _plant_alien(_partition(tmp_path, month=7))
        with capture_logs() as logs:
            rows = summarize_spot_datasets(store.connection, tmp_path)
        assert rows[0]["bars"] == 1  # the good June bar is served
        assert not [e for e in logs if e["event"] == "coverage.parquet_skip"]

    def test_mixed_dtype_alien_skipped_good_file_served(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The partial-skip arm: a wrong-DTYPE alien (LIST timestamp) beside a
        # good file DOES detonate the union bind; the schema probe passes the
        # good file, fails the alien one → good rows served + one alien skip.
        base = tmp_path / "mixed"
        base.mkdir()
        pl.DataFrame(
            {
                "timestamp": pl.Series(
                    [datetime(2024, 6, 3, 4, 1, tzinfo=UTC)], dtype=pl.Datetime("us", "UTC")
                ),
                "close": [100.0],
            }
        ).write_parquet(base / "good.parquet")
        pl.DataFrame({"timestamp": [[1.0]], "close": [100.0]}).write_parquet(base / "alien.parquet")
        with capture_logs() as logs:
            rows = _execute_parquet_resilient(
                store.connection,
                base,
                'SELECT "timestamp", close FROM read_parquet(?, union_by_name=true) '
                'WHERE "timestamp" >= ?',
                [datetime(2024, 1, 1, tzinfo=UTC)],
                source="unit",
            )
        assert [float(row[1]) for row in rows] == [100.0]
        skips = [e for e in logs if e["event"] == "db.parquet_skip"]
        assert len(skips) == 1
        assert skips[0]["reason"] == "alien_schema"
        assert skips[0]["path"].endswith("alien.parquet")

    def test_option_summary_single_alien_file_degrades_not_500(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        alien = tmp_path / "options" / "SERIES-A" / "2024-06.parquet"
        _plant_alien(alien)
        with capture_logs() as logs:
            rows = summarize_option_datasets(store.connection, tmp_path)
        assert rows == []  # degraded-but-correct empty, never a raise
        assert [e for e in logs if e["event"] == "coverage.parquet_skip"]

    def test_candle_pager_single_alien_file_empty_page_not_500(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        _plant_alien(_partition(tmp_path, symbol="NSE-NIFTY"))
        with capture_logs() as logs:
            out = query_candles(
                store.connection,
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NSE-NIFTY",
                interval="1minute",
                limit=50,
            )
        assert out["total"] == 0
        assert out["rows"] == []
        assert [
            e for e in logs if e["event"] == "db.parquet_skip" and e["reason"] == "alien_schema"
        ]

    def test_coverage_spot_route_200_with_alien_file(self, tmp_path: Path) -> None:
        # Route-level fails-before: /coverage/spot 500'd per poll on the alien
        # file. Now 200 with the dataset degraded to 0 bars.
        import brutex.dashboard.app as app_module
        from fastapi.testclient import TestClient

        lake = tmp_path / "lake"
        lake.mkdir()
        s = InstrumentStore.open(lake / "instruments.duckdb")
        s.upsert_many(
            [
                Instrument(
                    exch_id=Exchange.NSE,
                    security_id=13,
                    symbol_name="NIFTY",
                    display_name="NIFTY 50",
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
            ],
            snapshot_date=date(2024, 6, 3),
            run_id="r1",
            csv_source_sha256="0" * 64,
        )
        _plant_alien(_partition(lake))
        client = TestClient(
            app_module.build_app(app_module.DashboardState(), instrument_store=s),
            raise_server_exceptions=False,
        )
        resp = client.get("/api/v1/coverage/spot")
        assert resp.status_code == 200
        payload = resp.json()
        rows = {row["symbol"]: row for row in payload["rows"]}
        assert rows["NIFTY"]["bars"] == 0  # skipped + counted honestly, never a 500
        s.close()


class TestUnionConflictResidue:
    def _plant_conflict_pair(self, base: Path) -> None:
        """Two files that EACH pass their own probe yet conflict in union.

        ``close`` DOUBLE in one file vs LIST(DOUBLE) in the other: per-file
        ``COUNT(close)`` succeeds on both; ``union_by_name`` over the pair
        raises the schema-unification ConversionException (verified against
        the pinned duckdb).
        """
        base.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"timestamp": [1], "close": [1.0]}).write_parquet(base / "a.parquet")
        pl.DataFrame({"timestamp": [2], "close": [[1.0, 2.0]]}).write_parquet(base / "b.parquet")

    def test_execute_resilient_union_conflict_degrades_empty(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        base = tmp_path / "pair"
        self._plant_conflict_pair(base)
        skip_sink: list[bool] = []
        with capture_logs() as logs:
            rows = _execute_parquet_resilient(
                store.connection,
                base,
                "SELECT COUNT(close) FROM read_parquet(?, union_by_name=true)",
                [],
                source="unit",
                skip_sink=skip_sink,
            )
        assert rows == []
        assert skip_sink == [True]  # adjacency-sensitive callers must blank
        unreadable = [e for e in logs if e["event"] == "db.parquet_unreadable"]
        assert len(unreadable) == 1
        # No per-file skip fired: BOTH files passed their own probes.
        assert not [e for e in logs if e["event"] == "db.parquet_skip"]

    def test_execute_resilient_union_conflict_no_sink(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The same residue arm with NO skip_sink wired (the non-adjacency
        # callers): still the loud empty degrade, nothing to append to.
        base = tmp_path / "pair"
        self._plant_conflict_pair(base)
        with capture_logs() as logs:
            rows = _execute_parquet_resilient(
                store.connection,
                base,
                "SELECT COUNT(close) FROM read_parquet(?, union_by_name=true)",
                [],
                source="unit",
            )
        assert rows == []
        assert [e for e in logs if e["event"] == "db.parquet_unreadable"]

    def test_page_resilient_union_conflict_degrades_empty(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        base = tmp_path / "pair"
        self._plant_conflict_pair(base)
        with capture_logs() as logs:
            out_columns, raw_rows = _execute_candle_page_resilient(
                store.connection,
                base,
                "SELECT COUNT(close) FROM read_parquet(?, union_by_name=true)",
                [],
                source="unit",
            )
        assert (out_columns, raw_rows) == ([], [])
        assert [e for e in logs if e["event"] == "db.parquet_unreadable"]
