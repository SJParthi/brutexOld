"""Tests for `brutex.adapters.dhan.auth_guard` — the one-way "token is dead" latch.

100% line+branch: an un-tripped guard, the first-cause-wins idempotent trip, the
``tripped`` / ``cause`` accessors, and thread-safe concurrent trips (the first
writer's cause is the one retained) — DEC-DHAN-AUTH-FAILFAST-RENEW-001.
"""

from __future__ import annotations

import threading

from brutex.adapters.dhan.auth_guard import AuthGuard


class TestAuthGuard:
    def test_starts_untripped_with_no_cause(self) -> None:
        guard = AuthGuard()
        assert guard.tripped() is False
        assert guard.cause() is None

    def test_trip_sets_tripped_and_records_cause(self) -> None:
        guard = AuthGuard()
        guard.trip("DH-901 token expired")
        assert guard.tripped() is True
        assert guard.cause() == "DH-901 token expired"

    def test_trip_is_idempotent_first_cause_wins(self) -> None:
        guard = AuthGuard()
        guard.trip("first cause")
        guard.trip("second cause")  # later trip never overwrites the first
        assert guard.tripped() is True
        assert guard.cause() == "first cause"

    def test_concurrent_trips_keep_exactly_one_cause(self) -> None:
        guard = AuthGuard()
        barrier = threading.Barrier(8)

        def _hammer(i: int) -> None:
            barrier.wait()
            guard.trip(f"cause-{i}")

        threads = [threading.Thread(target=_hammer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert guard.tripped() is True
        cause = guard.cause()
        # Exactly ONE of the racing causes survived (the first writer to win the lock).
        assert cause in {f"cause-{i}" for i in range(8)}
