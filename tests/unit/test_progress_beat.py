"""Unit tests for the process-global liveness-beat seam.

DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15. :mod:`brutex.observability.progress_beat`
is the decoupled seam the bounded partition lock-wait + the rate pacer fire into so a
slow-but-progressing pull keeps its watchdog heartbeat alive. 100% line+branch:
register → emit → unregister, the no-op-when-unregistered fast path, and the
best-effort swallow of a raising beat (never crashes the worker).
"""

from __future__ import annotations

import pytest
from brutex.observability.progress_beat import (
    emit_progress_beat,
    register_progress_beat,
    unregister_progress_beat,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> object:
    """Each test starts + ends with no registered beat (no cross-test bleed)."""
    unregister_progress_beat()
    yield
    unregister_progress_beat()


def test_emit_is_noop_when_unregistered() -> None:
    """With nothing registered, emit is a cheap no-op (every non-dashboard caller)."""
    emit_progress_beat()  # must not raise


def test_register_then_emit_fires_beat() -> None:
    beats: list[int] = []
    register_progress_beat(lambda: beats.append(1))
    emit_progress_beat()
    emit_progress_beat()
    assert beats == [1, 1]


def test_unregister_stops_the_beat() -> None:
    beats: list[int] = []
    register_progress_beat(lambda: beats.append(1))
    emit_progress_beat()
    unregister_progress_beat()
    emit_progress_beat()  # no longer fires
    assert beats == [1]


def test_unregister_is_idempotent() -> None:
    unregister_progress_beat()
    unregister_progress_beat()  # no error when nothing is registered


def test_register_last_write_wins() -> None:
    a: list[int] = []
    b: list[int] = []
    register_progress_beat(lambda: a.append(1))
    register_progress_beat(lambda: b.append(1))
    emit_progress_beat()
    assert a == []
    assert b == [1]


def test_raising_beat_is_swallowed() -> None:
    """A buggy beat can never crash the worker that is merely signalling liveness."""
    calls: list[int] = []

    def _boom() -> None:
        calls.append(1)
        raise RuntimeError("buggy beat")

    register_progress_beat(_boom)
    emit_progress_beat()  # the RuntimeError is swallowed + logged, not raised
    assert calls == [1]
