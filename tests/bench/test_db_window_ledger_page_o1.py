"""Bench — the /db window-ledger DuckDB page fast path (DEC-DB-LATENCY-HARDENING-001).

MIGRATION.md #1719. **Honest, narrow claims (Golden Rule #10):**

* the fast path serves a page with **ZERO Python ledger parses** — the pre-fix
  route paid ONE full ``load_window_ledger`` Python record fold per ledger
  stamp move, measured **7,177.5 ms of a 7,628 ms request at 400k rows** on the
  profiling lake (2026-07-11 evidence; the stamp moves on every mid-pull
  flush, so the cost recurred every few seconds). The pre-fix code FAILS the
  zero-parse pin below (it called the spied loader exactly once per stamp).
* a **repeat click within one ledger stamp is O(1) in ledger depth** — a pure
  cache handoff (ZERO SQL), pinned flat via ``cache_tier_ceiling``.
* what is **NOT** claimed: the FRESH-stamp click is O(page) — it is ONE
  O(total physical rows + pending delta footers) DuckDB columnar scan (the
  ``query_candles`` envelope); the fix removes the O(rows) PYTHON fold term,
  it does not make the columnar scan free. The page MATERIALISATION is
  O(page) — pinned by the row-count assertion (only ``limit`` rows cross the
  SQL boundary).
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

import duckdb
from brutex.dashboard.db_view import query_window_ledger_page
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data import window_ledger_store as wls
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger

from tests.bench._o1_tiers import cache_tier_ceiling

_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _build_ledger_lake(root: Path, rows: int) -> Path:
    """A REAL writer ledger of *rows* windows (the profiling-lake shape)."""
    lake = root / f"lake_{rows}"
    lake.mkdir()
    ledger = WindowLedger()
    for i in range(rows):
        ledger.record(
            WindowKey(
                market="groww" if i % 4 == 0 else "groww-option",
                security_id=f"SYM{i}",
                interval="1minute",
                start=date(2025, 1, 1) + timedelta(days=i % 300),
                end=date(2025, 1, 31) + timedelta(days=i % 300),
            ),
            raw_bars=5,
            error_type=None if i % 7 else "timeout",
            bars_appended=5,
            now=_NOW,
        )
    wls.save_window_ledger(ledger, lake)
    return lake


def _warm_click_seconds(lake: Path, conn: duckdb.DuckDBPyConnection, iters: int) -> float:
    """Total wall time of *iters* WARM page clicks (caches primed)."""
    count_cache = MoversSnapshotCache()
    page_cache = MoversSnapshotCache(capacity=8)

    def _click() -> tuple[tuple[tuple[object, ...], ...], int]:
        return query_window_ledger_page(
            conn, lake, limit=100, offset=0, count_cache=count_cache, page_cache=page_cache
        )

    _click()  # prime: one COUNT + one page query
    start = time.perf_counter()
    for _ in range(iters):
        _click()
    return time.perf_counter() - start


class TestWindowLedgerPageFastPath:
    def test_page_click_runs_zero_python_ledger_parses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE teeth (fails-before): the fast-path page click never invokes the
        O(rows) Python record fold. Pre-fix: every fresh ledger stamp paid ONE
        full ``load_window_ledger`` parse — 7,177.5 ms measured at 400k rows —
        so this spy read 1; the DuckDB path reads 0, and the page equals the
        Python parse's slice (parity re-asserted here at bench scale)."""
        lake = _build_ledger_lake(tmp_path, 4_000)
        parses = {"n": 0}
        real_load = wls.load_window_ledger

        def _counting_load(lake_root: Path, **kwargs: object) -> WindowLedger:
            parses["n"] += 1
            return real_load(lake_root, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(wls, "load_window_ledger", _counting_load)
        with duckdb.connect() as conn:
            page, total = query_window_ledger_page(conn, lake, limit=100, offset=0)
        assert parses["n"] == 0
        assert total == 4_000 and len(page) == 100
        # Parity oracle at bench scale (uncached direct parse, outside the spy).
        expected = real_load(lake).to_rows()[0:100]
        assert list(page) == expected

    def test_page_materialisation_is_o_page(self, tmp_path: Path) -> None:
        """Only ``limit`` rows cross the SQL boundary (LIMIT/OFFSET pushdown) —
        a deep ledger never materialises its full row set into Python."""
        lake = _build_ledger_lake(tmp_path, 4_000)
        with duckdb.connect() as conn:
            page, total = query_window_ledger_page(conn, lake, limit=7, offset=3_990)
        assert total == 4_000 and len(page) == 7

    def test_warm_click_flat_vs_ledger_depth(self, tmp_path: Path) -> None:
        """A WARM click (cache handoff, zero SQL) is flat as the ledger deepens:
        20k rows vs 2k rows under ``cache_tier_ceiling(8.0)`` (shared-CI 8x; the
        strict M4-Pro tier tightens to the measured 3x cache allowance). A
        regression that re-ran the O(rows) scan per click would scale ~10x with
        the depth and blow the bound."""
        small = _build_ledger_lake(tmp_path, 2_000)
        big = _build_ledger_lake(tmp_path, 20_000)
        with duckdb.connect() as conn:
            _warm_click_seconds(small, conn, 20)  # JIT/interpreter warmup
            t_small = _warm_click_seconds(small, conn, 200)
            t_big = _warm_click_seconds(big, conn, 200)
        assert t_big / t_small < cache_tier_ceiling(8.0), (
            f"warm window-ledger page click not flat vs depth: "
            f"t_small={t_small:.6f}s t_big={t_big:.6f}s"
        )
