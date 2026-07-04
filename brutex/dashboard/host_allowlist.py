"""Default dashboard ``Host``-header allowlist (DEC-DASHBOARD-SECURITY-HYGIENE-001).

A LEAF constants module, deliberately importing nothing beyond
:mod:`typing`. Both the dashboard app (:mod:`brutex.dashboard.app`, which
registers the Starlette ``TrustedHostMiddleware``) and the CLI launcher
(:mod:`brutex.cli`, which widens the allowlist for a non-loopback ``--host``
bind) need this ONE default; hosting it here lets ``brutex.cli`` read it
WITHOUT importing ``brutex.dashboard.app``, so the CLI helper adds no new
edge to the ``cli → dashboard.app → … → sweep_runner → cli`` static import
cycle (CodeQL py/cyclic-import) while the single COMMON source of truth is
preserved (CLAUDE.md §2 Rule 11).
"""

from __future__ import annotations

from typing import Final

#: Default Host allowlist: the loopback names on ANY port (the ``:*`` suffix
#: is the operator-facing "any port" spelling — normalized for Starlette by
#: :func:`brutex.dashboard.app._trusted_host_patterns`). LOOPBACK-ONLY by
#: design: ``testserver`` (the FastAPI ``TestClient`` default Host) is
#: deliberately NOT shipped in the production default — an attacker who can
#: make that single-label name resolve to the operator machine (malicious
#: DHCP search-domain / intranet DNS, the exact rebinding class this guard
#: closes) would bypass it. Tests ride the root conftest fixture that
#: re-points ``TestClient`` at ``base_url="http://localhost"``, or inject
#: ``allowed_hosts`` explicitly. Public so the CLI launcher can extend it
#: with the actual bound host.
DEFAULT_ALLOWED_HOSTS: Final[tuple[str, ...]] = (
    "127.0.0.1",
    "127.0.0.1:*",
    "localhost",
    "localhost:*",
)
