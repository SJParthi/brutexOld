"""Unit tests for :mod:`brutex.data.greeks_revision` (DEC-GREEKS-BULK-REENRICH-001).

Fix round 8 — the ONE shared cross-process Greeks-overlay freshness-marker
touch, extracted from the backfill so the merge-absorbed
DEC-CLI-GREEKS-ENRICH-001 enrich pass (the SECOND sanctioned ledger-less
overlay writer) stops leaving a running dashboard's ``lake_revision``-probed
caches stale. 100% line + branch: the pure path helper, the atomic payload
write (fields + ``updated_at``, sorted keys, replace-on-retouch), and the
best-effort OSError → one caller-scoped WARN fence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from brutex.data.greeks_revision import (
    GREEKS_SIDECAR_DIR_NAME,
    REVISION_MARKER_FILE_NAME,
    revision_marker_path,
    touch_greeks_revision_marker,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    return [entry for entry in logs if entry["event"] == name]


class TestRevisionMarkerPath:
    def test_path_shape_matches_the_lake_revision_pinned_literals(self, tmp_path: Path) -> None:
        """The marker lives at ``<lake>/.greeks_backfill/revision.json`` —
        exactly the component path ``lake_revision`` stats (pinned there too)."""
        assert revision_marker_path(tmp_path) == tmp_path / ".greeks_backfill" / "revision.json"
        assert GREEKS_SIDECAR_DIR_NAME == ".greeks_backfill"
        assert REVISION_MARKER_FILE_NAME == "revision.json"


class TestTouchGreeksRevisionMarker:
    def test_touch_creates_parent_and_writes_fields_plus_updated_at(self, tmp_path: Path) -> None:
        """One touch materialises the sidecar dir + the JSON payload verbatim."""
        touch_greeks_revision_marker(
            tmp_path,
            fields={"writer": "greeks_enrich_pass", "bars_written": 3},
            failure_event="fno.greeks_enrich.revision_marker_failed",
        )
        marker = revision_marker_path(tmp_path)
        assert marker.is_file()
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload["writer"] == "greeks_enrich_pass"
        assert payload["bars_written"] == 3
        assert isinstance(payload["updated_at"], float)

    def test_retouch_replaces_the_payload_atomically(self, tmp_path: Path) -> None:
        """A second touch REPLACES the marker (os.replace) — latest write wins."""
        touch_greeks_revision_marker(
            tmp_path, fields={"backfill_id": "a", "bars_written": 1}, failure_event="x.failed"
        )
        touch_greeks_revision_marker(
            tmp_path, fields={"backfill_id": "b", "bars_written": 2000}, failure_event="x.failed"
        )
        payload = json.loads(revision_marker_path(tmp_path).read_text(encoding="utf-8"))
        assert payload["backfill_id"] == "b" and payload["bars_written"] == 2000
        # No temp-file droppings beside the marker (the atomic-core cleanup).
        assert [p.name for p in revision_marker_path(tmp_path).parent.iterdir()] == [
            "revision.json"
        ]

    def test_oserror_logs_the_caller_scoped_event_and_never_raises(self, tmp_path: Path) -> None:
        """A stray FILE at the sidecar dir (ENOTDIR/EEXIST) is fenced to ONE
        WARN carrying the CALLER's event name — the marker is a freshness
        signal, never data, so it can never fail the writing run."""
        (tmp_path / GREEKS_SIDECAR_DIR_NAME).write_text("stray", encoding="utf-8")
        with capture_logs() as logs:
            touch_greeks_revision_marker(
                tmp_path,
                fields={"writer": "greeks_enrich_pass", "bars_written": 7},
                failure_event="fno.greeks_enrich.revision_marker_failed",
            )
        (warn,) = _events(logs, "fno.greeks_enrich.revision_marker_failed")
        assert str(revision_marker_path(tmp_path)) == warn["path"]
        assert warn["error"]
