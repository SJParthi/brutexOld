"""Guards the "every /db tab is O(page)" pagination wiring (DEC-DB-VIEWER-PAGINATE-ALL-001).

The candle / option / F&O / DuckDB-table tabs were already SQL ``LIMIT``/``OFFSET``
bounded; this decision paginated the last two whole-table reads — the F&O **registry**
(expiries + contracts) and the **window-ledger** sidecar — so NO /db tab loads every
row any more. The dashboard JS has no unit harness, so (following the established
``test_dashboard_*_ux.py`` pattern) these substring guards lock the frontend wiring;
the backend halves are proven in ``tests/unit/test_registry_store.py`` +
``tests/unit/test_db_view_registry.py`` + ``tests/integration/test_dashboard_coverage.py``.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own _render_db_html.)

from __future__ import annotations

from brutex.dashboard.app import _render_db_html


def test_registry_tab_is_paginated() -> None:
    """The registry tabs fetch ONE limit/offset page + page in place (grid-only)."""
    html = _render_db_html()
    assert "function runRegistryQuery(" in html, "the paged registry query path is missing"
    assert "function updateRegistryGrid(" in html, "the grid-only registry update is missing"
    # the URL carries limit + offset (SQL-bounded page, not all rows).
    assert (
        '"?limit=" + encodeURIComponent(limitSel.value) + "&offset=" + encodeURIComponent(rstate.offset)'
        in html
    ), "the registry fetch must pass limit + offset"
    # prev/next re-fetch the next page (grid-only refresh), state carries the offset.
    assert "const rstate = { kind: null, offset: 0 };" in html, "rstate must carry the page offset"
    assert "runRegistryQuery(true)" in html, "registry paging must update only the grid"


def test_window_ledger_tab_is_paginated() -> None:
    """The window-ledger sidecar fetches one limit/offset page + pages in place."""
    html = _render_db_html()
    assert "function runLedgerQuery(" in html, "the paged ledger query path is missing"
    assert "function updateLedgerGrid(" in html, "the grid-only ledger update is missing"
    assert "const lstate = { offset: 0 };" in html, "lstate must carry the ledger page offset"
    assert '"/api/v1/db/window-ledger?limit=" + encodeURIComponent(limitSel.value)' in html, (
        "the ledger fetch must pass limit + offset"
    )
    assert "runLedgerQuery(true)" in html, "ledger paging must update only the grid"


def test_limit_dropdown_and_refresh_drive_both_paged_tabs() -> None:
    """Changing the row-limit / Refresh re-pages the registry AND the ledger."""
    html = _render_db_html()
    assert 'else if (mode === "registry") { rstate.offset = 0; runRegistryQuery(true); }' in html
    assert 'else if (mode === "ledger") { lstate.offset = 0; runLedgerQuery(true); }' in html
    assert 'else if (mode === "ledger") selectLedger("window_ledger");' in html, (
        "Refresh must re-load the ledger tab"
    )
