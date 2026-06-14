"""The ``/universe`` EXPIRIES + CONTRACTS panels read the F&O REGISTRY.

Guards **DEC-UNIVERSE-REGISTRY-PANELS-001**. After the metadata/historical split
(ADR-0010) the "⛓ Pull expiries + contracts" button fills the SEPARATE
``registry.duckdb`` cache, but the ``/universe`` EXPIRIES / CONTRACTS panels were
still reading the expired-options OHLC lake (``/api/v1/expiries`` /
``/api/v1/contracts``) — so a fresh metadata pull populated the registry yet the
panels showed *"No expired-option series in the lake yet"*. This repoints BOTH
panels to the registry routes (``/api/v1/db/registry/expiries`` +
``.../contracts``, DEC-DERIV-REGISTRY-DB-VIEW-001) so a pulled chain is
immediately visible structured by expiry / strike / CE-PE.

These substring assertions FAIL on the pre-fix template (panels still call the
options-lake routes) and pass after the repoint. The options-lake routes
themselves are UNCHANGED (the ``/db`` options browser still uses them) — only the
``/universe`` panels stopped calling them.
"""

from __future__ import annotations

from brutex.dashboard.app import _render_universe_html


def test_expiries_panel_reads_the_registry_route() -> None:
    """The EXPIRIES panel fetches the registry expiries, not the options lake."""
    html = _render_universe_html()
    assert "/api/v1/db/registry/expiries?underlying=" in html
    # The /universe panel no longer calls the expired-options-lake expiries route
    # (the route itself stays for the /db options browser).
    assert "/api/v1/expiries?underlying=" not in html


def test_contracts_panel_reads_the_registry_route() -> None:
    """The CONTRACTS chain fetches the registry contracts by (underlying, expiry_date)."""
    html = _render_universe_html()
    assert "/api/v1/db/registry/contracts?underlying=" in html
    # The chain is now keyed by the ISO expiry_date the registry stores, not the
    # options-lake expiry_code selector.
    assert "expiry_date=" in html
    assert "/api/v1/contracts?underlying=" not in html


def test_empty_registry_shows_honest_pull_first_hint() -> None:
    """An empty registry surfaces an honest "pull first" hint (or the route's own note)."""
    html = _render_universe_html()
    assert "No expiries pulled yet" in html
    # the panel prefers the route's own missing-registry note, falling back to the hint
    assert "body.note ||" in html
