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

from brutex.engine.sweep_progress import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_ERROR,
    STATE_IDLE,
    STATE_RUNNING,
    SweepProgress,
)
from hypothesis import given
from hypothesis import strategies as st


class _Clock:
    """A manually-advanced fake clock."""

    def __init__(self) -> None:
        self.t = 0.0

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


def test_cancelled_terminal_state() -> None:
    """cancelled() is the honest operator-cancel terminal (DEC-JOB-COOPERATIVE-CANCEL-001).

    The state reads ``cancelled`` (never ``error``/``done``), ``running`` flips
    False (so the v29 settle branch fires), the detail names the boundary, the
    clock stamps the mutation, and the wire dict round-trips the state string.
    """
    clk = _Clock()
    sp = SweepProgress(clock=clk)
    sp.start(enumerated=4)
    clk.t = 2.0
    sp.cancelled(detail="cancelled at tuple 2/4")
    s = sp.snapshot()
    assert s.state == STATE_CANCELLED
    assert s.state == "cancelled"
    assert s.detail == "cancelled at tuple 2/4"
    assert s.running is False
    assert s.updated_at == 2.0
    assert s.to_dict()["state"] == "cancelled"
    assert s.to_dict()["running"] is False


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
