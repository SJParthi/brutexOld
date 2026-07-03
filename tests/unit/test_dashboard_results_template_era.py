"""Regression: the ``/results`` page carries the honest mixed-era banner + conflict note.

Guards the frontend half of **DEC-RESULTS-SURFACE-HONESTY-001**: when the
leaderboard payload reads ``mixed_snapshot_eras: true`` the page must render a
LOUD banner (results span N data eras — census/heal pending) via
``textContent`` (never innerHTML — the repo XSS law; the era hashes are server
data), and a non-zero ``run_id_conflicts`` must surface in the status note.

Static-template assertions (no JS engine) — the
``test_dashboard_movers_template_xss.py`` convention. They FAIL on the pre-fix
template (no ``#eraBanner`` element, no ``mixed_snapshot_eras`` /
``run_id_conflicts`` consumption) and pass after the fix.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.dashboard.app import _render_results_html


def test_results_ships_the_era_banner_element() -> None:
    """The banner element exists, alert-roled, hidden until a mixed-era payload."""
    html = _render_results_html()
    assert '<div class="erabanner" id="eraBanner" role="alert"></div>' in html


def test_results_banner_driven_by_mixed_snapshot_eras_via_textcontent() -> None:
    """The banner consumes ``mixed_snapshot_eras`` + ``snapshot_hashes`` safely."""
    html = _render_results_html()
    assert "board.mixed_snapshot_eras" in html
    assert "board.snapshot_hashes" in html
    # The message names the era count + the pending census/heal, honestly.
    assert 'results span " + eras.length + " data eras' in html
    assert "census/heal pending" in html
    # textContent only — the server-derived era hashes never reach innerHTML.
    assert "eraBanner.textContent" in html
    assert "eraBanner.innerHTML" not in html


def test_results_status_note_surfaces_run_id_conflicts() -> None:
    """A non-zero ``run_id_conflicts`` is appended to the (textContent) status note."""
    html = _render_results_html()
    assert "board.run_id_conflicts" in html
    assert "run-id conflict(s)" in html
    assert "first-wins, logged" in html
