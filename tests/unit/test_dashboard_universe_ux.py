"""Guards the ``/universe`` ease-of-use controls (DEC-UNIVERSE-MULTISELECT-001).

The operator hit a scroll round-trip (scroll down to select, scroll up to pull)
and could not pull several instruments at once. This pins the fixes against the
rendered template string (the dashboard JS has no unit harness, so these
substring guards are the established way to lock the markup + wiring in CI,
mirroring ``test_dashboard_universe_template.py``):

* the pull bar is **sticky** (never scrolls out of view);
* every row has a **multi-select checkbox** that toggles ``selMulti`` WITHOUT
  triggering the row's single-select (``stopPropagation``);
* ONE selection-aware **"▶ Pull · full pipeline"** button
  (DEC-UNIVERSE-PULL-CONSOLIDATE-001 — the redundant per-stage buttons "Pull spot"
  / "Pull expiries + contracts" / "Pull historical" / "Load instruments" and the
  separate "Pull selected" button are removed; the single ``pullPipelineBtn`` scopes
  to the ticks or pulls the whole universe) + **select-all-shown** / **clear**
  controls;
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
    """The checkbox + the ONE pull button + select-all/clear controls are rendered.

    DEC-UNIVERSE-PULL-CONSOLIDATE-001: the per-stage ``pullbtn`` (Pull spot) is gone;
    the single selection-aware ``pullPipelineBtn`` is the one pull button.
    """
    html = _render_universe_html()
    for needle in ('id="pullPipelineBtn"', 'id="selallbtn"', 'id="clrselbtn"', 'class="msel"'):
        assert needle in html, f"missing selection control: {needle}"


def test_one_pull_button_is_selection_aware_full_pipeline() -> None:
    """ONE "▶ Pull · full pipeline" button — selection-aware (DEC-UNIVERSE-PULL-CONSOLIDATE-001).

    The redundant per-stage buttons (Pull spot / Pull expiries + contracts / Pull
    historical / Load instruments) AND the separate "Pull selected" button are GONE.
    The single ``pullPipelineBtn`` posts the SHARED ``/pull-pipeline`` route: with
    ticked rows it sends ``?symbols=`` (scoped to the selection); with NO ticks it
    posts the bare route (the whole universe). It runs the fixed gated chain.
    """
    html = _render_universe_html()
    # The ONE pull button + its label span exist.
    assert 'id="pullPipelineBtn"' in html, "the single full-pipeline button is missing"
    assert 'id="pullPipelineLabel"' in html, "the selection-aware label span is missing"
    assert "Pull · full pipeline" in html, "the consolidated button label is missing"
    # Selection-aware: the ticked-rows branch posts ?symbols=, the empty branch the bare route.
    assert '"/api/v1/universe/pull-pipeline?symbols=" + encodeURIComponent(ids)' in html, (
        "the ticked-selection branch must post the scoped pipeline route"
    )
    assert '"/api/v1/universe/pull-pipeline",' in html, (
        "the no-selection branch must post the bare (whole-universe) pipeline route"
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
    """The page explains 1-minute-only fetch + that 1-day / intraday TFs are derived, not stored.

    DEC-1MINUTE-ONLY-DERIVE-1DAY-001: 1-minute is the ONLY granularity fetched from Groww;
    1-day + the 19 intraday timeframes are DERIVED from it. The page copy explains this and
    that the intraday TFs are computed on demand (not written to the drive by default).
    """
    html = _render_universe_html()
    assert "1-minute is the <b>only</b> granularity fetched from Groww" in html, (
        "the page must explain that 1-minute is the only granularity fetched"
    )
    assert "1-day is DERIVED" in html, (
        "the page must explain that 1-day is derived from the 1-minute base, not fetched"
    )
    assert "NOT written to your drive" in html, (
        "the page must state the intraday timeframes are derived on demand, not materialised; "
        "pre-materialising is a future external-drive (BRUTEX_LAKE_ROOT) step"
    )
    assert "BRUTEX_LAKE_ROOT" in html, "the external-drive config knob must be named"


def test_intrusive_tf_dropdown_stays_removed_but_clean_button_replaces_it() -> None:
    """The intrusive auto-appearing TF *dropdown* stays GONE; a single clean
    "⚙ Pre-build timeframes" button replaces it (DEC-UNIVERSE-TF-MATERIALIZE-BUTTON-001
    amends DEC-UNIVERSE-TFS-DROPDOWN-REMOVE-001).

    History: the operator first hit a confusing, intrusive ``TFs`` multi-select +
    "⚙ Derive timeframes" button that auto-appeared in the pull bar the moment an
    instrument was selected ("why suddenly this small tfs ... just remove that")
    → it was removed, keeping the materialise *backend*
    (``/api/v1/universe/derive-timeframes`` + ``timeframe_materialize``,
    DEC-TF-MATERIALIZE-001). The operator then explicitly asked for a
    pre-build-timeframes affordance back — so this adds ONE always-present,
    non-intrusive ``matbtn`` button (NOT the auto-appearing ``tfsel``/``derivetfs``
    dropdown). This guard pins BOTH halves: the dropdown cannot creep back, and
    the clean button is wired to the route.
    """
    html = _render_universe_html()
    # The intrusive dropdown + its auto-appearing widgets stay removed.
    for gone in ('id="derivebtn"', 'id="tfsel"', 'id="derivetfs"', "⚙ Derive timeframes"):
        assert gone not in html, (
            f"the intrusive derive-timeframes dropdown control {gone!r} must stay removed "
            "(DEC-UNIVERSE-TFS-DROPDOWN-REMOVE-001)"
        )
    assert 'getElementById("derivebtn")' not in html, (
        "no code may reference the removed dropdown button (would throw at load)"
    )
    # The clean replacement button IS present + wired to the (kept) backend route.
    assert 'id="matbtn"' in html, "the single '⚙ Pre-build timeframes' button must be present"
    assert "⚙ Pre-build timeframes" in html, "the clean materialise button label is missing"
    assert "function matSelection" in html, (
        "the one-or-many materialise selection helper is missing"
    )
    assert "matInFlight" in html, "the materialise double-click re-entrancy guard is missing"
    assert "derive-timeframes?symbol=" in html, (
        "the clean button must POST the (kept) materialise route"
    )


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


def test_universe_daily_quota_gauge_present() -> None:
    """The Dhan daily-quota gauge (DEC-DASHBOARD-QUOTA-GAUGE-001): the header gauge
    markup, the 80%/95% colour-band CSS, and the poller that reads /api/v1/quota
    (the persisted IST-midnight tracker — DEC-DASHBOARD-QUOTA-PERSISTED-001) so the
    gauge shows today's budget on page-load, before any pull. Hidden until a
    positive limit is reported."""
    html = _render_universe_html()
    assert 'id="quotaGauge"' in html, "the quota gauge block is missing"
    for span in ("qgUsed", "qgLimit", "qgRem", "qgPct", "qgFill"):
        assert f'id="{span}"' in html, f"the {span} element is missing"
    assert "display:none" in html, "the gauge must start hidden until usage is reported"
    assert ".quota-gauge.warn" in html, "the ≥80% amber band is missing"
    assert ".quota-gauge.crit" in html, "the ≥95% red band is missing"
    # The poller reads the persisted-quota endpoint + the crit branch.
    assert "/api/v1/quota" in html, "the gauge does not poll the persisted-quota endpoint"
    assert "s.quota_limit" in html, "the poller does not read quota_limit"
    assert "pct >= 95" in html, "the crit threshold branch is missing"


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
    # Tab switches clear the multi-select (a different universe).
    assert "selMulti.clear();" in html
    # F&O-only ON prunes non-pullable ticks so hidden rows can't ride a pull.
    assert "if (v.option_pullable === false) selMulti.delete(k);" in html


def test_universe_date_pickers_carry_today_ceiling() -> None:
    """The pickers refuse future dates client-side; the rendered page carries
    a real ISO max (the raw token never leaks)."""
    html = _render_universe_html()
    assert "__BRUTEX_DATE_MAX__" not in html, "the max token leaked unfilled"
    assert html.count('max="2') == 2, "both pickers must carry the ISO max ceiling"
