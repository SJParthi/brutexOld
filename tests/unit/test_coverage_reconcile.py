"""Unit test for the slimmed :mod:`brutex.dashboard.coverage_reconcile`.

DEC-WINDOW-LEDGER-001 (Stage d). The fetch-coverage reconcile band-aids
(``reconcile_windows`` / ``reconcile_coverage_with_lake`` /
``reconcile_empty_terminal`` / ``reconcile_failed_windows_for_repull``) were
RETIRED once the unified :class:`~brutex.data.window_ledger.WindowLedger` became
the sole skip authority — its per-window record + the ledger cold-start
(:func:`~brutex.data.window_ledger_store.reconcile_ledger_with_lake`) replace
every coarse coverage-set patch. The only survivor is :func:`load_lake_spans`
(the read-only lake span reader the ledger cold-start consumes), a thin DuckDB
I/O surface exercised end-to-end by the ledger's integration tests.
"""

from __future__ import annotations

from pathlib import Path

from brutex.dashboard.coverage_reconcile import load_lake_spans


def test_load_lake_spans_returns_a_list(tmp_path: Path) -> None:
    # Smoke: an empty lake (no datasets) yields an empty span list, never raises —
    # whether DuckDB is present (empty read) or absent (ImportError degrade).
    spans = load_lake_spans(tmp_path)
    assert isinstance(spans, list)
