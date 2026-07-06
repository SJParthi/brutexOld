"""Tests for `brutex.dashboard.pull_progress` — the live pull-progress tracker.

100% line+branch: the state machine (start → advance → finish/fail), the O(1)
counters (completed clamp, bars accumulation, failed tally, current label), the
``percent`` zero-total + normal branches, the snapshot/`to_dict` projection, the
reusable re-start reset, the default monotonic clock, and thread-safety under
concurrent advances.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from brutex.dashboard.pull_progress import (
    STATE_DONE,
    STATE_ERROR,
    STATE_IDLE,
    STATE_RUNNING,
    ProgressSnapshot,
    PullProgress,
)


def _ticking_clock() -> Callable[[], float]:
    """A deterministic, monotonically-increasing injected clock.

    A plain ``def`` closure over a mutable holder — NOT a callable class — so
    CodeQL's cross-function points-to analysis can prove the production
    ``clock()`` call sites callable (the callable-class fixture pattern
    false-positives ``py/call-to-non-callable`` on production lines; see the
    team playbook).
    """
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    return clock


def test_initial_state_is_idle() -> None:
    p = PullProgress(clock=_ticking_clock())
    snap = p.snapshot()
    assert snap.state == STATE_IDLE
    assert snap.total == 0
    assert snap.completed == 0
    assert snap.percent == 0.0  # zero-total branch
    assert snap.running is False


def test_start_sets_running_and_total() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=4, detail="kick")
    snap = p.snapshot()
    assert snap.state == STATE_RUNNING
    assert snap.total == 4
    assert snap.detail == "kick"
    assert snap.running is True
    assert snap.percent == 0.0  # nothing completed yet (total>0 branch)


def test_negative_total_clamped_to_zero() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=-5)
    assert p.snapshot().total == 0


def test_advance_counts_and_percent() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=4)
    p.advance(current="NIFTY", bars=100)
    p.advance(current="BANKNIFTY", bars=50, failed=True)
    snap = p.snapshot()
    assert snap.completed == 2
    assert snap.bars == 150
    assert snap.failed == 1
    assert snap.current == "BANKNIFTY"
    assert snap.percent == 50.0  # 2/4


def test_advance_without_current_keeps_prior_label() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=2)
    p.advance(current="NIFTY", bars=10)
    p.advance(bars=5)  # no current → label unchanged (the `if current` False branch)
    snap = p.snapshot()
    assert snap.current == "NIFTY"
    assert snap.bars == 15
    assert snap.failed == 0  # the `if failed` False branch


def test_negative_bars_ignored() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1)
    p.advance(bars=-99)
    assert p.snapshot().bars == 0


def test_completed_clamps_to_total() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1)
    p.advance()
    p.advance()  # second advance would exceed total → clamped
    snap = p.snapshot()
    assert snap.completed == 1
    assert snap.percent == 100.0


def test_finish_marks_done() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=2)
    p.advance()
    p.finish(detail="all good")
    snap = p.snapshot()
    assert snap.state == STATE_DONE
    assert snap.detail == "all good"
    assert snap.running is False


def test_fail_marks_error() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=2)
    p.fail("DhanError: token expired")
    snap = p.snapshot()
    assert snap.state == STATE_ERROR
    assert snap.detail == "DhanError: token expired"


def test_restart_resets_counters() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=3)
    p.advance(current="X", bars=7, failed=True)
    p.finish()
    # Re-click → a fresh run on the SAME instance resets everything.
    p.start(total=2)
    snap = p.snapshot()
    assert snap.state == STATE_RUNNING
    assert (snap.total, snap.completed, snap.failed, snap.bars) == (2, 0, 0, 0)
    assert snap.current == ""


def test_updated_at_advances_with_clock() -> None:
    clock = _ticking_clock()
    p = PullProgress(clock=clock)
    p.start(total=1)
    first = p.snapshot().updated_at
    p.advance()
    assert p.snapshot().updated_at > first


def test_to_dict_shape() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=2)
    p.advance(current="NIFTY", bars=10)
    d = p.snapshot().to_dict()
    assert d == {
        "state": STATE_RUNNING,
        "total": 2,
        "completed": 1,
        "failed": 0,
        "gaps": 0,
        "bars": 10,
        "current": "NIFTY",
        "detail": "",
        "percent": 50.0,
        "running": True,
        "updated_at": d["updated_at"],
        "phase": "",
        "stalled": False,
        "stall_detail": "",
        "wedged": False,
        "wedge_detail": "",
        "restart_required": False,
        # DEC-PULL-PROGRESS-HONESTY-001 — ADDITIVE ETA / open-ended keys: the FIRST
        # counter mutation only STAMPS the phase rate baseline (no measured rate
        # yet), so units_per_sec is an honest 0.0 and eta_seconds an honest None.
        "started_at": d["started_at"],
        "units_per_sec": 0.0,
        "eta_seconds": None,
        "open_ended": False,
    }
    assert isinstance(d["updated_at"], float)
    assert isinstance(d["started_at"], float)


def test_advance_counts_no_data_gaps_separately_from_failures() -> None:
    # DEC-DHAN-NO-DATA-GAP-COUNT-001: a benign DH-905 gap bumps ``gaps``, NOT
    # ``failed`` — the two counters are independent and a gap is still a processed
    # unit (``completed`` advances). A reset via ``start`` clears ``gaps`` too.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=3)
    p.advance(current="NIFTY", bars=100)  # clean
    p.advance(current="NIFTY NEXT 50", gap=True)  # benign DH-905 boundary
    p.advance(current="BADSYM", failed=True)  # real failure
    snap = p.snapshot()
    assert snap.completed == 3
    assert snap.failed == 1
    assert snap.gaps == 1
    assert snap.to_dict()["gaps"] == 1
    p.start(total=1)  # reset clears the gap counter
    assert p.snapshot().gaps == 0


def test_default_monotonic_clock() -> None:
    # No injected clock → exercises the module-level _monotonic default.
    p = PullProgress()
    p.start(total=1)
    assert p.snapshot().updated_at > 0.0


def test_thread_safe_concurrent_advances() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1000)

    def worker() -> None:
        for _ in range(100):
            p.advance(bars=1)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = p.snapshot()
    # 10 threads × 100 advances = 1000 — no lost updates under the lock.
    assert snap.completed == 1000
    assert snap.bars == 1000


def test_snapshot_is_immutable() -> None:
    snap = PullProgress(clock=_ticking_clock()).snapshot()
    assert isinstance(snap, ProgressSnapshot)
    try:
        snap.total = 9  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover - frozen dataclass must reject assignment
        raise AssertionError("ProgressSnapshot must be frozen")


class TestSync:
    """`sync` — absolute, idempotent WINDOW-level progress (DEC-SPOT-WINDOW-PROGRESS-001)."""

    def test_sets_absolute_counters(self) -> None:
        p = PullProgress(clock=_ticking_clock())
        p.start(total=779)  # initial symbol placeholder
        p.sync(completed=118, total=10906, bars=12054, failed=0, current="NIFTYSMALL 1day")
        snap = p.snapshot()
        assert snap.total == 10906  # re-scaled from symbols to windows
        assert snap.completed == 118
        assert snap.bars == 12054
        assert snap.failed == 0
        assert snap.current == "NIFTYSMALL 1day"
        assert snap.percent == round(118 / 10906 * 100.0, 1)

    def test_is_idempotent_not_cumulative(self) -> None:
        # Unlike advance(), a repeated sync SETS rather than adds — so the per-window live
        # poll can never double-count windows the retry rounds re-touch.
        p = PullProgress(clock=_ticking_clock())
        p.start(total=10)
        p.sync(completed=5, total=10, bars=50)
        p.sync(completed=5, total=10, bars=50)
        assert p.snapshot().completed == 5
        assert p.snapshot().bars == 50

    def test_clamps_completed_to_total_and_non_negative(self) -> None:
        p = PullProgress(clock=_ticking_clock())
        p.start(total=10)
        p.sync(completed=999, total=10, bars=-3, failed=-1)
        snap = p.snapshot()
        assert snap.completed == 10  # clamped to total
        assert snap.bars == 0  # clamped non-negative
        assert snap.failed == 0

    def test_empty_current_keeps_prior_label(self) -> None:
        p = PullProgress(clock=_ticking_clock())
        p.start(total=10)
        p.sync(completed=1, total=10, bars=1, current="NIFTY 1day")
        p.sync(completed=2, total=10, bars=2, current="")  # no live label this tick
        assert p.snapshot().current == "NIFTY 1day"


class TestSetCurrent:
    """`set_current` — refresh only the label, no counter change."""

    def test_updates_label_only(self) -> None:
        p = PullProgress(clock=_ticking_clock())
        p.start(total=10)
        p.sync(completed=3, total=10, bars=30)
        p.set_current("BANKNIFTY 1day")
        snap = p.snapshot()
        assert snap.current == "BANKNIFTY 1day"
        assert snap.completed == 3  # untouched
        assert snap.bars == 30  # untouched

    def test_empty_label_is_a_noop(self) -> None:
        clock = _ticking_clock()
        p = PullProgress(clock=clock)
        p.start(total=10)
        before = p.snapshot().updated_at
        p.set_current("")  # no-op — does not even stamp the clock
        assert p.snapshot().updated_at == before


# ── set_phase — the COARSE pipeline phase label (DEC-UNIVERSE-PER-PHASE-PROGRESS-001) ──


def test_phase_defaults_to_empty() -> None:
    # A fresh tracker has no phase — the live per-phase view highlights nothing.
    assert PullProgress(clock=_ticking_clock()).snapshot().phase == ""


def test_set_phase_records_the_running_phase() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=3)
    p.set_phase("spot")
    assert p.snapshot().phase == "spot"
    assert p.snapshot().to_dict()["phase"] == "spot"
    # A later transition overwrites it (spot → metadata → historical).
    p.set_phase("historical")
    assert p.snapshot().phase == "historical"


def test_set_phase_empty_clears_the_label() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1)
    p.set_phase("futures")
    p.set_phase("")  # honoured — back to "no phase in flight"
    assert p.snapshot().phase == ""


def test_set_phase_stamps_the_clock() -> None:
    clock = _ticking_clock()
    p = PullProgress(clock=clock)
    p.start(total=1)
    before = p.snapshot().updated_at
    p.set_phase("options")
    assert p.snapshot().updated_at > before


def test_start_resets_the_phase() -> None:
    # A fresh pull (idempotent re-click) clears any stale phase label.
    p = PullProgress(clock=_ticking_clock())
    p.start(total=2)
    p.set_phase("historical")
    p.start(total=2)  # reset
    assert p.snapshot().phase == ""


def test_set_phase_does_not_touch_counters() -> None:
    # Purely a label — no counter is keyed by it (no fabricated progress).
    p = PullProgress(clock=_ticking_clock())
    p.start(total=4)
    p.advance(current="NIFTY", bars=10)
    p.set_phase("spot")
    snap = p.snapshot()
    assert snap.completed == 1 and snap.bars == 10 and snap.total == 4


# ── DEC-PIPELINE-STALL-HEARTBEAT-001: grow_total / bars=None / stalled marker ──


def test_advance_grow_total_grows_total_with_completed() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=2)
    for _ in range(3):
        p.advance(current="pair", bars=10, grow_total=True)
    snap = p.snapshot()
    assert snap.completed == 3
    assert snap.total == 3  # grew past the starting 2 — open-ended units
    assert snap.bars == 30


def test_advance_grow_total_keeps_a_larger_total() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=10)
    p.advance(grow_total=True)
    snap = p.snapshot()
    assert snap.completed == 1
    assert snap.total == 10  # max(total, completed) — a known-larger total survives


def test_sync_bars_none_preserves_the_cumulative_tally() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=10)
    p.sync(completed=5, total=10, bars=1_000)
    # A bar-less sync source (the metadata pull) must NOT wipe the tally to 0.
    p.sync(completed=8, total=10, bars=None)
    snap = p.snapshot()
    assert snap.bars == 1_000
    assert snap.completed == 8


def test_mark_stalled_sets_marker_while_running_only() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1, detail="pipeline")
    p.mark_stalled("stalled-suspect — no heartbeat for 769s during phase 'prebuild'")
    snap = p.snapshot()
    assert snap.stalled is True
    assert "769" in snap.stall_detail
    assert snap.state == STATE_RUNNING  # never a false FAILED
    assert snap.detail == "pipeline"  # the start detail is untouched


def test_mark_stalled_is_a_noop_on_a_settled_run() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1)
    p.finish(detail="done")
    p.mark_stalled("late timer edge")  # must NOT stamp a settled run
    snap = p.snapshot()
    assert snap.stalled is False
    assert snap.stall_detail == ""


def test_clear_stalled_lifts_the_marker() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1)
    p.mark_stalled("suspect")
    p.clear_stalled()
    snap = p.snapshot()
    assert snap.stalled is False and snap.stall_detail == ""
    assert snap.state == STATE_RUNNING


def test_finish_and_fail_clear_the_stalled_marker() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1)
    p.mark_stalled("suspect")
    p.finish(detail="done")
    assert p.snapshot().stalled is False
    p.start(total=1)
    p.mark_stalled("suspect")
    p.fail("boom")
    assert p.snapshot().stalled is False


def test_start_resets_the_stalled_marker() -> None:
    p = PullProgress(clock=_ticking_clock())
    p.start(total=1)
    p.mark_stalled("suspect")
    p.start(total=1)  # a fresh run never inherits the prior run's marker
    snap = p.snapshot()
    assert snap.stalled is False and snap.stall_detail == ""


# ── DEC-PULL-WEDGE-ESCALATE-001 — the terminal WEDGED marker ────────────────


def test_mark_wedged_sets_terminal_marker_while_running_only() -> None:
    p = PullProgress()
    p.start(total=1)
    p.mark_wedged("WEDGED — restart required")
    snap = p.snapshot()
    assert snap.state == STATE_RUNNING  # the worker is NOT killed
    assert snap.wedged is True
    assert snap.wedge_detail == "WEDGED — restart required"
    assert snap.to_dict()["wedged"] is True
    assert snap.to_dict()["restart_required"] is True


def test_mark_wedged_is_a_noop_on_a_settled_run() -> None:
    p = PullProgress()
    p.start(total=1)
    p.finish()
    p.mark_wedged("late timer edge")
    snap = p.snapshot()
    assert snap.wedged is False
    assert snap.to_dict()["restart_required"] is False


def test_clear_stalled_does_not_clear_wedged() -> None:
    """WEDGED is terminal: a stray late heartbeat recovery never withdraws the verdict."""
    p = PullProgress()
    p.start(total=1)
    p.mark_stalled("suspect")
    p.mark_wedged("WEDGED")
    p.clear_stalled()
    snap = p.snapshot()
    assert snap.stalled is False
    assert snap.wedged is True  # the restart-required verdict survives


def test_finish_and_fail_clear_the_wedged_marker() -> None:
    p = PullProgress()
    p.start(total=1)
    p.mark_wedged("WEDGED")
    p.finish()
    assert p.snapshot().wedged is False
    p.start(total=1)
    p.mark_wedged("WEDGED")
    p.fail("boom")
    snap = p.snapshot()
    assert snap.wedged is False and snap.wedge_detail == ""


def test_start_resets_the_wedged_marker() -> None:
    p = PullProgress()
    p.start(total=1)
    p.mark_wedged("WEDGED")
    p.start(total=1)
    snap = p.snapshot()
    assert snap.wedged is False and snap.wedge_detail == ""
