"""Unit tests for :mod:`brutex.notifications.sweep_alerts` (DEC-TELEGRAM-WIRE-001).

100% line + branch coverage of the sweep-supervisor → Telegram alert seam. Every
test injects either an explicit fake notifier or a fake resolver, so the default
singleton resolver (``_resolve_singleton_notifier``, ``# pragma: no cover`` — it
lazily imports the ``fastbloom_rs`` wiring chain) is never touched.

Test types present (CLAUDE.md §12 / Golden Rule #12):
* unit — every method + branch (resolver explicit / lazy / None / raising;
  publish ok / None / raising; each alert + each callback adapter).
* property (hypothesis) — the MarkdownV2 escaper length + safety invariants.
* golden — a byte-exact escaped string.
* bench — the per-publish O(1) ratio (in ``tests/bench``).
* stress — a high-volume publish burst stays leak-free + never raises.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from brutex.notifications.sweep_alerts import (
    SweepAlerter,
    emit_lake_heal_alert,
    emit_pull_halt_alert,
    escape_markdown_v2,
)
from brutex.notifications.telegram import (
    TelegramEvent,
    TelegramEventKind,
    TelegramSeverity,
)
from hypothesis import given
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────────
# Fakes
# ────────────────────────────────────────────────────────────────────────


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
class FakeSummary:
    """Typed :class:`SweepSummaryLike` stand-in (matches the supervised-grid summary)."""

    sweep_id: str
    tuples_run: int
    tuples_total: int
    evaluated: int
    ranked: int
    persisted: int
    tuples_failed: int = 0


@dataclass(frozen=True)
class FakeWorkerEvent:
    """Typed :class:`WorkerEventLike` stand-in (matches the watchdog event)."""

    worker_name: str
    last_beat_seconds_ago: float
    timeout_seconds: float


@dataclass(frozen=True)
class FakeSymbolFailureEvent:
    """Typed :class:`SymbolFailureLike` stand-in (matches the retry-storm event)."""

    symbol: str
    failed_windows: int
    total_windows: int


class FakeController:
    """Duck-typed :class:`RateControllerLike` — records the throttle call + snapshot."""

    def __init__(self, *, current_rps: float = 4.0, recent_hits: int = 3) -> None:
        self.rate_limit_calls = 0
        self._snap: dict[str, object] = {
            "current_rps": current_rps,
            "recent_hits": recent_hits,
            "recent_timeouts": 0,
        }

    def on_rate_limit(self) -> None:
        """Count an invocation of the controller's real throttle hook."""
        self.rate_limit_calls += 1

    def snapshot(self) -> dict[str, object]:
        """Return the canned snapshot dict."""
        return dict(self._snap)


# ────────────────────────────────────────────────────────────────────────
# escape_markdown_v2
# ────────────────────────────────────────────────────────────────────────


class TestEscapeMarkdownV2:
    def test_specials_are_backslash_escaped(self) -> None:
        assert escape_markdown_v2("a.b-c(d)") == "a\\.b\\-c\\(d\\)"

    def test_plain_text_unchanged(self) -> None:
        assert escape_markdown_v2("abc 123 NIFTY") == "abc 123 NIFTY"

    def test_empty_string(self) -> None:
        assert escape_markdown_v2("") == ""

    def test_golden_all_specials(self) -> None:
        # Byte-exact: every reserved char gets exactly one leading backslash.
        raw = r"_*[]()~`>#+-=|{}.!"
        expected = "".join("\\" + ch for ch in raw)
        assert escape_markdown_v2(raw) == expected

    @given(st.text())
    def test_length_invariant(self, text: str) -> None:
        specials = set(r"_*[]()~`>#+-=|{}.!")
        n_specials = sum(1 for ch in text if ch in specials)
        assert len(escape_markdown_v2(text)) == len(text) + n_specials

    @given(st.text())
    def test_every_special_is_escaped(self, text: str) -> None:
        out = escape_markdown_v2(text)
        specials = set(r"_*[]()~`>#+-=|{}.!")
        # Walk the output: every special char must be immediately preceded by `\`.
        for idx, ch in enumerate(out):
            if ch in specials:
                assert idx > 0 and out[idx - 1] == "\\"


# ────────────────────────────────────────────────────────────────────────
# Notifier resolution + best-effort publish
# ────────────────────────────────────────────────────────────────────────


class TestResolution:
    def test_explicit_notifier_used(self) -> None:
        fake = FakeNotifier()
        alerter = SweepAlerter(notifier=fake)
        assert alerter.test_digest() is True
        assert len(fake.published) == 1

    def test_lazy_resolver_used_when_no_explicit(self) -> None:
        fake = FakeNotifier()
        alerter = SweepAlerter(resolver=lambda: fake)
        assert alerter.test_digest() is True
        assert len(fake.published) == 1

    def test_resolver_returns_none_is_silent_noop(self) -> None:
        alerter = SweepAlerter(resolver=lambda: None)
        assert alerter.test_digest() is False

    def test_resolver_raising_is_swallowed(self) -> None:
        def _boom() -> FakeNotifier:
            raise RuntimeError("resolver exploded")

        alerter = SweepAlerter(resolver=_boom)
        assert alerter.test_digest() is False

    def test_publish_raising_is_swallowed(self) -> None:
        fake = FakeNotifier(raise_on_publish=True)
        alerter = SweepAlerter(notifier=fake)
        assert alerter.test_digest() is False
        assert fake.published == []


# ────────────────────────────────────────────────────────────────────────
# Alert content / kind / severity / dedupe-key
# ────────────────────────────────────────────────────────────────────────


class TestAlerts:
    def test_test_digest(self) -> None:
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).test_digest()
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.TEST
        assert ev.severity == TelegramSeverity.INFO
        assert "test" in ev.message.lower()

    def test_watchdog_hung(self) -> None:
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).watchdog_hung(
            worker_name="sweep-worker-0",
            last_beat_seconds_ago=412.5,
            timeout_seconds=300.0,
        )
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.WORKER_HUNG
        assert ev.severity == TelegramSeverity.CRITICAL
        assert ev.dedupe_key == "sweep-worker-0"  # dedupe key is the RAW (unescaped) name
        assert "HUNG" in ev.message
        # The worker name is MarkdownV2-escaped in the body (hyphens → `\-`).
        assert escape_markdown_v2("sweep-worker-0") in ev.message

    def test_rate_breach(self) -> None:
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).rate_breach(current_rps=3.0, recent_hits=4)
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.RATE_LIMIT_DOWNSHIFT
        assert ev.severity == TelegramSeverity.WARN
        assert ev.dedupe_key == "rate-breach"
        assert "429" in ev.message

    def test_auth_renew_failed(self) -> None:
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).auth_renew_failed(reason="token expired")
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.AUTH_RENEW_FAILED
        assert ev.severity == TelegramSeverity.ERROR
        assert ev.dedupe_key == "auth-renew"
        assert "token expired" in ev.message

    def test_pull_halted(self) -> None:
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).pull_halted(reason="disk_full")
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.PHASE_BLOCKED
        assert ev.severity == TelegramSeverity.CRITICAL
        assert ev.dedupe_key == "pull-halt:disk_full"  # reason-scoped dedupe (RAW, unescaped)
        # The body is MarkdownV2-escaped (underscore → `\_`), so assert the escaped forms.
        assert escape_markdown_v2("disk_full") in ev.message
        assert escape_markdown_v2("Pull HALTED") in ev.message

    def test_pull_degraded(self) -> None:
        """pull_degraded mirrors rate_breach: WARN, deduped per rule, carries the detail."""
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).pull_degraded(
            rule="pull.stalled",
            achieved=0.1,
            allowed=5.0,
            detail="OUR pipeline is the limiter — check write latency",
        )
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.RATE_LIMIT_DOWNSHIFT
        assert ev.severity == TelegramSeverity.WARN
        assert ev.dedupe_key == "pull-degraded:pull.stalled"  # rule-scoped dedupe (RAW)
        assert escape_markdown_v2("pull.stalled") in ev.message
        assert escape_markdown_v2("OUR pipeline is the limiter") in ev.message

    def test_pull_degraded_vendor_detail(self) -> None:
        """A VENDOR-gated detail string renders distinctly from the our-code one."""
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).pull_degraded(
            rule="pull.rate_storm",
            achieved=2.0,
            allowed=5.0,
            detail="VENDOR 429-gated — expected; the pull continues",
        )
        ev = fake.published[0]
        assert escape_markdown_v2("VENDOR 429-gated") in ev.message
        assert "OUR pipeline" not in ev.message

    def test_sweep_complete(self) -> None:
        fake = FakeNotifier()
        summary = FakeSummary(
            sweep_id="abcdef0123456789",
            tuples_run=10,
            tuples_total=12,
            evaluated=4096,
            ranked=20,
            persisted=15,
        )
        SweepAlerter(notifier=fake).sweep_complete(summary)
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.RUN_COMPLETE
        assert ev.severity == TelegramSeverity.INFO
        assert ev.dedupe_key == "abcdef0123456789"
        assert "10/12" in ev.message
        assert "abcdef012345" in ev.message  # sweep_id[:12]
        # No failures → the ✅ headline, no FAILED clause (clean run).
        assert "✅" in ev.message
        assert "FAILED" not in ev.message

    def test_sweep_complete_with_failures_warns(self) -> None:
        """tuples_failed > 0 downgrades ✅ → ⚠️ and names the FAILED count (Golden Rule #10)."""
        fake = FakeNotifier()
        summary = FakeSummary(
            sweep_id="abcdef0123456789",
            tuples_run=9,
            tuples_total=12,
            evaluated=4096,
            ranked=20,
            persisted=15,
            tuples_failed=3,
        )
        SweepAlerter(notifier=fake).sweep_complete(summary)
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.RUN_COMPLETE
        assert ev.severity == TelegramSeverity.INFO
        assert ev.dedupe_key == "abcdef0123456789"
        assert "⚠️" in ev.message
        assert "✅" not in ev.message
        # The escaped MarkdownV2 form of "3 tuples FAILED" appears.
        assert escape_markdown_v2("3 tuples FAILED") in ev.message

    def test_symbol_window_failure(self) -> None:
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).symbol_window_failure(
            symbol="BANK-NIFTY",
            failed_windows=15,
            total_windows=15,
        )
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.SYMBOL_WINDOW_FAILURE
        assert ev.severity == TelegramSeverity.CRITICAL
        assert ev.dedupe_key == "BANK-NIFTY"  # dedupe key is the RAW (unescaped) symbol
        # The body is MarkdownV2-escaped (hyphen → `\-`), so assert the escaped forms.
        assert escape_markdown_v2("100% window-failure") in ev.message
        assert escape_markdown_v2("BANK-NIFTY") in ev.message

    def test_alert_disabled_returns_false(self) -> None:
        # Every alert routes through `_publish`, so a None notifier no-ops them all.
        alerter = SweepAlerter(resolver=lambda: None)
        assert (
            alerter.watchdog_hung(worker_name="w", last_beat_seconds_ago=1.0, timeout_seconds=2.0)
            is False
        )
        assert alerter.rate_breach(current_rps=3.0, recent_hits=1) is False
        assert alerter.auth_renew_failed(reason="x") is False
        assert alerter.symbol_window_failure(symbol="s", failed_windows=1, total_windows=1) is False
        assert alerter.pull_halted(reason="disk_full") is False
        summary = FakeSummary(
            sweep_id="s", tuples_run=1, tuples_total=1, evaluated=1, ranked=1, persisted=1
        )
        assert alerter.sweep_complete(summary) is False


# ────────────────────────────────────────────────────────────────────────
# emit_pull_halt_alert — the COMMON best-effort helper (CLI + dashboard)
# ────────────────────────────────────────────────────────────────────────


class TestEmitPullHaltAlert:
    """The shared `emit_pull_halt_alert(exc)` used by both cmd_fetch and the dashboard job."""

    def test_surfaces_lake_write_halt_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.data.errors import LakeWriteHalt

        captured: list[str] = []

        class _FakeAlerter:
            def pull_halted(self, *, reason: str) -> bool:
                captured.append(reason)
                return True

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
        emit_pull_halt_alert(LakeWriteHalt("disk_full"))
        assert captured == ["disk_full"]  # LakeWriteHalt.reason surfaced verbatim

    def test_other_stop_uses_class_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[str] = []

        class _FakeAlerter:
            def pull_halted(self, *, reason: str) -> bool:
                captured.append(reason)
                return True

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
        emit_pull_halt_alert(RuntimeError("boom"))
        assert captured == ["RuntimeError"]  # non-LakeWriteHalt → class name

    def test_swallows_its_own_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failure CONSTRUCTING/publishing the alert is swallowed — never propagates."""

        class _BoomAlerter:
            def __init__(self) -> None:
                raise RuntimeError("notifier construction blew up")

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _BoomAlerter)
        # The no-raise IS the assertion (the blanket except swallows + logs it).
        emit_pull_halt_alert(RuntimeError("halt"))


# ────────────────────────────────────────────────────────────────────────
# Callback adapters for the existing seams
# ────────────────────────────────────────────────────────────────────────


class TestCallbackAdapters:
    def test_watchdog_hung_callback(self) -> None:
        fake = FakeNotifier()
        cb = SweepAlerter(notifier=fake).watchdog_hung_callback()
        event = FakeWorkerEvent(
            worker_name="sweep-worker-0",
            last_beat_seconds_ago=999.0,
            timeout_seconds=300.0,
        )
        cb(event)
        assert len(fake.published) == 1
        assert fake.published[0].kind == TelegramEventKind.WORKER_HUNG

    def test_symbol_failure_callback(self) -> None:
        fake = FakeNotifier()
        cb = SweepAlerter(notifier=fake).symbol_failure_callback()
        event = FakeSymbolFailureEvent(symbol="SENSEX", failed_windows=15, total_windows=15)
        cb(event)
        assert len(fake.published) == 1
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.SYMBOL_WINDOW_FAILURE
        assert ev.dedupe_key == "SENSEX"

    def test_rate_breach_callback_throttles_then_alerts(self) -> None:
        fake = FakeNotifier()
        controller = FakeController(current_rps=4.0, recent_hits=3)
        cb = SweepAlerter(notifier=fake).rate_breach_callback(controller)
        cb()
        # The controller's own throttle ran FIRST, then a single deduped alert.
        assert controller.rate_limit_calls == 1
        assert len(fake.published) == 1
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.RATE_LIMIT_DOWNSHIFT
        assert "4 r/s" in ev.message  # current_rps formatted as {:.0f}

    def test_rate_breach_callback_runs_even_when_disabled(self) -> None:
        # A disabled channel must NOT skip the controller's real throttle.
        controller = FakeController()
        cb = SweepAlerter(resolver=lambda: None).rate_breach_callback(controller)
        cb()
        assert controller.rate_limit_calls == 1


# ────────────────────────────────────────────────────────────────────────
# Stress — a high-volume burst never raises + never leaks an alert
# ────────────────────────────────────────────────────────────────────────


class TestStress:
    def test_many_publishes_no_error(self) -> None:
        fake = FakeNotifier()
        alerter = SweepAlerter(notifier=fake)
        for i in range(5_000):
            alerter.watchdog_hung(
                worker_name=f"w{i}", last_beat_seconds_ago=float(i), timeout_seconds=300.0
            )
        assert len(fake.published) == 5_000

    def test_burst_with_intermittent_failures(self) -> None:
        # A flaky notifier (raises on publish) must never bubble an exception.
        flaky = FakeNotifier(raise_on_publish=True)
        alerter = SweepAlerter(notifier=flaky)
        results = [alerter.rate_breach(current_rps=3.0, recent_hits=i) for i in range(1_000)]
        assert all(r is False for r in results)


# ────────────────────────────────────────────────────────────────────────
# lake_heal_report + emit_lake_heal_alert (DEC-LAKE-HEAL-001)
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FakeHealAction:
    """Typed :class:`LakeHealActionLike` stand-in (matches ``lake_heal.HealAction``)."""

    dropped: int
    conflicts: int
    quarantine_path: str | None


@dataclass(frozen=True)
class FakeHealReport:
    """Typed :class:`LakeHealReportLike` stand-in (matches ``lake_heal.HealReport``)."""

    mode: str
    findings: int
    healed: int
    actions: tuple[FakeHealAction, ...]


class TestLakeHealReport:
    def test_heal_batch_warns_with_counts(self) -> None:
        fake = FakeNotifier()
        ok = SweepAlerter(notifier=fake).lake_heal_report(
            mode="auto",
            findings=3,
            healed=3,
            quarantined=2,
            dropped=5,
            conflicts=1,
            scope="reconcile",
        )
        assert ok is True
        (event,) = fake.published
        assert event.kind == TelegramEventKind.QUARANTINE_NEW
        assert event.severity == TelegramSeverity.WARN
        assert event.dedupe_key == "lake-heal:reconcile"
        assert "findings\\=3" in event.message
        assert "healed\\=3" in event.message
        assert "quarantined\\=2" in event.message
        assert "dropped\\=5" in event.message
        assert "conflicts\\=1" in event.message

    def test_observe_with_findings_is_critical(self) -> None:
        """Poison found, NOTHING fixed (observe mode) → CRITICAL."""
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).lake_heal_report(
            mode="observe",
            findings=2,
            healed=0,
            quarantined=0,
            dropped=0,
            conflicts=0,
            scope="startup-sweep",
        )
        (event,) = fake.published
        assert event.severity == TelegramSeverity.CRITICAL
        assert "NOTHING fixed" in event.message
        assert event.dedupe_key == "lake-heal:startup-sweep"

    def test_refused_mass_heal_is_critical(self) -> None:
        """Findings but healed==0 (a refused / all-errored auto batch) → CRITICAL."""
        fake = FakeNotifier()
        SweepAlerter(notifier=fake).lake_heal_report(
            mode="auto",
            findings=11,
            healed=0,
            quarantined=0,
            dropped=0,
            conflicts=0,
            scope="pull:abc",
        )
        (event,) = fake.published
        assert event.severity == TelegramSeverity.CRITICAL

    def test_disabled_notifier_returns_false(self) -> None:
        ok = SweepAlerter(resolver=lambda: None).lake_heal_report(
            mode="auto",
            findings=1,
            healed=1,
            quarantined=1,
            dropped=1,
            conflicts=0,
            scope="reconcile",
        )
        assert ok is False


class TestEmitLakeHealAlert:
    def _report(self, *, findings: int = 2, healed: int = 2, mode: str = "auto") -> FakeHealReport:
        return FakeHealReport(
            mode=mode,
            findings=findings,
            healed=healed,
            actions=(
                FakeHealAction(dropped=3, conflicts=1, quarantine_path="/q/a.parquet"),
                FakeHealAction(dropped=2, conflicts=0, quarantine_path=None),
            ),
        )

    def test_maps_report_sums_onto_alert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, object]] = []

        class _FakeAlerter:
            def lake_heal_report(self, **kwargs: object) -> bool:
                calls.append(kwargs)
                return True

        from brutex.notifications import sweep_alerts as mod

        monkeypatch.setattr(mod, "SweepAlerter", _FakeAlerter)
        emit_lake_heal_alert(self._report(), scope="pull:xyz")
        (kwargs,) = calls
        assert kwargs == {
            "mode": "auto",
            "findings": 2,
            "healed": 2,
            "quarantined": 1,  # only the action WITH a quarantine path
            "dropped": 5,
            "conflicts": 1,
            "scope": "pull:xyz",
        }

    def test_zero_findings_never_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A clean scan never alerts (the skip-entirely contract)."""

        class _BoomAlerter:
            def lake_heal_report(self, **_kwargs: object) -> bool:
                raise AssertionError("a zero-findings report must not reach the alerter")

        from brutex.notifications import sweep_alerts as mod

        monkeypatch.setattr(mod, "SweepAlerter", _BoomAlerter)
        report = FakeHealReport(mode="auto", findings=0, healed=0, actions=())
        emit_lake_heal_alert(report, scope="reconcile")  # no raise = never reached

    def test_swallows_its_own_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The blanket guard: an alert failure can never fail the heal."""

        class _RaisingAlerter:
            def lake_heal_report(self, **_kwargs: object) -> bool:
                raise RuntimeError("simulated alert failure")

        from brutex.notifications import sweep_alerts as mod

        monkeypatch.setattr(mod, "SweepAlerter", _RaisingAlerter)
        emit_lake_heal_alert(self._report(), scope="reconcile")  # must not raise

    def test_real_heal_report_satisfies_protocol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The REAL ``lake_heal.HealReport`` structurally satisfies the alert
        protocol (the cross-layer seam pin; imported lazily — this module
        stays wheel-free at module scope)."""
        pytest.importorskip("polars")
        from brutex.data.lake_heal import HealAction, HealReport

        report = HealReport(
            mode="auto",
            findings=1,
            healed=1,
            skipped=0,
            errors=0,
            refused_mass_heal=False,
            actions=(
                HealAction(
                    action="LAKE_HEAL_DEDUP",
                    partition="bars/NSE/INDEX/NIFTY/1minute/2024/06.parquet",
                    kept=2,
                    dropped=1,
                    moved=0,
                    conflicts=0,
                    raw_hash_before="a" * 64,
                    raw_hash_after="b" * 64,
                    quarantine_path="/lake/.quarantine/ts/x.parquet",
                ),
            ),
            quarantine_root="/lake/.quarantine/ts",
            recall=None,
        )
        fake = FakeNotifier()
        from brutex.notifications import sweep_alerts as mod

        monkeypatch.setattr(mod, "SweepAlerter", lambda: SweepAlerter(notifier=fake))
        emit_lake_heal_alert(report, scope="reconcile")
        (event,) = fake.published
        assert event.kind == TelegramEventKind.QUARANTINE_NEW
        assert event.severity == TelegramSeverity.WARN
        assert "quarantined\\=1" in event.message
