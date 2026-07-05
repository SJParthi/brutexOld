"""Template-source pins for the DEC-DASHBOARD-UX-ANIMATE-001 design-system adoption.

HONESTY ENVELOPE: this repo has NO JavaScript coverage harness — the inline
``<script>`` behavior of the dashboard templates cannot be exercised or
covered directly here. These tests pin the TEMPLATE SOURCE STRUCTURE (the
``test_dashboard_js_perf_frontend.py`` idiom) that the UX/animation adoption
relies on, for the SIX free (non-open-PR-fenced) templates:

(a) each adopting template links the shared design sheet
    (``/static/bx-design.css``) and the shared helper script
    (``/static/bx-ui.js``, deferred) — and where the page also links
    ``bx-theme.css`` the design sheet is linked AFTER it (the theme keeps
    winning the header cascade, per PLAN §2);
(b) each template ships a skeleton mechanism for its async regions (static
    ``.bx-skel`` ghosts and/or a skeleton builder) — the pre-fix bare
    ``<div class="empty">loading…</div>`` terminal placeholders are GONE
    ("Loading…" is banned as a terminal state);
(c) each template ships an honest ERROR-state mechanism (``BXUI.errorCard``
    / a ``.bx-state error`` builder / the coverage hub's ``.stat.err``
    retrying chips / v29's "unreachable — retrying" heartbeat slots);
(d) each template wires the fetch deadline (``fetchWithTimeout``) so a hung
    endpoint always resolves to an honest state;
(e) the truthful live dot: the poller pages drive ``live-ok/warn/down``
    state classes instead of a hard-coded always-green pulse.

These pins FAIL on the pre-adoption templates (the assets were not linked;
the bare loading placeholders existed) and pass after. No JS coverage is
claimed. The six fenced templates (universe/movers/fetch_windows/
completeness/reports/welcome) are deliberately NOT asserted here — their
adoption packages are report-only until their open-PR fences clear.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"
STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"

# The six FREE templates that adopt the system in this decision.
ADOPTING = (
    "v29.html",
    "results.html",
    "db.html",
    "coverage.html",
    "spot_coverage.html",
    "expired_options.html",
)

# Pages that also link the shared theme sheet (v29 is deliberately
# self-sufficient on bx-design's token copy and does not link bx-theme).
THEMED = tuple(name for name in ADOPTING if name != "v29.html")


def _template(name: str) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    assert text, f"expected a non-empty template at {name}"
    return text


# ---------------------------------------------------------------------------
# (a) Shared assets linked on every adopting template.
# ---------------------------------------------------------------------------


def test_shared_design_assets_exist_on_disk() -> None:
    """The two NEW shared static files ship with the templates that link them."""
    assert (STATIC / "bx-design.css").is_file()
    assert (STATIC / "bx-ui.js").is_file()


@pytest.mark.parametrize("name", ADOPTING)
def test_template_links_design_sheet_and_helper(name: str) -> None:
    """Each adopting template links bx-design.css + the deferred bx-ui.js."""
    html = _template(name)
    assert '<link rel="stylesheet" href="/static/bx-design.css">' in html, (
        f"{name}: missing the bx-design.css stylesheet link"
    )
    assert '<script defer src="/static/bx-ui.js"></script>' in html, (
        f"{name}: missing the deferred bx-ui.js helper script"
    )


@pytest.mark.parametrize("name", THEMED)
def test_design_sheet_is_linked_after_theme(name: str) -> None:
    """bx-theme.css keeps winning the header cascade: bx-design comes AFTER it."""
    html = _template(name)
    theme = html.index('href="/static/bx-theme.css"')
    design = html.index('href="/static/bx-design.css"')
    assert theme < design, f"{name}: bx-design.css must be linked after bx-theme.css"


# ---------------------------------------------------------------------------
# (b) Skeletons present; the bare terminal "loading…" placeholders are gone.
# ---------------------------------------------------------------------------

_SKELETON_NEEDLES: dict[str, tuple[str, ...]] = {
    # Heartbeat hero slots boot as static shimmer rows.
    "v29.html": ('<div class="bx-skel bx-skel-row">',),
    # Ghost KPI cards + ghost table grid on load start.
    "results.html": ("bx-skel", "bx-skel-card"),
    # ONE consolidated skeleton builder + the 10 s honesty watchdog caption.
    "db.html": (
        "function showSkeleton(panel)",
        '"still working — the lake is busy (a pull/prebuild may be running)"',
    ),
    # Per-card/KPI shimmer bars.
    "coverage.html": ("bx-skel",),
    "spot_coverage.html": ("bx-skel",),
    "expired_options.html": ("bx-skel",),
}


@pytest.mark.parametrize("name", sorted(_SKELETON_NEEDLES))
def test_template_ships_skeleton_mechanism(name: str) -> None:
    """Async regions paint shimmer skeletons instead of bare text."""
    html = _template(name)
    for needle in _SKELETON_NEEDLES[name]:
        assert needle in html, f"{name}: missing skeleton fragment {needle!r}"


@pytest.mark.parametrize("name", ADOPTING)
def test_bare_terminal_loading_placeholder_is_gone(name: str) -> None:
    """The pre-fix ``<div class="empty">loading…`` terminal sinks are dead."""
    html = _template(name)
    assert 'empty">loading' not in html, f"{name}: a bare terminal loading placeholder is back"


# ---------------------------------------------------------------------------
# (c) Honest error-state mechanism per template.
# ---------------------------------------------------------------------------

_ERROR_NEEDLES: dict[str, tuple[str, ...]] = {
    # Heartbeat slots resolve to an honest "unreachable — retrying" state.
    "v29.html": ("unreachable — retrying",),
    # .bx-state.error card with a Retry button + honest empty state.
    "results.html": ("BXUI.errorCard(", "BXUI.emptyState("),
    # Page-local error/offline/empty builders + ONE delegated Retry listener.
    "db.html": (
        '<div class="bx-state error">',
        '<div class="bx-state offline">',
        "bx-state-retry",
    ),
    # Per-card amber "… retrying" chips (the hub keeps last-good values).
    "coverage.html": ("function statError(", '"error " + r.status + " · retrying"'),
    # Error card on a dead first load; stale ribbon keeps the last table after.
    "spot_coverage.html": ("BXUI.errorCard(", "stalebar"),
    # Error card + the pull-active gradient ribbon.
    "expired_options.html": ("BXUI.errorCard(", 'id="pullribbon"'),
}


@pytest.mark.parametrize("name", sorted(_ERROR_NEEDLES))
def test_template_ships_honest_error_state(name: str) -> None:
    """Every async region can resolve to an honest error state (never stuck)."""
    html = _template(name)
    for needle in _ERROR_NEEDLES[name]:
        assert needle in html, f"{name}: missing error-state fragment {needle!r}"


# ---------------------------------------------------------------------------
# (d) Fetch deadline wired — a hung endpoint always resolves.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ADOPTING)
def test_template_wires_fetch_with_timeout(name: str) -> None:
    """Every adopting template routes polls through BXUI.fetchWithTimeout."""
    assert "fetchWithTimeout" in _template(name), f"{name}: fetchWithTimeout is not wired"


# ---------------------------------------------------------------------------
# (e) Truthful live dot — state-driven, never a hard-coded green pulse.
# ---------------------------------------------------------------------------

_DOT_NEEDLES: dict[str, tuple[str, ...]] = {
    "v29.html": ("bx-live-dot",),
    "results.html": ("bx-live-dot", "BXUI.setDotState("),
    "coverage.html": ("bx-live-dot", "BXUI.setDotState("),
    "spot_coverage.html": ("bx-live-dot", "BXUI.setDotState("),
    # Drives the live-* classes directly (no BXUI dependency for the dot).
    "expired_options.html": (
        "bx-live-dot",
        'liveDot.classList.remove("live-ok", "live-warn", "live-down");',
    ),
}


@pytest.mark.parametrize("name", sorted(_DOT_NEEDLES))
def test_template_ships_truthful_live_dot(name: str) -> None:
    """The heartbeat dot is state-driven (ok/warn/down), not always-green."""
    html = _template(name)
    for needle in _DOT_NEEDLES[name]:
        assert needle in html, f"{name}: missing truthful-dot fragment {needle!r}"
