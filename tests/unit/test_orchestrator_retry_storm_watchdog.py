"""Unit tests for the retry-storm give-up guard (DEC-RATE-STORM-WATCHDOG-001).

Covers the per-``(symbol, window)`` give-up policy oracle in
:mod:`brutex.orchestrator.watchdog`: time-based give-up at the exact boundary,
the once-per-symbol 100%-window-failure alert, the terminal-failure fast path,
hash-chained audit stamping, and the graceful-degradation guarantees (a buggy
callback / a failing audit sink never crash the run).

All timing uses a fake clock — no test ever sleeps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from brutex.models import BruteXError
from brutex.orchestrator.watchdog import (
    AUDIT_ACTION_GIVE_UP,
    AUDIT_ACTION_SYMBOL_FAILURE,
    GiveUpEvent,
    GiveUpReason,
    RetryStormWatchdog,
    SymbolFailureEvent,
    build_retry_storm_watchdog,
)

# ────────────────────────────────────────────────────────────────────────
# Test doubles
# ────────────────────────────────────────────────────────────────────────


class FakeClock:
    """Deterministic monotonic clock the tests advance by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class RecordingAuditSink:
    """Captures every appended row; matches the :class:`AuditSink` surface."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    def append(self, *, action: str, subject: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = {"action": action, "subject": subject, "payload": payload}
        self.rows.append(entry)
        return entry


class RaisingAuditSink:
    """Audit sink whose append always raises the given exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def append(self, *, action: str, subject: str, payload: dict[str, Any]) -> Any:
        raise self._exc


# ────────────────────────────────────────────────────────────────────────
# Construction + validation
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0.0, -1.0, -0.0001])
def test_rejects_nonpositive_max_retry_seconds(bad: float) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        RetryStormWatchdog(max_retry_seconds=bad)


def test_build_factory_converts_minutes_to_seconds() -> None:
    wd = build_retry_storm_watchdog(max_retry_minutes=2)
    assert wd.max_retry_seconds == 120.0


def test_build_factory_uses_supplied_callbacks() -> None:
    seen: list[str] = []
    wd = build_retry_storm_watchdog(
        max_retry_minutes=1,
        on_give_up=lambda e: seen.append(f"giveup:{e.window}"),
        on_symbol_failure=lambda e: seen.append(f"symbol:{e.symbol}"),
    )
    wd.register("NIFTY", "w0")
    assert wd.record_terminal_failure("NIFTY", "w0") is True
    assert seen == ["giveup:w0", "symbol:NIFTY"]


# ────────────────────────────────────────────────────────────────────────
# Registration
# ────────────────────────────────────────────────────────────────────────


def test_register_is_idempotent() -> None:
    wd = RetryStormWatchdog(max_retry_seconds=60.0)
    wd.register("NIFTY", "w0")
    wd.register("NIFTY", "w0")  # duplicate — must not double-count
    wd.register("NIFTY", "w1")
    assert wd.window_total("NIFTY") == 2
    assert wd.given_up_count("NIFTY") == 0


def test_register_fanout_is_full_product() -> None:
    wd = RetryStormWatchdog(max_retry_seconds=60.0)
    wd.register_fanout(["A", "B"], ["w0", "w1", "w2"])
    assert wd.window_total("A") == 3
    assert wd.window_total("B") == 3


def test_recording_unregistered_tuple_raises() -> None:
    wd = RetryStormWatchdog(max_retry_seconds=60.0)
    with pytest.raises(KeyError, match="was not registered"):
        wd.record_failure("GHOST", "w0")


# ────────────────────────────────────────────────────────────────────────
# Time-based give-up — the headline behaviour
# ────────────────────────────────────────────────────────────────────────


def test_give_up_fires_exactly_at_the_boundary() -> None:
    clock = FakeClock()
    events: list[GiveUpEvent] = []
    wd = RetryStormWatchdog(max_retry_seconds=300.0, on_give_up=events.append, clock=clock)
    wd.register("NIFTY", "w0")

    # First failure only ARMS the streak timer — no give-up yet.
    assert wd.record_failure("NIFTY", "w0") is False
    assert wd.has_given_up("NIFTY", "w0") is False

    # One tick below the boundary: still no give-up.
    clock.advance(299.999)
    assert wd.record_failure("NIFTY", "w0") is False
    assert wd.has_given_up("NIFTY", "w0") is False

    # EXACTLY at the boundary (streak == max_retry_seconds): give up now.
    clock.t = 300.0
    assert wd.record_failure("NIFTY", "w0") is True
    assert wd.has_given_up("NIFTY", "w0") is True

    assert len(events) == 1
    assert events[0].reason is GiveUpReason.TIMEOUT
    assert events[0].failure_seconds == 300.0
    assert events[0].max_retry_seconds == 300.0


def test_record_failure_after_give_up_is_noop() -> None:
    clock = FakeClock()
    wd = RetryStormWatchdog(max_retry_seconds=10.0, clock=clock)
    wd.register("NIFTY", "w0")
    wd.record_failure("NIFTY", "w0")
    clock.advance(10.0)
    assert wd.record_failure("NIFTY", "w0") is True
    # Already terminal — further failures are a no-op.
    assert wd.record_failure("NIFTY", "w0") is False


def test_success_resets_the_continuous_failure_streak() -> None:
    clock = FakeClock()
    wd = RetryStormWatchdog(max_retry_seconds=100.0, clock=clock)
    wd.register("NIFTY", "w0")

    wd.record_failure("NIFTY", "w0")  # arm at t=0
    clock.advance(80.0)
    wd.record_success("NIFTY", "w0")  # streak cleared at t=80

    # The next failure RE-ARMS the streak from t=160 (NOT from t=0) ...
    clock.advance(80.0)
    assert wd.record_failure("NIFTY", "w0") is False
    # ... so it only gives up a full 100s LATER, at t=260 — proving the
    # success genuinely reset the timer (else it would have fired at t=100).
    clock.advance(100.0)
    assert wd.record_failure("NIFTY", "w0") is True


def test_success_on_given_up_tuple_stays_terminal() -> None:
    clock = FakeClock()
    wd = RetryStormWatchdog(max_retry_seconds=5.0, clock=clock)
    wd.register("NIFTY", "w0")
    wd.record_failure("NIFTY", "w0")
    clock.advance(5.0)
    assert wd.record_failure("NIFTY", "w0") is True
    wd.record_success("NIFTY", "w0")  # late success
    assert wd.has_given_up("NIFTY", "w0") is True


# ────────────────────────────────────────────────────────────────────────
# Terminal-failure fast path
# ────────────────────────────────────────────────────────────────────────


def test_terminal_failure_gives_up_immediately_with_zero_streak() -> None:
    events: list[GiveUpEvent] = []
    wd = RetryStormWatchdog(max_retry_seconds=600.0, on_give_up=events.append)
    wd.register("NIFTY", "w0")
    assert wd.record_terminal_failure("NIFTY", "w0") is True
    assert events[0].reason is GiveUpReason.TERMINAL
    assert events[0].failure_seconds == 0.0


def test_terminal_failure_uses_existing_streak_when_armed() -> None:
    clock = FakeClock()
    events: list[GiveUpEvent] = []
    wd = RetryStormWatchdog(max_retry_seconds=600.0, on_give_up=events.append, clock=clock)
    wd.register("NIFTY", "w0")
    wd.record_failure("NIFTY", "w0")  # arm at t=0
    clock.advance(42.0)
    assert wd.record_terminal_failure("NIFTY", "w0") is True
    assert events[0].reason is GiveUpReason.TERMINAL
    assert events[0].failure_seconds == 42.0


def test_terminal_failure_after_give_up_is_noop() -> None:
    wd = RetryStormWatchdog(max_retry_seconds=600.0)
    wd.register("NIFTY", "w0")
    assert wd.record_terminal_failure("NIFTY", "w0") is True
    assert wd.record_terminal_failure("NIFTY", "w0") is False


# ────────────────────────────────────────────────────────────────────────
# Per-symbol 100%-window-failure alert — once per symbol, not per window
# ────────────────────────────────────────────────────────────────────────


def test_symbol_alert_fires_once_after_all_windows_give_up() -> None:
    alerts: list[SymbolFailureEvent] = []
    wd = RetryStormWatchdog(max_retry_seconds=60.0, on_symbol_failure=alerts.append)
    wd.register_fanout(["NIFTY"], ["w0", "w1", "w2"])

    wd.record_terminal_failure("NIFTY", "w0")
    assert alerts == []  # 1/3 — no alert
    wd.record_terminal_failure("NIFTY", "w1")
    assert alerts == []  # 2/3 — no alert
    wd.record_terminal_failure("NIFTY", "w2")  # 3/3 — alert

    assert len(alerts) == 1
    assert alerts[0] == SymbolFailureEvent(symbol="NIFTY", failed_windows=3, total_windows=3)
    assert wd.symbol_failed("NIFTY") is True


def test_symbol_alert_is_isolated_per_symbol() -> None:
    alerts: list[str] = []
    wd = RetryStormWatchdog(
        max_retry_seconds=60.0, on_symbol_failure=lambda e: alerts.append(e.symbol)
    )
    wd.register_fanout(["A", "B"], ["w0", "w1"])

    wd.record_terminal_failure("A", "w0")
    wd.record_terminal_failure("A", "w1")  # A is 100% → alert
    wd.record_terminal_failure("B", "w0")  # B is 50% → no alert
    assert alerts == ["A"]
    assert wd.symbol_failed("B") is False


# ────────────────────────────────────────────────────────────────────────
# Audit stamping
# ────────────────────────────────────────────────────────────────────────


def test_audit_rows_written_for_give_up_and_symbol_failure() -> None:
    sink = RecordingAuditSink()
    wd = RetryStormWatchdog(max_retry_seconds=60.0, audit=sink)
    wd.register_fanout(["NIFTY"], ["w0", "w1"])

    wd.record_terminal_failure("NIFTY", "w0")
    wd.record_terminal_failure("NIFTY", "w1")

    actions = [r["action"] for r in sink.rows]
    # Two give-up rows + one symbol-failure row, in order.
    assert actions == [
        AUDIT_ACTION_GIVE_UP,
        AUDIT_ACTION_GIVE_UP,
        AUDIT_ACTION_SYMBOL_FAILURE,
    ]
    assert sink.rows[0]["subject"] == "NIFTY|w0"
    assert sink.rows[0]["payload"]["reason"] == "terminal"
    assert sink.rows[-1]["subject"] == "NIFTY"
    assert sink.rows[-1]["payload"] == {"failed_windows": 2, "total_windows": 2}


def test_audit_none_is_a_silent_skip() -> None:
    wd = RetryStormWatchdog(max_retry_seconds=60.0, audit=None)
    wd.register("NIFTY", "w0")
    # No audit sink wired — must still give up cleanly without raising.
    assert wd.record_terminal_failure("NIFTY", "w0") is True


@pytest.mark.parametrize("exc", [OSError("disk full"), BruteXError("chain broke")])
def test_audit_append_failure_is_swallowed(exc: BaseException) -> None:
    wd = RetryStormWatchdog(max_retry_seconds=60.0, audit=RaisingAuditSink(exc))
    wd.register("NIFTY", "w0")
    # A failing audit write must NOT crash the run; give-up state still recorded.
    assert wd.record_terminal_failure("NIFTY", "w0") is True
    assert wd.has_given_up("NIFTY", "w0") is True


# ────────────────────────────────────────────────────────────────────────
# Callback fault-isolation
# ────────────────────────────────────────────────────────────────────────


def test_give_up_callback_exception_is_swallowed() -> None:
    def boom(_e: GiveUpEvent) -> None:
        raise RuntimeError("callback bug")

    wd = RetryStormWatchdog(max_retry_seconds=60.0, on_give_up=boom)
    wd.register("NIFTY", "w0")
    assert wd.record_terminal_failure("NIFTY", "w0") is True


def test_symbol_failure_callback_exception_is_swallowed() -> None:
    def boom(_e: SymbolFailureEvent) -> None:
        raise ValueError("callback bug")

    wd = RetryStormWatchdog(max_retry_seconds=60.0, on_symbol_failure=boom)
    wd.register("NIFTY", "w0")
    assert wd.record_terminal_failure("NIFTY", "w0") is True
    assert wd.symbol_failed("NIFTY") is True


# ────────────────────────────────────────────────────────────────────────
# Queries
# ────────────────────────────────────────────────────────────────────────


def test_queries_for_unknown_symbol_default_safely() -> None:
    wd = RetryStormWatchdog(max_retry_seconds=60.0)
    assert wd.given_up_count("UNKNOWN") == 0
    assert wd.window_total("UNKNOWN") == 0
    assert wd.symbol_failed("UNKNOWN") is False
    assert wd.has_given_up("UNKNOWN", "w0") is False


# ────────────────────────────────────────────────────────────────────────
# Progress-stream adapter
# ────────────────────────────────────────────────────────────────────────


def test_progress_hook_routes_failed_and_success() -> None:
    wd = RetryStormWatchdog(max_retry_seconds=600.0)
    wd.register("NIFTY", "1minute")
    hook = wd.progress_hook()

    hook("NIFTY 1minute", 0, False, False)  # success → no give-up
    assert wd.has_given_up("NIFTY", "1minute") is False
    hook("NIFTY 1minute", 0, True, False)  # terminal failure → give up
    assert wd.has_given_up("NIFTY", "1minute") is True


def test_progress_hook_ignores_no_data_gap_ticks() -> None:
    # DEC-DHAN-NO-DATA-GAP-COUNT-001: a benign DH-905 gap tick (gap=True) is
    # neither a failure nor a success — it must NOT trip the give-up, so a
    # recurring known gap (e.g. NIFTY NEXT 50's empty pre-2021 daily) can never
    # raise a false retry-storm alert.
    wd = RetryStormWatchdog(max_retry_seconds=600.0)
    wd.register("NIFTY NEXT 50", "1day")
    hook = wd.progress_hook()
    hook("NIFTY NEXT 50 1day", 0, False, True)  # gap → ignored
    assert wd.has_given_up("NIFTY NEXT 50", "1day") is False


def test_progress_hook_ignores_unregistered_label() -> None:
    wd = RetryStormWatchdog(max_retry_seconds=600.0)
    wd.register("NIFTY", "1minute")
    hook = wd.progress_hook()
    # Stray label — never registered. Must be ignored, never raise.
    hook("BANKNIFTY 1day", 0, True, False)
    assert wd.has_given_up("BANKNIFTY", "1day") is False


def test_progress_hook_label_without_separator() -> None:
    wd = RetryStormWatchdog(max_retry_seconds=600.0)
    wd.register("SOLO", "")  # a label with no separator resolves to window=""
    hook = wd.progress_hook()
    hook("SOLO", 0, True, False)
    assert wd.has_given_up("SOLO", "") is True
