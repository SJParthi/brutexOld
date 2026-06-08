"""Integration test for the retry-storm give-up guard (DEC-RATE-STORM-WATCHDOG-001).

Wires the real pieces end-to-end with a **fake clock** + a **fake Telegram
notifier** and a **real hash-chained** :class:`~brutex.orchestrator.audit.AuditWriter`::

    RetryStormWatchdog → SweepAlerter.symbol_failure_callback() → FakeNotifier
                       ↘ AuditWriter.append (real JSONL hash chain)

Asserts the three contract behaviours the task names:

1. give-up fires exactly at the N-minute boundary,
2. the symbol alert fires **once per symbol, not per window**,
3. give-up + alert are written as audit rows whose chain verifies.
"""

from __future__ import annotations

from pathlib import Path

from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.notifications.telegram import TelegramEvent, TelegramEventKind
from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain
from brutex.orchestrator.watchdog import (
    AUDIT_ACTION_GIVE_UP,
    AUDIT_ACTION_SYMBOL_FAILURE,
    build_retry_storm_watchdog,
)


class FakeClock:
    """Deterministic monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeNotifier:
    """Records every published :class:`TelegramEvent` (the `publish` surface)."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        self.published.append(event)


def test_storm_gives_up_at_boundary_alerts_once_and_audits(tmp_path: Path) -> None:
    clock = FakeClock()
    notifier = FakeNotifier()
    audit_path = tmp_path / "audit" / "retry-storm.jsonl"

    # A 100-minute give-up boundary; 1 symbol × 3 windows under a continuous storm.
    minutes = 100
    with AuditWriter.open(audit_path) as audit:
        watchdog = build_retry_storm_watchdog(
            max_retry_minutes=minutes,
            audit=audit,
            on_symbol_failure=SweepAlerter(notifier=notifier).symbol_failure_callback(),
            clock=clock,
        )
        watchdog.register_fanout(["NIFTY"], ["w0", "w1", "w2"])

        # Arm every window's streak at t=0.
        for window in ("w0", "w1", "w2"):
            assert watchdog.record_failure("NIFTY", window) is False

        # One second BELOW the boundary — nothing gives up yet.
        clock.t = minutes * 60 - 1
        for window in ("w0", "w1", "w2"):
            assert watchdog.record_failure("NIFTY", window) is False
        assert notifier.published == []  # no alert before any give-up

        # EXACTLY at the boundary — every window gives up; the LAST one (100%
        # window-failure) fires the single symbol alert.
        clock.t = minutes * 60
        assert watchdog.record_failure("NIFTY", "w0") is True
        assert watchdog.record_failure("NIFTY", "w1") is True
        assert len(notifier.published) == 0  # 2/3 — still no symbol alert
        assert watchdog.record_failure("NIFTY", "w2") is True  # 3/3 → alert

    # (1) + (2): exactly one CRITICAL symbol alert for NIFTY.
    assert len(notifier.published) == 1
    alert = notifier.published[0]
    assert alert.kind == TelegramEventKind.SYMBOL_WINDOW_FAILURE
    assert alert.dedupe_key == "NIFTY"
    assert "NIFTY" in alert.message

    # (3): the real audit chain verifies and holds the give-up + symbol rows.
    assert verify_chain(audit_path) == 4  # 3 give-ups + 1 symbol-failure
    entries = read_entries(audit_path)
    actions = [e.action for e in entries]
    assert actions == [
        AUDIT_ACTION_GIVE_UP,
        AUDIT_ACTION_GIVE_UP,
        AUDIT_ACTION_GIVE_UP,
        AUDIT_ACTION_SYMBOL_FAILURE,
    ]
    give_up_subjects = {e.subject for e in entries if e.action == AUDIT_ACTION_GIVE_UP}
    assert give_up_subjects == {"NIFTY|w0", "NIFTY|w1", "NIFTY|w2"}
    symbol_row = entries[-1]
    assert symbol_row.subject == "NIFTY"
    assert symbol_row.payload == {"failed_windows": 3, "total_windows": 3}
    # Every give-up row records the time-based reason + the boundary seconds.
    for entry in entries[:3]:
        assert entry.payload["reason"] == "timeout"
        assert entry.payload["max_retry_seconds"] == float(minutes * 60)


def test_partial_failure_does_not_alert_and_audits_only_give_ups(tmp_path: Path) -> None:
    """A symbol that does NOT reach 100% window-failure must never alert."""
    notifier = FakeNotifier()
    audit_path = tmp_path / "audit" / "partial.jsonl"

    with AuditWriter.open(audit_path) as audit:
        watchdog = build_retry_storm_watchdog(
            max_retry_minutes=30,
            audit=audit,
            on_symbol_failure=SweepAlerter(notifier=notifier).symbol_failure_callback(),
        )
        watchdog.register_fanout(["BANKNIFTY"], ["w0", "w1"])
        # Only one of two windows terminally fails → 50% → no symbol alert.
        assert watchdog.record_terminal_failure("BANKNIFTY", "w0") is True

    assert notifier.published == []
    assert verify_chain(audit_path) == 1
    (entry,) = read_entries(audit_path)
    assert entry.action == AUDIT_ACTION_GIVE_UP
    assert entry.subject == "BANKNIFTY|w0"
    assert entry.payload["reason"] == "terminal"
