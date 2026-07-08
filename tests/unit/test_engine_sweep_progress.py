"""Unit + property tests for the sweep progress tracker (`brutex.engine.sweep_progress`).

100% line + branch coverage of the O(1) thread-safe live tracker
(DEC-SWEEP-REALTIME-GUARANTEE-001):

* :meth:`SweepProgress.start` (negative-enumerated clamp, reusable reset),
  :meth:`advance` (evaluated clamp, survived / skipped / current branches),
  :meth:`finish`, :meth:`fail`, :meth:`snapshot`;
* every :class:`SweepProgressSnapshot` derived property — ``percent`` (zero /
  nonzero), ``elapsed_seconds`` (zero / positive), ``combos_per_sec``
  (zero-rate / measured), ``eta_seconds`` (None / zero-remaining / measured),
  ``survivor_rate``, ``running``, ``to_dict``;
* the default :func:`time.monotonic` clock path;
* a hypothesis property that ``evaluated`` never exceeds ``enumerated`` and
  ``percent`` stays in ``[0, 100]`` for any advance sequence;
* a thread-safety smoke test (many threads advancing concurrently).
"""

from __future__ import annotations

import threading
from typing import Any

from brutex.engine.sweep_progress import (
    HEAL_FRESH_SECONDS,
    STATE_DONE,
    STATE_ERROR,
    STATE_IDLE,
    STATE_RUNNING,
    SweepProgress,
    progress_running_fresh,
)
from hypothesis import given
from hypothesis import strategies as st


class _Clock:
    """A manually-advanced fake clock."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def test_initial_state_is_idle() -> None:
    sp = SweepProgress(clock=_Clock())
    s = sp.snapshot()
    assert s.state == STATE_IDLE
    assert not s.running
    assert s.percent == 0.0  # enumerated == 0 branch
    assert s.combos_per_sec == 0.0
    assert s.eta_seconds is None
    assert s.survivor_rate == 0.0
    assert s.elapsed_seconds == 0.0


def test_start_clamps_negative_enumerated() -> None:
    sp = SweepProgress(clock=_Clock())
    sp.start(enumerated=-5, detail="go")
    s = sp.snapshot()
    assert s.enumerated == 0
    assert s.state == STATE_RUNNING
    assert s.running
    assert s.detail == "go"


def test_advance_counts_and_clamps() -> None:
    clk = _Clock()
    sp = SweepProgress(clock=clk)
    sp.start(enumerated=2)
    clk.t = 4.0
    sp.advance(current="c0", survived=True)
    sp.advance(current="c1", skipped=True)
    sp.advance()  # would be the 3rd — clamped to enumerated=2
    s = sp.snapshot()
    assert s.evaluated == 2
    assert s.survivors == 1
    assert s.skipped == 1
    assert s.current == "c1"
    assert s.percent == 100.0


def test_advance_without_current_keeps_prior_label() -> None:
    sp = SweepProgress(clock=_Clock())
    sp.start(enumerated=3)
    sp.advance(current="first")
    sp.advance()  # current="" → label unchanged
    assert sp.snapshot().current == "first"


def test_rate_and_eta_measured() -> None:
    clk = _Clock()
    sp = SweepProgress(clock=clk)
    sp.start(enumerated=10)
    clk.t = 5.0
    sp.advance()  # 1 evaluated over 5s → 0.2/s
    s = sp.snapshot()
    assert s.elapsed_seconds == 5.0
    assert s.combos_per_sec == 0.2
    # remaining 9 / 0.2 = 45s
    assert s.eta_seconds == 45.0
    assert s.percent == 10.0


def test_eta_zero_when_all_evaluated() -> None:
    clk = _Clock()
    sp = SweepProgress(clock=clk)
    sp.start(enumerated=2)
    clk.t = 1.0
    sp.advance()
    sp.advance()  # all done, positive rate, remaining 0 → eta 0.0
    s = sp.snapshot()
    assert s.evaluated == 2
    assert s.eta_seconds == 0.0


def test_combos_per_sec_zero_when_no_elapsed_but_evaluated() -> None:
    # clock constant → updated_at == started_at → elapsed 0, evaluated 1.
    sp = SweepProgress(clock=_Clock())
    sp.start(enumerated=4)
    sp.advance()
    s = sp.snapshot()
    assert s.evaluated == 1
    assert s.elapsed_seconds == 0.0  # elapsed <= 0 branch
    assert s.combos_per_sec == 0.0  # elapsed <= 0 short-circuit


def test_combos_per_sec_zero_when_elapsed_but_no_eval() -> None:
    clk = _Clock()
    sp = SweepProgress(clock=clk)
    sp.start(enumerated=4)
    clk.t = 3.0  # but no advance → evaluated 0
    s = sp.snapshot()
    # elapsed is measured at last mutation (start), so still 0 here AND eval 0.
    assert s.evaluated == 0
    assert s.combos_per_sec == 0.0
    assert s.eta_seconds is None  # rate <= 0 branch


def test_survivor_rate() -> None:
    clk = _Clock()
    sp = SweepProgress(clock=clk)
    sp.start(enumerated=4)
    clk.t = 1.0
    sp.advance(survived=True)
    sp.advance()
    assert sp.snapshot().survivor_rate == 0.5


def test_finish_and_fail() -> None:
    sp = SweepProgress(clock=_Clock())
    sp.start(enumerated=1)
    sp.finish(detail="done")
    assert sp.snapshot().state == STATE_DONE
    sp.fail("boom")
    s = sp.snapshot()
    assert s.state == STATE_ERROR
    assert s.detail == "boom"


def test_start_resets_for_reuse() -> None:
    clk = _Clock()
    sp = SweepProgress(clock=clk)
    sp.start(enumerated=2)
    sp.advance(survived=True)
    sp.finish()
    sp.start(enumerated=5)  # reuse — counters reset
    s = sp.snapshot()
    assert s.enumerated == 5
    assert s.evaluated == 0
    assert s.survivors == 0
    assert s.state == STATE_RUNNING


def test_to_dict_shape() -> None:
    clk = _Clock()
    sp = SweepProgress(clock=clk)
    sp.start(enumerated=2)
    clk.t = 1.0
    sp.advance(survived=True)
    d = sp.snapshot().to_dict()
    assert set(d) == {
        "state",
        "enumerated",
        "evaluated",
        "survivors",
        "skipped",
        "current",
        "detail",
        "percent",
        "running",
        "elapsed_seconds",
        "combos_per_sec",
        "eta_seconds",
        "survivor_rate",
        "started_at",
        "updated_at",
        "updated_wall",
    }
    assert d["state"] == STATE_RUNNING
    assert d["evaluated"] == 1


def test_default_monotonic_clock() -> None:
    sp = SweepProgress()  # default _monotonic clock
    sp.start(enumerated=1)
    sp.advance()
    s = sp.snapshot()
    assert s.evaluated == 1
    assert s.started_at > 0.0
    assert s.elapsed_seconds >= 0.0


# ── property + thread-safety ────────────────────────────────────────────────


@given(
    enumerated=st.integers(min_value=0, max_value=50),
    advances=st.integers(min_value=0, max_value=100),
)
def test_property_evaluated_bounded_and_percent_in_range(enumerated: int, advances: int) -> None:
    sp = SweepProgress(clock=_Clock())
    sp.start(enumerated=enumerated)
    for _ in range(advances):
        sp.advance()
    s = sp.snapshot()
    assert 0 <= s.evaluated <= s.enumerated
    assert 0.0 <= s.percent <= 100.0


def test_thread_safe_concurrent_advance() -> None:
    sp = SweepProgress(clock=_Clock())
    sp.start(enumerated=1000)

    def worker() -> None:
        for _ in range(100):
            sp.advance(survived=True)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = sp.snapshot()
    assert s.evaluated == 1000  # no lost updates
    assert s.survivors == 1000


# ── DEFECT 2: wall-clock heartbeat + cross-process liveness verdict ──────────
#
# The bug: `sweep-heal`'s C6 guard compared `time.time()` (~1.7e9) against the
# snapshot's `updated_at`, which is a MONOTONIC stamp (a small per-process
# value), so `time.time() - updated_at` was ALWAYS >> 600 ⇒ never "fresh" ⇒
# `sweep_running` ALWAYS False ⇒ the heal proceeded even while a sweep was LIVE.
# The fix stamps a separate WALL-clock `updated_wall` and judges freshness on it.


def _live_snapshot(*, mono_t: float = 1234.0, wall_t: float = 1_750_000_000.0) -> dict[str, Any]:
    """Build a snapshot dict EXACTLY as a live sweep does — a small monotonic
    ``updated_at`` alongside a realistic wall-clock ``updated_wall``."""
    sp = SweepProgress(clock=_Clock(mono_t), wall_clock=_Clock(wall_t))
    sp.start(enumerated=10)
    sp.advance(current="NIFTY/1minute")
    return sp.snapshot().to_dict()


def test_wall_clock_is_stamped_independently_of_monotonic() -> None:
    snap = _live_snapshot(mono_t=1234.0, wall_t=1_750_000_000.0)
    # updated_at is the MONOTONIC stamp; updated_wall the WALL stamp — distinct.
    assert snap["updated_at"] == 1234.0
    assert snap["updated_wall"] == 1_750_000_000.0


def test_running_fresh_detects_a_just_updated_live_sweep() -> None:
    # FAILS-BEFORE / PASSES-AFTER regression: a running sweep updated 5 s ago in
    # wall time is LIVE.
    snap = _live_snapshot(mono_t=1234.0, wall_t=1_750_000_000.0)
    assert progress_running_fresh(snap, now_wall=1_750_000_000.0 + 5) is True
    # And the OLD (buggy) expression — wall-now minus the MONOTONIC updated_at —
    # is falsely stale, proving why the dead guard never fired. A revert of the
    # helper to read `updated_at` fails the assertion above.
    old_fresh = (1_750_000_000.0 + 5 - float(snap["updated_at"])) < HEAL_FRESH_SECONDS
    assert old_fresh is False


def test_running_stale_snapshot_is_not_fresh() -> None:
    snap = _live_snapshot(wall_t=1_750_000_000.0)
    # A wall heartbeat far in the past → not fresh → heal allowed.
    assert progress_running_fresh(snap, now_wall=1_750_000_000.0 + 10_000) is False


def test_running_exactly_at_boundary_is_not_fresh() -> None:
    # Strict `<` boundary: an age exactly == fresh_seconds is NOT fresh.
    snap = _live_snapshot(wall_t=1_000_000.0)
    assert progress_running_fresh(snap, now_wall=1_000_000.0 + HEAL_FRESH_SECONDS) is False


def test_not_running_is_never_fresh() -> None:
    sp = SweepProgress(clock=_Clock(1.0), wall_clock=_Clock(1_750_000_000.0))
    sp.start(enumerated=1)
    sp.finish()  # state = done, running = False
    snap = sp.snapshot().to_dict()
    assert snap["running"] is False
    # Even with a brand-new wall stamp, a NON-running sweep is not live.
    assert progress_running_fresh(snap, now_wall=1_750_000_000.0) is False


def test_missing_updated_wall_running_refuses_safely() -> None:
    # Old-format file (no updated_wall) that is running → treat as LIVE (safety).
    assert progress_running_fresh({"running": True}, now_wall=1_750_000_000.0) is True


def test_missing_updated_wall_not_running_is_not_fresh() -> None:
    assert progress_running_fresh({"running": False}, now_wall=1_750_000_000.0) is False
    # A totally empty snapshot (no running key) is not-running → not fresh.
    assert progress_running_fresh({}, now_wall=1_750_000_000.0) is False


def test_malformed_updated_wall_running_refuses_safely() -> None:
    # Non-numeric / non-finite stamp on a running sweep → treat as LIVE (safety).
    assert progress_running_fresh({"running": True, "updated_wall": "junk"}, now_wall=1.0) is True
    assert progress_running_fresh({"running": True, "updated_wall": None}, now_wall=1.0) is True
    assert (
        progress_running_fresh({"running": True, "updated_wall": float("nan")}, now_wall=1.0)
        is True
    )
    assert (
        progress_running_fresh({"running": True, "updated_wall": float("inf")}, now_wall=1.0)
        is True
    )


def test_bool_updated_wall_running_refuses_safely() -> None:
    # A bool is an int subclass (`float(True) == 1.0`), so WITHOUT the explicit
    # bool guard it would read as a valid ancient finite stamp and PERMIT the heal.
    # Safety-first: a bool wall stamp on a running sweep is malformed → refuse.
    assert progress_running_fresh({"running": True, "updated_wall": True}, now_wall=1.0) is True
    assert progress_running_fresh({"running": True, "updated_wall": False}, now_wall=1.0) is True
    # And a NOT-running snapshot with a bool stamp is still not-fresh (the running
    # short-circuit wins before the stamp is ever inspected).
    assert progress_running_fresh({"running": False, "updated_wall": True}, now_wall=1.0) is False


def test_default_wall_clock_is_time_time() -> None:
    # The default wall_clock is a real epoch (~1.7e9), distinct from monotonic.
    sp = SweepProgress()  # default monotonic + wall clocks
    sp.start(enumerated=1)
    sp.advance()
    snap = sp.snapshot().to_dict()
    assert snap["updated_wall"] > 1_000_000_000.0  # a real Unix epoch
    # A just-created running snapshot reads fresh against a now taken right after.
    import time

    assert progress_running_fresh(snap, now_wall=time.time()) is True
