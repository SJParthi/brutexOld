"""Guards the premium animated landing page (DEC-DASHBOARD-WELCOME-001).

The ``/welcome`` page is an ADDITIVE, self-contained marketing landing — it never
touches the locked v29 operator dashboard at ``/``. These render-substring guards
pin: the brand + the navigation/CTA links into the real app pages, the
accessibility polish (keyboard ``:focus-visible`` rings + ``prefers-reduced-motion``
— additive, cannot change the static mouse layout), the animated stat targets, and
the **honest envelope** (no "millions in minutes"; the sweep SLA names its hardware
envelope, India VIX is reference-only). Rendered against the page's own
``_render_welcome_html`` helper, mirroring ``test_dashboard_a11y_polish.py``.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="dashboard render helpers need the [ui] extra")

from brutex.dashboard.app import _render_welcome_html


def test_welcome_is_valid_self_contained_html() -> None:
    html = _render_welcome_html()
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "<title>BruteX" in html
    # Self-contained: no external resource fetched (CSP + offline safe) — the
    # absence of any http(s) URL is the real guard.
    assert "http://" not in html
    assert "https://" not in html


def test_welcome_links_into_every_app_page() -> None:
    html = _render_welcome_html()
    for href in (
        'href="/"',
        'href="/universe"',
        'href="/coverage"',
        'href="/reports"',
        'href="/db"',
    ):
        assert href in html, f"welcome page missing nav link {href}"
    # The /logs webpage was removed (DEC-DASHBOARD-LOGS-REMOVE-001).
    assert 'href="/logs"' not in html
    # The in-app Groww credentials page was removed (DEC-AWS-ONLY-CREDS-001) — credentials
    # now live ONLY in AWS Parameter Store, so the welcome page no longer links it.
    assert 'href="/groww-credentials"' not in html


def test_welcome_has_accessibility_polish() -> None:
    html = _render_welcome_html()
    assert ":focus-visible" in html
    assert "outline: 2px solid var(--blue)" in html
    assert "prefers-reduced-motion" in html


def test_welcome_has_animated_stat_targets() -> None:
    # The count-up animation reads honest, repo-true targets from data-target.
    html = _render_welcome_html()
    assert 'data-target="33"' in html  # ~33B trials / sweep
    assert 'data-target="241"' in html  # universe underlyings
    assert 'data-target="21"' in html  # timeframes
    assert "requestAnimationFrame" in html  # the count-up is genuinely animated


def test_welcome_o1_card_states_two_tier_enforcement() -> None:
    # DEC-O1-CLAIMS-HONESTY-001: the "O(1) everything" card states the TRUE
    # two-tier enforcement of tests/bench/_o1_tiers.py — the robust 3–10×
    # shared-CI ceiling, the strict 1.1× decree tier on the self-hosted
    # M4 Pro runner, and the measured 3× CPU cache-tier allowance — instead
    # of the stale absolute "CI-gated at p99 ≤ 1.1×" claim, which is
    # HARD-asserted absent so it can never silently return.
    html = _render_welcome_html()
    assert "CI-gated at p99 ≤ 1.1×" not in html
    assert "3–10×" in html
    assert "strict 1.1×" in html
    assert "cache tier" in html


def test_welcome_o1_card_discloses_strict_tier_dormancy() -> None:
    # Round-1 review fix (DEC-O1-CLAIMS-HONESTY-001): the strict 1.1× tier is
    # DORMANT until the operator registers the self-hosted [self-hosted,
    # macos, m4pro] runner (bench-m4pro.yml sits queued until then), so the
    # customer-facing card must not present it as a gate that runs today
    # (Golden Rule #10 / Rule #13(b): never claim "gated" for a dormant
    # tier). The dormancy qualifier + the today-gating attribution to the
    # shared-CI tier are HARD-asserted so the overstatement can never
    # silently return.
    html = _render_welcome_html()
    assert "dormant until the operator registers it" in html
    assert "reserved for the self-hosted M4 Pro runner" in html
    assert "gates every push today" in html


def test_welcome_honest_envelope_no_hype() -> None:
    # Golden Rule #10: the headline SLA names its hardware, and the page DISAVOWS
    # the classic hype phrase rather than claiming it (it appears only inside its
    # own "No ... claims" disavowal).
    html = _render_welcome_html()
    assert "M4 Pro" in html
    assert "signal-only" in html.lower()
    assert 'No "millions in minutes"' in html
