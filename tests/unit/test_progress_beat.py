"""Unit tests for the process-global liveness-beat seam.

DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15 + the MULTI-REGISTRANT registry of
DEC-GREEKS-BULK-REENRICH-001 fix round 3 + the JOB-SCOPED beat routing of fix
round 4. :mod:`brutex.observability.progress_beat`
is the decoupled seam the bounded partition lock-wait + the rate pacer + the
per-unit scan/enrich/prebuild seams fire into so a slow-but-progressing job
keeps its watchdog heartbeat alive. 100% line+branch: register → emit →
unregister with handle-scoped removal, the multi-registrant registry (two
concurrent jobs — the pull + the Greeks backfill — never clobber or clear each
other's beat), the fix-round-4 SCOPED routing (a job's own-thread beats reach
ONLY its own watchdog — a concurrent backfill scan can no longer mask a
genuinely-wedged pull's HUNG edge — while unscoped fan-out threads keep the
fail-open broadcast), the no-op-when-unregistered fast path, and the
best-effort swallow of a raising beat (never crashes the worker, never starves
a sibling beat).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from brutex.observability.progress_beat import (
    bind_progress_beat_scope,
    emit_progress_beat,
    register_progress_beat,
    reset_progress_beats_for_tests,
    unbind_progress_beat_scope,
    unregister_progress_beat,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Each test starts + ends with an EMPTY registry (no cross-test bleed)."""
    reset_progress_beats_for_tests()
    yield
    reset_progress_beats_for_tests()


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
    handle = register_progress_beat(lambda: beats.append(1))
    emit_progress_beat()
    unregister_progress_beat(handle)
    emit_progress_beat()  # no longer fires
    assert beats == [1]


def test_unregister_is_idempotent() -> None:
    beats: list[int] = []
    handle = register_progress_beat(lambda: beats.append(1))
    unregister_progress_beat(handle)
    unregister_progress_beat(handle)  # no error on an already-removed handle
    emit_progress_beat()
    assert beats == []


def test_two_registrants_both_receive_the_beat() -> None:
    """The multi-registrant broadcast (fix round 3): a second concurrent job's

    register must NOT clobber the first's beat — under the old single slot the
    dry-run Greeks backfill's register stole the running pull's slot, so every
    seam beat fed the WRONG watchdog and a healthy pull false-tripped the
    CRITICAL "pull halted" alert (refuter-confirmed).
    """
    a: list[int] = []
    b: list[int] = []
    register_progress_beat(lambda: a.append(1))
    register_progress_beat(lambda: b.append(1))
    emit_progress_beat()
    assert a == [1]
    assert b == [1]


def test_unregister_removes_only_its_own_handle() -> None:
    """A finishing job clears EXACTLY its own registration (fix round 3): under

    the old single slot whichever job finished FIRST zeroed the slot for the
    still-running sibling, stripping its only defence-in-depth liveness source
    for the rest of a multi-hour run (refuter-confirmed).
    """
    survivor: list[int] = []
    finisher: list[int] = []
    survivor_handle = register_progress_beat(lambda: survivor.append(1))
    finisher_handle = register_progress_beat(lambda: finisher.append(1))
    unregister_progress_beat(finisher_handle)  # the first job finishes
    emit_progress_beat()
    assert survivor == [1]  # the still-running job's beat SURVIVES
    assert finisher == []
    unregister_progress_beat(survivor_handle)
    emit_progress_beat()
    assert survivor == [1]


def test_same_callable_registered_twice_yields_independent_handles() -> None:
    """Handles are identity tokens: the SAME beat callable registered twice is

    two registrations — removing one leaves the other firing (a job restart
    reusing one bound method can never be confused with its prior run).
    """
    beats: list[int] = []

    def _beat() -> None:
        beats.append(1)

    first = register_progress_beat(_beat)
    second = register_progress_beat(_beat)
    assert first is not second
    emit_progress_beat()
    assert beats == [1, 1]  # both registrations fired
    unregister_progress_beat(first)
    emit_progress_beat()
    assert beats == [1, 1, 1]  # the second registration still fires
    unregister_progress_beat(second)
    emit_progress_beat()
    assert beats == [1, 1, 1]


def test_raising_beat_is_swallowed_and_never_starves_a_sibling() -> None:
    """A buggy beat can never crash the worker NOR block a sibling job's beat."""
    calls: list[int] = []
    sibling: list[int] = []

    def _boom() -> None:
        calls.append(1)
        raise RuntimeError("buggy beat")

    register_progress_beat(_boom)
    register_progress_beat(lambda: sibling.append(1))
    emit_progress_beat()  # the RuntimeError is swallowed + logged, not raised
    assert calls == [1]
    assert sibling == [1]  # the sibling beat fired despite the raiser


def test_reset_for_tests_clears_every_registration() -> None:
    """The test-only isolation seam empties the whole registry in one call."""
    beats: list[int] = []
    register_progress_beat(lambda: beats.append(1))
    register_progress_beat(lambda: beats.append(2))
    reset_progress_beats_for_tests()
    emit_progress_beat()
    assert beats == []


def test_scoped_emit_delivers_only_to_its_own_registration() -> None:
    """Fix round 4 (refuter-confirmed MAJOR): a job's OWN-thread beats route to

    its OWN watchdog only. Under the round-3 broadcast a concurrent dry-run
    backfill's per-contract/per-file scan beats kept a genuinely-WEDGED live
    pull's watchdog HEALTHY for the whole ~30 min scan — suppressing the HUNG
    edge and with it the CRITICAL alert AND the terminal WEDGED verdict (which
    is gated ON the HUNG edge, so the wedge dual signal never ran).
    """
    pull: list[int] = []
    backfill: list[int] = []
    register_progress_beat(lambda: pull.append(1))
    backfill_handle = register_progress_beat(lambda: backfill.append(1))
    token = bind_progress_beat_scope(backfill_handle)
    try:
        for _ in range(5):  # the backfill scan's per-file beats
            emit_progress_beat()
    finally:
        unbind_progress_beat_scope(token)
    assert backfill == [1] * 5  # the emitter's own watchdog stays fed
    assert pull == []  # the wedged sibling is NOT masked — fails on broadcast


def test_scoped_emit_after_unregister_reaches_nobody() -> None:
    """A finished run's dying-breath beats must not feed siblings either."""
    survivor: list[int] = []
    register_progress_beat(lambda: survivor.append(1))
    finished_handle = register_progress_beat(lambda: None)
    unregister_progress_beat(finished_handle)
    token = bind_progress_beat_scope(finished_handle)
    try:
        emit_progress_beat()
    finally:
        unbind_progress_beat_scope(token)
    assert survivor == []


def test_unscoped_emit_still_broadcasts_to_every_registrant() -> None:
    """Fan-out worker threads carry no scope — the fail-open broadcast stays

    (losing a beat to its own watchdog would resurrect the round-3
    false-CRITICAL class; the residual sibling-masking of the UNscoped
    direction is the module's documented honest envelope).
    """
    a: list[int] = []
    b: list[int] = []
    register_progress_beat(lambda: a.append(1))
    register_progress_beat(lambda: b.append(1))
    emit_progress_beat()  # no scope bound on this thread
    assert a == [1] and b == [1]


def test_scope_token_restore_is_nesting_safe() -> None:
    """An inline-spawned nested job hands the thread back to the OUTER scope."""
    outer: list[int] = []
    inner: list[int] = []
    outer_handle = register_progress_beat(lambda: outer.append(1))
    inner_handle = register_progress_beat(lambda: inner.append(1))
    outer_token = bind_progress_beat_scope(outer_handle)
    try:
        emit_progress_beat()
        inner_token = bind_progress_beat_scope(inner_handle)
        try:
            emit_progress_beat()
        finally:
            unbind_progress_beat_scope(inner_token)
        emit_progress_beat()  # back on the OUTER job's scope
    finally:
        unbind_progress_beat_scope(outer_token)
    assert outer == [1, 1]
    assert inner == [1]
    emit_progress_beat()  # unscoped again → broadcast
    assert outer == [1, 1, 1] and inner == [1, 1]
