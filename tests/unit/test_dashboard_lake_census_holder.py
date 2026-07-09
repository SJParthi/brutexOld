"""``LakeCensusHolder`` — the non-blocking census runner (DEC-LAKE-CENSUS-NONBLOCKING-001).

Covers the whole holder contract at 100% line+branch:

* SINGLE-FLIGHT — concurrent ``maybe_refresh`` calls spawn exactly ONE check
  thread; an in-flight scan is never duplicated and never waited on;
* the O(1) ``snapshot`` — the honest no-verdict-yet warming arm, the landed
  verdict merged with the additive scan-state keys (incl. the split
  ``scan_stalled`` / ``refresh_probe_wedged`` staleness pair), live progress
  counting via the scanner's ``per_file_pause`` seam;
* the ERROR arm — a raised scan stores ``scan_error``, keeps the last-known
  verdict serving, and re-arms after the BASE floor (never a fabricated
  verdict, never a wedged holder);
* freshness — a generation move rescans; an unchanged generation is a cheap
  check with NO scan; the #920 adaptive TTL floor throttles rescans; a
  generation-probe failure serves stale and retries (payload present) but
  never blocks the FIRST-ever scan (payload absent);
* the first-ever no-sentinel arm — a ``None`` / raising estimate degrades to
  an honest ``files_total_estimate: null``;
* seams — injectable clock / spawn / now_utc; a spawn failure WARNs and
  releases the single-flight; the default spawn runs a named daemon thread.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import brutex.dashboard.lake_census_holder as lake_census_holder_module
import pytest
from brutex.dashboard.lake_census_holder import (
    CENSUS_THREAD_NAME,
    SCAN_STALE_FLOOR_S,
    LakeCensusHolder,
    _default_spawn,
    _utc_now_iso,
)

# pyright: reportPrivateUsage=none


class _FakeClock:
    """A manually-advanced monotonic clock driving the adaptive TTL floor."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _sync_spawn(body: Callable[[], None]) -> None:
    """Run the check body inline — deterministic single-threaded tests."""
    body()


class _CountingScan:
    """A scan seam counting invocations; optionally ticks progress / raises."""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        ticks: int = 0,
        raises: Exception | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.calls = 0
        self._payload = payload if payload is not None else {"present": True, "findings_total": 0}
        self._ticks = ticks
        self._raises = raises
        self._on_call = on_call

    def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
        self.calls += 1
        if self._on_call is not None:
            self._on_call()
        for _ in range(self._ticks):
            per_file_pause()
        if self._raises is not None:
            raise self._raises
        return dict(self._payload)


def _holder(
    scan: _CountingScan,
    *,
    generation: Callable[[], int] | None = None,
    estimate: Callable[[], int | None] | None = None,
    clock: _FakeClock | None = None,
    spawn: Callable[[Callable[[], None]], None] | None = _sync_spawn,
    now_utc: Callable[[], str] | None = None,
) -> LakeCensusHolder:
    return LakeCensusHolder(
        scan=scan,
        generation=generation if generation is not None else (lambda: 0),
        files_total_estimate=estimate,
        clock=clock,
        spawn=spawn,
        now_utc=now_utc if now_utc is not None else (lambda: "2026-07-09T00:00:00+00:00"),
    )


class TestSnapshotWarmingArm:
    """The honest no-verdict-yet payload — never a fabricated verdict."""

    def test_first_ever_snapshot_has_no_verdict_and_no_scan_state(self) -> None:
        scan = _CountingScan()
        holder = _holder(scan, clock=_FakeClock(), spawn=lambda body: None)  # never runs
        snap = holder.snapshot()
        assert snap == {
            "present": True,
            "scan_in_progress": False,
            "scan_stalled": False,
            "refresh_probe_wedged": False,
            "refresh_probe_wedged_s": None,
            "scan_error": None,
            "as_of_utc": None,
            "scan_progress": {
                "files_scanned": 0,
                "files_total_estimate": None,
                "started_utc": None,
            },
        }
        assert holder.scans_completed == 0

    def test_in_flight_first_scan_reads_scan_in_progress_before_the_scan_body(self) -> None:
        """A first-ever in-flight CHECK (no payload) is honestly in-progress.

        With a deferred spawn the check body has not run yet, but a first-ever
        check ALWAYS proceeds to the scan (nothing to serve), so the snapshot
        reads in-progress instead of a misleading idle blank.
        """
        bodies: list[Callable[[], None]] = []
        scan = _CountingScan()
        holder = _holder(scan, clock=_FakeClock(), spawn=bodies.append)
        assert holder.maybe_refresh() is True
        assert holder.snapshot()["scan_in_progress"] is True
        bodies[0]()  # land the scan
        assert holder.snapshot()["scan_in_progress"] is False
        assert holder.scans_completed == 1

    def test_progress_counter_ticks_per_file_and_is_reported_live(self) -> None:
        seen: list[int] = []
        holder_ref: list[LakeCensusHolder] = []

        class _ProgressProbeScan(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                for _ in range(3):
                    per_file_pause()
                    seen.append(holder_ref[0].snapshot()["scan_progress"]["files_scanned"])
                return {"present": True, "findings_total": 0}

        scan = _ProgressProbeScan()
        holder = _holder(scan, estimate=lambda: 42, clock=_FakeClock())
        holder_ref.append(holder)
        assert holder.maybe_refresh() is True
        # Live + monotone mid-scan; the estimate rode along.
        assert seen == [1, 2, 3]
        snap = holder.snapshot()
        assert snap["scan_progress"]["files_scanned"] == 3
        assert snap["scan_progress"]["files_total_estimate"] == 42
        assert snap["scan_progress"]["started_utc"] is not None


class TestVerdictSnapshot:
    """A landed verdict merges byte-compatible keys + the additive scan-state ones."""

    def test_landed_verdict_merges_additive_keys(self) -> None:
        stamps = iter(["start-1", "as-of-1"])
        scan = _CountingScan({"present": True, "findings_total": 2, "engine_clean": False})
        holder = _holder(scan, clock=_FakeClock(), now_utc=lambda: next(stamps))
        assert holder.maybe_refresh() is True
        snap = holder.snapshot()
        assert snap["present"] is True
        assert snap["findings_total"] == 2
        assert snap["engine_clean"] is False
        assert snap["scan_in_progress"] is False
        assert snap["scan_stalled"] is False
        assert snap["refresh_probe_wedged"] is False
        assert snap["refresh_probe_wedged_s"] is None
        assert snap["scan_error"] is None
        assert snap["as_of_utc"] == "as-of-1"
        assert snap["scan_progress"]["started_utc"] == "start-1"
        assert holder.scans_completed == 1

    def test_snapshot_returns_a_copy_not_the_stored_payload(self) -> None:
        scan = _CountingScan()
        holder = _holder(scan, clock=_FakeClock())
        holder.maybe_refresh()
        first = holder.snapshot()
        first["findings_total"] = 999
        assert holder.snapshot()["findings_total"] == 0


class TestSingleFlight:
    """Concurrent refreshes spawn exactly one check; nothing ever waits."""

    def test_second_refresh_while_in_flight_is_a_noop(self) -> None:
        bodies: list[Callable[[], None]] = []
        scan = _CountingScan()
        holder = _holder(scan, clock=_FakeClock(), spawn=bodies.append)
        assert holder.maybe_refresh() is True
        assert holder.maybe_refresh() is False  # in-flight — no second spawn
        assert len(bodies) == 1
        bodies[0]()
        assert scan.calls == 1

    def test_concurrent_threads_spawn_exactly_one_check(self) -> None:
        spawned: list[Callable[[], None]] = []
        lock = threading.Lock()

        def _capture(body: Callable[[], None]) -> None:
            with lock:
                spawned.append(body)

        scan = _CountingScan()
        holder = _holder(scan, clock=_FakeClock(), spawn=_capture)
        barrier = threading.Barrier(8)
        results: list[bool] = []
        results_lock = threading.Lock()

        def _race() -> None:
            barrier.wait(timeout=10.0)
            got = holder.maybe_refresh()
            with results_lock:
                results.append(got)

        threads = [threading.Thread(target=_race, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert len(spawned) == 1
        assert results.count(True) == 1
        assert results.count(False) == 7

    def test_spawn_failure_warns_and_releases_the_single_flight(self) -> None:
        def _broken_spawn(body: Callable[[], None]) -> None:
            raise RuntimeError("no threads left")

        scan = _CountingScan()
        clock = _FakeClock()
        holder = _holder(scan, clock=clock, spawn=_broken_spawn)
        assert holder.maybe_refresh() is False  # spawn failed — swallowed, WARNed
        # The single-flight was RELEASED: the next poll past the check floor retries.
        clock.advance(3.0)
        bodies: list[Callable[[], None]] = []
        holder._spawn = bodies.append
        assert holder.maybe_refresh() is True
        assert len(bodies) == 1


class TestFreshnessLaw:
    """Generation re-arm + the #920 adaptive TTL floor + the check floor."""

    def test_generation_move_rescans_and_unchanged_generation_does_not(self) -> None:
        gen = {"n": 0}
        scan = _CountingScan()
        clock = _FakeClock()
        holder = _holder(scan, generation=lambda: gen["n"], clock=clock)
        assert holder.maybe_refresh() is True
        assert scan.calls == 1
        # Past both floors, generation UNCHANGED → a cheap check, NO scan.
        clock.advance(3.0)
        assert holder.maybe_refresh() is True
        assert scan.calls == 1
        # Generation moved → rescan.
        gen["n"] += 1
        clock.advance(3.0)
        assert holder.maybe_refresh() is True
        assert scan.calls == 2

    def test_adaptive_floor_throttles_a_slow_scan_to_its_own_duration(self) -> None:
        gen = {"n": 0}
        clock = _FakeClock()
        scan = _CountingScan(on_call=lambda: clock.advance(30.0))  # each scan "takes" 30 s
        holder = _holder(scan, generation=lambda: gen["n"], clock=clock)
        holder.maybe_refresh()
        assert scan.calls == 1
        # Five dirty polls inside the 30 s adaptive floor window pay ZERO scans
        # AND zero check threads (pure clock math on the caller's thread).
        for _ in range(5):
            gen["n"] += 1
            clock.advance(1.0)
            assert holder.maybe_refresh() is False
        assert scan.calls == 1
        # Past the floor the dirty holder rescans — bounded, never a freeze.
        clock.advance(30.0)
        assert holder.maybe_refresh() is True
        assert scan.calls == 2

    def test_check_floor_bounds_no_op_check_spawns(self) -> None:
        """Unchanged-generation checks are ALSO throttled to one per base floor."""
        scan = _CountingScan()
        clock = _FakeClock()
        holder = _holder(scan, clock=clock)
        holder.maybe_refresh()
        clock.advance(3.0)
        assert holder.maybe_refresh() is True  # cheap check, no scan
        assert holder.maybe_refresh() is False  # within the base check floor
        assert scan.calls == 1

    def test_generation_probe_failure_serves_stale_with_payload(self) -> None:
        scan = _CountingScan()
        clock = _FakeClock()

        def _raising_generation() -> int:
            raise OSError("stalled volume")

        holder = _holder(scan, generation=_raising_generation, clock=clock)
        # FIRST-ever: the probe failure must NOT block the first scan.
        assert holder.maybe_refresh() is True
        assert scan.calls == 1
        # With a payload, an unknowable generation serves stale — no scan.
        clock.advance(3.0)
        assert holder.maybe_refresh() is True
        assert scan.calls == 1
        snap = holder.snapshot()
        assert snap["findings_total"] == 0  # the stale verdict still serves


class TestErrorArm:
    """A raised scan is stored honestly and re-arms after the BASE floor."""

    def test_scan_error_is_surfaced_and_last_verdict_keeps_serving(self) -> None:
        gen = {"n": 0}
        clock = _FakeClock()
        good_then_bad = {"fail": False}

        class _FlippingScan(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                if good_then_bad["fail"]:
                    raise OSError("lake unreadable")
                return {"present": True, "findings_total": 0}

        scan = _FlippingScan()
        holder = _holder(scan, generation=lambda: gen["n"], clock=clock)
        holder.maybe_refresh()
        assert holder.snapshot()["scan_error"] is None
        # The next scan fails: the verdict keeps serving + the error is stamped.
        good_then_bad["fail"] = True
        gen["n"] += 1
        clock.advance(3.0)
        holder.maybe_refresh()
        snap = holder.snapshot()
        assert snap["findings_total"] == 0  # last-known verdict — never blanked
        assert snap["scan_error"] == (
            "census scan failed (OSError) — see server log (lake_census.scan_failed)"
        )
        # SECURITY: the raw exception text (which may carry absolute lake
        # paths) is NEVER served — class name only, detail in the log.
        assert "lake unreadable" not in snap["scan_error"]
        assert snap["scan_in_progress"] is False
        assert holder.scans_completed == 1
        # Re-arm after the BASE floor (the error reset the adaptive duration),
        # and a clean scan CLEARS the error.
        good_then_bad["fail"] = False
        clock.advance(3.0)
        holder.maybe_refresh()
        assert holder.snapshot()["scan_error"] is None
        assert holder.scans_completed == 2

    def test_first_ever_scan_error_reads_error_not_a_fabricated_verdict(self) -> None:
        scan = _CountingScan(raises=ValueError("boom"))
        clock = _FakeClock()
        holder = _holder(scan, clock=clock)
        holder.maybe_refresh()
        snap = holder.snapshot()
        assert "findings_total" not in snap  # NEVER fabricated
        assert snap["scan_error"] == (
            "census scan failed (ValueError) — see server log (lake_census.scan_failed)"
        )
        assert "boom" not in snap["scan_error"]
        # Within the base floor: no re-arm; past it: the retry scans again.
        clock.advance(1.0)
        assert holder.maybe_refresh() is False
        clock.advance(2.0)
        assert holder.maybe_refresh() is True
        assert scan.calls == 2


class TestEstimateSeam:
    """The first-ever no-sentinel arm + the raising estimator degrade."""

    def test_no_estimator_reads_null_estimate(self) -> None:
        scan = _CountingScan()
        holder = _holder(scan, estimate=None, clock=_FakeClock())
        holder.maybe_refresh()
        assert holder.snapshot()["scan_progress"]["files_total_estimate"] is None

    def test_raising_estimator_degrades_to_null_never_a_crash(self) -> None:
        def _broken() -> int | None:
            raise OSError("sentinel unreadable")

        scan = _CountingScan()
        holder = _holder(scan, estimate=_broken, clock=_FakeClock())
        holder.maybe_refresh()
        snap = holder.snapshot()
        assert snap["scan_progress"]["files_total_estimate"] is None
        assert snap["findings_total"] == 0  # the scan itself still landed


class TestDefaultSeams:
    """The production defaults: named daemon thread + real monotonic clock."""

    def test_default_spawn_runs_a_named_daemon_thread(self) -> None:
        ran = threading.Event()
        names: list[str] = []
        daemons: list[bool] = []

        def _body() -> None:
            names.append(threading.current_thread().name)
            daemons.append(threading.current_thread().daemon)
            ran.set()

        _default_spawn(_body)
        assert ran.wait(timeout=10.0)
        assert names == [CENSUS_THREAD_NAME]
        assert daemons == [True]

    def test_default_clock_and_now_utc_produce_sane_values(self) -> None:
        scan = _CountingScan()
        holder = LakeCensusHolder(scan=scan, generation=lambda: 0, spawn=_sync_spawn)
        assert holder.maybe_refresh() is True
        snap = holder.snapshot()
        assert snap["findings_total"] == 0
        assert isinstance(snap["as_of_utc"], str) and "T" in snap["as_of_utc"]

    def test_utc_now_iso_is_utc_aware(self) -> None:
        stamp = _utc_now_iso()
        assert stamp.endswith("+00:00")


class TestNegativeClockClamp:
    """A non-monotonic injected clock can never store a negative duration."""

    def test_backwards_clock_clamps_duration_to_zero(self) -> None:
        clock = _FakeClock()
        scan = _CountingScan(on_call=lambda: clock.advance(-5.0))
        holder = _holder(scan, clock=clock)
        holder.maybe_refresh()
        assert holder.scans_completed == 1
        # The clamped 0 duration keeps the BASE floor (no negative freeze).
        clock.advance(8.0)
        assert holder.maybe_refresh() is True


@pytest.mark.parametrize("in_flight_payload", [True, False])
def test_scan_in_progress_reflects_scanning_only_with_a_payload(
    in_flight_payload: bool,
) -> None:
    """With a payload, an in-flight CHECK (not yet scanning) is NOT 'scanning'.

    The check prefix is one generation read (~ms) — reporting it as a rescan
    would flicker the chip every window; only the actual O(files) scan reads
    ``scan_in_progress`` alongside a stored verdict.
    """
    bodies: list[Callable[[], None]] = []
    scan = _CountingScan()
    clock = _FakeClock()
    holder = _holder(scan, clock=clock, spawn=bodies.append)
    if in_flight_payload:
        holder.maybe_refresh()
        bodies.pop()()  # land the first scan synchronously
        clock.advance(3.0)
        holder.maybe_refresh()  # arm a check; body NOT yet run
        assert holder.snapshot()["scan_in_progress"] is False
        bodies.pop()()
    else:
        holder.maybe_refresh()  # first-ever: in-flight with no payload
        assert holder.snapshot()["scan_in_progress"] is True
        bodies.pop()()


class _RecordingLog:
    """A `_LOG` stand-in recording warning events (the once-per-episode oracle)."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.warnings.append((event, kw))


class TestScanStalledVerdict:
    """ADD-3 loud staleness: a never-finishing scan is flagged, never silent."""

    def test_fresh_scan_not_stalled_then_past_bound_stalls_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """not-stalled → stalled + ONE WARN per episode → completion clears."""
        rec = _RecordingLog()
        monkeypatch.setattr(lake_census_holder_module, "_LOG", rec)
        clock = _FakeClock()
        seen: list[bool] = []
        holder_ref: list[LakeCensusHolder] = []

        class _StallingScan(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                seen.append(holder_ref[0].snapshot()["scan_stalled"])  # fresh → False
                clock.advance(100.0)  # exactly AT the bound — NOT stalled (strict >,
                seen.append(holder_ref[0].snapshot()["scan_stalled"])  # kills >= mutants)
                clock.advance(1.0)  # strictly past the bound
                seen.append(holder_ref[0].snapshot()["scan_stalled"])  # → True + WARN
                seen.append(holder_ref[0].snapshot()["scan_stalled"])  # still True, NO 2nd WARN
                return {"present": True, "findings_total": 0}

        scan = _StallingScan()
        holder = LakeCensusHolder(
            scan=scan,
            generation=lambda: 0,
            clock=clock,
            spawn=_sync_spawn,
            scan_stale_after_s=100.0,
        )
        holder_ref.append(holder)
        assert holder.maybe_refresh() is True
        assert seen == [False, False, True, True]
        stall_warns = [w for w in rec.warnings if w[0] == "lake_census.scan_stalled"]
        assert len(stall_warns) == 1  # once per episode, never a warn storm
        assert stall_warns[0][1]["files_scanned"] == 0
        assert stall_warns[0][1]["phase"] == "scan"
        # Completion CLEARS the flag — the landed verdict serves normally.
        done = holder.snapshot()
        assert done["scan_stalled"] is False
        assert done["findings_total"] == 0

    def test_dynamic_bound_floor_arm_first_ever_scan(self) -> None:
        """No explicit bound + no prior duration → the 30-min floor governs."""
        clock = _FakeClock()
        seen: list[bool] = []
        holder_ref: list[LakeCensusHolder] = []

        class _FloorScan(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                clock.advance(SCAN_STALE_FLOOR_S)  # exactly AT the bound — not stalled
                seen.append(holder_ref[0].snapshot()["scan_stalled"])
                clock.advance(1.0)  # strictly past → stalled
                seen.append(holder_ref[0].snapshot()["scan_stalled"])
                return {"present": True, "findings_total": 0}

        holder = _holder(_FloorScan(), clock=clock)
        holder_ref.append(holder)
        assert holder.maybe_refresh() is True
        assert seen == [False, True]

    def test_dynamic_bound_scales_to_twice_the_last_scan_duration(self) -> None:
        """A landed slow scan WIDENS the bound to 2x its own duration."""
        gen = {"n": 0}
        clock = _FakeClock()
        seen: list[bool] = []
        holder_ref: list[LakeCensusHolder] = []
        durations = iter([2000.0])  # first scan "takes" 2000 s; second probes the bound

        class _TwoPhaseScan(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                if self.calls == 1:
                    clock.advance(next(durations))
                else:
                    # bound = max(2 x 2000, floor 1800) = 4000: at 3999 not
                    # stalled, past it stalled.
                    clock.advance(3999.0)
                    seen.append(holder_ref[0].snapshot()["scan_stalled"])
                    clock.advance(2.0)
                    seen.append(holder_ref[0].snapshot()["scan_stalled"])
                return {"present": True, "findings_total": 0}

        holder = _holder(_TwoPhaseScan(), generation=lambda: gen["n"], clock=clock)
        holder_ref.append(holder)
        assert holder.maybe_refresh() is True
        gen["n"] += 1
        clock.advance(121.0)  # past the (capped) adaptive TTL floor
        assert holder.maybe_refresh() is True
        assert seen == [False, True]

    def test_backwards_clock_skew_never_false_stalls(self) -> None:
        """now < scan start (skew) → negative elapsed → NEVER stalled."""
        clock = _FakeClock()
        seen: list[bool] = []
        holder_ref: list[LakeCensusHolder] = []

        class _SkewScan(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                clock.advance(-50.0)  # the injected clock jumps BACKWARDS
                seen.append(holder_ref[0].snapshot()["scan_stalled"])
                return {"present": True, "findings_total": 0}

        holder = LakeCensusHolder(
            scan=_SkewScan(),
            generation=lambda: 0,
            clock=clock,
            spawn=_sync_spawn,
            scan_stale_after_s=10.0,
        )
        holder_ref.append(holder)
        clock.advance(100.0)  # a non-zero start baseline so the rewind is real skew
        assert holder.maybe_refresh() is True
        assert seen == [False]

    def test_each_scan_opens_a_fresh_warn_episode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _RecordingLog()
        monkeypatch.setattr(lake_census_holder_module, "_LOG", rec)
        gen = {"n": 0}
        clock = _FakeClock()
        holder_ref: list[LakeCensusHolder] = []

        class _AlwaysStalling(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                clock.advance(11.0)
                assert holder_ref[0].snapshot()["scan_stalled"] is True
                return {"present": True, "findings_total": 0}

        holder = LakeCensusHolder(
            scan=_AlwaysStalling(),
            generation=lambda: gen["n"],
            clock=clock,
            spawn=_sync_spawn,
            scan_stale_after_s=10.0,
        )
        holder_ref.append(holder)
        assert holder.maybe_refresh() is True
        gen["n"] += 1
        clock.advance(121.0)
        assert holder.maybe_refresh() is True
        stall_warns = [w for w in rec.warnings if w[0] == "lake_census.scan_stalled"]
        assert len(stall_warns) == 2  # one WARN per scan episode


class TestExtremePermutations:
    """ADD-4 permutation arms not already pinned by the suites above."""

    def test_restart_mid_scan_shape_warming_serves_sentinel_estimate(self) -> None:
        """A fresh process (no payload) mid-first-scan: honest warming + the
        L3-sentinel denominator — never a fabricated verdict."""
        probes: list[dict[str, Any]] = []
        holder_ref: list[LakeCensusHolder] = []

        class _MidScanProbe(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                per_file_pause()
                probes.append(holder_ref[0].snapshot())
                return {"present": True, "findings_total": 0}

        holder = _holder(_MidScanProbe(), estimate=lambda: 2_140_000, clock=_FakeClock())
        holder_ref.append(holder)
        assert holder.maybe_refresh() is True
        (mid,) = probes
        assert mid["scan_in_progress"] is True
        assert "findings_total" not in mid  # never fabricated mid-first-scan
        assert mid["scan_progress"]["files_total_estimate"] == 2_140_000
        assert mid["scan_progress"]["files_scanned"] == 1

    def test_disk_vanish_mid_scan_oserror_serves_last_known_and_rearms(self) -> None:
        """An OSError (volume vanished) mid-scan lands the error arm: WARN +
        last-known verdict keeps serving + the next poll re-arms."""
        gen = {"n": 0}
        clock = _FakeClock()
        flip = {"vanish": False}

        class _VanishingScan(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                if flip["vanish"]:
                    raise OSError("[Errno 5] Input/output error: lake volume vanished")
                return {"present": True, "findings_total": 0, "files_scanned": 7}

        scan = _VanishingScan()
        holder = _holder(scan, generation=lambda: gen["n"], clock=clock)
        holder.maybe_refresh()
        flip["vanish"] = True
        gen["n"] += 1
        clock.advance(3.0)
        holder.maybe_refresh()
        snap = holder.snapshot()
        assert snap["files_scanned"] == 7  # last-known verdict still serves
        assert snap["scan_error"] == (
            "census scan failed (OSError) — see server log (lake_census.scan_failed)"
        )
        # The raw OSError text carried the absolute lake path — never served.
        assert "vanished" not in snap["scan_error"]
        assert snap["scan_stalled"] is False and snap["scan_in_progress"] is False
        # The next poll past the base floor re-arms (the volume may be back).
        flip["vanish"] = False
        clock.advance(3.0)
        assert holder.maybe_refresh() is True
        assert scan.calls == 3
        assert holder.snapshot()["scan_error"] is None


class TestProbeWedgeStalled:
    """The check-PREFIX wedge (generation probe = lake I/O) is LOUD, never a
    silent forever-warming: `scan_stalled` flips even before `_scanning`."""

    def test_wedged_generation_probe_flags_probe_phase_stalled_then_recovers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _RecordingLog()
        monkeypatch.setattr(lake_census_holder_module, "_LOG", rec)
        clock = _FakeClock()
        entered = threading.Event()
        release = threading.Event()

        def _blocked_generation() -> int:
            entered.set()
            release.wait(timeout=30.0)
            return 0

        scan = _CountingScan()
        holder = LakeCensusHolder(
            scan=scan,
            generation=_blocked_generation,
            clock=clock,
            spawn=None,  # the REAL daemon thread — the probe genuinely blocks it
            scan_stale_after_s=10.0,
        )
        try:
            assert holder.maybe_refresh() is True
            assert entered.wait(timeout=10.0), "the generation probe was never entered"
            # The probe is wedged BEFORE the scan body: no started_utc, no
            # verdict — pre-fix this read scan_in_progress forever silently.
            clock.advance(10.0)  # exactly AT the bound — not stalled (strict >)
            snap = holder.snapshot()
            assert snap["scan_in_progress"] is True
            assert snap["scan_stalled"] is False
            clock.advance(1.0)  # strictly past → the probe-phase stall verdict
            snap = holder.snapshot()
            assert snap["scan_stalled"] is True
            assert "findings_total" not in snap  # never a fabricated verdict
            assert snap["scan_progress"]["started_utc"] is None  # honest: no scan yet
            # The first-boot probe episode IS the warming state: scan_stalled
            # (scan_in_progress is also true) — never the payload-present
            # refresh_probe_wedged verdict.
            assert snap["scan_in_progress"] is True
            assert snap["refresh_probe_wedged"] is False
            assert snap["refresh_probe_wedged_s"] is None
            holder.snapshot()  # second read: still stalled, NO second WARN
            stall_warns = [w for w in rec.warnings if w[0] == "lake_census.scan_stalled"]
            assert len(stall_warns) == 1
            assert stall_warns[0][1]["phase"] == "probe"
            # Probe-phase WARN stamps are honestly None — never a reused stamp.
            assert stall_warns[0][1]["started_utc"] is None
            assert stall_warns[0][1]["files_scanned"] is None
        finally:
            release.set()
        deadline = time.time() + 10.0
        while time.time() < deadline and holder.scans_completed < 1:
            time.sleep(0.02)
        assert holder.scans_completed == 1  # the released probe completed the scan
        done = holder.snapshot()
        assert done["scan_stalled"] is False  # recovery clears the verdict
        assert done["findings_total"] == 0


class TestFirstScanBoundHint:
    """The sentinel-seeded FIRST-scan stall bound (never false-flag a big lake)."""

    def _stalling_probe_holder(
        self,
        clock: _FakeClock,
        hint: Callable[[], float | None] | None,
        probes: list[tuple[float, bool]],
        advances: list[float],
    ) -> LakeCensusHolder:
        holder_ref: list[LakeCensusHolder] = []

        class _ProbeScan(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                for dt in advances:
                    clock.advance(dt)
                    probes.append((clock.now, holder_ref[0].snapshot()["scan_stalled"]))
                return {"present": True, "findings_total": 0}

        holder = LakeCensusHolder(
            scan=_ProbeScan(),
            generation=lambda: 0,
            clock=clock,
            spawn=_sync_spawn,
            first_scan_bound_hint=hint,
        )
        holder_ref.append(holder)
        return holder

    def test_sentinel_hint_widens_the_first_scan_bound(self) -> None:
        """hint=5000 s → bound max(5000, 1800): 4999 s in-scan is NOT stalled
        (the bare floor would have false-flagged at 1801 s), past 5000 is."""
        clock = _FakeClock()
        probes: list[tuple[float, bool]] = []
        holder = self._stalling_probe_holder(clock, lambda: 5000.0, probes, [4999.0, 2.0])
        assert holder.maybe_refresh() is True
        assert probes == [(4999.0, False), (5001.0, True)]

    def test_no_hint_keeps_the_bare_floor(self) -> None:
        clock = _FakeClock()
        probes: list[tuple[float, bool]] = []
        holder = self._stalling_probe_holder(clock, None, probes, [SCAN_STALE_FLOOR_S + 1.0])
        assert holder.maybe_refresh() is True
        assert probes == [(SCAN_STALE_FLOOR_S + 1.0, True)]

    def test_raising_hint_degrades_to_the_floor(self) -> None:
        def _broken_hint() -> float | None:
            raise OSError("sentinel unreadable")

        clock = _FakeClock()
        probes: list[tuple[float, bool]] = []
        holder = self._stalling_probe_holder(
            clock, _broken_hint, probes, [SCAN_STALE_FLOOR_S + 1.0]
        )
        assert holder.maybe_refresh() is True
        assert probes == [(SCAN_STALE_FLOOR_S + 1.0, True)]

    def test_none_hint_callable_result_degrades_to_the_floor(self) -> None:
        clock = _FakeClock()
        probes: list[tuple[float, bool]] = []
        holder = self._stalling_probe_holder(
            clock, lambda: None, probes, [SCAN_STALE_FLOOR_S + 1.0]
        )
        assert holder.maybe_refresh() is True
        assert probes == [(SCAN_STALE_FLOOR_S + 1.0, True)]

    @pytest.mark.parametrize("junk_hint", [float("inf"), float("-inf"), float("nan")])
    def test_non_finite_hint_degrades_to_the_bare_floor(self, junk_hint: float) -> None:
        """Round-1 refuter FINDING 2: ``inf`` yielded an INFINITE first-scan
        bound (staleness detection silently disabled) and ``nan`` poisoned the
        ``max`` — a non-finite hint now degrades to the bare 30-min floor."""
        clock = _FakeClock()
        probes: list[tuple[float, bool]] = []
        holder = self._stalling_probe_holder(
            clock, lambda: junk_hint, probes, [SCAN_STALE_FLOOR_S + 1.0]
        )
        assert holder.maybe_refresh() is True
        assert probes == [(SCAN_STALE_FLOOR_S + 1.0, True)]

    def test_hint_is_inert_after_the_first_landed_scan(self) -> None:
        """Post-first-completion the 2× last-duration law governs, not the hint."""
        gen = {"n": 0}
        clock = _FakeClock()
        seen: list[bool] = []
        holder_ref: list[LakeCensusHolder] = []

        class _TwoPhase(_CountingScan):
            def __call__(self, per_file_pause: Callable[[], None]) -> dict[str, Any]:
                self.calls += 1
                if self.calls == 1:
                    clock.advance(2000.0)  # first scan lands duration 2000
                else:
                    # 2x law bound = 4000 (the 9999 hint no longer applies).
                    clock.advance(4001.0)
                    seen.append(holder_ref[0].snapshot()["scan_stalled"])
                return {"present": True, "findings_total": 0}

        holder = LakeCensusHolder(
            scan=_TwoPhase(),
            generation=lambda: gen["n"],
            clock=clock,
            spawn=_sync_spawn,
            first_scan_bound_hint=lambda: 9999.0,
        )
        holder_ref.append(holder)
        assert holder.maybe_refresh() is True
        gen["n"] += 1
        clock.advance(121.0)
        assert holder.maybe_refresh() is True
        assert seen == [True]


class TestRefreshProbeWedged:
    """Round-1 refuter FINDING 1: a wedged REFRESH probe behind a LANDED
    verdict is the DISTINCT ``refresh_probe_wedged`` verdict — never a
    contradictory "✅ CLEAN yet census scan stalled" pair, and never judged
    from (or warned with) the PREVIOUS scan's ``started_utc``."""

    def test_wedged_refresh_probe_reads_distinct_flag_not_scan_stalled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """payload present + in-flight probe past the bound → wedged verdict.

        Deterministic wedge: the queued-body spawn seam arms the probe episode
        without ever running it — exactly the shape of a generation probe hung
        on a stalled lake volume. Pre-fix this read ``scan_stalled: true``
        with ``scan_in_progress: false`` and warned with the OLD scan's
        ``started_utc`` (the refuter's inconsistent-signal repro).
        """
        rec = _RecordingLog()
        monkeypatch.setattr(lake_census_holder_module, "_LOG", rec)
        bodies: list[Callable[[], None]] = []
        clock = _FakeClock()
        scan = _CountingScan()
        holder = _holder(scan, clock=clock, spawn=bodies.append)
        assert holder.maybe_refresh() is True
        bodies.pop()()  # land the first scan (duration 0 → bound = bare floor)
        clock.advance(3.0)  # past the base adaptive/check floors
        assert holder.maybe_refresh() is True  # arm the refresh probe (body queued = wedged)
        clock.advance(SCAN_STALE_FLOOR_S)  # exactly AT the bound — NOT wedged (strict >)
        snap = holder.snapshot()
        assert snap["refresh_probe_wedged"] is False
        assert snap["scan_stalled"] is False
        clock.advance(1.0)  # strictly past → the DISTINCT wedged verdict
        snap = holder.snapshot()
        assert snap["findings_total"] == 0  # the landed verdict keeps serving
        assert snap["scan_in_progress"] is False
        assert snap["scan_stalled"] is False  # NEVER "stalled scan" here
        assert snap["refresh_probe_wedged"] is True
        # Judged from the episode's OWN arming stamp — elapsed since arming,
        # NOT since the old scan's start.
        assert snap["refresh_probe_wedged_s"] == SCAN_STALE_FLOOR_S + 1.0
        # The OLD scan's started_utc still serves in scan_progress (last
        # scan's record) — but the WARN below must NOT reuse it.
        assert isinstance(snap["scan_progress"]["started_utc"], str)
        holder.snapshot()  # second read: still wedged, NO second WARN
        stall_warns = [w for w in rec.warnings if w[0] == "lake_census.scan_stalled"]
        assert len(stall_warns) == 1
        assert stall_warns[0][1]["phase"] == "probe"
        assert stall_warns[0][1]["elapsed_s"] == SCAN_STALE_FLOOR_S + 1.0
        assert stall_warns[0][1]["started_utc"] is None  # never the old scan's stamp
        assert stall_warns[0][1]["files_scanned"] is None
        # The wedged probe finally completes (generation unchanged → serve
        # stale, no rescan) — BOTH staleness flags clear.
        bodies.pop()()
        done = holder.snapshot()
        assert done["refresh_probe_wedged"] is False
        assert done["refresh_probe_wedged_s"] is None
        assert done["scan_stalled"] is False
        assert scan.calls == 1


class TestRunScanNeverStrandsScanning:
    """Round-1 refuter FINDING 3: ONE try/finally spans the scan setup + body —
    NO exit of ``_run_scan`` can strand ``_scanning`` true (a stranded flag
    read ``scan_in_progress`` forever while the fallback baseline kept
    ``scan_stalled`` unreachable — a silent permanent warming state)."""

    def test_raising_now_utc_in_setup_lands_the_error_arm_not_a_stranded_flag(
        self,
    ) -> None:
        clock = _FakeClock()
        flip = {"broken": True}

        def _flaky_now_utc() -> str:
            if flip["broken"]:
                raise RuntimeError("wall-clock backend gone /mnt/lake")
            return "2026-07-09T00:00:00+00:00"

        scan = _CountingScan()
        holder = _holder(scan, clock=clock, now_utc=_flaky_now_utc)
        assert holder.maybe_refresh() is True
        snap = holder.snapshot()
        # Pre-fix: _scanning stranded True → scan_in_progress forever with a
        # ~0 belt baseline (never stalled) — the silent forever-warming.
        assert snap["scan_in_progress"] is False
        assert snap["scan_stalled"] is False
        assert scan.calls == 0  # the setup raised BEFORE the scan body
        # The SAME sanitised error arm as a scan raise (token + class name;
        # the raw text carried a path-like detail — never served).
        assert snap["scan_error"] == (
            "census scan failed (RuntimeError) — see server log (lake_census.scan_failed)"
        )
        assert "/mnt/lake" not in snap["scan_error"]
        # Base-floor re-arm: the next poll past ~2 s retries and recovers.
        flip["broken"] = False
        clock.advance(3.0)
        assert holder.maybe_refresh() is True
        assert holder.scans_completed == 1
        assert holder.snapshot()["scan_error"] is None

    def test_raising_now_utc_at_the_success_landing_still_resets_scanning(
        self,
    ) -> None:
        """A raise AFTER the scan body (the as-of stamp) exits through the
        finally belt — ``_scanning`` reset, the honest error arm stamped."""
        clock = _FakeClock()
        calls = {"n": 0}

        def _second_call_raises() -> str:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("wall clock died mid-landing")
            return "2026-07-09T00:00:00+00:00"

        scan = _CountingScan()
        holder = _holder(scan, clock=clock, now_utc=_second_call_raises)
        assert holder.maybe_refresh() is True
        assert scan.calls == 1  # the scan itself RAN
        snap = holder.snapshot()
        assert snap["scan_in_progress"] is False  # the finally belt reset it
        assert "findings_total" not in snap  # the landing never completed
        assert snap["scan_error"] == (
            "census scan failed (RuntimeError) — see server log (lake_census.scan_failed)"
        )
        assert holder.scans_completed == 0
