"""Tests for ``brutex.adapters.error_storm_breaker`` (DEC-VENDOR-DOWN-BREAKER-001).

100% line+branch: the vendor-down error-storm rule (trip vs every non-trip
permutation — below-threshold, single-symbol, mixed-with-success, disabled,
rule-disabled), the dead-credential StillStale escalation (K-streak + T-minute
boundaries on an INJECTED clock, success/other-failure resets), the trip latch +
re-raise, the once-only CRITICAL alert hooks (incl. a raising hook), the
None-safe guarded observation seams (a breaker bug never crashes a pull; a TRIP
always propagates), the halt classes' generic bare-message construction, the env
resolver (defaults, overrides, fail-safe fallbacks, kill switch), and a
hypothesis property that a mixed success/failure stream NEVER trips.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from brutex.adapters.error_storm_breaker import (
    ErrorStormBreaker,
    VendorDownError,
    _resolve_minutes,  # pyright: ignore[reportPrivateUsage] — whitebox env-resolver test
    observe_window_failure,
    observe_window_success,
    resolve_error_storm_breaker,
)
from brutex.adapters.groww.constants import (
    GROWW_CREDENTIAL_DEAD_MIN_MINUTES,
    GROWW_CREDENTIAL_DEAD_MINUTES_ENV,
    GROWW_CREDENTIAL_DEAD_STREAK,
    GROWW_CREDENTIAL_DEAD_STREAK_ENV,
    GROWW_ERROR_STORM_ENABLED_ENV,
    GROWW_STORM_MIN_DISTINCT_SYMBOLS,
    GROWW_STORM_MIN_FAILED_WINDOWS,
    GROWW_STORM_MIN_FAILURES_ENV,
    GROWW_STORM_MIN_SYMBOLS_ENV,
)
from brutex.adapters.groww.errors import (
    GrowwCredentialDeadError,
    GrowwServerError,
    GrowwTimeoutError,
    GrowwTokenStillStaleError,
    is_auth_failure,
    is_retryable,
)
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs


class _Clock:
    """Deterministic injected monotonic clock — advances *step* seconds per call."""

    def __init__(self, *, step: float = 0.0, start: float = 1_000.0) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def _breaker(
    *,
    min_symbols: int = 3,
    min_failures: int = 5,
    streak: int = 4,
    min_seconds: float = 0.0,
    enabled: bool = True,
    now: Callable[[], float] | None = None,
    on_vendor: Callable[[VendorDownError], None] | None = None,
    on_cred: Callable[[GrowwCredentialDeadError], None] | None = None,
) -> ErrorStormBreaker:
    """A small-threshold breaker with recording no-op alert hooks by default."""
    return ErrorStormBreaker(
        min_distinct_symbols=min_symbols,
        min_failed_windows=min_failures,
        credential_dead_streak=streak,
        credential_dead_min_seconds=min_seconds,
        enabled=enabled,
        now=now if now is not None else _Clock(),
        alert_vendor_down=on_vendor if on_vendor is not None else (lambda _h: None),
        alert_credential_dead=on_cred if on_cred is not None else (lambda _h: None),
    )


def _fail_n(breaker: ErrorStormBreaker, n: int, *, symbol: str = "S") -> None:
    """Observe *n* non-StillStale transport failures for *symbol*."""
    for _ in range(n):
        breaker.observe_transport_failure(symbol, error_type="GrowwTimeoutError")


# ────────────────────────────────────────────────────────────────────────
# VendorDownError construction contract
# ────────────────────────────────────────────────────────────────────────


class TestVendorDownError:
    def test_bare_message_construction(self) -> None:
        """The drift-pin generic-construction contract: a bare message works."""
        exc = VendorDownError("boom")
        assert str(exc) == "boom"
        assert exc.failed_windows == 0
        assert exc.distinct_symbols == 0
        assert exc.symbols == ()

    def test_keyword_construction_names_counts_and_symbols(self) -> None:
        exc = VendorDownError(failed_windows=30, distinct_symbols=3, symbols=("A", "B", "C"))
        msg = str(exc)
        assert "VENDOR DOWN" in msg
        assert "30 windows" in msg
        assert "3 distinct symbols" in msg
        assert "A, B, C" in msg
        assert "WAIT" in msg
        assert "+ more" not in msg  # no overflow suffix under the cap

    def test_keyword_construction_caps_named_symbols(self) -> None:
        symbols = tuple(f"S{i:02d}" for i in range(13))
        exc = VendorDownError(failed_windows=13, distinct_symbols=13, symbols=symbols)
        msg = str(exc)
        assert "S09" in msg  # the 10th named symbol
        assert "S10" not in msg  # the 11th is folded into the overflow count
        assert "(+3 more)" in msg

    def test_credential_dead_bare_message_and_taxonomy(self) -> None:
        """GrowwCredentialDeadError: bare-constructible, TERMINAL, auth-family."""
        exc = GrowwCredentialDeadError("dead")
        assert str(exc) == "dead"
        assert is_retryable(exc) is False
        assert is_auth_failure(exc) is True


# ────────────────────────────────────────────────────────────────────────
# Vendor-down rule — trip vs non-trip permutations
# ────────────────────────────────────────────────────────────────────────


class TestVendorDownRule:
    def test_trips_at_exact_thresholds(self) -> None:
        b = _breaker(min_symbols=3, min_failures=5)
        _fail_n(b, 2, symbol="A")
        _fail_n(b, 2, symbol="B")
        with pytest.raises(VendorDownError) as exc_info:
            _fail_n(b, 1, symbol="C")
        assert b.tripped is True
        assert b.trip_rule == "vendor_down"
        assert exc_info.value.failed_windows == 5
        assert exc_info.value.distinct_symbols == 3
        assert exc_info.value.symbols == ("A", "B", "C")

    def test_below_failure_floor_never_trips(self) -> None:
        b = _breaker(min_symbols=3, min_failures=5)
        _fail_n(b, 1, symbol="A")
        _fail_n(b, 1, symbol="B")
        _fail_n(b, 2, symbol="C")  # 4 failures, 3 symbols — one short of the floor
        assert b.tripped is False
        assert b.counts() == (4, 3, 0)

    def test_single_symbol_blip_never_trips(self) -> None:
        """A lone symbol failing every window is a per-symbol problem — never a storm."""
        b = _breaker(min_symbols=3, min_failures=5)
        _fail_n(b, 50, symbol="ONLY")
        assert b.tripped is False
        assert b.counts() == (50, 1, 0)

    def test_two_symbols_below_distinct_floor_never_trips(self) -> None:
        b = _breaker(min_symbols=3, min_failures=5)
        _fail_n(b, 10, symbol="A")
        _fail_n(b, 10, symbol="B")
        assert b.tripped is False

    def test_any_success_resets_the_storm(self) -> None:
        """A mixed success/failure pull NEVER trips — every success zeroes the storm."""
        b = _breaker(min_symbols=2, min_failures=3)
        _fail_n(b, 2, symbol="A")
        b.observe_success("B")
        assert b.counts() == (0, 0, 0)
        _fail_n(b, 2, symbol="C")
        b.observe_success("C")
        _fail_n(b, 2, symbol="D")
        assert b.tripped is False

    def test_min_failures_zero_disables_the_rule(self) -> None:
        b = _breaker(min_symbols=1, min_failures=0)
        _fail_n(b, 100, symbol="A")
        _fail_n(b, 100, symbol="B")
        assert b.tripped is False

    def test_min_symbols_zero_clamps_to_any(self) -> None:
        b = _breaker(min_symbols=0, min_failures=3)
        with pytest.raises(VendorDownError):
            _fail_n(b, 3, symbol="ONLY")

    def test_disabled_breaker_observes_nothing(self) -> None:
        b = _breaker(min_symbols=1, min_failures=1, enabled=False)
        _fail_n(b, 10, symbol="A")
        b.observe_success("A")
        b.observe_transport_failure("A", error_type="x", still_stale=True)
        assert b.tripped is False
        assert b.counts() == (0, 0, 0)

    def test_latched_trip_keeps_raising_and_success_never_unlatches(self) -> None:
        b = _breaker(min_symbols=1, min_failures=2)
        with pytest.raises(VendorDownError):
            _fail_n(b, 2, symbol="A")
        # A stray late success from an in-flight sibling must NOT mask the halt.
        b.observe_success("A")
        assert b.tripped is True
        with pytest.raises(VendorDownError):
            b.observe_transport_failure("B", error_type="GrowwServerError")
        # Even a StillStale observation re-raises the LATCHED vendor-down halt.
        with pytest.raises(VendorDownError):
            b.observe_transport_failure("C", error_type="stale", still_stale=True)

    def test_still_stale_neither_counts_toward_nor_resets_the_storm(self) -> None:
        b = _breaker(min_symbols=1, min_failures=3, streak=0)  # stale rule disabled
        _fail_n(b, 2, symbol="A")
        b.observe_transport_failure("A", error_type="stale", still_stale=True)
        assert b.counts() == (2, 1, 1)  # storm unchanged, streak grew
        with pytest.raises(VendorDownError):
            _fail_n(b, 1, symbol="A")


# ────────────────────────────────────────────────────────────────────────
# Dead-credential StillStale escalation — K-streak + T-minute boundaries
# ────────────────────────────────────────────────────────────────────────


def _stale_n(breaker: ErrorStormBreaker, n: int, *, symbol: str = "S") -> None:
    for _ in range(n):
        breaker.observe_transport_failure(
            symbol, error_type="GrowwTokenStillStaleError", still_stale=True
        )


class TestCredentialDeadRule:
    def test_trips_at_streak_and_time_boundary(self) -> None:
        # The clock advances 60 s per read: obs1 stamps the streak start; once the
        # streak reaches K, EACH further observation reads the clock once for the
        # gate — so the elapsed span grows 60 s per post-K observation and crosses
        # the 180 s gate exactly on the 6th observation (streak 4 → 60 s,
        # 5 → 120 s, 6 → 180 s): the trip fires at the K-AND-T boundary, never on
        # streak alone.
        clock = _Clock(step=60.0)
        b = _breaker(streak=4, min_seconds=180.0, now=clock)
        _stale_n(b, 5)
        assert b.tripped is False  # streak ≥ K but the span is still < T
        with pytest.raises(GrowwCredentialDeadError) as exc_info:
            _stale_n(b, 1)
        assert b.trip_rule == "credential_dead"
        msg = str(exc_info.value)
        assert "OPERATOR ACTION NEEDED" in msg
        assert "6 consecutive StillStale" in msg
        assert is_retryable(exc_info.value) is False  # terminal by construction

    def test_streak_met_but_span_too_short_never_trips(self) -> None:
        clock = _Clock(step=0.0)  # frozen clock — zero elapsed span
        b = _breaker(streak=4, min_seconds=180.0, now=clock)
        _stale_n(b, 10)
        assert b.tripped is False
        assert b.counts() == (0, 0, 10)

    def test_time_gate_zero_means_pure_streak(self) -> None:
        b = _breaker(streak=3, min_seconds=0.0, now=_Clock(step=0.0))
        with pytest.raises(GrowwCredentialDeadError):
            _stale_n(b, 3)

    def test_negative_span_config_clamps_to_zero(self) -> None:
        b = _breaker(streak=2, min_seconds=-5.0, now=_Clock(step=0.0))
        with pytest.raises(GrowwCredentialDeadError):
            _stale_n(b, 2)

    def test_streak_zero_disables_the_rule(self) -> None:
        b = _breaker(streak=0, min_seconds=0.0)
        _stale_n(b, 50)
        assert b.tripped is False

    def test_adopted_token_success_clears_the_streak(self) -> None:
        clock = _Clock(step=60.0)
        b = _breaker(streak=4, min_seconds=0.0, now=clock)
        _stale_n(b, 3)
        b.observe_success("S")  # an adopted-token success — the credential works
        assert b.counts() == (0, 0, 0)
        _stale_n(b, 3)
        assert b.tripped is False  # the streak restarted — never reached 4 consecutively

    def test_other_transport_failure_breaks_strict_consecutiveness(self) -> None:
        b = _breaker(streak=3, min_seconds=0.0, min_failures=100)
        _stale_n(b, 2)
        b.observe_transport_failure("S", error_type="GrowwTimeoutError")  # breaks the run
        _stale_n(b, 2)
        assert b.tripped is False
        assert b.counts()[2] == 2

    def test_latched_credential_trip_keeps_raising(self) -> None:
        b = _breaker(streak=2, min_seconds=0.0)
        with pytest.raises(GrowwCredentialDeadError):
            _stale_n(b, 2)
        with pytest.raises(GrowwCredentialDeadError):
            b.observe_transport_failure("T", error_type="GrowwTimeoutError")


# ────────────────────────────────────────────────────────────────────────
# Trip alerts — fired ONCE at the transition, best-effort
# ────────────────────────────────────────────────────────────────────────


class TestTripAlerts:
    def test_vendor_alert_fires_exactly_once(self) -> None:
        fired: list[VendorDownError] = []
        b = _breaker(min_symbols=1, min_failures=2, on_vendor=fired.append)
        with capture_logs() as logs:
            with pytest.raises(VendorDownError):
                _fail_n(b, 2, symbol="A")
            with pytest.raises(VendorDownError):  # re-raise, NOT a re-alert
                _fail_n(b, 1, symbol="B")
        assert len(fired) == 1
        assert fired[0].failed_windows == 2
        trip_events = [e for e in logs if e["event"] == "error_storm.tripped"]
        assert len(trip_events) == 1
        assert trip_events[0]["rule"] == "vendor_down"

    def test_credential_alert_fires_exactly_once(self) -> None:
        fired: list[GrowwCredentialDeadError] = []
        b = _breaker(streak=2, min_seconds=0.0, on_cred=fired.append)
        with capture_logs() as logs:
            with pytest.raises(GrowwCredentialDeadError):
                _stale_n(b, 2)
            with pytest.raises(GrowwCredentialDeadError):
                _stale_n(b, 1)
        assert len(fired) == 1
        trip_events = [e for e in logs if e["event"] == "error_storm.tripped"]
        assert len(trip_events) == 1
        assert trip_events[0]["rule"] == "credential_dead"

    def test_raising_alert_hook_never_masks_the_halt(self) -> None:
        def _boom(_halt: VendorDownError) -> None:
            raise RuntimeError("notifier blew up")

        b = _breaker(min_symbols=1, min_failures=1, on_vendor=_boom)
        with capture_logs() as logs, pytest.raises(VendorDownError):
            # The HALT propagates, never the notifier's RuntimeError.
            _fail_n(b, 1)
        assert any(e["event"] == "error_storm.trip_alert_failed" for e in logs)

    def test_default_vendor_alert_publishes_via_sweep_alerter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[int, int]] = []

        class _FakeAlerter:
            def vendor_down(self, *, failed_windows: int, distinct_symbols: int) -> bool:
                captured.append((failed_windows, distinct_symbols))
                return True

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
        b = ErrorStormBreaker(min_distinct_symbols=1, min_failed_windows=1)
        with pytest.raises(VendorDownError):
            b.observe_transport_failure("A", error_type="GrowwTimeoutError")
        assert captured == [(1, 1)]

    def test_default_credential_alert_publishes_via_sweep_alerter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[str] = []

        class _FakeAlerter:
            def credential_dead(self, *, reason: str) -> bool:
                captured.append(reason)
                return True

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
        b = ErrorStormBreaker(
            credential_dead_streak=1, credential_dead_min_seconds=0.0, now=_Clock()
        )
        with pytest.raises(GrowwCredentialDeadError):
            b.observe_transport_failure("A", error_type="stale", still_stale=True)
        assert captured == ["GrowwCredentialDeadError"]

    def test_default_alert_swallows_its_own_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failure CONSTRUCTING the alerter is swallowed — the halt still raises."""

        class _BoomAlerter:
            def __init__(self) -> None:
                raise RuntimeError("alerter construction blew up")

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _BoomAlerter)
        b = ErrorStormBreaker(min_distinct_symbols=1, min_failed_windows=1)
        with capture_logs() as logs, pytest.raises(VendorDownError):
            b.observe_transport_failure("A", error_type="GrowwTimeoutError")
        assert any(e["event"] == "error_storm.vendor_down_alert_failed" for e in logs)
        b2 = ErrorStormBreaker(credential_dead_streak=1, credential_dead_min_seconds=0.0)
        with capture_logs() as logs, pytest.raises(GrowwCredentialDeadError):
            b2.observe_transport_failure("A", error_type="stale", still_stale=True)
        assert any(e["event"] == "error_storm.credential_dead_alert_failed" for e in logs)


# ────────────────────────────────────────────────────────────────────────
# The COMMON guarded observation seams (what both spines call)
# ────────────────────────────────────────────────────────────────────────


class TestGuardedSeams:
    def test_none_breaker_is_a_no_op(self) -> None:
        observe_window_success(None, symbol="A")
        observe_window_failure(None, symbol="A", exc=GrowwTimeoutError("t"))

    def test_failure_seam_classifies_still_stale_by_isinstance(self) -> None:
        b = _breaker(streak=100, min_failures=100)
        observe_window_failure(b, symbol="A", exc=GrowwTokenStillStaleError("stale"))
        observe_window_failure(b, symbol="A", exc=GrowwServerError("500"))
        # The StillStale fed the streak (then the 500 broke it) and only the 500
        # counted toward the storm — the isinstance routing worked.
        assert b.counts() == (1, 1, 0)

    def test_success_seam_resets(self) -> None:
        b = _breaker(min_failures=100)
        _fail_n(b, 3, symbol="A")
        observe_window_success(b, symbol="A")
        assert b.counts() == (0, 0, 0)

    def test_trips_propagate_through_the_failure_seam(self) -> None:
        b = _breaker(min_symbols=1, min_failures=1)
        with pytest.raises(VendorDownError):
            observe_window_failure(b, symbol="A", exc=GrowwTimeoutError("t"))
        b2 = _breaker(streak=1, min_seconds=0.0)
        with pytest.raises(GrowwCredentialDeadError):
            observe_window_failure(b2, symbol="A", exc=GrowwTokenStillStaleError("s"))

    def test_breaker_bug_never_crashes_a_pull(self) -> None:
        """An internal defect in either observation is swallowed + logged."""

        class _BuggyBreaker(ErrorStormBreaker):
            def observe_success(self, symbol: str) -> None:
                raise RuntimeError("bug in success path")

            def observe_transport_failure(
                self, symbol: str, *, error_type: str, still_stale: bool = False
            ) -> None:
                raise RuntimeError("bug in failure path")

        buggy = _BuggyBreaker()
        with capture_logs() as logs:
            observe_window_success(buggy, symbol="A")
            observe_window_failure(buggy, symbol="A", exc=GrowwTimeoutError("t"))
        events = {e["event"] for e in logs}
        assert "error_storm.observe_success_failed" in events
        assert "error_storm.observe_failure_failed" in events

    def test_thread_fanout_trips_exactly_once_alerted(self) -> None:
        """8 workers hammering one breaker: every post-trip observe raises, one alert."""
        fired: list[VendorDownError] = []
        b = _breaker(min_symbols=2, min_failures=10, on_vendor=fired.append)
        barrier = threading.Barrier(8)
        raises: list[BaseException] = []

        def _worker(idx: int) -> None:
            barrier.wait()
            for _ in range(10):
                try:
                    b.observe_transport_failure(f"S{idx % 4}", error_type="t")
                except VendorDownError as exc:
                    raises.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert b.tripped is True
        assert len(fired) == 1  # the alert fired exactly once
        assert raises  # at least the tripping observation raised


# ────────────────────────────────────────────────────────────────────────
# Property: a mixed success/failure stream NEVER trips
# ────────────────────────────────────────────────────────────────────────


@given(
    st.lists(
        st.tuples(st.sampled_from(["ok", "fail"]), st.sampled_from(["A", "B", "C", "D"])),
        max_size=60,
    )
)
def test_property_mixed_stream_with_frequent_successes_never_trips(
    events: list[tuple[str, str]],
) -> None:
    """Any stream whose failure RUNS stay below the floor can never trip.

    We inject a success whenever a run would reach the floor, modelling a vendor
    that keeps answering at least sometimes — the breaker must stay silent.
    """
    b = _breaker(min_symbols=2, min_failures=8)
    run = 0
    for kind, symbol in events:
        if kind == "fail" and run == 7:
            b.observe_success(symbol)
            run = 0
        if kind == "ok":
            b.observe_success(symbol)
            run = 0
        else:
            b.observe_transport_failure(symbol, error_type="t")
            run += 1
    assert b.tripped is False


@given(st.integers(min_value=1, max_value=200))
def test_property_single_symbol_never_trips(n: int) -> None:
    """However many failures ONE symbol produces, the distinct floor holds."""
    b = _breaker(min_symbols=2, min_failures=5)
    _fail_n(b, n, symbol="ONLY")
    assert b.tripped is False


# ────────────────────────────────────────────────────────────────────────
# resolve_error_storm_breaker — env config
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A monkeypatch with every breaker env knob cleared (isolation)."""
    for name in (
        GROWW_STORM_MIN_SYMBOLS_ENV,
        GROWW_STORM_MIN_FAILURES_ENV,
        GROWW_CREDENTIAL_DEAD_STREAK_ENV,
        GROWW_CREDENTIAL_DEAD_MINUTES_ENV,
        GROWW_ERROR_STORM_ENABLED_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.usefixtures("clean_env")
class TestResolveEnvConfig:
    def test_defaults(self) -> None:
        b = resolve_error_storm_breaker()
        assert b.min_distinct_symbols == GROWW_STORM_MIN_DISTINCT_SYMBOLS
        assert b.min_failed_windows == GROWW_STORM_MIN_FAILED_WINDOWS
        assert b.credential_dead_streak == GROWW_CREDENTIAL_DEAD_STREAK
        assert b.credential_dead_min_seconds == GROWW_CREDENTIAL_DEAD_MIN_MINUTES * 60.0
        assert b.enabled is True

    def test_env_overrides(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(GROWW_STORM_MIN_SYMBOLS_ENV, "5")
        clean_env.setenv(GROWW_STORM_MIN_FAILURES_ENV, "40")
        clean_env.setenv(GROWW_CREDENTIAL_DEAD_STREAK_ENV, "12")
        clean_env.setenv(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, "2.5")
        b = resolve_error_storm_breaker()
        assert b.min_distinct_symbols == 5
        assert b.min_failed_windows == 40
        assert b.credential_dead_streak == 12
        assert b.credential_dead_min_seconds == 150.0

    def test_bad_values_fail_safe_to_defaults(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(GROWW_STORM_MIN_FAILURES_ENV, "not-an-int")
        clean_env.setenv(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, "banana")
        b = resolve_error_storm_breaker()
        assert b.min_failed_windows == GROWW_STORM_MIN_FAILED_WINDOWS
        assert b.credential_dead_min_seconds == GROWW_CREDENTIAL_DEAD_MIN_MINUTES * 60.0

    @pytest.mark.parametrize("off", ["off", "0", "false", "NO"])
    def test_kill_switch(self, clean_env: pytest.MonkeyPatch, off: str) -> None:
        clean_env.setenv(GROWW_ERROR_STORM_ENABLED_ENV, off)
        assert resolve_error_storm_breaker().enabled is False

    def test_non_off_value_stays_enabled(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv(GROWW_ERROR_STORM_ENABLED_ENV, "on")
        assert resolve_error_storm_breaker().enabled is True

    def test_injected_clock_is_used(self) -> None:
        clock = _Clock()
        assert resolve_error_storm_breaker(now=clock).now is clock

    def test_resolve_minutes_edges(self, clean_env: pytest.MonkeyPatch) -> None:
        assert _resolve_minutes(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, 7.0) == 7.0  # unset
        clean_env.setenv(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, "   ")
        assert _resolve_minutes(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, 7.0) == 7.0  # blank
        clean_env.setenv(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, "-1")
        assert _resolve_minutes(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, 7.0) == 7.0  # negative
        clean_env.setenv(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, "0")
        assert _resolve_minutes(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, 7.0) == 0.0  # zero OK
        clean_env.setenv(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, "3.5")
        assert _resolve_minutes(GROWW_CREDENTIAL_DEAD_MINUTES_ENV, 7.0) == 3.5
