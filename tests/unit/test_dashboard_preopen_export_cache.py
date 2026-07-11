"""Tests — the pre-open EXPORT rows memo + span-bounded IEP reads (fix 2 of
DEC-DB-LATENCY-HARDENING-001, MIGRATION.md #1719).

Evidence (profiling worker, 2026-07-11): a REPEAT ``.xlsx``/``.zip`` export
click re-paid the IDENTICAL full scan — 206 DuckDB ``read_parquet`` statements
binding 616 files (minute basis = EVERY 1-minute month file of EVERY symbol) +
a 1,442-scandir dataset walk per click, 326–574 ms at 103 symbols, +22 MB RSS
cold — ZERO caching. The routes now memoise the built rows per
``(basis, [from, to], symbols filter)`` at the write generation
(``_preopen_rows_cache``), and the IEP (open) readers bind ONLY the report
span's month files.

Pins (each fails against the pre-fix route):

* repeat-click byte parity — cached vs fresh export bytes identical;
* repeat-click spy — ZERO DuckDB ``read_parquet`` statements AND zero report
  builds on the warm click (pre-fix: identical 206-statement re-scan);
* the ``.zip`` shares the ``.xlsx``'s rows memo (one build per coordinate);
* a write-generation bump (a pull) rebuilds — never a stale export;
* distinct coordinates (basis / range / symbols filter) never collide;
* the pure ``_preopen_rows_key`` law.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, _preopen_rows_key, build_app
from brutex.dashboard.preopen_report import build_preopen_rows as _real_build_preopen_rows
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from fastapi.testclient import TestClient

_XLSX = "/api/v1/reports/preopen.xlsx?from=2024-03-01&to=2024-03-08"
_ZIP = "/api/v1/reports/preopen.zip?from=2024-03-01&to=2024-03-08"


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


def _seed_lake(lake: Path) -> None:
    """Two symbols × (1day + 1minute) across TWO months — real writers."""
    writer = ParquetWriter(lake_root=lake)
    for symbol in ("COALINDIA", "RELIANCE"):
        for interval in ("1day", "1minute"):
            # One write per (year, month) partition — the writer's grouping law.
            writer.write(
                exchange="NSE",
                segment="CASH",
                symbol=symbol,
                interval=interval,
                bars=[_bar(datetime(2024, 2, 28, 3, 45, tzinfo=UTC), open_="455", close="458.15")],
            )
            writer.write(
                exchange="NSE",
                segment="CASH",
                symbol=symbol,
                interval=interval,
                bars=[
                    _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="459", close="460.10"),
                    _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="429", close="431.55"),
                ],
            )


class _ConnSpy:
    """Counts DuckDB ``read_parquet`` statements through the store connection."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.read_parquet_stmts = 0

    def execute(self, sql: str, parameters: Any = None) -> Any:
        if "read_parquet" in sql:
            self.read_parquet_stmts += 1
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _StoreSpy:
    """Wraps a real InstrumentStore, exposing a statement-counting connection."""

    def __init__(self, real: InstrumentStore) -> None:
        self._real = real
        self.conn_spy: _ConnSpy | None = None

    @property
    def path(self) -> Any:
        return self._real.path

    @property
    def connection(self) -> Any:
        if self.conn_spy is None:
            self.conn_spy = _ConnSpy(self._real.connection)
        return self.conn_spy

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _client(tmp_path: Path) -> tuple[TestClient, _StoreSpy, SummaryCache]:
    _seed_lake(tmp_path)
    store = _StoreSpy(InstrumentStore.open(tmp_path / "instruments.duckdb"))
    summary = SummaryCache()
    app = build_app(DashboardState(), instrument_store=store, summary_cache=summary)
    return TestClient(app), store, summary


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Patch the app module's ``build_preopen_rows`` binding with a counting
    delegator (string-form monkeypatch — no ``import brutex.dashboard.app``
    beside the file's ``from ... import``, the CodeQL both-import-styles
    finding) and return the live counter."""
    builds = {"n": 0}

    def _counting_build(*args: Any, **kwargs: Any) -> Any:
        builds["n"] += 1
        return _real_build_preopen_rows(*args, **kwargs)

    monkeypatch.setattr("brutex.dashboard.app.build_preopen_rows", _counting_build)
    return builds


class TestPreopenExportCache:
    def test_repeat_click_serves_identical_bytes_with_zero_scans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE fix-2 teeth (fails-before): the warm export click runs ZERO
        DuckDB ``read_parquet`` statements and ZERO report builds — the pre-fix
        route re-paid the identical full scan per click (206 statements / 616
        files measured) — and the served bytes are byte-identical."""
        client, store, _summary = _client(tmp_path)
        builds = _count_builds(monkeypatch)
        cold = client.get(_XLSX)
        assert cold.status_code == 200 and builds["n"] == 1
        spy = store.conn_spy
        assert spy is not None and spy.read_parquet_stmts > 0
        cold_stmts = spy.read_parquet_stmts
        warm = client.get(_XLSX)
        assert warm.status_code == 200
        assert builds["n"] == 1  # ZERO further builds
        assert spy.read_parquet_stmts == cold_stmts  # ZERO further statements
        assert warm.content == cold.content  # cached-vs-fresh byte parity

    def test_zip_shares_the_xlsx_rows_memo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``.zip`` export keys the SAME coordinate (mode/min_pct shape only
        the render), so it rides the ``.xlsx``'s cached rows — one build total."""
        client, _store, _summary = _client(tmp_path)
        builds = _count_builds(monkeypatch)
        assert client.get(_XLSX).status_code == 200
        assert client.get(_ZIP).status_code == 200
        assert client.get(_ZIP + "&mode=topmover&min_pct=0.1").status_code == 200
        assert builds["n"] == 1  # one build serves all three clicks

    def test_generation_bump_rebuilds_never_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pull (write-generation bump) invalidates the memo — the next click
        rebuilds over the fresh lake, never a stale export (Golden Rule #1)."""
        client, _store, summary = _client(tmp_path)
        builds = _count_builds(monkeypatch)
        assert client.get(_XLSX).status_code == 200 and builds["n"] == 1
        summary.mark_dirty()  # what every pull write does
        assert client.get(_XLSX).status_code == 200
        assert builds["n"] == 2  # fresh build at the new generation

    def test_distinct_coordinates_never_collide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """basis / range / symbols-filter each mint their own memo coordinate."""
        client, _store, _summary = _client(tmp_path)
        builds = _count_builds(monkeypatch)
        assert client.get(_XLSX).status_code == 200
        assert client.get(_XLSX + "&basis=minute").status_code == 200
        assert client.get(_XLSX + "&symbols=COALINDIA").status_code == 200
        assert (
            client.get("/api/v1/reports/preopen.xlsx?from=2024-03-01&to=2024-03-04").status_code
            == 200
        )
        assert builds["n"] == 4  # four coordinates, four builds


class TestPreopenRowsKey:
    def test_key_folds_every_row_shaping_input(self) -> None:
        base = _preopen_rows_key("day", date(2024, 3, 1), date(2024, 3, 8), None)
        assert base == "preopen-rows|day|2024-03-01|2024-03-08|None"
        assert _preopen_rows_key("minute", date(2024, 3, 1), date(2024, 3, 8), None) != base
        assert _preopen_rows_key("day", date(2024, 3, 2), date(2024, 3, 8), None) != base
        assert _preopen_rows_key("day", date(2024, 3, 1), date(2024, 3, 9), None) != base
        assert _preopen_rows_key("day", date(2024, 3, 1), date(2024, 3, 8), ["A"]) != base

    def test_filter_token_is_forgery_safe(self) -> None:
        """A comma/pipe-carrying symbols list can never collide with a
        different split (the tuple-repr law — the window-page precedent)."""
        joined = _preopen_rows_key("day", date(2024, 3, 1), date(2024, 3, 8), ["A|B"])
        split = _preopen_rows_key("day", date(2024, 3, 1), date(2024, 3, 8), ["A", "B"])
        assert joined != split
        assert _preopen_rows_key(
            "day", date(2024, 3, 1), date(2024, 3, 8), []
        ) != _preopen_rows_key("day", date(2024, 3, 1), date(2024, 3, 8), None)
