"""The zombie-write payload handoff (DEC-WRITE-DEADLINE-ZOMBIE-SLIM-001).

Breaker-B B6: ``append_with_deadline`` built ``lambda: sink.append(...,
bars=bars)`` — the closure CELL (plus the guard frame's own ``bars`` parameter,
which rides the raised ``WriteDeadlineExceeded``'s traceback up the spine)
pinned the whole window's payload (~10.3 MiB per 30-day 1-minute window,
bounded 20-60 zombies/pull) for the zombie daemon's ENTIRE wedge, even after
the executing ``sink.append`` frame no longer needed it. The fix is a ONE-SHOT
POP HANDOFF: the payload rides a single-slot holder the worker pops INLINE in
the call expression (CPython transfers argument ownership into the callee
frame), and the guard frame ``del``\\ s its own parameter — so after the call
starts, NO guard-layer reference (closure cell / thread args / traceback
frame) survives beyond the in-flight ``sink.append`` frame itself.

The pin is refcount-truth, deterministic (event-sequenced, no sleeps): a
weakref-able payload, a sink whose ``append`` DROPS its own arg then wedges —
post-fix the weakref dies while the zombie is still wedged; pre-fix the lambda
cell + guard frame kept it alive (captured failing on the pre-fix tree).

HONEST envelope: the EXECUTING frame's pin is irreducible — a real
``ParquetWriter.append`` holds ``bars`` (and its rendered arrow table) in its
own locals while wedged; this fix removes only the GUARD-layer duplicate
retention (see the ``write_deadline`` module docstring's bytes envelope).
"""

from __future__ import annotations

import gc
import threading
import weakref
from typing import Any

import pytest
from brutex.adapters.groww.write_deadline import append_with_deadline
from brutex.data.errors import WriteDeadlineExceeded


class _WeakrefableBars(list):
    """A ``list`` subclass so the payload is weakref-able (plain lists are not)."""

    __slots__ = ("__weakref__",)


class _WedgingDroppingSink:
    """``append`` drops its OWN reference to ``bars``, then wedges until released.

    Any surviving strong reference to the payload after ``started`` fires is
    therefore held by the GUARD layer (closure / thread args / caller frame) —
    exactly the retention class the one-shot pop handoff removes.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def append(self, **kwargs: Any) -> None:
        del kwargs  # the executing frame drops its args — the honest-minimum pin
        self.started.set()
        # The wedge: a hung fsync/replace stand-in, released by the test teardown.
        self.release.wait(timeout=30.0)


class _RecordingSink:
    """Passes the payload through and returns a sentinel result (the fast path)."""

    def __init__(self) -> None:
        self.seen: object | None = None

    def append(self, **kwargs: Any) -> str:
        self.seen = kwargs["bars"]
        return "sink-result"


class TestZombiePayloadHandoff:
    def test_deadline_zombie_retains_no_guard_layer_reference(self) -> None:
        """FAILS-BEFORE: the lambda closure cell + the guard frame's ``bars``
        parameter (via the raised exception's traceback) kept the payload
        alive for the zombie's whole wedge."""
        sink = _WedgingDroppingSink()
        payload = _WeakrefableBars([object() for _ in range(8)])
        ref = weakref.ref(payload)
        try:
            with pytest.raises(WriteDeadlineExceeded):
                append_with_deadline(
                    sink,  # type: ignore[arg-type]
                    exchange="NSE",
                    segment="CASH",
                    symbol="NIFTY",
                    interval="1minute",
                    bars=payload,  # type: ignore[arg-type]
                    deadline_seconds=0.2,
                )
            # Deterministic ordering: the zombie has POPPED once `started` is
            # set (the pop happens strictly before sink.append runs).
            assert sink.started.wait(10.0), "the zombie worker never started the append"
            del payload
            gc.collect()
            assert ref() is None, (
                "the guard layer still pins the bars payload while the zombie "
                "write is wedged (closure/thread-args/traceback retention)"
            )
        finally:
            sink.release.set()

    def test_completed_write_passes_the_same_object_and_returns_result(self) -> None:
        """The pop handoff is identity-preserving on the normal path."""
        sink = _RecordingSink()
        payload = _WeakrefableBars([object()])
        result = append_with_deadline(
            sink,  # type: ignore[arg-type]
            exchange="NSE",
            segment="CASH",
            symbol="NIFTY",
            interval="1minute",
            bars=payload,  # type: ignore[arg-type]
            deadline_seconds=5.0,
        )
        assert result == "sink-result"  # type: ignore[comparison-overlap]
        assert sink.seen is payload  # the SAME object — never a copy, never rebuilt
