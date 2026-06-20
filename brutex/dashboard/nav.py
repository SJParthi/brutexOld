"""Single source of truth for the dashboard top navigation.

DEC-DASHBOARD-NAV-UNIFY-001 — every served dashboard page exposes the SAME
top navigation: Universe · Credentials · Database · Coverage · Movers ·
Reports. Historically each template hand-coded its own ``<nav>``, which let the
``/groww-credentials`` page silently drift (a dead ``/credentials`` link, a
missing ``Reports`` link, a renamed "Groww" tab — DEC-DASHBOARD-NAV-CREDS-
RECONCILE-001). This module makes the link set CANONICAL in exactly one place;
``tests/unit/test_dashboard_nav_consistency.py`` renders every page and fails CI
the moment a nav diverges from :data:`NAV_LINKS`, so the drift class cannot
recur. ``render_topnav`` is the shared renderer pages can adopt so the markup
itself converges to one place too.
"""

from __future__ import annotations

from typing import NamedTuple


class NavLink(NamedTuple):
    """One top-nav entry: the ``href`` it points at and the visible ``text``."""

    href: str
    text: str


#: The canonical ordered top-nav. Changing the dashboard nav means editing
#: THIS tuple (and only this tuple); the consistency guard test enforces that
#: every page matches it exactly.
NAV_LINKS: tuple[NavLink, ...] = (
    NavLink("/universe", "Universe"),
    NavLink("/groww-credentials", "Credentials"),
    NavLink("/db", "Database"),
    NavLink("/coverage", "Coverage"),
    NavLink("/movers", "Movers"),
    NavLink("/reports", "Reports"),
)


def render_topnav(active: str) -> str:
    """Render the canonical top-nav as an HTML string.

    Parameters
    ----------
    active
        The ``href`` of the current page (e.g. ``"/db"``). The matching link is
        marked ``aria-current="page"``; an ``active`` that is not one of
        :data:`NAV_LINKS` marks no link current (valid for sub-pages whose own
        route is not itself a nav entry).

    Returns
    -------
    str
        The ``<nav class="topnav">…</nav>`` markup, byte-identical regardless of
        which page renders it — so adopting it removes per-page nav drift.
    """
    lines = [
        '<nav class="topnav" aria-label="Primary" '
        'style="margin-left:18px; display:flex; gap:14px; font-size:13px;">'
    ]
    for link in NAV_LINKS:
        current = ' aria-current="page"' if link.href == active else ""
        lines.append(f'    <a href="{link.href}"{current}>{link.text}</a>')
    lines.append("  </nav>")
    return "\n".join(lines)
