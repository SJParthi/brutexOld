"""Integration tests for ``/api/v1/db/candles/datasets`` option-label decode.

DEC-DB-OPTION-DATASET-DECODE-001 (the #629 tracked follow-up): the /db
expired-options dataset names rendered the raw on-disk vendor segment
(``NSE_FNO__25__MONTH__1__ATM-10__CALL``) instead of the operator's
coordinates. The route now builds the id→symbol map from the wired
:class:`InstrumentStore` on the SAME threadpool worker
(DEC-DASHBOARD-FASTREAD-001) and each option entry carries a display-only
``label`` (``BANKNIFTY · MONTH#1 ATM-10 CALL · 1m``); the ``series`` KEY is
byte-unchanged so ``/api/v1/db/options?series=…`` keeps working — proven
end-to-end here with a real lake written by the production writer.

Skipped when ``fastapi`` / ``duckdb`` are absent — matches the existing
``test_dashboard_coverage.py`` convention.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _idx(symbol: str, security_id: int) -> Instrument:
    """A minimal NSE index row with an EXPLICIT security_id (the map key)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
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
def store(tmp_path: Path):
    # security_id 25 is the map key the option series below addresses — the
    # live BANKNIFTY id from the operator's #629 screenshot.
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("BANKNIFTY", 25)],
        snapshot_date=date(2026, 6, 1),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _obar(side: str) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2024, 3, 1, 4, 0, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("48000.00"),
        spot=Decimal("47980.55"),
        iv=Decimal("0.1523"),
    )


def _write_option_series(lake_root: Path, partition_key: str) -> None:
    """One CE+PE bar pair via the PRODUCTION writer (real on-disk layout)."""
    seg, sec, flag, code, offset, _typ = partition_key.split("|")
    OptionParquetWriter(lake_root=lake_root).write(
        partition_key=partition_key,
        coordinate=OptionSeriesCoordinate(
            exch_id="NSE",
            exchange_segment=seg,
            security_id=int(sec),
            underlying_symbol="BANKNIFTY",
            expiry_flag=flag,
            expiry_code=int(code),
            strike_offset=offset,
        ),
        bars=[_obar("CE"), _obar("PE")],
    )


class TestDbCandleDatasetsApi:
    """The dataset picker decodes option labels; the series KEY keeps querying."""

    def test_option_dataset_label_decoded_and_key_queryable(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        _write_option_series(tmp_path, "NSE_FNO|25|MONTH|1|ATM-10|CALL")
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        body = client.get("/api/v1/db/candles/datasets").json()
        assert body["datasets"] == []  # no spot bars written
        (entry,) = body["options"]
        assert entry == {
            "kind": "option",
            "series": "NSE_FNO__25__MONTH__1__ATM-10__CALL",
            "interval": "1minute",
            "label": "BANKNIFTY · MONTH#1 ATM-10 CALL · 1m",
        }
        # The RAW series key still drives the option reader — the decode is
        # display-only, queries are byte-unchanged.
        page = client.get("/api/v1/db/options", params={"series": entry["series"]}).json()
        assert page["total"] == 2  # the CE + PE bars written above

    def test_plus_strike_series_listed_decoded_and_queryable(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Regression for the bundled allowlist fix: an ATM+N series (stored
        # VERBATIM with `+`) was skipped by the picker and refused by the
        # reader before DEC-DB-OPTION-DATASET-DECODE-001.
        _write_option_series(tmp_path, "NSE_FNO|25|WEEK|1|ATM+3|CALL")
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        body = client.get("/api/v1/db/candles/datasets").json()
        (entry,) = body["options"]
        assert entry["series"] == "NSE_FNO__25__WEEK__1__ATM+3__CALL"
        assert entry["label"] == "BANKNIFTY · WEEK#1 ATM+3 CALL · 1m"
        page = client.get("/api/v1/db/options", params={"series": entry["series"]}).json()
        assert page["total"] == 2

    def test_datasets_discovery_cached_across_tab_switches(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DEC-DB-TAB-SWITCH-O1-001: the 3× O(files) `bars/` dir-walk behind
        # /api/v1/db/candles/datasets fires on EVERY /db candle tab switch
        # (spot↔F&O↔option). It is now memoised behind the write generation, so
        # repeated tab switches share ONE discovery instead of re-walking the whole
        # lake every time, AND a pull (generation bump) correctly busts it. Spy on
        # `list_fno_candle_datasets` (called exactly once per build) to prove BOTH the
        # cached-hit (one discovery across tab switches) and the invalidation half (a
        # `mark_dirty` → rediscover) at THIS call site, with an INJECTED SummaryCache.
        # The spy is installed by DOTTED-PATH string (CodeQL py/import-and-import-from:
        # the module is already imported via `from … import` above, so we do NOT also
        # `import` it as a module — the `real` fn comes from its db_view source, which
        # is the SAME object the consumer's namespace holds). Since
        # DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001 the route's `_datasets()` delegates the
        # payload shape to the COMMON `discovery_warm.build_db_datasets_payload`, so
        # the walk call site to spy is the `discovery_warm` namespace.
        from brutex.dashboard.db_view import list_fno_candle_datasets
        from brutex.dashboard.summary_cache import SummaryCache

        _write_option_series(tmp_path, "NSE_FNO|25|MONTH|1|ATM-10|CALL")
        calls = {"n": 0}

        def _counting(lake: Path) -> list[dict[str, object]]:
            calls["n"] += 1
            return list_fno_candle_datasets(lake)

        monkeypatch.setattr("brutex.dashboard.discovery_warm.list_fno_candle_datasets", _counting)
        summary_cache = SummaryCache()
        client = TestClient(
            build_app(DashboardState(), instrument_store=store, summary_cache=summary_cache)
        )
        first = client.get("/api/v1/db/candles/datasets").json()
        second = client.get("/api/v1/db/candles/datasets").json()
        third = client.get("/api/v1/db/candles/datasets").json()
        # The O(files) discovery ran EXACTLY ONCE across three tab-switch loads.
        assert calls["n"] == 1
        # The cache serves consistent discovery — every load is byte-identical.
        assert first == second == third
        # Invalidation half: a pull bumps the generation → the next switch rediscovers
        # (never serves a stale dataset list — the no-illusion guarantee at this site).
        summary_cache.mark_dirty()
        after_pull = client.get("/api/v1/db/candles/datasets").json()
        assert calls["n"] == 2  # the bumped generation forced exactly one re-discovery
        assert after_pull == first  # same lake → same payload, just freshly discovered

    def test_no_lake_root_returns_empty_payload(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/db/candles/datasets").json()
        assert body["datasets"] == []
        assert body["fno"] == []
        assert body["options"] == []
        # The F&O candle logical-table schema is ALWAYS present (the /db twin of the
        # schema_meta registration, DEC-FNO-CANDLE-LAKE-001), even with no lake root.
        assert [c["name"] for c in body["fno_schema"]] == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
            # STORED nullable Greeks overlay (schema v3, DEC-GREEKS-ENGINE-PR2B-001).
            "iv",
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "spot_at_bar",
            "t_years_used",
            "rate_used",
            "greeks_provenance_id",
            "exchange",
            "segment",
            "underlying",
            "expiry_date",
            "instrument_type",
            "strike",
            "option_type",
            "interval",
            # READ-time derived combined moneyness label (DEC-FNO-MONEYNESS-001) + the
            # value columns (DEC-DB-FNO-COLUMNS-001).
            "moneyness",
            "spot",
            "intrinsic",
            "time_value",
            "days_to_expiry",
        ]


class TestDbOptionsRouteCountCache:
    """``/api/v1/db/options`` shares the generation-stamped count cache (DEC-LAKE-READ-O1-AUDIT-001).

    The route now passes ``count_cache=_candle_count_cache`` +
    ``generation=_summary_cache.generation`` into :func:`query_option_candles`, so the
    O(files) ``COUNT(*)`` over the series glob runs ONCE per (series, generation) instead
    of on every paging click, and a pull (a ``mark_dirty`` generation bump) busts it.
    """

    def test_count_runs_once_across_paging_then_busts_on_generation(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A series whose page total is paging-invariant — two CE+PE bars (total 2).
        _write_option_series(tmp_path, "NSE_FNO|25|MONTH|1|ATM-10|CALL")
        series = "NSE_FNO__25__MONTH__1__ATM-10__CALL"

        # Spy on the COUNT(*) the option reader issues, installed by DOTTED-PATH string
        # so the real (db_view-sourced) helper is wrapped exactly once (CodeQL
        # py/import-and-import-from: db_view is already imported via `from … import`).
        from brutex.dashboard.db_view import query_option_candles as _real

        execs = {"count": 0}

        class _CountingConn:
            def __init__(self, real: object) -> None:
                self._real = real

            def execute(self, sql: str, parameters: object = None) -> object:
                # The deduped total is a COUNT(DISTINCT …) since
                # DEC-LAKE-READ-UNIQUE-001 — match any COUNT( aggregate.
                if "COUNT(" in sql:
                    execs["count"] += 1
                return (
                    self._real.execute(sql)  # type: ignore[attr-defined]
                    if parameters is None
                    else self._real.execute(sql, parameters)  # type: ignore[attr-defined]
                )

        def _wrapped(conn: object, *args: object, **kwargs: object) -> object:
            return _real(_CountingConn(conn), *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.dashboard.app.query_option_candles", _wrapped)

        from brutex.dashboard.summary_cache import SummaryCache

        summary_cache = SummaryCache()
        client = TestClient(
            build_app(DashboardState(), instrument_store=store, summary_cache=summary_cache)
        )

        # Three paging clicks of the SAME series + generation → stable total with
        # NO per-click recount. The FIRST click pays exactly two O(files) scans:
        # the legacy COUNT (a cache miss — the stats shard does not exist yet)
        # plus the ONE-TIME write-behind seed of the stats manifest
        # (DEC-LAKE-STATS-MANIFEST-001 — the route opts in, and a fallback
        # rebuilds the shard so every later click is manifest-served); clicks
        # 2-3 run ZERO counts (cache hit + manifest-pruned page).
        p1 = client.get("/api/v1/db/options", params={"series": series, "limit": 1}).json()
        p2 = client.get(
            "/api/v1/db/options", params={"series": series, "limit": 1, "offset": 1}
        ).json()
        p3 = client.get(
            "/api/v1/db/options", params={"series": series, "limit": 1, "desc": "true"}
        ).json()
        assert p1["total"] == p2["total"] == p3["total"] == 2
        assert execs["count"] == 2, (
            "click 1 = one legacy COUNT + the one-time seed scan; later clicks recount NOTHING"
        )

        # A pull bumps the generation → the next click REVALIDATES instead of
        # blindly recounting: the seeded stats shard still matches the live
        # directory (per-request stat-token validation), so the fresh total is
        # served from the manifest with ZERO further COUNT scans — never stale,
        # and cheaper than the pre-manifest recount (DEC-LAKE-STATS-MANIFEST-001).
        summary_cache.mark_dirty()
        after = client.get("/api/v1/db/options", params={"series": series, "limit": 1}).json()
        assert after["total"] == 2
        assert execs["count"] == 2, (
            "the bumped generation must revalidate via the manifest — no recount scan"
        )
