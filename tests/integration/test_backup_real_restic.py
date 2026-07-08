"""Real-restic roundtrip (skipped unless a restic binary is on PATH).

DEC-BACKUP-OFFBOX-001 §14: pins the ``--json`` summary / snapshots / ls
contracts the fake-restic stub mirrors, so fake-vs-real drift is caught
whenever a dev box has restic. Correctness NEVER depends on this test — CI
without restic skips it.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.backup.deadman import run_deadman_check
from brutex.backup.errors import EXIT_OK
from brutex.backup.restore_drill import run_restore_drill
from brutex.backup.run import run_backup
from brutex.backup.state import load_state, state_path
from brutex.config.backup_env import resolve_backup_env
from brutex.notifications.sweep_alerts import SweepAlerter

if TYPE_CHECKING:
    from brutex.notifications.telegram import TelegramEvent

pytestmark = pytest.mark.skipif(
    shutil.which("restic") is None, reason="real restic binary not installed"
)


class FakeNotifier:
    """Records every published TelegramEvent."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        """Record *event*."""
        self.published.append(event)


def test_real_init_backup_check_drill_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """local-dir repo: init → backup → verify → check FRESH → byte-verified drill."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lake = tmp_path / "lake"
    (lake / "bars" / "NSE").mkdir(parents=True)
    (lake / "bars" / "NSE" / "09.parquet").write_bytes(b"real-restic-bars")
    (lake / "audit").mkdir()
    (lake / "audit" / "chain.jsonl").write_bytes(b'{"seq": 1}\n')
    pw = tmp_path / "pw"
    pw.write_text("integration-password", encoding="utf-8")
    pw.chmod(0o600)
    monkeypatch.setenv("BRUTEX_BACKUP_REPO", str(tmp_path / "repo"))
    monkeypatch.setenv("BRUTEX_BACKUP_PASSWORD_FILE", str(pw))
    monkeypatch.delenv("BRUTEX_BACKUP_PASSWORD_COMMAND", raising=False)
    monkeypatch.setenv("BRUTEX_BACKUP_STATE_DIR", str(tmp_path / "bstate"))
    monkeypatch.setenv("BRUTEX_BACKUP_VERIFY_SUBSET", "auto")
    cfg = resolve_backup_env()
    notifier = FakeNotifier()
    alerter = SweepAlerter(notifier=notifier)
    now = datetime.now(UTC)

    # run: probes → init (fresh repo) → backup --json → check ×2 → forget.
    assert run_backup(cfg, lake_root=lake, alerter=alerter, clock=lambda: now) == EXIT_OK
    state = load_state(state_path(cfg))
    assert state is not None
    assert state.last_snapshot_id  # the REAL --json summary parse contract
    assert state.last_success_at == now
    assert state.verify_subset_cursor == 1

    # check (deep): the REAL snapshots --json --latest 1 --tag brutex contract.
    assert (
        run_deadman_check(cfg, deep=True, alerter=alerter, clock=lambda: now + timedelta(hours=1))
        == EXIT_OK
    )

    # drill: the REAL ls --json + restore contracts, byte-verified end to end.
    assert run_restore_drill(cfg, lake_root=lake, alerter=alerter) == EXIT_OK
    assert notifier.published == []  # nothing alarmed across the whole roundtrip
