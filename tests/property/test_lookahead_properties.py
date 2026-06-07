"""Property test for **P-06** — the runtime look-ahead barrier.

Promotes the ``test_invariants_catalog.py`` P-06 ROADMAP stub to a live
hypothesis invariant (DEC-LOOKAHEAD-BARRIER-001). The barrier
(:class:`brutex.feed.lookahead.LookAheadBarrier`) is the instrumented
bar accessor demanded by ``docs/TRACK1_VERIFICATION_PYRAMID.md`` §3 /
CLAUDE.md §4: for any current bar index ``N``, an ``access(i)`` reads
only indices ``i ≤ N``.

The two halves of the invariant, over arbitrary ``(N, i)``:

* **never-false-negative** — every access index ``i > N`` ALWAYS raises
  :class:`~brutex.feed.errors.LookAheadError`;
* **never-false-positive** — every access index ``0 ≤ i ≤ N`` NEVER
  raises, and returns ``i`` unchanged.

Plus a forward-replay invariant: replaying ``n`` bars in order touches
only indices in ``[0, n-1]`` and the max touched index never exceeds the
final cursor.

The barrier is pure (no Rust wheel, no lake), so this file runs on a bare
checkout.
"""

from __future__ import annotations

import pytest
from brutex.feed.errors import LookAheadError
from brutex.feed.lookahead import LookAheadBarrier
from hypothesis import given
from hypothesis import strategies as st


@given(
    current=st.integers(min_value=0, max_value=10_000),
    delta=st.integers(min_value=1, max_value=10_000),
)
def test_p06_access_beyond_current_always_raises(current: int, delta: int) -> None:
    """Never-false-negative: any access index > N raises (both record modes)."""
    for record in (True, False):
        barrier = LookAheadBarrier(record=record)
        barrier.advance_to(current)
        with pytest.raises(LookAheadError):
            barrier.access(current + delta)


@given(data=st.data(), current=st.integers(min_value=0, max_value=10_000))
def test_p06_access_within_window_never_raises(data: st.DataObject, current: int) -> None:
    """Never-false-positive: any access index in [0, N] is allowed and returned."""
    index = data.draw(st.integers(min_value=0, max_value=current))
    barrier = LookAheadBarrier()
    barrier.advance_to(current)
    assert barrier.access(index) == index
    assert index in barrier.touched_indices


@given(n=st.integers(min_value=1, max_value=500))
def test_p06_forward_replay_touches_only_past_or_present(n: int) -> None:
    """A forward replay over n bars touches only [0, n-1] and max ≤ final cursor."""
    barrier = LookAheadBarrier(record=True)
    for index in range(n):
        barrier.advance_to(index)
        barrier.access(index)
    touched = barrier.touched_indices
    assert touched == tuple(range(n))
    assert barrier.max_touched == n - 1
    assert barrier.max_touched <= barrier.current_index


@given(
    cursor_moves=st.lists(st.integers(min_value=0, max_value=1_000), min_size=1, max_size=50),
)
def test_p06_cursor_is_monotonic_or_rejected(cursor_moves: list[int]) -> None:
    """advance_to accepts a non-decreasing sequence and rejects any backward move."""
    barrier = LookAheadBarrier()
    ascending = sorted(cursor_moves)
    for index in ascending:
        barrier.advance_to(index)  # non-decreasing → always accepted
    assert barrier.current_index == ascending[-1]
    # A strictly-smaller move is always rejected.
    if ascending[-1] > 0:
        with pytest.raises(ValueError, match="backwards"):
            barrier.advance_to(ascending[-1] - 1)
