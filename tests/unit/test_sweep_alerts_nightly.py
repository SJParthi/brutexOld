# pyright: reportPrivateUsage=none
"""Nightly SweepAlerter methods (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Extends the ``test_sweep_alerts*`` stem family: ``nightly_summary`` (kind /
severity law INFO-WARN-CRITICAL / per-run dedupe key / ``"?"`` rendering /
skip-backup nag) + ``nightly_refused`` (PHASE_BLOCKED CRITICAL, per-run
dedupe), the disabled-Telegram silent no-op, the publish-raise swallow, and
MarkdownV2 escaping.
"""

from __future__ import annotations

import pytest
from brutex.notifications.sweep_alerts import SweepAlerter, _count_or_question
from brutex.notifications.telegram import TelegramEvent, TelegramEventKind, TelegramSeverity


class FakeNotifier:
    """Captures every published event; can simulate a publish raise."""

    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.published: list[TelegramEvent] = []
        self.raise_on_publish = raise_on_publish

    def publish(self, event: TelegramEvent) -> None:
        if self.raise_on_publish:
            raise RuntimeError("simulated publish failure")
        self.published.append(event)


def _summary_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "nightly_id": "nightly-20260708T013000Z-abc123",
        "verdict": "OK",
        "link_verdicts": {
            "collision": "ok",
            "disk": "ok",
            "backup": "ok",
            "pull": "ok",
            "prebuild": "ok",
            "sweep": "ok",
        },
        "pull_bars": 345,
        "prebuild_built": 6,
        "sweep_run": 7,
        "sweep_skipped_cache": 8,
        "sweep_failed": 0,
        "backup_files": 12,
        "blocker": None,
        "duration_seconds": 3661.0,
    }
    base.update(overrides)
    return base


def test_summary_ok_is_info_with_per_run_dedupe() -> None:
    notifier = FakeNotifier()
    alerter = SweepAlerter(notifier=notifier)
    assert alerter.nightly_summary(**_summary_kwargs()) is True  # type: ignore[arg-type]
    [event] = notifier.published
    assert event.kind == TelegramEventKind.RUN_COMPLETE
    assert event.severity == TelegramSeverity.INFO
    assert event.dedupe_key == "nightly-summary:nightly-20260708T013000Z-abc123"
    assert "✅ OK in 1:01" in event.message
    assert "collision ok · disk ok" in event.message
    assert "backup ok \\(12 files\\)" in event.message
    assert "sweep ok \\(7 run · 8 skipped\\-cache · 0 failed\\)" in event.message


def test_summary_suspect_is_warn() -> None:
    notifier = FakeNotifier()
    SweepAlerter(notifier=notifier).nightly_summary(
        **_summary_kwargs(verdict="SUSPECT")  # type: ignore[arg-type]
    )
    [event] = notifier.published
    assert event.severity == TelegramSeverity.WARN
    assert "⚠️ SUSPECT" in event.message


@pytest.mark.parametrize(("verdict", "marker"), [("REFUSED", "⛔"), ("FAILED", "❌")])
def test_summary_refused_failed_are_critical_naming_blocker(verdict: str, marker: str) -> None:
    notifier = FakeNotifier()
    SweepAlerter(notifier=notifier).nightly_summary(
        **_summary_kwargs(  # type: ignore[arg-type]
            verdict=verdict,
            blocker="disk",
            link_verdicts={
                "collision": "ok",
                "disk": verdict.lower(),
                "backup": "blocked(disk)",
                "pull": "blocked(disk)",
                "prebuild": "blocked(disk)",
                "sweep": "blocked(disk)",
            },
        )
    )
    [event] = notifier.published
    assert event.severity == TelegramSeverity.CRITICAL
    assert f"{marker} {verdict} at disk" in event.message
    # A blocked link never fakes counts.
    assert "blocked\\(disk\\)" in event.message
    assert "files" not in event.message


def test_summary_unknown_blocker_reads_unknown() -> None:
    notifier = FakeNotifier()
    SweepAlerter(notifier=notifier).nightly_summary(
        **_summary_kwargs(verdict="FAILED", blocker=None)  # type: ignore[arg-type]
    )
    [event] = notifier.published
    assert "❌ FAILED at unknown" in event.message


def test_summary_unknown_counts_render_question_marks() -> None:
    notifier = FakeNotifier()
    SweepAlerter(notifier=notifier).nightly_summary(
        **_summary_kwargs(  # type: ignore[arg-type]
            pull_bars=None,
            prebuild_built=None,
            sweep_run=None,
            sweep_skipped_cache=None,
            sweep_failed=None,
            backup_files=None,
        )
    )
    [event] = notifier.published
    assert "backup ok \\(? files\\)" in event.message
    assert "pull ok \\(? bars\\)" in event.message
    assert "sweep ok \\(? run · ? skipped\\-cache · ? failed\\)" in event.message
    assert "(0" not in event.message  # NEVER a fabricated zero


def test_summary_skip_backup_nag() -> None:
    notifier = FakeNotifier()
    SweepAlerter(notifier=notifier).nightly_summary(
        **_summary_kwargs(  # type: ignore[arg-type]
            link_verdicts={
                "collision": "ok",
                "disk": "ok",
                "backup": "skipped",
                "pull": "ok",
                "prebuild": "ok",
                "sweep": "ok",
            }
        )
    )
    [event] = notifier.published
    assert "backup SKIPPED by operator flag" in event.message
    assert "stores unprotected tonight" in event.message


def test_summary_negative_duration_clamps() -> None:
    notifier = FakeNotifier()
    SweepAlerter(notifier=notifier).nightly_summary(
        **_summary_kwargs(duration_seconds=-9.0)  # type: ignore[arg-type]
    )
    [event] = notifier.published
    assert "in 0:00" in event.message


def test_refused_is_phase_blocked_critical_with_per_run_key() -> None:
    notifier = FakeNotifier()
    alerter = SweepAlerter(notifier=notifier)
    assert (
        alerter.nightly_refused(nightly_id="nightly-X", link="collision", reason="lock held")
        is True
    )
    [event] = notifier.published
    assert event.kind == TelegramEventKind.PHASE_BLOCKED
    assert event.severity == TelegramSeverity.CRITICAL
    assert event.dedupe_key == "nightly-refused:nightly-X:collision"
    assert "REFUSED at collision: lock held" in event.message
    assert "Nothing after collision ran" in event.message
    assert "Next attempt: next schedule fire" in event.message


def test_disabled_telegram_is_silent_no_op() -> None:
    """Resolver → None (disabled): every emit returns False, publishes nothing."""
    alerter = SweepAlerter(resolver=lambda: None)
    assert alerter.nightly_summary(**_summary_kwargs()) is False  # type: ignore[arg-type]
    assert alerter.nightly_refused(nightly_id="n", link="disk", reason="floor") is False


def test_publish_raise_is_swallowed() -> None:
    notifier = FakeNotifier(raise_on_publish=True)
    alerter = SweepAlerter(notifier=notifier)
    assert alerter.nightly_summary(**_summary_kwargs()) is False  # type: ignore[arg-type]
    assert alerter.nightly_refused(nightly_id="n", link="disk", reason="floor") is False


def test_bodies_are_markdown_v2_escaped() -> None:
    """Special chars in ids/reasons arrive escaped (the whole-body house law)."""
    notifier = FakeNotifier()
    SweepAlerter(notifier=notifier).nightly_refused(
        nightly_id="nightly-a_b", link="disk", reason="free 1.5GiB < required 20GiB!"
    )
    [event] = notifier.published
    assert "nightly\\-a\\_b" in event.message
    assert "1\\.5GiB < required 20GiB\\!" in event.message


def test_count_or_question_helper() -> None:
    assert _count_or_question(None) == "?"
    assert _count_or_question(0) == "0"
    assert _count_or_question(42) == "42"
