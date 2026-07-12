"""Regression: the ``/universe`` pull-panel beam animates ONLY while running.

Guards **DEC-PULL-BEAM-RUNNING-ONLY-001**. The sweeping ``#pullpanel .beam``
indicator is styled ``animation: beam 1.1s linear infinite``; the panel is kept
``.on`` after a pull so the ✓/✕ result + chunk grid stay visible, so the beam
used to keep sweeping **forever** and read as "still running" long after the
pull finished. The fix hides the beam by default and shows it only while the
panel carries ``.running`` (toggled on at pull start, off the instant the job
settles, on BOTH the per-symbol spot path and the bulk/options path).

These assertions FAIL on the pre-fix template (no ``#pullpanel.running .beam``
gate, beam not ``display:none``, no ``.running`` toggle) and pass after it.
"""

from __future__ import annotations

import re

from brutex.dashboard.app import _render_universe_html


def test_beam_is_hidden_by_default_and_gated_on_running() -> None:
    """The beam's own rule hides it; a ``.running`` rule reveals it."""
    html = _render_universe_html()
    beam_rule = re.search(r"#pullpanel \.beam \{[^}]*\}", html, re.DOTALL)
    assert beam_rule is not None, "the #pullpanel .beam CSS rule is missing"
    assert "display:none" in beam_rule.group(0), (
        "the beam must be display:none by default so it cannot animate after a pull"
    )
    assert re.search(r"#pullpanel\.running \.beam \{[^}]*display:block[^}]*\}", html), (
        "the beam must be shown ONLY under #pullpanel.running"
    )


def test_js_flags_running_at_start_and_clears_it_on_finish() -> None:
    """The (one remaining) pull path sets ``.running`` at start and clears it settled.

    Was ``>= 2`` when the manual pre-build panel (``_matPanelStart`` /
    ``runMaterialize``) was the second path; DEC-PIPELINE-AUTOPREBUILD-001
    removed that surface, so the single pipeline pull path remains.
    """
    html = _render_universe_html()
    added = html.count('"on", "running"') + html.count("'on', 'running'")
    assert added >= 1, "the pipeline pull path must add the 'running' class at pull start"
    cleared = html.count('classList.remove("running")') + html.count("classList.remove('running')")
    assert cleared >= 1, "the 'running' class must be cleared when a pull finishes (finishLive)"


def test_spot_date_pickers_readded_and_tokens_filled() -> None:
    """DEC-UNIVERSE-PULL-DATERANGE-001: the from/to spot date pickers are RE-ADDED.

    DEC-UNIVERSE-PULL-CONSOLIDATE-001 had removed the picker because the route
    ignored it; the route now threads ``from_date`` / ``to_date`` into the SPOT
    history phase, so the picker is restored with real wiring. The inputs carry
    ``id="pipeFrom"`` / ``id="pipeTo"`` and the shared ``_fill_default_dates`` fills
    their ``__BRUTEX_DATE_*__`` tokens per render, so no raw token leaks. The old
    fixed-window label is gone."""
    from brutex.dashboard.app import _render_universe_html

    html = _render_universe_html()
    assert 'id="pipeFrom"' in html  # the spot from-date input is present
    assert 'id="pipeTo"' in html  # the spot to-date input is present
    assert "__BRUTEX_DATE_" not in html  # every placeholder token is filled (none leak)
    assert "window · <b>2020 → prev-day</b> (fixed)" not in html  # the static label is gone


def test_spot_interval_dropdown_is_retired() -> None:
    """DEC-1MINUTE-ONLY-DERIVE-1DAY-001: the granularity dropdown is RETIRED.

    1-minute is the ONLY granularity fetched from Groww (1-day is derived from it), so
    the ``#spotInterval`` select + its ``&spot_interval=`` threading are gone. The
    static "granularity · 1-minute (1-day derived)" chip that replaced the dropdown
    is ALSO gone (DEC-UNIVERSE-UI-POLISH-002 — dead UI noise: no route param ever
    read it, and the hero copy already states the 1-minute-only contract)."""
    html = _render_universe_html()
    assert 'id="spotInterval"' not in html  # the dropdown is removed
    assert "spot_interval=" not in html  # no longer threaded into any query string
    # The chip is gone too; the hero copy still explains the fixed granularity.
    assert "1-minute</b> (1-day derived)" not in html
    assert "1-minute is the <b>only</b> granularity fetched" in html


def test_instrument_rows_render_contract_economics() -> None:
    """The instruments column ships the lot/tick/freeze econ line (DEC-DASHBOARD-ECON-COLUMNS-001)."""
    from brutex.dashboard.app import _render_universe_html

    html = _render_universe_html()
    assert "function econLine(" in html  # the compact lot·tick·freeze helper exists
    assert ".inst .econ" in html  # its CSS rule ships
    assert 'class="econ"' in html  # rendered per instrument row
    # the helper reads the three economics fields the row now carries
    assert "i.lot_size" in html
    assert "i.tick_size" in html
    assert "i.freeze_quantity" in html


def test_universe_wires_per_symbol_completeness_strip() -> None:
    """The /universe page renders the per-symbol completeness strip (DEC-COMPLETENESS-UNIVERSE-001).

    Guards that the page fetches the per-symbol map, renders the explicit first→last date strip
    with the from-2020 flag per dimension, and keeps it live — so the operator can identify,
    right where they pull, whether a symbol has the entire 2020→today set.
    """
    from brutex.dashboard.app import _render_universe_html

    html = _render_universe_html()
    # fetches the per-symbol completeness endpoint + keeps it live.
    assert "/api/v1/universe/completeness" in html
    assert "function loadCompleteness(" in html
    assert "setInterval(loadCompleteness" in html
    # renders the strip per row (the three helpers + the row insertion + the CSS).
    assert "function covStrip(" in html
    assert "function spotSeg(" in html and "function optSeg(" in html
    assert "covStrip(i)" in html
    assert ".inst .cov" in html
    # the honest from-2020 flag drives the green ✓ vs amber ⚠ vs red ✕ states.
    assert "dim.from_2020" in html
    assert "first_date" in html and "last_date" in html


def test_universe_wires_mapping_integrity_panel() -> None:
    """The /universe page renders the green/red mapping-integrity panel
    (DEC-MAPPING-INTEGRITY-RECONCILE-001) — fetches the verdict endpoint, keeps it live,
    and has the panel element the JS targets."""
    from brutex.dashboard.app import _render_universe_html

    html = _render_universe_html()
    assert "/api/v1/universe/mapping-integrity" in html
    assert "function loadMappingIntegrity(" in html
    assert "setInterval(loadMappingIntegrity" in html
    assert 'id="mappanel"' in html and 'id="mapverdict"' in html


def test_stocks_badge_labels_full_universe_with_fno_split() -> None:
    """DEC-LABEL-TRUTH-001: the Stocks badge counts the LIST's population.

    The badge now reads ``stock_count_total`` (the full held universe the Stocks
    tab actually lists) with the F&O subset labelled beside it
    ("Stocks · M F&O", M = stock_count_total − reference_stock_count), and the
    load message mirrors the split — never a lost number, never an
    undifferentiated count presented as if it were the F&O universe.

    HONEST envelope (Golden Rule #10, the #1529/#1530 precedent): these are
    substring-PRESENCE pins on the served template — no headless JS runner.
    FAILS BEFORE: the new copy was absent and the old undifferentiated
    ``summary.stock_count`` badge / ``${body.stock_count} stocks`` message
    copy was present.
    """
    from brutex.dashboard.app import _render_universe_html

    html = _render_universe_html()
    # New badge copy present: total-preferring count + the F&O sub-label + title.
    assert "summary.stock_count_total" in html
    assert 'lbl + " · " + fnoStocks + " F&O"' in html
    assert "reference-only — the badge counts the full held universe" in html
    # New load-message copy present: list population + the F&O subset beside it.
    assert "body.stock_count_total - body.reference_stock_count" in html
    assert "stocks${msgFno}" in html
    # Old misleading copy absent (the undifferentiated badge count + message).
    assert "const counts = {index: summary.index_count, stock: summary.stock_count};" not in html
    assert "${body.stock_count} stocks)." not in html
