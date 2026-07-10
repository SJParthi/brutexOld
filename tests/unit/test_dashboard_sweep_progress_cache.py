"""Unit tests for ``brutex.dashboard.sweep_progress_cache`` (DEC-SWEEP-PROGRESS-NONBLOCK-CACHE-001).

100% line + branch on the non-blocking, deadline-guarded progress-read memo
(the sweep-progress twin of the #1522 rate-bridge cache suite):

* the zero-syscall TTL hit (reader called ONCE across polls — must-not-call pin);
* the file-arm contract byte-preserved: missing file → ``None`` (fall through),
  present-but-corrupt → ``read_progress``'s own idle fallback dict;
* the SLOW-READ latency bound through the REAL ``run_with_deadline`` guard (an
  Event-wedged reader returns bounded, served ``unverified``, exactly ONE
  abandoned daemon per outage, late adoption self-heals);
* non-blocking single-flight followers; once-per-outage degraded WARN +
  recovered INFO;
* the GUARD-SPAWN-FAILURE arm (the #1522 hostile-review MAJOR, mirrored): a
  guard raising other than ``DeadlineExceeded`` never raises out of
  :meth:`read`, releases the single-flight flag, WARNs once per outage, and
  ``KeyboardInterrupt`` propagates with the flag released;
* the fail-loud env overrides, the additive-only freshness stamp, and the
  late-binding reader seam.

All deterministic: injected clock / reader / guard seams; the one real-thread
test coordinates on :class:`threading.Event` released in ``finally``.
"""

# Whitebox: the ctor-resolution + single-flight pins read module-private state
# deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.sweep_progress_cache import (
    PROGRESS_SOURCE_CACHED,
    PROGRESS_SOURCE_LIVE,
    PROGRESS_SOURCE_UNVERIFIED,
    SWEEP_PROGRESS_CACHE_TTL_S,
    SWEEP_PROGRESS_READ_DEADLINE_ENV,
    SWEEP_PROGRESS_READ_DEADLINE_S,
    SWEEP_PROGRESS_TTL_ENV,
    ProgressReadResult,
    SweepProgressCache,
    _stamp_progress_freshness,
    resolve_sweep_progress_read_deadline_s,
    resolve_sweep_progress_ttl_s,
)
from structlog.testing import capture_logs

_MISSING = Path("/nonexistent-lake-root/sweeps/progress/sweep-progress.json")


def _running_payload(enumerated: int = 100) -> dict[str, Any]:
    """A minimal running progress payload (values are opaque to the cache)."""
    return {"state": "running", "running": True, "enumerated": enumerated}


class _Clock:
    """Injected monotonic clock — fully deterministic, steppable both ways."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Reader:
    """Counting reader returning a scripted payload sequence (last one repeats)."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.calls = 0

    def __call__(self, path: Path) -> dict[str, Any]:
        self.calls += 1
        return self.payloads[min(self.calls - 1, len(self.payloads) - 1)]


def _inline_guard(fn: Callable[[], None], _seconds: float) -> None:
    """Thread-free guard seam: run the refresh synchronously on the caller."""
    fn()


class _ScriptedGuard:
    """Guard driven by a per-call script: ``None`` runs inline, an exception raises.

    Models ``run_with_deadline``'s one non-DeadlineExceeded raise path
    (``Thread.start()`` under thread/memory exhaustion) where the refresh
    daemon NEVER runs, so ``_refresh``'s ``finally`` can never clear
    ``_inflight``. The last script step repeats.
    """

    def __init__(self, script: list[BaseException | None]) -> None:
        self.script = script
        self.calls = 0

    def __call__(self, fn: Callable[[], None], _seconds: float) -> None:
        self.calls += 1
        step = self.script[min(self.calls - 1, len(self.script) - 1)]
        if step is not None:
            raise step
        fn()


class TestEnvResolvers:
    """Fail-loud env override parsing for both tunables."""

    def test_ttl_default_when_unset(self) -> None:
        assert resolve_sweep_progress_ttl_s(env={}) == SWEEP_PROGRESS_CACHE_TTL_S

    def test_ttl_default_when_blank(self) -> None:
        assert (
            resolve_sweep_progress_ttl_s(env={SWEEP_PROGRESS_TTL_ENV: "   "})
            == SWEEP_PROGRESS_CACHE_TTL_S
        )

    def test_ttl_valid_value_adopted(self) -> None:
        assert resolve_sweep_progress_ttl_s(env={SWEEP_PROGRESS_TTL_ENV: "2.5"}) == 2.5

    def test_ttl_zero_is_valid_and_disables_fast_path(self) -> None:
        assert resolve_sweep_progress_ttl_s(env={SWEEP_PROGRESS_TTL_ENV: "0"}) == 0.0

    def test_ttl_reads_process_env_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_PROGRESS_TTL_ENV, "3.0")
        assert resolve_sweep_progress_ttl_s() == 3.0

    @pytest.mark.parametrize("raw", ["junk", "-1", "nan", "inf"])
    def test_ttl_malformed_fails_loud(self, raw: str) -> None:
        with pytest.raises(ValueError, match=SWEEP_PROGRESS_TTL_ENV):
            resolve_sweep_progress_ttl_s(env={SWEEP_PROGRESS_TTL_ENV: raw})

    def test_deadline_default_when_unset(self) -> None:
        assert resolve_sweep_progress_read_deadline_s(env={}) == SWEEP_PROGRESS_READ_DEADLINE_S

    def test_deadline_valid_value_adopted(self) -> None:
        assert (
            resolve_sweep_progress_read_deadline_s(env={SWEEP_PROGRESS_READ_DEADLINE_ENV: "1.5"})
            == 1.5
        )

    def test_deadline_reads_process_env_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_PROGRESS_READ_DEADLINE_ENV, "0.25")
        assert resolve_sweep_progress_read_deadline_s() == 0.25

    @pytest.mark.parametrize("raw", ["junk", "0", "-0.5", "nan", "inf"])
    def test_deadline_malformed_or_nonpositive_fails_loud(self, raw: str) -> None:
        with pytest.raises(ValueError, match=SWEEP_PROGRESS_READ_DEADLINE_ENV):
            resolve_sweep_progress_read_deadline_s(env={SWEEP_PROGRESS_READ_DEADLINE_ENV: raw})


class TestFreshnessStamp:
    """The additive-only ``progress_source`` / ``progress_probe_age_s`` stamp."""

    def test_writes_exactly_the_two_keys(self) -> None:
        payload = {"state": "running"}
        _stamp_progress_freshness(
            payload, ProgressReadResult(payload, PROGRESS_SOURCE_LIVE, 0.123456)
        )
        assert payload == {
            "state": "running",
            "progress_source": PROGRESS_SOURCE_LIVE,
            "progress_probe_age_s": 0.123,
        }

    def test_none_age_passes_through(self) -> None:
        payload: dict[str, Any] = {}
        _stamp_progress_freshness(
            payload, ProgressReadResult(None, PROGRESS_SOURCE_UNVERIFIED, None)
        )
        assert payload["progress_probe_age_s"] is None


class TestCtorResolution:
    """Constructor default-vs-explicit resolution for both tunables."""

    def test_defaults_resolved_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_PROGRESS_TTL_ENV, "4.0")
        monkeypatch.setenv(SWEEP_PROGRESS_READ_DEADLINE_ENV, "0.75")
        cache = SweepProgressCache()
        assert cache._ttl_s == 4.0
        assert cache._read_deadline_s == 0.75

    def test_explicit_args_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_PROGRESS_TTL_ENV, "4.0")
        monkeypatch.setenv(SWEEP_PROGRESS_READ_DEADLINE_ENV, "0.75")
        cache = SweepProgressCache(ttl_s=9.0, read_deadline_s=2.0)
        assert cache._ttl_s == 9.0
        assert cache._read_deadline_s == 2.0


class TestFileArmContract:
    """The direct view's file-arm behaviour is preserved byte-for-byte."""

    def test_missing_file_reads_none_and_never_calls_reader(self) -> None:
        reader = _Reader([_running_payload()])
        cache = SweepProgressCache(ttl_s=1.0, clock=_Clock(), reader=reader, guard=_inline_guard)
        assert cache.read(_MISSING) is None
        assert reader.calls == 0

    def test_present_file_served_and_stamped_live(self, tmp_path: Path) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload(enumerated=999)), encoding="utf-8")
        cache = SweepProgressCache(ttl_s=1.0, clock=_Clock(), guard=_inline_guard)
        out = cache.read(progress_file)
        assert out is not None
        assert out["enumerated"] == 999
        assert out["progress_source"] == PROGRESS_SOURCE_LIVE
        assert out["progress_probe_age_s"] == 0.0

    def test_corrupt_file_serves_read_progress_idle_fallback(self, tmp_path: Path) -> None:
        """PRESENT-but-corrupt → ``read_progress``'s idle dict (never a fall-through)."""
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text("{not json", encoding="utf-8")
        cache = SweepProgressCache(ttl_s=1.0, clock=_Clock(), guard=_inline_guard)
        out = cache.read(progress_file)
        assert out is not None
        assert out["state"] == "idle"
        assert out["progress_source"] == PROGRESS_SOURCE_LIVE

    def test_late_bound_default_reader_is_monkeypatchable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``reader=None`` late-binds ``sweep_supervisor.read_progress`` at call time."""
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            "brutex.engine.sweep_supervisor.read_progress",
            lambda path: {"state": "patched"},
        )
        cache = SweepProgressCache(ttl_s=1.0, clock=_Clock(), guard=_inline_guard)
        out = cache.read(progress_file)
        assert out is not None
        assert out["state"] == "patched"

    def test_stamp_is_on_a_copy_never_the_memo(self, tmp_path: Path) -> None:
        """Two serves stamp two COPIES — the memoised payload stays unstamped."""
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")
        clock = _Clock()
        cache = SweepProgressCache(ttl_s=10.0, clock=clock, guard=_inline_guard)
        first = cache.read(progress_file)
        assert first is not None
        first["enumerated"] = -1  # mutate the served copy
        second = cache.read(progress_file)
        assert second is not None
        assert second["enumerated"] == 100  # the memo was never mutated
        assert cache._payload is not None
        assert "progress_source" not in cache._payload


class TestTtlMemo:
    """The zero-syscall TTL fast path + its boundaries."""

    def test_second_read_within_ttl_is_cached_zero_reader_calls(self, tmp_path: Path) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")
        clock = _Clock()
        reader = _Reader([_running_payload()])
        cache = SweepProgressCache(ttl_s=1.0, clock=clock, reader=reader, guard=_inline_guard)
        assert cache.read(progress_file) is not None
        clock.advance(0.5)
        out = cache.read(progress_file)
        assert out is not None
        assert out["progress_source"] == PROGRESS_SOURCE_CACHED
        assert out["progress_probe_age_s"] == 0.5
        assert reader.calls == 1  # the hit paid ZERO reads

    def test_exact_ttl_boundary_misses(self, tmp_path: Path) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")
        clock = _Clock()
        reader = _Reader([_running_payload()])
        cache = SweepProgressCache(ttl_s=1.0, clock=clock, reader=reader, guard=_inline_guard)
        cache.read(progress_file)
        clock.advance(1.0)  # age == TTL: strict `<` ⇒ miss ⇒ refresh
        out = cache.read(progress_file)
        assert out is not None
        assert out["progress_source"] == PROGRESS_SOURCE_LIVE
        assert reader.calls == 2

    def test_ttl_zero_always_revalidates(self, tmp_path: Path) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")
        reader = _Reader([_running_payload()])
        cache = SweepProgressCache(ttl_s=0.0, clock=_Clock(), reader=reader, guard=_inline_guard)
        cache.read(progress_file)
        cache.read(progress_file)
        assert reader.calls == 2

    def test_backwards_clock_age_clamped_to_zero(self, tmp_path: Path) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")
        clock = _Clock()
        cache = SweepProgressCache(ttl_s=1.0, clock=clock, guard=_inline_guard)
        cache.read(progress_file)
        clock.advance(-5.0)  # skew backwards
        out = cache.read(progress_file)
        assert out is not None
        assert out["progress_source"] == PROGRESS_SOURCE_CACHED
        assert out["progress_probe_age_s"] == 0.0

    def test_missing_file_result_is_memoised_too(self) -> None:
        """A committed ``None`` (missing file) rides the TTL like a payload."""
        clock = _Clock()
        reader = _Reader([_running_payload()])
        cache = SweepProgressCache(ttl_s=1.0, clock=clock, reader=reader, guard=_inline_guard)
        assert cache.read(_MISSING) is None
        clock.advance(0.5)
        assert cache.read(_MISSING) is None  # TTL hit on the None memo
        assert reader.calls == 0


class TestRefreshFailure:
    """A raising INJECTED reader never wedges the cache and retries at TTL cadence."""

    def test_raising_reader_keeps_memo_and_advances_probe(self, tmp_path: Path) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")
        clock = _Clock()
        cache = SweepProgressCache(ttl_s=1.0, clock=clock, guard=_inline_guard)
        first = cache.read(progress_file)
        assert first is not None and first["state"] == "running"

        def _boom(path: Path) -> dict[str, Any]:
            raise OSError("injected reader failure")

        cache._reader = _boom
        clock.advance(2.0)
        with capture_logs() as logs:
            out = cache.read(progress_file)
        assert out is not None
        assert out["state"] == "running"  # the memo survived the failed refresh
        # LIVE: the refresh attempt COMPLETED (an honest completed degrade).
        assert out["progress_source"] == PROGRESS_SOURCE_LIVE
        assert any(entry["event"] == "sweep_progress_cache.refresh_failed" for entry in logs)
        # _probed_at advanced ⇒ the next poll within TTL is a cached hit, not a retry.
        clock.advance(0.5)
        cached = cache.read(progress_file)
        assert cached is not None
        assert cached["progress_source"] == PROGRESS_SOURCE_CACHED


class TestWedgedReadBounded:
    """The DeadlineExceeded arm through the REAL ``run_with_deadline`` guard."""

    def test_wedged_reader_is_bounded_served_unverified_then_self_heals(
        self, tmp_path: Path
    ) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload(enumerated=1)), encoding="utf-8")
        release = threading.Event()
        committed = threading.Event()
        real_calls: list[int] = []

        def _wedged(path: Path) -> dict[str, Any]:
            real_calls.append(1)
            if len(real_calls) == 1:
                release.wait(timeout=10.0)
                committed.set()
            return _running_payload(enumerated=42)

        cache = SweepProgressCache(ttl_s=0.0, read_deadline_s=0.05, reader=_wedged)
        try:
            with capture_logs() as logs:
                start = time.perf_counter()
                first = cache.read(progress_file)
                owner_elapsed = time.perf_counter() - start
                # COLD wedge: no memo yet — honest None (fall through), bounded.
                assert first is None
                assert owner_elapsed < 2.0
                # Follower while the daemon is still wedged: O(1) immediate,
                # unverified, no second reader call.
                follower = cache.read(progress_file)
                assert follower is None
                assert len(real_calls) == 1
            assert any(entry["event"] == "sweep_progress_cache.read_degraded" for entry in logs), (
                logs
            )
            # Once-per-outage: the follower did not WARN a second time.
            assert (
                sum(1 for entry in logs if entry["event"] == "sweep_progress_cache.read_degraded")
                == 1
            )
        finally:
            release.set()
        # The abandoned daemon's LATE return self-heals the memo (last-write-wins),
        # and the first COMPLETED refresh flips the degraded latch back (INFO).
        assert committed.wait(timeout=10.0)
        deadline = time.monotonic() + 10.0
        healed: dict[str, Any] | None = None
        with capture_logs() as logs:
            while time.monotonic() < deadline:
                healed = cache.read(progress_file)
                if healed is not None:
                    break
                time.sleep(0.01)
        assert healed is not None
        assert healed["enumerated"] == 42
        assert any(entry["event"] == "sweep_progress_cache.read_recovered" for entry in logs)


class TestDeadlineWarnsOncePerOutage:
    """A SECOND owner deadline inside one outage stays quiet (once-per-outage latch)."""

    def test_second_owner_deadline_does_not_rewarn(self, tmp_path: Path) -> None:
        from brutex.adapters.groww.deadline import DeadlineExceeded

        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")

        def _ran_then_deadline(fn: Callable[[], None], _seconds: float) -> None:
            # Models the daemon COMPLETING just after the owner's deadline
            # expired (the late-return self-heal window): the refresh ran (so
            # ``_inflight`` was cleared by its finally) but the OWNER still
            # sees DeadlineExceeded — the next poll is an owner again.
            fn()
            raise DeadlineExceeded("deadline")

        cache = SweepProgressCache(ttl_s=0.0, clock=_Clock(), guard=_ran_then_deadline)
        with capture_logs() as logs:
            first = cache.read(progress_file)
            second = cache.read(progress_file)
        assert first is not None and first["progress_source"] == PROGRESS_SOURCE_UNVERIFIED
        assert second is not None and second["progress_source"] == PROGRESS_SOURCE_UNVERIFIED
        assert (
            sum(1 for entry in logs if entry["event"] == "sweep_progress_cache.read_degraded") == 1
        )


class TestGuardSpawnFailure:
    """The #1522 MAJOR fold, mirrored: a spawn-failed guard never wedges the cache."""

    def test_spawn_failure_serves_unverified_and_releases_flag(self, tmp_path: Path) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")
        guard = _ScriptedGuard([RuntimeError("can't start new thread"), None])
        cache = SweepProgressCache(ttl_s=0.0, clock=_Clock(), guard=guard)
        with capture_logs() as logs:
            assert cache.read(progress_file) is None  # cold: nothing known, labelled degrade
        assert any(entry["event"] == "sweep_progress_cache.refresh_spawn_failed" for entry in logs)
        assert cache._inflight is False  # the flag was released by the OWNER
        # The next poll re-spawns (script step 2 runs inline) and recovers.
        with capture_logs() as logs:
            out = cache.read(progress_file)
        assert out is not None
        assert out["progress_source"] == PROGRESS_SOURCE_LIVE
        assert any(entry["event"] == "sweep_progress_cache.read_recovered" for entry in logs)

    def test_spawn_failure_warns_once_per_outage(self, tmp_path: Path) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")
        guard = _ScriptedGuard([RuntimeError("exhausted")])  # repeats forever
        cache = SweepProgressCache(ttl_s=0.0, clock=_Clock(), guard=guard)
        with capture_logs() as logs:
            cache.read(progress_file)
            cache.read(progress_file)
        assert (
            sum(
                1 for entry in logs if entry["event"] == "sweep_progress_cache.refresh_spawn_failed"
            )
            == 1
        )

    def test_keyboard_interrupt_propagates_with_flag_released(self, tmp_path: Path) -> None:
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_payload()), encoding="utf-8")
        guard = _ScriptedGuard([KeyboardInterrupt(), None])
        cache = SweepProgressCache(ttl_s=0.0, clock=_Clock(), guard=guard)
        with pytest.raises(KeyboardInterrupt):
            cache.read(progress_file)
        assert cache._inflight is False
        # The cache still works afterwards (the flag did not leak).
        out = cache.read(progress_file)
        assert out is not None
        assert out["state"] == "running"
