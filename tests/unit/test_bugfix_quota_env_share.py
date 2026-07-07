"""Regression tests — two ``DailyQuotaTracker``s in ONE process must share the env.

DEC-QUOTA-ENV-SINGLE-OPEN-001. Live-operator bug (brutex.log 2026-06-10):
``GET /api/v1/quota`` 500'd on EVERY 3 s gauge poll with
``lmdb.Error: The environment '~/.brutex/quota/quota.lmdb' is already open in
this process`` — the dashboard's lazy gauge tracker
(DEC-DASHBOARD-QUOTA-PERSISTED-001, ``app.py``) collided with the pull
controller's tracker (DEC-AUTO-RESUME-QUOTA-001, ``pull_controller.py``)
inside one uvicorn process, because LMDB forbids opening the same
environment twice per process. In the reverse construction order the FIRST
PULL would have crashed instead.

Every test in this module FAILED (raised ``lmdb.Error``) before the fix:
the process-wide ref-counted env registry in
``brutex/orchestrator/quota_tracker.py`` plus the single-write-txn
increment that makes counting linearizable ACROSS tracker instances.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from brutex.orchestrator.quota_tracker import DailyQuotaTracker

_IST = ZoneInfo("Asia/Kolkata")

#: One fixed IST instant for every tracker in this module, so persisted
#: dates always match the observed date (no midnight-crossing flake).
_NOON = datetime(2026, 6, 10, 12, 0, 0, tzinfo=_IST)


def _static_clock() -> Callable[[], datetime]:
    """A clock pinned to one IST noon."""
    return lambda: _NOON


def _tracker(path: Path, limit: int = 1_000) -> DailyQuotaTracker:
    """Build a tracker on *path* with the pinned clock."""
    return DailyQuotaTracker(daily_limit=limit, store_path=path, clock=_static_clock())


class TestSharedEnvAcrossInstances:
    """The exact in-process collision the operator hit, replayed."""

    def test_second_tracker_on_same_path_shares_the_env(self, tmp_path: Path) -> None:
        """Constructing a 2nd tracker on the SAME path must not raise.

        Pre-fix this second construction raised
        ``lmdb.Error: ... already open in this process``.
        """
        path = tmp_path / "quota.lmdb"
        pull_side = _tracker(path)
        gauge_side = _tracker(path)  # raised lmdb.Error before the fix

        assert pull_side.acquire_or_raise() == 1
        assert pull_side.acquire_or_raise() == 2
        # The read-only gauge instance sees the SAME persisted counter.
        assert gauge_side.current_count() == 2
        assert gauge_side.remaining() == 1_000 - 2

        pull_side.close()
        gauge_side.close()

    def test_concurrent_increments_across_instances_never_lose_updates(
        self, tmp_path: Path
    ) -> None:
        """Increments from TWO instances interleave without lost updates.

        The single-write-txn increment makes the get-check-put exclusive at
        the LMDB level, so cross-instance counting is linearizable — not
        merely safe under each instance's own Python lock.
        """
        path = tmp_path / "quota.lmdb"
        a = _tracker(path, limit=10_000)
        b = _tracker(path, limit=10_000)
        per_thread = 200

        def _spin(tracker: DailyQuotaTracker) -> None:
            for _ in range(per_thread):
                tracker.acquire_or_raise()

        threads = [threading.Thread(target=_spin, args=(t,)) for t in (a, b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert a.current_count() == 2 * per_thread
        assert b.current_count() == 2 * per_thread
        a.close()
        b.close()


class TestRefCountedClose:
    """close() releases a reference; only the LAST holder closes the env."""

    def test_close_releases_only_the_last_reference(self, tmp_path: Path) -> None:
        """Closing the gauge's tracker must not break the live pull's."""
        path = tmp_path / "quota.lmdb"
        pull_side = _tracker(path)
        gauge_side = _tracker(path)
        pull_side.acquire_or_raise()

        gauge_side.close()
        # The pull side keeps working — its env was NOT closed underneath it.
        assert pull_side.acquire_or_raise() == 2
        pull_side.close()

        # Last reference released → a fresh tracker re-opens the env and
        # still reads the persisted count (durability preserved).
        reopened = _tracker(path)
        assert reopened.current_count() == 2
        reopened.close()

    def test_close_is_idempotent_per_instance(self, tmp_path: Path) -> None:
        """Double-close on one instance must not steal the other's reference."""
        path = tmp_path / "quota.lmdb"
        a = _tracker(path)
        b = _tracker(path)
        a.acquire_or_raise()

        a.close()
        a.close()  # second close is a no-op, NOT a second release
        assert b.acquire_or_raise() == 2
        b.close()

    def test_distinct_paths_use_distinct_envs(self, tmp_path: Path) -> None:
        """Trackers on different paths stay fully independent."""
        a = _tracker(tmp_path / "a.lmdb")
        b = _tracker(tmp_path / "b.lmdb")
        a.acquire_or_raise()
        assert a.current_count() == 1
        assert b.current_count() == 0
        a.close()
        b.close()


class TestDashboardGaugeWithLivePull:
    """The production failure replayed end-to-end through the real app.

    CONSCIOUSLY REWRITTEN for DEC-QUOTA-VERDICT-MOOT-001: the original test
    asserted ``GET /api/v1/quota`` returned the ``quota_limit=-1`` sentinel
    without constructing a second tracker (the double-open-500 regression,
    repurposed by DEC-GROWW-NO-DAILY-QUOTA-UI-001 #718). The route is now
    DELETED, which makes the historical failure mode STRUCTURALLY impossible
    at a stronger level — there is no gauge endpoint left that could ever
    construct a tracker. This rewrite pins that: the app builds and serves
    while a pull-side tracker already holds the default-path env, and the
    removed route answers 404 without touching LMDB.
    """

    def test_removed_quota_route_never_touches_lmdb(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The app 404s the removed route while a pull tracker holds the env."""
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import DashboardState, build_app
        from fastapi.testclient import TestClient

        # Point the DEFAULT quota path (~/.brutex/quota/quota.lmdb) at a
        # tmp home, exactly as the original regression did, so the pull-side
        # tracker below opens the SAME default env a second construction
        # would have collided with.
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # The "pull controller" side: opened FIRST, counts some calls.
        pull_side = DailyQuotaTracker()
        for _ in range(3):
            pull_side.acquire_or_raise()

        # The dashboard (no quota surface at all since DEC-QUOTA-VERDICT-MOOT-001).
        app = build_app(DashboardState())
        with TestClient(app) as client:
            resp = client.get("/api/v1/quota")

        # The route is GONE — a stale poller gets a plain 404, and no code path
        # exists that could construct a second tracker, so the historical
        # double-open 500 is structurally impossible.
        assert resp.status_code == 404
        # The pull-side tracker is untouched by the app lifecycle.
        assert pull_side.current_count() == 3
        pull_side.close()
