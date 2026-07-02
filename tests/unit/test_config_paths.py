"""Unit tests for :mod:`brutex.config.paths` — the canonical path resolvers.

Pure path computation, ``$HOME`` + ``BRUTEX_*`` injected; covers every branch
(override set / unset, home injected / default) at 100% line + branch — including
``lake_root_identity`` (the G1 mount/split-brain seam, DEC-DOCTOR-LIVE-PROOF-KIT-001).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.config.paths import (
    ENV_LAKE_ROOT,
    brutex_home,
    lake_root_identity,
    resolve_lake_root,
)


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


# ── lake_root_identity (G1; the kit's mount/split-brain seam) ───────────────
# Stem-matched HERE (the module's own per-file coverage gate) rather than in
# the doctor-live suite: scripts/ci/coverage_gate_discovery.py maps
# brutex/config/paths.py -> tests/unit/test_config_paths.py.


class TestLakeRootIdentity:
    def test_volumes_path_mounted(self) -> None:
        identity = lake_root_identity(
            Path("/Volumes/T8/brutex-lake"),
            ismount=lambda p: p == "/Volumes/T8",
            stat_dev=lambda _p: 42,
        )
        assert identity.volume_parent == Path("/Volumes/T8")
        assert identity.volume_mounted is True
        assert identity.device == 42
        assert identity.exists is False

    def test_volumes_path_unmounted(self) -> None:
        identity = lake_root_identity(
            Path("/Volumes/T8/brutex-lake"), ismount=lambda _p: False, stat_dev=lambda _p: 1
        )
        assert identity.volume_mounted is False

    def test_non_volumes_path_has_no_volume_verdict(self, tmp_path: Path) -> None:
        identity = lake_root_identity(tmp_path, stat_dev=lambda _p: 7)
        assert identity.volume_parent is None
        assert identity.volume_mounted is None
        assert identity.exists is True

    def test_real_stat_dev_default(self, tmp_path: Path) -> None:
        identity = lake_root_identity(tmp_path)
        assert identity.device is not None

    def test_device_walks_to_nearest_existing_ancestor(self, tmp_path: Path) -> None:
        seen: list[str] = []

        def _stat(path: str) -> int:
            seen.append(path)
            if path == str(tmp_path):
                return 99
            raise OSError("does not exist")

        identity = lake_root_identity(tmp_path / "missing" / "lake", stat_dev=_stat)
        assert identity.device == 99
        assert seen[0] == str(tmp_path / "missing" / "lake")

    def test_device_none_when_every_stat_fails(self) -> None:
        def _stat(_path: str) -> int:
            raise OSError("no")

        identity = lake_root_identity(Path("/nowhere/lake"), stat_dev=_stat)
        assert identity.device is None

    def test_expanduser_applied(self) -> None:
        identity = lake_root_identity(Path("~/lake"), stat_dev=lambda _p: 1)
        assert "~" not in str(identity.path)
