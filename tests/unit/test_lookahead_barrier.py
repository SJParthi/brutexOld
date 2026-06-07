"""Unit tests for :mod:`brutex.feed.lookahead` — Property **P-06**'s live enforcer.

Pins the instrumented look-ahead barrier (DEC-LOOKAHEAD-BARRIER-001) at strict
100% line + branch:

* the boundary contract — access at exactly ``N`` (ok), ``N-1`` (ok), ``N+1``
  (raises :class:`LookAheadError`);
* every guard branch — access-before-advance, negative index, backward /
  negative cursor moves, the ``record`` toggle, the ``max_touched`` empty/non-
  empty split;
* :func:`verify_with_barrier` over both a clean run and a look-ahead run.

The barrier is pure (no Rust wheel, no lake), so these tests run on a bare
Python checkout. ``verify_with_barrier`` lazy-imports the core ``structlog``
runtime dep for its one material-action log line.
"""

from __future__ import annotations

import pytest
from brutex.feed.errors import LookAheadError
from brutex.feed.lookahead import (
    LookAheadBarrier,
    LookAheadReport,
    verify_with_barrier,
)

# ────────────────────────────────────────────────────────────────────────
# Construction + introspection
# ────────────────────────────────────────────────────────────────────────


def test_fresh_barrier_has_no_cursor_and_records_by_default() -> None:
    barrier = LookAheadBarrier()
    assert barrier.current_index == -1
    assert barrier.records is True
    assert barrier.touched_indices == ()
    assert barrier.max_touched is None


def test_record_false_keeps_o1_memory() -> None:
    barrier = LookAheadBarrier(record=False)
    barrier.advance_to(5)
    barrier.access(3)
    barrier.access(5)
    assert barrier.records is False
    assert barrier.touched_indices == ()  # nothing retained
    assert barrier.max_touched is None
    assert barrier.current_index == 5


# ────────────────────────────────────────────────────────────────────────
# Boundary contract — N-1 ok, N ok, N+1 raises
# ────────────────────────────────────────────────────────────────────────


def test_access_at_exactly_current_is_allowed() -> None:
    barrier = LookAheadBarrier()
    barrier.advance_to(7)
    assert barrier.access(7) == 7  # exactly N


def test_access_below_current_is_allowed() -> None:
    barrier = LookAheadBarrier()
    barrier.advance_to(7)
    assert barrier.access(6) == 6  # N-1
    assert barrier.access(0) == 0  # far back


def test_access_one_past_current_raises() -> None:
    barrier = LookAheadBarrier()
    barrier.advance_to(7)
    with pytest.raises(LookAheadError, match="look-ahead read forbidden"):
        barrier.access(8)  # N+1


def test_violating_index_is_recorded_before_raising() -> None:
    """The audit trail captures the attempted look-ahead index."""
    barrier = LookAheadBarrier(record=True)
    barrier.advance_to(2)
    with pytest.raises(LookAheadError):
        barrier.access(9)
    assert barrier.touched_indices == (9,)
    assert barrier.max_touched == 9


def test_record_false_does_not_retain_violating_index() -> None:
    barrier = LookAheadBarrier(record=False)
    barrier.advance_to(2)
    with pytest.raises(LookAheadError):
        barrier.access(9)
    assert barrier.touched_indices == ()


# ────────────────────────────────────────────────────────────────────────
# access guard branches
# ────────────────────────────────────────────────────────────────────────


def test_access_before_any_advance_is_lookahead() -> None:
    barrier = LookAheadBarrier()
    with pytest.raises(LookAheadError, match="no bar reached yet"):
        barrier.access(0)


def test_negative_access_index_raises_value_error() -> None:
    barrier = LookAheadBarrier()
    barrier.advance_to(3)
    with pytest.raises(ValueError, match="must be >= 0"):
        barrier.access(-1)


def test_touched_trail_accumulates_in_call_order() -> None:
    barrier = LookAheadBarrier()
    for i in range(4):
        barrier.advance_to(i)
        barrier.access(i)
    barrier.access(0)  # look back
    assert barrier.touched_indices == (0, 1, 2, 3, 0)
    assert barrier.max_touched == 3


# ────────────────────────────────────────────────────────────────────────
# advance_to branches — forward-only, non-negative
# ────────────────────────────────────────────────────────────────────────


def test_advance_to_negative_raises() -> None:
    barrier = LookAheadBarrier()
    with pytest.raises(ValueError, match="must be >= 0"):
        barrier.advance_to(-1)


def test_advance_to_backwards_raises() -> None:
    barrier = LookAheadBarrier()
    barrier.advance_to(5)
    with pytest.raises(ValueError, match="cannot move backwards"):
        barrier.advance_to(4)


def test_advance_to_same_index_is_allowed() -> None:
    """Re-evaluating the same bar (cursor unchanged) is not a regression."""
    barrier = LookAheadBarrier()
    barrier.advance_to(5)
    barrier.advance_to(5)  # equal — allowed
    assert barrier.current_index == 5


# ────────────────────────────────────────────────────────────────────────
# guard + reset
# ────────────────────────────────────────────────────────────────────────


def test_guard_returns_value_unchanged_when_index_ok() -> None:
    barrier = LookAheadBarrier()
    barrier.advance_to(1)
    sentinel = object()
    assert barrier.guard(1, sentinel) is sentinel
    assert barrier.touched_indices == (1,)


def test_guard_raises_on_future_index() -> None:
    barrier = LookAheadBarrier()
    barrier.advance_to(1)
    with pytest.raises(LookAheadError):
        barrier.guard(2, object())


def test_reset_clears_cursor_and_trail() -> None:
    barrier = LookAheadBarrier()
    barrier.advance_to(3)
    barrier.access(3)
    barrier.reset()
    assert barrier.current_index == -1
    assert barrier.touched_indices == ()
    assert barrier.max_touched is None
    # usable again after reset
    barrier.advance_to(0)
    assert barrier.access(0) == 0


# ────────────────────────────────────────────────────────────────────────
# verify_with_barrier — manual-recheck core
# ────────────────────────────────────────────────────────────────────────


def test_verify_clean_forward_replay_passes() -> None:
    barrier = LookAheadBarrier(record=True)

    def run() -> None:
        for i in range(5):
            barrier.advance_to(i)
            barrier.access(i)

    report = verify_with_barrier(run, barrier)
    assert isinstance(report, LookAheadReport)
    assert report.passed is True
    assert report.indices_checked == 5
    assert report.max_index_touched == 4
    assert report.final_cursor == 4
    assert report.violation is None


def test_verify_detects_lookahead_run() -> None:
    barrier = LookAheadBarrier(record=True)

    def run() -> None:
        barrier.advance_to(0)
        barrier.access(0)
        barrier.access(3)  # peek into the future → LookAheadError

    report = verify_with_barrier(run, barrier)
    assert report.passed is False
    assert report.violation is not None
    assert "look-ahead" in report.violation
    # the trail still captured the good read + the violating index
    assert report.indices_checked == 2
    assert report.max_index_touched == 3
    assert report.final_cursor == 0


def test_verify_with_record_false_barrier_reports_zero_checks() -> None:
    barrier = LookAheadBarrier(record=False)

    def run() -> None:
        for i in range(3):
            barrier.advance_to(i)
            barrier.access(i)

    report = verify_with_barrier(run, barrier)
    assert report.passed is True
    assert report.indices_checked == 0  # nothing retained in O(1) mode
    assert report.max_index_touched is None
    assert report.final_cursor == 2
