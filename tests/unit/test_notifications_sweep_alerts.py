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
