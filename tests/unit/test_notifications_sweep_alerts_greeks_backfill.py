"""The Greeks-backfill completion Telegram alert (DEC-GREEKS-BULK-REENRICH-001).

Unit coverage of :meth:`SweepAlerter.greeks_backfill_complete` (both honesty
heads, MarkdownV2 escaping, the ``backfill_id``-scoped dedupe key, the
disabled-notifier silent ``False``) and the module helper
:func:`emit_greeks_backfill_complete_alert` (delegation + the structural
swallow-all fire-and-forget contract). Injected fakes only — the lazy
singleton resolver is never touched (the sibling ``test_notifications_sweep_alerts``
convention). Stem-named into the ``test_notifications_sweep_alerts*`` family
(like ``test_notifications_sweep_alerts_conflict.py``) so the per-module
coverage-discovery gate maps it to :mod:`brutex.notifications.sweep_alerts`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from brutex.notifications.sweep_alerts import (
    SweepAlerter,
    emit_greeks_backfill_complete_alert,
    escape_markdown_v2,
)
from brutex.notifications.telegram import (
    TelegramEvent,
    TelegramEventKind,
    TelegramSeverity,
)


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


@dataclass(frozen=True)
class FakeBackfillReport:
    """Typed :class:`GreeksBackfillReportLike` stand-in (matches the real report)."""

    backfill_id: str = "a" * 64
    planned: int = 5
    enriched: int = 5
    already_current: int = 2
    errored: int = 0
    stale_unfixable: int = 0
    bars_written: int = 123


class TestGreeksBackfillComplete:
    def test_clean_head_and_dedupe_key(self) -> None:
        fake = FakeNotifier()
        alerter = SweepAlerter(notifier=fake)
        assert alerter.greeks_backfill_complete(FakeBackfillReport()) is True
        (event,) = fake.published
        assert event.kind == TelegramEventKind.RUN_COMPLETE
        assert event.severity == TelegramSeverity.INFO
        assert event.dedupe_key == f"greeks-backfill:{'a' * 12}"
        assert "✅ Greeks backfill complete" in event.message
        assert escape_markdown_v2("5/5 contracts re-stamped v2") in event.message

    def test_failures_downgrade_to_warning_head(self) -> None:
        fake = FakeNotifier()
        alerter = SweepAlerter(notifier=fake)
        report = FakeBackfillReport(enriched=3, errored=2)
        assert alerter.greeks_backfill_complete(report) is True
        (event,) = fake.published
        assert "⚠️ Greeks backfill complete with failures" in event.message
        assert escape_markdown_v2("2 errored (re-runnable)") in event.message

    def test_unfixable_contracts_downgrade_to_warning_head(self) -> None:
        """Fix rounds 4-7 (refuter-confirmed): a driver-UNFIXABLE contract (a
        stale/null stamp on a row the enrich driver skips by design —
        non-finite OHLC — or on a row its dedup/month-bound read never sees)
        must never ride a green ✅ — the stale stamp persists and no re-run
        can clear it (a lake repair can).
        """
        fake = FakeNotifier()
        alerter = SweepAlerter(notifier=fake)
        report = FakeBackfillReport(errored=0, stale_unfixable=1)
        assert alerter.greeks_backfill_complete(report) is True
        (event,) = fake.published
        assert "⚠️ Greeks backfill complete with failures" in event.message
        assert (
            escape_markdown_v2("1 unfixable (driver-unactionable rows — lake repair needed)")
            in event.message
        )

    def test_errored_and_unfixable_clauses_combine(self) -> None:
        fake = FakeNotifier()
        alerter = SweepAlerter(notifier=fake)
        report = FakeBackfillReport(enriched=3, errored=2, stale_unfixable=1)
        assert alerter.greeks_backfill_complete(report) is True
        (event,) = fake.published
        assert "⚠️" in event.message
        assert escape_markdown_v2("2 errored (re-runnable)") in event.message
        assert escape_markdown_v2("1 unfixable") in event.message

    def test_zero_target_run_still_answers_honestly(self) -> None:
        fake = FakeNotifier()
        alerter = SweepAlerter(notifier=fake)
        report = FakeBackfillReport(planned=0, enriched=0, already_current=0, bars_written=0)
        assert alerter.greeks_backfill_complete(report) is True
        (event,) = fake.published
        assert escape_markdown_v2("0/0 contracts re-stamped v2") in event.message

    def test_body_is_markdownv2_escaped(self) -> None:
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).greeks_backfill_complete(FakeBackfillReport())
        (event,) = fake.published
        # The trailing period + the '=' of the tallies must arrive escaped.
        assert "\\." in event.message and "\\=" in event.message

    def test_disabled_notifier_is_a_silent_false(self) -> None:
        alerter = SweepAlerter(resolver=lambda: None)
        assert alerter.greeks_backfill_complete(FakeBackfillReport()) is False

    def test_publish_raise_is_swallowed(self) -> None:
        alerter = SweepAlerter(notifier=FakeNotifier(raise_on_publish=True))
        assert alerter.greeks_backfill_complete(FakeBackfillReport()) is False


class TestEmitGreeksBackfillCompleteAlert:
    """The COMMON module helper (the ``emit_pull_halt_alert`` shape)."""

    def test_delegates_to_the_alerter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[FakeBackfillReport] = []

        class _FakeAlerter:
            def greeks_backfill_complete(self, report: FakeBackfillReport) -> bool:
                captured.append(report)
                return True

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
        report = FakeBackfillReport()
        emit_greeks_backfill_complete_alert(report)
        assert captured == [report]

    def test_swallows_its_own_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failure constructing/publishing the alert NEVER propagates."""

        class _BoomAlerter:
            def __init__(self) -> None:
                raise RuntimeError("notifier construction blew up")

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _BoomAlerter)
        emit_greeks_backfill_complete_alert(FakeBackfillReport())  # must not raise
