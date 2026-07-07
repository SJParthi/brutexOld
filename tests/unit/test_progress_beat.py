"""Unit tests for the process-global liveness-beat seam.

DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15 + the MULTI-REGISTRANT registry of
DEC-GREEKS-BULK-REENRICH-001 fix round 3. :mod:`brutex.observability.progress_beat`
is the decoupled seam the bounded partition lock-wait + the rate pacer + the
per-unit scan/enrich/prebuild seams fire into so a slow-but-progressing job
keeps its watchdog heartbeat alive. 100% line+branch: register → emit →
unregister with handle-scoped removal, the multi-registrant broadcast (two
concurrent jobs — the pull + the Greeks backfill — never clobber or clear each
other's beat), the no-op-when-unregistered fast path, and the best-effort
swallow of a raising beat (never crashes the worker, never starves a sibling
beat).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from brutex.observability.progress_beat import (
    emit_progress_beat,
    register_progress_beat,
    reset_progress_beats_for_tests,
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
