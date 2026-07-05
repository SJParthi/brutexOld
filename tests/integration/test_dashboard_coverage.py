"""Integration tests for the dashboard coverage-ledger endpoints.

Exercises the ``/api/v1/coverage`` + ``/coverage`` routes
(DEC-COVERAGE-LEDGER-001) via FastAPI's
:class:`TestClient` with a real wired :class:`InstrumentStore`, plus the
unwired-store 503 path. These cover the app.py glue (coverage-omit in the
unit run) end-to-end.

Skipped when ``fastapi`` / ``duckdb`` are absent — matches the existing
``test_dashboard_instruments.py`` convention.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.dashboard import app as _app_module
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
from brutex.dashboard.completeness import DIM_NONE, RECONCILE_MISMATCH_EVENT
from brutex.data.coverage_ledger import CoverageRecord, classify_cell
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient
from structlog.testing import capture_logs


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


def _eq(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """An F&O-eligible cash-equity row (scored as ``equity``), e.g. ``M&M``."""
    return Instrument(
        exch_id=exchange,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=symbol,
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
        # DEC-FETCHES-COL-HONESTY-001: the column is RELABELLED "EMPTY-retries" and the
        # legend states it is NOT the total vendor calls — the deeper timeout/429 in-call
        # retry ladder a FAILED window burns is not counted, so the operator cannot read
        # the column as "barely retried".
        assert "EMPTY-retries" in win.text
        assert "NOT the total vendor calls" in win.text


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
                # Re-baselined (DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001): both fixture
                # sessions' first stored minute is 14:45 IST (> the 09:15 open) — the
                # substitution counts pass through the /coverage/spot payload.
                "open_substituted_days": 2,
                "session_days": 2,
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


def _index_inst(symbol: str) -> Instrument:
    """A REAL index instrument on the ``INDEX`` segment (DEC-INDEX-SEGMENT-PK-FIX-001)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
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


class TestColdStartSeedInteriorGap:
    """DEC-COLDSTART-SEED-INTERIOR-GAP-001 end-to-end over a REAL lake (REVISION 2):
    the INDEX partitions are written by the actual ``ParquetWriter`` and the density
    branch is driven through the REAL ``load_lake_spans`` DuckDB read — a green suite
    can never sit over a dead feature."""

    def test_gappy_index_window_not_seeded_and_completeness_not_full(self, tmp_path: Path) -> None:
        # FAILS-BEFORE / PASSES-AFTER: pre-fix the gappy NIFTY window was span-seeded
        # SUCCESS on the lost-ledger cold-start (skippable forever) and /completeness
        # read the symbol "full" — the interior 18-session hole was masked. Post-fix
        # the window is left un-seeded → PLANNED → an honest "partial" on the board.
        from datetime import timedelta

        from brutex.calendars.nse import is_trading_day
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import (
            plan_windows,
            reconcile_ledger_with_lake,
            save_window_ledger,
        )

        with InstrumentStore.open(tmp_path / "instruments.duckdb") as s:
            s.upsert_many(
                [_index_inst("NIFTY"), _index_inst("BANKNIFTY")],
                snapshot_date=date(2024, 1, 2),
                run_id="r-coldstart-gap",
                csv_source_sha256="0" * 64,
            )
            writer = ParquetWriter(lake_root=tmp_path)
            # GAPPY: real bars at BOTH ends of the window (2020-01-01 + 2020-01-28, both
            # NSE trading days) with an 18-trading-session interior hole between them.
            writer.write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[
                    _bar(datetime(2020, 1, 1, 4, 5, tzinfo=UTC), "100.4"),
                    _bar(datetime(2020, 1, 28, 4, 5, tzinfo=UTC), "100.9"),
                ],
            )
            # CLEAN: a bar on EVERY trading session of the same window — the control
            # proving the density gate never false-skips a healthy dataset.
            clean_days = []
            cursor = date(2020, 1, 1)
            while cursor <= date(2020, 1, 28):
                if is_trading_day(cursor):
                    clean_days.append(cursor)
                cursor += timedelta(days=1)
            writer.write(
                exchange="NSE",
                segment="INDEX",
                symbol="BANKNIFTY",
                interval="1minute",
                bars=[
                    _bar(datetime(d.year, d.month, d.day, 4, 5, tzinfo=UTC), "99.9")
                    for d in clean_days
                ],
            )

            ledger = WindowLedger()
            start, end = date(2020, 1, 1), date(2020, 1, 29)  # ONE sub-daily tile
            instruments = [_index_inst("NIFTY"), _index_inst("BANKNIFTY")]
            with capture_logs() as logs:
                seeded = reconcile_ledger_with_lake(
                    ledger,
                    tmp_path,
                    instruments=instruments,
                    intervals=("1minute",),
                    start=start,
                    end=end,
                    market=GROWW_COVERAGE_MARKET,
                )
            # The clean window cold-start-seeded; the gappy one did NOT — and said so.
            assert seeded == 1
            skips = [e for e in logs if e["event"] == "window_ledger.seed_interior_gap_skip"]
            assert len(skips) == 1
            # DEC-DATA-HONESTY-ALLFILTERED-SEED-001: the refusal WARN now names the
            # confirmed run's inclusive bar-less bounds instead of a bare length.
            gap_first = date.fromisoformat(str(skips[0]["gap_first"]))
            gap_last = date.fromisoformat(str(skips[0]["gap_last"]))
            assert date(2020, 1, 1) < gap_first <= gap_last < date(2020, 1, 28)

            # The un-seeded gappy window is minted PLANNED (re-pullable, self-heals via
            # the fetch-time detector); the seeded clean row stays settled.
            plan_windows(
                ledger,
                instruments=instruments,
                intervals=("1minute",),
                start=start,
                end=end,
                market=GROWW_COVERAGE_MARKET,
            )
            status_by_symbol = {(r.security_id, r.status) for r in ledger.records()}
            assert ("NIFTY", "PLANNED") in status_by_symbol
            assert ("BANKNIFTY", "SUCCESS") in status_by_symbol
            save_window_ledger(ledger, tmp_path)

            client = TestClient(build_app(DashboardState(), instrument_store=s))
            spot_rows = {r["symbol"]: r for r in client.get("/api/v1/coverage/spot").json()["rows"]}
            assert spot_rows["NIFTY"]["completeness"] == "pending"
            assert spot_rows["NIFTY"]["windows_pending"] == 1
            assert spot_rows["BANKNIFTY"]["completeness"] == "settled"
            board = client.get("/api/v1/completeness/scoreboard").json()
            per_symbol = {r["symbol"]: r for r in board["spot"]["per_symbol"]}
            # The mask is CLOSED: the gappy symbol reads an honest NOT-full verdict
            # (pre-fix it read "full" — settled ledger + Jan-2020 first bar).
            assert per_symbol["NIFTY"]["1minute"] == "partial"
            # No false-red on the healthy control (settled + Jan-2020 start → full).
            assert per_symbol["BANKNIFTY"]["1minute"] == "full"


class TestDailyOpenSubstitutionHonesty:
    """DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001 end-to-end over a REAL lake: a session
    whose 09:15 IST minute is ABSENT gets its derived 1-day open silently substituted
    from the earliest present minute (tf_resampler ``.first()`` — deliberately
    unchanged); the completeness surface now REPORTS the substitution on every
    projection (scoreboard JSON + /universe map + CSV + /coverage/spot pass-through)
    while the verdict honestly stays ``full`` (informational, never a downgrade)."""

    def test_missing_0915_session_surfaces_on_every_completeness_projection(
        self, tmp_path: Path
    ) -> None:
        # FAILS-BEFORE / PASSES-AFTER: pre-fix none of these payloads carried ANY
        # substitution field — the missing-09:15 session was invisible everywhere.
        from datetime import date as _date

        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import (
            reconcile_ledger_with_lake,
            save_window_ledger,
        )

        with InstrumentStore.open(tmp_path / "instruments.duckdb") as s:
            s.upsert_many(
                [_index_inst("NIFTY")],
                snapshot_date=date(2024, 1, 2),
                run_id="r-open-subst",
                csv_source_sha256="0" * 64,
            )
            # Three consecutive Jan-2020 trading sessions (the 2020 floor, so the verdict
            # can read a genuine "full"): 01+03 open at the canonical 09:15 IST (03:45 UTC);
            # 02's 09:15 minute is MISSING — its first stored minute is 09:16 IST.
            ParquetWriter(lake_root=tmp_path).write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[
                    _bar(datetime(2020, 1, 1, 3, 45, tzinfo=UTC), "100.4"),
                    _bar(datetime(2020, 1, 2, 3, 46, tzinfo=UTC), "100.5"),  # 09:16 IST first
                    _bar(datetime(2020, 1, 3, 3, 45, tzinfo=UTC), "100.6"),
                ],
            )
            # Settle the fetch window (the cold-start seed over the continuous 3-session
            # span) so the ledger-backed verdict is a genuine "full" — proving end-to-end
            # that the substitution is INFORMATIONAL, never a verdict downgrade.
            ledger = WindowLedger()
            seeded = reconcile_ledger_with_lake(
                ledger,
                tmp_path,
                instruments=[_index_inst("NIFTY")],
                intervals=("1minute",),
                start=_date(2020, 1, 1),
                end=_date(2020, 1, 4),
                market=GROWW_COVERAGE_MARKET,
            )
            assert seeded == 1
            save_window_ledger(ledger, tmp_path)

            client = TestClient(build_app(DashboardState(), instrument_store=s))
            # /coverage/spot — the {**row} pass-through.
            (spot_row,) = client.get("/api/v1/coverage/spot").json()["rows"]
            assert spot_row["open_substituted_days"] == 1
            assert spot_row["session_days"] == 3

            # /api/v1/completeness/scoreboard — per-symbol row + per-category sum,
            # verdict stays "full" (settled + Jan-2020 floor + informational count).
            board = client.get("/api/v1/completeness/scoreboard").json()
            (row,) = board["spot"]["per_symbol"]
            assert row["symbol"] == "NIFTY"
            assert row["1minute"] == "full" and row["overall"] == "full"
            assert row["open_substituted_days"] == 1
            assert row["session_days"] == 3
            assert board["spot"]["by_category"]["index"]["open_substituted_days"] == 1

            # /api/v1/universe/completeness — the per-symbol detail map.
            detail = client.get("/api/v1/universe/completeness").json()
            one_min = detail["NIFTY"]["spot"]["1minute"]
            assert one_min["status"] == "full"
            assert one_min["open_substituted_days"] == 1
            assert one_min["session_days"] == 3
            assert one_min["open_substituted"] is True

            # The CSV report — enriched DETAIL-spot section carries N + M.
            csv_text = client.get("/api/v1/completeness/scoreboard.csv").text
            assert "1-minute open-substituted days" in csv_text
            assert "1-minute sessions" in csv_text
            assert "index,NIFTY,NSE,full,2020-01-01,2020-01-03,yes,1,3,full" in (
                csv_text.splitlines()
            )


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
            "empty_threshold": 3,
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

    def test_windows_ledger_fast_path_through_real_connection(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """The DuckDB O(page) fast path drives the route via the REAL store.connection.

        DEC-COVERAGE-WINDOWS-O1-001 (hostile-review MAJOR): the other window tests use the
        legacy ``window_status.json`` sidecar, so this is the ONLY end-to-end proof that
        ``build_window_payload``'s DuckDB branch works through the real ``_LockedReadConn``
        wrapper (lock-hold across ``description``+``fetchall``) + the generation counts
        cache wired in ``build_app`` — exercising the SHIPPED route path, not a bare
        ``duckdb.connect()``. Asserts ``source=="ledger"`` (not the sidecar), the always-
        complete counts, the status filter, and offset paging.

        DEC-UNIVERSE-WINDOW-SEGMENTS-001 extends it to also record a future + an option
        window and assert the payload's ``futures`` / ``options`` buckets are populated —
        pinning the contract the ``/universe`` all-segments inline summary now relies on
        (it renders all three buckets, not only ``spot``, so the F&O counts grow live).
        """
        from brutex.adapters.groww.constants import (
            GROWW_FUTURE_COVERAGE_MARKET,
            GROWW_OPTION_COVERAGE_MARKET,
        )
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import save_window_ledger

        def _key(symbol: str, *, market: str = "groww") -> WindowKey:
            return WindowKey(
                market=market,
                security_id=symbol,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 4, 1),
            )

        now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
        ledger = WindowLedger()
        ledger.record(_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=now)
        ledger.record(
            _key("BANKNIFTY"), raw_bars=0, error_type="GrowwTimeoutError", bars_appended=0, now=now
        )
        ledger.plan(_key("SENSEX"), now=now)  # PLANNED — excluded from the default list
        # DEC-UNIVERSE-WINDOW-SEGMENTS-001: a future + an option window so the payload
        # carries non-empty `futures` and `options` buckets — the contract the /universe
        # all-segments inline summary now relies on (it renders all three buckets, not
        # only `spot`). Pins that `_window_counts_total` keeps splitting the namespaces.
        ledger.record(
            _key("25", market=GROWW_FUTURE_COVERAGE_MARKET),
            raw_bars=7,
            error_type=None,
            bars_appended=7,
            now=now,
        )
        ledger.record(
            _key("42", market=GROWW_OPTION_COVERAGE_MARKET),
            raw_bars=0,
            error_type="GrowwTimeoutError",
            bars_appended=0,
            now=now,
        )
        save_window_ledger(ledger, tmp_path)  # store.path parent (tmp_path) is the lake root

        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/coverage/windows").json()
        assert body["source"] == "ledger"  # the DuckDB fast path, NOT the status_log sidecar
        assert body["count"] == 5
        assert body["counts"]["spot"] == {"SUCCESS": 1, "FAILED": 1, "PLANNED": 1}
        # The /universe all-segments summary reads these two buckets LIVE during the F&O
        # phase (DEC-UNIVERSE-WINDOW-SEGMENTS-001): they must be present + populated so the
        # inline line no longer freezes at the spot total.
        assert body["counts"]["futures"] == {"SUCCESS": 1}
        assert body["counts"]["options"] == {"FAILED": 1}
        symbols = {w["symbol"] for w in body["windows"]}
        # NIFTY/BANKNIFTY spot + the future (security_id 25) + the option (42); PLANNED
        # (SENSEX) is excluded from the default list.
        assert {"NIFTY", "BANKNIFTY"} <= symbols
        assert body["windows"][0]["status"] == "FAILED"  # severity-first

        failed = client.get("/api/v1/coverage/windows?status=FAILED").json()
        # Both FAILED rows surface across the spot + options namespaces (severity-first).
        assert {w["symbol"] for w in failed["windows"]} == {"BANKNIFTY", "42"}

        page = client.get("/api/v1/coverage/windows?limit=1&offset=1").json()
        assert page["returned"] == 1
        assert page["capped"] is True  # matched (4) > limit (1)

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

    def test_spot_scan_shared_cache_across_completeness_and_coverage(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DEC-COMPLETENESS-O1-LATENCY-001: the O(datasets) spot lake scan is now routed
        # through the SAME _summary_cache SPOT entry /coverage/spot uses, so repeat
        # completeness loads — and a /coverage/spot load — SHARE ONE scan instead of
        # re-running the full Parquet-footer aggregate on every request (the 7-9 s page).
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4")],
        )
        calls = {"n": 0}
        # DEC-COMPLETENESS-FNO-SKIP-001: the shared scan is now the FNO-pruned
        # verdict wrapper (same cache entry, same sharing law).
        real = _app_module.summarize_spot_verdict_datasets

        def _counting(conn: object, lake: Path) -> list[dict[str, Any]]:
            calls["n"] += 1
            return real(conn, lake)

        monkeypatch.setattr(_app_module, "summarize_spot_verdict_datasets", _counting)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        # Three reads that each USED to re-run the full scan; now they share one.
        first = client.get("/api/v1/completeness/scoreboard")
        second = client.get("/api/v1/completeness/scoreboard")
        spot = client.get("/api/v1/coverage/spot")
        assert first.status_code == second.status_code == spot.status_code == 200
        # The expensive lake scan ran EXACTLY ONCE across all three loads (shared cache).
        assert calls["n"] == 1
        # Correctness preserved — the cached scan serves a consistent, well-shaped board.
        assert set(first.json()) == {"spot", "options"}
        assert first.json() == second.json()

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
        # 1-day is DERIVED from 1-minute (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) so it is not scored.
        assert "1-minute first bar" in resp.text and "1-minute from 2020?" in resp.text
        assert "1-day first bar" not in resp.text

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

    def test_reconcile_diagnostic_pinpoints_mnm_lake_key_divergence(self, tmp_path: Path) -> None:
        """DEC-COMPLETENESS-SPOT-KEY-RECOVER-001 e2e (the FIX of #907's diagnostic): a scored
        ``M&M`` whose lake partition keys on the DIVERGENT exchange-prefixed ``NSE-M&M`` is now
        RECOVERED — the read-side resolver strips the row's own ``NSE-`` prefix and groups the
        real on-disk data under the canonical ``M&M``, so ``M&M`` SHOWS on the board instead of
        reading empty. The #907 reconciliation diagnostic STILL fires its WARNING (the raw
        partition key is genuinely drifted and should be cleaned up), so the operator both SEES
        the data AND is told to fix the stored key. Nothing is fabricated — only the partition's
        physically-present bar surfaces under the clean symbol."""
        s = InstrumentStore.open(tmp_path / "instruments.duckdb")
        s.upsert_many(
            [_eq("M&M")],  # the scored universe keys on the clean engine symbol_name ``M&M``
            snapshot_date=date(2024, 1, 2),
            run_id="r-mnm",
            csv_source_sha256="0" * 64,
        )
        # The lake partition is written under the DIVERGENT ``NSE-M&M`` — the stored key drift.
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-M&M",
            interval="1minute",
            bars=[_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4")],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=s))
        try:
            with capture_logs() as logs:
                body = client.get("/api/v1/completeness/scoreboard").json()
        finally:
            s.close()

        # The board now RECOVERS the divergent partition: the exchange-prefixed ``NSE-M&M`` lake
        # key is resolved to the canonical scored ``M&M`` (DEC-COMPLETENESS-SPOT-KEY-RECOVER-001),
        # so ``M&M`` appears with its real on-disk data and the raw drifted key is NOT shown.
        per_symbol = {row["symbol"]: row for row in body["spot"]["per_symbol"]}
        assert "NSE-M&M" not in per_symbol  # the raw drifted key is never shown verbatim
        assert "M&M" in per_symbol  # recovered + grouped under the canonical scored symbol
        assert per_symbol["M&M"]["exchange"] == "NSE"  # carries the partition's exchange
        # The recovered row is now VISIBLE on the board (vs silently dropped pre-fix); its spot
        # verdict is ledger-driven (``none`` here — this e2e seeds a bar but no planned window;
        # the real-verdict recovery path is proven by the DIM_FULL unit test). The fix surfaces
        # the partition, not a fabricated verdict.
        assert per_symbol["M&M"]["1minute"] == DIM_NONE

        # ONE structured WARNING STILL fires, naming BOTH repr() strings — the partition key is
        # genuinely drifted (clean it up) even though the board now recovers its data.
        warnings = [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT]
        assert len(warnings) == 1
        entry = warnings[0]
        assert entry["log_level"] == "warning"
        assert entry["stray_lake_symbols"] == [repr("NSE-M&M")]
        assert entry["unmatched_scored_symbols"] == [repr("M&M")]

    def test_reconcile_diagnostic_silent_when_lake_key_matches_scored(self, tmp_path: Path) -> None:
        """The clean counterpart: when the lake partition key EQUALS the scored ``symbol_name``
        (``M&M`` on both sides) the symbol scores normally and NO reconciliation WARNING fires —
        the diagnostic is silent in the healthy steady state."""
        s = InstrumentStore.open(tmp_path / "instruments.duckdb")
        s.upsert_many(
            [_eq("M&M")],
            snapshot_date=date(2024, 1, 2),
            run_id="r-mnm-clean",
            csv_source_sha256="0" * 64,
        )
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="CASH",
            symbol="M&M",  # CONSISTENT with the scored symbol_name → no divergence
            interval="1minute",
            bars=[_bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4")],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=s))
        try:
            with capture_logs() as logs:
                body = client.get("/api/v1/completeness/scoreboard").json()
        finally:
            s.close()

        per_symbol = {row["symbol"]: row for row in body["spot"]["per_symbol"]}
        assert "M&M" in per_symbol  # scored normally (not dropped, not ``none`` for no-key)
        assert [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT] == []

    def test_universe_completeness_map_has_seeded_underlying(self, store: InstrumentStore) -> None:
        # DEC-COMPLETENESS-UNIVERSE-001: the per-symbol map (for the /universe strip) carries
        # the seeded NIFTY underlying with its option status + the honest no-spot defaults.
        years = _coverage_years()
        store.upsert_coverage(_cov(years[0]))
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/universe/completeness").json()
        assert "NIFTY" in body
        entry = body["NIFTY"]
        # 1-day is DERIVED from 1-minute so it is not scored — only 1-minute + overall remain.
        assert set(entry["spot"]) == {"1minute", "overall"}
        assert entry["spot"]["1minute"]["status"] == "none"  # no spot lake data in this fixture
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
    "bars_net_new",
]


class TestWindowLedgerApi:
    """GET /api/v1/db/window-ledger — the unified per-window ledger as a /db table."""

    def test_empty_when_no_ledger(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/db/window-ledger")
        assert resp.status_code == 200
        assert resp.json() == {
            "columns": _LEDGER_COLS,
            "rows": [],
            "total": 0,
            "limit": 100,
            "offset": 0,
        }

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
            key,
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            bars_net_new=4,
            now=datetime(2026, 6, 9, tzinfo=UTC),
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
        # in_pull_fetches (idx 12, DEC-WINDOW-INPULL-FETCHES-001): 1 for this
        # first-try SUCCESS — the /db grid surfaces the in-pull retry too.
        assert row[12] == 1
        # bars_net_new (idx 13, DEC-INGEST-BOUNDARY-INTEGRITY-001): the last column —
        # the sink's keep-first-DEDUPED net-new count, persisted + surfaced on /db.
        assert row[13] == 4
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
            "limit": 100,
            "offset": 0,
        }

    def test_paginates_with_limit_offset(self, store: InstrumentStore, tmp_path: Path) -> None:
        """The ledger returns ONE LIMIT/OFFSET page with the real (full) total
        (DEC-DB-VIEWER-PAGINATE-ALL-001), and the page-slice preserves the
        canonical to_rows() sort byte-for-byte (a page boundary never duplicates
        or drops a row vs the full read)."""
        from datetime import UTC, date, datetime

        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import save_window_ledger

        ledger = WindowLedger()
        for i in range(5):
            ledger.record(
                WindowKey(
                    market="dhan",
                    security_id=f"SYM{i:02d}",
                    interval="1minute",
                    start=date(2021, 1, 1),
                    end=date(2021, 4, 1),
                ),
                raw_bars=5,
                error_type=None,
                bars_appended=5,
                now=datetime(2026, 6, 9, tzinfo=UTC),
            )
        save_window_ledger(ledger, tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))

        full = client.get("/api/v1/db/window-ledger?limit=500&offset=0").json()
        assert full["total"] == 5 and len(full["rows"]) == 5

        page1 = client.get("/api/v1/db/window-ledger?limit=2&offset=0").json()
        page2 = client.get("/api/v1/db/window-ledger?limit=2&offset=2").json()
        assert page1["total"] == 5 and page1["limit"] == 2 and page1["offset"] == 0
        assert len(page1["rows"]) == 2 and len(page2["rows"]) == 2
        # Contiguous, non-overlapping pages identical to the full read's slices.
        assert page1["rows"] == full["rows"][0:2]
        assert page2["rows"] == full["rows"][2:4]
        # Offset past the end → a clean empty page, total still the real count.
        tail = client.get("/api/v1/db/window-ledger?limit=10&offset=999").json()
        assert tail["rows"] == [] and tail["total"] == 5
