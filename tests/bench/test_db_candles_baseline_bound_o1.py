"""Bench — the /db candles %CHNG-baseline file bound (fix 3 of
DEC-DB-LATENCY-HARDENING-001, MIGRATION.md #1719).

**Honest, narrow claim (Golden Rule #10):** this bench proves ONLY that a
``query_candles`` page click's DuckDB statements bind **O(page-span) month
files** — the #863 timestamp bounds already pruned ROW GROUPS, but the two
prev-close baseline statements still bound the whole ``/**/*.parquet`` glob,
so DuckDB opened EVERY stored month's Parquet FOOTER twice per click. The
profiling evidence (2026-07-11) measured both baseline statements binding the
full 72-file glob (7.4 + 9.5 ms each) while the manifest-served page read
bound 1 file (4.5 ms) — the pre-fix code FAILS the file-touch pin below (it
touches every stored month; the fixed click touches a small constant).

NOT claimed: O(1) total — the candidate-file ENUMERATION is an O(existing
files) directory listing per click (the same cost the readers' presence guard
already paid), and the manifest revalidation sweep's O(D) stat term
(DEC-LAKE-STATS-MANIFEST-001) stands unchanged. The accounting is the repo's
own deterministic bound-file counting (``test_db_paging_o_page.py``'s
``_FileProbeConn``), noise-free — no wall-clock flake surface.
"""

from __future__ import annotations

import glob as globmod
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

import duckdb
from brutex.dashboard.db_view import query_candles
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

_MONTHS = 12
_BARS_PER_DAY = 20


def _bar(ts: datetime) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


def _build_lake(root: Path) -> None:
    writer = ParquetWriter(lake_root=root)
    for m in range(1, _MONTHS + 1):
        bars = [
            _bar(datetime(2024, m, d, 4, i, tzinfo=UTC))
            for d in (2, 15, 27)
            for i in range(_BARS_PER_DAY)
        ]
        writer.write(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )


class _FileProbeConn:
    """Records the CONCRETE parquet files every ``read_parquet`` binding touches
    (the ``test_db_paging_o_page.py`` accounting: a bound glob is expanded — DuckDB
    opens every matched file's footer — a bound list is its own file set)."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.touched: set[str] = set()

    def reset(self) -> None:
        self.touched = set()

    def execute(self, sql: str, parameters: Any = None) -> Any:
        if parameters is not None and "read_parquet" in sql:
            src = parameters[0]
            if isinstance(src, list):
                self.touched.update(str(p) for p in src)
            else:
                self.touched.update(globmod.glob(str(src), recursive=True))
        if parameters is None:
            return self._real.execute(sql)
        return self._real.execute(sql, parameters)


class TestCandlesBaselineFileBound:
    def test_page_click_touches_o_span_files_not_o_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE fix-3 teeth (fails-before): a warm page click's statements touch a
        page-span-bounded file set, NOT every stored month. Pre-fix the two
        baseline statements each expanded the full glob → the click touched all
        12 stored files (72 in the profiling evidence); fixed, the click
        touches the page's own month ± the pad + the predecessor candidates.
        Manifest OFF so the PAGE read's own whole-glob legacy arm is out of the
        accounting and the pin isolates the baseline statements' bound."""
        monkeypatch.setenv("BRUTEX_LAKE_STATS_MANIFEST", "off")
        _build_lake(tmp_path)
        conn = _FileProbeConn(duckdb.connect())
        # desc page 1 = the newest (Dec-2024) bars — page dates live in ONE month.
        payload = query_candles(
            conn,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            descending=True,
            limit=20,
        )
        assert payload["rows"]  # a real served page (structural non-vacuity)
        total = _MONTHS  # 12 stored month files
        stored = len(list((tmp_path / "bars").rglob("*.parquet")))
        assert stored == total
        # The legacy COUNT + page SELECT bind the whole glob (manifest OFF —
        # their bound is #1614's territory, not this fix's), so subtract that
        # known whole-glob term by accounting the BASELINE statements alone:
        conn.reset()
        from datetime import date

        from brutex.dashboard.db_view import _intraday_prev_close_by_page_date

        baseline = _intraday_prev_close_by_page_date(
            conn,
            tmp_path,
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            page_dates={date(2024, 12, 2), date(2024, 12, 15), date(2024, 12, 27)},
        )
        assert baseline  # served
        touched = len(conn.touched)
        assert touched <= 4, (
            f"the %CHNG baseline touched {touched} files for a one-month page "
            f"on a {total}-month lake — expected O(page-span) (pre-fix: {total})"
        )
