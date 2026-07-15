"""Guards the consolidated ``/universe`` pull controls (DEC-UNIVERSE-PULL-CONSOLIDATE-001,
amended by the DEC-PULL-SCOPED-ALL-ONECLICK-001 round-6 TWO-BUTTON split).

The one-button law becomes the TWO-BUTTON law at the UI level: the combined
``▶ Pull · full pipeline`` button is REMOVED and the operator surface is
exactly ``▶ Pull Spot`` (scope=spot-only, whole-universe, auto-arming) +
``▶ Pull F&O`` (scope=fno-only, selection/date-aware, manual only) — while the
``/pull-pipeline`` route keeps ``scope=full`` as its API default
(byte-identical; nightly + old intents + external callers). The per-stage
buttons stay collapsed, instruments auto-load on an empty master, the ONE
unified pre-build runs as the pipeline's final stage, and the misleading
"lost the live feed: Failed to fetch" header stays fixed.

The dashboard JS has no unit harness, so these substring guards lock the markup +
wiring in CI (mirroring ``test_dashboard_universe_ux.py``). The pure pipeline /
header / prebuild LOGIC is unit-tested in ``test_pipeline_pull.py``; the route's
prebuild binding is exercised end-to-end in ``test_dashboard_pull_pipeline_route.py``.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own ``_render_universe_html``.)

from __future__ import annotations

from brutex.dashboard.app import _render_universe_html


def test_exactly_two_pull_buttons_remain() -> None:
    """The TWO-BUTTON law — ▶ Pull Spot + ▶ Pull F&O, nothing else.

    CONSCIOUS RE-PIN (DEC-PULL-SCOPED-ALL-ONECLICK-001 round-6 two-button
    split, superseding the one-button law of DEC-UNIVERSE-PULL-CONSOLIDATE-001
    at the UI level only): the combined ``▶ Pull · full pipeline`` button is
    REMOVED — its id + label join the removed-button negative pins — and the
    two remaining buttons both ride the SAME ``/pull-pipeline`` launcher with
    additive scope tokens (every rail inherited; the route's scope=full
    default stays API-reachable, pinned in
    test_dashboard_pull_pipeline_scope.py). The redundant per-stage buttons
    (Pull spot / Pull expiries + contracts / Pull historical / Load
    instruments), the bulk ①②③④ matrix, and the separate "Pull selected"
    button stay GONE.
    """
    html = _render_universe_html()
    assert 'id="pullAllSpotBtn"' in html, "the ▶ Pull Spot button must be present"
    assert 'id="pullFnoBtn"' in html, "the ▶ Pull F&O button must be present"
    assert ">Pull Spot</b>" in html, "the ▶ Pull Spot label is missing"
    assert ">Pull F&amp;O</b>" in html, "the ▶ Pull F&O label is missing"
    # Every removed button id is absent (a null getElementById().onclick can't throw).
    for gone in (
        'id="pullPipelineBtn"',  # the round-6 removal — the combined button
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
    assert "Pull · full pipeline" not in html, (
        "the combined-button label must be gone (the round-6 two-button split)"
    )
    assert "scope=full" not in html, (
        "the UI must never send scope=full explicitly — the full chain is API-only now"
    )


def test_kept_controls_are_present() -> None:
    """The selection helpers stay (the manual pre-build + granularity chips are gone).

    DEC-PIPELINE-AUTOPREBUILD-001 removed the ``matbtn`` manual pre-build — the
    pipeline's automatic prebuild stage replaces it, so it is pinned ABSENT here
    (the removed-surface pins live in test_dashboard_universe_ux.py).
    DEC-UNIVERSE-UI-POLISH-002 removed the static "granularity · 1-minute
    (1-day derived)" chip — 1-minute is the ONLY granularity
    (DEC-1MINUTE-ONLY-DERIVE-1DAY-001) so the chip was dead UI noise; its
    absence is pinned here + in ``test_dashboard_universe_crosslist_select.py``.
    """
    html = _render_universe_html()
    for needle in (
        'id="selallbtn"',  # ✓ all shown
        'id="clrselbtn"',  # ✗ clear
        'id="tickconstbtn"',  # ⊕ tick its constituents
    ):
        assert needle in html, f"kept control missing: {needle}"
    assert 'id="matbtn"' not in html, "the manual pre-build button must stay removed"
    assert "granularity · <b>1-minute</b>" not in html, (
        "the dead granularity chip must stay removed (DEC-UNIVERSE-UI-POLISH-002)"
    )


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
    # The two date inputs + their range container are present (the aria-label
    # honestly names BOTH scoping roles — DEC-EXPIRY-SCOPE-LIVE-EDGE-001 killed
    # the "spot only" lie).
    assert 'id="pipeFrom"' in html, "the from-date input must be present"
    assert 'id="pipeTo"' in html, "the to-date input must be present"
    assert 'aria-label="Pull date range (spot history + F&amp;O expiry scope)"' in html, (
        "the daterange group must be present with the honest two-role label"
    )
    # The pre-fill / ceiling tokens are SUBSTITUTED (no raw token leaks) —
    # including the data-default stamps (str.replace fills every occurrence).
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


def test_date_pickers_stamp_defaults_and_omit_unchanged_values() -> None:
    """DEC-EXPIRY-SCOPE-LIVE-EDGE-001 frontend: default-equal picks are OMITTED.

    Each picker carries a ``data-default`` stamped with the SAME pre-fill value,
    and ``pipelineDateQuery()`` skips a param whose value still equals its
    stamped default — so an untouched picker behaves exactly like a blank one
    and the default one-click pull threads NO dates (the F&O expiry scope stays
    unbounded; live/unexpired contracts are pulled). The lying "spot only" copy
    is gone.
    """
    from brutex.dashboard.pull_controller import default_date_range

    html = _render_universe_html()
    # data-default stamped on BOTH inputs with the pre-fill value.
    from_iso, to_iso = default_date_range()
    assert f'value="{from_iso}" data-default="{from_iso}"' in html, (
        "pipeFrom must stamp its pre-fill as data-default"
    )
    assert f'value="{to_iso}" data-default="{to_iso}"' in html, (
        "pipeTo must stamp its pre-fill as data-default"
    )
    # pipelineDateQuery omits a value equal to its stamped default.
    assert "fromEl.value !== fromEl.dataset.default" in html, (
        "from_date must be omitted when still at its stamped default"
    )
    assert "toEl.value !== toEl.dataset.default" in html, (
        "to_date must be omitted when still at its stamped default"
    )
    # The honest copy: the lead names the F&O-expiry role + the unbounded
    # default (re-pinned at the round-6 two-button split: the dates scope
    # ▶ Pull F&O; ▶ Pull Spot ignores them by promise).
    assert "select which F&amp;O expiries ▶ Pull F&amp;O pulls" in html
    assert "ALL expiries including live/unexpired contracts" in html


def test_pull_fno_button_is_selection_and_date_aware() -> None:
    """▶ Pull F&O pulls the ticks (or the whole F&O universe) with the date
    query — the selection/date surfaces the removed combined button carried
    now scope the fno-only pull (round-6 two-button split); NO auto_arm ever
    rides it (manual only — the scheduler is spot-only-scoped)."""
    html = _render_universe_html()
    assert "function pipelineSelection" in html, "the selection-scope helper is missing"
    assert (
        '"/api/v1/universe/pull-pipeline?scope=fno-only&symbols=" + encodeURIComponent(ids) + dq'
        in html
    ), "the ticked branch must post the fno-only scoped route with the date query"
    assert '"/api/v1/universe/pull-pipeline?scope=fno-only" + dq' in html, (
        "the no-selection branch must post the whole-F&O-universe fno-only route"
    )
    # The F&O button never arms the daily auto-pull (the coordinator ruling:
    # arming is spot-only-scoped; auto_arm on any other scope is a 400).
    # Slice from the const decl to the ▶ Pull Spot comment block (which
    # legitimately mentions auto_arm) — the F&O button's OWN wiring only.
    fno_wiring = html.split('const PULL_FNO_BTN = document.getElementById("pullFnoBtn")', 1)[
        1
    ].split("/* ▶ Pull Spot")[0]
    assert "auto_arm" not in fno_wiring, "▶ Pull F&O must never send auto_arm"
    assert "startAndPoll(" in fno_wiring


def test_pull_all_spot_button_wired_scope_chip_and_verify_row() -> None:
    """The scoped-ALL spot-only button (DEC-PULL-SCOPED-ALL-ONECLICK-001).

    Static markup + a static-URL ``startAndPoll`` onclick ONLY (textContent
    discipline — no new dynamic sink): the label, the always-visible scope
    chip (the chip IS the consent), the verify-links row, and the JS wiring to
    the SAME ``/pull-pipeline`` launcher with ``scope=spot-only``. Deliberately
    NOT wired to the ``#pipeFrom``/``#pipeTo`` pickers — full-range convergence
    is the button's promise. CONSCIOUS RE-PIN (round-5 fold): the static URL
    additionally carries ``auto_arm=1`` — the first click ARMS the daily
    post-market auto-pull scheduler (the self-arming operator semantics); the
    chip names it and the ``#autoPullChip`` row renders the armed state with
    the disarm/re-arm toggle (textContent only).
    """
    html = _render_universe_html()
    assert ">Pull Spot</b>" in html, "the button label is missing"
    assert "Pull EVERYTHING — spot only" not in html, (
        "the pre-round-6 label must be gone (relabeled ▶ Pull Spot)"
    )
    # The always-visible scope chip — the consent text, verbatim.
    assert (
        "all indices + all ~750 stocks · 1-minute · 2020-01-01 → previous day · "
        "incremental (settled windows skipped) · NO options, NO Greeks · "
        "cross-verify producer runs after the pull · first click arms the daily auto-pull"
    ) in html, "the scope chip must state the whole contract incl. the self-arming clause"
    # The verify row: the three read-only verification surfaces.
    assert 'id="pullAllSpotVerify"' in html
    for href in ('href="/coverage/windows"', 'href="/completeness"', 'href="/coverage"'):
        assert href in html, f"verify-row link missing: {href}"
    # The auto-pull chip row (round-5 fold): armed state + toggle, fed by the
    # pull-progress payload's additive auto_pull object — textContent only.
    assert 'id="autoPullChip"' in html
    assert 'id="autoPullToggleBtn"' in html
    assert "renderAutoPullChip" in html
    assert "'/api/v1/universe/pull-all-spot-auto?armed='" in html
    # JS wiring: the SAME launcher + the additive scope token + the self-arming
    # auto_arm=1, single-flighted through the shared startAndPoll; the date
    # pickers are NOT consulted.
    assert '"/api/v1/universe/pull-pipeline?scope=spot-only&auto_arm=1"' in html
    assert '"/api/v1/universe/pull-pipeline?scope=spot-only"' not in html, (
        "the arm-less URL form must not linger beside the self-arming one"
    )
    # The wiring slice ends at the DHAN button const (the next handler on the
    # page) — the standalone Dhan feed button legitimately reuses the date
    # pickers (DEC-DHAN-PULL-SURFACE-001, MIGRATION.md entry #1794), so the
    # spot-only full-range pin must scope to the ▶ Pull Spot handler alone.
    wiring = html.split('getElementById("pullAllSpotBtn")', 1)[1].split(
        'getElementById("pullDhanBtn")'
    )[0]
    assert "startAndPoll(" in wiring
    assert "pipelineDateQuery" not in wiring, (
        "the spot-only button must NOT thread the date pickers (full-range promise)"
    )
    assert "pipelineSelection" not in wiring, (
        "the spot-only button must NOT scope to row ticks (whole-universe promise)"
    )


def test_auto_pull_chip_seed_rides_the_one_pull_progress_fetch_site() -> None:
    """The chip's one-shot seed reuses pollProgress's SINGLE fetch site.

    Workspace-health fix (DEC-PULL-SCOPED-ALL-ONECLICK-001): the round-5
    fold's ``refreshAutoPullChip`` seed carried its OWN literal
    ``fetch('/api/v1/universe/pull-progress'`` call — a SECOND fetch site
    tripping the pre-existing ``test_no_new_poll_loop_added`` one-fetch-site
    architectural pin (test_dashboard_universe_stall_chip.py). The seed now
    requests through the ONE shared ``fetchPullProgress`` helper (declared
    beside pollProgress; the ``opts`` passthrough keeps the hung-fetch
    abort-signal teeth — pinned in test_dashboard_progress_frontend.py), so
    exactly one literal site exists while the chip still seeds at page load
    AND refreshes after an arming click; the LIVE updates keep riding
    pollProgress's own ``renderAutoPullChip(s.auto_pull)``. The seed stays a
    one-shot request — never a loop.
    """
    html = _render_universe_html()
    # The ONE shared site (the stall-chip pin's law, re-asserted here beside
    # the helper mechanism) + the helper that owns the literal.
    assert html.count("fetch('/api/v1/universe/pull-progress'") == 1
    assert "function fetchPullProgress(opts)" in html
    seed = html.split("async function refreshAutoPullChip()", 1)[1].split("\n}", 1)[0]
    assert "await fetchPullProgress()" in seed, "the seed must ride the shared site"
    assert "fetch('" not in seed, "the seed may not mint its own literal fetch site"
    assert "renderAutoPullChip(s.auto_pull)" in seed, "the seed renders the chip on success"
    assert "setInterval" not in seed and "setTimeout" not in seed, (
        "the seed is a one-shot request — never a second poll loop"
    )
    # Page-load seed: the bare top-level invocation (function declarations
    # hoist within the ONE inline script, so the later helper is in scope).
    assert "\nrefreshAutoPullChip();" in html, "the chip must seed at page load"
    # Post-click refresh: the ▶ Pull Spot onclick re-seeds (the arming took
    # effect even on a market-hours-refused start).
    wiring = html.split('getElementById("pullAllSpotBtn")', 1)[1].split(
        'getElementById("pullDhanBtn")'
    )[0]
    assert "refreshAutoPullChip();" in wiring, "the ▶ click must refresh the chip"
    # LIVE updates: pollProgress renders the chip from its own payload — the
    # subscription that makes the one-shot seed sufficient.
    poll = html.split("async function pollProgress(label)", 1)[1].split("\n}", 1)[0]
    assert "renderAutoPullChip(s.auto_pull);" in poll


def test_set_bulk_disabled_covers_both_pull_buttons() -> None:
    """[refuter-r1 r2 NIT-5, re-pinned at the round-6 two-button split]
    ``setBulkDisabled`` disables BOTH remaining pull buttons — ▶ Pull F&O AND
    ▶ Pull Spot — so the wedged-POST comments' "an in-page re-click is
    impossible" invariant is genuinely true: during any in-flight watch
    neither button can wipe the live watch UI with a re-click."""
    html = _render_universe_html()
    body = html.split("function setBulkDisabled", 1)[1].split("}", 1)[0]
    assert "PULL_FNO_BTN.disabled = on" in body, "the ▶ Pull F&O button must be disabled"
    assert "PULL_ALL_SPOT_BTN.disabled = on" in body, (
        "the spot button must join the in-flight disable (the no-in-page-re-click invariant)"
    )
    # The consts are declared before use; the removed combined button's const
    # is gone with its handler.
    assert 'const PULL_ALL_SPOT_BTN = document.getElementById("pullAllSpotBtn")' in html
    assert 'const PULL_FNO_BTN = document.getElementById("pullFnoBtn")' in html
    assert "PIPELINE_BTN" not in html, "the removed combined button's const must be gone"
    assert "disables the ONE pipeline button" not in html


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
    # When the quick budget is exhausted the poll backs off and KEEPS retrying
    # (DEC-PULL-UI-STALENESS-001 removed the give-up tail that once pointed the
    # operator at the verdict refresh); the honest "feed dropped" wording survives
    # as EXECUTABLE text inside the never-give-up #feedage stale chip — pinned as
    # the full JS string literal so a comment mention can never satisfy it.
    assert "'live progress feed dropped — retrying'" in html, (
        "the honest feed-dropped note is missing"
    )


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
