"""Unit tests for ``brutex.dashboard.rate_bridge_cache`` (DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001).

100% line + branch on the non-blocking, deadline-guarded bridge-read memo:

* the zero-syscall TTL hit (probe/reader called ONCE across polls — must-not-call pins);
* the post-TTL stat revalidation (token unchanged → NO re-read) and every
  content-token invalidation permutation (moved token / ABSENT steady state /
  ``None`` "unknown — if uncertain, fetch");
* the SLOW-READ latency bound through the REAL ``run_with_deadline`` guard
  (an Event-wedged reader returns bounded, served ``unverified``, exactly ONE
  abandoned daemon per outage, late adoption self-heals, threads leak-free);
* non-blocking single-flight followers + an N-thread barrier race;
* once-per-outage degraded WARN + recovered INFO;
* the fail-loud env overrides, the TTL < ``STALE_AFTER_S`` invariant pin, the
  additive-only :func:`stamp_bridge_freshness`, and the late-binding reader seam.

All deterministic: injected clock / probe / reader / guard seams; thread tests
coordinate on :class:`threading.Event` released in ``finally`` (never a raced
sleep — bounded waits only).
"""

# Whitebox: the lock-hygiene + ctor-resolution pins read module-private state
# deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from brutex.dashboard.lake_revision import ABSENT_TOKEN
from brutex.dashboard.pull_rate_view import STALE_AFTER_S
from brutex.dashboard.rate_bridge_cache import (
    BRIDGE_SOURCE_CACHED,
    BRIDGE_SOURCE_LIVE,
    BRIDGE_SOURCE_UNVERIFIED,
    BRIDGE_SOURCE_UNWIRED,
    RATE_BRIDGE_CACHE_TTL_S,
    RATE_BRIDGE_READ_DEADLINE_ENV,
    RATE_BRIDGE_READ_DEADLINE_S,
    RATE_BRIDGE_TTL_ENV,
    BridgeReadResult,
    RateBridgeCache,
    resolve_rate_bridge_read_deadline_s,
    resolve_rate_bridge_ttl_s,
    stamp_bridge_freshness,
)
from brutex.data.rate_bridge import RateBridgeSnapshot
from structlog.testing import capture_logs

_LAKE = Path("/nonexistent-lake-root")


def _snap(*, wall_time: float = 1_000.0, effective_rps: float = 4.5) -> RateBridgeSnapshot:
    """A minimal valid schema-v3 snapshot (values are opaque to the cache)."""
    return RateBridgeSnapshot(
        schema=3,
        effective_rps=effective_rps,
        current_rps=5.0,
        recent_minute=290,
        recent_429=1,
        ceiling_per_min=500,
        recent_1s=4,
        recent_600s=100,
        recent_3600s=400,
        wall_time=wall_time,
        monotonic=0.0,
        pull_active=True,
    )


class _Clock:
    """Injected monotonic clock — fully deterministic, steppable both ways."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Step the clock (negative = a backwards skew, the clamp fixture)."""
        self.now += seconds


class _Probe:
    """Counting probe returning a scripted token sequence (last one repeats)."""

    def __init__(self, tokens: list[str | None]) -> None:
        self.tokens = tokens
        self.calls = 0

    def __call__(self, lake_root: Path) -> str | None:
        self.calls += 1
        return self.tokens[min(self.calls - 1, len(self.tokens) - 1)]


class _Reader:
    """Counting reader returning a scripted snapshot sequence (last one repeats)."""

    def __init__(self, snapshots: list[RateBridgeSnapshot | None]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    def __call__(self, lake_root: Path) -> RateBridgeSnapshot | None:
        self.calls += 1
        return self.snapshots[min(self.calls - 1, len(self.snapshots) - 1)]


def _inline_guard(fn: Callable[[], None], _seconds: float) -> None:
    """Thread-free guard seam: run the refresh synchronously on the caller."""
    fn()


class TestEnvResolvers:
    """Fail-loud env override parsing for both tunables."""

    def test_ttl_default_when_unset(self) -> None:
        """No env var → the shipped default."""
        assert resolve_rate_bridge_ttl_s(env={}) == RATE_BRIDGE_CACHE_TTL_S

    def test_ttl_default_when_blank(self) -> None:
        """A blank/whitespace value reads as unset."""
        assert resolve_rate_bridge_ttl_s(env={RATE_BRIDGE_TTL_ENV: "   "}) == (
            RATE_BRIDGE_CACHE_TTL_S
        )

    def test_ttl_valid_value_adopted(self) -> None:
        """A valid float ≥ 0 is adopted verbatim."""
        assert resolve_rate_bridge_ttl_s(env={RATE_BRIDGE_TTL_ENV: "7.5"}) == 7.5

    def test_ttl_zero_is_valid_and_disables_fast_path(self) -> None:
        """``0`` is VALID — it disables the TTL fast path (always revalidate)."""
        assert resolve_rate_bridge_ttl_s(env={RATE_BRIDGE_TTL_ENV: "0"}) == 0.0

    def test_ttl_reads_process_env_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``env=None`` reads ``os.environ`` (the production path)."""
        monkeypatch.setenv(RATE_BRIDGE_TTL_ENV, "3.5")
        assert resolve_rate_bridge_ttl_s() == 3.5

    @pytest.mark.parametrize("raw", ["banana", "-1", "-0.001", "inf", "nan"])
    def test_ttl_malformed_fails_loud(self, raw: str) -> None:
        """Malformed / negative / non-finite → ValueError, never a silent default."""
        with pytest.raises(ValueError, match=RATE_BRIDGE_TTL_ENV):
            resolve_rate_bridge_ttl_s(env={RATE_BRIDGE_TTL_ENV: raw})

    def test_deadline_default_when_unset(self) -> None:
        """No env var → the shipped default."""
        assert resolve_rate_bridge_read_deadline_s(env={}) == RATE_BRIDGE_READ_DEADLINE_S

    def test_deadline_valid_value_adopted(self) -> None:
        """A valid float > 0 is adopted verbatim."""
        assert resolve_rate_bridge_read_deadline_s(env={RATE_BRIDGE_READ_DEADLINE_ENV: "1.25"}) == (
            1.25
        )

    def test_deadline_reads_process_env_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``env=None`` reads ``os.environ`` (the production path)."""
        monkeypatch.setenv(RATE_BRIDGE_READ_DEADLINE_ENV, "0.75")
        assert resolve_rate_bridge_read_deadline_s() == 0.75

    @pytest.mark.parametrize("raw", ["banana", "0", "-2", "inf", "nan"])
    def test_deadline_malformed_or_nonpositive_fails_loud(self, raw: str) -> None:
        """Malformed / zero / negative / non-finite would defeat the bound → ValueError."""
        with pytest.raises(ValueError, match=RATE_BRIDGE_READ_DEADLINE_ENV):
            resolve_rate_bridge_read_deadline_s(env={RATE_BRIDGE_READ_DEADLINE_ENV: raw})


class TestInvariantPins:
    """Constant relationships the honest-freshness argument depends on."""

    def test_ttl_strictly_below_stale_after(self) -> None:
        """A cached serve can lag ≤ TTL — it must NEVER cover the 12 s verdict window."""
        assert RATE_BRIDGE_CACHE_TTL_S < STALE_AFTER_S

    def test_deadline_strictly_positive(self) -> None:
        """A non-positive deadline would defeat the freeze guard."""
        assert RATE_BRIDGE_READ_DEADLINE_S > 0.0


class TestStampBridgeFreshness:
    """The ONE additive-only payload stamp."""

    @pytest.mark.parametrize(
        "source",
        [BRIDGE_SOURCE_LIVE, BRIDGE_SOURCE_CACHED, BRIDGE_SOURCE_UNVERIFIED, BRIDGE_SOURCE_UNWIRED],
    )
    def test_writes_exactly_the_two_keys(self, source: str) -> None:
        """Every source value stamps exactly the two keys; nothing else is mutated."""
        payload: dict[str, object] = {"flow_state": "idle", "req_per_min": 3}
        stamp_bridge_freshness(payload, BridgeReadResult(None, source, 1.5))
        assert payload == {
            "flow_state": "idle",
            "req_per_min": 3,
            "bridge_source": source,
            "bridge_probe_age_s": 1.5,
        }

    def test_age_rounded_to_3dp(self) -> None:
        """The age is rounded to 3 dp (the ``stamp_freshness`` precedent)."""
        payload: dict[str, object] = {}
        stamp_bridge_freshness(payload, BridgeReadResult(None, BRIDGE_SOURCE_CACHED, 1.23456))
        assert payload["bridge_probe_age_s"] == 1.235

    def test_none_age_passes_through(self) -> None:
        """A ``None`` age (cold / unwired) passes through as JSON ``null``."""
        payload: dict[str, object] = {}
        stamp_bridge_freshness(payload, BridgeReadResult(None, BRIDGE_SOURCE_UNWIRED, None))
        assert payload["bridge_probe_age_s"] is None


class TestConstructorResolution:
    """Explicit args win; ``None`` resolves the env-overridable defaults."""

    def test_defaults_resolved_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``ttl_s=None`` / ``read_deadline_s=None`` resolve the env overrides."""
        monkeypatch.setenv(RATE_BRIDGE_TTL_ENV, "9.0")
        monkeypatch.setenv(RATE_BRIDGE_READ_DEADLINE_ENV, "0.9")
        cache = RateBridgeCache()
        assert cache._ttl_s == 9.0
        assert cache._read_deadline_s == 0.9

    def test_explicit_args_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit ctor values are used verbatim (env not consulted)."""
        monkeypatch.setenv(RATE_BRIDGE_TTL_ENV, "9.0")
        monkeypatch.setenv(RATE_BRIDGE_READ_DEADLINE_ENV, "0.9")
        cache = RateBridgeCache(ttl_s=1.0, read_deadline_s=0.25)
        assert cache._ttl_s == 1.0
        assert cache._read_deadline_s == 0.25


class TestTtlHit:
    """The zero-syscall O(1) hot path."""

    def test_second_get_within_ttl_is_cached_zero_syscall(self) -> None:
        """Two gets inside the TTL: probe ONCE, reader ONCE, second = cached + age."""
        clock = _Clock()
        probe = _Probe(["t1"])
        reader = _Reader([_snap()])
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=probe,
            reader=reader,
            guard=_inline_guard,
        )
        first = cache.get(_LAKE)
        assert first.source == BRIDGE_SOURCE_LIVE
        assert first.probe_age_s == 0.0
        assert first.snapshot == _snap()
        clock.advance(1.0)
        second = cache.get(_LAKE)
        assert second.source == BRIDGE_SOURCE_CACHED
        assert second.probe_age_s == 1.0
        assert second.snapshot == _snap()
        assert probe.calls == 1
        assert reader.calls == 1

    def test_hit_never_touches_probe_or_reader(self) -> None:
        """The must-not-call pin (the ntm_cache ``_must_not_fetch_opener`` precedent)."""
        clock = _Clock()
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=_Probe(["t1"]),
            reader=_Reader([_snap()]),
            guard=_inline_guard,
        )
        cache.get(_LAKE)

        def _boom_probe(_root: Path) -> str | None:
            raise AssertionError("probe must not run on a TTL hit")

        def _boom_reader(_root: Path) -> RateBridgeSnapshot | None:
            raise AssertionError("reader must not run on a TTL hit")

        cache._probe = _boom_probe
        cache._reader = _boom_reader
        clock.advance(1.9)
        assert cache.get(_LAKE).source == BRIDGE_SOURCE_CACHED

    def test_exact_ttl_boundary_misses(self) -> None:
        """age == ttl exactly → NOT a hit (strict ``<`` — revalidates)."""
        clock = _Clock()
        probe = _Probe(["t1"])
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=probe,
            reader=_Reader([_snap()]),
            guard=_inline_guard,
        )
        cache.get(_LAKE)
        clock.advance(2.0)
        assert cache.get(_LAKE).source == BRIDGE_SOURCE_LIVE
        assert probe.calls == 2

    def test_ttl_zero_always_revalidates(self) -> None:
        """``ttl_s=0`` disables the fast path — every get is an owner refresh."""
        clock = _Clock()
        probe = _Probe(["t1"])
        cache = RateBridgeCache(
            ttl_s=0.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=probe,
            reader=_Reader([_snap()]),
            guard=_inline_guard,
        )
        assert cache.get(_LAKE).source == BRIDGE_SOURCE_LIVE
        assert cache.get(_LAKE).source == BRIDGE_SOURCE_LIVE
        assert probe.calls == 2

    def test_backwards_clock_age_clamped_to_zero(self) -> None:
        """A backwards monotonic step (clock skew seam) clamps the age ≥ 0."""
        clock = _Clock()
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=_Probe(["t1"]),
            reader=_Reader([_snap()]),
            guard=_inline_guard,
        )
        cache.get(_LAKE)
        clock.advance(-5.0)
        result = cache.get(_LAKE)
        assert result.source == BRIDGE_SOURCE_CACHED
        assert result.probe_age_s == 0.0


class TestTokenRevalidation:
    """The content-token decision matrix inside one refresh."""

    def test_unchanged_token_revalidates_without_reread(self) -> None:
        """Post-TTL with an unchanged token: probe again, NO re-read, live + age 0."""
        clock = _Clock()
        probe = _Probe(["t1", "t1"])
        reader = _Reader([_snap()])
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=probe,
            reader=reader,
            guard=_inline_guard,
        )
        cache.get(_LAKE)
        clock.advance(3.0)
        result = cache.get(_LAKE)
        assert result.source == BRIDGE_SOURCE_LIVE
        assert result.probe_age_s == 0.0
        assert result.snapshot == _snap()
        assert probe.calls == 2
        assert reader.calls == 1  # revalidated — the decode was skipped

    def test_moved_token_rereads_once(self) -> None:
        """A moved token (a CLI re-publish) → exactly one re-read, new snapshot served."""
        clock = _Clock()
        probe = _Probe(["t1", "t2"])
        new = _snap(effective_rps=7.7)
        reader = _Reader([_snap(), new])
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=probe,
            reader=reader,
            guard=_inline_guard,
        )
        cache.get(_LAKE)
        clock.advance(3.0)
        result = cache.get(_LAKE)
        assert result.snapshot == new
        assert reader.calls == 2

    def test_absent_token_steady_state_stat_only(self) -> None:
        """ABSENT steady state: one read (None), thereafter stat-only revalidation."""
        clock = _Clock()
        probe = _Probe([ABSENT_TOKEN, ABSENT_TOKEN, "t1"])
        reader = _Reader([None, _snap()])
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=probe,
            reader=reader,
            guard=_inline_guard,
        )
        first = cache.get(_LAKE)
        assert first.snapshot is None  # honest absence = panel-only downstream
        assert reader.calls == 1
        clock.advance(3.0)
        second = cache.get(_LAKE)
        assert second.snapshot is None
        assert reader.calls == 1  # ABSENT == ABSENT → revalidated, zero opens
        clock.advance(3.0)
        third = cache.get(_LAKE)  # the file appeared → token moved → one read
        assert third.snapshot == _snap()
        assert reader.calls == 2

    def test_none_token_reads_and_is_never_trusted(self) -> None:
        """A ``None`` token (stat error): read anyway, and the NEXT refresh re-reads too."""
        clock = _Clock()
        probe = _Probe([None, None])
        reader = _Reader([_snap()])
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=probe,
            reader=reader,
            guard=_inline_guard,
        )
        assert cache.get(_LAKE).snapshot == _snap()  # "if uncertain, fetch"
        assert reader.calls == 1
        clock.advance(3.0)
        cache.get(_LAKE)
        assert reader.calls == 2  # None stored as None → unknown never revalidates


class TestRefreshFailure:
    """A raising INJECTED probe/reader (production ones never raise by contract)."""

    def test_raising_reader_keeps_memo_and_retries_at_ttl_cadence(self) -> None:
        """WARN + memo kept + ``_probed_at`` advanced (TTL-cadence retry, never per poll)."""
        clock = _Clock()
        probe = _Probe(["t1", "t2", "t3"])

        calls = {"n": 0}

        def flaky_reader(_root: Path) -> RateBridgeSnapshot | None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected reader fault")
            return _snap()

        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=probe,
            reader=flaky_reader,
            guard=_inline_guard,
        )
        cache.get(_LAKE)
        clock.advance(3.0)
        with capture_logs() as logs:
            result = cache.get(_LAKE)
        assert result.source == BRIDGE_SOURCE_LIVE  # the attempt COMPLETED (WARN is the signal)
        assert result.snapshot == _snap()  # prior memo kept
        events = [entry["event"] for entry in logs]
        assert events.count("rate_bridge_cache.refresh_failed") == 1
        # _probed_at advanced: a get within the TTL is a plain cached hit (no per-poll retry).
        clock.advance(1.0)
        assert cache.get(_LAKE).source == BRIDGE_SOURCE_CACHED
        assert calls["n"] == 2
        # _inflight was cleared: the next post-TTL get retries and succeeds.
        clock.advance(3.0)
        assert cache.get(_LAKE).snapshot == _snap()
        assert calls["n"] == 3

    def test_raising_probe_keeps_memo_and_token(self) -> None:
        """A raising probe is caught the same way; the stale token is NOT overwritten."""
        clock = _Clock()

        probes = {"n": 0}

        def flaky_probe(_root: Path) -> str | None:
            probes["n"] += 1
            if probes["n"] == 2:
                raise RuntimeError("injected probe fault")
            return "t1"

        reader = _Reader([_snap()])
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=clock,
            probe=flaky_probe,
            reader=reader,
            guard=_inline_guard,
        )
        cache.get(_LAKE)
        clock.advance(3.0)
        with capture_logs() as logs:
            result = cache.get(_LAKE)
        assert result.snapshot == _snap()
        assert any(entry["event"] == "rate_bridge_cache.refresh_failed" for entry in logs)
        clock.advance(3.0)
        # Recovered probe returns the SAME token → revalidate without re-reading.
        assert cache.get(_LAKE).source == BRIDGE_SOURCE_LIVE
        assert reader.calls == 1


class TestLateBindingReader:
    """``reader=None`` resolves ``brutex.data.rate_bridge.read_rate_snapshot`` at call time."""

    def test_monkeypatch_after_construction_takes_effect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The existing ``monkeypatch.setattr("brutex.data.rate_bridge...")`` seam works."""
        cache = RateBridgeCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=_Clock(),
            probe=_Probe(["t1"]),
            guard=_inline_guard,
        )
        sentinel = _snap(effective_rps=9.9)
        monkeypatch.setattr("brutex.data.rate_bridge.read_rate_snapshot", lambda _root: sentinel)
        assert cache.get(_LAKE).snapshot == sentinel


class TestWedgedReadBounded:
    """The slow-read latency bound through the REAL ``run_with_deadline`` guard."""

    def test_wedged_reader_returns_bounded_and_unverified(self) -> None:
        """A wedged (Event-blocked) read: get() returns fast; served ``unverified``.

        The teeth are hang-vs-return: without the deadline graft the owner
        would block until the Event (10 s safety), failing the wall budget.
        """
        release = threading.Event()
        started = threading.Event()
        baseline_threads = threading.active_count()

        def wedged_reader(_root: Path) -> RateBridgeSnapshot | None:
            started.set()
            release.wait(10.0)  # a wedged external volume: blocks far past the deadline
            return _snap()

        cache = RateBridgeCache(
            ttl_s=0.0, read_deadline_s=0.05, probe=_Probe(["t1"]), reader=wedged_reader
        )
        try:
            t0 = time.perf_counter()
            with capture_logs() as logs:
                first = cache.get(_LAKE)
            elapsed = time.perf_counter() - t0
            assert elapsed < 2.0  # generous non-flaky budget; the pin is bounded-vs-hang
            assert first.source == BRIDGE_SOURCE_UNVERIFIED
            assert first.snapshot is None  # cold memo — honest None, panel-only downstream
            assert first.probe_age_s is None  # no refresh has EVER completed
            assert any(entry["event"] == "rate_bridge_cache.read_degraded" for entry in logs)
            assert started.wait(5.0)
            # Followers while wedged: immediate, no second daemon (single-flight
            # keyed on daemon COMPLETION — _inflight stays set).
            t0 = time.perf_counter()
            second = cache.get(_LAKE)
            assert time.perf_counter() - t0 < 1.0
            assert second.source == BRIDGE_SOURCE_UNVERIFIED
        finally:
            release.set()
        # Late adoption self-heals: the daemon's finally commits + clears
        # _inflight, so a later get serves the wedged read's eventual result.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            result = cache.get(_LAKE)
            if result.snapshot is not None:
                break
            time.sleep(0.01)
        assert cache.get(_LAKE).snapshot == _snap()
        # Thread hygiene: the abandoned daemon exits once the Event fires.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and threading.active_count() > baseline_threads:
            time.sleep(0.01)
        assert threading.active_count() <= baseline_threads

    def test_degraded_warn_once_per_outage_and_recovered_info_once(self) -> None:
        """Exactly ONE ``read_degraded`` WARN per outage flip + ONE ``read_recovered`` INFO."""
        release = threading.Event()
        wedged = {"active": True}

        def reader(_root: Path) -> RateBridgeSnapshot | None:
            if wedged["active"]:
                release.wait(10.0)
            return _snap()

        probe = _Probe(["t1", "t2", "t3", "t4"])  # every refresh re-reads (token moves)
        cache = RateBridgeCache(ttl_s=0.0, read_deadline_s=0.05, probe=probe, reader=reader)
        try:
            with capture_logs() as logs:
                assert cache.get(_LAKE).source == BRIDGE_SOURCE_UNVERIFIED  # WARN (flip in)
                assert cache.get(_LAKE).source == BRIDGE_SOURCE_UNVERIFIED  # in-flight: no WARN
                release.set()  # first daemon completes + clears _inflight
                wedged["active"] = False
                # Wait for the wedged daemon to finish, then a clean owner refresh
                # recovers: INFO exactly once, back to live.
                deadline = time.monotonic() + 5.0
                result = cache.get(_LAKE)
                while time.monotonic() < deadline and result.source != BRIDGE_SOURCE_LIVE:
                    time.sleep(0.01)
                    result = cache.get(_LAKE)
                assert result.source == BRIDGE_SOURCE_LIVE
                assert cache.get(_LAKE).source == BRIDGE_SOURCE_LIVE  # no second INFO
            events = [entry["event"] for entry in logs]
            assert events.count("rate_bridge_cache.read_degraded") == 1
            assert events.count("rate_bridge_cache.read_recovered") == 1
        finally:
            release.set()

    def test_wedge_with_prior_memo_serves_it_with_growing_age(self) -> None:
        """A wedge AFTER a good refresh serves the last-known memo, age ticking."""
        clock = _Clock()
        release = threading.Event()
        calls = {"n": 0}

        def reader(_root: Path) -> RateBridgeSnapshot | None:
            calls["n"] += 1
            if calls["n"] >= 2:
                release.wait(10.0)
            return _snap()

        probe = _Probe(["t1", "t2"])
        cache = RateBridgeCache(
            ttl_s=2.0, read_deadline_s=0.05, clock=clock, probe=probe, reader=reader
        )
        try:
            assert cache.get(_LAKE).source == BRIDGE_SOURCE_LIVE
            clock.advance(3.0)
            wedged = cache.get(_LAKE)  # owner: deadline exceeded — memo + age served
            assert wedged.source == BRIDGE_SOURCE_UNVERIFIED
            assert wedged.snapshot == _snap()
            assert wedged.probe_age_s == 3.0
            clock.advance(2.0)
            later = cache.get(_LAKE)  # in-flight follower: age keeps growing
            assert later.source == BRIDGE_SOURCE_UNVERIFIED
            assert later.probe_age_s == 5.0
        finally:
            release.set()

    def test_second_outage_after_unrecovered_first_does_not_rewarn(self) -> None:
        """A repeat DeadlineExceeded with ``_degraded`` still latched does not re-WARN."""
        first_release = threading.Event()
        second_release = threading.Event()
        calls = {"n": 0}

        def reader(_root: Path) -> RateBridgeSnapshot | None:
            calls["n"] += 1
            if calls["n"] == 1:
                first_release.wait(10.0)
            else:
                second_release.wait(10.0)
            return None

        probe = _Probe(["t1", "t2"])
        cache = RateBridgeCache(ttl_s=0.0, read_deadline_s=0.05, probe=probe, reader=reader)
        try:
            with capture_logs() as logs:
                assert cache.get(_LAKE).source == BRIDGE_SOURCE_UNVERIFIED  # WARN once
                first_release.set()  # daemon 1 completes → _inflight clears
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and calls["n"] < 2:
                    # The next owner wedges AGAIN (daemon 2) — no second WARN.
                    if cache.get(_LAKE).source == BRIDGE_SOURCE_UNVERIFIED and calls["n"] >= 2:
                        break
                    time.sleep(0.01)
                assert calls["n"] >= 2
            events = [entry["event"] for entry in logs]
            assert events.count("rate_bridge_cache.read_degraded") == 1
        finally:
            first_release.set()
            second_release.set()


class TestConcurrency:
    """Single-flight + lock hygiene under real threads."""

    def test_barrier_race_single_reader_no_exceptions(self) -> None:
        """N threads racing a cold get(): exactly ONE read, zero exceptions."""
        n = 8
        barrier = threading.Barrier(n)
        reads = {"n": 0}
        cache = RateBridgeCache(ttl_s=60.0, read_deadline_s=5.0, probe=_Probe(["t1"]))

        def reader(_root: Path) -> RateBridgeSnapshot | None:
            reads["n"] += 1
            return _snap()

        cache._reader = reader
        results: list[BridgeReadResult] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(10.0)
                results.append(cache.get(_LAKE))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        assert not errors
        assert len(results) == n
        assert reads["n"] == 1  # single-flight: one owner, everyone else immediate
        sources = {result.source for result in results}
        assert sources <= {BRIDGE_SOURCE_LIVE, BRIDGE_SOURCE_UNVERIFIED, BRIDGE_SOURCE_CACHED}
        assert BRIDGE_SOURCE_LIVE in sources  # the owner completed

    def test_lock_never_held_across_probe_or_reader_io(self) -> None:
        """The cache lock is FREE while the probe / reader run (never held across I/O).

        Deterministic single-thread form: with the inline guard the whole
        refresh runs on the caller, and no other thread exists — so a held
        lock could only be the refresh's own (the hygiene bug this pins out).
        """
        cache = RateBridgeCache(ttl_s=0.0, read_deadline_s=0.5, guard=_inline_guard)
        seen: list[bool] = []

        def probe(_root: Path) -> str | None:
            seen.append(cache._lock.locked())
            return "t1"

        def reader(_root: Path) -> RateBridgeSnapshot | None:
            seen.append(cache._lock.locked())
            return _snap()

        cache._probe = probe
        cache._reader = reader
        assert cache.get(_LAKE).source == BRIDGE_SOURCE_LIVE
        assert seen == [False, False]

    def test_inflight_follower_returns_memo_immediately(self) -> None:
        """While the owner refresh is in flight a follower serves last-known, labelled."""
        clock = _Clock()
        started = threading.Event()
        release = threading.Event()
        calls = {"n": 0}

        def reader(_root: Path) -> RateBridgeSnapshot | None:
            calls["n"] += 1
            if calls["n"] >= 2:
                started.set()
                release.wait(10.0)
            return _snap(effective_rps=float(calls["n"]))

        probe = _Probe(["t1", "t2"])
        cache = RateBridgeCache(
            ttl_s=2.0, read_deadline_s=5.0, clock=clock, probe=probe, reader=reader
        )
        owner_result: list[BridgeReadResult] = []
        try:
            assert cache.get(_LAKE).source == BRIDGE_SOURCE_LIVE
            clock.advance(3.0)
            owner = threading.Thread(target=lambda: owner_result.append(cache.get(_LAKE)))
            owner.start()
            assert started.wait(5.0)  # the owner's refresh is now genuinely in flight
            follower = cache.get(_LAKE)
            assert follower.source == BRIDGE_SOURCE_UNVERIFIED
            assert follower.snapshot == _snap(effective_rps=1.0)  # last-known memo
            assert calls["n"] == 2  # the follower spawned NO second refresh
        finally:
            release.set()
        owner.join(10.0)
        assert owner_result[0].source == BRIDGE_SOURCE_LIVE
        assert owner_result[0].snapshot == _snap(effective_rps=2.0)
