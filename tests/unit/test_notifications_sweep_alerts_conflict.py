"""Vendor-conflict-drift alert surface (DEC-VENDOR-CONFLICT-SURFACE-001).

ONE per-pull Telegram alert when a pull's MAX keep-first close delta reaches
the ``BRUTEX_CONFLICT_CLOSE_DELTA_ALERT`` threshold (exact-Decimal compare,
default ``1.00`` rupees). Best-effort: never crashes the pull, silent no-op
below threshold / with Telegram disabled.

Fail-before proof: ``SweepAlerter.vendor_conflict_drift`` /
``emit_vendor_conflict_alert`` / ``conflict_alert_threshold`` do not exist on
origin/main (``AttributeError`` / ``ImportError``) — this suite cannot pass
there.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.notifications import sweep_alerts
from brutex.notifications.sweep_alerts import (
    CONFLICT_DELTA_ALERT_ENV,
    SweepAlerter,
    conflict_alert_threshold,
    emit_vendor_conflict_alert,
)
from brutex.notifications.telegram import TelegramEvent, TelegramEventKind, TelegramSeverity
from structlog.testing import capture_logs


class FakeNotifier:
    """Captures every published :class:`TelegramEvent`."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        self.published.append(event)


class TestThreshold:
    def test_default_is_one_rupee(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CONFLICT_DELTA_ALERT_ENV, raising=False)
        assert conflict_alert_threshold() == Decimal("1.00")

    def test_env_override_exact_decimal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CONFLICT_DELTA_ALERT_ENV, "0.25")
        assert conflict_alert_threshold() == Decimal("0.25")

    def test_malformed_env_falls_back_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CONFLICT_DELTA_ALERT_ENV, "not-a-number")
        with capture_logs() as logs:
            assert conflict_alert_threshold() == Decimal("1.00")
        assert any(e["event"] == "conflict_alert_threshold_invalid" for e in logs)


class TestVendorConflictDriftMethod:
    def test_publishes_warn_with_fixed_dedupe_key(self) -> None:
        fake = FakeNotifier()
        assert (
            SweepAlerter(notifier=fake).vendor_conflict_drift(
                conflicts=25, max_close_delta_display="₹44.70", dup_rows=9141
            )
            is True
        )
        ev = fake.published[0]
        assert ev.kind == TelegramEventKind.RATE_LIMIT_DOWNSHIFT
        assert ev.severity == TelegramSeverity.WARN
        assert ev.dedupe_key == "vendor-conflict-drift"
        assert "25" in ev.message
        assert "9141" in ev.message


class TestEmitVendorConflictAlert:
    def _fires(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        delta: Decimal | None,
        env: str | None = None,
    ) -> list[dict[str, object]]:
        """Run the emit helper against a spy SweepAlerter method; return calls."""
        if env is None:
            monkeypatch.delenv(CONFLICT_DELTA_ALERT_ENV, raising=False)
        else:
            monkeypatch.setenv(CONFLICT_DELTA_ALERT_ENV, env)
        calls: list[dict[str, object]] = []

        def _spy(self: SweepAlerter, **kw: object) -> bool:
            calls.append(kw)
            return True

        monkeypatch.setattr(sweep_alerts.SweepAlerter, "vendor_conflict_drift", _spy)
        emit_vendor_conflict_alert(conflicts=25, max_close_delta=delta, dup_rows=9141)
        return calls

    def test_fires_exactly_once_at_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._fires(monkeypatch, delta=Decimal("1.00"))
        assert len(calls) == 1
        assert calls[0]["conflicts"] == 25
        assert calls[0]["dup_rows"] == 9141
        assert calls[0]["max_close_delta_display"] == "₹1.00"

    def test_display_rides_brutex_money_grouping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEC-MONEY-2DP-001: the display goes through format_inr (₹, 2dp)."""
        calls = self._fires(monkeypatch, delta=Decimal("44.70"))
        assert calls[0]["max_close_delta_display"] == "₹44.70"

    def test_below_threshold_never_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._fires(monkeypatch, delta=Decimal("0.99")) == []

    def test_none_delta_never_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._fires(monkeypatch, delta=None) == []

    def test_env_threshold_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._fires(monkeypatch, delta=Decimal("0.30"), env="0.25")
        assert self._fires(monkeypatch, delta=Decimal("0.20"), env="0.25") == []

    def test_alert_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A raising alerter is logged + swallowed — the pull can never crash."""
        monkeypatch.delenv(CONFLICT_DELTA_ALERT_ENV, raising=False)

        def _boom(self: SweepAlerter, **kw: object) -> bool:
            raise RuntimeError("simulated alert failure")

        monkeypatch.setattr(sweep_alerts.SweepAlerter, "vendor_conflict_drift", _boom)
        with capture_logs() as logs:
            assert (
                emit_vendor_conflict_alert(conflicts=1, max_close_delta=Decimal("5.00"), dup_rows=0)
                is False
            )
        assert any(e["event"] == "vendor_conflict_alert_failed" for e in logs)

    def test_disabled_telegram_is_silent_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default (no Telegram configured) publishes nothing and returns False."""
        monkeypatch.delenv(CONFLICT_DELTA_ALERT_ENV, raising=False)
        monkeypatch.setattr(
            sweep_alerts,
            "_resolve_singleton_notifier",
            lambda: None,
        )
        assert (
            emit_vendor_conflict_alert(conflicts=1, max_close_delta=Decimal("5.00"), dup_rows=2)
            is False
        )
