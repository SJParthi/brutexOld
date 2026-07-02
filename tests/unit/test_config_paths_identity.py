"""Unit tests for ``paths.lake_root_identity`` (G1 seam, DEC-DOCTOR-LIVE-PROOF-KIT-001).

Stem-matched to ``brutex/config/paths.py`` so the strict per-module coverage
discovery gate (``scripts/ci/coverage_gate_discovery.py``) scopes these tests to
the module they cover (moved out of the combined ``test_doctor_live_seams.py``,
whose stem matched no touched module — the same relocation discipline as the
sibling R1 PR's gate fix).
"""

from __future__ import annotations

from pathlib import Path

from brutex.config.paths import lake_root_identity


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
