"""Cost gate for the pre-open export rows memo (DEC-DB-LATENCY-HARDENING-001 fix 2).

DEC-O1-GUARANTEE-HARDEN-001 (H4c): the #1719 memo — ``build_preopen_rows``
memoised per (basis, range, symbols) at the write generation — shipped with
unit pins only; its measured numbers (repeat click 206 DuckDB statements /
326-574 ms → 0 statements / 4-18 ms) lived in the ledger, not a gate. This
bench-dir gate pins the cost law DETERMINISTICALLY through the REAL route
(statement + build counting spies — no wall clock, no flake):

* the WARM ``.xlsx`` click within one write generation runs ZERO DuckDB
  ``read_parquet`` statements and ZERO report-row builds (the O(1)-on-repeat
  handoff; only the workbook RENDER re-runs — O(report rows), disclosed);
* the ``.zip`` export shares the SAME rows memo coordinate — its first click
  after a warmed ``.xlsx`` is also zero-statement.

Harness REUSED from ``tests/unit/test_dashboard_preopen_export_cache.py``
(§11 COMMON — the ``test_sweep_worker_scaling`` cross-suite import precedent),
so the lake seed / statement spy exist ONCE.

HONEST envelope (Golden Rule #10): O(1)-on-repeat — the COLD click per
generation pays the real discovery walk + per-symbol DuckDB aggregates, and a
generation bump (a pull) honestly re-pays it (pinned in the unit suite).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the preopen reads need duckdb")
pytest.importorskip("polars", reason="the lake seed writes real Parquet")
# NO openpyxl importorskip (review round 1, MAJOR-B): the export renders via
# the repo's own hand-rolled `preopen_report.render_xlsx` (deliberately zero
# third-party workbook deps) and the reused unit harness imports no openpyxl —
# the former skip was spurious and would have silently disabled this gate on
# the ci.yml bench job, which installs no openpyxl (verified: this file passes
# with openpyxl absent).

from pathlib import Path

from tests.unit.test_dashboard_preopen_export_cache import (
    _XLSX,
    _ZIP,
    _client,
    _count_builds,
)


class TestPreopenRepeatClickIsZeroStatement:
    def test_warm_xlsx_click_runs_zero_statements_and_zero_builds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        builds = _count_builds(monkeypatch)
        client, store, _summary = _client(tmp_path)
        with client:
            cold = client.get(_XLSX)
            assert cold.status_code == 200
            spy = store.conn_spy
            assert spy is not None and spy.read_parquet_stmts > 0  # the honest cold cost
            assert builds["n"] == 1
            cold_stmts = spy.read_parquet_stmts
            for _ in range(3):
                warm = client.get(_XLSX)
                assert warm.status_code == 200
                assert warm.content == cold.content
            assert spy.read_parquet_stmts == cold_stmts, (
                "warm pre-open .xlsx clicks ran DuckDB read_parquet statements — "
                "the #1719 rows memo regressed to a per-click scan"
            )
            assert builds["n"] == 1  # zero further report-row builds

    def test_zip_export_shares_the_rows_memo_zero_statement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        builds = _count_builds(monkeypatch)
        client, store, _summary = _client(tmp_path)
        with client:
            assert client.get(_XLSX).status_code == 200
            spy = store.conn_spy
            assert spy is not None
            cold_stmts = spy.read_parquet_stmts
            assert client.get(_ZIP).status_code == 200  # same memo coordinate
            assert spy.read_parquet_stmts == cold_stmts
            assert builds["n"] == 1
