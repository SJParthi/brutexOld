"""The ``/universe`` UI fixes pack (DEC-UNIVERSE-UI-POLISH-002).

Three operator-reported defects (fresh-clone screenshots, 2026-07-03):

1. **Dead granularity chip** — the static "granularity · 1-minute (1-day
   derived)" ``ctl-field`` chip was UI noise: since
   DEC-1MINUTE-ONLY-DERIVE-1DAY-001 1-minute is the ONLY fetched granularity
   (``resolve_fetch_intervals`` is backend-fixed; no route param ever read the
   chip). Removed outright; the hero copy still states the contract.
2. **Cross-list selection bug** — ticking ADANIENT on the STOCKS list and
   NIFTY on the INDEX list did NOT produce a combined pull: the tab onclick
   called ``selMulti.clear()`` on every INDEX↔STOCKS switch
   (DEC-UNIVERSE-SELECTION-PRECISE-001 — a hidden tick was invisible, so it
   was wiped). SUPERSEDED: the switch now KEEPS the ticks — one combined
   selection spans BOTH lists — and invisibility is solved honestly by the
   pull-bar lead NAMING every ticked symbol ("Selected: ADANIENT, NIFTY (2)",
   ``updateSelectionLead``, textContent-only). "✗ clear" empties BOTH lists;
   "✓ all shown" still ticks only the VISIBLE list (documented in its title).
   The route half (``?symbols=`` spanning both kinds) is pinned end-to-end in
   ``test_dashboard_pull_pipeline_route.py::test_pull_pipeline_cross_list_selection_scopes_both_kinds``.
3. **Occluding live-proof toast** — the ``bx-liveproof.js`` pill at
   bottom-left overlapped the instruments list's symbol rows. It now
   AUTO-COLLAPSES to a compact dot-only badge (``bxlp-min``) after
   ``AUTO_MIN_MS``; hover expands it (CSS-only), a NEW verdict re-expands it.

The dashboard JS has no unit harness, so these substring guards lock the
markup + wiring in CI — the established pattern of
``test_dashboard_universe_ux.py`` / ``test_dashboard_liveproof_widget.py``.
Every pin here FAILS on the pre-fix template/widget.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own ``_render_universe_html``.)

from __future__ import annotations

from pathlib import Path

from brutex.dashboard.app import _render_universe_html

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"


# ── defect 1: the dead granularity chip is GONE ───────────────────────────────


def test_granularity_chip_is_removed() -> None:
    """The static granularity chip + its ``ctl-field`` shell are gone.

    1-minute is the ONLY granularity (DEC-1MINUTE-ONLY-DERIVE-1DAY-001,
    backend-fixed via ``resolve_fetch_intervals``), so the chip informed
    nothing and fed no route param — dead UI noise the operator wanted gone.
    """
    html = _render_universe_html()
    assert "granularity · <b>1-minute</b>" not in html, "the granularity chip must be gone"
    assert 'class="ctl-field"' not in html, "the chip's ctl-field shell must be gone"
    assert ".ctl-field {" not in html, "the dead ctl-field CSS rule must be gone"
    # The 1-minute-only contract is still stated to the operator (hero copy).
    assert "1-minute is the <b>only</b> granularity fetched" in html


# ── defect 2: ONE combined selection across the Index + Stocks lists ─────────


def test_tab_switch_keeps_the_multi_select() -> None:
    """The INDEX↔STOCKS tab switch no longer wipes the ticks.

    Root cause of the operator's "ADANIENT + NIFTY can't be pulled together":
    the tab onclick called ``selMulti.clear()``, so a tick made on one list
    died the moment the other list was opened. The ONLY remaining
    ``selMulti.clear()`` is the explicit "✗ clear" button (which empties BOTH
    lists — one shared Map). FAILS on the pre-fix template (two clears).
    """
    html = _render_universe_html()
    assert "CROSS-LIST SELECTION (DEC-UNIVERSE-UI-POLISH-002)" in html, (
        "the tab handler must document the kept-selection contract"
    )
    assert html.count("selMulti.clear();") == 1, (
        "exactly ONE selMulti.clear() may remain — the explicit ✗ clear button; "
        "a second one is the tab-switch wipe regressing"
    )


def test_lead_names_every_ticked_symbol_across_lists() -> None:
    """The pull-bar lead lists the combined selection — never an invisible tick.

    ``updateSelectionLead`` renders "Selected: ADANIENT, NIFTY (2) — one
    combined selection across the Index + Stocks lists", capped at
    ``LEAD_MAX_NAMED`` names with an honest "+N more". textContent ONLY —
    the old single-select ``pulllead.innerHTML`` write is gone (XSS guard).
    """
    html = _render_universe_html()
    assert "function updateSelectionLead" in html, "the combined-selection lead is missing"
    assert "DEFAULT_PULL_LEAD" in html, "the restorable default lead capture is missing"
    assert "LEAD_MAX_NAMED" in html, "the honest +N-more display cap is missing"
    assert "one combined selection across the " in html, (
        "the lead must say the selection spans both lists"
    )
    assert "lead.textContent =" in html, "the lead must be written via textContent"
    assert 'pulllead").innerHTML' not in html, (
        "the lead must never be written via innerHTML (textContent-only, XSS guard)"
    )
    # updateMultiBtn drives BOTH selection surfaces (button label + lead).
    assert "updateSelectionLead();" in html


def test_bulk_buttons_document_their_scope() -> None:
    """ "✓ all shown" is visible-list-only; "✗ clear" clears BOTH lists."""
    html = _render_universe_html()
    assert "in THIS list only" in html, '"✓ all shown" must document it ticks only the visible list'
    assert "across BOTH the Index and Stocks lists" in html, (
        '"✗ clear" must document it unticks both lists'
    )


# ── defect 3: the live-proof toast never occludes content ─────────────────────


def test_liveproof_pill_auto_collapses_to_a_dot_badge() -> None:
    """The bottom-left live-proof pill auto-collapses so it stops covering rows.

    The full pill overlapped the /universe instruments list (operator
    screenshot). After ``AUTO_MIN_MS`` it shrinks to a dot-only ``bxlp-min``
    badge; hovering expands it (CSS-only); a NEW verdict (changed
    kind/checked_at signature) re-expands it and restarts the timer. The RED
    failure banner is untouched — a failing verdict stays loud.
    """
    js = (_STATIC / "bx-liveproof.js").read_text(encoding="utf-8")
    assert "AUTO_MIN_MS" in js, "the auto-collapse timer constant is missing"
    assert "bxlp-min" in js, "the collapsed dot-badge class is missing"
    assert "function scheduleMinimize" in js, "the collapse scheduler is missing"
    assert "function verdictSignature" in js, (
        "a NEW verdict must be detectable (re-expand + restart)"
    )
    assert ".bxlp-min:hover" in js, "hover must expand the collapsed badge (CSS-only)"
    assert ".bxlp-min .bxlp-text" in js, "the collapsed badge must hide the text rows"
    # The widget stays textContent-only (the DEC-DOCTOR-LIVE-AUTORUN-001 XSS
    # contract) — the collapse layer adds no markup sink.
    assert "innerHTML" not in js
    # The collapse never touches the red banner (failing verdicts stay loud).
    assert 'banner.classList.remove("open")' in js
