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
    """The selection helpers + granularity stay (the manual pre-build button is gone).

    DEC-PIPELINE-AUTOPREBUILD-001 removed the ``matbtn`` manual pre-build — the
    pipeline's automatic prebuild stage replaces it, so it is pinned ABSENT here
    (the removed-surface pins live in test_dashboard_universe_ux.py).
    """
    html = _render_universe_html()
    for needle in (
        'id="selallbtn"',  # ✓ all shown
        'id="clrselbtn"',  # ✗ clear
        'id="tickconstbtn"',  # ⊕ tick its constituents
        "granularity · <b>1-minute</b> (1-day derived)",  # granularity display
    ):
        assert needle in html, f"kept control missing: {needle}"
    assert 'id="matbtn"' not in html, "the manual pre-build button must stay removed"


def test_spot_date_range_picker_is_readded_and_wired() -> None:
    """The from/to date-range picker is RE-ADDED + properly wired (DEC-UNIVERSE-PULL-DATERANGE-001).

    DEC-UNIVERSE-PULL-CONSOLIDATE-001 had removed the picker BECAUSE the route ignored
    it (an inert/misleading control). The route now accepts ``from_date`` / ``to_date``
    and threads them into the SPOT history phase, so the picker is re-added with a real
    wiring — resolving that honesty concern. The two native date inputs carry the
    pre-fill + ceiling tokens (``_fill_default_dates`` fills them per render), and the
    onclick appends ``&from_date=`` / ``&to_date=`` ONLY when non-blank.
    """
    html = _render_universe_html()
    # The two spot date inputs + their range container are present.
    assert 'id="pipeFrom"' in html, "the spot from-date input must be present"
    assert 'id="pipeTo"' in html, "the spot to-date input must be present"
    assert 'aria-label="Spot history date range"' in html, "the daterange group must be present"
    # The pre-fill / ceiling tokens are SUBSTITUTED (no raw token leaks).
    assert "__BRUTEX_DATE_FROM__" not in html, "no unfilled from-date token may leak"
    assert "__BRUTEX_DATE_TO__" not in html, "no unfilled to-date token may leak"
    assert "__BRUTEX_DATE_MAX__" not in html, "no unfilled max token may leak"
    # The onclick appends the picked dates ONLY when non-blank (default-preservation).
    assert "function pipelineDateQuery" in html, "the date-query helper is missing"
    assert '"&from_date=" + encodeURIComponent(fromEl.value)' in html, (
        "from_date is appended only when the input is non-blank"
    )
    assert '"&to_date=" + encodeURIComponent(toEl.value)' in html, (
        "to_date is appended only when the input is non-blank"
    )


def test_pull_button_is_selection_aware() -> None:
    """The ONE button pulls the ticks, or the whole universe when none are ticked."""
    html = _render_universe_html()
    assert "function pipelineSelection" in html, "the selection-scope helper is missing"
    # Ticked rows → ?symbols= (scoped, + optional date query); no ticks → the bare
    # route (whole universe) with the date query folded into the URL when present.
    assert '"/api/v1/universe/pull-pipeline?symbols=" + encodeURIComponent(ids) + dq' in html, (
        "the ticked branch must post the scoped pipeline route with the date query"
    )
    assert '"/api/v1/universe/pull-pipeline"' in html, (
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
    """The page advertises the pipeline's FINAL pre-build stage in the chain copy.

    DEC-PIPELINE-AUTOPREBUILD-001: the pre-build is AUTOMATIC-only now, so the
    old "on-demand re-warm" framing (the removed manual button) must be gone.
    """
    html = _render_universe_html()
    # The hero sub-line names the full gated chain INCLUDING pre-build as the final stage.
    assert "historical → pre-build" in html, (
        "the chain copy must name pre-build as the pipeline's final stage"
    )
    assert "on-demand re-warm" not in html, (
        "the manual re-warm framing must be gone — the prebuild is automatic-only"
    )
