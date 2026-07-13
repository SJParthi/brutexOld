# pyright: reportPrivateUsage=none
"""Cross-verify SweepAlerter methods + emit helpers (DEC-CROSSVERIFY-PRODUCER-001).

Extends the ``test_notifications_sweep_alerts*`` stem family (appended to the
ci.yml sweep_alerts gate line): ``crossverify_summary`` (RUN_COMPLETE kind /
the INFO-WARN-CRITICAL severity law incl. the unknown-future-verdict CRITICAL
arm / per-run dedupe key / the ``"?"`` unknown-count rendering) +
``crossverify_refused`` (PHASE_BLOCKED CRITICAL, per-run dedupe), the
module-level blanket-try ``emit_crossverify_summary_alert`` /
``emit_crossverify_refused_alert`` helpers, the disabled-Telegram silent
no-op, the publish-raise swallow — and the END-TO-END binding pin proving the
producer's ``_default_alerter`` (which resolves the helpers by NAME via
``getattr``) actually reaches a live notifier now that phase B landed them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pytest
from brutex.notifications.sweep_alerts import (
    SweepAlerter,
    emit_crossverify_refused_alert,
    emit_crossverify_summary_alert,
)
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


@dataclass(frozen=True, slots=True)
class _Report:
    """A structural CrossverifyReportLike double (counts nullable — the "?" law)."""

    day: date
    verdict: str
    published: int | None
    skipped_already: int | None
    publish_strict: int | None
    publish_observe: int | None
    withheld: int | None
    blocked: int | None
    nothing: int | None
    publish_failed: int | None
    recovered_prior: int | None
    regated: int | None
    derived: int | None
    derive_failed: int | None
    detail: str


def _report(**overrides: object) -> _Report:
    base = _Report(
        day=date(2026, 7, 10),
        verdict="published",
        published=748,
        skipped_already=2,
        publish_strict=748,
        publish_observe=0,
        withheld=0,
        blocked=0,
        nothing=10,
        publish_failed=0,
        recovered_prior=0,
        regated=0,
        derived=750,
        derive_failed=0,
        detail="",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


RUN_ID = "cv-20260710-161501-4242"


class TestCrossverifySummary:
    def test_all_published_is_info_with_per_run_dedupe(self) -> None:
        notifier = FakeNotifier()
        alerter = SweepAlerter(notifier=notifier)
        assert alerter.crossverify_summary(run_id=RUN_ID, report=_report()) is True
        [event] = notifier.published
        assert event.kind == TelegramEventKind.RUN_COMPLETE
        assert event.severity == TelegramSeverity.INFO
        assert event.dedupe_key == f"crossverify-summary:{RUN_ID}"
        assert "✅ published" in event.message
        assert "2026\\-07\\-10" in event.message
        assert "published 748" in event.message
        assert "derived 750" in event.message

    def test_published_with_recovered_prior_is_warn(self) -> None:
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID, report=_report(recovered_prior=3)
        )
        [event] = notifier.published
        assert event.severity == TelegramSeverity.WARN
        assert "recovered\\-prior 3" in event.message

    @pytest.mark.parametrize("verdict", ["staged_only", "nothing_to_do"])
    def test_benign_anomaly_verdicts_are_warn(self, verdict: str) -> None:
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID, report=_report(verdict=verdict)
        )
        [event] = notifier.published
        assert event.severity == TelegramSeverity.WARN

    @pytest.mark.parametrize(
        ("verdict", "marker"),
        [
            ("blocked", "⛔ BLOCKED"),
            ("withheld_partial", "⚠️ WITHHELD \\(partial\\)"),
            ("publish_failed", "❌ PUBLISH FAILED"),
            ("suspect", "🛑 SUSPECT"),
        ],
    )
    def test_failure_verdicts_are_critical(self, verdict: str, marker: str) -> None:
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID, report=_report(verdict=verdict, withheld=4, blocked=1)
        )
        [event] = notifier.published
        assert event.severity == TelegramSeverity.CRITICAL
        assert marker in event.message

    def test_unknown_future_verdict_is_critical_never_silent_info(self) -> None:
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID, report=_report(verdict="totally_new_state")
        )
        [event] = notifier.published
        assert event.severity == TelegramSeverity.CRITICAL
        assert "totally\\_new\\_state" in event.message

    def test_withheld_and_blocked_counts_surface_in_the_summary(self) -> None:
        # DEC-CROSSVERIFY-SPOT-ONLY-001 (#1788) operator-visibility pin: at
        # ~776 spot symbols the per-day refused-symbol counts MUST reach the
        # operator — the summary body carries withheld/blocked/nothing
        # verbatim (the runbook's gate-class table relies on this).
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID,
            report=_report(verdict="withheld_partial", withheld=17, blocked=2, nothing=41),
        )
        [event] = notifier.published
        assert "withheld 17" in event.message
        assert "blocked 2" in event.message
        assert "nothing 41" in event.message

    def test_three_way_publish_split_surfaces_in_the_summary(self) -> None:
        # The APPROVED index-policy ruling (#1788 fix round): the summary
        # shows the three-way split VISIBLY — strict-published /
        # observe-published / the refused tail — so an observe-tier index
        # shipped under the count-anomaly law never ships invisibly.
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID,
            report=_report(
                verdict="withheld_partial",
                publish_strict=740,
                publish_observe=8,
                withheld=17,
                nothing=41,
            ),
        )
        [event] = notifier.published
        assert "strict\\-published 740" in event.message
        assert "observe\\-published 8" in event.message
        assert "withheld 17" in event.message
        assert "nothing 41" in event.message

    def test_unknown_publish_split_counts_render_question(self) -> None:
        # The "?" law covers the new split counts too — never a fabricated 0.
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID, report=_report(publish_strict=None, publish_observe=None)
        )
        [event] = notifier.published
        assert "strict\\-published ?" in event.message
        assert "observe\\-published ?" in event.message

    def test_unknown_counts_render_question_never_zero(self) -> None:
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID,
            report=_report(published=None, derived=None, derive_failed=None),
        )
        [event] = notifier.published
        assert "published ?" in event.message
        assert "derived ? \\(? failed\\)" in event.message

    def test_published_with_regated_is_warn(self) -> None:
        # An auto-re-gated prior day is worth a look, not a page — WARN
        # (DEC-CROSSVERIFY-WITHHELD-REGATE-001, the recovered-prior law).
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID, report=_report(regated=2)
        )
        assert notifier.published[0].severity == TelegramSeverity.WARN
        assert "re\\-gated 2" in notifier.published[0].message  # MarkdownV2-escaped

    def test_regated_count_always_surfaces_in_the_counts_line(self) -> None:
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(run_id=RUN_ID, report=_report())
        assert "re\\-gated 0" in notifier.published[0].message  # MarkdownV2-escaped

    def test_published_with_unknown_regated_is_warn_not_info(self) -> None:
        # regated=None is an UNKNOWN — the INFO arm requires a REAL 0.
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID, report=_report(regated=None)
        )
        assert notifier.published[0].severity == TelegramSeverity.WARN
        assert "re\\-gated ?" in notifier.published[0].message

    def test_published_with_unknown_recovered_is_warn_not_info(self) -> None:
        # recovered_prior=None is an UNKNOWN — the INFO arm requires a REAL 0.
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID, report=_report(recovered_prior=None)
        )
        [event] = notifier.published
        assert event.severity == TelegramSeverity.WARN

    def test_detail_line_appended_when_present(self) -> None:
        notifier = FakeNotifier()
        SweepAlerter(notifier=notifier).crossverify_summary(
            run_id=RUN_ID, report=_report(verdict="staged_only", detail="publish disabled")
        )
        [event] = notifier.published
        assert "publish disabled" in event.message

    def test_disabled_telegram_is_silent_false(self) -> None:
        alerter = SweepAlerter(resolver=lambda: None)
        assert alerter.crossverify_summary(run_id=RUN_ID, report=_report()) is False

    def test_publish_raise_is_swallowed_false(self) -> None:
        alerter = SweepAlerter(notifier=FakeNotifier(raise_on_publish=True))
        assert alerter.crossverify_summary(run_id=RUN_ID, report=_report()) is False


class TestCrossverifyRefused:
    def test_refused_is_critical_phase_blocked_per_run_dedupe(self) -> None:
        notifier = FakeNotifier()
        alerter = SweepAlerter(notifier=notifier)
        assert (
            alerter.crossverify_refused(run_id=RUN_ID, reason="pull locks held by a peer") is True
        )
        [event] = notifier.published
        assert event.kind == TelegramEventKind.PHASE_BLOCKED
        assert event.severity == TelegramSeverity.CRITICAL
        assert event.dedupe_key == f"crossverify-refused:{RUN_ID}"
        assert "REFUSED: pull locks held by a peer" in event.message
        assert "TickVault sees absence, never wrong data" in event.message

    def test_disabled_telegram_is_silent_false(self) -> None:
        alerter = SweepAlerter(resolver=lambda: None)
        assert alerter.crossverify_refused(run_id=RUN_ID, reason="gate absent") is False


class TestEmitHelpers:
    """The module-level blanket-try fire-and-forget helpers."""

    def test_emit_summary_reaches_a_live_notifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        notifier = FakeNotifier()
        monkeypatch.setattr(
            "brutex.notifications.sweep_alerts.SweepAlerter",
            lambda: SweepAlerter(notifier=notifier),
        )
        assert emit_crossverify_summary_alert(run_id=RUN_ID, report=_report()) is True
        [event] = notifier.published
        assert event.dedupe_key == f"crossverify-summary:{RUN_ID}"

    def test_emit_refused_reaches_a_live_notifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        notifier = FakeNotifier()
        monkeypatch.setattr(
            "brutex.notifications.sweep_alerts.SweepAlerter",
            lambda: SweepAlerter(notifier=notifier),
        )
        assert emit_crossverify_refused_alert(run_id=RUN_ID, reason="deadline") is True
        [event] = notifier.published
        assert event.dedupe_key == f"crossverify-refused:{RUN_ID}"

    def test_emit_summary_swallows_construction_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.notifications.sweep_alerts as sa

        def _boom(**_kwargs: object) -> None:
            raise RuntimeError("construction failure")

        monkeypatch.setattr(sa, "SweepAlerter", _boom)
        assert emit_crossverify_summary_alert(run_id=RUN_ID, report=_report()) is False

    def test_emit_refused_swallows_construction_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.notifications.sweep_alerts as sa

        monkeypatch.setattr(
            sa, "SweepAlerter", lambda **_kw: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert emit_crossverify_refused_alert(run_id=RUN_ID, reason="x") is False

    def test_emit_helpers_are_disabled_silent_noops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.notifications.sweep_alerts as sa

        monkeypatch.setattr(sa, "_resolve_singleton_notifier", lambda: None)
        assert emit_crossverify_summary_alert(run_id=RUN_ID, report=_report()) is False
        assert emit_crossverify_refused_alert(run_id=RUN_ID, reason="x") is False


class TestProducerBindingEndToEnd:
    """The producer's getattr-by-NAME binding picks the landed helpers up.

    Phase A shipped ``brutex.crossverify.run._default_alerter`` binding
    ``emit_crossverify_summary_alert`` / ``emit_crossverify_refused_alert``
    by name with a WARN-and-False degrade while they did not exist; this pin
    proves the phase-B helpers are now found and actually publish — the
    zero-producer-edit wake-up the handoff promised.
    """

    def test_default_alerter_summary_publishes_through_the_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.crossverify.run import CrossverifyReport, _default_alerter

        notifier = FakeNotifier()
        monkeypatch.setattr(
            "brutex.notifications.sweep_alerts.SweepAlerter",
            lambda: SweepAlerter(notifier=notifier),
        )
        report = CrossverifyReport(
            day=date(2026, 7, 10),
            run_id=RUN_ID,
            verdict="published",
            published=1,
            skipped_already=0,
            publish_strict=1,
            publish_observe=0,
            withheld=0,
            blocked=0,
            nothing=0,
            excluded=0,
            publish_failed=0,
            recovered_prior=0,
            regated=0,
            derived=1,
            derive_failed=0,
            movers_warm="revision-propagated (headless)",
            detail="",
            exit_code=0,
        )
        alerter = _default_alerter()
        assert alerter.crossverify_summary(run_id=RUN_ID, report=report) is True
        [event] = notifier.published
        assert event.kind == TelegramEventKind.RUN_COMPLETE
        assert event.dedupe_key == f"crossverify-summary:{RUN_ID}"

    def test_default_alerter_refused_publishes_through_the_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.crossverify.run import _default_alerter

        notifier = FakeNotifier()
        monkeypatch.setattr(
            "brutex.notifications.sweep_alerts.SweepAlerter",
            lambda: SweepAlerter(notifier=notifier),
        )
        assert _default_alerter().crossverify_refused(run_id=RUN_ID, reason="locks") is True
        [event] = notifier.published
        assert event.kind == TelegramEventKind.PHASE_BLOCKED
        assert event.dedupe_key == f"crossverify-refused:{RUN_ID}"
