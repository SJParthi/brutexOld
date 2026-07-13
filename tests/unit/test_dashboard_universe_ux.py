"""Guards the ``/universe`` ease-of-use controls (DEC-UNIVERSE-MULTISELECT-001).

The operator hit a scroll round-trip (scroll down to select, scroll up to pull)
and could not pull several instruments at once. This pins the fixes against the
rendered template string (the dashboard JS has no unit harness, so these
substring guards are the established way to lock the markup + wiring in CI,
mirroring ``test_dashboard_universe_template.py``):

* the pull bar is **sticky** (never scrolls out of view);
* every row has a **multi-select checkbox** that toggles ``selMulti`` WITHOUT
  triggering the row's single-select (``stopPropagation``);
* the TWO-BUTTON law (the DEC-PULL-SCOPED-ALL-ONECLICK-001 round-6 split,
  superseding the one-button law of DEC-UNIVERSE-PULL-CONSOLIDATE-001 at the
  UI level): the selection/date-aware **"▶ Pull F&O"** button (``pullFnoBtn``,
  ``?scope=fno-only`` on the shared launcher — scopes to the ticks or pulls
  the whole F&O universe) + the fixed-scope auto-arming **"▶ Pull Spot"**
  button (``pullAllSpotBtn``, the static ``?scope=spot-only&auto_arm=1``) +
  **select-all-shown** / **clear** controls; the combined full-pipeline
  button is REMOVED (the route's full-chain API default survives) — the
  removed per-stage buttons stay gone; pinned in
  ``test_dashboard_universe_pull_consolidate.py``;
* the page states the pull **stores 1m + 1d only** and the 19 intraday
  timeframes are **derived on demand, not written to the drive** (pre-
  materialising is a future external-drive step) — the operator asked "where do
  I click to generate timeframes?" and wants no local-drive bloat.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own ``_render_universe_html``.)

from __future__ import annotations

import re

from brutex.dashboard.app import _render_universe_html


def test_pull_bar_is_sticky() -> None:
    """The pull bar is ``position:sticky`` so the date + pull buttons never scroll away."""
    html = _render_universe_html()
    rule = re.search(r"\.pullbar \{[^}]*\}", html, re.DOTALL)
    assert rule is not None, "the .pullbar CSS rule is missing"
    assert "position:sticky" in rule.group(0), (
        "the pull bar must be sticky so it stays visible while scrolling the list"
    )


def test_accessibility_focus_ring_and_reduced_motion_present() -> None:
    """The page carries keyboard focus rings + honours reduce-motion
    (DEC-DASHBOARD-A11Y-POLISH-001).

    Both are safe, additive polish: ``:focus-visible`` shows the ring ONLY for
    keyboard users (mouse clicks never trigger it → no visual/layout change for
    mouse), and ``prefers-reduced-motion`` only applies when the OS opts in. This
    pins them so the accessibility polish cannot silently regress.
    """
    html = _render_universe_html()
    assert ":focus-visible" in html, "keyboard focus rings (:focus-visible) are missing"
    assert "prefers-reduced-motion" in html, "the reduce-motion media query is missing"
    assert "outline: 2px solid var(--blue)" in html, "the focus-ring outline rule is missing"


def test_multi_select_controls_present() -> None:
    """The checkbox + the selection-aware pull button + select-all/clear controls render.

    Round-6 two-button split: ``pullFnoBtn`` is the SELECTION-AWARE button now
    (the combined ``pullPipelineBtn`` is removed; the fixed-scope spot button
    ``pullAllSpotBtn`` is pinned in the consolidate suite).
    """
    html = _render_universe_html()
    for needle in ('id="pullFnoBtn"', 'id="selallbtn"', 'id="clrselbtn"', 'class="msel"'):
        assert needle in html, f"missing selection control: {needle}"


def test_one_pull_button_is_selection_aware_full_pipeline() -> None:
    """The "▶ Pull F&O" button — selection-aware (round-6 two-button split).

    The redundant per-stage buttons (Pull spot / Pull expiries + contracts / Pull
    historical / Load instruments) AND the separate "Pull selected" button stay
    GONE, and the combined ``pullPipelineBtn`` is now removed too.
    ``pullFnoBtn`` posts the SHARED ``/pull-pipeline`` route with
    ``scope=fno-only``: with ticked rows it sends ``?symbols=`` (scoped to the
    selection); with NO ticks it posts the whole F&O universe. (The page's
    OTHER pull button — the fixed-scope spot-only ``pullAllSpotBtn`` — is
    deliberately NOT selection-aware and is pinned in
    ``test_dashboard_universe_pull_consolidate.py``.)
    """
    html = _render_universe_html()
    # The selection-aware pull button + its label span exist.
    assert 'id="pullFnoBtn"' in html, "the ▶ Pull F&O button is missing"
    assert 'id="pullFnoLabel"' in html, "the selection-aware label span is missing"
    assert 'id="pullPipelineBtn"' not in html, "the combined button must be gone (round 6)"
    # Selection-aware: the ticked-rows branch posts ?symbols=, the empty branch
    # the whole-F&O-universe fno-only route.
    assert (
        '"/api/v1/universe/pull-pipeline?scope=fno-only&symbols=" + encodeURIComponent(ids)' in html
    ), "the ticked-selection branch must post the scoped pipeline route"
    assert '"/api/v1/universe/pull-pipeline?scope=fno-only" + dq' in html, (
        "the no-selection branch must post the whole-F&O-universe fno-only route"
    )
    assert "function pipelineSelection" in html, "the selection-scope helper is missing"
    # The redundant per-stage + separate-selected buttons are REMOVED (no null
    # getElementById().onclick can throw at load).
    for gone in (
        'id="pullbtn"',
        'id="pullDerivBtn"',
        'id="pullHistBtn"',
        'id="pullSelPipelineBtn"',
        'id="loadbtn"',
        'id="bulkIdxSpot"',
    ):
        assert gone not in html, f"the redundant button {gone} must be removed"
    for ref in (
        'getElementById("pullbtn")',
        'getElementById("pullDerivBtn")',
        'getElementById("pullHistBtn")',
        'getElementById("pullSelPipelineBtn")',
        'getElementById("loadbtn")',
    ):
        assert ref not in html, f"no code may reference the removed element ({ref})"
    # The removed per-stage button labels are gone from the UI.
    assert "⤓ Pull spot" not in html, "the removed 'Pull spot' label must be gone"
    assert "▶ Pull selected — full pipeline" not in html, (
        "the separate 'Pull selected' button label must be gone"
    )


def test_multi_select_wiring_present() -> None:
    """The state + toggle + the selection-scoping of the ONE pull button are wired."""
    html = _render_universe_html()
    assert "selMulti" in html, "the multi-select state is missing"
    assert "function toggleMulti" in html
    assert "function updateMultiBtn" in html
    # the per-row checkbox must NOT trigger the row's single-select (independent)
    assert "stopPropagation" in html, "the checkbox must stopPropagation off the row click"
    # the ticks scope the ONE pull button via pipelineSelection (not a removed runPull loop).
    assert "function pipelineSelection" in html, "the selection must scope the single pipeline pull"


def test_timeframe_storage_is_explained_honestly() -> None:
    """The page explains 1-minute-only fetch + the auto-prebuild storage envelope.

    DEC-1MINUTE-ONLY-DERIVE-1DAY-001: 1-minute is the ONLY granularity fetched
    from Groww; 1-day + the 19 intraday timeframes are DERIVED from it.
    DEC-PIPELINE-AUTOPREBUILD-001: the derived timeframes are WRITTEN (only) by
    the pipeline's automatic final pre-build stage — covering SPOT + FUTURES +
    OPTIONS — and the page states the operator-accepted ~2-3× options storage
    cost honestly (Golden Rule #10).
    """
    html = _render_universe_html()
    assert "1-minute is the <b>only</b> granularity fetched from Groww" in html, (
        "the page must explain that 1-minute is the only granularity fetched"
    )
    assert "1-day is DERIVED" in html, (
        "the page must explain that 1-day is derived from the 1-minute base, not fetched"
    )
    assert "automatic FINAL pre-build stage" in html, (
        "the page must state derived TFs are written only by the automatic prebuild"
    )
    assert "SPOT, FUTURES and OPTIONS" in html, "the full prebuild coverage must be stated"
    assert "2–3×" in html, "the ~2-3× options-lake storage cost must be stated (honesty)"
    assert "BRUTEX_LAKE_ROOT" in html, "the external-drive config knob must be named"


def test_manual_prebuild_surface_removed() -> None:
    """The manual "⚙ Pre-build timeframes" surface is GONE (DEC-PIPELINE-AUTOPREBUILD-001).

    REGRESSION FLIP of the old pin (which required ``matbtn`` + the
    ``derive-timeframes`` POST handlers): derived timeframes are written ONLY by
    the pipeline's automatic prebuild stage, so the button, its handlers, and
    every reference to the deleted route must stay removed — no dead JS. The
    even older intrusive TF *dropdown* (DEC-UNIVERSE-TFS-DROPDOWN-REMOVE-001)
    stays removed too.
    """
    html = _render_universe_html()
    # The old intrusive dropdown stays removed.
    for gone in ('id="derivebtn"', 'id="tfsel"', 'id="derivetfs"', "⚙ Derive timeframes"):
        assert gone not in html, f"the intrusive dropdown control {gone!r} must stay removed"
    # The manual pre-build button + its handlers + the deleted route are gone.
    for gone in (
        'id="matbtn"',
        "⚙ Pre-build timeframes",
        "runMaterialize",
        "runGatedMaterialize",
        "matSelection",
        "matInFlight",
        "_matPanelStart",
        "derive-timeframes",
        'getElementById("matbtn")',
    ):
        assert gone not in html, (
            f"the manual pre-build surface {gone!r} must stay removed "
            "(DEC-PIPELINE-AUTOPREBUILD-001 — the pipeline prebuilds automatically)"
        )


def test_prebuild_copy_is_segment_accurate() -> None:
    """The hero blurb tells the TRUTH about the auto-prebuild scope.

    DEC-PIPELINE-AUTOPREBUILD-001 flips the old "OPTIONS intraday stays
    on-demand" honesty: the prebuild now covers OPTIONS too, so the OLD copy
    must be gone and the storage cost stated instead.
    """
    html = _render_universe_html()
    assert "OPTIONS intraday stays on-demand" not in html, (
        "the superseded options-on-demand claim must be gone (options now prebuild)"
    )
    assert "SPOT and FUTURES</b> once the fetch fully succeeds" not in html, (
        "the old spot+futures-only prebuild scope claim must be gone"
    )
    assert "SPOT, FUTURES and OPTIONS" in html


def test_universe_visual_upgrade_present() -> None:
    """The /universe visual upgrade (DEC-DASHBOARD-UNIVERSE-UX-001): the REF chip +
    its CSS, the inline chip legend, the skeleton-load shimmer, and the
    reference-only row dim — all keyed off the option_pullable flag."""
    html = _render_universe_html()
    # REF chip styling + the reference-only row dim.
    assert ".chip.ref" in html, "the REF chip style is missing"
    assert ".inst.refonly" in html, "the reference-only row dim is missing"
    # The chip is rendered for non-option-pullable rows (the NTM / reference set).
    assert "i.option_pullable === false" in html, "rows do not branch on option_pullable"
    assert ">REF<" in html, "the REF chip label is missing"
    # The inline legend explaining INDEX / STOCK / REF.
    assert 'legend.className = "legend"' in html, "the chip legend is missing"
    assert "reference-only · no options" in html, "the legend's REF explanation is missing"
    # Skeleton-shimmer load placeholder (reuses the existing shimmer keyframe).
    assert "skel-row" in html, "the skeleton-load shimmer rows are missing"


def test_universe_opt_badge_present() -> None:
    """The positive OPT badge (DEC-OPTIONS-ELIGIBLE-GUARD-001): an option-pullable
    row gets a green OPT chip — the mutually-exclusive partner of the REF chip —
    plus its CSS + legend key, so F&O vs spot-only reads at a glance."""
    html = _render_universe_html()
    assert ".chip.opt" in html, "the OPT chip style is missing"
    # The OPT chip is the else-branch of the refOnly conditional (every row is
    # one or the other), so it renders for every option-pullable row.
    assert '<span class="chip opt"' in html, "the OPT chip is not rendered"
    assert ">OPT<" in html, "the OPT chip label is missing"
    assert "option-pullable · F&amp;O" in html, "the legend's OPT explanation is missing"


def test_universe_daily_quota_gauge_absent() -> None:
    """The daily-quota gauge is GONE (DEC-QUOTA-VERDICT-MOOT-001).

    Inverts the old ``_present`` guard: Groww has no daily quota and nothing
    ever injected a tracker, so the gauge markup (DEC-DASHBOARD-QUOTA-GAUGE-001),
    the 80%/95% colour bands, and the ``/api/v1/quota`` poller
    (DEC-DASHBOARD-QUOTA-PERSISTED-001) are deleted with their endpoint — the
    shipped page never 404-polls a removed route."""
    html = _render_universe_html()
    assert "quotaGauge" not in html, "the deleted quota gauge markup resurfaced"
    for span in ("qgUsed", "qgLimit", "qgRem", "qgPct", "qgFill"):
        assert f'id="{span}"' not in html, f"the deleted {span} element resurfaced"
    assert ".quota-gauge" not in html, "the deleted quota gauge CSS resurfaced"
    assert "/api/v1/quota" not in html, "the page must not poll the removed quota endpoint"
    assert "s.quota_limit" not in html, "the deleted quota poller resurfaced"


def test_universe_no_data_gap_live_stat_present() -> None:
    """The live pull stats split benign DH-905 no-data boundaries out of "failed"
    into their own amber "no-data gaps" counter (DEC-DHAN-NO-DATA-GAP-COUNT-001),
    so a known empty range stops reading as a failure."""
    html = _render_universe_html()
    assert "no-data gaps" in html, "the no-data gaps live stat is missing"
    assert "s.gaps" in html, "the live stats do not read the gaps counter"
    assert ".bstat.gap" in html, "the amber gap stat style is missing"


def test_universe_fno_only_toggle_present() -> None:
    """The "F&O only" toggle (DEC-FNO-ONLY-FILTER-001): the operator can show
    ONLY option-pullable rows — a client-side filter over the option_pullable
    flag the explorer rows already carry, composed with the search box."""
    html = _render_universe_html()
    assert 'fnoBtn.id = "fno-only"' in html, "the F&O-only toggle is missing"
    assert ".fnobtn" in html, "the toggle style is missing"
    assert ".fnobtn.on" in html, "the toggle's active style is missing"
    # The filter must compose with search: hidden rows are exactly the
    # option_pullable === false ones, and only while the toggle is on.
    assert "!fnoOnly || i.option_pullable !== false" in html, (
        "the F&O-only predicate is not composed into renderInstruments"
    )


def test_universe_selection_respects_composed_visibility() -> None:
    """DEC-UNIVERSE-SELECTION-PRECISE-001: the rendered list, "✓ all shown",
    and the F&O-only prune share ONE visibility predicate, so what the
    operator sees is exactly what bulk actions operate on (the live bug:
    4 indices visible under ⚡F&O-only, yet "all shown" ticked all 33)."""
    html = _render_universe_html()
    assert "function isVisible(i)" in html, "the shared visibility predicate is missing"
    assert "function visibleInstruments()" in html
    # all-shown consumes the shared predicate, never its own copy.
    assert "visibleInstruments().forEach(i => selMulti.set(i.symbol, i))" in html
    # Tab switches KEEP the multi-select (DEC-UNIVERSE-UI-POLISH-002 supersedes
    # the tab-switch clear: one combined selection spans the Index + Stocks
    # lists, made visible by the lead naming every ticked symbol). The
    # cross-list pins live in test_dashboard_universe_crosslist_select.py; the
    # explicit "✗ clear" button still empties BOTH lists.
    assert "selMulti.clear();" in html
    # F&O-only ON prunes non-pullable ticks so hidden rows can't ride a pull.
    assert "if (v.option_pullable === false) selMulti.delete(k);" in html


def test_universe_date_pickers_removed_no_token_leak() -> None:
    """The from/to date pickers are REMOVED (DEC-UNIVERSE-PULL-CONSOLIDATE-001
    FIX-FIRST follow-up — the consolidated pipeline accepts no from/to and always
    pulls the fixed default window, so the picker was dead UI). The pickers + their
    ``__BRUTEX_DATE_*__`` placeholder tokens are gone with them."""
    html = _render_universe_html()
    assert "__BRUTEX_DATE_MAX__" not in html, "no max token may leak unfilled"
    assert 'id="fromDate"' not in html and 'id="toDate"' not in html, (
        "the dead date pickers must be removed"
    )
