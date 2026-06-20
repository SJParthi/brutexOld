"""Guards the /db + /universe per-interaction latency fixes (DEC-DB-UNIVERSE-O1-LATENCY-001).

Both pages lagged on every interaction. The dashboard JS has no unit harness, so —
following the established ``test_dashboard_universe_ux.py`` / ``test_dashboard_db_ux.py``
pattern — these substring guards lock the markup + wiring in CI. The *backend* half
of the decision (page-bounded candle baseline) is proven byte-for-byte +
flat-latency in ``tests/unit/test_dashboard_db_query.py`` +
``tests/bench/test_db_candle_query_o1.py``; these guards pin the *frontend* half:

/db — paging / sort / apply / limit within the same dataset update ONLY the grid
(#dbgrid), leaving the control bar + its date pickers in place (so the datepicker
MutationObserver no longer re-scans the document + rebuilds the calendars on every
click), and the grid is dimmed in place instead of flashed to "loading…".

/universe — each tab's instrument set is fetched once + cached (no repeat
3-self-join server round-trip on a tab flip), the search box is debounced, and the
15s completeness poll patches only the per-row .cov strip instead of rebuilding the
~750-row list.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own _render_db_html / _render_universe_html.)

from __future__ import annotations

from brutex.dashboard.app import _render_db_html, _render_universe_html

# ── /db: grid-only updates, controls stay put ────────────────────────────────


def test_db_candle_paging_updates_grid_only_not_controls() -> None:
    """Paging / sort / apply route through updateCandleGrid (grid only), not a full render."""
    html = _render_db_html()
    assert "function updateCandleGrid(" in html, "the grid-only candle update path is missing"
    # the grid lives in its own #dbgrid container so the control bar can stay put.
    assert "'<div id=\"dbgrid\">'" in html, "the candle grid must be wrapped in #dbgrid"
    # prev/next + apply + limit-change ask for a grid-only refresh (runCandleQuery(true)).
    assert "runCandleQuery(true)" in html, "paging/apply must update only the grid"
    # a dataset switch (column set may change) still does the full render.
    assert "runCandleQuery(false)" in html, "a dataset switch must do the full render"


def test_db_table_paging_updates_grid_only_not_controls() -> None:
    """The structured-table browser shares the same grid-only paging split."""
    html = _render_db_html()
    assert "function updateTableGrid(" in html, "the grid-only table update path is missing"
    assert "runTableQuery(name, true)" in html, "table paging/apply must update only the grid"


def test_db_grid_dims_in_place_instead_of_flashing_to_loading() -> None:
    """A fetch dims #dbgrid in place rather than wiping the panel to a 'loading…' string."""
    html = _render_db_html()
    assert "#dbgrid.is-loading" in html, "the in-place dim style is missing"
    assert 'classList.add("is-loading")' in html, (
        "the grid must be dimmed (not wiped) during a fetch"
    )


# ── /universe: per-tab cache, debounced search, in-place completeness ─────────


def test_universe_instruments_cached_per_tab() -> None:
    """A tab the operator already visited renders from cache — no repeat server round-trip."""
    html = _render_universe_html()
    assert "const UNIV_CACHE" in html, "the per-tab instrument cache is missing"
    # a cache hit short-circuits the fetch and re-renders from memory.
    assert "if (!forceReload && UNIV_CACHE[kind])" in html, "loadInstruments must consult the cache"
    # a master reload busts the cache so the new set is refetched.
    assert "Object.keys(UNIV_CACHE).forEach(k => delete UNIV_CACHE[k])" in html, (
        "a master reload must invalidate the per-tab cache"
    )
    assert "loadInstruments(true)" in html, "a master reload must force a refetch"
    # an in-flight pre-reload fetch must abort instead of re-pinning stale data
    # (the reload bumps the epoch; the fetch captured the old one) — MINOR-1.
    assert "const epoch = reloadEpoch;" in html, "loadInstruments must capture the reload epoch"
    assert "if (epoch !== reloadEpoch) return;" in html, (
        "a fetch that predates a master reload must abort, not re-pin stale data"
    )


def test_universe_search_is_debounced() -> None:
    """The search box coalesces keystrokes instead of rebuilding the list per character."""
    html = _render_universe_html()
    assert "searchTimer" in html, "the search debounce handle is missing"
    assert "setTimeout(renderInstruments, 140)" in html, "the search must be debounced (~140ms)"


def test_universe_completeness_poll_patches_cov_in_place() -> None:
    """The 15s completeness poll updates only the .cov strips, not the whole list."""
    html = _render_universe_html()
    assert "function refreshCov(" in html, "the in-place completeness patch is missing"
    # the poll calls refreshCov, NOT a full renderInstruments rebuild.
    assert "refreshCov();   // patch ONLY the per-row .cov strips" in html, (
        "loadCompleteness must patch in place, not rebuild"
    )
    assert "cov.outerHTML = covStrip(i)" in html, "refreshCov must replace only the .cov node"
