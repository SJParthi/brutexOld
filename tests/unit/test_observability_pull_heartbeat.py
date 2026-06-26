"""Tests for :mod:`brutex.observability.pull_heartbeat` (DEC-PULL-WORKER-HEARTBEAT-001).

100% line + branch coverage of the per-worker heartbeat + live pull-progress signal:

* :meth:`PullHeartbeat.beat` — O(1) stamp + counter bump, first-vs-repeat slot, the
  non-positive ``units`` clamp, the latch-clear on re-beat, and the best-effort swallow.
* :meth:`PullHeartbeat.scan` — stale-flag detection, edge-triggered ``on_stuck`` (fires
  ONCE per continuous stuck spell), the recovery latch-clear, the structlog WARNING, the
  ``on_stuck``-raises swallow, the no-callback branch, and the best-effort scan swallow.
* :meth:`PullHeartbeat.progress` — the aggregate snapshot (done / active / stuck /
  oldest age), the empty-registry ``None`` oldest, the declared-vs-undeclared total, and
  the best-effort swallow returning a zeroed snapshot.
* gauge mirroring into a real :class:`~brutex.observability.metrics.MetricRegistry`.
* :func:`make_heartbeat_window_tick` — the wrap beats then delegates; a ``None``
  heartbeat returns the inner unchanged; the injected ``worker_id``.

Test types present (CLAUDE.md §12 / Golden Rule #12):
* unit — every branch above.
* property (hypothesis) — N concurrent threads stamping heartbeats never lose a beat
  (the per-worker counter sums to the issued count, no race / no corruption).
* stuck-detection — a wedged worker (no beat past the threshold) is flagged exactly
  once, and a recovered worker can flag again on a fresh stall.
* integration — a stuck worker → one ``pull_degraded`` push through a real
  :class:`~brutex.notifications.sweep_alerts.SweepAlerter` + fake notifier.
"""

from __future__ import annotations

import threading

import pytest
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.notifications.telegram import TelegramEvent
from brutex.observability.metrics import MetricRegistry
from brutex.observability.pull_heartbeat import (
    DEFAULT_STALE_AFTER_S,
    PullHeartbeat,
    PullProgress,
    WorkerBeat,
    make_heartbeat_window_tick,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────────
# Fakes / helpers
# ────────────────────────────────────────────────────────────────────────


class _Clock:
    """A manually-advanced monotonic clock for deterministic age tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, by: float) -> None:
        self.now += by


class RecordingStuck:
    """Captures every ``on_stuck`` call; can simulate a raise."""

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.calls: list[tuple[str, float, int]] = []
        self.raise_on_call = raise_on_call

    def __call__(self, worker_id: str, age_s: float, units: int) -> None:
        if self.raise_on_call:
            raise RuntimeError("simulated on_stuck failure")
        self.calls.append((worker_id, age_s, units))


def _hb(
    *,
    on_stuck: RecordingStuck | None = None,
    units_total: int | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    clock: _Clock | None = None,
) -> tuple[PullHeartbeat, RecordingStuck | None, MetricRegistry, _Clock]:
    """Build a heartbeat on a fresh registry with an optional recording callback."""
    registry = MetricRegistry()
    clk = clock if clock is not None else _Clock()
    hb = PullHeartbeat(
        registry=registry,
        units_total=units_total,
        on_stuck=on_stuck,
        stale_after_s=stale_after_s,
        clock=clk,
    )
    return hb, on_stuck, registry, clk


# ────────────────────────────────────────────────────────────────────────
# beat
# ────────────────────────────────────────────────────────────────────────


def test_beat_creates_then_increments_slot() -> None:
    hb, _, _, clk = _hb()
    hb.beat("w1")
    clk.advance(1.0)
    hb.beat("w1", units=2)
    beats = hb.scan()
    assert len(beats) == 1
    assert isinstance(beats[0], WorkerBeat)
    assert beats[0].worker_id == "w1"
    assert beats[0].units == 3  # 1 + 2
    assert beats[0].last_beat == clk.now  # stamped at the latest beat


def test_beat_non_positive_units_is_liveness_only() -> None:
    hb, _, _, clk = _hb()
    hb.beat("w1", units=5)
    clk.advance(1.0)
    hb.beat("w1", units=0)  # liveness beat — counter must not move
    clk.advance(1.0)
    hb.beat("w1", units=-3)  # negative clamped to 0 — counter never goes backwards
    beats = hb.scan()
    assert beats[0].units == 5
    assert beats[0].last_beat == clk.now  # timestamp still advanced


def test_beat_swallows_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clock that raises must NOT propagate out of beat (best-effort)."""

    def _boom() -> float:
        raise RuntimeError("clock exploded")

    registry = MetricRegistry()
    hb = PullHeartbeat(registry=registry, clock=_boom)
    hb.beat("w1")  # must not raise
    # No slot was recorded (the body raised before the dict write).
    assert hb.progress().workers_active == 0


# ────────────────────────────────────────────────────────────────────────
# scan — stuck detection + hysteresis
# ────────────────────────────────────────────────────────────────────────


def test_scan_flags_stuck_worker_once() -> None:
    cb = RecordingStuck()
    hb, _, _, clk = _hb(on_stuck=cb, stale_after_s=100.0)
    hb.beat("w1")
    clk.advance(50.0)
    assert hb.scan()[0].stuck is False  # 50s < 100s threshold → not yet stuck
    assert cb.calls == []
    clk.advance(60.0)  # now 110s since the beat
    beats = hb.scan()
    assert beats[0].stuck is True
    assert cb.calls == [("w1", 110.0, 1)]  # fired ONCE
    # A second scan at the same staleness must NOT re-fire (edge-triggered latch).
    hb.scan()
    assert cb.calls == [("w1", 110.0, 1)]


def test_scan_recovered_worker_can_flag_again() -> None:
    cb = RecordingStuck()
    hb, _, _, clk = _hb(on_stuck=cb, stale_after_s=100.0)
    hb.beat("w1")
    clk.advance(110.0)
    hb.scan()
    assert len(cb.calls) == 1
    # Worker recovers (a fresh beat clears the latch)...
    hb.beat("w1")
    fresh = hb.scan()
    assert fresh[0].stuck is False
    # ...then wedges again → a SECOND flag.
    clk.advance(110.0)
    hb.scan()
    assert len(cb.calls) == 2


def test_scan_clears_latch_when_not_stuck_without_rebeat() -> None:
    """The ``elif not is_stuck`` latch-clear branch: a worker flagged stuck whose age
    later drops below the threshold (a clock skew / a long but recovering wait) clears
    its latch via scan itself, so a subsequent stall can flag again."""
    cb = RecordingStuck()
    clk = _Clock()
    hb, _, _, _ = _hb(on_stuck=cb, stale_after_s=100.0, clock=clk)
    hb.beat("w1")
    clk.advance(110.0)
    hb.scan()  # flagged stuck (latched)
    assert len(cb.calls) == 1
    clk.now = 5.0  # simulate the beat being "recent" again (skew) → age 5s < 100s
    after = hb.scan()
    assert after[0].stuck is False  # latch cleared via the not-stuck branch
    clk.now = 200.0  # age 195s > 100s again
    hb.scan()
    assert len(cb.calls) == 2  # re-flagged after the latch was cleared by scan


def test_scan_no_callback_only_logs(caplog: pytest.LogCaptureFixture) -> None:
    hb, _, _, clk = _hb(on_stuck=None, stale_after_s=10.0)
    hb.beat("w1")
    clk.advance(20.0)
    import logging

    with caplog.at_level(logging.WARNING, logger="brutex.observability.pull_heartbeat"):
        beats = hb.scan()
    assert beats[0].stuck is True
    assert any("worker_stuck" in r.message for r in caplog.records)


def test_scan_swallows_on_stuck_raise() -> None:
    cb = RecordingStuck(raise_on_call=True)
    hb, _, _, clk = _hb(on_stuck=cb, stale_after_s=10.0)
    hb.beat("w1")
    clk.advance(20.0)
    beats = hb.scan()  # must not raise despite the callback raising
    assert beats[0].stuck is True


def test_scan_swallows_internal_error() -> None:
    """A scan whose clock raises returns () and never propagates."""

    def _boom() -> float:
        raise RuntimeError("clock exploded")

    hb = PullHeartbeat(registry=MetricRegistry(), clock=_boom)
    assert hb.scan() == ()


def test_scan_sorts_newest_age_first() -> None:
    clk = _Clock()
    hb, _, _, _ = _hb(clock=clk)
    hb.beat("recent")
    clk.advance(5.0)
    hb.beat("older_target")  # beaten 5s before "recent"... wait, it's newer
    # Re-stamp to make "recent" the newest: beat it last.
    clk.advance(5.0)
    hb.beat("recent")
    beats = hb.scan()
    # "older_target" has the larger age → sorts first.
    assert beats[0].worker_id == "older_target"
    assert beats[1].worker_id == "recent"
    assert beats[0].age_s >= beats[1].age_s


def test_scan_empty_registry() -> None:
    hb, _, _, _ = _hb()
    assert hb.scan() == ()


# ────────────────────────────────────────────────────────────────────────
# progress
# ────────────────────────────────────────────────────────────────────────


def test_progress_aggregate() -> None:
    clk = _Clock()
    hb, _, _, _ = _hb(units_total=100, stale_after_s=50.0, clock=clk)
    hb.beat("w1", units=3)
    hb.beat("w2", units=7)
    clk.advance(60.0)  # both now stale (60 > 50) — and w3 fresh below
    hb.beat("w3", units=2)
    prog = hb.progress()
    assert isinstance(prog, PullProgress)
    assert prog.units_done == 12  # 3 + 7 + 2
    assert prog.units_total == 100
    assert prog.workers_active == 3
    assert prog.workers_stuck == 2  # w1 + w2 (w3 fresh)
    assert prog.oldest_beat_age_s == 60.0  # w1/w2 aged 60s


def test_progress_empty_registry_oldest_none() -> None:
    hb, _, _, _ = _hb()
    prog = hb.progress()
    assert prog.units_done == 0
    assert prog.workers_active == 0
    assert prog.workers_stuck == 0
    assert prog.oldest_beat_age_s is None
    assert prog.units_total is None  # undeclared


def test_progress_swallows_internal_error() -> None:
    """A progress whose clock raises returns a zeroed snapshot (best-effort)."""

    def _boom() -> float:
        raise RuntimeError("clock exploded")

    hb = PullHeartbeat(registry=MetricRegistry(), units_total=42, clock=_boom)
    prog = hb.progress()
    assert prog == PullProgress(
        units_done=0,
        units_total=42,  # the declared total is still surfaced
        workers_active=0,
        workers_stuck=0,
        oldest_beat_age_s=None,
    )


# ────────────────────────────────────────────────────────────────────────
# gauge mirroring
# ────────────────────────────────────────────────────────────────────────


def test_gauges_mirrored_on_scan() -> None:
    clk = _Clock()
    hb, _, registry, _ = _hb(units_total=10, stale_after_s=50.0, clock=clk)
    hb.beat("w1", units=4)
    clk.advance(60.0)  # w1 now stale
    hb.scan()
    assert registry.gauge_value("brutex_pull_units_done") == 4
    assert registry.gauge_value("brutex_pull_units_total") == 10
    assert registry.gauge_value("brutex_pull_workers_active") == 1
    assert registry.gauge_value("brutex_pull_workers_stuck") == 1
    assert registry.gauge_value("brutex_pull_oldest_beat_age_seconds") == 60.0


def test_gauges_units_total_zero_when_undeclared() -> None:
    hb, _, registry, _ = _hb(units_total=None)
    hb.beat("w1")
    hb.progress()
    # Undeclared total mirrors as 0.0 (a gauge cannot be None).
    assert registry.gauge_value("brutex_pull_units_total") == 0.0


def test_gauges_oldest_zero_on_empty() -> None:
    hb, _, registry, _ = _hb()
    hb.progress()  # no workers → oldest mirrors 0.0
    assert registry.gauge_value("brutex_pull_oldest_beat_age_seconds") == 0.0


# ────────────────────────────────────────────────────────────────────────
# make_heartbeat_window_tick
# ────────────────────────────────────────────────────────────────────────


def test_window_tick_beats_then_delegates() -> None:
    hb, _, _, _ = _hb()
    inner_calls: list[int] = []
    tick = make_heartbeat_window_tick(hb, lambda: inner_calls.append(1), worker_id=lambda: "wX")
    tick()
    tick()
    assert inner_calls == [1, 1]  # inner ran each time
    beats = hb.scan()
    assert beats[0].worker_id == "wX"
    assert beats[0].units == 2  # one beat per tick


def test_window_tick_none_heartbeat_returns_inner() -> None:
    inner = lambda: None  # noqa: E731 — sentinel identity check
    assert make_heartbeat_window_tick(None, inner) is inner


def test_window_tick_default_worker_id_is_thread_name() -> None:
    hb, _, _, _ = _hb()
    tick = make_heartbeat_window_tick(hb, lambda: None)
    tick()  # default worker_id = current thread name
    beats = hb.scan()
    assert beats[0].worker_id == threading.current_thread().name


# ────────────────────────────────────────────────────────────────────────
# property — concurrency: many threads stamping heartbeats lose no beat
# ────────────────────────────────────────────────────────────────────────


@settings(max_examples=25, deadline=None)
@given(
    n_workers=st.integers(min_value=2, max_value=8),
    beats_each=st.integers(min_value=1, max_value=40),
)
def test_concurrent_beats_never_lose_a_count(n_workers: int, beats_each: int) -> None:
    """N threads each beat ``beats_each`` times on its OWN worker id concurrently; the
    per-worker counters must sum to exactly ``n_workers * beats_each`` (no lost update,
    no corruption) and every worker must be registered."""
    hb = PullHeartbeat(registry=MetricRegistry())
    barrier = threading.Barrier(n_workers)

    def _run(idx: int) -> None:
        barrier.wait()  # maximise contention — all start together
        for _ in range(beats_each):
            hb.beat(f"w{idx}")

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    prog = hb.progress()
    assert prog.workers_active == n_workers
    assert prog.units_done == n_workers * beats_each


@settings(max_examples=25, deadline=None)
@given(n_workers=st.integers(min_value=2, max_value=8))
def test_concurrent_same_worker_beats_serialize(n_workers: int) -> None:
    """N threads ALL beating the SAME worker id concurrently sum correctly (the lock
    serialises the shared counter — no torn read-modify-write)."""
    hb = PullHeartbeat(registry=MetricRegistry())
    per_thread = 30
    barrier = threading.Barrier(n_workers)

    def _run() -> None:
        barrier.wait()
        for _ in range(per_thread):
            hb.beat("shared")

    threads = [threading.Thread(target=_run) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    beats = hb.scan()
    assert len(beats) == 1
    assert beats[0].units == n_workers * per_thread


# ────────────────────────────────────────────────────────────────────────
# integration — stuck worker → one pull_degraded alert through a real alerter
# ────────────────────────────────────────────────────────────────────────


class _FakeNotifier:
    """Captures published Telegram events (mirrors the pull_health integration test)."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        self.published.append(event)


def test_stuck_worker_routes_to_pull_degraded_alert() -> None:
    notifier = _FakeNotifier()
    alerter = SweepAlerter(notifier=notifier)

    def _on_stuck(worker_id: str, age_s: float, units: int) -> None:
        alerter.pull_degraded(
            rule="pull.worker_stuck",
            achieved=float(units),
            allowed=age_s,
            detail=f"worker {worker_id} wedged for {age_s:.0f}s",
        )

    clk = _Clock()
    hb = PullHeartbeat(
        registry=MetricRegistry(),
        on_stuck=_on_stuck,
        stale_after_s=10.0,
        clock=clk,
    )
    hb.beat("NSE-NIFTY 1minute")
    clk.advance(20.0)
    hb.scan()
    assert len(notifier.published) == 1
    assert "wedged" in notifier.published[0].message
    # A second scan at the same staleness coalesces (edge-triggered) — no dup push.
    hb.scan()
    assert len(notifier.published) == 1
