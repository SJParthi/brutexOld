"""Tests for `brutex.dashboard.pull_jobs.BackgroundPullJob`.

100% line+branch via a synchronous injected spawner (no real threads): the
single-flight guard, the success → done path, the worker-exception → error
path, the positional→keyword progress adaptation, and the `running` / snapshot
accessors. One real-thread test exercises the daemon default end-to-end.

The DEC-DASHBOARD-FASTREAD-001 hooks are covered here too: the write funnel
feeds the incremental :class:`CoverageCounters` (idempotent on ``bars == 0``) and
invalidates an injected summary cache (``mark_dirty``) once per window.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from brutex.dashboard import pull_jobs
from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook
from brutex.dashboard.pull_progress import STATE_DONE, STATE_ERROR, STATE_RUNNING, PullProgress
from brutex.dashboard.pull_stall import PullWatchdog
from brutex.dashboard.summary_cache import CoverageCounters
from brutex.observability.watchdog import WorkerEvent, WorkerState


@pytest.fixture(autouse=True)
def _disable_account_singleflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the account-wide cross-process pull lock for these unit tests.

    DEC-ACCOUNT-PULL-SINGLEFLIGHT-001 makes :class:`BackgroundPullJob` take a real
    ``fcntl.flock`` under the production lake root by default. These tests are about the
    job's progress / single-flight-flag / watchdog behaviour, NOT the cross-process
    guard, so we point the default lake-root resolver at ``None`` (guard disabled) — this
    avoids touching the real ``~/.brutex/lake`` and avoids a captured-but-never-run worker
    leaking a process-wide flock into a sibling test. The dedicated guard tests live in
    ``test_pull_jobs_singleflight.py`` and inject their own resolver / factory.
    """
    monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)


def _ticking_clock() -> Callable[[], float]:
    """A deterministic, monotonically-increasing injected clock.

    A plain ``def`` closure over a mutable holder — NOT a callable class — so
    CodeQL's cross-function points-to analysis can prove the production
    ``clock()`` call sites callable (the callable-class fixture pattern
    false-positives ``py/call-to-non-callable`` on production lines; see the
    team playbook).
    """
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    return clock


def _sync_spawn(fn: Callable[[], None]) -> None:
    """Run the worker inline so the test is deterministic."""
    fn()


def _job() -> BackgroundPullJob:
    return BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=_sync_spawn)


def test_successful_job_advances_then_finishes() -> None:
    job = _job()

    def work(on_progress: ProgressHook) -> None:
        on_progress("NIFTY 1minute", 100, False, False)
        on_progress("NIFTY 1day", 5, False, False)

    started = job.start(work, total=2, detail="kick")
    assert started is True
    snap = job.snapshot()
    assert snap.state == STATE_DONE
    assert snap.detail == "done"
    assert snap.completed == 2
    assert snap.bars == 105
    assert snap.failed == 0
    assert snap.current == "NIFTY 1day"
    assert job.running is False


def test_worker_exception_becomes_error_state() -> None:
    job = _job()

    def work(on_progress: ProgressHook) -> None:
        on_progress("13 1minute", 1, False, False)
        raise RuntimeError("boom")

    assert job.start(work, total=1) is True
    snap = job.snapshot()
    assert snap.state == STATE_ERROR
    assert snap.detail == "RuntimeError: boom"
    assert job.running is False  # flag reset even on failure


def test_on_success_fires_only_on_a_successful_run() -> None:
    """A clean return fires the success hook exactly once (MAJOR-1, DEC-DASHBOARD-RESULTS-PAGE-001)."""
    job = _job()
    calls: list[int] = []

    def work(on_progress: ProgressHook) -> None:
        on_progress("NIFTY 1minute", 10, False, False)

    # Call OUT of the assert: ``start`` spawns the worker (a side effect) and a
    # bare ``assert call()`` is dropped under ``python -O`` (CodeQL
    # py/side-effect-in-assert). Store the result, then assert it.
    started = job.start(work, total=1, on_success=lambda: calls.append(1))
    assert started is True
    assert job.snapshot().state == STATE_DONE
    assert calls == [1]  # fired once on success


def test_on_success_does_not_fire_on_a_failed_run() -> None:
    """A worker exception NEVER fires the success hook (the leaderboard cache stays untouched)."""
    job = _job()
    calls: list[int] = []

    def work(on_progress: ProgressHook) -> None:
        raise RuntimeError("boom")

    started = job.start(work, total=1, on_success=lambda: calls.append(1))
    assert started is True
    assert job.snapshot().state == STATE_ERROR
    assert calls == []  # never fired on failure


def test_on_success_raising_is_swallowed_and_never_crashes_the_worker() -> None:
    """A success hook that raises is logged + swallowed; the job still reaches DONE + clears running."""
    job = _job()

    def work(on_progress: ProgressHook) -> None:
        return None

    def boom() -> None:
        raise RuntimeError("hook blew up")

    started = job.start(work, total=1, on_success=boom)
    assert started is True
    assert job.snapshot().state == STATE_DONE  # success state unaffected by the raising hook
    assert job.running is False  # the finally still cleared the single-flight flag


def test_pull_halt_global_stop_fires_alert_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pull-wide LakeWriteHalt → error state AND a best-effort halt alert fires once.

    DEC-TELEGRAM-PULL-HALT-DASHBOARD-001 — the dashboard twin of the CLI cmd_fetch
    wiring: a disk-full/OOM/quota global-stop halts EVERY remaining window, so the
    operator gets a CRITICAL Telegram alert. The error state already reported is
    unchanged; the alert is best-effort (the emit is monkeypatched here).
    """
    from brutex.data.errors import LakeWriteHalt

    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    job = _job()

    def work(_on_progress: ProgressHook) -> None:
        raise LakeWriteHalt("disk_full")

    assert job.start(work, total=1) is True
    snap = job.snapshot()
    assert snap.state == STATE_ERROR  # error state byte-unchanged
    assert "LakeWriteHalt" in snap.detail
    assert len(halts) == 1  # alert fired EXACTLY once for the global stop
    assert isinstance(halts[0], LakeWriteHalt)


def test_greeks_pool_broken_fires_pull_halt_alert_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead Greeks pool worker → error state AND the CRITICAL alert fires once.

    DEC-GREEKS-PARALLEL-001: ``GreeksEnrichPoolBroken`` (a spawn worker died
    abruptly — an OOM SIGKILL / external kill / hard interpreter crash,
    honestly indistinguishable through the public executor API) is a
    HOST-resource halt: ``is_pull_halt`` recognises it and the alert fires,
    exactly like a ``LakeWriteHalt``.
    """
    from brutex.data.errors import GreeksEnrichPoolBroken

    assert pull_jobs.is_pull_halt(GreeksEnrichPoolBroken("a worker died")) is True
    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    job = _job()

    def work(_on_progress: ProgressHook) -> None:
        raise GreeksEnrichPoolBroken("a worker process died before completing its unit")

    assert job.start(work, total=1) is True
    snap = job.snapshot()
    assert snap.state == STATE_ERROR  # error state byte-unchanged
    assert "GreeksEnrichPoolBroken" in snap.detail
    assert len(halts) == 1  # alert fired EXACTLY once for the global stop
    assert isinstance(halts[0], GreeksEnrichPoolBroken)


def test_greeks_pool_broken_rides_the_generic_reason_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``emit_pull_halt_alert`` needed ZERO source change: the non-LakeWriteHalt
    branch reasons on the type name — pinned so a refactor cannot drop it."""
    from brutex.data.errors import GreeksEnrichPoolBroken
    from brutex.notifications import sweep_alerts

    reasons: list[str] = []

    class _Recorder:
        def pull_halted(self, *, reason: str) -> None:
            reasons.append(reason)

    monkeypatch.setattr(sweep_alerts, "SweepAlerter", _Recorder)
    sweep_alerts.emit_pull_halt_alert(GreeksEnrichPoolBroken("a worker died"))
    assert reasons == ["GreeksEnrichPoolBroken"]


def test_generic_worker_error_does_not_fire_pull_halt_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-symbol / generic worker error → error state but NO pull-halt alert."""
    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    job = _job()

    def work(_on_progress: ProgressHook) -> None:
        raise RuntimeError("boom")

    assert job.start(work, total=1) is True
    assert job.snapshot().state == STATE_ERROR
    assert halts == []  # a generic worker error NEVER fires the pull-halt alert


def test_terminal_auth_failure_fires_pull_halt_alert_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TERMINAL GrowwAuthError (dead credential) → error state AND the CRITICAL alert fires once.

    DEC-GROWW-AUTH-HALT-001 regression: a dead credential previously quarantined every
    symbol one by one — the dashboard pull "completed" all-failed with NO alert (the
    silent-for-days shape). Now the spines propagate it and ``_is_pull_halt`` reads it
    as a pull-wide global stop, so the operator learns in REAL TIME.
    """
    from brutex.adapters.groww.errors import GrowwAuthError

    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    job = _job()

    def work(_on_progress: ProgressHook) -> None:
        raise GrowwAuthError("bad credential — re-mint budget exhausted")

    assert job.start(work, total=1) is True
    snap = job.snapshot()
    assert snap.state == STATE_ERROR  # error state byte-unchanged
    assert "GrowwAuthError" in snap.detail
    assert len(halts) == 1  # the CRITICAL pull-halt alert fired EXACTLY once
    assert isinstance(halts[0], GrowwAuthError)


def test_retryable_auth_shapes_never_fire_pull_halt_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RETRYABLE GrowwAuthError subclasses NEVER read as a pull halt (no alert).

    DEC-GROWW-AUTH-HALT-001 carve-out: a throttled token mint / a stale read-only
    re-read are transient by design — even if one escapes to the job layer the halt
    alert must stay silent (the error state still reports honestly).
    """
    from brutex.adapters.groww.errors import (
        GrowwTokenIssuanceThrottled,
        GrowwTokenStillStaleError,
    )

    for exc in (
        GrowwTokenIssuanceThrottled("mint throttled"),
        GrowwTokenStillStaleError("same dead token"),
    ):
        halts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
        job = _job()

        def work(_on_progress: ProgressHook, _exc: BaseException = exc) -> None:
            raise _exc

        assert job.start(work, total=1) is True
        assert job.snapshot().state == STATE_ERROR  # still honestly reported as an error
        assert halts == []  # but NEVER the CRITICAL pull-halt alert


def test_transient_auth_expiry_with_successful_remint_pull_continues_zero_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END-TO-END self-heal regression: the 06:00 IST token reset must NEVER halt.

    DEC-GROWW-AUTH-HALT-001 requirement (2): a pull whose FIRST vendor call dies on an
    auth expiry that the ``SdkRefresher`` single-flight re-mint heals (DEC-GROWW-TOKEN-
    REAUTH-001) completes NORMALLY — the healed auth error never escapes
    ``client._call``, so the job finishes DONE with ZERO pull-halt alerts. Drives the
    REAL ``GrowwClient`` reauth branch + the REAL ``fetch_candle_windows`` through a
    ``BackgroundPullJob`` — the same layers a dashboard pull crosses.
    """
    from datetime import date

    from brutex.adapters.groww.candles import fetch_candle_windows
    from brutex.adapters.groww.client import GrowwClient

    class GrowwAPIAuthenticationException(Exception):
        """Fake SDK auth exception (named so from_groww_exception maps it)."""

    class _SDK:
        def __init__(self, raises: Exception | None) -> None:
            self._raises = raises
            self.calls = 0

        def get_historical_candles(self, **_kw: object) -> dict[str, object]:
            self.calls += 1
            if self._raises is not None:
                raise self._raises
            return {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 5, None]]}

        def get_expiries(self, **_kw: object) -> dict[str, object]:  # pragma: no cover
            raise AssertionError("unused")

        def get_contracts(self, **_kw: object) -> dict[str, object]:  # pragma: no cover
            raise AssertionError("unused")

    class _Refresher:
        """Single-leaf fake SdkRefresher: the re-mint swaps the stale SDK for the fresh one."""

        def __init__(self, stale: _SDK, fresh: _SDK) -> None:
            self._sdks = [stale, fresh]
            self._index = 0
            self.generation = 0
            self.reauth_calls = 0

        def sdk_at(self, _index: int) -> _SDK:
            return self._sdks[self._index]

        def current_generation(self) -> int:
            return self.generation

        def reauth(self, seen_generation: int) -> int:
            del seen_generation
            self.reauth_calls += 1
            self._index = 1
            self.generation += 1
            return self.generation

    stale = _SDK(GrowwAPIAuthenticationException("token expired or invalid"))
    fresh = _SDK(None)
    refresher = _Refresher(stale, fresh)
    client = GrowwClient(
        sdk=stale,
        refresher=refresher,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    job = _job()
    fetched: list[int] = []

    def work(_on_progress: ProgressHook) -> None:
        bars = fetch_candle_windows(
            client,
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            drop_volume=True,
        )
        fetched.append(len(bars))

    assert job.start(work, total=1) is True
    assert job.snapshot().state == STATE_DONE  # the pull CONTINUED to completion
    assert fetched == [1]  # the healed retry served real bars
    assert refresher.reauth_calls == 1  # exactly one single-flight re-mint
    assert halts == []  # ZERO pull-halt alerts — the self-heal never reaches the halt path


def test_rotation_lag_double_401_two_remints_pull_continues_zero_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FP-1 e2e regression (DEC-GROWW-AUTH-HALT-001): the 06:00 IST rotation-lag
    shape — refresh adopts the fresh token, the FIRST post-refresh retry 401s
    (fleet-propagation lag, vendor-UNVERIFIED), the SECOND re-auth + retry
    succeeds — completes DONE with ZERO pull-halt alerts under the production
    ``GROWW_MAX_REAUTHS = 2`` budget. With the old budget of 1 the second 401
    exhausted the budget → terminal ``GrowwAuthError`` → the halt killed the
    whole overnight pull.
    """
    from datetime import date

    from brutex.adapters.groww.candles import fetch_candle_windows
    from brutex.adapters.groww.client import GrowwClient
    from brutex.adapters.groww.constants import GROWW_MAX_REAUTHS

    class GrowwAPIAuthenticationException(Exception):
        """Fake SDK auth exception (named so from_groww_exception maps it)."""

    class _SDK:
        def __init__(self, raises: Exception | None) -> None:
            self._raises = raises
            self.calls = 0

        def get_historical_candles(self, **_kw: object) -> dict[str, object]:
            self.calls += 1
            if self._raises is not None:
                raise self._raises
            return {"candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 5, None]]}

        def get_expiries(self, **_kw: object) -> dict[str, object]:  # pragma: no cover
            raise AssertionError("unused")

        def get_contracts(self, **_kw: object) -> dict[str, object]:  # pragma: no cover
            raise AssertionError("unused")

    class _LaggingRefresher:
        """Each re-auth advances the SDK queue: stale → fresh-but-lagging → valid."""

        def __init__(self, sdks: list[_SDK]) -> None:
            self._sdks = sdks
            self._index = 0
            self.generation = 0
            self.reauth_calls = 0

        def sdk_at(self, _index: int) -> _SDK:
            return self._sdks[self._index]

        def current_generation(self) -> int:
            return self.generation

        def reauth(self, seen_generation: int) -> int:
            del seen_generation
            self.reauth_calls += 1
            self._index = min(self._index + 1, len(self._sdks) - 1)
            self.generation += 1
            return self.generation

    stale = _SDK(GrowwAPIAuthenticationException("token expired or invalid"))
    lagging = _SDK(GrowwAPIAuthenticationException("fresh token not fleet-valid yet"))
    valid = _SDK(None)
    refresher = _LaggingRefresher([stale, lagging, valid])
    client = GrowwClient(
        sdk=stale,
        refresher=refresher,  # type: ignore[arg-type]
        sleep=lambda _s: None,
        max_reauths=GROWW_MAX_REAUTHS,  # the production live-build-site budget (2)
    )
    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    job = _job()
    fetched: list[int] = []

    def work(_on_progress: ProgressHook) -> None:
        bars = fetch_candle_windows(
            client,
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            drop_volume=True,
        )
        fetched.append(len(bars))

    assert job.start(work, total=1) is True
    assert job.snapshot().state == STATE_DONE  # the pull CONTINUED to completion
    assert fetched == [1]  # the second healed retry served real bars
    assert refresher.reauth_calls == 2  # the rotation lag absorbed within the budget
    assert halts == []  # ZERO pull-halt alerts — no false halt at the rotation edge
    # And the TRUE-dead-credential case still halts after the budget: an SDK fleet
    # that NEVER validates exhausts GROWW_MAX_REAUTHS and the terminal auth error
    # escapes to the job layer, firing the CRITICAL alert exactly once.
    dead = [
        _SDK(GrowwAPIAuthenticationException("dead")),
        _SDK(GrowwAPIAuthenticationException("still dead")),
        _SDK(GrowwAPIAuthenticationException("never valid")),
    ]
    dead_refresher = _LaggingRefresher(dead)
    dead_client = GrowwClient(
        sdk=dead[0],
        refresher=dead_refresher,  # type: ignore[arg-type]
        sleep=lambda _s: None,
        max_reauths=GROWW_MAX_REAUTHS,
    )
    dead_halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", dead_halts.append)
    dead_job = _job()

    def dead_work(_on_progress: ProgressHook) -> None:
        fetch_candle_windows(
            dead_client,
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            drop_volume=True,
        )

    assert dead_job.start(dead_work, total=1) is True
    assert dead_job.snapshot().state == STATE_ERROR  # the dead credential HALTS
    assert dead_refresher.reauth_calls == GROWW_MAX_REAUTHS  # bounded, never a loop
    assert len(dead_halts) == 1  # the CRITICAL pull-halt alert fired exactly once


def test_prebuild_global_stop_escapes_guard_and_fires_halt_alert_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END-TO-END F1 pin (DEC-PIPELINE-AUTOPREBUILD-001 hostile-review fold): a
    LakeWriteHalt raised INSIDE the guarded prebuild RE-RAISES (``is_pull_halt``,
    the ONE common predicate) out of the work callable, the job fails, and the
    CRITICAL pull-halt alert fires exactly once — the halt is never swallowed
    into a benign "built 0" outcome."""
    from typing import NoReturn

    from brutex.dashboard.pipeline_prebuild import PrebuildTarget, run_pipeline_prebuild_guarded
    from brutex.data.errors import LakeWriteHalt

    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    job = _job()

    def _halting_cascade() -> NoReturn:
        raise LakeWriteHalt("disk_full")

    def _never_materialize(_target: PrebuildTarget) -> NoReturn:
        raise AssertionError("materialize must never be called — the cascade halts first")

    def work(_on_progress: ProgressHook) -> object:
        return run_pipeline_prebuild_guarded(
            instruments=[],
            cascade_or_error=_halting_cascade,
            materialize=_never_materialize,
        )

    assert job.start(work, total=1) is True
    snap = job.snapshot()
    assert snap.state == STATE_ERROR  # the job FAILED — never a fake "done"
    assert "LakeWriteHalt" in snap.detail
    assert len(halts) == 1  # the CRITICAL halt alert fired exactly once
    assert isinstance(halts[0], LakeWriteHalt)


def test_failed_unit_flag_is_forwarded() -> None:
    job = _job()
    job.start(lambda on_progress: on_progress("BAD 1minute", 0, True, False), total=1)
    snap = job.snapshot()
    assert snap.failed == 1
    assert snap.state == STATE_DONE  # a failed UNIT is not a failed JOB


def test_no_data_gap_flag_is_forwarded_and_not_a_failure() -> None:
    # DEC-DHAN-NO-DATA-GAP-COUNT-001: a benign DH-905 gap tick (gap=True) bumps
    # ``gaps``, NOT ``failed`` — so a known empty boundary stops reading as a failure.
    job = _job()
    job.start(lambda on_progress: on_progress("NIFTY NEXT 50 1day", 0, False, True), total=1)
    snap = job.snapshot()
    assert snap.gaps == 1
    assert snap.failed == 0
    assert snap.state == STATE_DONE


def test_single_flight_blocks_second_start() -> None:
    # A spawner that captures (does NOT run) the worker keeps the job "running",
    # so a second start must be a no-op returning False.
    captured: list[Callable[[], None]] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=captured.append,
    )
    first = job.start(lambda _h: None, total=1)
    assert first is True
    assert job.running is True  # spawner hasn't run the worker yet
    second = job.start(lambda _h: None, total=1)
    assert second is False  # single-flight: blocked
    assert len(captured) == 1  # only the first job was scheduled
    # Drain the captured worker → job goes idle again.
    captured[0]()
    assert job.running is False
    assert job.snapshot().state == STATE_DONE


def test_running_true_during_work() -> None:
    job = _job()
    seen: list[bool] = []
    job.start(lambda _h: seen.append(job.running), total=1)
    assert seen == [True]  # inside the worker the job reports running


def test_default_progress_when_none_injected() -> None:
    # No injected tracker → a fresh PullProgress is created internally.
    job = BackgroundPullJob(spawn=_sync_spawn)
    job.start(lambda on_progress: on_progress("X", 1, False, False), total=1)
    assert job.snapshot().state == STATE_DONE


def test_real_daemon_thread_end_to_end() -> None:
    # Exercise the default daemon spawner (the pragma path is the lambda only;
    # here we use a real thread + join to prove the engine works off-thread).
    done = threading.Event()
    progress = PullProgress(clock=_ticking_clock())

    def spawn(fn: Callable[[], None]) -> None:
        t = threading.Thread(target=fn)
        t.start()
        t.join()
        done.set()

    job = BackgroundPullJob(progress=progress, spawn=spawn)
    job.start(lambda on_progress: on_progress("13 1minute", 7, False, False), total=1)
    assert done.is_set()
    assert job.snapshot().state == STATE_DONE
    assert job.snapshot().bars == 7


def test_snapshot_running_state_before_completion() -> None:
    # With a capturing spawner the tracker is started but not yet finished.
    captured: list[Callable[[], None]] = []
    job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=captured.append)
    job.start(lambda _h: None, total=3, detail="warming")
    snap = job.snapshot()
    assert snap.state == STATE_RUNNING
    assert snap.total == 3
    assert snap.detail == "warming"


# ────────────────────────────────────────────────────────────────────────
# DEC-DASHBOARD-FASTREAD-001 — coverage counters + summary-cache invalidation
# ────────────────────────────────────────────────────────────────────────


class _SpyCache:
    """A minimal summary-cache double recording every mark_dirty call."""

    def __init__(self) -> None:
        self.dirty_calls = 0

    def mark_dirty(self) -> None:
        self.dirty_calls += 1


def test_write_funnel_feeds_coverage_counters() -> None:
    # The default (auto-created) counters accumulate every window's POST-DEDUP
    # bar count through the single _on_progress funnel.
    job = _job()

    def work(on_progress: ProgressHook) -> None:
        on_progress("NIFTY 1minute", 100, False, False)
        on_progress("NIFTY 1minute", 50, False, False)
        on_progress("BANKNIFTY 1minute", 7, False, False)

    job.start(work, total=3)
    snap = job.counters.snapshot()
    assert snap.by_label == {"NIFTY 1minute": 150, "BANKNIFTY 1minute": 7}
    assert snap.total_bars == 157


def test_injected_counters_are_used() -> None:
    counters = CoverageCounters()
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()), spawn=_sync_spawn, counters=counters
    )
    job.start(lambda on_progress: on_progress("X 1minute", 9, False, False), total=1)
    assert job.counters is counters
    assert counters.snapshot().total_bars == 9


def test_duplicate_repull_zero_bars_leaves_counter_unchanged() -> None:
    # bars == 0 (post-dedup duplicate) is a no-op → idempotent headline.
    job = _job()
    job.start(lambda on_progress: on_progress("DUP 1minute", 0, False, False), total=1)
    snap = job.counters.snapshot()
    assert snap.total_bars == 0
    assert snap.by_label == {}


def test_summary_cache_invalidated_once_per_window() -> None:
    cache = _SpyCache()
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()), spawn=_sync_spawn, summary_cache=cache
    )

    def work(on_progress: ProgressHook) -> None:
        on_progress("A 1minute", 10, False, False)
        on_progress("B 1minute", 0, True, False)  # a failed/zero window still ticks the funnel

    job.start(work, total=2)
    # mark_dirty fires on EVERY progress tick (so the next poll refreshes),
    # independent of whether bars were written.
    assert cache.dirty_calls == 2


def test_no_summary_cache_means_no_invalidation_branch() -> None:
    # Default job has summary_cache=None → the mark_dirty branch is skipped
    # (no AttributeError), proving the None guard.
    job = _job()
    job.start(lambda on_progress: on_progress("X 1minute", 1, False, False), total=1)
    assert job.snapshot().state == STATE_DONE


class _Cache:
    """A minimal mark_dirty-able double (CLAUDE.md §4 — behaviour not concrete type)."""

    def __init__(self) -> None:
        self.dirty = 0

    def mark_dirty(self) -> None:
        self.dirty += 1


class TestLedgerSynced:
    """DEC-SPOT-WINDOW-PROGRESS-001: ledger-synced mode drives the bar per WINDOW."""

    def test_sync_from_ledger_sets_absolute_bar(self) -> None:
        cache = _Cache()
        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()), spawn=_sync_spawn, summary_cache=cache
        )

        def work(_on_progress: ProgressHook) -> None:
            job.sync_from_ledger(
                completed=118, total=10906, bars=12054, failed=0, current="NIFTYSMALL 1day"
            )

        job.start(work, total=779, ledger_synced=True)
        snap = job.snapshot()
        assert snap.total == 10906
        assert snap.completed == 118
        assert snap.bars == 12054
        assert snap.current == "NIFTYSMALL 1day"
        assert cache.dirty >= 1  # the sync refreshes the coverage summary mid-pull

    def test_synced_mode_per_pair_hook_only_sets_label_not_completed(self) -> None:
        # In ledger-synced mode the per-pair hook must NOT advance completed (the bar
        # comes from the ledger sync); it only refreshes the "current" label + counters.
        job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=_sync_spawn)

        def work(on_progress: ProgressHook) -> None:
            on_progress("NIFTY 1day", 500, False, False)  # a finished pair
            on_progress("BANKNIFTY 1day", 700, False, False)

        job.start(work, total=10, ledger_synced=True)
        snap = job.snapshot()
        assert snap.completed == 0  # NOT advanced per pair (ledger drives it)
        assert snap.bars == 0  # bars come from the ledger sync, not the per-pair hook
        assert snap.current == "BANKNIFTY 1day"  # label still refreshed
        assert job.counters.snapshot().total_bars == 1200  # counters still fed (500 + 700)

    def test_default_mode_still_advances_per_pair(self) -> None:
        # ledger_synced defaults to False → the per-pair advance is byte-unchanged.
        job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=_sync_spawn)
        job.start(lambda h: h("NIFTY 1day", 5, False, False), total=2)
        assert job.snapshot().completed == 1

    def test_sync_from_ledger_without_cache_is_a_noop_on_dirty(self) -> None:
        # No summary cache wired → sync still updates the bar, just no mark_dirty.
        job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=_sync_spawn)
        job.start(
            lambda _h: job.sync_from_ledger(completed=3, total=10, bars=30),
            total=10,
            ledger_synced=True,
        )
        assert job.snapshot().completed == 3


# ────────────────────────────────────────────────────────────────────────
# Stall watchdog `_on_hung` + the watchdog-identity gate (DEC-PULL-STALL-DETECTION-001)
# ────────────────────────────────────────────────────────────────────────


def _noop_spawn(_fn: Callable[[], None]) -> None:
    """A timer spawner that NEVER runs the timer body, so a real ``PullWatchdog``
    can be built (and its ``on_hung`` captured) without a background thread firing.
    """


def _hung_event() -> WorkerEvent:
    """A HEALTHY→HUNG :class:`WorkerEvent` for driving ``_on_hung`` deterministically."""
    return WorkerEvent(
        worker_name="dashboard-pull",
        new_state=WorkerState.HUNG,
        last_beat_seconds_ago=120.0,
        timeout_seconds=30.0,
    )


class TestOnHung:
    """``BackgroundPullJob._on_hung``: the stall-watchdog HUNG edge + identity gate.

    The job builds its watchdog via the injected ``watchdog_factory`` (given the
    ``on_hung`` edge callback). Here the factory captures that callback so the test
    can fire it directly with a HUNG ``WorkerEvent`` — exercising the whole
    best-effort body (stop the timer, halt alert, ``progress.fail``, identity-gated
    ``_running`` clear) WITHOUT a real daemon timer.
    """

    def _job_capturing_on_hung(
        self, captured: list[Callable[[WorkerEvent], None]]
    ) -> BackgroundPullJob:
        """A job whose watchdog factory records the ``on_hung`` callback + never spawns."""

        def factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
            captured.append(on_hung)
            return PullWatchdog(
                on_hung=on_hung, timeout_seconds=30.0, spawn=_noop_spawn, clock=_ticking_clock()
            )

        def _capture_only(_fn: Callable[[], None]) -> None:
            """Capture-only spawner: never runs the worker, so the run stays in flight."""

        return BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_capture_only,
            watchdog_factory=factory,
        )

    def test_on_hung_marks_stalled_suspect_and_holds_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A HUNG edge while THIS run is current (DEC-PIPELINE-STALL-HEARTBEAT-001):
        # progress → STALLED-SUSPECT (state stays running, never a false FAILED), the
        # single-flight flag stays HELD (the worker thread may still be alive — releasing
        # here was the forensically-proven double-run race), and a best-effort halt alert
        # fires (monkeypatched so no real notifier runs).
        alerts: list[BaseException] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", alerts.append)
        captured: list[Callable[[WorkerEvent], None]] = []
        job = self._job_capturing_on_hung(captured)

        # Start the job with a capture-only spawner → the worker never runs, so the
        # watchdog stays the CURRENT run's watchdog when on_hung fires.
        started = job.start(lambda _h: None, total=1, detail="stalled-pull")
        assert started is True
        assert job.running is True

        captured[0](_hung_event())  # fire the HUNG edge directly

        snap = job.snapshot()
        assert snap.state == STATE_RUNNING  # NOT failed — an honest DEGRADED marker only
        assert snap.stalled is True
        assert "stalled-suspect" in snap.stall_detail
        assert snap.detail == "stalled-pull"  # the start detail is untouched
        assert job.running is True  # single-flight HELD while the worker may be alive
        assert len(alerts) == 1  # one best-effort halt alert (still detected + reported)
        assert isinstance(alerts[0], TimeoutError)

    def test_on_hung_late_fire_does_not_clobber_a_newer_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A STALE on_hung (its run already superseded by a newer watchdog) must NOT
        # clear _running — the identity gate (`self._watchdog is watchdog`) is False.
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        captured: list[Callable[[WorkerEvent], None]] = []
        job = self._job_capturing_on_hung(captured)
        started = job.start(lambda _h: None, total=1)
        assert started is True
        stale_on_hung = captured[0]

        # Supersede the run: a newer watchdog now owns the job's identity, so the OLD
        # on_hung must see `self._watchdog is watchdog` as False (the ->exit branch).
        newer = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=30.0, spawn=_noop_spawn, clock=_ticking_clock()
        )
        job._watchdog = newer  # pyright: ignore[reportPrivateUsage]

        stale_on_hung(_hung_event())  # the late fire
        # The identity gate refused to touch the newer run: no stalled marker, no alert
        # side effects on the current run's tracker.
        assert job._watchdog is newer  # pyright: ignore[reportPrivateUsage]
        assert job.snapshot().stalled is False

    def test_on_hung_body_is_best_effort_and_swallows_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the halt alert raises, the whole _on_hung body is swallowed (logged) so
        # the daemon timer can never crash — the except arm of the best-effort wrap.
        def _raise(_exc: BaseException) -> None:
            raise RuntimeError("alert blew up")

        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", _raise)
        captured: list[Callable[[WorkerEvent], None]] = []
        job = self._job_capturing_on_hung(captured)
        started = job.start(lambda _h: None, total=1)
        assert started is True

        captured[0](_hung_event())  # must NOT raise out — swallowed + logged
        # The run is unaffected by the swallowed error (no crash propagated).
        assert job._watchdog is not None  # pyright: ignore[reportPrivateUsage]


def test_run_finally_skips_clearing_running_when_superseded() -> None:
    """``_run``'s finally identity gate: a worker whose watchdog was superseded mid-run
    must NOT clear ``_running`` (the ``self._watchdog is watchdog`` ->exit branch)."""
    captured: list[Callable[[], None]] = []
    newer = PullWatchdog(
        on_hung=lambda _e: None, timeout_seconds=30.0, spawn=_noop_spawn, clock=_ticking_clock()
    )

    def factory(_on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
        return PullWatchdog(
            on_hung=_on_hung, timeout_seconds=30.0, spawn=_noop_spawn, clock=_ticking_clock()
        )

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=captured.append,  # capture the worker so we can supersede before it runs
        watchdog_factory=factory,
    )
    started = job.start(lambda _h: None, total=1)
    assert started is True
    job._running = True  # pyright: ignore[reportPrivateUsage]
    # Supersede THIS run's watchdog before draining the captured worker, so the
    # worker's finally sees `self._watchdog is watchdog` as False (->exit).
    job._watchdog = newer  # pyright: ignore[reportPrivateUsage]
    captured[0]()  # drain the worker → its finally runs the identity gate
    assert job._running is True  # pyright: ignore[reportPrivateUsage]  # NOT cleared (superseded)


def test_beat_is_a_noop_when_no_watchdog_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_beat`` short-circuits at the UNLOCKED ``self._watchdog is None`` fast exit
    before any ``start`` built a watchdog — paying NO contextvars scan and NO job-lock
    acquire (the restored pre-gate short-circuit, review fold round 3): a raising
    ``current_pull_id`` stand-in proves the scan is never reached."""
    job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=_sync_spawn)
    assert job._watchdog is None  # pyright: ignore[reportPrivateUsage]

    def _must_not_scan() -> str:
        raise AssertionError("the no-watchdog fast exit must skip the contextvars scan")

    monkeypatch.setattr(pull_jobs, "current_pull_id", _must_not_scan)
    job._beat()  # pyright: ignore[reportPrivateUsage]  # must not raise — the fast exit


def test_beat_lost_race_with_identity_rollback_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-lock ``watchdog is None`` re-check: the unlocked fast exit saw a watchdog,
    but a refused ``start()``'s identity rollback restored a ``None`` pre-claim token
    (a FRESH job's refusal — the rollback RESTORES, review fold NIT-2) before the locked
    snapshot — simulated deterministically by nulling it inside the contextvars scan.
    The beat is a silent no-op, never an ``AttributeError`` on ``None``."""
    job = _job()
    assert job.start(lambda _h: None, total=1) is True  # leaves the retained token
    assert job._watchdog is not None  # pyright: ignore[reportPrivateUsage]

    def _null_token_and_return_empty() -> str:
        job._watchdog = None  # pyright: ignore[reportPrivateUsage] — the rollback landing mid-_beat
        return ""

    monkeypatch.setattr(pull_jobs, "current_pull_id", _null_token_and_return_empty)
    job._beat()  # pyright: ignore[reportPrivateUsage]  # must not raise — locked None snapshot


def test_set_phase_passthrough_reaches_the_snapshot() -> None:
    """``BackgroundPullJob.set_phase`` delegates to the tracker so the phase reaches the
    snapshot the /pull-progress route serializes (DEC-UNIVERSE-PER-PHASE-PROGRESS-001)."""
    job = _job()
    assert job.snapshot().phase == ""  # idle default
    job.set_phase("spot")
    assert job.snapshot().phase == "spot"
    assert job.snapshot().to_dict()["phase"] == "spot"
    job.set_phase("historical")  # a later transition overwrites it
    assert job.snapshot().phase == "historical"


# ── the honest completion headline (DEC-PIPELINE-AUTOPREBUILD-001 chunk 3) ─────


class TestFinishDetailFromWorkNote:
    """The work's returned report note becomes the finish detail — never a bare
    "done" hiding a quarantined symbol; every other return shape keeps "done"."""

    def test_mapping_with_note_becomes_the_finish_detail(self) -> None:
        note = "pipeline complete — 1 symbol(s) quarantined: NIFTY — spot clean"
        job = _job()
        assert job.start(lambda _h: {"note": note, "ok": True}, total=1) is True
        snap = job.snapshot()
        assert snap.state == STATE_DONE
        assert snap.detail == note

    def test_none_return_keeps_done(self) -> None:
        job = _job()
        assert job.start(lambda _h: None, total=1) is True
        assert job.snapshot().detail == "done"

    def test_non_mapping_return_keeps_done(self) -> None:
        job = _job()
        assert job.start(lambda _h: 42, total=1) is True
        assert job.snapshot().detail == "done"

    def test_mapping_without_note_keeps_done(self) -> None:
        job = _job()
        assert job.start(lambda _h: {"ok": True}, total=1) is True
        assert job.snapshot().detail == "done"

    def test_empty_or_whitespace_note_keeps_done(self) -> None:
        job = _job()
        assert job.start(lambda _h: {"note": "   "}, total=1) is True
        assert job.snapshot().detail == "done"

    def test_non_string_note_keeps_done(self) -> None:
        job = _job()
        assert job.start(lambda _h: {"note": 7}, total=1) is True
        assert job.snapshot().detail == "done"

    def test_oversized_note_is_truncated_defensively(self) -> None:
        job = _job()
        huge = "q" * 5_000
        assert job.start(lambda _h: {"note": huge}, total=1) is True
        detail = job.snapshot().detail
        assert detail == "q" * 500  # _FINISH_DETAIL_MAX
        assert len(detail) == 500

    def test_failed_run_ignores_the_note_path(self) -> None:
        # A raising work never reaches the finish-detail branch — the error
        # detail wins (byte-identical to before).
        job = _job()

        def work(_h: ProgressHook) -> object:
            raise RuntimeError("boom")

        assert job.start(work, total=1) is True
        snap = job.snapshot()
        assert snap.state == STATE_ERROR
        assert snap.detail == "RuntimeError: boom"


# ────────────────────────────────────────────────────────────────────────
# DEC-PIPELINE-STALL-HEARTBEAT-001: the recovery edge + the pair-progress
# per-pair advance (the frozen-counter fix's job-layer branch).
# ────────────────────────────────────────────────────────────────────────


def _recovery_event() -> WorkerEvent:
    return WorkerEvent(
        worker_name="dashboard-pull",
        new_state=WorkerState.HEALTHY,
        last_beat_seconds_ago=1.0,
        timeout_seconds=30.0,
    )


class TestOnRecovered:
    """``BackgroundPullJob._on_recovered``: the HUNG→HEALTHY edge clears the marker."""

    def _job_and_watchdogs(self) -> tuple[BackgroundPullJob, list[PullWatchdog]]:
        built: list[PullWatchdog] = []

        def factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
            wd = PullWatchdog(
                on_hung=on_hung, timeout_seconds=30.0, spawn=_noop_spawn, clock=_ticking_clock()
            )
            built.append(wd)
            return wd

        def _capture_only(_fn: Callable[[], None]) -> None:
            """Capture-only spawner: the worker stays in flight."""

        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_capture_only,
            watchdog_factory=factory,
        )
        return job, built

    def test_recovery_clears_the_stalled_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        job, built = self._job_and_watchdogs()
        assert job.start(lambda _h: None, total=1) is True
        job._on_hung(_hung_event(), built[0])  # pyright: ignore[reportPrivateUsage]
        assert job.snapshot().stalled is True
        # start() wired the watchdog's on_recovery to _on_recovered — fire it.
        assert built[0].on_recovery is not None
        built[0].on_recovery(_recovery_event())
        assert job.snapshot().stalled is False
        assert job.running is True  # the run itself was never touched

    def test_stale_recovery_for_superseded_run_is_gated_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)
        job, built = self._job_and_watchdogs()
        job.start(lambda _h: None, total=1)
        stale = built[0]
        # Supersede the run identity: a newer watchdog owns the job now.
        newer = PullWatchdog(
            on_hung=lambda _e: None, timeout_seconds=30.0, spawn=_noop_spawn, clock=_ticking_clock()
        )
        job._watchdog = newer  # pyright: ignore[reportPrivateUsage]
        job._on_hung(_hung_event(), newer)  # pyright: ignore[reportPrivateUsage]
        assert job.snapshot().stalled is True
        # The STALE run's recovery edge must NOT clear the current run's marker.
        job._on_recovered(_recovery_event(), stale)  # pyright: ignore[reportPrivateUsage]
        assert job.snapshot().stalled is True

    def test_on_recovered_swallows_a_raising_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _e: None)

        class _BoomProgress(PullProgress):
            def clear_stalled(self) -> None:
                raise RuntimeError("clear boom")

        built: list[PullWatchdog] = []

        def factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
            wd = PullWatchdog(
                on_hung=on_hung, timeout_seconds=30.0, spawn=_noop_spawn, clock=_ticking_clock()
            )
            built.append(wd)
            return wd

        job = BackgroundPullJob(
            progress=_BoomProgress(clock=_ticking_clock()),
            spawn=lambda _fn: None,
            watchdog_factory=factory,
        )
        job.start(lambda _h: None, total=1)
        # Must NOT raise out of the best-effort recovery body (swallowed + logged).
        job._on_recovered(_recovery_event(), built[0])  # pyright: ignore[reportPrivateUsage]
        assert job.running is True


def test_pair_progress_phase_advances_open_ended_counters() -> None:
    """The ledger-synced + pair_progress branch of ``_on_progress`` (frozen-counter fix)."""
    job = BackgroundPullJob(progress=PullProgress(clock=_ticking_clock()), spawn=lambda fn: fn())

    def work(on_progress: ProgressHook) -> None:
        job.sync_from_ledger(completed=45, total=45, bars=None)  # the metadata freeze shape
        job.set_phase("historical", pair_progress=True)
        on_progress("NSE-NIFTY-25Jun26-FUT 1minute", 1_000, False, False)
        on_progress("NSE-NIFTY-25Jun26-22000-CE 1minute", 500, True, False)
        snap = job.snapshot()
        assert snap.completed == 47 and snap.total == 47  # units MOVE past 45/45
        assert snap.bars == 1_500  # cumulative bars — never frozen at 0
        assert snap.failed == 1

    assert job.start(work, total=45, ledger_synced=True) is True
    assert job.snapshot().state == STATE_DONE


# ════════════════════════════════════════════════════════════════════════
# Durable pull-journal watchdog heartbeat (DEC-PULL-TELEMETRY-TRIAD-001) —
# wiring the previously call-site-less ``record_pull_heartbeat``.
# ════════════════════════════════════════════════════════════════════════


class TestJournalHeartbeat:
    """The throttled durable heartbeat: O(1), best-effort, never affects the pull."""

    @staticmethod
    def _manual_clock(values: list[float]) -> Callable[[], float]:
        """A scripted journal clock (one value per throttle probe, in order)."""

        def clock() -> float:
            return values.pop(0)

        return clock

    @staticmethod
    def _job(tmp_path: object, clock: Callable[[], float], interval: float) -> BackgroundPullJob:
        return BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_sync_spawn,
            lake_root_resolver=lambda: tmp_path,  # type: ignore[arg-type, return-value]
            journal_heartbeat_interval_s=interval,
            journal_clock=clock,
        )

    def test_heartbeat_written_once_per_interval_with_bound_pull_id(self, tmp_path: object) -> None:
        """Three beats inside one run → the first + the post-interval one journal; the
        mid-interval one is throttled. Each record carries the BOUND pull_id."""
        from pathlib import Path

        from brutex.observability.pull_journal import EVENT_HEARTBEAT, read_journal

        root = Path(str(tmp_path))
        job = self._job(root, self._manual_clock([0.0, 50.0, 150.0]), 100.0)

        def work(on_progress: ProgressHook) -> None:
            on_progress("NSE-NIFTY 1minute", 10, False, False)  # t=0 → writes
            on_progress("NSE-NIFTY 1minute", 10, False, False)  # t=50 → throttled
            on_progress("NSE-NIFTY 1minute", 10, False, False)  # t=150 → writes

        # The RUN id is the bound id (DEC-PULL-BEAT-PERRUN-001 fix round: a beat under a
        # FOREIGN id is now dropped by the _beat ambient gate, so the test binds the id
        # the sanctioned way — through start()).
        assert job.start(work, total=3, pull_id="abc123def456") is True
        assert job.snapshot().state == STATE_DONE  # PARITY: the pull is unaffected
        beats = [e for e in read_journal(root) if e.event == EVENT_HEARTBEAT]
        assert len(beats) == 2
        assert {b.pull_id for b in beats} == {"abc123def456"}

    def test_in_run_beat_without_explicit_scope_journals_under_run_id(
        self, tmp_path: object
    ) -> None:
        """RE-PINNED (DEC-PULL-BEAT-PERRUN-001, was ``test_unbound_pull_id_journals_nothing``):
        ``_run`` now binds the run's ``pull_id_scope``, so a beat inside the work with NO
        explicit scope is no longer unbound — the durable heartbeat records the RUN's id
        (the ``if not pull_id`` skip no longer fires during the run's own phases)."""
        from pathlib import Path

        from brutex.observability.pull_journal import EVENT_HEARTBEAT, read_journal

        root = Path(str(tmp_path))
        job = self._job(root, self._manual_clock([0.0, 1.0, 2.0]), 0.5)

        def work(on_progress: ProgressHook) -> None:
            on_progress("NSE-NIFTY 1minute", 10, False, False)

        assert job.start(work, total=1) is True
        beats = [e for e in read_journal(root) if e.event == EVENT_HEARTBEAT]
        assert beats  # the post-fetch-phase beat is now durably attributed
        assert {b.pull_id for b in beats} == {job.pull_id}
        assert len(job.pull_id) == 12  # the minted run id, never ""

    def test_unbound_pull_id_journals_nothing(self, tmp_path: object) -> None:
        """A beat from a context with NO bound ``pull_id`` (a stale between-runs beat on
        a foreign thread — nothing could group it) still skips the durable write — a
        missing heartbeat is a weaker signal, never a wrong one."""
        from pathlib import Path

        from brutex.observability.pull_journal import read_journal

        root = Path(str(tmp_path))
        job = self._job(root, self._manual_clock([0.0]), 0.5)
        # Whitebox: drive the throttle body directly OUTSIDE any pull_id_scope (the
        # only production shape left with no bound id — _run now always binds one).
        job._maybe_journal_heartbeat()  # pyright: ignore[reportPrivateUsage]
        assert read_journal(root) == []

    def test_none_lake_root_journals_nothing(self, tmp_path: object) -> None:
        """A ``None`` resolved lake root (dry-run / test) skips the write gracefully."""
        from pathlib import Path

        from brutex.observability.pull_journal import read_journal

        root = Path(str(tmp_path))
        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_sync_spawn,
            lake_root_resolver=lambda: None,
            journal_heartbeat_interval_s=1.0,
            journal_clock=self._manual_clock([0.0]),
        )

        def work(on_progress: ProgressHook) -> None:
            on_progress("NSE-NIFTY 1minute", 10, False, False)

        assert job.start(work, total=1, pull_id="abc123def456") is True
        assert job.snapshot().state == STATE_DONE
        assert read_journal(root) == []  # nothing written anywhere under tmp

    def test_write_failure_never_affects_the_pull(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising journal writer is swallowed + logged; the pull finishes DONE with
        the SAME progress it would have had (the Session-B best-effort parity pin)."""
        from pathlib import Path

        from brutex.observability import pull_journal
        from structlog.testing import capture_logs

        def _boom(*_a: object, **_kw: object) -> None:
            raise RuntimeError("journal disk wedged")

        monkeypatch.setattr(pull_journal, "record_pull_heartbeat", _boom)
        root = Path(str(tmp_path))
        job = self._job(root, self._manual_clock([0.0]), 1.0)

        def work(on_progress: ProgressHook) -> None:
            on_progress("NSE-NIFTY 1minute", 42, False, False)

        with capture_logs() as logs:
            assert job.start(work, total=1, pull_id="abc123def456") is True
        snap = job.snapshot()
        assert snap.state == STATE_DONE  # the pull is UNAFFECTED
        assert snap.bars == 42
        assert any(
            e["event"] == "pull_job.journal_heartbeat failed; the pull is unaffected" for e in logs
        )

    def test_heartbeats_group_into_last_run_forensics(self, tmp_path: object) -> None:
        """The journaled heartbeat carries the run's pull_id, so ``last_run`` groups it
        with the ``started`` record — the post-mortem 'the run was alive' evidence."""
        from pathlib import Path

        from brutex.observability.pull_journal import last_run, read_journal, record_pull_start

        root = Path(str(tmp_path))
        record_pull_start(root, pull_id="abc123def456", dry_run=False)
        job = self._job(root, self._manual_clock([0.0]), 100.0)

        def work(on_progress: ProgressHook) -> None:
            on_progress("NSE-NIFTY 1minute", 10, False, False)

        assert job.start(work, total=1, pull_id="abc123def456") is True
        run = last_run(read_journal(root))
        assert run is not None
        assert len(run.heartbeats) == 1
        assert run.interrupted is True  # start + heartbeat, no finish (died-mid-run shape)

    def test_stale_beat_between_runs_journals_nothing(self, tmp_path: object) -> None:
        """A beat BEFORE the first start (the only shape where ``_watchdog is None``)
        exits at the unlocked fast path and never journals. The between-RUNS shape —
        the identity token retained after a finished run — is pinned separately below."""
        from pathlib import Path

        from brutex.observability.correlation import pull_id_scope
        from brutex.observability.pull_journal import read_journal

        root = Path(str(tmp_path))
        job = self._job(root, self._manual_clock([0.0]), 1.0)
        with pull_id_scope("abc123def456"):
            job._beat()  # pyright: ignore[reportPrivateUsage] — whitebox: no run active
        assert read_journal(root) == []

    def test_finished_run_bound_stale_beat_journals_nothing_between_runs(
        self, tmp_path: object
    ) -> None:
        """REGRESSION (review fold, round 3): the finished run's identity token
        (``_watchdog`` + ``_pull_id``) is deliberately RETAINED after ``_run``'s
        ``finally``, so a drain-abandoned thread still bound to the FINISHED run's id
        passes ``_beat``'s ambient gate between runs — it must inertly beat only the
        already-STOPPED watchdog and journal NOTHING (a durable heartbeat landing
        AFTER the run's finish record is forensic noise, not liveness)."""
        from pathlib import Path

        from brutex.observability.correlation import pull_id_scope
        from brutex.observability.pull_journal import EVENT_HEARTBEAT, read_journal

        root = Path(str(tmp_path))
        # ONE scripted clock value — consumed by the in-run beat; a between-runs
        # journal attempt would pop the exhausted script and fail LOUDLY.
        job = self._job(root, self._manual_clock([0.0]), 100.0)

        def work(on_progress: ProgressHook) -> None:
            on_progress("NSE-NIFTY 1minute", 10, False, False)  # t=0 → journals

        assert job.start(work, total=1, pull_id="abc123def456") is True
        assert job.running is False  # finished — yet the identity token is retained:
        assert job._watchdog is not None  # pyright: ignore[reportPrivateUsage]
        assert job.pull_id == "abc123def456"
        with pull_id_scope("abc123def456"):  # the zombie still bound to the FINISHED id
            job._beat()  # pyright: ignore[reportPrivateUsage] — gate passes, running=False
        beats = [e for e in read_journal(root) if e.event == EVENT_HEARTBEAT]
        assert len(beats) == 1  # ONLY the in-run heartbeat — nothing after the finish


# ── the on_finally completion hook (DEC-PULL-INTENT-RESUME-001) ───────────────


class _OrderGuard:
    """A single-flight guard double appending "released" to a shared order log."""

    def __init__(self, order: list[str]) -> None:
        self._order = order

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        self._order.append("released")


class TestOnFinally:
    """`start(on_finally=…)` — the durable pull-intent clear hook: fires on the
    worker's success AND failure, inside the run-identity gate, strictly BEFORE
    the cross-process single-flight release; best-effort (a raising hook is
    swallowed and the release still happens); `None` is byte-identical."""

    def test_fires_once_on_success(self) -> None:
        job = _job()
        fired: list[str] = []
        started = job.start(lambda _h: None, total=1, on_finally=lambda: fired.append("f"))
        assert started is True
        assert job.snapshot().state == STATE_DONE
        assert fired == ["f"]

    def test_fires_once_on_failure(self) -> None:
        job = _job()
        fired: list[str] = []

        def work(_on_progress: ProgressHook) -> None:
            raise RuntimeError("boom")

        started = job.start(work, total=1, on_finally=lambda: fired.append("f"))
        assert started is True
        assert job.snapshot().state == STATE_ERROR
        assert fired == ["f"], "the intent clear must fire on failure too (worker exited)"

    def test_none_hook_is_byte_identical(self) -> None:
        job = _job()
        assert job.start(lambda _h: None, total=1, on_finally=None) is True
        assert job.snapshot().state == STATE_DONE
        assert job.running is False

    def test_fires_before_singleflight_release(self, tmp_path: Path) -> None:
        # Trap #5 ordering: a peer process acquiring the freed flock must never
        # see a live intent — the hook runs BEFORE the guard's __exit__.
        order: list[str] = []
        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_sync_spawn,
            lake_root_resolver=lambda: tmp_path,
            singleflight_factory=lambda _root: _OrderGuard(order),
        )
        started = job.start(lambda _h: None, total=1, on_finally=lambda: order.append("finally"))
        assert started
        assert order == ["finally", "released"]

    def test_raising_hook_is_swallowed_and_release_continues(self, tmp_path: Path) -> None:
        order: list[str] = []

        def _boom_hook() -> None:
            order.append("finally")
            raise RuntimeError("clear exploded")

        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_sync_spawn,
            lake_root_resolver=lambda: tmp_path,
            singleflight_factory=lambda _root: _OrderGuard(order),
        )
        assert job.start(lambda _h: None, total=1, on_finally=_boom_hook) is True
        assert order == ["finally", "released"], "the release still happens after a raising hook"
        assert job.running is False
        # The job is reusable — a fresh start succeeds after the raising hook.
        assert job.start(lambda _h: None, total=1) is True

    def test_superseded_run_does_not_fire_on_finally(self) -> None:
        # The hook sits INSIDE the identity gate: a stale worker whose watchdog
        # was superseded must never clear a NEWER run's intent.
        captured: list[Callable[[], None]] = []
        fired: list[str] = []
        newer = PullWatchdog(
            on_hung=lambda _e: None,
            timeout_seconds=30.0,
            spawn=_noop_spawn,
            clock=_ticking_clock(),
        )

        def factory(_on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
            return PullWatchdog(
                on_hung=_on_hung,
                timeout_seconds=30.0,
                spawn=_noop_spawn,
                clock=_ticking_clock(),
            )

        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=captured.append,
            watchdog_factory=factory,
        )
        started = job.start(lambda _h: None, total=1, on_finally=lambda: fired.append("f"))
        assert started is True
        job._running = True  # pyright: ignore[reportPrivateUsage]
        job._watchdog = newer  # pyright: ignore[reportPrivateUsage]
        captured[0]()  # drain the stale worker → its finally hits the ->exit branch
        assert fired == [], "a superseded worker must not fire the newer run's clear"


# ── the on_run_end per-JOB completion hook (DEC-RATE-METER-LIFECYCLE-001) ──────


class TestOnRunEnd:
    """`BackgroundPullJob(on_run_end=…)` — the per-JOB run-end hook (the production
    wiring is ``SessionRateController.clear_meter``, the #1544 C0 cross-pull meter-leak
    fix): fires ONCE per run — success AND failure — inside the identity-gated finally,
    strictly AFTER ``on_finally`` and strictly BEFORE the single-flight release, in its
    OWN try/except; never on a refused start; never from a superseded stale worker."""

    def test_on_run_end_fires_on_success_and_on_failure(self) -> None:
        # MUTANT KILLED: the `_run`-finally `self._on_run_end()` invocation severed →
        # neither run fires the hook.
        fired: list[str] = []
        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_sync_spawn,
            on_run_end=lambda: fired.append("end"),
        )
        assert job.start(lambda _h: None, total=1) is True
        assert job.snapshot().state == STATE_DONE
        assert fired == ["end"], "the hook fires exactly once on a successful run"
        assert job.running is False

        def failing(_on_progress: ProgressHook) -> None:
            raise RuntimeError("boom")

        assert job.start(failing, total=1) is True
        assert job.snapshot().state == STATE_ERROR
        assert fired == ["end", "end"], "the hook fires on a FAILED run too (worker exited)"
        assert job.running is False

    def test_on_run_end_raising_swallowed_and_singleflight_released(self, tmp_path: Path) -> None:
        # MUTANT KILLED: the hook's OWN try/except removed → the raise would escape
        # the locked finally and skip the single-flight release below it.
        order: list[str] = []

        def _boom_end() -> None:
            order.append("run_end")
            raise RuntimeError("clear exploded")

        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_sync_spawn,
            lake_root_resolver=lambda: tmp_path,
            singleflight_factory=lambda _root: _OrderGuard(order),
            on_run_end=_boom_end,
        )
        assert job.start(lambda _h: None, total=1) is True
        assert order == ["run_end", "released"], "the release still happens after a raising hook"
        assert job.running is False
        # The job is reusable — a fresh start succeeds after the raising hook.
        assert job.start(lambda _h: None, total=1) is True

    def test_on_run_end_fires_after_on_finally_and_before_release(self, tmp_path: Path) -> None:
        # The documented ordering contract: on_finally → on_run_end → flock release
        # (a peer acquiring the freed flock must never see the pre-clear state).
        order: list[str] = []
        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_sync_spawn,
            lake_root_resolver=lambda: tmp_path,
            singleflight_factory=lambda _root: _OrderGuard(order),
            on_run_end=lambda: order.append("run_end"),
        )
        started = job.start(lambda _h: None, total=1, on_finally=lambda: order.append("finally"))
        assert started is True
        assert order == ["finally", "run_end", "released"]

    def test_on_finally_raising_does_not_skip_on_run_end(self, tmp_path: Path) -> None:
        # MUTANT KILLED: the two hooks merged into ONE shared try/except → a raising
        # on_finally would jump past the on_run_end call (the meter clear skipped).
        order: list[str] = []

        def _boom_finally() -> None:
            order.append("finally")
            raise RuntimeError("intent clear exploded")

        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=_sync_spawn,
            lake_root_resolver=lambda: tmp_path,
            singleflight_factory=lambda _root: _OrderGuard(order),
            on_run_end=lambda: order.append("run_end"),
        )
        assert job.start(lambda _h: None, total=1, on_finally=_boom_finally) is True
        assert order == ["finally", "run_end", "released"], (
            "a raising on_finally must not skip the meter clear nor the release"
        )

    def test_on_run_end_not_fired_on_refused_start(self) -> None:
        # A refused single-flight start spawns NO worker → no run ends → no fire.
        fired: list[str] = []
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=captured.append,
            on_run_end=lambda: fired.append("end"),
        )
        assert job.start(lambda _h: None, total=1) is True
        assert job.start(lambda _h: None, total=1) is False  # single-flight refusal
        assert fired == [], "a refused start must not fire the run-end hook"
        captured[0]()  # drain the one real worker
        assert fired == ["end"], "the real run's exit fires it exactly once"

    def test_superseded_run_does_not_fire_on_run_end(self) -> None:
        # MUTANT KILLED: the hook moved OUTSIDE the `self._watchdog is watchdog`
        # identity gate → a stale worker would clear a NEWER run's meter.
        captured: list[Callable[[], None]] = []
        fired: list[str] = []
        newer = PullWatchdog(
            on_hung=lambda _e: None,
            timeout_seconds=30.0,
            spawn=_noop_spawn,
            clock=_ticking_clock(),
        )

        def factory(_on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
            return PullWatchdog(
                on_hung=_on_hung,
                timeout_seconds=30.0,
                spawn=_noop_spawn,
                clock=_ticking_clock(),
            )

        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=captured.append,
            watchdog_factory=factory,
            on_run_end=lambda: fired.append("end"),
        )
        assert job.start(lambda _h: None, total=1) is True
        job._running = True  # pyright: ignore[reportPrivateUsage]
        job._watchdog = newer  # pyright: ignore[reportPrivateUsage]
        captured[0]()  # drain the stale worker → its finally hits the ->exit branch
        assert fired == [], "a superseded worker must not clear the newer run's meter"


# ── the job-kind label + running_job_kind probe (DEC-RATE-METER-LIFECYCLE-001) ─


class TestRunningJobKind:
    """`start(job_kind=…)` + the atomic ``running_job_kind`` property — the C5 fix:
    the rate route's probe distinguishes a vendor PULL from the zero-vendor-call
    SWEEP sharing the single-flight job; idle reads ``None``; a refused start never
    overwrites the running job's kind."""

    def test_running_job_kind_lifecycle(self) -> None:
        # MUTANTS KILLED: `self._job_kind = job_kind` severed in start (a sweep would
        # read "pull"); the property ignoring `_running` (idle would read a kind);
        # the property returning None while running.
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=captured.append,
        )
        assert job.running_job_kind is None, "a never-started job reads None (no AttributeError)"

        assert job.start(lambda _h: None, total=1) is True
        assert job.running_job_kind == pull_jobs.JOB_KIND_PULL, "the default start is pull-kind"
        captured.pop()()  # drain → idle
        assert job.running_job_kind is None, "an idle job reads None, never a stale kind"

        assert job.start(lambda _h: None, total=1, job_kind=pull_jobs.JOB_KIND_SWEEP) is True
        assert job.running_job_kind == pull_jobs.JOB_KIND_SWEEP
        captured.pop()()
        assert job.running_job_kind is None

    def test_refused_start_does_not_overwrite_running_kind(self) -> None:
        # MUTANT KILLED: `self._job_kind = job_kind` moved BEFORE the `if
        # self._running: return False` refusal → a refused sweep start would
        # relabel the RUNNING pull as a sweep (the rate widget would go dark
        # mid-pull — a fake "idle" during a live vendor pull).
        captured: list[Callable[[], None]] = []
        job = BackgroundPullJob(
            progress=PullProgress(clock=_ticking_clock()),
            spawn=captured.append,
        )
        assert job.start(lambda _h: None, total=1) is True  # a PULL is running
        refused = job.start(lambda _h: None, total=1, job_kind=pull_jobs.JOB_KIND_SWEEP)
        assert refused is False
        assert job.running_job_kind == pull_jobs.JOB_KIND_PULL, (
            "the refused sweep start must not relabel the running pull"
        )
        captured[0]()
        assert job.running_job_kind is None
