"""Guards the ``/credentials`` visual upgrade (DASH-UX D4, DEC-DASHBOARD-CREDENTIALS-UX-001).

DASH-UX page 4 — the Dhan credential-entry page. The dashboard JS has no unit
harness, so these substring guards lock the markup + wiring in CI (the same
established pattern as the D1/D2/D3 UX tests). Each guard pins one additive,
presentational upgrade against the rendered ``_render_credentials_html``:

* a **sticky, frosted header** (matches /db D2 + /reports D3);
* a **show/hide (👁) eye toggle** on each secret field so the operator can
  verify a paste — purely client-side, no value leaves the field;
* an **animated status pill** with a live pulse dot when credentials are set;
* a **card entrance animation**.

Crucially this stays HONEST (Golden Rule #1/#10): it adds NO fabricated
token-expiry / "valid for Nh" countdown — the status endpoint returns no
expiry, so inventing one would be a hallucination. The masked preview already
shipped (DEC-CREDS-PREVIEW-001) is untouched. All additive: no route or
credential-handling change.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own ``_render_credentials_html``.)

from __future__ import annotations

import re

from brutex.dashboard.app import _render_credentials_html


def test_header_is_sticky_and_frosted() -> None:
    """The header is ``position:sticky`` with a blur backdrop (matches D2/D3)."""
    html = _render_credentials_html()
    rule = re.search(r"\bheader \{[^}]*\}", html, re.DOTALL)
    assert rule is not None, "the header CSS rule is missing"
    block = rule.group(0)
    assert "position:sticky" in block, "the header must be sticky"
    assert "backdrop-filter:blur" in block, "the header must have a frosted blur backdrop"


def test_show_hide_eye_toggle_present_and_wired() -> None:
    """Each secret field has a show/hide eye toggle, wired client-side only."""
    html = _render_credentials_html()
    # Two eye buttons (one per secret field), each pointing at its input.
    assert html.count('class="eye"') == 2, "each secret field needs its own eye toggle"
    assert 'data-target="access_token"' in html, "the access-token eye must target its input"
    assert 'data-target="client_id"' in html, "the client-id eye must target its input"
    # The toggle helper flips the input type + the glyph.
    assert "function toggleSecret" in html, "the show/hide helper is missing"
    assert "input.type === 'text'" in html, "the toggle must read the current input type"
    assert ".eye" in html, "the eye-button style is missing"


def test_animated_status_pill_present() -> None:
    """The 'set' status renders as a pill with a live pulse dot."""
    html = _render_credentials_html()
    assert "@keyframes livePulse" in html, "the status-pill pulse animation is missing"
    assert ".status.ok::before" in html, "the live pulse dot on the OK status is missing"


def test_card_entrance_animation_present() -> None:
    """The card + header carry the standard entrance animations."""
    html = _render_credentials_html()
    assert "@keyframes fadeInUp" in html, "the card entrance animation is missing"
    assert "@keyframes fadeInDown" in html, "the header entrance animation is missing"


def test_no_fabricated_token_expiry_countdown() -> None:
    """HONESTY GUARD (Golden Rule #1): the page must NOT invent a token-expiry
    countdown.

    The credentials status endpoint returns no expiry, so any "valid for Nh" /
    countdown timer would be a hallucinated Dhan fact. This pins that the upgrade
    stays presentational and never fabricates one.
    """
    html = _render_credentials_html()
    lowered = html.lower()
    for forbidden in ("valid for", "expires in", "countdown", "time left", "23h", "24h"):
        assert forbidden not in lowered, (
            f"the page must not fabricate a token-expiry indicator ({forbidden!r}) — "
            "the status endpoint returns no expiry"
        )


def test_credential_form_and_routes_unchanged() -> None:
    """The upgrade is additive — the form + API wiring + masked preview are intact."""
    html = _render_credentials_html()
    for needle in (
        "/api/v1/credentials/status",
        "/api/v1/credentials",
        'id="savebtn"',
        'id="access_token"',
        'id="client_id"',
        "access_token_preview",
    ):
        assert needle in html, f"a pre-existing /credentials control regressed: {needle}"
