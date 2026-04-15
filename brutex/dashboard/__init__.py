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
