"""Unit tests for the :mod:`brutex.dashboard` PEP 562 lazy re-exports.

DEC-COV-001: covers both arms of the package ``__getattr__`` — the
lazy-export hit path (resolving an ``app`` symbol on first attribute
access without eagerly importing :mod:`brutex.dashboard.app` at package
import time) and the unknown-attribute rejection, whose message is
pinned byte-exact (the mutation-gate lesson, DEC-MUTATION-RUNTIME-001).

Both modules are resolved via :func:`importlib.import_module` — the
test probes module machinery, so it loads modules through the module
machinery (and CodeQL's import/import-from mixing alert stays quiet).
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi")


def test_lazy_export_resolves_app_symbols() -> None:
    """Accessing a declared export resolves to the real ``app`` symbol."""
    dash = importlib.import_module("brutex.dashboard")
    app = importlib.import_module("brutex.dashboard.app")

    assert dash.build_app is app.build_app
    assert dash.DashboardState is app.DashboardState
    assert dash.PhaseSnapshot is app.PhaseSnapshot
    assert dash.snapshot_to_json is app.snapshot_to_json


def test_unknown_attribute_raises_exact_message() -> None:
    """An attribute outside ``__all__`` fails loud with the exact message."""
    dash = importlib.import_module("brutex.dashboard")

    with pytest.raises(AttributeError) as exc:
        _ = dash.does_not_exist
    assert str(exc.value) == "module 'brutex.dashboard' has no attribute 'does_not_exist'"
