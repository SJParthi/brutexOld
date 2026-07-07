"""Unit tests for :mod:`brutex.backup.state` (DEC-BACKUP-OFFBOX-001 §5.1).

Two files, single-writer-per-file: the runner-owned ``state.json`` (any parse
defect → :class:`BackupStateError` — the F13 hard-alarm taxonomy) and the
checker-owned ``deadman.json`` throttle sidecar (missing/corrupt → EMPTY +
WARN — fail-toward-alarm). Both write atomically 0600 with byte-stable
sorted-keys JSON (the golden below is the mutation killer).
"""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog.testing
from brutex.backup.errors import BackupStateError
from brutex.backup.state import (
    STATE_SCHEMA_VERSION,
    BackupState,
    DeadmanState,
    canary_dir,
    deadman_path,
    drill_tmp_dir,
    empty_backup_state,
    load_deadman,
    load_state,
    lock_path,
    save_deadman,
    save_state,
    state_path,
)
from brutex.config.backup_env import resolve_backup_env

_UTC_STAMP = datetime(2026, 7, 6, 22, 19, 41, tzinfo=UTC)


def _full_state() -> BackupState:
    """One fully-populated state (every optional field set)."""
    return BackupState(
        schema=STATE_SCHEMA_VERSION,
        running_since=datetime(2026, 7, 6, 22, 5, 2, tzinfo=UTC),
        last_attempt_at=datetime(2026, 7, 6, 22, 5, 2, tzinfo=UTC),
        last_success_at=_UTC_STAMP,
        last_snapshot_id="ab12cd34",
        last_failure_at=datetime(2026, 7, 5, 3, 0, 0, tzinfo=UTC),
        last_failure_reason="timeout",
        consecutive_failures=2,
        consecutive_partials=1,
        maintenance_ok=False,
        verify_subset_cursor=14,
        lake_device=16777233,
    )


class TestBackupStateRoundtrip:
    def test_state_roundtrip_all_set(self) -> None:
        state = _full_state()
        assert BackupState.from_json(state.to_json()) == state

    def test_state_roundtrip_all_none(self) -> None:
        state = empty_backup_state()
        assert BackupState.from_json(state.to_json()) == state

    def test_to_json_sorted_keys_golden(self) -> None:
        """Byte-stable golden (§5.1 example shape) — the designed mutant killer."""
        state = BackupState(
            schema=1,
            running_since=None,
            last_attempt_at=datetime(2026, 7, 6, 22, 5, 2, tzinfo=UTC),
            last_success_at=_UTC_STAMP,
            last_snapshot_id="ab12cd34",
            last_failure_at=None,
            last_failure_reason=None,
            consecutive_failures=0,
            consecutive_partials=0,
            maintenance_ok=True,
            verify_subset_cursor=14,
            lake_device=16777233,
        )
        assert state.to_json() == (
            '{"consecutive_failures": 0, "consecutive_partials": 0, '
            '"lake_device": 16777233, "last_attempt_at": "2026-07-06T22:05:02Z", '
            '"last_failure_at": null, "last_failure_reason": null, '
            '"last_snapshot_id": "ab12cd34", "last_success_at": "2026-07-06T22:19:41Z", '
            '"maintenance_ok": true, "running_since": null, "schema": 1, '
            '"verify_subset_cursor": 14}'
        )

    def test_non_utc_stamp_normalised_to_utc_z(self) -> None:
        """A +05:30 aware stamp serialises as the equivalent UTC ``…Z``."""
        ist = timezone(timedelta(hours=5, minutes=30))
        state = replace(
            empty_backup_state(), last_success_at=datetime(2026, 7, 7, 3, 49, 41, tzinfo=ist)
        )
        assert '"last_success_at": "2026-07-06T22:19:41Z"' in state.to_json()


class TestBackupStateParseDefects:
    def test_bad_json_raises(self) -> None:
        with pytest.raises(BackupStateError, match="not valid JSON"):
            BackupState.from_json("{nope")

    def test_non_object_payload_raises(self) -> None:
        with pytest.raises(BackupStateError, match="must be a JSON object"):
            BackupState.from_json("[1, 2]")

    def test_wrong_schema_raises(self) -> None:
        raw = json.loads(_full_state().to_json())
        raw["schema"] = 99
        with pytest.raises(BackupStateError, match="schema drift"):
            BackupState.from_json(json.dumps(raw))

    def test_missing_field_raises(self) -> None:
        raw = json.loads(_full_state().to_json())
        del raw["last_success_at"]
        with pytest.raises(BackupStateError, match="missing required field"):
            BackupState.from_json(json.dumps(raw))

    def test_non_iso_stamp_raises(self) -> None:
        raw = json.loads(_full_state().to_json())
        raw["last_success_at"] = "yesterday-ish"
        with pytest.raises(BackupStateError, match="unparseable timestamp"):
            BackupState.from_json(json.dumps(raw))

    def test_naive_datetime_raises(self) -> None:
        raw = json.loads(_full_state().to_json())
        raw["last_success_at"] = "2026-07-06T22:19:41"
        with pytest.raises(BackupStateError, match="naive"):
            BackupState.from_json(json.dumps(raw))

    def test_non_string_stamp_raises(self) -> None:
        raw = json.loads(_full_state().to_json())
        raw["last_success_at"] = 12345
        with pytest.raises(BackupStateError, match="ISO-8601 string"):
            BackupState.from_json(json.dumps(raw))

    def test_bool_counter_raises(self) -> None:
        """JSON true is NOT an int counter (the bool-is-int trap)."""
        raw = json.loads(_full_state().to_json())
        raw["consecutive_failures"] = True
        with pytest.raises(BackupStateError, match="must be an integer"):
            BackupState.from_json(json.dumps(raw))

    def test_non_string_snapshot_id_raises(self) -> None:
        raw = json.loads(_full_state().to_json())
        raw["last_snapshot_id"] = 7
        with pytest.raises(BackupStateError, match="string or null"):
            BackupState.from_json(json.dumps(raw))

    def test_non_int_lake_device_raises(self) -> None:
        raw = json.loads(_full_state().to_json())
        raw["lake_device"] = "sda1"
        with pytest.raises(BackupStateError, match="integer or null"):
            BackupState.from_json(json.dumps(raw))

    def test_bool_lake_device_raises(self) -> None:
        raw = json.loads(_full_state().to_json())
        raw["lake_device"] = True
        with pytest.raises(BackupStateError, match="integer or null"):
            BackupState.from_json(json.dumps(raw))

    def test_non_bool_maintenance_ok_raises(self) -> None:
        raw = json.loads(_full_state().to_json())
        raw["maintenance_ok"] = "yes"
        with pytest.raises(BackupStateError, match="must be a boolean"):
            BackupState.from_json(json.dumps(raw))


class TestLoadSaveState:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_state(tmp_path / "absent" / "state.json") is None

    def test_unreadable_file_raises_state_error(self, tmp_path: Path) -> None:
        """A non-FileNotFound OSError (here: a directory at the path) → BackupStateError."""
        target = tmp_path / "state.json"
        target.mkdir()
        with pytest.raises(BackupStateError, match="failed to read"):
            load_state(target)

    def test_bit_rot_non_utf8_byte_raises_state_error(self, tmp_path: Path) -> None:
        """Round-4 MAJOR: a single flipped high bit (0x80-0xFF) in the pure-ASCII
        state.json raises UnicodeDecodeError — a ValueError, NOT an OSError —
        which must map to BackupStateError, never escape raw (F13 bit-rot)."""
        target = tmp_path / "state.json"
        target.write_bytes(b'{"schema": 1\xff}')
        with pytest.raises(BackupStateError, match="failed to read"):
            load_state(target)

    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "state.json"
        save_state(target, _full_state())
        assert load_state(target) == _full_state()

    def test_save_mode_0600(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        save_state(target, _full_state())
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_save_atomic_tmp_cleaned_on_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An injected os.replace raiser leaves NO .brutex-write-* temp behind."""

        def _raise(src: object, dst: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _raise)
        target = tmp_path / "state.json"
        with pytest.raises(OSError, match="No space left"):
            save_state(target, _full_state())
        assert not target.exists()
        assert list(tmp_path.glob(".brutex-write-*")) == []


class TestDeadmanState:
    def test_deadman_roundtrip(self) -> None:
        sidecar = DeadmanState(
            schema=STATE_SCHEMA_VERSION,
            last_overdue_alert_at=datetime(2026, 7, 5, 4, 0, 0, tzinfo=UTC),
            last_deep_check_at=datetime(2026, 7, 6, 9, 0, 11, tzinfo=UTC),
        )
        assert DeadmanState.from_json(sidecar.to_json()) == sidecar

    def test_deadman_golden(self) -> None:
        sidecar = DeadmanState(
            last_overdue_alert_at=datetime(2026, 7, 5, 4, 0, 0, tzinfo=UTC),
            last_deep_check_at=datetime(2026, 7, 6, 9, 0, 11, tzinfo=UTC),
        )
        assert sidecar.to_json() == (
            '{"last_deep_check_at": "2026-07-06T09:00:11Z", '
            '"last_overdue_alert_at": "2026-07-05T04:00:00Z", "schema": 1}'
        )

    def test_deadman_missing_returns_empty(self, tmp_path: Path) -> None:
        assert load_deadman(tmp_path / "absent.json") == DeadmanState()

    def test_deadman_corrupt_returns_empty_plus_warn(self, tmp_path: Path) -> None:
        """Corrupt sidecar → EMPTY + ONE WARN — can only cause an EXTRA alert (F13)."""
        target = tmp_path / "deadman.json"
        target.write_text("{broken", encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            assert load_deadman(target) == DeadmanState()
        assert [entry["event"] for entry in logs] == ["backup.deadman.sidecar_corrupt"]

    def test_deadman_oserror_returns_empty_plus_warn(self, tmp_path: Path) -> None:
        """An OSError read (directory at the path) also degrades to EMPTY + WARN."""
        target = tmp_path / "deadman.json"
        target.mkdir()
        with structlog.testing.capture_logs() as logs:
            assert load_deadman(target) == DeadmanState()
        assert [entry["event"] for entry in logs] == ["backup.deadman.sidecar_corrupt"]

    def test_deadman_non_utf8_returns_empty_plus_warn(self, tmp_path: Path) -> None:
        """Round-4 MAJOR: a non-UTF-8 sidecar byte (bit-rot) rides the same
        corrupt→EMPTY (+ ONE WARN) arm — its own docstring contract."""
        target = tmp_path / "deadman.json"
        target.write_bytes(b'{"schema": 1\xfe}')
        with structlog.testing.capture_logs() as logs:
            assert load_deadman(target) == DeadmanState()
        assert [entry["event"] for entry in logs] == ["backup.deadman.sidecar_corrupt"]
        assert logs[0]["error_type"] == "UnicodeDecodeError"

    def test_deadman_bad_json_raises_in_from_json(self) -> None:
        with pytest.raises(BackupStateError, match="not valid JSON"):
            DeadmanState.from_json("nope{")

    def test_deadman_non_object_raises_in_from_json(self) -> None:
        with pytest.raises(BackupStateError, match="must be a JSON object"):
            DeadmanState.from_json('"just a string"')

    def test_save_deadman_mode_0600(self, tmp_path: Path) -> None:
        target = tmp_path / "deadman.json"
        save_deadman(target, DeadmanState())
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert load_deadman(target) == DeadmanState()


class TestDerivedPaths:
    def test_all_derived_paths(self, tmp_path: Path) -> None:
        """state/deadman/lock/canary/drill-tmp all hang off cfg.state_dir."""
        cfg = resolve_backup_env({"BRUTEX_BACKUP_STATE_DIR": str(tmp_path / "sd")})
        base = tmp_path / "sd"
        assert state_path(cfg) == base / "state.json"
        assert deadman_path(cfg) == base / "deadman.json"
        assert lock_path(cfg) == base / "backup.lock"
        assert canary_dir(cfg) == base / "canary"
        assert drill_tmp_dir(cfg) == base / "drill-tmp"
