"""Unit tests for ``brutex.adapters.groww.deadline`` (the wall-clock freeze guard).

DEC-GROWW-PULL-CALL-DEADLINE-001 + MINOR-1 of DEC-GROWW-TOKEN-REAUTH-001. The
:func:`run_with_deadline` helper bounds an otherwise-unbounded SDK call (historical
candles OR the token re-mint) by a wall-clock deadline. These cover all three
branches with NO ``growwapi`` / ``pyotp`` and no network — the fast success path, the
inner-exception re-raise, and the TIMEOUT path (a fn that blocks on a
:class:`threading.Event` past a short deadline → :class:`TimeoutError`) — plus the
caller-context propagation contract (DEC-PULL-BEAT-PERRUN-001 fix round: the daemon
worker runs in a COPY of the caller's contextvars context, one fresh copy per call).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

import pytest
import structlog.contextvars
from brutex.adapters.groww.deadline import DeadlineExceeded, run_with_deadline
from brutex.observability.correlation import current_pull_id, pull_id_scope


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


class TestCallerContextPropagation:
    """The daemon worker runs inside a COPY of the CALLER's contextvars context.

    DEC-PULL-BEAT-PERRUN-001 fix round. A fresh :class:`threading.Thread` starts with an
    EMPTY contextvars context, so before the fix the wrapped *fn* — including the
    partition-flock lock-wait ``emit_progress_beat`` inside a deadline-wrapped
    ``sink.append`` in BOTH spine tails — resolved the ambient ``pull_id`` to ``""`` and
    its beats DROPPED as foreign instead of feeding the run's keyed watchdog channel.
    Fails-before / passes-after: ``test_worker_inherits_the_callers_ambient_pull_id``
    returned ``""`` on the pre-fix empty-context worker.
    """

    def test_worker_inherits_the_callers_ambient_pull_id(self) -> None:
        """The wrapped fn sees the caller's ambient run id (the keyed-beat prerequisite)."""
        with pull_id_scope("abc123def456"):
            assert run_with_deadline(current_pull_id, 5.0) == "abc123def456"

    def test_scopeless_caller_propagates_an_empty_context(self) -> None:
        """No caller scope → the worker's ambient id stays "" (the CLI path, byte-identical)."""
        assert run_with_deadline(current_pull_id, 5.0) == ""

    def test_per_call_copy_never_leaks_binds_across_calls_or_into_the_caller(self) -> None:
        """A bind made INSIDE the worker dies with its per-call context copy.

        The ``PoolRunParallel`` per-task-copy precedent: *fn* poisons its own copy with a
        raw (never-unbound) ``bind_contextvars``; the poison must be visible to that call
        only — a later call starts from a fresh copy of the caller's (clean) context, and
        the caller itself is untouched.
        """

        def _poison() -> str:
            structlog.contextvars.bind_contextvars(pull_id="deadbeefbad1")  # deliberately raw
            return current_pull_id()

        assert run_with_deadline(_poison, 5.0) == "deadbeefbad1"
        assert run_with_deadline(current_pull_id, 5.0) == ""  # no cross-call leak
        assert current_pull_id() == ""  # the caller's own context is untouched


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
