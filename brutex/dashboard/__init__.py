"""BruteX real-time ingest dashboard.

FastAPI + Jinja2 + vanilla-JS WebSocket — single-file backend plus
one HTML template. Implements ``FR-UI-006`` / ``FR-UI-007`` /
``FR-UI-008``.

The dashboard is deliberately **minimal by design**: no React, no
npm, no build step. Open in any browser, auto-refresh every 500ms
via WebSocket.

See :mod:`brutex.dashboard.app` for the implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# Lazy re-export (PEP 562). Importing this package — or one of its
# import-light submodules like ``universe_query`` / ``pull_controller`` —
# must NOT eagerly pull in :mod:`brutex.dashboard.app`, which transitively
# imports the orchestrator (and the ``fastbloom_rs`` PyO3 extension). That
# eager chain breaks per-module coverage runs that import only the pure
# submodule. The ``app`` symbols below resolve on first attribute access.

if TYPE_CHECKING:
    from brutex.dashboard.app import (
        DashboardState,
        PhaseSnapshot,
        build_app,
        snapshot_to_json,
    )

__all__ = [
    "DashboardState",
    "PhaseSnapshot",
    "build_app",
    "snapshot_to_json",
]

_LAZY_EXPORTS = frozenset(__all__)


def __getattr__(name: str) -> Any:
    """Resolve the ``app`` re-exports lazily on first access (PEP 562)."""
    if name in _LAZY_EXPORTS:
        from brutex.dashboard import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
