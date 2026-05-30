"""Unit tests for :mod:`brutex.config.paths` — the canonical path resolvers.

Pure path computation, ``$HOME`` + ``BRUTEX_*`` injected; covers every branch
(override set / unset, home injected / default) at 100% line + branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.config.paths import ENV_LAKE_ROOT, brutex_home, resolve_lake_root


def test_brutex_home_with_injected_home(tmp_path: Path) -> None:
    """``brutex_home`` appends ``.brutex`` to the injected home dir."""
    assert brutex_home(home=tmp_path) == tmp_path / ".brutex"


def test_brutex_home_defaults_to_real_home() -> None:
    """``brutex_home`` falls back to ``Path.home()`` when no home is injected."""
    assert brutex_home() == Path.home() / ".brutex"


def test_resolve_lake_root_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``$BRUTEX_LAKE_ROOT`` (env=None path) → ``$HOME/.brutex/lake``."""
    monkeypatch.delenv(ENV_LAKE_ROOT, raising=False)
    assert resolve_lake_root(home=tmp_path) == tmp_path / ".brutex" / "lake"


def test_resolve_lake_root_default_with_empty_mapping(tmp_path: Path) -> None:
    """An explicit env mapping without the override also yields the default."""
    assert resolve_lake_root({}, home=tmp_path) == tmp_path / ".brutex" / "lake"


def test_resolve_lake_root_override_wins(tmp_path: Path) -> None:
    """``$BRUTEX_LAKE_ROOT`` (when set) overrides the default, ``~``-expanded."""
    assert resolve_lake_root({ENV_LAKE_ROOT: "/data/mylake"}) == Path("/data/mylake")


def test_resolve_lake_root_override_expands_tilde() -> None:
    """A ``~`` in the override is expanded to the real home."""
    resolved = resolve_lake_root({ENV_LAKE_ROOT: "~/customlake"})
    assert resolved == Path.home() / "customlake"
