"""Unit tests — the run-core alert additions to :mod:`brutex.notifications.sweep_alerts`.

DEC-SWEEP-UNKILLABLE-CORE-001 (design §2): ``sweep_milestone`` (R41 Telegram
milestones), ``run_suspect`` (R39/D-12 SUSPECT fan-out), ``sweep_halted`` (the
``pull_halted`` twin) + the module-level blanket-guarded
``emit_run_suspect_alert`` helper. House contract pinned via the FakeNotifier
pattern: exact kinds/severities/dedupe keys, MarkdownV2 escaping, ``bool``
returns, never raises, silent no-op when disabled.

This file joins the sweep_alerts per-module coverage gate beside
``test_notifications_sweep_alerts.py`` + ``..._conflict.py`` (WP-6 wires the
ci.yml gate line — see wp-integration-notes.md).
"""

from __future__ import annotations

import pytest
from brutex.notifications import sweep_alerts
from brutex.notifications.sweep_alerts import SweepAlerter, emit_run_suspect_alert
from brutex.notifications.telegram import (
    TelegramEvent,
    TelegramEventKind,
    TelegramSeverity,
)

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_r41_sweep_milestone_event_exact": ("R41",),
    "test_r41_sweep_milestone_dedupe_key_scopes_run_and_pct": ("R41",),
    "test_r39_run_suspect_event_exact": ("R39",),
    "test_r39_run_suspect_detail_is_markdownv2_escaped": ("R39",),
    "test_r16_sweep_halted_event_exact": ("R16",),
    "test_r16_sweep_halted_dedupe_scopes_run_and_reason": ("R16",),
    "test_r16_alert_methods_return_false_when_disabled": ("R16",),
    "test_r16_alert_methods_never_raise_on_publish_failure": ("R16",),
    "test_r39_emit_run_suspect_alert_delegates": ("R39",),
    "test_r39_emit_run_suspect_alert_blanket_guarded": ("R39",),
}

#: A deterministic 64-hex run identity.
RUN_ID = "ab12" * 16


class FakeNotifier:
    """Captures every published :class:`TelegramEvent`; can simulate a raise."""

    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.published: list[TelegramEvent] = []
        self.raise_on_publish = raise_on_publish

    def publish(self, event: TelegramEvent) -> None:
        """Record *event* (or raise to exercise the swallow-and-log path)."""
        if self.raise_on_publish:
            raise RuntimeError("simulated publish failure")
        self.published.append(event)


# ────────────────────────────────────────────────────────────────────────
# sweep_milestone (R41)
# ────────────────────────────────────────────────────────────────────────


def test_r41_sweep_milestone_event_exact() -> None:
    fake = FakeNotifier()
    alerter = SweepAlerter(notifier=fake)
    assert alerter.sweep_milestone(run_id=RUN_ID, pct=50, tuples_done=512, total=1024) is True
    assert len(fake.published) == 1
    event = fake.published[0]
    assert event.kind == TelegramEventKind.PHASE_PROGRESS
    assert event.severity == TelegramSeverity.INFO
    assert event.dedupe_key == f"sweep-milestone:{RUN_ID}:50"
    # The run id is truncated to 12 chars and the counts render verbatim
    # (MarkdownV2: '%' is not reserved; '/' is not reserved).
    assert f"Sweep {RUN_ID[:12]} reached 50%" in event.message
    assert "512/1024 tuples done" in event.message


def test_r41_sweep_milestone_dedupe_key_scopes_run_and_pct() -> None:
    fake = FakeNotifier()
    alerter = SweepAlerter(notifier=fake)
    alerter.sweep_milestone(run_id=RUN_ID, pct=25, tuples_done=1, total=4)
    alerter.sweep_milestone(run_id=RUN_ID, pct=75, tuples_done=3, total=4)
    assert [e.dedupe_key for e in fake.published] == [
        f"sweep-milestone:{RUN_ID}:25",
        f"sweep-milestone:{RUN_ID}:75",
    ]


# ────────────────────────────────────────────────────────────────────────
# run_suspect (R39 / D-12)
# ────────────────────────────────────────────────────────────────────────


def test_r39_run_suspect_event_exact() -> None:
    fake = FakeNotifier()
    alerter = SweepAlerter(notifier=fake)
    assert (
        alerter.run_suspect(run_id=RUN_ID, tripwire="count_reconcile", detail="done 5 vs 7") is True
    )
    event = fake.published[0]
    assert event.kind == TelegramEventKind.QUARANTINE_NEW
    assert event.severity == TelegramSeverity.CRITICAL
    assert event.dedupe_key == f"run-suspect:{RUN_ID}:count_reconcile"
    assert f"Sweep {RUN_ID[:12]} marked SUSPECT by tripwire count\\_reconcile" in event.message


def test_r39_run_suspect_detail_is_markdownv2_escaped() -> None:
    fake = FakeNotifier()
    alerter = SweepAlerter(notifier=fake)
    alerter.run_suspect(run_id=RUN_ID, tripwire="nan_screen", detail="sharpe=NaN (tuple #3)")
    message = fake.published[0].message
    # '=' '(' ')' '#' are MarkdownV2-reserved — each must arrive escaped.
    assert "sharpe\\=NaN \\(tuple \\#3\\)" in message


# ────────────────────────────────────────────────────────────────────────
# sweep_halted (the pull_halted twin)
# ────────────────────────────────────────────────────────────────────────


def test_r16_sweep_halted_event_exact() -> None:
    fake = FakeNotifier()
    alerter = SweepAlerter(notifier=fake)
    assert alerter.sweep_halted(run_id=RUN_ID, reason="disk_full") is True
    event = fake.published[0]
    assert event.kind == TelegramEventKind.PHASE_BLOCKED
    assert event.severity == TelegramSeverity.CRITICAL
    assert event.dedupe_key == f"sweep-halt:{RUN_ID}:disk_full"
    assert f"Sweep {RUN_ID[:12]} HALTED: disk\\_full" in event.message
    # The resume promise (checkpoint + sentinel laws) is part of the message.
    assert "the run resumes where it stopped" in event.message


def test_r16_sweep_halted_dedupe_scopes_run_and_reason() -> None:
    fake = FakeNotifier()
    alerter = SweepAlerter(notifier=fake)
    alerter.sweep_halted(run_id="", reason="daemon_init")
    assert fake.published[0].dedupe_key == "sweep-halt::daemon_init"


# ────────────────────────────────────────────────────────────────────────
# House contract: bool returns, never raise, disabled ⇒ False
# ────────────────────────────────────────────────────────────────────────


def test_r16_alert_methods_return_false_when_disabled() -> None:
    """Resolver returning None (Telegram disabled) ⇒ every method is a False no-op."""
    alerter = SweepAlerter(resolver=lambda: None)
    assert alerter.sweep_milestone(run_id=RUN_ID, pct=25, tuples_done=1, total=4) is False
    assert alerter.run_suspect(run_id=RUN_ID, tripwire="t", detail="d") is False
    assert alerter.sweep_halted(run_id=RUN_ID, reason="r") is False


def test_r16_alert_methods_never_raise_on_publish_failure() -> None:
    """A raising notifier is swallowed + returns False — never into the run."""
    alerter = SweepAlerter(notifier=FakeNotifier(raise_on_publish=True))
    assert alerter.sweep_milestone(run_id=RUN_ID, pct=25, tuples_done=1, total=4) is False
    assert alerter.run_suspect(run_id=RUN_ID, tripwire="t", detail="d") is False
    assert alerter.sweep_halted(run_id=RUN_ID, reason="r") is False


# ────────────────────────────────────────────────────────────────────────
# emit_run_suspect_alert — the module-level blanket-guarded helper
# ────────────────────────────────────────────────────────────────────────


def test_r39_emit_run_suspect_alert_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, str]] = []

    class _FakeAlerter:
        def run_suspect(self, *, run_id: str, tripwire: str, detail: str) -> bool:
            captured.append((run_id, tripwire, detail))
            return True

    monkeypatch.setattr(sweep_alerts, "SweepAlerter", _FakeAlerter)
    assert emit_run_suspect_alert(run_id=RUN_ID, tripwire="tw", detail="d") is True
    assert captured == [(RUN_ID, "tw", "d")]


def test_r39_emit_run_suspect_alert_blanket_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a CONSTRUCTION failure is swallowed + logged — returns False, never raises."""

    class _ExplodingAlerter:
        def __init__(self) -> None:
            raise RuntimeError("construction failure")

    monkeypatch.setattr(sweep_alerts, "SweepAlerter", _ExplodingAlerter)
    assert emit_run_suspect_alert(run_id=RUN_ID, tripwire="tw", detail="d") is False
