"""The lake write-epoch marker (DEC-LAKE-HEAL-STARTUP-EPOCH-001) — 100% of the leaf.

The O(1) cheap gate the L3 startup sweep consults before the O(files)
``lake_content_token`` walk: a lake-root-sibling marker refreshed to a fresh
opaque token on every content mutation. Pins: pure path helper, a bump moves the
value + is best-effort (an unwritable sidecar WARNs, never raises), a read fails
to ``None`` on missing/empty/unreadable — INCLUDING non-UTF-8 marker bytes
(review MINOR 1: ``UnicodeDecodeError`` escaped the contract pre-fix), the
non-fsync atomic replace's cleanup finally, and the ``bump_failed`` per-root
per-outage WARN throttle (review MINOR 21 — the #1630 log-volume class).
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

    def test_non_utf8_marker_reads_none(self, tmp_path: Path) -> None:
        """Review MINOR 1: NON-UTF-8 marker bytes are "unreadable" per the
        documented contract — ``UnicodeDecodeError`` is a ``ValueError``, NOT an
        ``OSError``, so pre-fix it ESCAPED ``read_write_epoch`` and crashed the
        L3 sweep's boot body on every boot (permanently inert healing) instead
        of the honest ``None`` fallthrough to the full token. Verified raising
        pre-fix."""
        dest = epoch_marker_path(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xfe\x9c junk \x80")  # 0xff is never valid UTF-8
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


class TestBumpFailedThrottle:
    """Review MINOR 21 (the #1630 log-volume class): ``bump_failed`` WARNs once
    per process per lake root per outage — a persistently-unwritable sidecar on
    the ~10⁵–10⁶-write prebuild path must not stream one WARN per write."""

    def test_bump_failed_warns_once_per_process_per_lake_root(self, tmp_path: Path) -> None:
        """Three failing bumps on ONE root → exactly ONE WARN (pre-fix: three).
        Verified failing pre-fix."""
        (tmp_path / ".lake_heal").write_text("not a dir", encoding="utf-8")
        with capture_logs() as logs:
            bump_write_epoch(tmp_path)
            bump_write_epoch(tmp_path)
            bump_write_epoch(tmp_path)
        assert len(_events(logs, "lake_write_epoch.bump_failed")) == 1

    def test_bump_failed_warn_is_keyed_per_lake_root(self, tmp_path: Path) -> None:
        """Two DISTINCT failing roots each get their own single WARN — the
        throttle key is the lake root, never a process-wide mute."""
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        for root in (root_a, root_b):
            root.mkdir()
            (root / ".lake_heal").write_text("not a dir", encoding="utf-8")
        with capture_logs() as logs:
            bump_write_epoch(root_a)
            bump_write_epoch(root_b)
        assert len(_events(logs, "lake_write_epoch.bump_failed")) == 2

    def test_successful_bump_rearms_the_throttle_for_a_later_outage(self, tmp_path: Path) -> None:
        """A SUCCESS between two outages re-arms the root: the second, distinct
        outage WARNs again (once) instead of hiding behind the first WARN — a
        long-lived dashboard process never goes permanently silent on a
        recurring sidecar fault."""
        blocker = tmp_path / ".lake_heal"
        blocker.write_text("not a dir", encoding="utf-8")
        with capture_logs() as logs_outage_one:
            bump_write_epoch(tmp_path)
        assert len(_events(logs_outage_one, "lake_write_epoch.bump_failed")) == 1
        blocker.unlink()  # outage one over
        bump_write_epoch(tmp_path)  # success → re-arms the throttle
        assert read_write_epoch(tmp_path) is not None
        # Outage two: a DIRECTORY planted at the marker path (os.replace fails).
        marker = epoch_marker_path(tmp_path)
        marker.unlink()
        marker.mkdir()
        with capture_logs() as logs_outage_two:
            bump_write_epoch(tmp_path)
            bump_write_epoch(tmp_path)  # still throttled WITHIN outage two
        assert len(_events(logs_outage_two, "lake_write_epoch.bump_failed")) == 1
