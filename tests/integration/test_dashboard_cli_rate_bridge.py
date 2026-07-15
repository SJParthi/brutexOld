"""Integration tests for the cross-process CLI→dashboard rate bridge (DEC-CLI-RATE-BRIDGE-001).

End-to-end: a CLI-style pull publishes its rate to ``<lake_root>/.rate/pull_rate.json``,
and the dashboard ``/api/v1/pull/rate`` route surfaces it on the widget JSON — so a
``brutex fetch`` pull (a separate process) is VISIBLE on the dashboard live-rate widget.
Also covers: a stale bridge → idle, no bridge file → the in-memory panel fallback, a
panel fresher than the bridge keeps the dashboard numbers, and the route never 500s when
no instrument store is wired (no resolvable lake root).

DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001 additions: the route's bridge read now goes
through the injected :class:`~brutex.dashboard.rate_bridge_cache.RateBridgeCache`
(TTL+token memo, deadline-guarded, threadpool-offloaded). The new suites pin: the
SLOW-READ LATENCY BOUND (an Event-wedged reader — the route still answers bounded,
labelled ``unverified``, single-flight, and the event loop stays free); the cache-hit
spy (one read across polls within the TTL); BYTE-COMPAT (the payload key set is the
frozen pre-change set plus exactly the two additive freshness keys and the additive
``last_pull`` outcome block of DEC-OPERATOR-HEALTH-TRUTH-001, and the fresh-bridge
values match a DIRECT un-cached ``build_pull_rate_view_merged`` computation); staleness
honesty under the memo (a stale/frozen snapshot ages from its own ``wall_time`` — never
a fabricated "flowing"); deleted-file / permission-denied / unwired degrades; and the
GUARD-SPAWN-FAILURE fold (a guard raising other than ``DeadlineExceeded`` — thread
exhaustion — answers 200 + ``unverified`` and the NEXT poll recovers: never a 500,
never a permanent wedge).
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.app import DashboardState, build_app, make_rate_observer
from brutex.dashboard.rate_bridge_cache import RateBridgeCache
from brutex.data.rate_bridge import (
    RateBridgeSnapshot,
    publish_rate_snapshot,
    rate_bridge_path,
    read_rate_snapshot,
)

#: The FROZEN pre-change ``/api/v1/pull/rate`` payload key set (the byte-compat pin) —
#: a literal list, deliberately NOT derived from ``PullRateView`` (a field added there
#: must consciously extend this pin).
_PRE_CHANGE_KEYS = frozenset(
    {
        "active",
        "ceiling_per_min",
        "ceiling_rps",
        "flow_detail",
        "flow_state",
        "recent_429_count",
        "req_per_10min",
        "req_per_hour_measured",
        "req_per_hour_observed",
        "req_per_min",
        "req_per_min_rate",
        "req_per_sec_achieved",
        "req_per_sec_allowed",
        "req_per_sec_count",
        "req_per_sec_failed",
        "seconds_since_update",
        "stalled",
    }
)

#: The two ADDITIVE honest-freshness keys (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001).
_ADDITIVE_KEYS = frozenset({"bridge_source", "bridge_probe_age_s"})

#: The ADDITIVE honest last-pull OUTCOME block (DEC-OPERATOR-HEALTH-TRUTH-001,
#: MIGRATION.md #1747) — the conscious extension this pin exists to force: the
#: operator heartbeat joins it so a HALTED pull never renders the clean green.
_LAST_PULL_KEYS = frozenset({"last_pull"})


class _FakeStore:
    """A minimal instrument-store stand-in exposing only ``.path`` (its parent = lake root)."""

    def __init__(self, lake_root: Path) -> None:
        self.path = lake_root / "instruments.duckdb"


def _client(
    state: DashboardState,
    instrument_store: Any = None,
    *,
    pull_active: bool | None = None,
    rate_bridge_cache: RateBridgeCache | None = None,
) -> Any:
    """``pull_active`` pins the stall gate's pull-run probe (DEC-RATE-STALL-PULL-GATE-001):
    ``True`` simulates a mid-flight DASHBOARD pull; ``None`` keeps the production
    default (the fresh app's job is not running). ``rate_bridge_cache`` injects the
    bridge-read memo (``None`` = the production default cache)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    probe = None if pull_active is None else (lambda: pull_active)
    return TestClient(
        build_app(
            state,
            instrument_store=instrument_store,
            pull_active_probe=probe,
            rate_bridge_cache=rate_bridge_cache,
        )
    )


def _publish_now(lake_root: Path, *, effective_rps: float, current_rps: float) -> None:
    """Publish a FRESH bridge snapshot (wall_time == now) into *lake_root*."""
    publish_rate_snapshot(
        lake_root,
        effective_rps=effective_rps,
        current_rps=current_rps,
        recent_minute=290,
        recent_429=3,
        ceiling_per_min=500,
        wall_time=time.time(),
        monotonic=time.monotonic(),
    )


def test_route_surfaces_fresh_cli_bridge_when_panel_idle(tmp_path: Path) -> None:
    """A fresh CLI bridge file + an idle dashboard panel → the widget shows the CLI rate."""
    _publish_now(tmp_path, effective_rps=4.8, current_rps=5.0)
    state = DashboardState()  # dashboard never pulled (panel idle)
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "flowing"
    assert body["active"] is True
    assert body["req_per_sec_achieved"] == 4.8
    assert body["req_per_sec_allowed"] == 5.0
    assert body["req_per_min"] == 290
    assert body["recent_429_count"] == 3


def test_route_ignores_stale_cli_bridge(tmp_path: Path) -> None:
    """A CLI bridge older than the staleness window → honest IDLE, never a stale live number."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=4.8,
        current_rps=5.0,
        recent_minute=290,
        recent_429=3,
        ceiling_per_min=500,
        wall_time=time.time() - 3600.0,  # an hour ago
        monotonic=time.monotonic(),
    )
    state = DashboardState()
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "idle"
    assert body["req_per_sec_achieved"] == 0.0
    assert body["req_per_min"] == 0


def test_route_no_bridge_file_uses_in_memory_panel(tmp_path: Path) -> None:
    """No CLI bridge file present → the dashboard's own in-memory panel is projected."""
    state = DashboardState()
    make_rate_observer(state)(
        {"current_rps": 8.0, "recent_hits": 1, "recent_minute": 400, "ceiling_per_min": 500},
        {"effective_rps": 7.0, "latency_ms_p95": 150.0},
    )
    with _client(state, _FakeStore(tmp_path), pull_active=True) as c:
        body = c.get("/api/v1/pull/rate").json()
    # No bridge → the dashboard pull's numbers win.
    assert body["flow_state"] == "flowing"
    assert body["req_per_sec_achieved"] == 7.0
    assert body["req_per_min"] == 400


def test_route_panel_fresher_than_bridge_keeps_dashboard_numbers(tmp_path: Path) -> None:
    """A just-ticked dashboard panel beats an older CLI bridge (the fresher source wins)."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=4.8,
        current_rps=5.0,
        recent_minute=290,
        recent_429=3,
        ceiling_per_min=500,
        wall_time=time.time() - 8.0,  # CLI ticked 8 s ago (still within the 12 s window)
        monotonic=time.monotonic(),
    )
    state = DashboardState()
    make_rate_observer(state)(
        {"current_rps": 8.0, "recent_hits": 0, "recent_minute": 410, "ceiling_per_min": 500},
        {"effective_rps": 7.5, "latency_ms_p95": 140.0},
    )  # dashboard ticked just now → fresher than the 8 s-old bridge
    with _client(state, _FakeStore(tmp_path), pull_active=True) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["req_per_sec_achieved"] == 7.5
    assert body["req_per_min"] == 410


def test_route_no_store_wired_falls_back_to_panel(tmp_path: Path) -> None:
    """No instrument store (no resolvable lake) → the route never 500s, projects the panel."""
    state = DashboardState()  # idle panel, no store
    with _client(state, instrument_store=None) as c:
        resp = c.get("/api/v1/pull/rate")
    assert resp.status_code == 200
    assert resp.json()["flow_state"] == "idle"


def test_route_corrupt_bridge_file_falls_back_to_panel(tmp_path: Path) -> None:
    """A corrupt bridge file → ignored (read returns None), the panel is projected (no 500)."""
    bridge = tmp_path / ".rate" / "pull_rate.json"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text("{garbage", encoding="utf-8")
    state = DashboardState()
    make_rate_observer(state)(
        {"current_rps": 8.0, "recent_hits": 0, "recent_minute": 333, "ceiling_per_min": 500},
        {"effective_rps": 6.0, "latency_ms_p95": 100.0},
    )
    with _client(state, _FakeStore(tmp_path), pull_active=True) as c:
        resp = c.get("/api/v1/pull/rate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["req_per_sec_achieved"] == 6.0  # panel, not the corrupt bridge
    assert body["req_per_min"] == 333


def test_route_bridge_pull_inactive_reads_done_not_stalled(tmp_path: Path) -> None:
    """DEC-CLI-BRIDGE-PULL-ACTIVE-001: a FRESH bridge with pull_active=False reads "done".

    Before schema v3 a just-finished CLI pull's fresh-but-zero final tick could read as a
    red "stalled"; the authoritative False flag now gates the verdict exactly like a
    dashboard pull's ``BackgroundPullJob.running`` probe.
    """
    publish_rate_snapshot(
        tmp_path,
        effective_rps=0.0,  # the pull just ended — nothing is completing any more
        current_rps=5.0,
        recent_minute=0,
        recent_429=0,
        ceiling_per_min=500,
        wall_time=time.time(),
        monotonic=time.monotonic(),
        pull_active=False,
    )
    state = DashboardState()
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "done"
    assert body["stalled"] is False
    assert body["active"] is False


def test_route_bridge_pull_active_true_zero_rate_reads_stalled(tmp_path: Path) -> None:
    """The True flag keeps the stall teeth: a running CLI pull at 0 achieved reads stalled."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=0.0,
        current_rps=5.0,
        recent_minute=0,
        recent_429=0,
        ceiling_per_min=500,
        wall_time=time.time(),
        monotonic=time.monotonic(),
        pull_active=True,
    )
    state = DashboardState()
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "stalled"
    assert body["stalled"] is True


def test_dead_cli_bridge_decays_to_idle_at_route(tmp_path: Path) -> None:
    """DEC-RATE-METER-LIFECYCLE-001 (audit C3): a DEAD CLI's stuck ``pull_active=True``
    file decays to an honest idle at the REAL route — never a PERMANENT red.

    A SIGKILLed ``brutex fetch`` leaves its last per-window ``pull_active=True`` stamp
    on disk forever (the in-process ``finally`` that flips it False never ran). On a
    freshly-restarted never-pulled dashboard (panel age = inf) any finite bridge age
    used to win the freshness pick FOREVER → a permanent site-wide red "stalled". The
    ``BRIDGE_MAX_AGE_S`` TTL treats the 2-day-old file as DEAD EVIDENCE ≡ absent.

    MUTANT KILLED: the TTL bypassed at the route/merged-projection boundary.
    """
    publish_rate_snapshot(
        tmp_path,
        effective_rps=4.8,
        current_rps=5.0,
        recent_minute=290,
        recent_429=0,
        ceiling_per_min=500,
        wall_time=time.time() - 172_800.0,  # the CLI died 2 days ago
        monotonic=time.monotonic(),
        pull_active=True,  # the stuck stamp
    )
    state = DashboardState()  # never-ticked panel (freshly-restarted dashboard)
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "idle"
    assert body["stalled"] is False
    assert body["active"] is False


def test_recently_dead_cli_bridge_still_reads_stalled_at_route(tmp_path: Path) -> None:
    """The 12 s < age ≤ 900 s ALERT WINDOW is preserved end to end: a CLI that died
    60 s ago (within the TTL) still reads the honest red "stalled" — the pull died
    mid-run and the operator should see it."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=4.8,
        current_rps=5.0,
        recent_minute=290,
        recent_429=0,
        ceiling_per_min=500,
        wall_time=time.time() - 60.0,  # died a minute ago — inside the alert window
        monotonic=time.monotonic(),
        pull_active=True,
    )
    state = DashboardState()
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "stalled"
    assert body["stalled"] is True


def _bridge_snap(*, wall_time: float, effective_rps: float = 4.8) -> RateBridgeSnapshot:
    """A schema-v3 snapshot for injected readers (values mirror ``_publish_now``)."""
    return RateBridgeSnapshot(
        schema=3,
        effective_rps=effective_rps,
        current_rps=5.0,
        recent_minute=290,
        recent_429=3,
        ceiling_per_min=500,
        recent_1s=4,
        recent_600s=100,
        recent_3600s=400,
        wall_time=wall_time,
        monotonic=0.0,
        pull_active=None,
    )


class TestSlowReadLatencyBound:
    """DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001 — THE required slow-read pin.

    Without the fix the route performed the bridge read inline on the event
    loop: with a wedged (Event-blocked) reader the FIRST request would hang
    until the Event's 10 s safety timeout and blow the wall budget — verified
    by reverting during development. With the fix EVERY request is bounded.
    """

    def test_wedged_bridge_read_never_wedges_the_route(self, tmp_path: Path) -> None:
        """A wedged lake volume: every request bounded, labelled, single-flight."""
        release = threading.Event()
        reads: list[Path] = []

        def wedged_reader(root: Path) -> RateBridgeSnapshot | None:
            reads.append(root)
            release.wait(10.0)  # a wedged external SSD: blocks far past the deadline
            return _bridge_snap(wall_time=time.time())

        cache = RateBridgeCache(
            ttl_s=0.0, read_deadline_s=0.1, probe=lambda _root: "tok", reader=wedged_reader
        )
        state = DashboardState()
        try:
            with _client(state, _FakeStore(tmp_path), rate_bridge_cache=cache) as c:
                # The OWNER request (first poll of the outage) is bounded by the
                # deadline graft — the route answers with the panel + "unverified".
                t0 = time.perf_counter()
                resp = c.get("/api/v1/pull/rate")
                owner_elapsed = time.perf_counter() - t0
                assert resp.status_code == 200
                assert owner_elapsed < 2.0  # generous CI budget; the pin is bounded-vs-hang
                body = resp.json()
                assert body["bridge_source"] == "unverified"
                assert body["bridge_probe_age_s"] is None  # cold — nothing ever completed
                assert set(body) == _PRE_CHANGE_KEYS | _ADDITIVE_KEYS | _LAST_PULL_KEYS
                assert body["flow_state"] == "idle"  # panel-only — never fabricated
                # A FOLLOWER while still wedged: immediate, and NO second read
                # was started (single-flight keyed on daemon completion).
                t0 = time.perf_counter()
                resp2 = c.get("/api/v1/pull/rate")
                assert time.perf_counter() - t0 < 1.0
                assert resp2.json()["bridge_source"] == "unverified"
                assert len(reads) == 1
                # The event loop is FREE: an unrelated route answers inside the bound.
                t0 = time.perf_counter()
                assert c.get("/api/v1/rate").status_code == 200
                assert time.perf_counter() - t0 < 1.0
                # RECOVERY: release the wedge — the abandoned daemon adopts its
                # late result (last-write-wins) and polls converge to live.
                release.set()
                deadline = time.monotonic() + 5.0
                body = c.get("/api/v1/pull/rate").json()
                while time.monotonic() < deadline and body["bridge_source"] != "live":
                    time.sleep(0.02)
                    body = c.get("/api/v1/pull/rate").json()
                assert body["bridge_source"] == "live"
                assert body["req_per_sec_achieved"] == 4.8
                assert body["flow_state"] == "flowing"
                assert len(reads) == 1  # recovery revalidated by token — no re-read
        finally:
            release.set()


class TestGuardSpawnFailureNever500s:
    """Hostile-review MAJOR fold: a guard that cannot spawn its daemon must not 500 or wedge.

    The production guard's one non-``DeadlineExceeded`` raise path is
    ``threading.Thread(...).start()`` failing under thread/memory exhaustion.
    Pre-fix that raise propagated through ``run_in_threadpool`` (a 500 on the
    poll route) AND permanently latched the cache's single-flight flag (every
    later poll served ``unverified`` with zero refreshes until restart).
    """

    def test_raising_guard_route_stays_200_and_recovers_next_poll(self, tmp_path: Path) -> None:
        """The failing poll answers 200 + ``unverified``; the next poll goes live."""
        _publish_now(tmp_path, effective_rps=4.8, current_rps=5.0)

        calls = {"n": 0}

        def flaky_guard(fn: Callable[[], None], _seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("can't start new thread")
            fn()  # inline refresh — the real probe + reader run on the caller

        cache = RateBridgeCache(ttl_s=0.0, read_deadline_s=0.5, guard=flaky_guard)
        with _client(DashboardState(), _FakeStore(tmp_path), rate_bridge_cache=cache) as c:
            resp = c.get("/api/v1/pull/rate")
            assert resp.status_code == 200  # pre-fix: a 500 through run_in_threadpool
            body = resp.json()
            assert body["bridge_source"] == "unverified"
            assert body["bridge_probe_age_s"] is None  # cold — nothing ever completed
            assert set(body) == _PRE_CHANGE_KEYS | _ADDITIVE_KEYS | _LAST_PULL_KEYS
            # The flag was released: the NEXT poll re-spawns the refresh and
            # surfaces the fresh CLI bridge (pre-fix: 'unverified' forever).
            resp2 = c.get("/api/v1/pull/rate")
            assert resp2.status_code == 200
            body2 = resp2.json()
            assert body2["bridge_source"] == "live"
            assert body2["req_per_sec_achieved"] == 4.8
            assert body2["flow_state"] == "flowing"
        assert calls["n"] == 2  # exactly one failed spawn + one successful retry


class TestCacheHitSpy:
    """The zero-syscall TTL hit end-to-end through the route."""

    def test_repeat_poll_within_ttl_reads_bridge_once(self, tmp_path: Path) -> None:
        """Two polls inside one TTL: the reader runs ONCE; stable fields identical."""
        _publish_now(tmp_path, effective_rps=4.8, current_rps=5.0)
        reads: list[Path] = []

        def counting_reader(root: Path) -> RateBridgeSnapshot | None:
            reads.append(root)
            return read_rate_snapshot(root)

        cache = RateBridgeCache(ttl_s=60.0, read_deadline_s=5.0, reader=counting_reader)
        with _client(DashboardState(), _FakeStore(tmp_path), rate_bridge_cache=cache) as c:
            first = c.get("/api/v1/pull/rate").json()
            second = c.get("/api/v1/pull/rate").json()
        assert len(reads) == 1  # the second poll was a pure in-memory hit
        assert first["bridge_source"] == "live"
        assert first["bridge_probe_age_s"] == 0.0
        assert second["bridge_source"] == "cached"
        assert second["bridge_probe_age_s"] is not None
        assert 0.0 <= second["bridge_probe_age_s"] <= 60.0
        for key in sorted(_PRE_CHANGE_KEYS - {"seconds_since_update"}):
            assert first[key] == second[key], key
        # The staleness gauge derives from the snapshot's own wall_time and may
        # tick between the polls — equal within a generous tolerance.
        assert abs(first["seconds_since_update"] - second["seconds_since_update"]) < 5.0


class TestByteCompatPin:
    """Additive-only payload contract: the frozen pre-change keys + the additive sets."""

    def test_payload_key_set_across_degrade_scenarios(self, tmp_path: Path) -> None:
        """no-store / fresh / stale / corrupt / absent all carry the same key set."""
        expected = _PRE_CHANGE_KEYS | _ADDITIVE_KEYS | _LAST_PULL_KEYS
        # no store wired
        with _client(DashboardState(), instrument_store=None) as c:
            body = c.get("/api/v1/pull/rate").json()
        assert set(body) == expected
        assert body["bridge_source"] == "unwired"
        # fresh bridge
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        _publish_now(fresh, effective_rps=4.8, current_rps=5.0)
        with _client(DashboardState(), _FakeStore(fresh)) as c:
            assert set(c.get("/api/v1/pull/rate").json()) == expected
        # stale bridge (an hour old)
        stale = tmp_path / "stale"
        stale.mkdir()
        publish_rate_snapshot(
            stale,
            effective_rps=4.8,
            current_rps=5.0,
            recent_minute=290,
            recent_429=3,
            ceiling_per_min=500,
            wall_time=time.time() - 3600.0,
            monotonic=time.monotonic(),
        )
        with _client(DashboardState(), _FakeStore(stale)) as c:
            assert set(c.get("/api/v1/pull/rate").json()) == expected
        # corrupt bridge file
        corrupt = tmp_path / "corrupt"
        bridge = rate_bridge_path(corrupt)
        bridge.parent.mkdir(parents=True, exist_ok=True)
        bridge.write_text("{garbage", encoding="utf-8")
        with _client(DashboardState(), _FakeStore(corrupt)) as c:
            body = c.get("/api/v1/pull/rate").json()
        assert set(body) == expected
        assert body["bridge_source"] == "live"  # a COMPLETED honest degrade
        # absent bridge file (no CLI pull has ever run)
        absent = tmp_path / "absent"
        absent.mkdir()
        with _client(DashboardState(), _FakeStore(absent)) as c:
            assert set(c.get("/api/v1/pull/rate").json()) == expected

    def test_fresh_bridge_values_match_direct_uncached_computation(self, tmp_path: Path) -> None:
        """Every pre-existing key equals a DIRECT un-cached merge computation."""
        from brutex.dashboard.pull_rate_view import build_pull_rate_view_merged

        _publish_now(tmp_path, effective_rps=4.8, current_rps=5.0)
        snapshot = read_rate_snapshot(tmp_path)
        assert snapshot is not None
        with _client(DashboardState(), _FakeStore(tmp_path)) as c:
            body = c.get("/api/v1/pull/rate").json()
        direct = build_pull_rate_view_merged(
            panel_effective_rps=0.0,
            panel_current_rps=0.0,
            panel_recent_minute=0,
            panel_recent_429_count=0,
            panel_seconds_since_update=math.inf,  # the fresh app's panel never ticked
            panel_recent_1s=0,
            panel_recent_600s=0,
            panel_recent_3600s=0,
            panel_meter_present=True,
            panel_failed_rps=0.0,
            panel_pull_active=False,  # the production probe: the job is not running
            panel_phase="",  # the production probe: no pipeline phase in flight
            bridge_effective_rps=snapshot.effective_rps,
            bridge_current_rps=snapshot.current_rps,
            bridge_recent_minute=snapshot.recent_minute,
            bridge_recent_429_count=snapshot.recent_429,
            bridge_seconds_since_update=max(0.0, time.time() - snapshot.wall_time),
            bridge_recent_1s=snapshot.recent_1s,
            bridge_recent_600s=snapshot.recent_600s,
            bridge_recent_3600s=snapshot.recent_3600s,
            bridge_pull_active=snapshot.pull_active,
        ).to_dict()
        for key in sorted(_PRE_CHANGE_KEYS - {"seconds_since_update"}):
            assert body[key] == direct[key], key
        assert abs(body["seconds_since_update"] - direct["seconds_since_update"]) < 5.0


class TestStalenessHonestyUnderMemo:
    """A memoised snapshot AGES from its own wall_time — never a stale illusion."""

    def test_stale_snapshot_served_from_memo_reads_idle_never_flowing(self, tmp_path: Path) -> None:
        """A stale (1 h old) snapshot reads idle when live-read AND when the
        next refresh WEDGES — the frozen memo can never fake a "flowing"."""
        stale = _bridge_snap(wall_time=time.time() - 3600.0)
        release = threading.Event()
        calls = {"n": 0}

        def reader(_root: Path) -> RateBridgeSnapshot | None:
            calls["n"] += 1
            if calls["n"] >= 2:
                release.wait(10.0)  # the volume wedges AFTER the first good read
            return stale

        tokens = {"n": 0}

        def probe(_root: Path) -> str | None:
            tokens["n"] += 1
            return f"t{tokens['n']}"  # every refresh re-reads (a moving token)

        cache = RateBridgeCache(ttl_s=0.0, read_deadline_s=0.1, probe=probe, reader=reader)
        try:
            with _client(DashboardState(), _FakeStore(tmp_path), rate_bridge_cache=cache) as c:
                first = c.get("/api/v1/pull/rate").json()
                assert first["bridge_source"] == "live"
                assert first["flow_state"] == "idle"  # stale → idle, per STALE_AFTER_S
                assert first["req_per_sec_achieved"] == 0.0
                second = c.get("/api/v1/pull/rate").json()  # owner: wedged refresh
                assert second["bridge_source"] == "unverified"
                assert second["flow_state"] == "idle"  # the memo ages honestly
                assert second["req_per_sec_achieved"] == 0.0
        finally:
            release.set()


class TestDegradePaths:
    """Deleted / unreadable / unwired degrades stay 200 + honest."""

    def test_first_poll_after_publish_is_live_with_default_cache(self, tmp_path: Path) -> None:
        """NO cold-start regression: a cold app's FIRST poll serves the bridge live."""
        _publish_now(tmp_path, effective_rps=4.8, current_rps=5.0)
        with _client(DashboardState(), _FakeStore(tmp_path)) as c:
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "flowing"
        assert body["req_per_sec_achieved"] == 4.8
        assert body["bridge_source"] == "live"
        assert body["bridge_probe_age_s"] == 0.0

    def test_deleted_bridge_file_degrades_to_panel_on_next_revalidation(
        self, tmp_path: Path
    ) -> None:
        """Publish → poll → DELETE: the next revalidation reads panel-only, never
        a stale serve of the deleted bridge (ABSENT token moves → one re-read)."""
        _publish_now(tmp_path, effective_rps=4.8, current_rps=5.0)
        cache = RateBridgeCache(ttl_s=0.0, read_deadline_s=5.0)  # real probe + reader
        with _client(DashboardState(), _FakeStore(tmp_path), rate_bridge_cache=cache) as c:
            first = c.get("/api/v1/pull/rate").json()
            assert first["flow_state"] == "flowing"
            rate_bridge_path(tmp_path).unlink()
            second = c.get("/api/v1/pull/rate").json()
        assert second["flow_state"] == "idle"  # honest absence = panel-only
        assert second["req_per_sec_achieved"] == 0.0
        assert second["bridge_source"] == "live"  # a COMPLETED honest answer

    def test_unreadable_bridge_file_degrades_to_panel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permission-denied read (EACCES) → the reader's own OSError swallow
        yields None → panel-only 200, never a 500."""
        _publish_now(tmp_path, effective_rps=4.8, current_rps=5.0)
        state = DashboardState()
        make_rate_observer(state)(
            {"current_rps": 8.0, "recent_hits": 0, "recent_minute": 333, "ceiling_per_min": 500},
            {"effective_rps": 7.0, "latency_ms_p95": 100.0},
        )
        cache = RateBridgeCache(ttl_s=0.0, read_deadline_s=5.0)  # real probe + reader

        def _deny(_self: Path) -> bytes:
            raise PermissionError(13, "denied")

        with _client(state, _FakeStore(tmp_path), pull_active=True, rate_bridge_cache=cache) as c:
            monkeypatch.setattr(Path, "read_bytes", _deny)
            resp = c.get("/api/v1/pull/rate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["req_per_sec_achieved"] == 7.0  # the panel, not the unreadable bridge
        assert body["bridge_source"] == "live"  # the refresh attempt COMPLETED

    def test_unwired_store_reports_unwired_and_skips_the_cache(self) -> None:
        """No instrument store: honest ``unwired`` provenance + ZERO cache work."""

        class _MustNotCallCache(RateBridgeCache):
            def get(self, lake_root: Path) -> Any:
                raise AssertionError("the unwired route must not touch the bridge cache")

        with _client(
            DashboardState(), instrument_store=None, rate_bridge_cache=_MustNotCallCache()
        ) as c:
            body = c.get("/api/v1/pull/rate").json()
        assert body["bridge_source"] == "unwired"
        assert body["bridge_probe_age_s"] is None
        assert body["flow_state"] == "idle"
