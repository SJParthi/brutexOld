"""Cross-process single-flight wiring in `BackgroundPullJob` (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).

100% line+branch on the NEW branches added to ``pull_jobs.py``:

* the account-wide guard is taken on ``start`` and RELEASED on a clean finish (so a
  re-pull succeeds) and on an exception finish;
* a held lock (peer process mid-pull) REFUSES the second pull — ``start`` returns
  ``False``, rolls back the in-process ``_running`` flag, marks the progress failed, and
  logs — never spawning work that would double the Groww account rate (Golden Rule #6);
* a ``None`` lake-root resolver DISABLES the guard (dry-run / test);
* ``_on_hung`` releases the held lock so a wedged run still admits a future pull.

The cross-process lock itself is exercised with the REAL
:func:`~brutex.adapters.groww.pull_singleflight.pull_singleflight_lock` against a tmp
lake root (a separate fd in-process proves mutual exclusion), plus a capturing fake for
the deterministic lifecycle assertions.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import pytest
from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    pull_singleflight_lock,
    pull_singleflight_lock_path,
)
from brutex.dashboard.pull_jobs import (
    BackgroundPullJob,
    ProgressHook,
    _default_lake_root_resolver,  # pyright: ignore[reportPrivateUsage]
)
from brutex.dashboard.pull_progress import STATE_DONE, STATE_ERROR, PullProgress
from brutex.dashboard.pull_stall import PullWatchdog
from brutex.observability.watchdog import WorkerEvent, WorkerState


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
    fn()


def _capture_spawn(captured: list[Callable[[], None]]) -> Callable[[Callable[[], None]], None]:
    return captured.append


class _SpyGuard(AbstractContextManager[None]):
    """A capturing single-flight guard double recording enter/exit + raising on demand."""

    def __init__(self, *, raise_on_enter: bool = False, lock_path: Path | None = None) -> None:
        self.entered = 0
        self.exited = 0
        self._raise_on_enter = raise_on_enter
        self._lock_path = lock_path if lock_path is not None else Path("/tmp/x.lock")

    def __enter__(self) -> None:
        self.entered += 1
        if self._raise_on_enter:
            raise PullAlreadyRunningError(self._lock_path)

    def __exit__(self, *exc: object) -> None:
        self.exited += 1


def test_default_lake_root_resolver_delegates_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The production default resolver delegates to the COMMON config resolver
    # (BRUTEX_LAKE_ROOT → ~/.brutex/lake), so the guard is ACTIVE by default.
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path / "lake"))
    assert _default_lake_root_resolver() == tmp_path / "lake"


def test_default_guard_is_active_and_held_for_the_run(tmp_path: Path) -> None:
    # Default resolver injected at a tmp lake root + the REAL flock factory: while the
    # job holds the lock, a SEPARATE fd (a peer process) cannot acquire it.
    captured: list[Callable[[], None]] = []
    held_during_run: dict[str, bool] = {}

    def work(_on_progress: ProgressHook) -> None:
        try:
            with pull_singleflight_lock(tmp_path):  # pragma: no cover — must NOT be reached
                held_during_run["peer_acquired"] = True
        except PullAlreadyRunningError:
            held_during_run["peer_acquired"] = False

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_capture_spawn(captured),
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=pull_singleflight_lock,
    )
    started = job.start(work, total=1)
    assert started is True
    # The lock file exists (acquired) while the captured worker has not yet run.
    assert pull_singleflight_lock_path(tmp_path).exists()
    captured[0]()  # run the worker now — it must find the lock HELD
    assert held_during_run["peer_acquired"] is False
    # After a clean finish the lock is released → a fresh acquire succeeds.
    with pull_singleflight_lock(tmp_path):
        pass


def test_held_lock_refuses_second_pull_and_rolls_back(tmp_path: Path) -> None:
    spy = _SpyGuard(raise_on_enter=True, lock_path=pull_singleflight_lock_path(tmp_path))
    spawned: list[Callable[[], None]] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=spawned.append,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: spy,
    )
    started = job.start(lambda _h: None, total=1)
    assert started is False  # refused
    assert spawned == []  # NO work spawned — never doubles the account rate
    assert job.running is False  # in-process flag rolled back
    snap = job.snapshot()
    assert snap.state == STATE_ERROR
    assert "PullAlreadyRunningError" in snap.detail
    assert spy.entered == 1
    assert spy.exited == 0  # never entered the block, so nothing to exit


def test_refused_then_idle_can_start_again(tmp_path: Path) -> None:
    # After a refusal the job is idle and a later start (lock now free) succeeds.
    spy_refuse = _SpyGuard(raise_on_enter=True, lock_path=pull_singleflight_lock_path(tmp_path))
    spy_ok = _SpyGuard()
    factories: list[_SpyGuard] = [spy_refuse, spy_ok]
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: factories.pop(0),
    )
    assert job.start(lambda _h: None, total=1) is False
    assert job.start(lambda on_progress: on_progress("X", 1, False, False), total=1) is True
    assert job.snapshot().state == STATE_DONE
    assert spy_ok.entered == 1
    assert spy_ok.exited == 1  # released on clean finish


def test_guard_released_on_worker_exception(tmp_path: Path) -> None:
    spy = _SpyGuard()

    def boom(_on_progress: ProgressHook) -> None:
        raise RuntimeError("kaboom")

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: spy,
    )
    started = job.start(boom, total=1)
    assert started is True
    assert job.snapshot().state == STATE_ERROR
    assert spy.entered == 1
    assert spy.exited == 1  # released in _run's finally even on failure
    assert job.running is False


def test_none_resolver_disables_the_guard(tmp_path: Path) -> None:
    # A None lake root → guard skipped entirely (factory never called), pull runs.
    calls: list[Path] = []

    def factory(root: Path) -> AbstractContextManager[None]:  # pragma: no cover — never called
        calls.append(root)
        return _SpyGuard()

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: None,
        singleflight_factory=factory,
    )
    started = job.start(lambda on_progress: on_progress("X", 1, False, False), total=1)
    assert started is True
    assert job.snapshot().state == STATE_DONE
    assert calls == []  # the factory was never consulted


def test_release_is_best_effort_and_swallows_errors(tmp_path: Path) -> None:
    # A guard whose __exit__ raises must not crash the worker; the run still completes,
    # _running is cleared, and the error is swallowed (logged).
    class _BadExitGuard(AbstractContextManager[None]):
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> None:
            raise OSError("close failed")

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _BadExitGuard(),
    )
    started = job.start(lambda on_progress: on_progress("X", 1, False, False), total=1)
    assert started is True
    assert job.snapshot().state == STATE_DONE
    assert job.running is False


def _noop_spawn(_fn: Callable[[], None]) -> None:
    """A timer-spawner double that never starts the watchdog's daemon timer."""


def _hung_event() -> WorkerEvent:
    return WorkerEvent(
        worker_name="dashboard-pull",
        new_state=WorkerState.HUNG,
        last_beat_seconds_ago=120.0,
        timeout_seconds=30.0,
    )


def test_on_hung_holds_the_guard_until_worker_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DEC-PIPELINE-STALL-HEARTBEAT-001 (supersedes the release-on-hung pin): the watchdog
    # HUNG edge must NOT release the account lock while the worker thread may still be
    # alive — releasing admitted a SECOND pull concurrent with the still-running zombie
    # worker (the forensically-proven double-run race on the same lake). The guard is
    # released ONLY when the worker's own finally runs (the thread actually exits).
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", lambda _exc: None)
    spy = _SpyGuard()
    captured: list[Callable[[WorkerEvent], None]] = []

    def factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
        captured.append(on_hung)
        return PullWatchdog(
            on_hung=on_hung, timeout_seconds=30.0, spawn=_noop_spawn, clock=_ticking_clock()
        )

    workers: list[Callable[[], None]] = []

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=workers.append,  # capture-only: the worker stays "alive" until we run it
        watchdog_factory=factory,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: spy,
    )
    started = job.start(lambda _h: None, total=1)
    assert started is True
    assert spy.entered == 1
    # Fire the hung edge: the guard stays HELD and _running stays True (no double-run).
    captured[0](_hung_event())
    assert spy.exited == 0
    assert job.running is True
    assert job.snapshot().stalled is True  # the stall is still detected + reported
    # The worker thread finally exits → the guard is released exactly once, there.
    workers[0]()
    assert spy.exited == 1
    assert job.running is False


# ════════════════════════════════════════════════════════════════════════
# Account-keyed second lock wiring (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001)
# ════════════════════════════════════════════════════════════════════════


class _OrderedGuard(AbstractContextManager[None]):
    """A guard double appending named enter/exit events to a SHARED order log."""

    def __init__(self, name: str, order: list[str], *, raise_on_enter: bool = False) -> None:
        self._name = name
        self._order = order
        self._raise_on_enter = raise_on_enter

    def __enter__(self) -> None:
        if self._raise_on_enter:
            raise PullAlreadyRunningError(Path("/tmp/contended.flock"))
        self._order.append(f"enter:{self._name}")

    def __exit__(self, *exc: object) -> None:
        self._order.append(f"exit:{self._name}")


def test_account_lock_taken_first_released_last(tmp_path: Path) -> None:
    """ORDER pin: account lock strictly BEFORE the lake lock; release is LIFO."""
    order: list[str] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _OrderedGuard("lake", order),
        account_singleflight_factory=lambda _key: _OrderedGuard("account", order),
        account_key_resolver=lambda: "key-A",
    )
    assert job.start(lambda on_progress: on_progress("X", 1, False, False), total=1) is True
    assert job.snapshot().state == STATE_DONE
    assert order == ["enter:account", "enter:lake", "exit:lake", "exit:account"]


def test_account_lock_contended_refuses_before_lake_lock(tmp_path: Path) -> None:
    """A held ACCOUNT lock (peer pull, possibly another lake root) refuses the pull."""
    order: list[str] = []
    lake_calls: list[Path] = []

    def lake_factory(root: Path) -> AbstractContextManager[None]:  # pragma: no cover
        lake_calls.append(root)
        return _OrderedGuard("lake", order)

    spawned: list[Callable[[], None]] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=spawned.append,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lake_factory,
        account_singleflight_factory=lambda _key: _OrderedGuard(
            "account", order, raise_on_enter=True
        ),
        account_key_resolver=lambda: "key-A",
    )
    assert job.start(lambda _h: None, total=1) is False
    assert spawned == []  # never doubles the account rate
    assert lake_calls == []  # the lake lock was never even consulted
    assert job.running is False  # in-process flag rolled back
    snap = job.snapshot()
    assert snap.state == STATE_ERROR
    assert "PullAlreadyRunningError" in snap.detail


def test_lake_contention_rolls_back_the_held_account_lock(tmp_path: Path) -> None:
    """Lake-lock contention releases the just-taken account lock (LIFO rollback)."""
    order: list[str] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _OrderedGuard("lake", order, raise_on_enter=True),
        account_singleflight_factory=lambda _key: _OrderedGuard("account", order),
        account_key_resolver=lambda: "key-A",
    )
    assert job.start(lambda _h: None, total=1) is False
    assert order == ["enter:account", "exit:account"]  # rolled back, never leaked
    assert job.running is False


def test_none_account_key_skips_account_lock_gracefully(tmp_path: Path) -> None:
    """No credentials → the account lock is SKIPPED; the lake lock still guards."""
    order: list[str] = []
    account_calls: list[str] = []

    def account_factory(key: str) -> AbstractContextManager[None]:  # pragma: no cover
        account_calls.append(key)
        return _OrderedGuard("account", order)

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _OrderedGuard("lake", order),
        account_singleflight_factory=account_factory,
        account_key_resolver=lambda: None,
    )
    assert job.start(lambda on_progress: on_progress("X", 1, False, False), total=1) is True
    assert job.snapshot().state == STATE_DONE
    assert account_calls == []  # skipped — the factory was never consulted
    assert order == ["enter:lake", "exit:lake"]


def test_cross_process_lock_false_skips_both_locks_for_a_zero_write_run(tmp_path: Path) -> None:
    """``start(cross_process_lock=False)`` takes NEITHER cross-process lock.

    DEC-GREEKS-BULK-REENRICH-001 fix round 2 (refuter-confirmed): the dashboard
    DRY-RUN Greeks backfill is a zero-write scan, yet it held the lake-root
    flock for its whole O(files) duration and REFUSED a real production pull —
    while the CLI ``--dry-run`` twin is deliberately lock-free. The zero-write
    run must not exclude a writer: neither lock factory (nor the account-key
    resolver) is consulted, a REAL peer flock on the same root acquires freely
    DURING the run, and the in-process single-flight still applies.
    """
    lake_calls: list[Path] = []
    account_calls: list[str] = []
    resolver_calls: list[bool] = []
    peer_acquired: dict[str, bool] = {}

    def lake_factory(root: Path) -> AbstractContextManager[None]:  # pragma: no cover
        lake_calls.append(root)
        return _SpyGuard()

    def account_factory(key: str) -> AbstractContextManager[None]:  # pragma: no cover
        account_calls.append(key)
        return _SpyGuard()

    def key_resolver() -> str | None:  # pragma: no cover — never called
        resolver_calls.append(True)
        return "key-A"

    def work(on_progress: ProgressHook) -> None:
        # DURING the lock-free run a peer writer (the REAL flock) acquires
        # freely — the zero-write scan excludes nobody.
        with pull_singleflight_lock(tmp_path):
            peer_acquired["ok"] = True
        on_progress("X", 1, False, False)

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lake_factory,
        account_singleflight_factory=account_factory,
        account_key_resolver=key_resolver,
    )
    assert job.start(work, total=1, cross_process_lock=False) is True
    assert job.snapshot().state == STATE_DONE
    assert peer_acquired == {"ok": True}
    assert lake_calls == [] and account_calls == [] and resolver_calls == []
    # A later DEFAULT start on the same job still takes the lock (per-run flag).
    spy_ok = _SpyGuard()
    job2 = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: spy_ok,
        account_key_resolver=lambda: None,
    )
    assert job2.start(lambda _h: None, total=1, cross_process_lock=False) is True
    assert spy_ok.entered == 0  # lock-free run: never consulted
    assert job2.start(lambda _h: None, total=1) is True  # default → locked
    assert spy_ok.entered == 1 and spy_ok.exited == 1


# ── the SWEEP job's lock posture (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001) ────────


def _sweep_shaped_job(tmp_path: Path) -> BackgroundPullJob:
    """A job composed exactly like the dashboard sweep route's ``_pull_job``:
    the REAL lake flock factory + the production no-AWS-creds graceful account
    skip (``account_key_resolver`` → None), synchronous spawn."""
    return BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=pull_singleflight_lock,
        account_key_resolver=lambda: None,
    )


def test_sweep_job_lock_free_worker_takes_the_g10_preload_guard(tmp_path: Path) -> None:
    """THE self-conflict regression (fails before) — the investigation's Repro B
    as a permanent test.

    DEC-SWEEP-LOCK-PULL-CONCURRENCY-001: a job composed exactly like the
    ``/api/v1/sweep/run`` route (``job_kind=SWEEP``, and — since the fix —
    ``cross_process_lock=False``) whose worker performs ``cmd_sweep_all``'s
    verbatim preload step: the REAL G10 ``lake_read_guard`` ``LOCK_SH`` on the
    job's OWN lake. The guard must ACQUIRE (no ``LakeBusyError``) and the job
    must finish DONE. BEFORE the fix the route left ``cross_process_lock`` at
    the default ``True``: the job's whole-run ``LOCK_EX`` on the same file made
    the SH acquire raise ``LakeBusyError`` (flock is per open file description)
    — every dashboard sweep died at preload with ``state='error'`` since G10
    landed (commit 3f32b3e).
    """
    from brutex.dashboard.pull_jobs import JOB_KIND_SWEEP
    from brutex.engine.sweep_singleflight import lake_read_guard

    guard_outcome: dict[str, bool] = {}

    def work(on_progress: ProgressHook) -> None:
        with lake_read_guard(tmp_path):  # cmd_sweep_all's G10 preload step
            guard_outcome["acquired"] = True
        on_progress("X", 1, False, False)

    job = _sweep_shaped_job(tmp_path)
    started = job.start(work, total=1, job_kind=JOB_KIND_SWEEP, cross_process_lock=False)
    assert started is True
    assert guard_outcome == {"acquired": True}  # G10 acquired — no self-conflict
    assert job.snapshot().state == STATE_DONE


def test_sweep_job_with_job_level_ex_lock_self_conflicts_g10(tmp_path: Path) -> None:
    """The INVERSE pin — the PRE-fix composition MUST self-conflict.

    With ``cross_process_lock=True`` (the route's old default) the job holds
    the lake pull flock EX for the whole run, and the worker's G10 ``LOCK_SH``
    acquire on the SAME file — a second open file description in the SAME
    process — is DENIED (flock(2) locks attach to the open file description).
    Pins the mechanism the fix removes from the route path, so the
    flock-per-OFD fact this whole DEC rests on can never silently drift.
    """
    from brutex.dashboard.pull_jobs import JOB_KIND_SWEEP
    from brutex.engine.sweep_singleflight import LakeBusyError, lake_read_guard

    outcome: dict[str, str] = {}

    def work(_on_progress: ProgressHook) -> None:
        try:
            with lake_read_guard(tmp_path):  # pragma: no cover — must NOT acquire
                outcome["acquired"] = "yes"
        except LakeBusyError as exc:
            outcome["error"] = str(exc)

    job = _sweep_shaped_job(tmp_path)
    started = job.start(work, total=1, job_kind=JOB_KIND_SWEEP, cross_process_lock=True)
    assert started is True  # the job STARTS — it dies at (the fake) preload
    assert "acquired" not in outcome
    assert "pull single-flight lock held exclusively" in outcome["error"]
    assert "refusing to preload sweep bars" in outcome["error"]


def test_sweep_job_lock_free_never_touches_the_pull_lock_file(tmp_path: Path) -> None:
    """A ``JOB_KIND_SWEEP`` start with ``cross_process_lock=False`` consults
    NEITHER lock factory and creates NO ``.locks/pull_singleflight.lock`` —
    at the JOB layer the file is never opened; the only toucher during a real
    sweep is cmd_sweep_all's own G10 guard (SH, preload-bounded)."""
    from brutex.dashboard.pull_jobs import JOB_KIND_SWEEP

    lake_calls: list[Path] = []
    account_calls: list[str] = []

    def lake_factory(root: Path) -> AbstractContextManager[None]:  # pragma: no cover
        lake_calls.append(root)
        return _SpyGuard()

    def account_factory(key: str) -> AbstractContextManager[None]:  # pragma: no cover
        account_calls.append(key)
        return _SpyGuard()

    captured: list[Callable[[], None]] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_capture_spawn(captured),
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lake_factory,
        account_singleflight_factory=account_factory,
        account_key_resolver=lambda: "key-A",
    )
    started = job.start(
        lambda on_progress: on_progress("X", 1, False, False),
        total=1,
        job_kind=JOB_KIND_SWEEP,
        cross_process_lock=False,
    )
    assert started is True
    # Probe BETWEEN start() and the worker run: no lock file materialised.
    assert not pull_singleflight_lock_path(tmp_path).exists()
    captured[0]()  # run the worker
    assert job.snapshot().state == STATE_DONE
    assert lake_calls == [] and account_calls == []
    assert not pull_singleflight_lock_path(tmp_path).exists()


def test_none_lake_root_disables_both_locks(tmp_path: Path) -> None:
    """A None lake root (dry-run/test) skips BOTH locks AND the key resolver."""
    resolver_calls: list[bool] = []

    def key_resolver() -> str | None:  # pragma: no cover — never called
        resolver_calls.append(True)
        return "key-A"

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: None,
        account_key_resolver=key_resolver,
    )
    assert job.start(lambda on_progress: on_progress("X", 1, False, False), total=1) is True
    assert resolver_calls == []


def test_account_release_is_best_effort_and_swallows_errors(tmp_path: Path) -> None:
    """A raising account-guard __exit__ never crashes the worker; the run completes."""

    class _BadExitGuard(AbstractContextManager[None]):
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> None:
            raise OSError("close failed")

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _SpyGuard(),
        account_singleflight_factory=lambda _key: _BadExitGuard(),
        account_key_resolver=lambda: "key-A",
    )
    assert job.start(lambda on_progress: on_progress("X", 1, False, False), total=1) is True
    assert job.snapshot().state == STATE_DONE
    assert job.running is False


def test_default_account_key_resolver_returns_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default resolver reads the AWS-only source and returns the api_key."""
    from types import SimpleNamespace

    import brutex.config.credentials as credentials_mod
    from brutex.dashboard.pull_jobs import (
        _default_account_key_resolver,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setattr(
        credentials_mod,
        "resolve_groww_aws_credentials",
        lambda: SimpleNamespace(api_key="aws-key", totp_secret="s"),
    )
    assert _default_account_key_resolver() == "aws-key"


def test_default_account_key_resolver_maps_credentials_error_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CredentialsError (no AWS) → None → the account lock is skipped, never a crash."""
    import brutex.config.credentials as credentials_mod
    from brutex.config.credentials_types import CredentialsError
    from brutex.dashboard.pull_jobs import (
        _default_account_key_resolver,  # pyright: ignore[reportPrivateUsage]
    )

    def _boom() -> object:
        raise CredentialsError("aws unreachable")

    monkeypatch.setattr(credentials_mod, "resolve_groww_aws_credentials", _boom)
    assert _default_account_key_resolver() is None


# ════════════════════════════════════════════════════════════════════════
# Cross-host pull LEASE wiring (DEC-ACCOUNT-LEASE-CROSSHOST-001)
# ════════════════════════════════════════════════════════════════════════


def test_lease_taken_last_released_first(tmp_path: Path) -> None:
    """ORDER pin: account → lake → lease on enter; lease → lake → account on release."""
    order: list[str] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _OrderedGuard("lake", order),
        account_singleflight_factory=lambda _key: _OrderedGuard("account", order),
        account_key_resolver=lambda: "key-A",
        lease_factory=lambda _root: _OrderedGuard("lease", order),
    )
    assert job.start(lambda on_progress: on_progress("X", 1, False, False), total=1) is True
    assert job.snapshot().state == STATE_DONE
    assert order == [
        "enter:account",
        "enter:lake",
        "enter:lease",
        "exit:lease",
        "exit:lake",
        "exit:account",
    ]


def test_lease_contention_rolls_back_both_flocks(tmp_path: Path) -> None:
    """A fresh FOREIGN-host lease refuses the pull and rolls BOTH flocks back (LIFO)."""
    order: list[str] = []
    spawned: list[Callable[[], None]] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=spawned.append,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _OrderedGuard("lake", order),
        account_singleflight_factory=lambda _key: _OrderedGuard("account", order),
        account_key_resolver=lambda: "key-A",
        lease_factory=lambda _root: _OrderedGuard("lease", order, raise_on_enter=True),
    )
    assert job.start(lambda _h: None, total=1) is False
    assert spawned == []  # never doubles the account rate
    assert order == ["enter:account", "enter:lake", "exit:lake", "exit:account"]
    assert job.running is False  # in-process flag rolled back
    snap = job.snapshot()
    assert snap.state == STATE_ERROR
    assert "PullAlreadyRunningError" in snap.detail


def test_default_lease_factory_refuses_a_fresh_foreign_lease(tmp_path: Path) -> None:
    """END-TO-END with the REAL default lease guard: a foreign lease refuses start."""
    import json
    import time

    from brutex.adapters.groww.pull_singleflight import pull_lease_path

    lease_path = pull_lease_path(tmp_path)
    lease_path.parent.mkdir(parents=True)
    lease_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "token": "peer-token",
                "host": "definitely-not-this-host",
                "pid": 1,
                "started_wall": time.time(),
                "wall_time": time.time(),  # FRESH heartbeat
            }
        )
    )
    spawned: list[Callable[[], None]] = []
    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=spawned.append,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _SpyGuard(),
        account_singleflight_factory=lambda _key: _SpyGuard(),
        account_key_resolver=lambda: "key-A",
        # lease_factory NOT injected — the production default pull_lease_guard runs.
    )
    assert job.start(lambda _h: None, total=1) is False
    assert spawned == []
    snap = job.snapshot()
    assert snap.state == STATE_ERROR
    assert "another host" in snap.detail


def test_default_lease_factory_writes_and_clears_the_lease(tmp_path: Path) -> None:
    """END-TO-END clean run with the REAL default lease guard: lease held then cleared."""
    from brutex.adapters.groww.pull_singleflight import pull_lease_path, read_pull_lease

    seen: dict[str, bool] = {}

    def work(_on_progress: ProgressHook) -> None:
        seen["lease_during_run"] = pull_lease_path(tmp_path).exists()

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _SpyGuard(),
        account_singleflight_factory=lambda _key: _SpyGuard(),
        account_key_resolver=lambda: "key-A",
    )
    assert job.start(work, total=1) is True
    assert seen["lease_during_run"] is True  # our lease was on disk during the run
    assert read_pull_lease(tmp_path) is None  # cleared on the clean finish


def test_lease_release_is_best_effort_and_swallows_errors(tmp_path: Path) -> None:
    """A raising lease-guard __exit__ never crashes the worker; the run completes."""

    class _BadExitGuard(AbstractContextManager[None]):
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> None:
            raise OSError("lease close failed")

    job = BackgroundPullJob(
        progress=PullProgress(clock=_ticking_clock()),
        spawn=_sync_spawn,
        lake_root_resolver=lambda: tmp_path,
        singleflight_factory=lambda _root: _SpyGuard(),
        account_singleflight_factory=lambda _key: _SpyGuard(),
        account_key_resolver=lambda: "key-A",
        lease_factory=lambda _root: _BadExitGuard(),
    )
    assert job.start(lambda on_progress: on_progress("X", 1, False, False), total=1) is True
    assert job.snapshot().state == STATE_DONE
