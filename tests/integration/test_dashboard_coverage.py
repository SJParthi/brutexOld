"""Integration tests for the dashboard coverage-ledger endpoints.

Exercises the ``/api/v1/coverage`` + ``/coverage`` routes
(DEC-COVERAGE-LEDGER-001) via FastAPI's
:class:`TestClient` with a real wired :class:`InstrumentStore`, plus the
unwired-store 503 path. These cover the app.py glue (coverage-omit in the
unit run) end-to-end.

Skipped when ``fastapi`` / ``duckdb`` are absent — matches the existing
``test_dashboard_instruments.py`` convention.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.dashboard.app import (
    DashboardState,
    _coverage_years,
    _render_completeness_html,
    _render_coverage_html,
    _render_expired_options_html,
    _render_fetch_windows_html,
    _render_spot_coverage_html,
    build_app,
)
from brutex.data.coverage_ledger import CoverageRecord, classify_cell
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _idx(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    return Instrument(
        exch_id=exchange,
        security_id=abs(hash(symbol)) % 100_000,
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
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    # One index → the expected grid has (NSE, NIFTY) × every coverage year.
    s.upsert_many(
        [_idx("NIFTY")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _cov(year: int, *, error: str | None = None, expiries: int = 2) -> CoverageRecord:
    return CoverageRecord(
        exch_id="NSE",
        underlying_symbol="NIFTY",
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=expiries * 10,
        status=classify_cell(expiries, expiries * 10, error),
        last_run_id="run-1",
        last_fetched_at=datetime(2026, 5, 30, 9, 15, tzinfo=UTC),
        error_message=error,
    )


class TestCoverageApi:
    def test_coverage_empty_ledger_all_missing(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/coverage")
        assert resp.status_code == 200
        body = resp.json()
        years = _coverage_years()
        assert body["headline"]["total"] == len(years)
        assert body["headline"]["complete"] == 0
        assert body["headline"]["missing"] == len(years)
        assert body["headline"]["percent"] == 0
        # Every row is a missing cell for NIFTY.
        assert len(body["rows"]) == len(years)
        assert all(r["status"] == "missing" for r in body["rows"])
        assert all(r["underlying"] == "NIFTY" for r in body["rows"])

    def test_coverage_with_complete_and_failed_cells(self, store: InstrumentStore) -> None:
        years = _coverage_years()
        store.upsert_coverage(_cov(years[0]))  # complete
        store.upsert_coverage(_cov(years[1], error="boom", expiries=0))  # failed
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/coverage").json()
        assert body["headline"]["complete"] == 1
        assert body["headline"]["failed"] == 1
        assert body["headline"]["missing"] == len(years) - 2
        statuses = {r["year"]: r["status"] for r in body["rows"]}
        assert statuses[years[0]] == "complete"
        assert statuses[years[1]] == "failed"
        failed_row = next(r for r in body["rows"] if r["year"] == years[1])
        assert failed_row["error"] == "boom"

    def test_coverage_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        resp = client.get("/api/v1/coverage")
        assert resp.status_code == 503

    def test_coverage_page_renders_hub(self, store: InstrumentStore) -> None:
        # The /coverage page is now a HUB (DEC-DASHBOARD-COVERAGE-SPLIT-001): it
        # links to the three focused sub-pages + shows headline counts. The detail
        # tables moved to /coverage/options, /coverage/spot, /coverage/windows.
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/coverage")
        assert resp.status_code == 200
        assert "Coverage" in resp.text
        assert 'href="/coverage"' in resp.text
        # The hub links to each focused sub-page.
        assert 'href="/coverage/options"' in resp.text
        assert 'href="/coverage/spot"' in resp.text
        assert 'href="/coverage/windows"' in resp.text
        assert 'href="/completeness"' in resp.text  # DEC-COMPLETENESS-PAGE-001 card

    def test_coverage_subpages_render_html(self, store: InstrumentStore) -> None:
        # Each focused sub-page (DEC-DASHBOARD-COVERAGE-SPLIT-001) renders 200 + a
        # cross-link sub-nav + polls its own coverage API.
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        opt = client.get("/coverage/options")
        assert opt.status_code == 200
        assert "Expired options" in opt.text
        assert "/api/v1/coverage/options" in opt.text
        assert 'href="/coverage/spot"' in opt.text

        spot = client.get("/coverage/spot")
        assert spot.status_code == 200
        assert "Spot coverage" in spot.text
        assert "/api/v1/coverage/spot" in spot.text
        assert 'href="/coverage/windows"' in spot.text

        win = client.get("/coverage/windows")
        assert win.status_code == 200
        # The per-window status page (DEC-WINDOW-STATUS-001): the operator SEES
        # which fetch windows are SUCCESS / EMPTY / FAILED without the raw API.
        assert "Fetch windows" in win.text
        assert "/api/v1/coverage/windows" in win.text
        assert 'href="/coverage/options"' in win.text
        # DEC-WINDOW-RETRY-LEGEND-001: the visible legend explaining attempts (pulls, not
        # fetches), the in-pull 3x empty-retry, and EMPTY x/3 — so the operator can read a
        # row without the raw code comments (the operator-reported confusion).
        assert "How to read a row" in win.text
        assert "in-pull" in win.text


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


class TestSpotCoverageApi:
    """The SPOT coverage section of /coverage (DEC-COVERAGE-SPOT-001)."""

    def test_spot_empty_lake_is_zero(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/coverage/spot")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"headline": {"datasets": 0, "total_bars": 0}, "rows": []}

    def test_spot_reports_written_datasets(self, store: InstrumentStore, tmp_path: Path) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[
                _bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4"),
                _bar(datetime(2024, 3, 2, 9, 15, tzinfo=UTC), "100.8"),
            ],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/coverage/spot").json()
        assert body["headline"] == {"datasets": 1, "total_bars": 2}
        # DEC-SPOT-COVERAGE-COMPLETENESS-001: rows carry the fetch-window chip
        # fields; a lake with NO window ledger is an honest "no_ledger_record"
        # (counts null), never implied-settled.
        assert body["rows"] == [
            {
                "exchange": "NSE",
                "segment": "CASH",
                "symbol": "NSE-NIFTY",
                "interval": "1minute",
                "bars": 2,
                "first_date": "2024-03-01",
                "last_date": "2024-03-02",
                "completeness": "no_ledger_record",
                "windows_settled": None,
                "windows_pending": None,
                "windows_failed": None,
            }
        ]

    def test_spot_chip_settled_from_window_ledger(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # DEC-SPOT-COVERAGE-COMPLETENESS-001 e2e: a lake whose window ledger has
        # every window settled for the dataset renders the SETTLED counts.
        from datetime import date

        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import save_window_ledger

        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4")],
        )
        ledger = WindowLedger()
        ledger.record(
            WindowKey(
                market=GROWW_COVERAGE_MARKET,  # the REAL spot namespace the rollup matches
                security_id="NSE-NIFTY",
                interval="1minute",
                start=date(2024, 3, 1),
                end=date(2024, 3, 2),
            ),
            raw_bars=1,
            error_type=None,
            bars_appended=1,
            now=datetime(2024, 3, 2, 12, 0, tzinfo=UTC),
        )
        save_window_ledger(ledger, tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        (row,) = client.get("/api/v1/coverage/spot").json()["rows"]
        assert row["completeness"] == "settled"
        assert row["windows_settled"] == 1
        assert row["windows_pending"] == 0
        assert row["windows_failed"] == 0

    def test_spot_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        assert client.get("/api/v1/coverage/spot").status_code == 503

    def test_spot_empty_payload_when_no_lake_root(self) -> None:
        # A store whose `path` is not a filesystem Path → empty payload, never 500.
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/coverage/spot").json()
        assert body == {"headline": {"datasets": 0, "total_bars": 0}, "rows": []}


class TestWindowStatusApi:
    """The per-window SUCCESS / EMPTY / FAILED status table (DEC-WINDOW-STATUS-001).

    Covers ``/api/v1/coverage/windows``: the empty-sidecar default, a recorded
    window read back through the durable sidecar, the unwired-store 503, and the
    no-lake-root empty-payload (never a 500).
    """

    def test_windows_empty_when_no_sidecar(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/coverage/windows")
        assert resp.status_code == 200
        # DEC-WINDOW-LEDGER-001 (PR-3): the payload now carries a `source` field
        # (which backing answered) — falls back to the status log when no ledger.
        # DEC-OPTION-WINDOW-PLAN-001 (hostile C3): plus the always-complete
        # counts + the capped-row-list accounting fields.
        # DEC-EMPTY-STREAK-VISIBLE-001: plus the R1 EMPTY→SKIPPED promotion bound.
        assert resp.json() == {
            "source": "status_log",
            "count": 0,
            "counts": {"spot": {}, "options": {}, "futures": {}},
            "empty_threshold": 10,
            "matched": 0,
            "returned": 0,
            "capped": False,
            "windows": [],
        }

    def test_windows_reports_recorded_status(self, store: InstrumentStore, tmp_path: Path) -> None:
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_status import WindowStatusLog
        from brutex.data.window_status_store import save_window_status

        log = WindowStatusLog()
        log.record(
            WindowKey(
                market="dhan",
                security_id="NIFTY",
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 1, 1),
            ),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=datetime(2026, 6, 4, 9, 15, tzinfo=UTC),
        )
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        save_window_status(log, tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/coverage/windows").json()
        assert body["count"] == 1
        (row,) = body["windows"]
        assert row["market"] == "dhan"
        assert row["symbol"] == "NIFTY"
        assert row["interval"] == "1day"
        assert row["status"] == "SUCCESS"
        assert row["bars"] == 5
        assert row["attempts"] == 1
        assert row["last_error"] is None

    def test_windows_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        assert client.get("/api/v1/coverage/windows").status_code == 503

    def test_windows_empty_payload_when_no_lake_root(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        # No lake root wired → `source="none"` (DEC-WINDOW-LEDGER-001, PR-3).
        assert client.get("/api/v1/coverage/windows").json() == {
            "source": "none",
            "count": 0,
            "windows": [],
        }


class TestCoverageHelpers:
    def test_coverage_years_starts_2020(self) -> None:
        # Floor is 2020-01-01 (DEC-PULL-FLOOR-2020-001) now that the Phase-0
        # granularity probe confirmed 1-minute data exists from 2020-01-01; the
        # coverage grid aligns to the SAME floor the pull pickers default to.
        years = _coverage_years()
        assert years[0] == 2020
        assert years[-1] == datetime.now(UTC).year
        assert years == list(range(2020, datetime.now(UTC).year + 1))

    def test_render_coverage_hub_links_subpages(self) -> None:
        # The hub (DEC-DASHBOARD-COVERAGE-SPLIT-001) links to each focused page.
        html = _render_coverage_html()
        assert "/coverage" in html
        for href in (
            'href="/coverage/options"',
            'href="/coverage/spot"',
            'href="/coverage/windows"',
            'href="/completeness"',  # DEC-COMPLETENESS-PAGE-001
        ):
            assert href in html, f"hub missing sub-page link {href}"

    def test_subpages_render_their_detail_table(self) -> None:
        # The detail tables live on the focused pages now, not the hub.
        assert "Underlying" in _render_expired_options_html()
        assert "Segment" in _render_spot_coverage_html()
        assert "Last error" in _render_fetch_windows_html()

    def test_fetch_windows_page_has_futures_filter(self) -> None:
        # DEC-COVERAGE-FUTURES-TAB-001: #673 surfaced the `futures` bucket in the
        # /coverage/windows payload (counts + ?market=futures); the page exposes a
        # Futures filter chip so the operator can scope the window list + KPIs to
        # the groww-future namespace (the JS already handles any data-mkt value).
        html = _render_fetch_windows_html()
        assert 'data-mkt="futures"' in html
        assert "🔮 Futures" in html
        assert "no FUTURE windows match" in html  # the futures-specific empty message

    def test_coverage_pages_have_accessibility_polish(self) -> None:
        # Keyboard focus rings + reduce-motion honoured, same safe a11y polish as
        # /universe (DEC-DASHBOARD-A11Y-POLISH-001) — additive, no layout change.
        # Pinned across the hub + all three focused sub-pages.
        for render in (
            _render_coverage_html,
            _render_expired_options_html,
            _render_spot_coverage_html,
            _render_fetch_windows_html,
            _render_completeness_html,
        ):
            html = render()
            assert ":focus-visible" in html
            assert "prefers-reduced-motion" in html
            assert "outline: 2px solid var(--blue)" in html


class TestCompletenessScoreboard:
    """GET /api/v1/completeness/scoreboard{,.csv} + /completeness page
    (DEC-COMPLETENESS-SCOREBOARD-001 / DEC-COMPLETENESS-PAGE-001)."""

    def test_json_scoreboard_has_shape_and_option_rows(self, store: InstrumentStore) -> None:
        years = _coverage_years()
        store.upsert_coverage(_cov(years[0]))  # one complete option-coverage cell for NIFTY
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/completeness/scoreboard").json()
        # The well-shaped payload: spot + options each with a per-category table + drill-down.
        assert set(body) == {"spot", "options"}
        for cat in ("index", "equity", "reference"):
            assert cat in body["spot"]["by_category"]
            assert cat in body["options"]["by_category"]
        # The seeded NIFTY option-coverage row is rolled up into the per-underlying list.
        underlyings = {r["underlying"] for r in body["options"]["per_underlying"]}
        assert "NIFTY" in underlyings

    def test_csv_scoreboard_is_attachment_with_sections(self, store: InstrumentStore) -> None:
        years = _coverage_years()
        store.upsert_coverage(_cov(years[0]))
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/completeness/scoreboard.csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]
        assert "completeness-scoreboard.csv" in resp.headers["content-disposition"]
        # The four self-describing report sections are present + the seeded underlying.
        assert "SUMMARY — spot" in resp.text
        assert "SUMMARY — options" in resp.text
        assert "DETAIL — spot" in resp.text
        assert "DETAIL — options" in resp.text
        assert "NIFTY" in resp.text
        # DEC-COMPLETENESS-CSV-READABLE-001: the live download takes the date-ENRICHED path
        # (per_symbol map wired), so the spot detail carries the first→last bar date columns.
        assert "1-day first bar" in resp.text and "1-day from 2020?" in resp.text

    def test_scoreboard_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        assert client.get("/api/v1/completeness/scoreboard").status_code == 503
        assert client.get("/api/v1/completeness/scoreboard.csv").status_code == 503

    def test_scoreboard_empty_payload_when_no_lake_root(self) -> None:
        # A store whose `path` is not a filesystem Path → empty (well-shaped) board +
        # CSV report, never a 500 (mirrors the /coverage/spot no-lake-root path).
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/completeness/scoreboard").json()
        assert set(body) == {"spot", "options"}
        assert body["spot"]["per_symbol"] == [] and body["options"]["per_underlying"] == []
        csv_resp = client.get("/api/v1/completeness/scoreboard.csv")
        assert csv_resp.status_code == 200
        assert "SUMMARY — spot" in csv_resp.text  # section skeleton still renders

    def test_universe_completeness_map_has_seeded_underlying(self, store: InstrumentStore) -> None:
        # DEC-COMPLETENESS-UNIVERSE-001: the per-symbol map (for the /universe strip) carries
        # the seeded NIFTY underlying with its option status + the honest no-spot defaults.
        years = _coverage_years()
        store.upsert_coverage(_cov(years[0]))
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/universe/completeness").json()
        assert "NIFTY" in body
        entry = body["NIFTY"]
        assert set(entry["spot"]) == {"1day", "1minute", "overall"}
        assert entry["spot"]["1day"]["status"] == "none"  # no spot lake data in this fixture
        assert entry["options"]["status"] in {"stable", "growing", "partial", "failed"}
        assert entry["options"]["first_year"] == years[0]

    def test_universe_completeness_empty_when_no_lake_root(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        assert client.get("/api/v1/universe/completeness").json() == {}

    def test_universe_completeness_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        assert client.get("/api/v1/universe/completeness").status_code == 503

    def test_completeness_page_renders(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/completeness")
        assert resp.status_code == 200
        # The page polls the JSON API + links the CSV report + cross-links the hub.
        assert "/api/v1/completeness/scoreboard" in resp.text
        assert "/api/v1/completeness/scoreboard.csv" in resp.text
        assert 'href="/coverage"' in resp.text
        # Indices vs equities are surfaced SEPARATELY (the operator requirement).
        assert "Indices" in resp.text
        assert "Equities" in resp.text


_LEDGER_COLS = [
    "market",
    "security_id",
    "interval",
    "window_start",
    "window_end",
    "status",
    "bars",
    "attempts",
    "last_error",
    "first_seen_at",
    "last_attempt_at",
    "empty_streak",
    "in_pull_fetches",
]


class TestWindowLedgerApi:
    """GET /api/v1/db/window-ledger — the unified per-window ledger as a /db table."""

    def test_empty_when_no_ledger(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/db/window-ledger")
        assert resp.status_code == 200
        assert resp.json() == {"columns": _LEDGER_COLS, "rows": [], "total": 0}

    def test_lists_recorded_windows_in_grid_shape(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        from datetime import UTC, date, datetime

        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import save_window_ledger

        ledger = WindowLedger()
        key = WindowKey(
            market="dhan",
            security_id="NIFTY",
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 4, 1),
        )
        ledger.record(
            key, raw_bars=5, error_type=None, bars_appended=5, now=datetime(2026, 6, 9, tzinfo=UTC)
        )
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        save_window_ledger(ledger, tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/db/window-ledger").json()
        assert body["columns"] == _LEDGER_COLS
        assert body["total"] == 1
        row = body["rows"][0]
        assert row[1] == "NIFTY"  # security_id
        assert row[5] == "SUCCESS"  # status
        assert row[6] == 5  # bars
        # in_pull_fetches (idx 12, DEC-WINDOW-INPULL-FETCHES-001): the last column,
        # 1 for this first-try SUCCESS — the /db grid surfaces the in-pull retry too.
        assert row[12] == 1
        # first_seen_at (idx 9) shown as clean IST: 2026-06-09 00:00 UTC → 05:30 IST.
        assert row[9] == "2026-06-09 05:30:00"
        assert "+00:00" not in str(row[9])  # the raw UTC offset is gone

    def test_empty_payload_when_no_lake_root(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        assert client.get("/api/v1/db/window-ledger").json() == {
            "columns": _LEDGER_COLS,
            "rows": [],
            "total": 0,
        }
