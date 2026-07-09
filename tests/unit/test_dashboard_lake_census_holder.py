"""``LakeCensusHolder`` — the non-blocking census runner (DEC-LAKE-CENSUS-NONBLOCKING-001).

Covers the whole holder contract at 100% line+branch:

* SINGLE-FLIGHT — concurrent ``maybe_refresh`` calls spawn exactly ONE check
  thread; an in-flight scan is never duplicated and never waited on;
* the O(1) ``snapshot`` — the honest no-verdict-yet warming arm, the landed
  verdict merged with the four additive keys, live progress counting via the
  scanner's ``per_file_pause`` seam;
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
    """A landed verdict merges byte-compatible keys + the four additive ones."""

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
        assert snap["scan_error"] == "OSError: lake unreadable"
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
        assert snap["scan_error"] == "ValueError: boom"
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
                clock.advance(101.0)  # the in-flight scan outlives the bound
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
        assert seen == [False, True, True]
        stall_warns = [w for w in rec.warnings if w[0] == "lake_census.scan_stalled"]
        assert len(stall_warns) == 1  # once per episode, never a warn storm
        assert stall_warns[0][1]["files_scanned"] == 0
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
        assert snap["scan_error"] is not None and "vanished" in snap["scan_error"]
        assert snap["scan_stalled"] is False and snap["scan_in_progress"] is False
        # The next poll past the base floor re-arms (the volume may be back).
        flip["vanish"] = False
        clock.advance(3.0)
        assert holder.maybe_refresh() is True
        assert scan.calls == 3
        assert holder.snapshot()["scan_error"] is None
