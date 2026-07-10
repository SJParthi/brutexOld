"""The lake write-epoch marker (DEC-LAKE-HEAL-STARTUP-EPOCH-001) — 100% of the leaf.

The O(1) cheap gate the L3 startup sweep consults before the O(files)
``lake_content_token`` walk: a lake-root-sibling marker refreshed to a fresh
opaque token on every content mutation. Pins: pure path helper, a bump moves the
value + is best-effort (an unwritable sidecar WARNs, never raises), a read fails
to ``None`` on missing/empty/unreadable, and the non-fsync atomic replace's
cleanup finally.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from brutex.data.lake_write_epoch import (
    EPOCH_RELPATH,
    bump_write_epoch,
    epoch_marker_path,
    read_write_epoch,
)
from structlog.testing import capture_logs


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == name]


class TestEpochMarkerPath:
    def test_marker_path_is_the_lake_heal_sibling(self, tmp_path: Path) -> None:
        assert epoch_marker_path(tmp_path) == tmp_path / EPOCH_RELPATH
        # A dot-directory sibling of bars/ + options/, holding no *.parquet.
        assert EPOCH_RELPATH == ".lake_heal/write_epoch"
        assert not EPOCH_RELPATH.endswith(".parquet")


class TestReadWriteEpoch:
    def test_absent_marker_reads_none(self, tmp_path: Path) -> None:
        assert read_write_epoch(tmp_path) is None

    def test_bump_then_read_roundtrip(self, tmp_path: Path) -> None:
        bump_write_epoch(tmp_path)
        value = read_write_epoch(tmp_path)
        assert isinstance(value, str) and value
        # A fresh atomic marker file was created under the sidecar dir.
        assert epoch_marker_path(tmp_path).is_file()

    def test_empty_marker_reads_none(self, tmp_path: Path) -> None:
        dest = epoch_marker_path(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("   \n", encoding="utf-8")  # whitespace-only → stripped empty
        assert read_write_epoch(tmp_path) is None

    def test_unreadable_marker_reads_none(self, tmp_path: Path) -> None:
        # A DIRECTORY planted at the marker path → read_text raises OSError
        # (IsADirectoryError) regardless of uid (root bypasses chmod). Fails to
        # "unknown" so the L3 fast path falls through to the full token.
        dest = epoch_marker_path(tmp_path)
        dest.mkdir(parents=True, exist_ok=True)
        assert read_write_epoch(tmp_path) is None


class TestBumpWriteEpoch:
    def test_each_bump_moves_the_value(self, tmp_path: Path) -> None:
        bump_write_epoch(tmp_path)
        v1 = read_write_epoch(tmp_path)
        bump_write_epoch(tmp_path)
        v2 = read_write_epoch(tmp_path)
        assert v1 is not None and v2 is not None and v1 != v2

    def test_bump_is_best_effort_when_sidecar_dir_is_a_file(self, tmp_path: Path) -> None:
        # Plant a FILE at the `.lake_heal` dir so `mkdir(parents=True,
        # exist_ok=True)` raises FileExistsError (an OSError) → one WARN, no raise.
        (tmp_path / ".lake_heal").write_text("not a dir", encoding="utf-8")
        with capture_logs() as logs:
            bump_write_epoch(tmp_path)  # must NOT raise
        (warn,) = _events(logs, "lake_write_epoch.bump_failed")
        assert warn["log_level"] == "warning"
        # The dest path is repr()'d (vendor-influenceable per the caller site).
        assert warn["path"] == repr(str(epoch_marker_path(tmp_path)))

    def test_bump_best_effort_when_marker_is_a_directory_cleans_the_temp(
        self, tmp_path: Path
    ) -> None:
        """A post-``mkstemp`` failure: a DIRECTORY planted at the marker path
        makes ``os.replace(tmp, marker)`` raise ``IsADirectoryError`` — the WARN
        fires, and the ``finally`` unlinks the surviving temp (no
        ``.write_epoch-*.tmp`` litter). No ``os`` monkeypatching needed."""
        marker = epoch_marker_path(tmp_path)
        marker.mkdir(parents=True, exist_ok=True)  # a dir where the file should land
        with capture_logs() as logs:
            bump_write_epoch(tmp_path)  # must NOT raise
        (warn,) = _events(logs, "lake_write_epoch.bump_failed")
        assert warn["log_level"] == "warning"
        leftover = [p for p in marker.parent.iterdir() if p.name.startswith(".write_epoch-")]
        assert leftover == []  # the finally unlinked the temp
