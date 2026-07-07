"""Unit tests for the DEC-BACKUP-OFFBOX-001 :class:`SweepAlerter` additions.

The four backup alert methods (`backup_failed` / `backup_degraded` /
`backup_overdue` / `backup_repo_corrupt`) + the module-level best-effort
``emit_backup_failed_alert`` — extending the existing sweep-alerts suite
shape (recording fake notifier / None resolver / MarkdownV2 probe text).
Includes the zero-enum-surface pin: NO new ``TelegramEventKind`` member was
added (§7.3 — the per-process dedupe window is structurally inert for
one-shot launchd processes, so everything rides PHASE_BLOCKED).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog.testing
from brutex.notifications.sweep_alerts import SweepAlerter, emit_backup_failed_alert
from brutex.notifications.telegram import TelegramEventKind, TelegramSeverity

if TYPE_CHECKING:
    from brutex.notifications.telegram import TelegramEvent


class FakeNotifier:
    """Captures every published TelegramEvent."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        """Record *event*."""
        self.published.append(event)


class TestBackupFailed:
    def test_backup_failed_kind_severity_dedupe_and_escaped_message(self) -> None:
        fake = FakeNotifier()
        assert (
            SweepAlerter(notifier=fake).backup_failed(
                reason="restic_missing", detail="install it (M1)."
            )
            is True
        )
        event = fake.published[0]
        assert event.kind == TelegramEventKind.PHASE_BLOCKED
        assert event.severity == TelegramSeverity.CRITICAL
        assert event.dedupe_key == "backup-failed:restic_missing"
        assert "restic\\_missing" in event.message  # MarkdownV2-escaped reason
        assert "install it \\(M1\\)\\." in event.message  # MarkdownV2-escaped detail
        assert "brutex backup status" in event.message

    def test_backup_failed_no_detail_omits_extra(self) -> None:
        """The empty-detail branch renders no doubled space."""
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).backup_failed(reason="config")
        assert "config\\.  " not in fake.published[0].message
        assert "config\\." in fake.published[0].message

    def test_backup_failed_disabled_returns_false(self) -> None:
        assert SweepAlerter(resolver=lambda: None).backup_failed(reason="config") is False

    def test_backup_failed_partial_streak_body_is_honest(self) -> None:
        """Round-4 NIT: on the partial_streak arm success was ALREADY stamped
        (run.py step 9 precedes the escalation) so the dead-man reads FRESH —
        the message must NOT claim the dead-man will keep re-alerting, and
        must say the snapshot succeeded/was stamped."""
        fake = FakeNotifier()
        assert (
            SweepAlerter(notifier=fake).backup_failed(
                reason="partial_streak", detail="3 consecutive partial (rc-3) backups."
            )
            is True
        )
        event = fake.published[0]
        assert event.dedupe_key == "backup-failed:partial_streak"
        assert event.severity == TelegramSeverity.CRITICAL
        assert "re\\-alerting" not in event.message
        assert "SUCCEEDED" in event.message
        assert "stays quiet" in event.message
        assert "3 consecutive partial" in event.message


class TestBackupDegraded:
    def test_backup_degraded_kind_severity_dedupe_and_escaped_message(self) -> None:
        fake = FakeNotifier()
        assert (
            SweepAlerter(notifier=fake).backup_degraded(
                aspect="maintenance", detail="restic forget failed (rc=1) - data stamp stands"
            )
            is True
        )
        event = fake.published[0]
        assert event.kind == TelegramEventKind.PHASE_BLOCKED
        assert event.dedupe_key == "backup-degraded:maintenance"
        assert "\\(rc\\=1\\) \\- data stamp stands" in event.message
        assert "NOT" not in event.message.split("DEGRADED")[0]  # header stays calm

    def test_backup_degraded_is_warn_not_critical(self) -> None:
        """R4: degraded rides PHASE_BLOCKED with severity=WARN — never a page."""
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).backup_degraded(aspect="partial", detail="streak 1")
        assert fake.published[0].severity == TelegramSeverity.WARN

    def test_backup_degraded_disabled_returns_false(self) -> None:
        alerter = SweepAlerter(resolver=lambda: None)
        assert alerter.backup_degraded(aspect="partial", detail="x") is False


class TestBackupOverdue:
    def test_backup_overdue_kind_severity_dedupe_and_escaped_message(self) -> None:
        fake = FakeNotifier()
        assert (
            SweepAlerter(notifier=fake).backup_overdue(
                reason="overdue", hours_since_success=41.5, threshold_hours=30.0
            )
            is True
        )
        event = fake.published[0]
        assert event.kind == TelegramEventKind.PHASE_BLOCKED
        assert event.severity == TelegramSeverity.CRITICAL
        assert event.dedupe_key == "backup-overdue:overdue"
        assert "41\\.5 h ago" in event.message
        assert "threshold 30 h" in event.message
        assert "NOT" in event.message  # "NOT re-pullable" honesty

    def test_backup_overdue_never_age_display(self) -> None:
        """hours_since_success=None reads 'never' (F12 bootstrap nagging)."""
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).backup_overdue(
            reason="never_succeeded", hours_since_success=None, threshold_hours=30.0
        )
        assert "backup never" in fake.published[0].message
        assert fake.published[0].dedupe_key == "backup-overdue:never_succeeded"

    def test_backup_overdue_disabled_returns_false(self) -> None:
        alerter = SweepAlerter(resolver=lambda: None)
        assert (
            alerter.backup_overdue(reason="overdue", hours_since_success=40.0, threshold_hours=30.0)
            is False
        )


class TestBackupRepoCorrupt:
    def test_backup_repo_corrupt_kind_severity_dedupe_and_escaped_message(self) -> None:
        fake = FakeNotifier()
        assert (
            SweepAlerter(notifier=fake).backup_repo_corrupt(detail="check failed: /lake/x.pq")
            is True
        )
        event = fake.published[0]
        assert event.kind == TelegramEventKind.PHASE_BLOCKED
        assert event.severity == TelegramSeverity.CRITICAL
        assert event.dedupe_key == "backup-repo-corrupt"
        assert "check failed: /lake/x\\.pq" in event.message
        assert "Do NOT run automated repair" in event.message
        # The nightly-verify arm (default): NO success is stamped, so the
        # dead-man really does go OVERDUE and escalate — the claim is honest.
        assert "keeps escalating" in event.message
        assert "ONLY automated signal" not in event.message

    def test_backup_repo_corrupt_drill_arm_says_deadman_stays_quiet(self) -> None:
        """Round-5 NIT pin: the DRILL-mismatch arm must NOT promise dead-man
        escalation — nightly backups keep stamping success (the rotating
        read-data subset can miss the corrupt pack for ~56 nights, the deep
        check id-matches), so that page is the only automated signal."""
        fake = FakeNotifier()
        assert (
            SweepAlerter(notifier=fake).backup_repo_corrupt(
                detail="drill mismatch: /lake/x.pq", source="drill"
            )
            is True
        )
        event = fake.published[0]
        assert event.dedupe_key == "backup-repo-corrupt"
        assert "keeps escalating" not in event.message
        assert "dead\\-man stays QUIET" in event.message
        assert "ONLY automated signal" in event.message

    def test_backup_repo_corrupt_disabled_returns_false(self) -> None:
        assert SweepAlerter(resolver=lambda: None).backup_repo_corrupt(detail="x") is False


class TestEmitBackupFailedAlert:
    def test_emit_backup_failed_alert_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The module-level helper delegates reason+detail to a fresh alerter."""
        captured: list[tuple[str, str]] = []

        class _FakeAlerter:
            def backup_failed(self, *, reason: str, detail: str = "") -> bool:
                captured.append((reason, detail))
                return True

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
        emit_backup_failed_alert("state_write", "ENOSPC")
        assert captured == [("state_write", "ENOSPC")]

    def test_emit_backup_failed_alert_swallows_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A construction/publish blow-up is swallowed + logged — NEVER raises."""

        class _BoomAlerter:
            def __init__(self) -> None:
                raise RuntimeError("notifier construction blew up")

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _BoomAlerter)
        with structlog.testing.capture_logs() as logs:
            emit_backup_failed_alert("config")  # must not raise
        assert any(entry["event"] == "backup_failed_alert_failed" for entry in logs)


class TestZeroEnumSurface:
    def test_no_new_telegram_event_kinds(self) -> None:
        """§7.3 pin: the TelegramEventKind member set is UNCHANGED — every backup
        alert rides PHASE_BLOCKED (the per-process dedupe cache is structurally
        inert for one-shot launchd processes; the durable bound is deadman.json)."""
        assert {member.name for member in TelegramEventKind} == {
            "RUN_START",
            "PHASE_START",
            "PHASE_PROGRESS",
            "PHASE_COMPLETE",
            "PHASE_BLOCKED",
            "QUARANTINE_NEW",
            "RATE_LIMIT_DOWNSHIFT",
            "RATE_LIMIT_RECOVERED",
            "WORKER_HUNG",
            "SYMBOL_WINDOW_FAILURE",
            "AUTH_RENEW_FAILED",
            "CHILD_RELAUNCHED",
            "SUPERVISOR_GAVE_UP",
            "RUN_HEARTBEAT",
            "RUN_COMPLETE",
            "TEST",
        }
