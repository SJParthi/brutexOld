"""Unit tests for ``brutex.adapters.groww.deadline`` (the wall-clock freeze guard).

DEC-GROWW-PULL-CALL-DEADLINE-001 + MINOR-1 of DEC-GROWW-TOKEN-REAUTH-001. The
:func:`run_with_deadline` helper bounds an otherwise-unbounded SDK call (historical
candles OR the token re-mint) by a wall-clock deadline. These cover all three
branches with NO ``growwapi`` / ``pyotp`` and no network — the fast success path, the
inner-exception re-raise, and the TIMEOUT path (a fn that blocks on a
:class:`threading.Event` past a short deadline → :class:`TimeoutError`).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

import pytest
from brutex.adapters.groww.deadline import DeadlineExceeded, run_with_deadline


def test_returns_the_value_when_fast() -> None:
    """A fn that returns within the deadline yields its value verbatim."""
    assert run_with_deadline(lambda: {"candles": []}, 5.0) == {"candles": []}


def test_propagates_the_underlying_error() -> None:
    """An inner exception (not a timeout) is re-raised on the caller thread."""

    def _boom() -> Mapping[str, object]:
        raise ValueError("real failure, not a timeout")

    with pytest.raises(ValueError, match="real failure"):
        run_with_deadline(_boom, 5.0)


def test_raises_timeout_when_fn_blows_the_deadline() -> None:
    """A fn that blocks past the deadline raises a ``deadline exceeded`` TimeoutError.

    The released-after event lets the abandoned daemon worker exit cleanly once the
    deadline has already fired (it never blocks the test process — it is a daemon).
    """
    release = threading.Event()

    def _hang() -> Mapping[str, object]:
        release.wait(timeout=5.0)  # blocks well past the 0.05s deadline below
        return {"candles": []}

    try:
        with pytest.raises(TimeoutError, match="deadline exceeded"):
            run_with_deadline(_hang, 0.05)
    finally:
        release.set()  # let the abandoned daemon worker exit


def test_deadline_timeout_is_a_deadline_exceeded_subclass() -> None:
    """The guard's own timeout is a ``DeadlineExceeded`` — a ``TimeoutError`` SUBCLASS.

    DEC-FETCH-HUNG-WRITE-DEADLINE-001 hardening: the distinct subclass lets a caller tell the
    GUARD's timeout apart from a ``TimeoutError`` raised by ``fn`` itself, while every existing
    ``except TimeoutError`` caller (reauth re-mint + the candle-call classification) still catches
    it because ``DeadlineExceeded`` IS a ``TimeoutError``.
    """
    release = threading.Event()

    def _hang() -> Mapping[str, object]:
        release.wait(timeout=5.0)
        return {"candles": []}

    try:
        with pytest.raises(DeadlineExceeded) as excinfo:
            run_with_deadline(_hang, 0.05)
        assert isinstance(excinfo.value, TimeoutError)  # back-compat: existing callers still catch
        assert "deadline exceeded" in str(excinfo.value)  # message hint unchanged
    finally:
        release.set()


def test_fn_own_timeout_error_propagates_not_retyped() -> None:
    """A real ``TimeoutError`` raised by ``fn`` itself propagates UNCHANGED (not a DeadlineExceeded).

    The guard only RAISES ``DeadlineExceeded`` for its OWN deadline; an ``fn``-raised
    ``TimeoutError`` rides the ``failure`` re-raise path and is NOT re-typed — so
    ``append_with_deadline`` can match ``DeadlineExceeded`` and let a writer's own timeout through.
    """
    sentinel = TimeoutError("fn's own timeout")

    def _boom() -> Mapping[str, object]:
        raise sentinel

    with pytest.raises(TimeoutError) as excinfo:
        run_with_deadline(_boom, 5.0)
    assert excinfo.value is sentinel  # the exact object, never re-typed to DeadlineExceeded
    assert not isinstance(excinfo.value, DeadlineExceeded)


def test_is_generic_over_the_return_type() -> None:
    """The guard preserves a non-Mapping return type (e.g. a tuple — the re-mint shape)."""
    out = run_with_deadline(lambda: ("a", "b", "c"), 5.0)
    assert out == ("a", "b", "c")


def test_daemon_threads_drain_after_a_timeout_storm() -> None:
    """A timeout storm leaves no daemon backlog once the simulated sockets fire.

    DEC-PULL-HEALTH-CONCURRENCY-001 GAP #18. Each timed-out call abandons ONE daemon
    worker that blocks until its underlying SDK socket finally returns — the daemon dies
    with the call. So the live-daemon count is bounded by the (finite) socket timeout,
    NOT by :func:`run_with_deadline`. This pins that bound: fire N timed-out calls (each
    blocking on a per-call release event that stands in for a short socket timeout), then
    release every socket and assert ``threading.active_count()`` returns to its pre-storm
    baseline — i.e. every abandoned daemon drained, none accumulated.
    """
    storm = 20
    baseline = threading.active_count()
    releases = [threading.Event() for _ in range(storm)]

    def _make_hang(event: threading.Event) -> Callable[[], Mapping[str, object]]:
        def _hang() -> Mapping[str, object]:
            event.wait(timeout=5.0)  # stands in for the SDK socket timeout
            return {"candles": []}

        return _hang

    try:
        for event in releases:
            with pytest.raises(TimeoutError, match="deadline exceeded"):
                run_with_deadline(_make_hang(event), 0.05)
        # The storm has abandoned `storm` daemon workers, each still blocked on its event.
        # Now fire every "socket": each daemon returns and exits, so the count must drain.
        for event in releases:
            event.set()
        deadline = time.monotonic() + 5.0
        while threading.active_count() > baseline and time.monotonic() < deadline:
            time.sleep(0.01)
        assert threading.active_count() == baseline, (
            "abandoned deadline daemons did not drain back to baseline after their "
            "simulated sockets fired"
        )
    finally:
        for event in releases:
            event.set()  # belt-and-suspenders: never leave a daemon blocked


def test_shim_reexports_the_common_config_primitive() -> None:
    """DEC-STORE-WRITE-DEADLINE-001 relocation pin: this module is a byte-compatible
    re-export of the ONE COMMON primitive at :mod:`brutex.config.deadline` — the
    exception CLASS is the SAME object on both import paths, so an ``except
    DeadlineExceeded`` caller catches identically regardless of which path raised."""
    from brutex.config import deadline as config_deadline

    assert DeadlineExceeded is config_deadline.DeadlineExceeded
    assert run_with_deadline is config_deadline.run_with_deadline
