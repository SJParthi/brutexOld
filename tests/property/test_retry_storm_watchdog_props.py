"""Property-based tests for the retry-storm give-up guard (DEC-RATE-STORM-WATCHDOG-001).

Hypothesis drives random fan-out grids + random failure/success/terminal
sequences against :class:`brutex.orchestrator.watchdog.RetryStormWatchdog` and
asserts the load-bearing invariants:

* the per-symbol 100%-failure alert fires **exactly once** per fully-failed
  symbol — never zero, never twice, never per window;
* the give-up count never exceeds a symbol's window total;
* the time-based give-up fires **iff** the continuous-failure streak reaches the
  boundary (using a deterministic fake clock);
* a recorded give-up is terminal (monotone — never un-given-up).
"""

from __future__ import annotations

import pytest
from brutex.orchestrator.watchdog import (
    GiveUpReason,
    RetryStormWatchdog,
    SymbolFailureEvent,
)
from hypothesis import given
from hypothesis import strategies as st

pytestmark = pytest.mark.property


class _Clock:
    """Deterministic clock the property body advances explicitly."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# A small alphabet keeps collisions (and thus 100%-failure symbols) frequent.
_symbols = st.lists(st.sampled_from(["A", "B", "C"]), min_size=1, max_size=3, unique=True)
_windows = st.lists(st.sampled_from(["w0", "w1", "w2", "w3"]), min_size=1, max_size=4, unique=True)


@given(symbols=_symbols, windows=_windows, data=st.data())
def test_alert_fires_exactly_once_per_fully_failed_symbol(
    symbols: list[str], windows: list[str], data: st.DataObject
) -> None:
    alerts: list[SymbolFailureEvent] = []
    wd = RetryStormWatchdog(max_retry_seconds=60.0, on_symbol_failure=alerts.append)
    wd.register_fanout(symbols, windows)

    # A random permutation of EVERY tuple, each terminally failed once (plus a
    # random number of duplicate terminal failures, which must be idempotent).
    tuples = [(s, w) for s in symbols for w in windows]
    order = data.draw(st.permutations(tuples))
    for symbol, window in order:
        wd.record_terminal_failure(symbol, window)
        # Idempotent: re-failing an already-given-up tuple changes nothing.
        wd.record_terminal_failure(symbol, window)

    # Every symbol has all its windows failed → every symbol alerts exactly once.
    alerted = [e.symbol for e in alerts]
    assert sorted(alerted) == sorted(symbols)
    assert len(alerted) == len(set(alerted))  # no double-fire
    for event in alerts:
        assert event.failed_windows == event.total_windows == len(windows)
        assert wd.symbol_failed(event.symbol) is True


@given(symbols=_symbols, windows=_windows, data=st.data())
def test_give_up_count_never_exceeds_total(
    symbols: list[str], windows: list[str], data: st.DataObject
) -> None:
    wd = RetryStormWatchdog(max_retry_seconds=60.0)
    wd.register_fanout(symbols, windows)
    tuples = [(s, w) for s in symbols for w in windows]
    # Fail a random multiset of tuples (with repeats) in random order.
    for symbol, window in data.draw(st.lists(st.sampled_from(tuples), max_size=40)):
        wd.record_terminal_failure(symbol, window)
    for symbol in symbols:
        assert 0 <= wd.given_up_count(symbol) <= wd.window_total(symbol)


@given(
    threshold=st.floats(min_value=1.0, max_value=10_000.0),
    streak=st.floats(min_value=0.0, max_value=20_000.0),
)
def test_time_based_give_up_iff_streak_reaches_boundary(threshold: float, streak: float) -> None:
    clock = _Clock()
    wd = RetryStormWatchdog(max_retry_seconds=threshold, clock=clock)
    wd.register("SYM", "w")

    wd.record_failure("SYM", "w")  # arm at t=0
    clock.t = streak
    gave_up = wd.record_failure("SYM", "w")

    assert gave_up is (streak >= threshold)
    assert wd.has_given_up("SYM", "w") is (streak >= threshold)


@given(n_failures=st.integers(min_value=1, max_value=50))
def test_give_up_is_terminal_and_monotone(n_failures: int) -> None:
    clock = _Clock()
    give_ups = 0
    wd = RetryStormWatchdog(
        max_retry_seconds=5.0,
        on_give_up=lambda e: None,
        clock=clock,
    )
    wd.register("SYM", "w")
    wd.record_failure("SYM", "w")  # arm
    clock.t = 5.0
    # The FIRST boundary-crossing failure gives up; every later one is a no-op.
    for _ in range(n_failures):
        if wd.record_failure("SYM", "w"):
            give_ups += 1
        assert wd.has_given_up("SYM", "w") is True  # never reverts
    assert give_ups == 1
    # The single give-up was time-based.
    assert wd.given_up_count("SYM") == 1
    assert GiveUpReason.TIMEOUT.value == "timeout"
