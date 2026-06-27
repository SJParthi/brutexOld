"""Tests for :mod:`brutex.observability.failure_correlation` (DEC-FAILURE-CORRELATION-001).

100% line + branch coverage of the combined-failure correlation layer:

* :class:`FailureCorrelator` — co-occurrence detection within a tunable window,
  highest-priority root attribution + ordered symptoms, dedupe on the
  root+symptoms shape, time + size eviction, the never-raises-into-the-pull guard,
  and the single-failure pass-through.
* :class:`CorrelatedVerdict` — ``is_correlated`` + the ``summary()`` message for
  both the correlated and the lone-failure cases.
* :func:`make_correlated_failure_emitter` — the COMMON Telegram-alert wiring (a
  silent no-op when Telegram is disabled, the default).

Test types present (CLAUDE.md §12 / Golden Rule #12):
* unit — every branch of the correlator + verdict + emitter.
* property — random interleavings of failure events always attribute the
  highest-priority kind as the root and never include the root in the symptoms.
* scenario — the two key co-occurrence scenarios (disk-fill + rate-storm, OOM +
  reauth) and the single-failure-passes-through-unchanged case.
* integration — a correlated verdict routes through a REAL
  :class:`~brutex.notifications.sweep_alerts.SweepAlerter` + fake notifier.
"""

from __future__ import annotations

import pytest
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.notifications.telegram import TelegramEvent
from brutex.observability.failure_correlation import (
    DEFAULT_PRIORITY,
    DEFAULT_WINDOW_SECONDS,
    CorrelatedVerdict,
    FailureCorrelator,
    FailureKind,
    FailureSignal,
    make_correlated_failure_emitter,
)

# ────────────────────────────────────────────────────────────────────────
# Fakes / helpers
# ────────────────────────────────────────────────────────────────────────


class Recorder:
    """Captures every correlated verdict; can simulate a raising callback."""

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.verdicts: list[CorrelatedVerdict] = []
        self.raise_on_call = raise_on_call

    def __call__(self, verdict: CorrelatedVerdict) -> None:
        if self.raise_on_call:
            raise RuntimeError("simulated correlated callback failure")
        self.verdicts.append(verdict)


class FakeClock:
    """A monotonic clock whose value the test advances explicitly."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _correlator(
    recorder: Recorder | None = None,
    *,
    clock: FakeClock | None = None,
    **kwargs: object,
) -> tuple[FailureCorrelator, Recorder, FakeClock]:
    """Build a correlator on a fake clock with a recording callback."""
    cb = recorder if recorder is not None else Recorder()
    clk = clock if clock is not None else FakeClock()
    correlator = FailureCorrelator(
        on_correlated=cb,
        clock=clk,
        **kwargs,  # type: ignore[arg-type]
    )
    return correlator, cb, clk


# ────────────────────────────────────────────────────────────────────────
# Construction validation
# ────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_rejects_non_positive_window(self) -> None:
        with pytest.raises(ValueError, match="window_seconds must be > 0"):
            FailureCorrelator(on_correlated=Recorder(), window_seconds=0.0)

    def test_rejects_zero_max_signals(self) -> None:
        with pytest.raises(ValueError, match="max_window_signals must be >= 1"):
            FailureCorrelator(on_correlated=Recorder(), max_window_signals=0)

    def test_rejects_priority_missing_a_kind(self) -> None:
        partial = {k: 1 for k in FailureKind if k is not FailureKind.AUTH}
        with pytest.raises(ValueError, match="priority is missing kinds"):
            FailureCorrelator(on_correlated=Recorder(), priority=partial)

    def test_accepts_full_custom_priority(self) -> None:
        custom = {k: i for i, k in enumerate(FailureKind)}
        correlator, _cb, _clk = _correlator(priority=custom)
        # custom priority makes AUTH (last in enum) the highest — proves it is used.
        correlator.observe(FailureKind.DISK_FULL)
        verdict = correlator.observe(FailureKind.AUTH)
        assert verdict is not None
        assert verdict.root is FailureKind.AUTH
        assert verdict.symptoms == (FailureKind.DISK_FULL,)

    def test_default_window_constant_used_when_unspecified(self) -> None:
        clk = FakeClock()
        correlator = FailureCorrelator(on_correlated=Recorder(), clock=clk)
        verdict = correlator.observe(FailureKind.TIMEOUT)
        assert verdict is not None
        assert verdict.window_seconds == DEFAULT_WINDOW_SECONDS


# ────────────────────────────────────────────────────────────────────────
# Single-failure pass-through
# ────────────────────────────────────────────────────────────────────────


class TestSingleFailurePassThrough:
    def test_lone_failure_is_not_correlated_and_never_alerts(self) -> None:
        correlator, cb, _clk = _correlator()
        verdict = correlator.observe(FailureKind.RATE_LIMIT, detail="429 burst")
        assert verdict is not None
        assert verdict.is_correlated is False
        assert verdict.symptoms == ()
        assert verdict.root is FailureKind.RATE_LIMIT
        assert verdict.root_detail == "429 burst"
        assert cb.verdicts == []  # no correlated alert for a lone failure

    def test_lone_failure_summary_with_detail(self) -> None:
        verdict = CorrelatedVerdict(
            root=FailureKind.TIMEOUT,
            root_detail="read timed out",
            symptoms=(),
            window_seconds=30.0,
        )
        assert verdict.summary() == "single failure: timeout (read timed out)"

    def test_lone_failure_summary_without_detail(self) -> None:
        verdict = CorrelatedVerdict(
            root=FailureKind.TIMEOUT,
            root_detail="",
            symptoms=(),
            window_seconds=30.0,
        )
        assert verdict.summary() == "single failure: timeout"


# ────────────────────────────────────────────────────────────────────────
# Co-occurrence scenarios
# ────────────────────────────────────────────────────────────────────────


class TestCoOccurrenceScenarios:
    def test_disk_fill_during_rate_storm_roots_on_disk(self) -> None:
        """A disk-fill ENOSPC during a vendor-429 storm → disk_full is the root."""
        correlator, cb, _clk = _correlator()
        correlator.observe(FailureKind.RATE_LIMIT, detail="429 ×30")
        verdict = correlator.observe(FailureKind.DISK_FULL, detail="ENOSPC on /lake")
        assert verdict is not None
        assert verdict.is_correlated is True
        assert verdict.root is FailureKind.DISK_FULL
        assert verdict.root_detail == "ENOSPC on /lake"
        assert verdict.symptoms == (FailureKind.RATE_LIMIT,)
        assert len(cb.verdicts) == 1
        assert cb.verdicts[0].root is FailureKind.DISK_FULL

    def test_oom_during_reauth_roots_on_oom(self) -> None:
        """An OOM during a token reauth (auth) → out_of_memory is the root."""
        correlator, cb, _clk = _correlator()
        correlator.observe(FailureKind.AUTH, detail="token expired, re-minting")
        verdict = correlator.observe(FailureKind.OUT_OF_MEMORY, detail="MemoryError")
        assert verdict is not None
        assert verdict.root is FailureKind.OUT_OF_MEMORY
        assert verdict.symptoms == (FailureKind.AUTH,)
        assert len(cb.verdicts) == 1

    def test_three_way_symptoms_sorted_highest_priority_first(self) -> None:
        correlator, _cb, _clk = _correlator()
        correlator.observe(FailureKind.EMPTY_RESPONSE)
        correlator.observe(FailureKind.TIMEOUT)
        correlator.observe(FailureKind.RATE_LIMIT)
        verdict = correlator.observe(FailureKind.QUOTA, detail="daily quota hit")
        assert verdict is not None
        assert verdict.root is FailureKind.QUOTA
        # symptoms ordered by descending priority: rate_limit > timeout > empty.
        assert verdict.symptoms == (
            FailureKind.RATE_LIMIT,
            FailureKind.TIMEOUT,
            FailureKind.EMPTY_RESPONSE,
        )

    def test_correlated_summary_message(self) -> None:
        verdict = CorrelatedVerdict(
            root=FailureKind.DISK_FULL,
            root_detail="ENOSPC",
            symptoms=(FailureKind.RATE_LIMIT, FailureKind.TIMEOUT),
            window_seconds=30.0,
        )
        msg = verdict.summary()
        assert "disk_full is the root" in msg
        assert "rate_limit, timeout" in msg
        assert "within 30s" in msg
        assert msg.endswith("— ENOSPC")

    def test_correlated_summary_without_root_detail(self) -> None:
        verdict = CorrelatedVerdict(
            root=FailureKind.DISK_FULL,
            root_detail="",
            symptoms=(FailureKind.RATE_LIMIT,),
            window_seconds=30.0,
        )
        msg = verdict.summary()
        assert "disk_full is the root" in msg
        assert "—" not in msg  # no trailing detail clause


# ────────────────────────────────────────────────────────────────────────
# Window eviction
# ────────────────────────────────────────────────────────────────────────


class TestEviction:
    def test_stale_signal_evicted_so_no_longer_co_occurs(self) -> None:
        correlator, cb, clk = _correlator(window_seconds=10.0)
        correlator.observe(FailureKind.RATE_LIMIT)
        clk.advance(20.0)  # past the 10 s window
        verdict = correlator.observe(FailureKind.TIMEOUT)
        assert verdict is not None
        # rate_limit fell out of the window → lone timeout, not a correlation.
        assert verdict.is_correlated is False
        assert verdict.root is FailureKind.TIMEOUT
        assert cb.verdicts == []

    def test_signal_exactly_at_window_edge_is_evicted(self) -> None:
        # at < cutoff drops it; a signal exactly window_seconds old (at == cutoff)
        # is KEPT (the boundary is strict-less-than).
        correlator, _cb, clk = _correlator(window_seconds=10.0)
        correlator.observe(FailureKind.RATE_LIMIT)
        clk.advance(10.0)  # now - window == the old signal's at → kept
        verdict = correlator.observe(FailureKind.DISK_FULL)
        assert verdict is not None
        assert verdict.is_correlated is True
        assert verdict.symptoms == (FailureKind.RATE_LIMIT,)

    def test_size_cap_trims_oldest_beyond_max(self) -> None:
        # max_window_signals=1 → only the newest survives, so two DISTINCT kinds
        # never co-occur (the oldest is trimmed before the verdict is built).
        correlator, cb, _clk = _correlator(max_window_signals=1)
        correlator.observe(FailureKind.RATE_LIMIT)
        verdict = correlator.observe(FailureKind.DISK_FULL)
        assert verdict is not None
        assert verdict.is_correlated is False
        assert verdict.root is FailureKind.DISK_FULL
        assert cb.verdicts == []


# ────────────────────────────────────────────────────────────────────────
# Dedupe + tie-break + detail freshness
# ────────────────────────────────────────────────────────────────────────


class TestDedupeAndTieBreak:
    def test_same_shape_fires_once(self) -> None:
        correlator, cb, _clk = _correlator()
        correlator.observe(FailureKind.RATE_LIMIT)
        correlator.observe(FailureKind.DISK_FULL)  # shape (disk, [rate]) → fire 1
        correlator.observe(FailureKind.RATE_LIMIT)  # same shape → no re-fire
        correlator.observe(FailureKind.DISK_FULL)
        assert len(cb.verdicts) == 1

    def test_new_symptom_changes_shape_and_refires(self) -> None:
        correlator, cb, _clk = _correlator()
        correlator.observe(FailureKind.RATE_LIMIT)
        correlator.observe(FailureKind.DISK_FULL)  # shape (disk, [rate]) → fire 1
        correlator.observe(FailureKind.TIMEOUT)  # shape (disk, [rate, timeout]) → 2
        assert len(cb.verdicts) == 2
        assert cb.verdicts[1].symptoms == (FailureKind.RATE_LIMIT, FailureKind.TIMEOUT)

    def test_lone_failure_clears_shape_so_next_co_occurrence_refires(self) -> None:
        correlator, cb, clk = _correlator(window_seconds=10.0)
        correlator.observe(FailureKind.RATE_LIMIT)
        correlator.observe(FailureKind.DISK_FULL)  # fire 1
        clk.advance(20.0)  # everything evicted
        correlator.observe(FailureKind.RATE_LIMIT)  # lone → clears last_shape
        clk.advance(1.0)
        correlator.observe(FailureKind.DISK_FULL)  # same shape but re-fires (cleared)
        assert len(cb.verdicts) == 2

    def test_priority_tie_broken_by_most_recent(self) -> None:
        # disk_full and out_of_memory share priority 100; the most-recent wins root.
        correlator, _cb, clk = _correlator()
        correlator.observe(FailureKind.DISK_FULL, detail="first")
        clk.advance(1.0)
        verdict = correlator.observe(FailureKind.OUT_OF_MEMORY, detail="second")
        assert verdict is not None
        assert verdict.root is FailureKind.OUT_OF_MEMORY
        assert verdict.root_detail == "second"
        assert verdict.symptoms == (FailureKind.DISK_FULL,)

    def test_root_detail_is_most_recent_of_root_kind(self) -> None:
        correlator, _cb, clk = _correlator()
        correlator.observe(FailureKind.DISK_FULL, detail="old")
        correlator.observe(FailureKind.RATE_LIMIT)
        clk.advance(1.0)
        verdict = correlator.observe(FailureKind.DISK_FULL, detail="fresh")
        assert verdict is not None
        assert verdict.root_detail == "fresh"


# ────────────────────────────────────────────────────────────────────────
# Never raises into the pull
# ────────────────────────────────────────────────────────────────────────


class TestNeverRaises:
    def test_callback_raise_is_swallowed(self) -> None:
        correlator, _cb, _clk = _correlator(Recorder(raise_on_call=True))
        correlator.observe(FailureKind.RATE_LIMIT)
        # the callback raises on the correlated edge — must be swallowed, no crash.
        verdict = correlator.observe(FailureKind.DISK_FULL)
        assert verdict is not None
        assert verdict.is_correlated is True  # verdict still returned

    def test_internal_error_returns_none(self) -> None:
        # a clock that raises makes the body raise internally → swallowed → None.
        def _bad_clock() -> float:
            raise RuntimeError("clock blew up")

        correlator = FailureCorrelator(on_correlated=Recorder(), clock=_bad_clock)
        assert correlator.observe(FailureKind.RATE_LIMIT) is None


# ────────────────────────────────────────────────────────────────────────
# Property: random interleavings → correct root attribution
# ────────────────────────────────────────────────────────────────────────


class TestRootAttributionProperty:
    @pytest.mark.parametrize("seed", range(40))
    def test_root_is_highest_priority_present_and_not_in_symptoms(self, seed: int) -> None:
        import random

        rng = random.Random(seed)
        kinds = list(FailureKind)
        # a wide window + huge cap so nothing is evicted — purely tests attribution.
        correlator, _cb, _clk = _correlator(window_seconds=10_000.0, max_window_signals=10_000)
        n = rng.randint(1, 12)
        chosen = [rng.choice(kinds) for _ in range(n)]
        verdict: CorrelatedVerdict | None = None
        for k in chosen:
            verdict = correlator.observe(k)
        assert verdict is not None
        present = set(chosen)
        expected_root_priority = max(DEFAULT_PRIORITY[k] for k in present)
        # the root must be one of the present kinds with the max priority.
        assert DEFAULT_PRIORITY[verdict.root] == expected_root_priority
        assert verdict.root in present
        # the root never appears in its own symptoms.
        assert verdict.root not in verdict.symptoms
        # symptoms = every OTHER distinct present kind, sorted desc by priority.
        assert set(verdict.symptoms) == present - {verdict.root}
        priorities = [DEFAULT_PRIORITY[k] for k in verdict.symptoms]
        assert priorities == sorted(priorities, reverse=True)
        # is_correlated iff more than one distinct kind co-occurred.
        assert verdict.is_correlated == (len(present) > 1)


# ────────────────────────────────────────────────────────────────────────
# FailureSignal record
# ────────────────────────────────────────────────────────────────────────


class TestFailureSignal:
    def test_signal_fields_and_default_detail(self) -> None:
        sig = FailureSignal(kind=FailureKind.TIMEOUT, at=12.5)
        assert sig.kind is FailureKind.TIMEOUT
        assert sig.at == 12.5
        assert sig.detail == ""


# ────────────────────────────────────────────────────────────────────────
# Telegram-alert wiring (COMMON emitter)
# ────────────────────────────────────────────────────────────────────────


class _FakeNotifier:
    """Captures published events (a live, enabled notifier surface)."""

    def __init__(self) -> None:
        self.events: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        self.events.append(event)


class TestCorrelatedFailureEmitter:
    def test_emitter_disabled_telegram_is_silent_no_op(self) -> None:
        # the default SweepAlerter resolver returns None when Telegram is disabled
        # (the default), so the emitter publishes nothing and never raises.
        emit = make_correlated_failure_emitter()
        verdict = CorrelatedVerdict(
            root=FailureKind.DISK_FULL,
            root_detail="ENOSPC",
            symptoms=(FailureKind.RATE_LIMIT,),
            window_seconds=30.0,
        )
        emit(verdict)  # must not raise

    def test_correlated_verdict_routes_to_alerter(self) -> None:
        """A correlated verdict fires exactly one CRITICAL PHASE_BLOCKED alert."""
        notifier = _FakeNotifier()
        alerter = SweepAlerter(notifier=notifier)

        def _on_correlated(verdict: CorrelatedVerdict) -> None:
            alerter.correlated_failure(
                root=verdict.root.value,
                symptoms=tuple(k.value for k in verdict.symptoms),
                summary=verdict.summary(),
            )

        correlator = FailureCorrelator(on_correlated=_on_correlated, clock=FakeClock())
        correlator.observe(FailureKind.RATE_LIMIT, detail="429 ×30")
        correlator.observe(FailureKind.DISK_FULL, detail="ENOSPC")
        assert len(notifier.events) == 1
        event = notifier.events[0]
        assert event.severity == "critical"
        assert event.kind == "phase.blocked"
        # the body is MarkdownV2-escaped (underscores → \_), so assert on the
        # un-escaped dedupe key and the escaped body fragments.
        assert r"disk\_full" in event.message
        assert r"rate\_limit" in event.message
        assert event.dedupe_key == "correlated:disk_full:rate_limit"

    def test_correlated_failure_alert_with_multiple_symptoms(self) -> None:
        notifier = _FakeNotifier()
        alerter = SweepAlerter(notifier=notifier)
        sent = alerter.correlated_failure(
            root="out_of_memory",
            symptoms=("rate_limit", "timeout"),
            summary="out_of_memory is the root; downstream symptoms: rate_limit, timeout",
        )
        assert sent is True
        assert notifier.events[0].dedupe_key == "correlated:out_of_memory:rate_limit|timeout"
