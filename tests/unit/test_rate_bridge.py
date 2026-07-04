"""Unit tests for the cross-process pull-rate bridge (DEC-CLI-RATE-BRIDGE-001).

Drive :mod:`brutex.data.rate_bridge` directly: the publisher atomically writes the bridge
file, the reader round-trips it, and every damaged / absent / wrong-shape case degrades to
``None`` (never raises, never fabricates — Golden Rule #1). Full line + branch coverage of
the pure module; no clock / no network / no global state.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.rate_bridge import (
    RATE_BRIDGE_FILENAME,
    RATE_BRIDGE_SUBDIR,
    RateBridgeSnapshot,
    publish_from_snapshots,
    publish_rate_snapshot,
    rate_bridge_path,
    read_rate_snapshot,
)


def test_rate_bridge_path_is_under_dot_rate_sidecar(tmp_path: Path) -> None:
    """The bridge file lives at ``<lake_root>/.rate/pull_rate.json`` (never created here)."""
    path = rate_bridge_path(tmp_path)
    assert path == tmp_path / RATE_BRIDGE_SUBDIR / RATE_BRIDGE_FILENAME
    assert not path.exists()


def test_publish_then_read_round_trips_every_field(tmp_path: Path) -> None:
    """A published snapshot reads back with byte-identical field values + schema."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=6.4,
        current_rps=8.0,
        recent_minute=380,
        recent_429=2,
        ceiling_per_min=500,
        recent_1s=7,
        recent_600s=3800,
        recent_3600s=22800,
        wall_time=1_700_000_000.5,
        monotonic=12345.6,
    )
    snap = read_rate_snapshot(tmp_path)
    assert snap == RateBridgeSnapshot(
        schema=3,
        effective_rps=6.4,
        current_rps=8.0,
        recent_minute=380,
        recent_429=2,
        ceiling_per_min=500,
        recent_1s=7,
        recent_600s=3800,
        recent_3600s=22800,
        wall_time=1_700_000_000.5,
        monotonic=12345.6,
    )


def test_publish_creates_the_sidecar_dir(tmp_path: Path) -> None:
    """The ``.rate`` sidecar dir is created on first publish (mkdir parents=True)."""
    assert not (tmp_path / RATE_BRIDGE_SUBDIR).exists()
    publish_rate_snapshot(
        tmp_path,
        effective_rps=1.0,
        current_rps=2.0,
        recent_minute=3,
        recent_429=0,
        ceiling_per_min=-1,
        wall_time=1.0,
        monotonic=2.0,
    )
    assert (tmp_path / RATE_BRIDGE_SUBDIR).is_dir()
    assert rate_bridge_path(tmp_path).is_file()


def test_publish_is_atomic_overwrite(tmp_path: Path) -> None:
    """A second publish atomically replaces the first (last-writer-wins, no temp left)."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=1.0,
        current_rps=1.0,
        recent_minute=1,
        recent_429=0,
        ceiling_per_min=-1,
        wall_time=1.0,
        monotonic=1.0,
    )
    publish_rate_snapshot(
        tmp_path,
        effective_rps=9.0,
        current_rps=8.0,
        recent_minute=400,
        recent_429=5,
        ceiling_per_min=500,
        wall_time=2.0,
        monotonic=2.0,
    )
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.effective_rps == 9.0
    assert snap.recent_minute == 400
    # No leftover temp file in the sidecar dir (the atomic-write cleanup ran).
    leftovers = [p.name for p in (tmp_path / RATE_BRIDGE_SUBDIR).iterdir()]
    assert leftovers == [RATE_BRIDGE_FILENAME]


def test_publish_from_snapshots_maps_controller_and_meter_fields(tmp_path: Path) -> None:
    """``publish_from_snapshots`` maps the SAME ``.get(...)`` fields the panel observer does."""
    publish_from_snapshots(
        tmp_path,
        {
            "current_rps": 8.0,
            "recent_minute": 350,
            "recent_hits": 4,
            "ceiling_per_min": 500,
            "recent_1s": 7,
            "recent_600s": 3500,
            "recent_3600s": 21000,
        },
        {"effective_rps": 6.1},
        wall_time=100.0,
        monotonic=200.0,
    )
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.effective_rps == 6.1
    assert snap.current_rps == 8.0
    assert snap.recent_minute == 350
    assert snap.recent_429 == 4
    assert snap.ceiling_per_min == 500
    # The expanded-monitor trailing-window real counts (DEC-RATE-MONITOR-EXPAND-001).
    assert snap.recent_1s == 7
    assert snap.recent_600s == 3500
    assert snap.recent_3600s == 21000
    assert snap.wall_time == 100.0
    assert snap.monotonic == 200.0


def test_publish_from_snapshots_defaults_missing_keys_safely(tmp_path: Path) -> None:
    """Absent snapshot keys default to 0 / -1 (no KeyError) — empty mappings publish a zero row."""
    publish_from_snapshots(
        tmp_path,
        {},  # no controller fields at all
        {},  # no meter fields at all
        wall_time=1.0,
        monotonic=2.0,
    )
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.effective_rps == 0.0
    assert snap.current_rps == 0.0
    assert snap.recent_minute == 0
    assert snap.recent_429 == 0
    # ``ceiling_per_min`` absent → the -1 "no governor wired" sentinel (the None branch).
    assert snap.ceiling_per_min == -1
    # Absent trailing-window counts default to 0 (DEC-RATE-MONITOR-EXPAND-001) — no KeyError.
    assert snap.recent_1s == 0
    assert snap.recent_600s == 0
    assert snap.recent_3600s == 0


def test_publish_from_snapshots_maps_none_governor_to_minus_one(tmp_path: Path) -> None:
    """An explicit ``ceiling_per_min=None`` maps to the -1 sentinel (the panel-observer rule)."""
    publish_from_snapshots(
        tmp_path,
        {"current_rps": 5.0, "ceiling_per_min": None},
        {"effective_rps": 4.0},
        wall_time=1.0,
        monotonic=2.0,
    )
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.ceiling_per_min == -1


def test_read_missing_file_returns_none(tmp_path: Path) -> None:
    """No bridge file (no CLI pull has run) → None (the OSError/FileNotFound branch)."""
    assert read_rate_snapshot(tmp_path) is None


def test_read_corrupt_json_returns_none(tmp_path: Path) -> None:
    """A torn / non-JSON write → None (the JSONDecodeError branch), never a crash."""
    path = rate_bridge_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert read_rate_snapshot(tmp_path) is None


def test_read_non_object_json_returns_none(tmp_path: Path) -> None:
    """A valid-JSON but non-object payload (e.g. a list) → None (the not-a-dict branch)."""
    path = rate_bridge_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_rate_snapshot(tmp_path) is None


def test_read_unrecognised_schema_returns_none(tmp_path: Path) -> None:
    """A future / unknown schema version → None (forward-safe, the schema-mismatch branch)."""
    path = rate_bridge_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 999,
        "effective_rps": 1.0,
        "current_rps": 1.0,
        "recent_minute": 1,
        "recent_429": 0,
        "ceiling_per_min": -1,
        "wall_time": 1.0,
        "monotonic": 1.0,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_rate_snapshot(tmp_path) is None


def test_read_missing_field_returns_none(tmp_path: Path) -> None:
    """A correct-schema but field-incomplete payload → None (the KeyError branch)."""
    path = rate_bridge_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Right schema, but ``effective_rps`` (and others) absent.
    path.write_text(json.dumps({"schema": 3, "current_rps": 1.0}), encoding="utf-8")
    assert read_rate_snapshot(tmp_path) is None


def test_read_wrong_typed_field_returns_none(tmp_path: Path) -> None:
    """A correct-schema payload with a non-numeric field → None (the ValueError branch)."""
    path = rate_bridge_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 3,
        "effective_rps": "not-a-number",
        "current_rps": 1.0,
        "recent_minute": 1,
        "recent_429": 0,
        "ceiling_per_min": -1,
        "recent_1s": 1,
        "recent_600s": 1,
        "recent_3600s": 1,
        "wall_time": 1.0,
        "monotonic": 1.0,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_rate_snapshot(tmp_path) is None


def test_read_unreadable_file_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-FileNotFound OSError on read (e.g. permission/IO) → None (the OSError branch)."""
    path = rate_bridge_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}")

    def _boom(self: Path) -> bytes:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    assert read_rate_snapshot(tmp_path) is None


def test_publish_enospc_maps_to_lake_write_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full-disk ENOSPC at the bridge write maps to LakeWriteHalt (lake_write_guard)."""

    def _enospc(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    # The dir already exists so the mkdir is a no-op; the atomic write itself hits ENOSPC.
    (tmp_path / RATE_BRIDGE_SUBDIR).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("brutex.data.rate_bridge.atomic_write_simple", _enospc)
    with pytest.raises(LakeWriteHalt) as excinfo:
        publish_rate_snapshot(
            tmp_path,
            effective_rps=1.0,
            current_rps=1.0,
            recent_minute=1,
            recent_429=0,
            ceiling_per_min=-1,
            wall_time=1.0,
            monotonic=1.0,
        )
    assert excinfo.value.reason == "disk_full"


def test_snapshot_to_json_bytes_is_compact_utf8(tmp_path: Path) -> None:
    """``to_json_bytes`` is compact UTF-8 JSON the reader round-trips."""
    snap = RateBridgeSnapshot(
        schema=3,
        effective_rps=1.5,
        current_rps=2.5,
        recent_minute=10,
        recent_429=1,
        ceiling_per_min=500,
        recent_1s=2,
        recent_600s=95,
        recent_3600s=540,
        wall_time=9.0,
        monotonic=8.0,
    )
    raw = snap.to_json_bytes()
    assert b", " not in raw  # compact separators (no spaces after commas)
    decoded = json.loads(raw)
    assert decoded["effective_rps"] == 1.5
    assert decoded["schema"] == 3
    assert decoded["recent_600s"] == 95


# ── pull_active flag (schema v3, DEC-CLI-BRIDGE-PULL-ACTIVE-001) ─────────────


@pytest.mark.parametrize("flag", [True, False, None])
def test_pull_active_round_trips(tmp_path: Path, flag: bool | None) -> None:
    """``pull_active`` (True / False / None) round-trips through publish → read."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=1.0,
        current_rps=2.0,
        recent_minute=3,
        recent_429=0,
        ceiling_per_min=500,
        wall_time=1.0,
        monotonic=2.0,
        pull_active=flag,
    )
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.pull_active is flag


@pytest.mark.parametrize("flag", [True, False])
def test_publish_from_snapshots_forwards_pull_active(tmp_path: Path, flag: bool) -> None:
    publish_from_snapshots(
        tmp_path,
        {"current_rps": 5.0},
        {"effective_rps": 4.0},
        wall_time=1.0,
        monotonic=2.0,
        pull_active=flag,
    )
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.pull_active is flag


def test_publish_from_snapshots_defaults_pull_active_none(tmp_path: Path) -> None:
    """Existing (flagless) call sites stay byte-compatible — the flag defaults None."""
    publish_from_snapshots(tmp_path, {}, {}, wall_time=1.0, monotonic=2.0)
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.pull_active is None


def test_read_stale_v2_file_is_ignored(tmp_path: Path) -> None:
    """A pre-#3 schema-2 file → None (the graceful old-file path — never mis-decoded)."""
    path = rate_bridge_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 2,
        "effective_rps": 1.0,
        "current_rps": 1.0,
        "recent_minute": 1,
        "recent_429": 0,
        "ceiling_per_min": -1,
        "recent_1s": 1,
        "recent_600s": 1,
        "recent_3600s": 1,
        "wall_time": 1.0,
        "monotonic": 1.0,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert read_rate_snapshot(tmp_path) is None


@pytest.mark.parametrize("raw", ["yes", 1, [], {}])
def test_read_non_bool_pull_active_reads_none(tmp_path: Path, raw: object) -> None:
    """A hand-edited non-bool ``pull_active`` gracefully reads None (never a crash)."""
    path = rate_bridge_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 3,
        "effective_rps": 1.0,
        "current_rps": 1.0,
        "recent_minute": 1,
        "recent_429": 0,
        "ceiling_per_min": -1,
        "recent_1s": 1,
        "recent_600s": 1,
        "recent_3600s": 1,
        "wall_time": 1.0,
        "monotonic": 1.0,
        "pull_active": raw,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.pull_active is None


def test_read_missing_pull_active_key_reads_none(tmp_path: Path) -> None:
    """A v3 file WITHOUT the key (hand-edited) reads pull_active=None — graceful default."""
    path = rate_bridge_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 3,
        "effective_rps": 1.0,
        "current_rps": 1.0,
        "recent_minute": 1,
        "recent_429": 0,
        "ceiling_per_min": -1,
        "recent_1s": 1,
        "recent_600s": 1,
        "recent_3600s": 1,
        "wall_time": 1.0,
        "monotonic": 1.0,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    snap = read_rate_snapshot(tmp_path)
    assert snap is not None
    assert snap.pull_active is None
