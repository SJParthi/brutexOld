"""Property **P-06** look-ahead proof for BOS/CHoCH market structure.

The MANDATORY look-ahead gate for DEC-NIFTY-BOS-CHOCH-001 (ADR-0012 §4.5 #13 —
the reason these bits were deferred). The BOS/CHoCH detector confirms a fractal
swing only ``k`` bars LATER, so when evaluating bar ``N`` it must reference only
bars at index ``≤ N`` — NEVER a future bar.

Since DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 the detector under proof is the
PRODUCTION ``trading_core.MarketStructureState`` pyclass (the single source the
engine drives — the hand-synced Python twin this file previously exercised is
deleted); the confirmed-swing levels are read back through its additive
``last_swing_high``/``last_swing_low`` getters, so the property holds at the
SAME strength against the code path production actually runs.

This file proves P-06 by routing EVERY bar read through
:class:`brutex.feed.lookahead.LookAheadBarrier` (the index-based P-06 enforcer):

* :func:`test_streaming_feed_touches_no_future_bar` — a barrier-backed feed
  drives the detector bar by bar; the barrier's cursor sits at ``N`` while bar
  ``N`` is consumed, and the recorded touched-index trail proves the drive
  reached NO index ``> N``.
* :func:`test_confirmed_swing_is_always_a_past_bar` — whenever the detector
  exposes a confirmed swing at bar ``N``, that swing's level equals a bar at an
  index ``≤ N`` (read back THROUGH the barrier, which would raise on a future
  index) — the substantive "a swing is confirmed k bars later, never peeked"
  invariant.
* :func:`test_barrier_catches_a_deliberate_future_read` — the NON-VACUOUS
  control: a driver that deliberately peeks bar ``N+1`` DOES trip the barrier, so
  the two passing tests above are not green merely because the barrier is inert.

The barrier is pure Python (no lake); the detector is the installed
``trading_core`` wheel — the same import every vocabulary test needs.
"""

from __future__ import annotations

from typing import Any

import pytest
import trading_core
from brutex.feed.errors import LookAheadError
from brutex.feed.lookahead import LookAheadBarrier
from hypothesis import given
from hypothesis import strategies as st

# A bar is (high, low, close) in int64 paisa, with high >= low enforced by the
# generator so the swing geometry is well-formed.
_Bar = tuple[int, int, int]

#: One bar's resolved 4-bool signal in the Rust FFI wire order.
_Signal = tuple[bool, bool, bool, bool]


def _bars() -> st.SearchStrategy[list[_Bar]]:
    """Streams of well-formed (high>=low) bars long enough to confirm swings."""
    return st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=500),
            st.integers(min_value=1, max_value=500),
            st.integers(min_value=1, max_value=500),
        ).map(lambda t: (max(t[0], t[1]), min(t[0], t[1]), t[2])),
        min_size=1,
        max_size=120,
    )


class _BarrierFeed:
    """A bar array whose reads are gated by a :class:`LookAheadBarrier`.

    ``read(i)`` routes index ``i`` through the barrier — raising
    :class:`LookAheadError` if ``i`` is beyond the current cursor ``N`` — then
    returns ``bars[i]``. This is the instrumented "bar access" of P-06: anything
    the drive reads goes through here, so the recorded trail PROVES which
    indices it reached.
    """

    def __init__(self, bars: list[_Bar], barrier: LookAheadBarrier) -> None:
        self._bars = bars
        self._barrier = barrier

    def read(self, index: int) -> _Bar:
        self._barrier.access(index)
        return self._bars[index]


def _drive_streaming(
    bars: list[_Bar], barrier: LookAheadBarrier
) -> tuple[list[_Signal], int | None, int | None]:
    """Feed the detector bar by bar through the barrier (the honest streaming path).

    At step ``N`` the cursor is advanced to ``N`` and bar ``N`` is read THROUGH the
    barrier before being fed. The detector is a streaming state object: it only
    ever consumes the bar handed to ``update`` (its swing confirmation reads its
    OWN internal ring of already-consumed bars, never the source array), so a
    correct drive touches only index ``N`` at step ``N`` — index ``≤ N`` overall.

    Returns the OBSERVABLE outcome — the per-bar signal sequence plus the final
    confirmed swing levels (the pyclass has no value equality, so the observable
    trail is what the determinism property compares).
    """
    feed = _BarrierFeed(bars, barrier)
    m: Any = trading_core.MarketStructureState(2)
    signals: list[_Signal] = []
    for n in range(len(bars)):
        barrier.advance_to(n)
        high, low, close = feed.read(n)  # the ONLY source read per step
        signals.append(tuple(m.update(high, low, close)))
    return signals, m.last_swing_high, m.last_swing_low


@given(bars=_bars())
def test_streaming_feed_touches_no_future_bar(bars: list[_Bar]) -> None:
    """P-06: across the whole stream the drive touches no index > its cursor."""
    barrier = LookAheadBarrier(record=True)
    _drive_streaming(bars, barrier)
    max_touched = barrier.max_touched
    if max_touched is not None:  # an empty stream records nothing
        assert max_touched <= barrier.current_index
    # And every touched index is in range (no negative / out-of-band read).
    assert all(0 <= i < len(bars) for i in barrier.touched_indices)


@given(bars=_bars())
def test_confirmed_swing_is_always_a_past_bar(bars: list[_Bar]) -> None:
    """A swing exposed at bar N is always a bar at index <= N (k bars in the past).

    The substantive look-ahead invariant: re-drive the detector step by step and,
    at every N where a confirmed swing is present (via the production getters),
    locate that swing's level among bars [0..N] read THROUGH the barrier. If the
    swing ever corresponded only to a future bar, the barrier read would raise —
    so reaching the assertion at all proves the swing came from a non-future bar.
    """
    barrier = LookAheadBarrier(record=False)
    feed = _BarrierFeed(bars, barrier)
    m: Any = trading_core.MarketStructureState(2)
    for n in range(len(bars)):
        barrier.advance_to(n)
        high, low, close = feed.read(n)
        m.update(high, low, close)
        if m.last_swing_high is not None:
            # Some bar at index <= N must carry this high (read through the barrier
            # — a future index would raise LookAheadError here, failing the test).
            assert any(feed.read(i)[0] == m.last_swing_high for i in range(n + 1))
        if m.last_swing_low is not None:
            assert any(feed.read(i)[1] == m.last_swing_low for i in range(n + 1))


def test_barrier_catches_a_deliberate_future_read() -> None:
    """NON-VACUOUS control: a driver that peeks bar N+1 trips the barrier.

    Proves the barrier is a real enforcer (the two passing tests above are not
    green merely because the barrier never fires). A correct drive NEVER does
    this; here we deliberately read one bar into the future and assert it raises.
    """
    bars: list[_Bar] = [(100, 90, 95), (110, 100, 105), (130, 120, 125), (115, 105, 110)]
    barrier = LookAheadBarrier(record=True)
    feed = _BarrierFeed(bars, barrier)
    barrier.advance_to(0)
    feed.read(0)  # legal: index 0 == cursor
    with pytest.raises(LookAheadError):
        feed.read(1)  # ILLEGAL: index 1 > cursor 0 — a one-bar peek into the future


def test_streaming_feed_is_deterministic_through_barrier() -> None:
    """Driving the same stream through a fresh barrier twice yields the same
    observable outcome (the barrier is observe-only — it never perturbs the
    detector), and the barrier-driven outcome equals the barrier-free one."""
    bars: list[_Bar] = [
        (100, 90, 95),
        (110, 100, 105),
        (130, 120, 125),
        (115, 105, 110),
        (112, 102, 108),
        (140, 120, 135),
    ]
    a = _drive_streaming(bars, LookAheadBarrier(record=False))
    b = _drive_streaming(bars, LookAheadBarrier(record=False))
    assert a == b
    # The same stream WITHOUT a barrier produces the identical observable
    # outcome (observe-only) — and the swing levels are genuinely confirmed
    # (the Some arm is non-vacuous for this scripted stream).
    plain: Any = trading_core.MarketStructureState(2)
    plain_signals: list[_Signal] = [tuple(plain.update(h, low, c)) for h, low, c in bars]
    assert (plain_signals, plain.last_swing_high, plain.last_swing_low) == a
    assert plain.last_swing_high == 130
    assert plain.last_swing_low is None
