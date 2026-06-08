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
    canonical [2021-01-01, previous day] default, and no token leaks through."""
    from brutex.dashboard.app import _render_universe_html
    from brutex.dashboard.pull_controller import default_date_range

    html = _render_universe_html()
    from_iso, to_iso = default_date_range()
    assert from_iso == "2021-01-01"
    assert f'id="fromDate" min="2021-01-01" value="{from_iso}"' in html
    assert f'id="toDate" min="2021-01-01" value="{to_iso}"' in html
    assert "__BRUTEX_DATE_" not in html  # the placeholder tokens were all substituted
