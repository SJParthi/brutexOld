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
from datetime import datetime
from zoneinfo import ZoneInfo

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
    """Both pull paths set ``.running`` at start and clear it when settled."""
    html = _render_universe_html()
    added = html.count('"on", "running"') + html.count("'on', 'running'")
    assert added >= 2, (
        "both the per-symbol spot path and the bulk/options path must add the "
        "'running' class at pull start"
    )
    cleared = html.count('classList.remove("running")') + html.count("classList.remove('running')")
    assert cleared >= 2, (
        "the 'running' class must be cleared when a pull finishes (runPull finally + finishLive)"
    )


def test_date_pickers_prefilled_with_canonical_default() -> None:
    """DEC-DEFAULT-DATE-RANGE-001: the from/to pickers are PRE-FILLED with the
    canonical [2020-01-01, previous day] default, and no token leaks through."""
    from brutex.dashboard.app import _render_universe_html
    from brutex.dashboard.pull_controller import default_date_range

    html = _render_universe_html()
    from_iso, to_iso = default_date_range()
    max_iso = datetime.now(tz=ZoneInfo("Asia/Kolkata")).date().isoformat()
    assert from_iso == "2020-01-01"
    assert f'id="fromDate" min="2020-01-01" max="{max_iso}" value="{from_iso}"' in html
    assert f'id="toDate" min="2020-01-01" max="{max_iso}" value="{to_iso}"' in html
    assert "__BRUTEX_DATE_" not in html  # the placeholder tokens were all substituted


def test_spot_interval_dropdown_is_retired() -> None:
    """DEC-1MINUTE-ONLY-DERIVE-1DAY-001: the granularity dropdown is RETIRED.

    1-minute is the ONLY granularity fetched from Groww (1-day is derived from it), so the
    ``#spotInterval`` select + its ``&spot_interval=`` threading are gone — the pull bar
    shows a static "1-minute (1-day derived)" label instead."""
    html = _render_universe_html()
    assert 'id="spotInterval"' not in html  # the dropdown is removed
    assert "spot_interval=" not in html  # no longer threaded into any query string
    # The static label tells the operator the fixed granularity.
    assert "1-minute</b> (1-day derived)" in html


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
