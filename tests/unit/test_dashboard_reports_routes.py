"""Functional tests for the pre-open report routes (DEC-PREOPEN-REPORT-001).

Drives the real FastAPI app via ``TestClient`` over a temp lake holding one
symbol (COALINDIA, matching the operator's pre-open CSV) — confirms the symbol
picker, the ``.xlsx`` download (valid workbook, NSE-matching numbers), the
date-validation 400s, and the ``/reports`` page. Skipped cleanly without the
``[ui]`` extra.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import Instrument, InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, InstrumentType, Segment
from fastapi.testclient import TestClient

_EXPECTED_PATHS = frozenset(
    {
        "/api/v1/reports/preopen/symbols",
        "/api/v1/reports/preopen.xlsx",
        "/api/v1/reports/preopen.zip",
        "/reports",
    }
)


def _bar(ts: datetime, *, open_: str, close: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(close),
        low=Decimal(open_),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


def _client(tmp_path: Path, *, with_data: bool = True) -> TestClient:
    if with_data:
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="COALINDIA",
            interval="1day",
            bars=[
                _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="455", close="458.15"),
                _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="429", close="431.55"),
            ],
        )
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="COALINDIA",
            interval="1minute",
            bars=[_bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="429.00", close="431.55")],
        )
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=store))


def test_report_routes_registered(tmp_path: Path) -> None:
    app = build_app(DashboardState(), instrument_store=None)
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert paths >= _EXPECTED_PATHS


def test_symbols_lists_report_eligible(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen/symbols")
    assert resp.status_code == 200
    rows = resp.json()["symbols"]
    assert [s["symbol"] for s in rows] == ["COALINDIA"]
    # Every symbol carries the option-pullability tag (DEC-OPTIONS-ELIGIBLE-GUARD-001).
    # The store here holds no instruments, so COALINDIA defaults to spot-only.
    assert rows[0]["option_pullable"] is False


def _eq(symbol: str) -> Instrument:
    """A minimal NSE cash-equity (EQ) instrument-master row."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(symbol) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
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


def _ce(underlying: str) -> Instrument:
    """A minimal NSE CE option leg naming *underlying* (makes it F&O-eligible)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=500_000 + abs(hash(f"{underlying}CE")) % 400_000,
        symbol_name=f"{underlying}CE",
        display_name=f"{underlying} CE",
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=hash(underlying) % 100_000,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=Decimal("20000"),
        option_type="CE",
        asm_gsm_flag=False,
    )


def test_symbols_tags_option_pullability(tmp_path: Path) -> None:
    """Each report symbol carries an ``option_pullable`` flag so /reports can badge
    F&O vs spot-only (DEC-OPTIONS-ELIGIBLE-GUARD-001): an F&O-eligible underlying →
    True; a spot-only one → False. Reuses the single-source predicate the
    /universe explorer uses."""
    writer = ParquetWriter(lake_root=tmp_path)
    for sym in ("COALINDIA", "SPOTONLY"):
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol=sym,
            interval="1day",
            bars=[
                _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="455", close="458.15"),
                _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="429", close="431.55"),
            ],
        )
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    # COALINDIA is F&O-eligible (it has a CE leg); SPOTONLY is not seeded at all.
    store.upsert_many(
        [_eq("COALINDIA"), _ce("COALINDIA")],
        snapshot_date=date.today(),
        run_id="seed",
        csv_source_sha256="0" * 64,
    )
    client = TestClient(build_app(DashboardState(), instrument_store=store))
    resp = client.get("/api/v1/reports/preopen/symbols")
    assert resp.status_code == 200
    by_sym = {s["symbol"]: s["option_pullable"] for s in resp.json()["symbols"]}
    assert by_sym["COALINDIA"] is True  # F&O-eligible → has an option chain
    assert by_sym["SPOTONLY"] is False  # absent from the universe → spot-only


def _counting_resolver(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy on the route's module-global resolver (the offload-test seam).

    Wraps the REAL ``resolve_report_datasets`` so payloads stay genuine while
    the returned list records each computed ``basis`` in call order — its
    length is the compute count (a cache hit never reaches the resolver).
    """
    from brutex.dashboard.preopen_report import resolve_report_datasets as real_resolve

    bases: list[str] = []

    def _counting(*args: Any, **kwargs: Any) -> Any:
        bases.append(str(kwargs.get("basis", "day")))
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr("brutex.dashboard.app.resolve_report_datasets", _counting)
    return bases


def test_symbols_discovery_cached_across_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DEC-O1-CLAIMS-HONESTY-001 (PART 1a): a repeat poll within one write
    # generation is an O(1) cache handoff (ONE resolver call), and a pull
    # (generation bump via mark_dirty) forces exactly one re-discovery.
    from brutex.dashboard.summary_cache import SummaryCache

    bases = _counting_resolver(monkeypatch)
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="COALINDIA",
        interval="1day",
        bars=[_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="455", close="458.15")],
    )
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    summary_cache = SummaryCache()
    client = TestClient(
        build_app(DashboardState(), instrument_store=store, summary_cache=summary_cache)
    )
    first = client.get("/api/v1/reports/preopen/symbols")
    second = client.get("/api/v1/reports/preopen/symbols")
    assert first.status_code == second.status_code == 200
    assert len(bases) == 1  # the second poll was a generation-memo hit
    assert first.json() == second.json()
    summary_cache.mark_dirty()  # a pull bumps the write generation
    third = client.get("/api/v1/reports/preopen/symbols")
    assert third.status_code == 200
    assert len(bases) == 2  # bumped generation → exactly one re-discovery
    assert third.json() == first.json()  # same lake → same payload, fresh


def test_symbols_cache_keys_per_basis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Each basis holds its OWN memo (the basis is IN the cache key): minute is
    # never served day triples, and a repeat of either basis is a hit.
    bases = _counting_resolver(monkeypatch)
    client = _client(tmp_path)
    day_first = client.get("/api/v1/reports/preopen/symbols?basis=day")
    minute_first = client.get("/api/v1/reports/preopen/symbols?basis=minute")
    assert day_first.status_code == minute_first.status_code == 200
    assert bases == ["day", "minute"]  # each computed with ITS basis
    day_second = client.get("/api/v1/reports/preopen/symbols?basis=day")
    minute_second = client.get("/api/v1/reports/preopen/symbols?basis=minute")
    assert bases == ["day", "minute"]  # both repeats were per-basis memo hits
    assert day_second.json() == day_first.json()
    assert minute_second.json() == minute_first.json()
    assert day_first.json()["basis"] == "day"
    assert minute_first.json()["basis"] == "minute"


def test_symbols_pullability_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the discovery TRIPLES are cached — the pullability tagging reads the
    # instrument master LIVE (a keyless load-instruments moves the master
    # WITHOUT a generation bump — the master_revision trap), so the badge
    # flips while the resolver still shows one call.
    bases = _counting_resolver(monkeypatch)
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="COALINDIA",
        interval="1day",
        bars=[_bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="455", close="458.15")],
    )
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    client = TestClient(build_app(DashboardState(), instrument_store=store))
    first = client.get("/api/v1/reports/preopen/symbols").json()
    assert [s["symbol"] for s in first["symbols"]] == ["COALINDIA"]
    assert first["symbols"][0]["option_pullable"] is False  # empty master
    # A keyless load-instruments: the master moves, the generation does NOT.
    store.upsert_many(
        [_eq("COALINDIA"), _ce("COALINDIA")],
        snapshot_date=date.today(),
        run_id="seed",
        csv_source_sha256="0" * 64,
    )
    second = client.get("/api/v1/reports/preopen/symbols").json()
    assert second["symbols"][0]["option_pullable"] is True  # tagged LIVE
    assert len(bases) == 1  # ...while the cached triples were a hit


def test_xlsx_download_matches_nse(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-01-01&to=2024-12-31")
    assert resp.status_code == 200
    assert "spreadsheetml.sheet" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    # DEC-REPORT-FILENAME-001: the name is self-describing — basis + selection + range.
    assert (
        "preopen_day_all-symbols_2024-01-01_2024-12-31.xlsx" in resp.headers["content-disposition"]
    )
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "COALINDIA" in sheet
    assert "458.15" in sheet  # PREV. CLOSE
    assert "-6.36" in sheet  # %CHNG matches NSE


def test_xlsx_symbol_filter(tmp_path: Path) -> None:
    # A filter that excludes the only symbol → a valid header-only workbook.
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.xlsx?from=2024-01-01&to=2024-12-31&symbols=RELIANCE"
    )
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "COALINDIA" not in sheet


def _write_ampersand_symbol(tmp_path: Path) -> None:
    """Seed the lake with M&M (a real ``&`` underlying) — both report intervals."""
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="M&M",
        interval="1day",
        bars=[
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="2900", close="2950.50"),
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="2960", close="2975"),
        ],
    )
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="M&M",
        interval="1minute",
        bars=[_bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="2960.00", close="2965")],
    )


def test_xlsx_all_symbols_with_ampersand_no_500(tmp_path: Path) -> None:
    # Reproduces the reported crash: an "all symbols" download (no ``?symbols=``)
    # over a lake holding M&M used to 500 with ``ValueError: illegal candle
    # symbol 'M&M'``. It must now stream a valid workbook with M&M included.
    _write_ampersand_symbol(tmp_path)
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-01-01&to=2024-12-31")
    assert resp.status_code == 200  # was 500 before DEC-PREOPEN-AMPERSAND-001
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "M&amp;M" in sheet  # the once-crashing symbol now renders (XML-escaped)
    assert "COALINDIA" in sheet  # and the rest of the report is intact


def test_xlsx_title_carries_requested_range(tmp_path: Path) -> None:
    # The row-1 banner states the requested date range (how a multi-day range
    # report conveys its dates beyond the per-row DATE column).
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-01-01&to=2024-12-31")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Pre-Open Market: 2024-01-01 to 2024-12-31" in sheet


def test_xlsx_bad_date_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=not-a-date&to=2024-12-31")
    assert resp.status_code == 400


def test_xlsx_inverted_range_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-12-31&to=2024-01-01")
    assert resp.status_code == 400


def test_reports_page_renders(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/reports")
    assert resp.status_code == 200
    assert "Pre-Open" in resp.text
    assert "Download .zip" in resp.text  # Part B: the per-year-zip download


def test_reports_page_has_index_constituent_dropdown(tmp_path: Path) -> None:
    # The index quick-select (DEC-REPORTS-INDEX-DROPDOWN-001): a dropdown that
    # adds an index's constituent stocks in one go, wired to the (existing,
    # cache-backed) /universe constituents endpoint — instead of clicking each
    # stock. Guards the control + its endpoint wiring against silent regression.
    html = _client(tmp_path).get("/reports").text
    assert 'id="idxpick"' in html, "the index quick-select dropdown is missing"
    assert "/api/v1/universe/index-constituents?symbol=" in html, (
        "the dropdown must call the constituents endpoint to add an index's stocks"
    )


def test_reports_page_has_category_toggle_and_minpct(tmp_path: Path) -> None:
    # The two-category restructure (DEC-REPORTS-EXCEL-CATEGORIES-001): a mode
    # toggle (All symbols / Top mover), a "Minimum % change" number input, and a
    # Download wired to the new per-year-zip endpoint with mode + min_pct params.
    html = _client(tmp_path).get("/reports").text
    assert 'name="mode"' in html, "the All-symbols / Top-mover toggle is missing"
    assert 'value="all"' in html and 'value="topmover"' in html, "both modes must be selectable"
    assert 'id="minpct"' in html and 'type="number"' in html, "the minimum-% input is missing"
    assert "/api/v1/reports/preopen.zip?" in html, "Download must hit the new .zip endpoint"
    assert "min_pct" in html and "mode" in html, "the mode + min_pct params must be wired"


def _open_zip_member(blob: bytes, member: str) -> str:
    """Read one ``.xlsx`` member out of the report ``.zip`` → its sheet1 XML."""
    with zipfile.ZipFile(io.BytesIO(blob)) as outer:
        inner = outer.read(member)
    with zipfile.ZipFile(io.BytesIO(inner)) as wb:
        return wb.read("xl/worksheets/sheet1.xml").decode("utf-8")


def test_zip_all_mode_download(tmp_path: Path) -> None:
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=all"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    # DEC-REPORT-FILENAME-001: the static "preopen_report.zip" is replaced by
    # the self-describing selection name (mode + basis + symbols + range).
    assert (
        "preopen_all_day_all-symbols_2024-01-01_2024-12-31.zip"
        in resp.headers["content-disposition"]
    )
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
    assert names == ["preopen_all_2024.xlsx"]  # one .xlsx for the single year
    assert "COALINDIA" in _open_zip_member(resp.content, "preopen_all_2024.xlsx")


def test_zip_topmover_mode_download(tmp_path: Path) -> None:
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=topmover&min_pct=1"
    )
    assert resp.status_code == 200
    assert _open_zip_member(resp.content, "preopen_topmover_2024.xlsx").count("COALINDIA") == 1


def test_zip_default_mode_is_all(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["preopen_all_2024.xlsx"]


def test_zip_bad_mode_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=bogus"
    )
    assert resp.status_code == 400


def test_zip_negative_min_pct_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=topmover&min_pct=-1"
    )
    assert resp.status_code == 400


def test_zip_minute_basis_download(tmp_path: Path) -> None:
    # DEC-PREOPEN-MINUTE-BASIS-001: ?basis=minute builds the report from the
    # 1-minute lake (09:15 open + prior last-min close) and still returns a valid
    # .zip — never a 500.
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&basis=minute"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


def test_zip_bad_basis_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&basis=bogus"
    )
    assert resp.status_code == 400


def test_symbols_minute_basis(tmp_path: Path) -> None:
    # The picker honours ?basis= and echoes it; COALINDIA has a 1minute partition
    # so it qualifies for the minute basis (DEC-PREOPEN-MINUTE-BASIS-001).
    resp = _client(tmp_path).get("/api/v1/reports/preopen/symbols?basis=minute")
    assert resp.status_code == 200
    body = resp.json()
    assert body["basis"] == "minute"
    assert any(s["symbol"] == "COALINDIA" for s in body["symbols"])


def test_reports_page_has_basis_toggle(tmp_path: Path) -> None:
    # The /reports page exposes the 1-day | minute basis toggle that threads
    # ?basis= into the .zip download (DEC-PREOPEN-MINUTE-BASIS-001).
    html = _client(tmp_path).get("/reports").text
    assert 'name="basis"' in html
    assert 'value="minute"' in html
    assert "currentBasis()" in html  # threaded into the download params


def test_zip_bad_date_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.zip?from=nope&to=2024-12-31")
    assert resp.status_code == 400


def test_zip_inverted_range_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.zip?from=2024-12-31&to=2024-01-01")
    assert resp.status_code == 400


def test_zip_no_lake_root_is_valid_empty_workbook() -> None:
    # A store whose `path` is not a filesystem Path → the route's non-Path
    # fallback builds NO rows and returns a valid (empty) `.zip`, never a 500
    # (DEC-REPORTS-EXCEL-CATEGORIES-001). Mirrors the coverage routes' own
    # no-lake-root guard so the route degrades honestly when unwired.
    from types import SimpleNamespace

    store = SimpleNamespace(path=None)
    client = TestClient(build_app(DashboardState(), instrument_store=store))
    resp = client.get("/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=all")
    assert resp.status_code == 200
    assert "application/zip" in resp.headers["content-type"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # The empty-selection fallback: a single header-only workbook member.
        assert zf.namelist()
