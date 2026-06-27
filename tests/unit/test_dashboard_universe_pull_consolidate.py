"""Guards the consolidated ``/universe`` pull controls (DEC-UNIVERSE-PULL-CONSOLIDATE-001).

The operator's "the pipeline ALWAYS runs the fixed gated order spot → futures →
options, so the per-stage buttons are redundant" — collapse to ONE selection-aware
``▶ Pull · full pipeline`` button, auto-load instruments on an empty master, keep ONE
unified pre-build (which also runs as the pipeline's final stage), and FIX the
misleading "lost the live feed: Failed to fetch" header.

The dashboard JS has no unit harness, so these substring guards lock the markup +
wiring in CI (mirroring ``test_dashboard_universe_ux.py``). The pure pipeline /
header / prebuild LOGIC is unit-tested in ``test_pipeline_pull.py``; the route's
prebuild binding is exercised end-to-end in ``test_dashboard_pull_pipeline_route.py``.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own ``_render_universe_html``.)

from __future__ import annotations

from brutex.dashboard.app import _render_universe_html


def test_only_one_pull_button_remains() -> None:
    """Exactly ONE pull button — the selection-aware full pipeline — survives.

    The redundant per-stage buttons (Pull spot / Pull expiries + contracts / Pull
    historical / Load instruments), the bulk ①②③④ matrix, and the separate "Pull
    selected" button are all GONE; only ``pullPipelineBtn`` remains.
    """
    html = _render_universe_html()
    assert 'id="pullPipelineBtn"' in html, "the single pull button must remain"
    assert "Pull · full pipeline" in html, "the consolidated label must be present"
    # Every removed button id is absent (a null getElementById().onclick can't throw).
    for gone in (
        'id="pullbtn"',
        'id="pullDerivBtn"',
        'id="pullHistBtn"',
        'id="pullSelPipelineBtn"',
        'id="loadbtn"',
        'id="loadbtn-empty"',
        'id="bulkIdxSpot"',
        'id="bulkStkSpot"',
        'id="bulkIdxDeriv"',
        'id="bulkStkDeriv"',
    ):
        assert gone not in html, f"the redundant button {gone} must be removed"


def test_kept_controls_are_present() -> None:
    """The selection helpers + granularity + unified pre-build stay."""
    html = _render_universe_html()
    for needle in (
        'id="selallbtn"',  # ✓ all shown
        'id="clrselbtn"',  # ✗ clear
        'id="tickconstbtn"',  # ⊕ tick its constituents
        "granularity · <b>1-minute</b> (1-day derived)",  # granularity display
        'id="matbtn"',  # the ONE unified pre-build button
        "⚙ Pre-build timeframes",
    ):
        assert needle in html, f"kept control missing: {needle}"


def test_dead_date_range_picker_is_removed() -> None:
    """The from/to date-range picker is GONE (FIX-FIRST review follow-up).

    The consolidated ``▶ Pull · full pipeline`` button posts the gated route, which
    accepts NO from/to and ALWAYS pulls the fixed default window — so an inert picker
    the button silently ignored was dead/misleading UI implying a scoping it no longer
    provides. It is removed in favour of a static "window · 2020 → prev-day (fixed)"
    label; a pull is scoped by TICKING ROWS, not by date (Golden Rule #10 honesty).
    """
    html = _render_universe_html()
    # The native date inputs + their range container are GONE from the page.
    assert 'id="fromDate"' not in html, "the dead from-date input must be removed"
    assert 'id="toDate"' not in html, "the dead to-date input must be removed"
    assert 'data-bxdp="range"' not in html, "the date-range component container must be removed"
    # The placeholder tokens the (now-removed) picker carried are gone with it.
    assert "__BRUTEX_DATE_MAX__" not in html, "no unfilled date token may leak"
    # The honest static window label replaces the picker.
    assert "window · <b>2020 → prev-day</b> (fixed)" in html, (
        "the static fixed-window label must replace the removed picker"
    )


def test_pull_button_is_selection_aware() -> None:
    """The ONE button pulls the ticks, or the whole universe when none are ticked."""
    html = _render_universe_html()
    assert "function pipelineSelection" in html, "the selection-scope helper is missing"
    # Ticked rows → ?symbols= (scoped); no ticks → the bare route (whole universe).
    assert '"/api/v1/universe/pull-pipeline?symbols=" + encodeURIComponent(ids)' in html, (
        "the ticked branch must post the scoped pipeline route"
    )
    assert '"/api/v1/universe/pull-pipeline",' in html, (
        "the no-selection branch must post the bare (whole-universe) pipeline route"
    )


def test_auto_load_fires_only_on_empty_master() -> None:
    """Instruments auto-load on page open, but ONLY when the master is empty.

    ``maybeAutoLoad`` is one-shot (``autoLoadAttempted``), yields to an in-flight
    load (``loadInFlight``), and short-circuits when the master is already populated
    (``if (total > 0) return;``) — so a populated master is NEVER re-loaded and the
    operator never needs a manual "Load instruments" click.
    """
    html = _render_universe_html()
    assert "function maybeAutoLoad" in html, "the auto-load helper is missing"
    assert "autoLoadAttempted" in html, "the one-shot guard is missing"
    assert "if (total > 0) return;" in html, (
        "auto-load must short-circuit on a populated master (never double-load)"
    )
    # The auto-load is wired into bootstrap() and calls the shared loader.
    assert "await maybeAutoLoad(" in html, "auto-load must be invoked from bootstrap"
    assert "loadInstrumentMaster" in html, "the shared loader must remain for auto-load"
    # The manual toolbar "Load instruments" button + its onclick are gone.
    assert 'document.getElementById("loadbtn").onclick' not in html, (
        "the manual Load instruments onclick must be removed"
    )


def test_pull_progress_header_reflects_real_terminal_state() -> None:
    """The misleading "lost the live feed: Failed to fetch" header is REPLACED.

    The header is built by ``terminalPullHeader`` from the REAL settled counts:
    a clean run (completed === total AND failed === 0 AND gaps === 0) shows a green
    ✓ complete; otherwise an amber ⚠ partial with the failed / gap / shortfall
    counts; an error state shows the real detail. The hardcoded leftover strings
    must be GONE from the page.
    """
    html = _render_universe_html()
    # The honest header builder + its three arms.
    assert "function terminalPullHeader" in html, "the honest header builder is missing"
    assert "failed === 0 && gaps === 0 && completed === total" in html, (
        "the clean-success predicate must be the real counts"
    )
    assert "} complete — " in html, "the success header arm is missing"
    assert "} partial — " in html, "the honest partial header arm is missing"
    # The misleading hardcoded strings are gone from the rendered page.
    assert "lost the live feed" not in html, (
        "the stale live-feed header must be removed (DEC-LIVE-REMOVE-001 leftover)"
    )
    assert "Failed to fetch" not in html, "no hardcoded 'Failed to fetch' header may remain"


def test_zero_unit_settle_is_neutral_not_a_green_success() -> None:
    """A degenerate zero-unit settle is NEUTRAL, never a green ✓ success tick.

    The clean predicate ``completed === total`` is trivially true at ``0 === 0``, so
    without a guard a pull that fetched nothing read "✓ complete — 0 units". A
    started pull always has ``total > 0`` (``resolve_pull_targets`` fails-closed on an
    empty selection), but an idle / planned-nothing snapshot can settle here — the
    header must show the honest "nothing to pull" note for it (FIX-FIRST hardening).
    """
    html = _render_universe_html()
    assert "total === 0 && completed === 0" in html, (
        "the zero-unit neutral guard must short-circuit before the clean-success arm"
    )
    assert "} — nothing to pull" in html, "the neutral zero-unit header arm is missing"


def test_transient_poll_blip_is_retried_not_a_failure() -> None:
    """A transient poll-fetch blip is retried, not surfaced as a pull failure."""
    html = _render_universe_html()
    assert "POLL_MAX_FEED_RETRIES" in html, "the bounded retry budget is missing"
    assert "pollFeedRetries" in html, "the retry counter is missing"
    # When the budget is exhausted, an HONEST "feed dropped" note (not "Failed to
    # fetch"), and the operator is pointed at the verdict refresh.
    assert "live progress feed dropped" in html, "the honest feed-dropped note is missing"


def test_pipeline_button_runs_final_prebuild_stage() -> None:
    """The page advertises the pipeline's FINAL pre-build stage in the chain copy."""
    html = _render_universe_html()
    # The hero sub-line names the full gated chain INCLUDING pre-build as the final stage.
    assert "historical → pre-build" in html, (
        "the chain copy must name pre-build as the pipeline's final stage"
    )
    # The ⚙ button is the on-demand re-warm (the automatic pre-build runs in the pipeline).
    assert "on-demand re-warm" in html, "the pre-build button must be framed as a re-warm"
